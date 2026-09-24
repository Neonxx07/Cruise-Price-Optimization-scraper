"""Closing the app hung, and a clean close reported failure.

Neon 2026-09-22: "when i press quite or close it stucks can u double check
everything and make sure it is fixed and the closing is not hunning or
stucking".

TWO DEFECTS, both visible in the launch log:

  1. HANG. _shutdown_all closed panels SEQUENTIALLY and each one waited up
     to 60 seconds for its scan to stop - four tabs could take four minutes.
     The wait is real, not theoretical: a batch deliberately finishes its
     CURRENT booking before stopping, and a measured ESPRESSO booking takes
     ~30s (espresso.timings total_ms=29902).

  2. FALSE FAILURE. A clean close - "gui.shutdown_complete" was the last
     line written - exited with code 1, so every ordinary quit looked like
     a crash.
"""
import inspect

from gui.main import main as gui_main
from gui.windows import CruiseLinePanel, MainWindow


def test_panels_are_closed_concurrently_not_one_after_another():
    src = inspect.getsource(MainWindow._shutdown_all)
    assert "asyncio.gather" in src
    assert "for line, panel in self.panels.items():\n            try:" not in src


def test_shutdown_has_a_hard_ceiling():
    """The window must always close. A browser that will not shut down
    cleanly is worth a bounded wait - session state is saved by then, and
    Chromium exits with the process."""
    src = inspect.getsource(MainWindow._shutdown_all)
    assert "asyncio.wait_for" in src
    assert "SHUTDOWN_TIMEOUT_SECONDS" in src
    assert isinstance(MainWindow.SHUTDOWN_TIMEOUT_SECONDS, int)


def test_the_ceiling_still_allows_a_booking_to_finish():
    """A batch stops after its current booking; ~30s measured on ESPRESSO.
    A ceiling below that would routinely kill a browser mid-save."""
    assert MainWindow.SHUTDOWN_TIMEOUT_SECONDS >= 35


def test_the_per_panel_wait_fits_inside_the_app_ceiling():
    """It was 60s - longer than the whole app's budget - which is what made
    four sequential tabs feel like a freeze."""
    src = inspect.getsource(CruiseLinePanel.shutdown)
    assert "range(70)" in src          # ~35s
    assert "range(120)" not in src     # the old ~60s


def test_the_panel_ordering_rule_is_preserved():
    """Load-bearing and unchanged: stop the scan BEFORE closing the browser.
    Closing one out from under an in-flight check_booking makes
    booking_service treat it as a crash and start a SECOND browser while the
    app is trying to quit."""
    src = inspect.getsource(CruiseLinePanel.shutdown)
    assert src.index("stop_processing") < src.index("close_live_session")


def test_a_timeout_is_reported_not_swallowed():
    src = inspect.getsource(MainWindow._shutdown_all)
    assert "gui.shutdown_timed_out" in src
    assert "still_busy" in src


def test_the_user_sees_the_wait_progressing():
    """A close that legitimately takes 30 seconds is indistinguishable from
    a hang if nothing on screen moves."""
    assert hasattr(MainWindow, "_tick_shutdown_message")
    src = inspect.getsource(MainWindow.closeEvent)
    assert "_shutdown_ticker" in src


def test_a_clean_shutdown_exits_zero():
    src = inspect.getsource(gui_main)
    assert "return loop.run_forever()" not in src
    assert "return 0" in src


def test_the_second_close_click_is_still_ignored():
    """Accepting the close on a second click let Qt stop the loop before
    browser sessions were saved - orphaned Chromium and a forced re-login
    next launch."""
    src = inspect.getsource(MainWindow.closeEvent)
    assert "self._shutting_down" in src
    assert "event.ignore()" in src


# ── the process itself would not exit ────────────────────────────────────
#
# Neon 2026-09-22, AFTER the concurrent-teardown fix: "same quiting issue
# it is not closing it is still hanging".
#
# The log showed teardown was never the problem:
#
#     18:14:19  gui.panel_shutdown_ok  NCL / GOCCL / MSC
#     18:14:19  browser.session_saved  ESPRESSO
#     18:14:20  browser.stopped        ESPRESSO
#     18:14:20  gui.shutdown_complete  seconds=0.7
#
# 0.7 seconds, everything saved - and the Python process was STILL ALIVE
# thirteen minutes later, with 13 Chromium processes behind it. quit()
# returns, the window goes, and the interpreter simply never exits: the
# qasync loop is stopped from inside one of its own callbacks, and what is
# left (pending tasks, the thread-pool executor behind asyncio.to_thread,
# Playwright's subprocess transports) holds the process up.


def test_the_loop_is_stopped_not_just_the_qt_app():
    src = inspect.getsource(MainWindow._shutdown_all)
    assert "app.quit()" in src
    assert "get_running_loop().stop()" in src


def _shutdown_code() -> str:
    """_shutdown_all with comments stripped.

    The comments deliberately quote the OLD broken calls to record what went
    wrong, so a naive substring check reads them and passes on prose.
    """
    src = inspect.getsource(MainWindow._shutdown_all)
    return chr(10).join(l for l in src.splitlines() if not l.strip().startswith("#"))


def test_there_is_a_force_exit_backstop():
    """A desktop app that will not close is worse than a blunt exit."""
    assert hasattr(MainWindow, "_force_exit")
    code = _shutdown_code()
    assert "FORCE_EXIT_MS" in code
    assert "threading.Timer" in code


def test_the_backstop_does_not_depend_on_the_loop_it_is_watching():
    """THE BUG IN MY OWN FIRST FIX. The backstop was armed with
    QTimer.singleShot AFTER app.quit() and loop.stop() - scheduled on an
    event loop that had just been stopped, so it could never fire. Neon had
    to end the process from Task Manager, which is precisely what it existed
    to prevent.

    A watchdog that depends on the thing it is watching is not a watchdog.
    """
    code = _shutdown_code()
    assert "QTimer.singleShot" not in code
    assert "threading.Timer" in code


def test_the_backstop_is_armed_BEFORE_the_loops_are_stopped():
    code = _shutdown_code()
    assert code.index("threading.Timer") < code.index("app.quit()")
    assert code.index("threading.Timer") < code.index("get_running_loop().stop()")


def test_the_backstop_thread_cannot_itself_hold_the_process_open():
    """A non-daemon timer thread would keep the interpreter alive for the
    full grace period even on a clean exit - trading one hang for another."""
    code = _shutdown_code()
    assert "daemon = True" in code


def test_the_grace_period_is_short_but_real():
    """Long enough for a normal interpreter shutdown, short enough that a
    hang is not something the operator sits through."""
    assert 1000 <= MainWindow.FORCE_EXIT_MS <= 10000


def test_the_force_exit_happens_only_after_state_is_saved():
    """browser.session_saved is logged during panel teardown, which the
    ceiling above already waited for - so by the time the backstop can fire
    there is nothing left to lose."""
    src = inspect.getsource(MainWindow._shutdown_all)
    assert src.index("asyncio.wait_for") < src.index("FORCE_EXIT_MS")
    assert src.index("gui.shutdown_complete") < src.index("FORCE_EXIT_MS")


def test_the_force_exit_flushes_logging_first():
    """Otherwise the very event explaining the forced exit is the one lost."""
    src = inspect.getsource(MainWindow._force_exit)
    assert "logging.shutdown()" in src
    assert "gui.force_exit" in src
    assert "os._exit(0)" in src


def test_the_backstop_reports_itself():
    """A silent force-exit would hide a real regression in normal shutdown."""
    src = inspect.getsource(MainWindow._force_exit)
    assert "logger.warning" in src
