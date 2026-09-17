"""Catch a run that has silently gone wrong, while it is still running.

Neon 2026-09-16: "what can we implement to make everything concrete?"

WHY THIS EXISTS. Twice this week a scan ran for hours producing nothing but
wrong answers, and nobody could tell until it finished:

  * 2026-09-15  NCL failed all 135 bookings with an UnboundLocalError that
                had been fixed on disk three hours earlier. Ran 15:06 to
                16:58 before being stopped.
  * 2026-09-16  NCL reported "no saving, price unchanged" on every booking
                because an unreadable price was being coerced to 0.

Both were invisible from the outside: the run looked busy and produced
plausible rows. That is the classic scraper silent failure - the site or
the code changes, the spider keeps running, and the damage is only found
later.

PATTERNS ADOPTED FROM SPIDERMON (github.com/scrapinghub/spidermon, Zyte's
battle-tested Scrapy monitor). Spidermon itself is Scrapy-coupled and this
project is on Playwright, so the patterns are reimplemented rather than
installed:

  1. compare a run against PREVIOUS runs, not against a fixed threshold
  2. run the checks PERIODICALLY, so a bad run is caught early
  3. canary items whose answer is known, so a change means WE broke

EVERY METRIC HERE WAS VALIDATED AGAINST TWO MONTHS OF REAL HISTORY FIRST.
One candidate was thrown out for failing that test: "% of priced bookings
whose price moved" looked ideal and does NOT work - NCL sat at 50%, 68.5%
and 65% across good and bad runs alike, and ESPRESSO reads 100% every run
by construction. A metric that cannot separate a known-good run from a
known-bad one is worse than none, because it manufactures confidence.

READ-ONLY. Never writes to the database.

    python run_health.py                 # today, every line
    python run_health.py NCL             # one line
    python run_health.py NCL 2026-09-16  # a specific day
"""
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DB = HERE / "cruise_intel.db"

#: A status mix this far from its own historical median is suspicious.
#: 0.30 = thirty percentage points, chosen because the NCL collapse moved
#: paid-in-full by 63 points (14% -> 77%) while ordinary run-to-run drift
#: stayed under 15.
STATUS_SHIFT_ALERT = 0.30

#: A canary needs enough history to be meaningful.
CANARY_MIN_OBSERVATIONS = 6


def _conn():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# ── canaries ────────────────────────────────────────────────────────


def find_canaries(con, cruise_line: str, before: str | None = None) -> list[dict]:
    """Bookings whose price has never moved across a long history.

    If one of these suddenly reports a different price, the market did not
    change - WE did. That makes them the cheapest silent-failure detector
    available, and the history to seed them already exists: eight ESPRESSO
    bookings have 11-12 observations spanning two months at exactly one
    price (3000067 at $7,656.00, 3000073 at $1,894.10, 3000061 at
    $4,004.66, and more).
    """
    # `before` EXCLUDES the day being checked, and it is not optional in
    # practice. Without it a canary that moves today has two distinct
    # prices, drops out of this very query, and the alert never fires -
    # the detector erases its own evidence. Found by its own test.
    rows = con.execute(
        """SELECT booking_id, COUNT(*) obs, COUNT(DISTINCT total) distinct_totals,
                  MIN(total) price, MIN(date(checked_at)) first_seen,
                  MAX(date(checked_at)) last_seen
           FROM price_history
           WHERE cruise_line = ? AND (? IS NULL OR date(checked_at) < ?)
           GROUP BY booking_id
           HAVING obs >= ? AND distinct_totals = 1 AND price > 0
           ORDER BY obs DESC""",
        (cruise_line, before, before, CANARY_MIN_OBSERVATIONS)).fetchall()
    return [dict(r) for r in rows]


def check_canaries(con, cruise_line: str, day: str) -> list[str]:
    """Did any canary's price change on this run? Returns alert lines."""
    alerts = []
    for canary in find_canaries(con, cruise_line, before=day):
        today = con.execute(
            """SELECT total FROM price_history
               WHERE booking_id = ? AND cruise_line = ? AND date(checked_at) = ?
               ORDER BY id DESC LIMIT 1""",
            (canary["booking_id"], cruise_line, day)).fetchone()
        if not today:
            continue
        if abs((today["total"] or 0) - canary["price"]) > 0.01:
            alerts.append(
                f"CANARY MOVED: {canary['booking_id']} was ${canary['price']:,.2f} "
                f"on every one of {canary['obs']} previous observations "
                f"({canary['first_seen']} to {canary['last_seen']}), now "
                f"${today['total']:,.2f} - suspect the scraper, not the market")
    return alerts


# ── status mix, compared against this line's own history ────────────


def status_mix(con, cruise_line: str, day: str) -> tuple[Counter, int]:
    rows = con.execute(
        """SELECT status FROM bookings
           WHERE cruise_line = ? AND date(created_at) = ?""",
        (cruise_line, day)).fetchall()
    return Counter(r["status"] for r in rows), len(rows)


def check_status_mix(con, cruise_line: str, day: str) -> list[str]:
    """Flag a status whose share has moved far from its own history.

    THE METRIC THAT CAUGHT THE REAL FAILURE. NCL's paid-in-full share went
    14% -> 77% between 28 Aug and 16 Sep. Price-movement percentage, which
    seemed the obvious choice, could not tell those two runs apart at all.
    """
    today, total = status_mix(con, cruise_line, day)
    if total < 20:
        return []

    history: dict[str, list[float]] = {}
    for r in con.execute(
            """SELECT date(created_at) d, status, COUNT(*) n FROM bookings
               WHERE cruise_line = ? AND date(created_at) < ?
               GROUP BY d, status""", (cruise_line, day)):
        history.setdefault(r["d"], []).append((r["status"], r["n"]))
    if len(history) < 2:
        return []

    shares: dict[str, list[float]] = {}
    for _day, entries in history.items():
        n = sum(c for _s, c in entries)
        if n < 20:
            continue
        for status, count in entries:
            shares.setdefault(status, []).append(count / n)
    if not shares:
        return []

    alerts = []
    seen = set(today) | set(shares)
    for status in sorted(seen):
        now = today.get(status, 0) / total
        past = shares.get(status) or [0.0]
        # median, padded so a status absent from some runs counts as 0
        padded = past + [0.0] * (len(history) - len(past))
        base = statistics.median(padded)
        if abs(now - base) >= STATUS_SHIFT_ALERT:
            direction = "UP" if now > base else "DOWN"
            alerts.append(
                f"STATUS SHIFT {direction}: {status} is {now*100:.0f}% of this run "
                f"but {base*100:.0f}% historically "
                f"({today.get(status, 0)}/{total} bookings)")
    return alerts


# ── everything a run produced that is not a verdict ─────────────────


def check_dead_weight(con, cruise_line: str, day: str) -> list[str]:
    """A run that produced no usable answer at all."""
    alerts = []
    row = con.execute(
        """SELECT COUNT(*) n,
                  SUM(status = 'ERROR') errors,
                  SUM(old_total > 0 AND new_total > 0) priced
           FROM bookings WHERE cruise_line = ? AND date(created_at) = ?""",
        (cruise_line, day)).fetchone()
    n = row["n"] or 0
    if n < 20:
        return []
    if (row["errors"] or 0) / n >= 0.50:
        alerts.append(
            f"ERROR STORM: {row['errors']}/{n} bookings errored "
            f"({(row['errors'] or 0)/n*100:.0f}%) - the 2026-09-15 NCL run "
            f"looked exactly like this")
    if (row["priced"] or 0) == 0:
        alerts.append(
            f"NOTHING PRICED: {n} bookings checked and not one produced both "
            f"an old and a new total")
    return alerts


# ── report ──────────────────────────────────────────────────────────


def check_line(con, cruise_line: str, day: str) -> list[str]:
    return (check_dead_weight(con, cruise_line, day)
            + check_status_mix(con, cruise_line, day)
            + check_canaries(con, cruise_line, day))


def main() -> None:
    lines = ([sys.argv[1].upper()] if len(sys.argv) > 1
             else ["ESPRESSO", "NCL", "GOCCL", "MSC"])
    day = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")

    if not DB.exists():
        print(f"database not found at {DB}")
        return

    con = _conn()
    print("=" * 74)
    print(f"RUN HEALTH — {day}")
    print("=" * 74)
    total_alerts = 0
    for line in lines:
        mix, n = status_mix(con, line, day)
        if not n:
            print(f"\n  {line:<9} no bookings recorded")
            continue
        alerts = check_line(con, line, day)
        total_alerts += len(alerts)
        flag = "OK" if not alerts else f"{len(alerts)} ALERT(S)"
        print(f"\n  {line:<9} {n:>4} bookings   {flag}")
        for status, count in mix.most_common(5):
            print(f"       {count:>4}  {status}")
        canaries = find_canaries(con, line, before=day)
        print(f"       canaries available: {len(canaries)}")
        for a in alerts:
            print(f"    !! {a}")
    con.close()
    print("\n" + "-" * 74)
    print(f"  {total_alerts} alert(s)")


if __name__ == "__main__":
    main()
