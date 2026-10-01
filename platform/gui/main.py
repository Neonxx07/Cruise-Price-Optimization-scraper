"""PySide6 desktop application entrypoint."""

from __future__ import annotations

import sys

# Windows attaches a cp1252 console by default, which can't encode the
# emoji used in main.py's print() calls (e.g. the anchor in the login
# check) — reconfigure to UTF-8 so those don't crash background tasks.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication, QMessageBox
from qasync import QEventLoop
from PySide6.QtCore import Qt, QCoreApplication

from config.settings import settings
from gui.windows import MainWindow
from utils.logging import (
    asyncio_exception_handler,
    get_logger,
    install_crash_handlers,
    setup_logging,
)


def _handle_async_exception(loop, context):
    """Async failures go to the LOG, not just to stdout.

    This used to be print() plus traceback.print_exception(), both to
    stdout. On 2026-09-22 a RuntimeError killed a running 721-booking scan
    and appeared four times in the terminal and zero times in
    data/cruiseintel.log - so scan_watchdog, which reads the log, was blind
    to it and the dead scan went unnoticed for thirteen hours.

    utils.logging.asyncio_exception_handler still calls the loop's default
    handler, so the stderr output that was there before is unchanged.
    """
    asyncio_exception_handler(loop, context)


logger = get_logger(__name__)


def main() -> int:
    # Without this, only the CLI paths configure structlog — every
    # logger.info/warning/error call made during a GUI-driven scan
    # (including ones that would explain a failed session save) is
    # silently dropped instead of reaching stderr.
    setup_logging(settings.log_level, settings.log_file)
    # Main-thread and per-thread crashes reach the log too, not only
    # the asyncio ones - a thread dying silently is how a keepalive
    # stops running with nothing appearing to be wrong.
    install_crash_handlers("gui")
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

    app = QApplication(sys.argv)

    # SINGLE-INSTANCE GUARD, added 2026-08-27. CONFIRMED REAL SITUATION
    # found during the forensic review: FOUR `python -m gui.main` processes
    # were live at once (two independent GUI instances), while a
    # 167-booking NCL scan was in flight. That is the exact hazard
    # SingleInstanceGuard was written for and it was wired into
    # msc_session_controller.py, msc_live_service.py and
    # run_persistent_watchlist_scan.py — but NOT into the desktop app,
    # which is how Neon actually drives every scan.
    #
    # Why it matters concretely: these portals allow ONE active session per
    # account (DOCUMENTATION.md section L), two instances clobber each
    # other's storage_state_*.json on save (last writer wins, so a dead
    # session can overwrite a good one), they contend on SQLite, and on NCL
    # a booking held under one instance's 30-minute edit lock reads as
    # "Reservation is not found" to the other — indistinguishable from a
    # genuinely missing booking.
    #
    # Refuses with a clear dialog rather than starting and corrupting
    # things silently. The guard is released automatically when the process
    # exits, and filelock >= 3.29.0 reclaims a lock whose holder died
    # without cleaning up, so a crash cannot wedge the app permanently.
    from services.resource_governor import SingleInstanceGuard

    guard = SingleInstanceGuard("cruiseintel_gui")
    if not guard.acquire():
        owner = ""
        try:
            with open(guard.lock_path + ".owner", encoding="utf-8") as f:
                owner = f" ({f.read().strip()})"
        except OSError:
            pass
        QMessageBox.critical(
            None, "CruiseIntel is already running",
            f"Another CruiseIntel window is already open{owner}.\n\n"
            "Running two at once is not safe: these portals allow only one "
            "active session per account, the two windows overwrite each "
            "other's saved login, and on NCL a booking locked by one "
            "window shows up as 'Reservation is not found' in the other.\n\n"
            "Switch to the window that is already open, or close it before "
            "starting a new one.",
        )
        logger.error("gui.refused_second_instance", lock_path=guard.lock_path)
        return 1

    loop = QEventLoop(app)
    loop.set_exception_handler(_handle_async_exception)

    window = MainWindow()
    window.show()

    # Load what already happened TODAY into every tab before the operator
    # touches anything. Without this the tables are blank on startup even
    # when a scan completed successfully minutes earlier - which is exactly
    # how a 769-booking ESPRESSO run that found 19 optimizations worth
    # $3,175 looked like "zero optimization" on 2026-08-28. Scheduled on the
    # loop rather than awaited so the window paints immediately.
    async def _prime():
        total = 0
        for panel in window.panels.values():
            total += await panel.load_todays_results()
        await window.refresh_last_scan_label()
        window._refresh_global_summary()
        if total:
            window._append_activity("ALL", f"restored {total} result(s) from today")

    loop.create_task(_prime())

    try:
        with loop:
            loop.run_forever()
        # EXPLICIT 0 ON A CLEAN SHUTDOWN. Fixed 2026-09-22: this returned
        # whatever qasync's run_forever handed back, and a perfectly normal
        # close - the log's own "gui.shutdown_complete" was the last line -
        # exited with code 1. That makes every ordinary quit look like a
        # crash, which is precisely the kind of noise that hides a REAL one.
        # A genuine failure still propagates as an exception.
        return 0
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
