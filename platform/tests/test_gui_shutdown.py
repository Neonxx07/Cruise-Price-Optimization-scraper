"""Regression tests for the GUI shutdown sequencing fix (2026-08-13
audit). Runs a REAL (offscreen, no display needed) QApplication +
qasync event loop -- this actually exercises MainWindow._shutdown_and_close(),
not just a description of what it should do.

Skipped automatically if PySide6/qasync aren't installed (they're
deliberately excluded from requirements.txt -- see START_GUI.bat).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pyside6 = pytest.importorskip("PySide6")
qasync = pytest.importorskip("qasync")

import asyncio
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp_and_loop():
    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        yield app, loop
    finally:
        # Close the loop HERE rather than leaving it to interpreter
        # shutdown. Left open, BaseEventLoop.__del__ runs after Qt has
        # already torn down its objects and qasync's close() raises
        # "RuntimeError: Signal source has been deleted" — harmless (the
        # suite still exits 0) but it prints two tracebacks after a green
        # run, which reads like a failure.
        asyncio.set_event_loop(None)
        try:
            loop.close()
        except Exception:
            pass


class _FakeQueueManagerRunning:
    def __init__(self):
        self.stop_called = False
        self._ticks_until_done = 5
        self.close_called = False

    @property
    def is_running(self):
        if self.stop_called:
            self._ticks_until_done -= 1
            return self._ticks_until_done > 0
        return True

    def stop_processing(self):
        self.stop_called = True
        return True

    async def close_live_session(self):
        self.close_called = True


class _FakeQueueManagerIdle:
    def __init__(self):
        self.is_running = False
        self.stop_called = False
        self.close_called = False

    def stop_processing(self):
        self.stop_called = True
        return True

    async def close_live_session(self):
        self.close_called = True


def test_regression_shutdown_stops_scan_before_closing_browser(qapp_and_loop):
    """CONFIRMED REAL BUG, fixed 2026-08-13: closeEvent used to close the
    browser immediately even with a scan still running, which made the
    in-flight scrape fail and triggered booking_service's dead-browser
    recovery to spin up a SECOND browser while the app was already
    quitting."""
    from gui.windows import CruiseLinePanel

    app, loop = qapp_and_loop
    # UPDATED 2026-08-28: shutdown became PER PANEL when the GUI went
    # tabbed — several lines can hold live browsers at once, so
    # MainWindow._shutdown_all() awaits every panel's shutdown() rather
    # than closing one session. The ordering rule under test is unchanged
    # and still load-bearing: STOP the scan before CLOSING the browser.
    from core.models import CruiseLine

    win = CruiseLinePanel(CruiseLine.ESPRESSO)
    win.queue_manager = _FakeQueueManagerRunning()

    loop.run_until_complete(win.shutdown())

    qm = win.queue_manager
    assert qm.stop_called, "stop_processing() was never called"
    assert qm.close_called, "close_live_session() was never called"
    # quit() is the SHELL's job now (MainWindow._shutdown_all), not a
    # panel's — a panel closing must not take the app down while other
    # tabs are still shutting down.


def test_shutdown_idle_still_closes_browser_and_quits(qapp_and_loop):
    from gui.windows import CruiseLinePanel

    app, loop = qapp_and_loop
    from core.models import CruiseLine

    win = CruiseLinePanel(CruiseLine.ESPRESSO)
    win.queue_manager = _FakeQueueManagerIdle()

    loop.run_until_complete(win.shutdown())

    qm = win.queue_manager
    assert not qm.stop_called, "stop_processing() should not be called when nothing is running"
    assert qm.close_called
