"""Unhandled errors must reach the log, and the watchdog must react to them.

THE INCIDENT. On 2026-09-22 a RuntimeError destroyed a running 721-booking
ESPRESSO scan overnight::

    RuntimeError: Cannot enter into task Task-17 <_on_start at :961>
    while another task Task-546 <_on_start at :898> is being executed
    Task was destroyed but it is pending!

Counted afterwards:

    "Cannot enter into task" in terminal stdout   : 4
    "Cannot enter into task" in data/cruiseintel.log: 0

gui.main._handle_async_exception printed to stdout and never logged, so
scan_watchdog - which parses JSON log lines - could not see the one error
that mattered. The dead scan sat unnoticed from 01:19 until 14:14.

These tests cover both halves: the error reaching the log, and the watchdog
reacting to it.
"""

import asyncio
import json
import sys
import threading

import pytest

from scan_watchdog import (
    CRASH_EVENT as WATCHDOG_CRASH_EVENT,
    ScanState,
    consume,
    find_scan_process,
    process_is_alive,
    run_monitors,
)
from utils.logging import (
    CRASH_EVENT,
    asyncio_exception_handler,
    install_crash_handlers,
    log_crash,
    setup_logging,
)


@pytest.fixture
def crash_log(tmp_path):
    """A real log file, configured exactly as the app configures its own."""
    path = tmp_path / "cruiseintel.log"
    setup_logging("INFO", str(path))

    def read():
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return [r for r in out if r.get("event") == CRASH_EVENT]

    return read


# ── the error reaches the log ────────────────────────────────────────────


def test_an_asyncio_crash_is_logged_with_its_traceback(crash_log):
    """The exact shape that killed the scan."""
    async def main():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(asyncio_exception_handler)
        loop.call_exception_handler({
            "message": "Exception in callback Task.task_wakeup()",
            "exception": RuntimeError("Cannot enter into task Task-17"),
        })

    asyncio.run(main())

    records = crash_log()
    assert len(records) == 1
    rec = records[0]
    assert rec["source"] == "asyncio"
    assert rec["error_type"] == "RuntimeError"
    assert "Cannot enter into task" in rec["error"]
    assert rec.get("exception"), "the traceback must survive to disk"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_thread_dying_silently_is_logged(crash_log):
    """A keepalive thread dying is how a session expires with nothing
    appearing to be wrong."""
    original = threading.excepthook
    try:
        install_crash_handlers("test")

        def boom():
            raise ValueError("keepalive thread died")

        t = threading.Thread(target=boom, name="keepalive")
        t.start()
        t.join()
    finally:
        threading.excepthook = original

    rec, = crash_log()
    assert rec["error_type"] == "ValueError"
    assert rec["thread"] == "keepalive"


def test_a_main_thread_crash_is_logged_and_still_prints(crash_log):
    """Chained, not replaced - the stderr output that existed before must
    keep working."""
    original = sys.excepthook
    called = []
    sys.excepthook = lambda *a: called.append(a)
    try:
        install_crash_handlers("test")
        try:
            raise KeyError("missing selector")
        except KeyError as exc:
            sys.excepthook(KeyError, exc, exc.__traceback__)
    finally:
        sys.excepthook = original

    rec, = crash_log()
    assert rec["error_type"] == "KeyError"
    assert called, "the previous excepthook must still be called"


def test_the_crash_logger_never_raises(crash_log):
    """It runs from excepthooks. Raising there would replace a diagnosable
    failure with an undiagnosable one."""
    class Awkward(Exception):
        def __str__(self):
            raise RuntimeError("even __str__ is broken")

    log_crash("test", Awkward())        # must not propagate


def test_a_crash_with_no_exception_object_is_still_recorded(crash_log):
    """asyncio hands over a context with no "exception" key for some
    failures - message only. Missing is not nothing."""
    asyncio_exception_handler(
        asyncio.new_event_loop(),
        {"message": "Task was destroyed but it is pending!"},
    )
    rec, = crash_log()
    assert "destroyed" in rec["message"]


# ── the watchdog reacts ──────────────────────────────────────────────────


def test_the_watchdog_alarms_on_the_first_crash():
    s = ScanState()
    consume(s, {"event": CRASH_EVENT, "source": "asyncio",
                "error_type": "RuntimeError",
                "error": "Cannot enter into task Task-17",
                "task": "Task-17"})
    alerts = [a for a in run_monitors(s) if a.monitor == "crash"]
    assert alerts and alerts[0].level == "ALARM"
    assert "RuntimeError" in alerts[0].message
    assert "Task-17" in alerts[0].message


def test_the_alarm_says_where_to_find_the_traceback():
    """"figure it out" is the point - the alert must lead somewhere."""
    s = ScanState()
    consume(s, {"event": CRASH_EVENT, "error_type": "ValueError", "error": "x"})
    alert, = [a for a in run_monitors(s) if a.monitor == "crash"]
    assert "crash.unhandled" in alert.message
    assert "cruiseintel.log" in alert.message


def test_earlier_crashes_are_counted_not_lost():
    s = ScanState()
    for i in range(4):
        consume(s, {"event": CRASH_EVENT, "error_type": "E", "error": f"e{i}"})
    alert, = [a for a in run_monitors(s) if a.monitor == "crash"]
    assert "e3" in alert.message and "+3 earlier" in alert.message


def test_the_crash_buffer_is_bounded():
    """A crash loop must not grow the watchdog's memory without limit."""
    s = ScanState()
    for _ in range(500):
        consume(s, {"event": CRASH_EVENT, "error_type": "E", "error": "x"})
    assert len(s.crashes) == 200


def test_the_watchdog_constant_matches_the_logger():
    """The watchdog duplicates CRASH_EVENT so it stays stdlib-only. If the
    logger's name changes, the watchdog goes silently blind."""
    assert WATCHDOG_CRASH_EVENT == CRASH_EVENT


def test_a_healthy_run_still_produces_no_crash_alert():
    s = ScanState()
    for _ in range(50):
        consume(s, {"event": "espresso.result", "status": "NO_SAVING"})
    assert not [a for a in run_monitors(s, repeat=True) if a.monitor == "crash"]


# ── the process ──────────────────────────────────────────────────────────


def test_a_dead_process_is_an_ALARM():
    s = ScanState()
    s.watched_pid = 999999
    s.process_gone = True
    alert, = [a for a in run_monitors(s) if a.monitor == "process"]
    assert alert.level == "ALARM" and "999999" in alert.message


def test_a_live_process_is_silent():
    s = ScanState()
    s.watched_pid = 999999
    s.process_gone = False
    assert not [a for a in run_monitors(s) if a.monitor == "process"]


def test_no_process_being_watched_is_silent_not_alarming():
    """Watching the log alone is a valid mode. It must not read as death."""
    s = ScanState()
    assert s.watched_pid is None
    assert not [a for a in run_monitors(s) if a.monitor == "process"]


def test_liveness_is_only_false_when_we_are_sure():
    """A false "the scan is GONE" during a real scan makes the tool
    ignorable, and an ignored watchdog is worse than none."""
    import os
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(999999) is False


def test_finding_the_scan_process_never_raises():
    """It runs on a machine where process inspection may be blocked."""
    result = find_scan_process()
    assert result is None or isinstance(result, int)
