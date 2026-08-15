"""PySide6 desktop application entrypoint."""

from __future__ import annotations

import sys

# Windows attaches a cp1252 console by default, which can't encode the
# emoji used in main.py's print() calls (e.g. the anchor in the login
# check) — reconfigure to UTF-8 so those don't crash background tasks.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from PySide6.QtCore import Qt, QCoreApplication

from config.settings import settings
from gui.windows import MainWindow
from utils.logging import setup_logging


def _handle_async_exception(loop, context):
    print("ASYNC EXCEPTION:", context)
    exc = context.get("exception")
    if exc:
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__)


def main() -> int:
    # Without this, only the CLI paths configure structlog — every
    # logger.info/warning/error call made during a GUI-driven scan
    # (including ones that would explain a failed session save) is
    # silently dropped instead of reaching stderr.
    setup_logging(settings.log_level)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    loop.set_exception_handler(_handle_async_exception)

    window = MainWindow()
    window.show()

    with loop:
        return loop.run_forever()


if __name__ == "__main__":
    sys.exit(main())
