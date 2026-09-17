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
from utils.logging import get_logger, setup_logging


def _handle_async_exception(loop, context):
    print("ASYNC EXCEPTION:", context)
    exc = context.get("exception")
    if exc:
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__)


logger = get_logger(__name__)


def main() -> int:
    # Without this, only the CLI paths configure structlog — every
    # logger.info/warning/error call made during a GUI-driven scan
    # (including ones that would explain a failed session save) is
    # silently dropped instead of reaching stderr.
    setup_logging(settings.log_level)
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
            return loop.run_forever()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
