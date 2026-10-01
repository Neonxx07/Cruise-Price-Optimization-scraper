"""The watchdog fires on real incidents and stays quiet otherwise.

Neon 2026-09-21: "i want an eye on the script code project ... a third eye
after me you to moniotor the scraping crawling".

Every monitor is pinned to an incident this project actually had. The
silence test matters as much as the firing ones: a watchdog that cries wolf
on a normal run gets ignored, and then it is worse than not having one.
"""
from scan_watchdog import (CRASH_EVENT, ScanState, consume, run_monitors,
                           summary)


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
    s = feed([{"event": "batch.checking"}] * 2
             + [{"event": "espresso.result", "status": "OPTIMIZATION"}] * 2
             + [{"event": "espresso.timings", "total_ms": 4000}])
    text = summary(s)
    assert "2 started" in text and "OPTIMIZATION=2" in text
    assert "reported" not in text, "no split shown when the counts agree"


def test_summary_shows_the_split_when_bookings_never_report():
    """A run 458 deep used to print "144 bookings" and look barely begun,
    because most ESPRESSO bookings exit before any result line."""
    s = feed([{"event": "batch.checking"}] * 10
             + [{"event": "espresso.result", "status": "NO_SAVING"}] * 3)
    assert "10 started / 3 reported" in summary(s)


def test_unknown_events_are_ignored_not_fatal():
    s = feed([{"event": "something.new", "field": 1}, {}, {"event": None}])
    assert run_monitors(s, repeat=True) == []


# ── 2026-09-23: the watchdog was watching the wrong events ───────────────
#
# Both defects were found by asking a simple question of a LIVE run: is the
# watchdog watching the process, or the bookings? It watches neither - it
# tails a log file, and it was reading the wrong lines in it.


def test_a_cancelled_booking_is_seen_even_though_it_emits_no_result():
    """ESPRESSO exits early on CX and logs espresso.booking_cancelled, never
    espresso.result. m_cancellations reads results["CANCELLED"], so it could
    never fire. Eight real cancellations went unreported on 2026-09-23 -
    the one outcome Neon called mandatory to report."""
    s = feed([{"event": "espresso.booking_cancelled",
               "msg": "reservation status CX"}])
    assert s.results["CANCELLED"] == 1
    alerts = run_monitors(s)
    assert any(a.monitor == "cancelled" and a.level == "ALARM" for a in alerts)


def test_progress_is_a_booking_starting_not_a_booking_reporting():
    """Measured on the live run of 2026-09-23, which never stalled:

        gaps between *.result       max 26.8 min - 5 over the ALARM line
        gaps between batch.checking max  1.0 min - none over WARN

    Keying the idle timer on *.result alone would have raised five false
    "the scan looks stuck" ALARMs while a booking started every minute.
    """
    import time as _time
    s = feed([{"event": "batch.checking"}])
    s.last_progress = _time.monotonic() - 900       # 15 min since a result
    assert any(a.monitor == "stall" for a in run_monitors(s)), \
        "a genuine 15-minute silence must still alarm"

    s2 = feed([{"event": "espresso.result", "status": "NO_SAVING"}])
    s2.last_progress = _time.monotonic() - 900
    consume(s2, {"event": "batch.checking"})        # a booking starts
    assert not any(a.monitor == "stall" for a in run_monitors(s2)), \
        "a booking starting is progress - it must clear the stall timer"


def test_bookings_that_never_report_still_count_as_progress():
    """PAID_IN_FULL and WLT exit without a result line. On the live run that
    was 255 of 458 bookings - long stretches with no result at all."""
    s = feed([{"event": "batch.checking"}] * 50)
    assert s.started_count == 50
    assert not any(a.monitor == "stall" for a in run_monitors(s))


# ── navigation retries: delay that never becomes an error ───────────────


def test_a_high_retry_rate_alarms():
    """Measured live 2026-09-23: 50-87% of navigations retried, every one
    recovered, so the run looked perfectly healthy while paying for its
    navigations twice."""
    s = feed([{"event": "espresso.navigate_home"}] * 100
             + [{"event": "browser.navigate_retry"}] * 55)
    a = alerts_from(s, "nav-retries")
    assert a and a[0].level == "ALARM" and "55%" in a[0].message


def test_a_moderate_retry_rate_only_warns():
    s = feed([{"event": "espresso.navigate_home"}] * 100
             + [{"event": "browser.navigate_retry"}] * 20)
    a = alerts_from(s, "nav-retries")
    assert a and a[0].level == "WARN"


def test_an_ordinary_retry_rate_is_silent():
    """2026-09-22T16 ran at 2.3% and was fine. Alerting on that would
    teach the reader to ignore this monitor."""
    s = feed([{"event": "espresso.navigate_home"}] * 100
             + [{"event": "browser.navigate_retry"}] * 3)
    assert not alerts_from(s, "nav-retries")


def test_a_few_navigations_prove_nothing():
    """Two retries out of three navigations is 67% and means nothing."""
    s = feed([{"event": "espresso.navigate_home"}] * 3
             + [{"event": "browser.navigate_retry"}] * 2)
    assert not alerts_from(s, "nav-retries")


def test_retries_are_counted_for_any_cruise_line():
    s = feed([{"event": "ncl.navigate_home"}] * 50
             + [{"event": "goccl.navigate_home"}] * 50
             + [{"event": "browser.navigate_retry"}] * 50)
    assert s.nav_attempts == 100
    assert alerts_from(s, "nav-retries")[0].level == "ALARM"


# ── per-run reset, 2026-09-28 ────────────────────────────────────────────
#
# A watchdog process started on the 23rd was still running five days later,
# accumulating forever:
#
#     watchdog:  27% of navigations needed a retry (433 of 1609)
#     reality:    0% (0 of 14) on the run actually in progress
#
# It averaged a FIXED bug's 35% into a healthy run's 0%, and kept a
# "re-login FAILED - the scan stopped" ALARM on screen about a scan that had
# finished days earlier. A monitor describing all of history describes
# nothing in particular.


def test_a_new_batch_resets_the_rates():
    s = feed([{"event": "espresso.navigate_home"}] * 100
             + [{"event": "browser.navigate_retry"}] * 50
             + [{"event": "batch.checking", "index": 1}])
    assert s.nav_attempts == 0 and s.nav_retries == 0
    assert s.runs_seen == 1


def test_a_new_batch_clears_a_stale_session_alarm():
    """The five-day-old "re-login FAILED" is the exact case."""
    s = feed([{"event": "batch.session_recovery_gave_up"},
              {"event": "batch.checking", "index": 1}])
    assert not alerts_from(s, "session")


def test_mid_batch_bookings_do_not_reset():
    """Only index 1 is a boundary. Resetting on every booking would make
    every rate meaningless."""
    s = feed([{"event": "espresso.navigate_home"}] * 50
             + [{"event": "browser.navigate_retry"}] * 25
             + [{"event": "batch.checking", "index": 7}])
    assert s.nav_attempts == 50 and s.nav_retries == 25
    assert s.runs_seen == 0


def test_the_very_first_batch_does_not_count_as_a_reset():
    """Starting the watchdog before a scan must not report a phantom run."""
    s = feed([{"event": "batch.checking", "index": 1}])
    assert s.runs_seen == 0
    assert s.started_count == 1


def test_lifetime_totals_survive_the_reset():
    """Resetting the thresholds must not throw away what was seen."""
    s = feed([{"event": "espresso.result", "status": "NO_SAVING"}] * 4
             + [{"event": "batch.checking", "index": 1}])
    assert s.results.get("NO_SAVING", 0) == 0
    assert s.lifetime_results["NO_SAVING"] == 4


def test_crashes_survive_a_new_run():
    """A crash earlier in the session is still worth knowing about - it is a
    standing fact about the process, not a per-run rate."""
    s = feed([{"event": CRASH_EVENT, "error_type": "RuntimeError", "error": "x"},
              {"event": "batch.checking", "index": 1}])
    assert s.crashes
    assert any(a.monitor == "crash" for a in run_monitors(s))


def test_an_alarm_can_fire_again_in_the_next_run():
    """`fired` must reset or the first run's cancellation alarm silences the
    second run's."""
    s = feed([{"event": "espresso.booking_cancelled"}])
    assert any(a.monitor == "cancelled" for a in run_monitors(s))
    consume(s, {"event": "batch.checking", "index": 1})
    consume(s, {"event": "espresso.booking_cancelled"})
    assert any(a.monitor == "cancelled" for a in run_monitors(s))


# ── hung-window detection ────────────────────────────────────────────────


def test_a_hung_window_alarms_with_where_it_is_blocked():
    """The one failure a log-reader cannot see: a frozen UI writes nothing,
    so silence looks exactly like an idle scan."""
    s = ScanState()
    s.watched_pid = 4242
    s.window_hung = True
    s.hang_stack = ("Thread 5628 (idle)\n"
                    "    _on_login_check (gui\windows.py:657)\n"
                    "    _run (asyncio\events.py:94)")
    alert, = [a for a in run_monitors(s) if a.monitor == "hung"]
    assert alert.level == "ALARM"
    assert "4242" in alert.message
    assert "windows.py:657" in alert.message, "must name where it is blocked"


def test_a_responsive_window_is_silent():
    s = ScanState()
    s.watched_pid = 4242
    s.window_hung = False
    assert not [a for a in run_monitors(s) if a.monitor == "hung"]


def test_a_dead_process_is_not_also_reported_as_hung():
    """One failure, one alert. m_process already covers death."""
    s = ScanState()
    s.watched_pid = 4242
    s.process_gone = True
    s.window_hung = True
    assert not [a for a in run_monitors(s) if a.monitor == "hung"]


def test_the_hang_probe_never_raises():
    from scan_watchdog import window_is_hung
    assert window_is_hung(999999) in (True, False)


# ── notifications, 2026-09-29 ────────────────────────────────────────────
#
# Every alert used to land in a terminal nobody was watching. That is how a
# crash killed an overnight scan on 2026-09-22 and it sat dead from 01:19
# until 14:14 - the watchdog had the evidence and no way to say so.


def test_only_alarms_are_notified():
    """A notifier that fires on every minor thing gets muted, and a muted
    notifier is worse than none."""
    from scan_watchdog import Alert, notify
    sent = notify([Alert("WARN", "nav-retries", "20% needed a retry")])
    assert sent == 0


def test_nothing_to_say_sends_nothing():
    from scan_watchdog import notify
    assert notify([]) == 0


def test_a_broken_notifier_never_takes_down_the_watchdog():
    """A notification failing must not kill the thing watching the scan."""
    from scan_watchdog import Alert, notify
    assert notify([Alert("ALARM", "crash", "boom")],
                  target="not-a-real-scheme://nowhere") == 0


def test_the_default_target_keeps_data_on_the_machine():
    """`windows://` is a local desktop toast - no API key, no account, no
    internet. Anything off-machine sends data somewhere and must never be
    the default."""
    import inspect

    import scan_watchdog
    src = inspect.getsource(scan_watchdog.notify)
    assert 'target: str = "windows://"' in src


# ── closing the GUI is not a failure, 2026-09-29 ─────────────────────────
#
# Neon: "python notification keeps firing althouhgh the gui is closed ...
# infact close the notifications at all we do not need it we just need a
# watcher watching the ciode script project monotiring as a third eye that
# everything is running and functioning properly."
#
# It fired BECAUSE he closed the GUI: m_process alarmed on the watched
# process disappearing, which is exactly what a deliberate shutdown looks
# like from outside. A monitor that shouts when you close an application
# trains you to dismiss it, and then it is no use when something real
# breaks.


def test_a_clean_shutdown_does_not_alarm():
    """The GUI announces its own exit. That is not a crash."""
    s = feed([{"event": "gui.shutdown_complete", "seconds": 1.9}])
    s.watched_pid = 4242
    s.process_gone = True
    assert not [a for a in run_monitors(s) if a.monitor == "process"]


def test_releasing_the_instance_lock_also_counts_as_a_clean_exit():
    s = feed([{"event": "single_instance.released", "scope": "cruiseintel_gui"}])
    s.watched_pid = 4242
    s.process_gone = True
    assert not [a for a in run_monitors(s) if a.monitor == "process"]


def test_a_process_that_vanishes_WITHOUT_shutting_down_still_alarms():
    """The case the monitor exists for: a crash or a kill mid-scan, with
    bookings still queued."""
    s = feed([{"event": "batch.checking", "index": 5}])
    s.watched_pid = 4242
    s.process_gone = True
    alerts = [a for a in run_monitors(s) if a.monitor == "process"]
    assert alerts and alerts[0].level == "ALARM"
    assert "did not shut down cleanly" in alerts[0].message


def test_notifications_are_off_unless_asked_for():
    """"close the notifications at all we do not need it". The watchdog
    still prints - it just does not push anything at the operator."""
    import scan_watchdog
    parser_src = __import__("inspect").getsource(scan_watchdog.main)
    assert '"--notify", default=""' in parser_src
