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
    yield app, loop


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
    from gui.windows import MainWindow

    app, loop = qapp_and_loop
    win = MainWindow()
    win.queue_manager = _FakeQueueManagerRunning()
    win._shutting_down = False
    quit_called = {"v": False}
    QApplication.instance().quit = lambda: quit_called.update(v=True)

    loop.run_until_complete(win._shutdown_and_close())

    qm = win.queue_manager
    assert qm.stop_called, "stop_processing() was never called"
    assert qm.close_called, "close_live_session() was never called"
    assert quit_called["v"], "quit() was never called"


def test_shutdown_idle_still_closes_browser_and_quits(qapp_and_loop):
    from gui.windows import MainWindow

    app, loop = qapp_and_loop
    win = MainWindow()
    win.queue_manager = _FakeQueueManagerIdle()
    win._shutting_down = False
    quit_called = {"v": False}
    QApplication.instance().quit = lambda: quit_called.update(v=True)

    loop.run_until_complete(win._shutdown_and_close())

    qm = win.queue_manager
    assert not qm.stop_called, "stop_processing() should not be called when nothing is running"
    assert qm.close_called
    assert quit_called["v"]
