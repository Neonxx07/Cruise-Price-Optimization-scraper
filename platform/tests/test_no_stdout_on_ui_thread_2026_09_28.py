"""The GUI must never write to stdout. A paused console freezes the app.

THE HANG, 2026-09-28. Neon: "the gui is not responding". py-spy dumped the
frozen process::

    Thread 5628 (idle)
        _on_login_check (gui\windows.py:657)
        _run (asyncio\events.py:94)
        timerEvent (qasync\__init__.py:307)

Line 657 was `print("GUI: _on_login_check entered")`. The stack was
IDENTICAL across dumps, CPU was 0.0%, and the process had zero children -
the browser had never launched. It was blocked inside print().

The GUI is started from a .bat through cmd.exe, so it owns a console
window, and Windows QuickEdit PAUSES console output the moment anyone
clicks or selects text in it. A paused console blocks whoever is writing to
it. One stray click in a black window froze the whole application, and
Windows reported the window as hung.

Eight debug prints sat on UI-thread paths - eight ways to freeze the app
with a mouse. Logging goes to a ROTATING FILE, which no mouse can pause.
"""

import ast
from pathlib import Path

import pytest

GUI_DIR = Path(__file__).resolve().parents[1] / "gui"


def _print_calls(path: Path) -> list[int]:
    """Line numbers of bare print() calls in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]


@pytest.mark.parametrize("name", ["windows.py", "main.py", "queue_manager.py"])
def test_gui_modules_do_not_print(name):
    """print() on a UI-thread path is a freeze waiting for a stray click."""
    path = GUI_DIR / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    lines = _print_calls(path)
    assert not lines, (
        f"{name} still calls print() at lines {lines} - use logger instead; "
        f"a paused Windows console blocks the writer and hangs the GUI")


def _code_only(path: Path) -> str:
    """Module source with comments stripped.

    The incident comment in windows.py QUOTES the offending print() line, so
    a plain substring search finds the prose and fails against correct code.
    This file made that mistake on its first run; the wider codebase has
    made it five times.
    """
    import io
    import tokenize
    src = path.read_text(encoding="utf-8")
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT
    )


def test_the_login_check_logs_instead_of_printing():
    """The exact call that froze it."""
    code = _code_only(GUI_DIR / "windows.py")
    assert "gui.login_check_entered" in code
    assert "_on_login_check entered" not in code


def test_the_start_handler_logs_instead_of_printing():
    code = _code_only(GUI_DIR / "windows.py")
    assert "gui.start_entered" in code
    assert "gui.start_snapshot" in code
    assert "_on_start entered" not in code
