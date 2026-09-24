"""The watchdog fires on real incidents and stays quiet otherwise.

Neon 2026-09-21: "i want an eye on the script code project ... a third eye
after me you to moniotor the scraping crawling".

Every monitor is pinned to an incident this project actually had. The
silence test matters as much as the firing ones: a watchdog that cries wolf
on a normal run gets ignored, and then it is worse than not having one.
"""
from scan_watchdog import ScanState, consume, run_monitors, summary


def feed(events):
    s = ScanState()
    for e in events:
        consume(s, e)
    return s


def alerts_from(s, monitor=None):
    out = run_monitors(s, repeat=True)
    return [a for a in out if monitor is None or a.monitor == monitor]


# ── the incidents ────────────────────────────────────────────────────────


def test_a_cascading_failure_raises_an_ALARM():
    """ESPRESSO bookings #400-403, 2026-08-27: the portal signed the session
    out and the batch kept going, failing identically one at a time."""
    s = feed([{"event": "espresso.result", "status": "NO_SAVING"}] * 12
             + [{"event": "espresso.result", "status": "ERROR"}] * 6)
    a = alerts_from(s, "error-streak")
    assert a and a[0].level == "ALARM"
    assert "consecutive ERROR" in a[0].message


def test_a_short_error_run_only_warns():
    s = feed([{"event": "espresso.result", "status": "NO_SAVING"}] * 10
             + [{"event": "espresso.result", "status": "ERROR"}] * 3)
    a = alerts_from(s, "error-streak")
    assert a and a[0].level == "WARN"


def test_errors_that_are_not_consecutive_do_not_alarm():
    """Isolated failures are normal; a STREAK is the signal."""
    s = feed([{"event": "espresso.result", "status": "ERROR"},
              {"event": "espresso.result", "status": "NO_SAVING"}] * 8)
    assert not alerts_from(s, "error-streak")


def test_the_paid_in_full_surge_is_caught():
    """NCL, 2026-09-16: 113 of 143 bookings (79%) came back PAID_IN_FULL
    against a historical 14%. The run "succeeded" - it just answered the
    wrong question about almost every booking."""
    s = feed([{"event": "ncl.result", "status": "PAID_IN_FULL"}] * 113
             + [{"event": "ncl.result", "status": "NO_SAVING"}] * 30)
    a = alerts_from(s, "status-mix")
    assert a and "79%" in a[0].message


def test_silent_feature_nulls_raise_an_ALARM():
    """ESPRESSO's driver fields come from a regex over its Angular
    bootstrap. A portal restructure makes them silently NULL rather than
    raising - the columns would fill with nothing and nobody would know
    until a model was trained on air."""
    s = feed([{"event": "price_history.features", "sail_date": None}] * 40)
    a = alerts_from(s, "features")
    assert a and a[0].level == "ALARM"


def test_features_landing_correctly_are_silent():
    s = feed([{"event": "price_history.features", "sail_date": "2027-02-21"}] * 40)
    assert not alerts_from(s, "features")


def test_session_loss_and_failed_relogin_are_reported_differently():
    recovered = feed([{"event": "batch.session_expired_recovering"}])
    assert alerts_from(recovered, "session")[0].level == "WARN"

    gave_up = feed([{"event": "batch.session_recovery_gave_up"}])
    a = alerts_from(gave_up, "session")
    assert a[0].level == "ALARM" and "MFA" in a[0].message


def test_portal_refusals_are_surfaced_but_1241_is_not():
    """5108 "The VIFP number is incorrect." is a fixable booking problem.
    1241 "Option extension is not applicable to deposited bookings." is
    informational noise present on 20 of 23 bookings."""
    s = feed([{"event": "goccl.advisory", "code": "5108"}] * 6
             + [{"event": "goccl.advisory", "code": "1241"}] * 40)
    a = alerts_from(s, "advisories")
    assert a and "5108" in a[0].message and "1241" not in a[0].message


def test_only_informational_advisories_stay_silent():
    s = feed([{"event": "goccl.advisory", "code": "1241"}] * 40)
    assert not alerts_from(s, "advisories")


def test_a_run_getting_slower_is_flagged():
    s = feed([{"event": "espresso.timings", "total_ms": 5000}] * 10
             + [{"event": "espresso.timings", "total_ms": 20000}] * 10)
    a = alerts_from(s, "slowdown")
    assert a and "20.0s" in a[0].message


def test_structure_drift_is_an_ALARM():
    s = feed([{"event": "structure.drift"}])
    a = alerts_from(s, "drift")
    assert a and a[0].level == "ALARM"


# ── the silence that makes it trustworthy ────────────────────────────────


def test_a_HEALTHY_run_produces_no_alerts_at_all():
    """THE MOST IMPORTANT TEST. 81% of prices never move (measured across
    808 bookings), so a run that is almost entirely NO_SAVING is the NORMAL
    outcome. An earlier version monitored NO_SAVING at a 97% threshold and
    tripped on exactly this - which would teach the reader to ignore it."""
    s = ScanState()
    for _ in range(50):
        consume(s, {"event": "espresso.result", "status": "NO_SAVING"})
        consume(s, {"event": "price_history.features", "sail_date": "2027-02-21"})
        consume(s, {"event": "espresso.timings", "total_ms": 8000})
    assert run_monitors(s, repeat=True) == []


def test_a_small_sample_does_not_alert():
    """Thresholds need a population; 3 bookings prove nothing."""
    s = feed([{"event": "ncl.result", "status": "PAID_IN_FULL"}] * 3)
    assert not alerts_from(s, "status-mix")


def test_each_condition_alerts_once_not_every_tick():
    s = feed([{"event": "batch.session_expired_recovering"}])
    assert len(run_monitors(s)) == 1
    assert run_monitors(s) == []       # already reported


def test_a_monitor_that_raises_cannot_take_down_the_watchdog():
    """A watchdog that crashes the thing it watches is worse than none."""
    import scan_watchdog

    def broken(_state):
        raise RuntimeError("boom")

    original = scan_watchdog.MONITORS
    scan_watchdog.MONITORS = original + (broken,)
    try:
        out = run_monitors(feed([{"event": "espresso.result", "status": "ERROR"}]))
        assert any("monitor failed" in a.message for a in out)
    finally:
        scan_watchdog.MONITORS = original


def test_summary_reports_what_was_seen():
    s = feed([{"event": "espresso.result", "status": "OPTIMIZATION"}] * 2
             + [{"event": "espresso.timings", "total_ms": 4000}])
    text = summary(s)
    assert "2 bookings" in text and "OPTIMIZATION=2" in text


def test_unknown_events_are_ignored_not_fatal():
    s = feed([{"event": "something.new", "field": 1}, {}, {"event": None}])
    assert run_monitors(s, repeat=True) == []
