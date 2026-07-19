"""PySide6 desktop application entrypoint."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from PySide6.QtCore import Qt, QCoreApplication

from gui.windows import MainWindow


def _handle_async_exception(loop, context):
    print("ASYNC EXCEPTION:", context)
    exc = context.get("exception")
    if exc:
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__)


def main() -> int:
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
