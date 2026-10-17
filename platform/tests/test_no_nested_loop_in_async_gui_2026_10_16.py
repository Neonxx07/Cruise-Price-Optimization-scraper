"""No modal pop-up / processEvents inside an async GUI handler, 2026-10-16.

Neon: "esspresso results are not showing ... is ncl working?" (4 lines
running). The log at 13:55:10: Princess's Start showed
QMessageBox.question("Code changed since this window opened") - a NESTED Qt
event loop inside an async slot. qasync resumed other tabs' tasks inside it
and Python 3.14 refused:

  Cannot enter into task <... CruiseLinePanel._on_start() ...> while another
  task <... _on_start() ...> is being executed.
  Task was destroyed but it is pending! <... playwright Connection.run() ...>

ESPRESSO's Start task - the one that copies results into the table - died:
its batch kept scanning and SAVING (56 rows in `bookings`) with 2 rows on
screen and 87 bookings stuck "RUNNING". NCL's Playwright connection task was
destroyed: its job sat at 45/232 with 0 results saved.
"""
import ast
import asyncio
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

ROOT = Path(__file__).resolve().parents[1]
BLOCKING = {"question", "warning", "information", "critical", "about"}


def test_no_blocking_dialog_or_process_events_inside_an_async_gui_handler():
    found = []
    for path in sorted((ROOT / "gui").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                    continue
                f = call.func
                if (f.attr in ("exec", "exec_", "processEvents")
                        or (getattr(f.value, "id", "") == "QMessageBox" and f.attr in BLOCKING)):
                    found.append(f"{path.name}:{call.lineno} {fn.name} {ast.unparse(f)}")
    assert not found, found


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _answer_with(qt_app, which):
    from core.models import CruiseLine
    from gui.windows import CruiseLinePanel

    panel = CruiseLinePanel(CruiseLine.ESPRESSO)

    async def scenario():
        task = asyncio.ensure_future(panel._ask(
            "Title", "Start anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No))
        await asyncio.sleep(0)          # _ask has opened the box and is waiting
        boxes = [b for b in panel.findChildren(QMessageBox) if b.isVisible()]
        assert len(boxes) == 1          # open() returned - no nested loop
        boxes[0].button(which).click()
        return await asyncio.wait_for(task, 2)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_ask_returns_the_button_clicked(qt_app):
    assert _answer_with(qt_app, QMessageBox.StandardButton.Yes) == QMessageBox.StandardButton.Yes
    assert _answer_with(qt_app, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No
