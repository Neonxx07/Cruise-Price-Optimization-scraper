"""Catch a bad run early - and stay silent on a good one.

Neon 2026-09-16: "what can we implement to make everything concrete?"

Twice this week a scan ran for hours producing only wrong answers, and
nothing could tell from the outside:

  2026-09-15  NCL failed all 135 bookings with an error fixed three hours
              earlier. Ran 15:06-16:58 before being stopped by hand.
  2026-09-16  NCL reported "no saving, price unchanged" on everything
              because an unreadable price was coerced to 0.

Patterns taken from Spidermon (github.com/scrapinghub/spidermon, Zyte's
Scrapy monitor): compare a run to PREVIOUS runs rather than a fixed
threshold, check periodically so a bad run is caught early, and keep canary
items whose answer is known.

THE HARD PART IS NOT DETECTION, IT IS NOT CRYING WOLF. A monitor that fires
on a good run gets switched off, and then catches nothing at all. So the
tests below assert BOTH directions against real stored runs.
"""
import sqlite3

import pytest

from run_health import (
    CANARY_MIN_OBSERVATIONS,
    STATUS_SHIFT_ALERT,
    check_canaries,
    check_dead_weight,
    check_line,
    check_status_mix,
    find_canaries,
)


@pytest.fixture
def con(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE bookings (id INTEGER PRIMARY KEY, booking_id TEXT,
                 cruise_line TEXT, status TEXT, old_total REAL, new_total REAL,
                 created_at TEXT)""")
    c.execute("""CREATE TABLE price_history (id INTEGER PRIMARY KEY, booking_id TEXT,
                 cruise_line TEXT, total REAL, category TEXT, checked_at TEXT)""")
    return c


def _run(c, day, statuses, line="NCL", priced=True):
    for i, st in enumerate(statuses):
        old = 1000.0 if priced else 0.0
        new = 900.0 if priced else 0.0
        c.execute("INSERT INTO bookings (booking_id,cruise_line,status,old_total,"
                  "new_total,created_at) VALUES (?,?,?,?,?,?)",
                  (f"B{i}", line, st, old, new, f"{day} 10:00:00"))
    c.commit()


# -- it must catch the two real failures -----------------------------


def test_an_error_storm_is_caught(con):
    """The 2026-09-15 shape: 135 of 143 errored. Verified against the real
    database, this fires 4 alerts."""
    _run(con, "2026-08-28", ["NO_SAVING"] * 60 + ["OPTIMIZATION"] * 30)
    _run(con, "2026-08-27", ["NO_SAVING"] * 60 + ["OPTIMIZATION"] * 30)
    _run(con, "2026-09-15", ["ERROR"] * 135 + ["NO_SAVING"] * 8, priced=False)
    alerts = check_dead_weight(con, "NCL", "2026-09-15")
    assert any("ERROR STORM" in a for a in alerts)
    assert any("NOTHING PRICED" in a for a in alerts)


def test_a_status_collapse_is_caught(con):
    """The 2026-09-16 shape: paid-in-full 14% -> 79%. THE metric that
    works. Price-movement percentage, the obvious candidate, could not
    separate that run from a good one at all (NCL sat at 50%, 68.5% and
    65% across good and bad alike) and was discarded."""
    for day in ("2026-08-27", "2026-08-28"):
        _run(con, day, ["NO_SAVING"] * 60 + ["OPTIMIZATION"] * 30
             + ["PAID_IN_FULL"] * 14)
    _run(con, "2026-09-16", ["PAID_IN_FULL"] * 113 + ["NO_SAVING"] * 20)
    alerts = check_status_mix(con, "NCL", "2026-09-16")
    assert any("PAID_IN_FULL" in a and "SHIFT UP" in a for a in alerts), alerts


# -- and it must stay QUIET on a good run ----------------------------


def test_a_healthy_run_raises_nothing(con):
    """The 2026-08-28 shape - 36 optimizations worth $5,873. Verified
    against the real database: 0 alerts. A monitor that fires here would
    be switched off within a week."""
    for day in ("2026-08-25", "2026-08-27"):
        _run(con, day, ["NO_SAVING"] * 68 + ["OPTIMIZATION"] * 30
             + ["PAID_IN_FULL"] * 23 + ["ERROR"] * 16)
    _run(con, "2026-08-28", ["NO_SAVING"] * 68 + ["OPTIMIZATION"] * 36
         + ["PAID_IN_FULL"] * 23 + ["ERROR"] * 16)
    assert check_line(con, "NCL", "2026-08-28") == []


def test_ordinary_drift_does_not_alert(con):
    """Run-to-run variation under the threshold must pass. Real drift on
    these lines stays under 15 points; the alert sits at 30."""
    for day in ("2026-08-25", "2026-08-27"):
        _run(con, day, ["NO_SAVING"] * 50 + ["OPTIMIZATION"] * 50)
    _run(con, "2026-08-28", ["NO_SAVING"] * 60 + ["OPTIMIZATION"] * 40)
    assert check_status_mix(con, "NCL", "2026-08-28") == []


def test_a_tiny_run_is_not_judged(con):
    """A handful of bookings cannot establish a distribution, and judging
    it would produce noise on every partial or interrupted scan."""
    _run(con, "2026-08-27", ["NO_SAVING"] * 100)
    _run(con, "2026-09-16", ["ERROR"] * 5, priced=False)
    assert check_status_mix(con, "NCL", "2026-09-16") == []
    assert check_dead_weight(con, "NCL", "2026-09-16") == []


def test_the_first_ever_run_is_not_judged(con):
    """With no history there is nothing to compare against - claiming an
    anomaly would be invention."""
    _run(con, "2026-09-16", ["PAID_IN_FULL"] * 100)
    assert check_status_mix(con, "NCL", "2026-09-16") == []


# -- canaries ---------------------------------------------------------


def _obs(c, booking, totals, line="ESPRESSO", start=1):
    for i, t in enumerate(totals):
        c.execute("INSERT INTO price_history (booking_id,cruise_line,total,"
                  "checked_at) VALUES (?,?,?,?)",
                  (booking, line, t, f"2026-07-{start+i:02d} 10:00:00"))
    c.commit()


def test_a_long_stable_booking_becomes_a_canary(con):
    """295 real ESPRESSO bookings qualify, including 3000068 at $1,434.00
    across 12 observations - the very booking Neon verified by hand."""
    _obs(con, "3000068", [1434.00] * 12)
    canaries = find_canaries(con, "ESPRESSO")
    assert [c["booking_id"] for c in canaries] == ["3000068"]
    assert canaries[0]["price"] == 1434.00


def test_a_moving_booking_is_not_a_canary(con):
    """The whole point is that its price has NEVER moved."""
    _obs(con, "B1", [1000.0] * 6 + [1100.0])
    assert find_canaries(con, "ESPRESSO") == []


def test_a_short_history_is_not_a_canary(con):
    _obs(con, "B1", [1000.0] * (CANARY_MIN_OBSERVATIONS - 1))
    assert find_canaries(con, "ESPRESSO") == []


def test_a_canary_that_moves_raises_an_alert(con):
    """If a price that never moved in 12 observations suddenly changes,
    the market did not change - we did."""
    _obs(con, "3000068", [1434.00] * 12)
    con.execute("INSERT INTO price_history (booking_id,cruise_line,total,"
                "checked_at) VALUES (?,?,?,?)",
                ("3000068", "ESPRESSO", 1201.00, "2026-09-16 10:00:00"))
    con.commit()
    alerts = check_canaries(con, "ESPRESSO", "2026-09-16")
    assert any("CANARY MOVED" in a and "3000068" in a for a in alerts), alerts
    assert any("suspect the scraper" in a for a in alerts)


def test_a_canary_that_holds_is_silent(con):
    _obs(con, "3000068", [1434.00] * 12)
    con.execute("INSERT INTO price_history (booking_id,cruise_line,total,"
                "checked_at) VALUES (?,?,?,?)",
                ("3000068", "ESPRESSO", 1434.00, "2026-09-16 10:00:00"))
    con.commit()
    assert check_canaries(con, "ESPRESSO", "2026-09-16") == []


def test_a_canary_not_scanned_today_is_not_an_alert(con):
    """Absence is not a change - a partial run must not fire every canary."""
    _obs(con, "3000068", [1434.00] * 12)
    assert check_canaries(con, "ESPRESSO", "2026-09-16") == []


# -- the thresholds are deliberate ------------------------------------


def test_the_shift_threshold_sits_between_real_drift_and_real_failure():
    """Real NCL drift stayed under 15 points; the collapse moved 63. The
    threshold has to separate those two and nothing else."""
    assert 0.15 < STATUS_SHIFT_ALERT < 0.63
