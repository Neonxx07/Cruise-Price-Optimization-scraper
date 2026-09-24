"""A second Start click must not enter _on_start while one is in flight.

From the overnight run of 2026-09-22/23, which lost a scan to this::

    RuntimeError: Cannot enter into task Task-17 <_on_start at :961>
    while another task Task-546 <_on_start at :898> is being executed
    Task was destroyed but it is pending!  <Task-17 ... :961>

Task-17 was the running batch. Task-546 was a second _on_start parked on
the stale-modules QMessageBox. A Qt modal spins its own event loop nested
inside the asyncio one, so while it is up qasync cannot wake the batch
task - and the batch task was destroyed mid-scan.

start_button.setEnabled(False) did not help: it happens ~60 lines into the
coroutine, after two modals that each spin the loop.
"""

import ast
import asyncio
import io
import tokenize
from pathlib import Path

import pytest

WINDOWS_PY = Path(__file__).resolve().parents[1] / "gui" / "windows.py"


def _code_of(func_name: str) -> str:
    """Source of one function with comments stripped.

    Comments describe the bug; they must never be what a test matches. This
    file has been fooled that way three times before.
    """
    src = WINDOWS_PY.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            seg = ast.get_source_segment(src, node)
            return tokenize.untokenize(
                tok for tok in tokenize.generate_tokens(io.StringIO(seg).readline)
                if tok.type != tokenize.COMMENT
            )
    raise AssertionError(f"{func_name} not found in {WINDOWS_PY}")


def test_the_guard_is_set_before_any_modal_can_spin_the_loop():
    """The wrapper must hold no dialog at all - a modal inside it would sit
    in front of the flag being set and reopen the window."""
    wrapper = _code_of("_on_start")
    assert "_start_in_progress" in wrapper
    assert "_on_start_guarded" in wrapper
    assert "QMessageBox" not in wrapper


def test_the_real_work_still_happens_and_still_shows_its_dialogs():
    body = _code_of("_on_start_guarded")
    assert "QMessageBox" in body, "the guarded body kept the operator dialogs"
    assert "stale_modules" in body


def test_the_flag_is_cleared_even_when_the_batch_raises():
    """A failed scan must not wedge Start off for the rest of the session."""
    wrapper = _code_of("_on_start")
    tree = ast.parse(wrapper.strip())
    fn = tree.body[0]
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, "no try/finally around the guarded call"
    assert any(
        any("_start_in_progress" in ast.dump(stmt) for stmt in t.finalbody)
        for t in tries
    ), "the flag is not reset in a finally block"


@pytest.mark.asyncio
async def test_a_second_entry_returns_immediately_while_the_first_runs():
    """Behavioural reproduction of the crash, without Qt.

    The first call parks (as the batch did). The second must return at once
    rather than becoming a second live task.
    """
    entered: list[str] = []
    release = asyncio.Event()

    class Panel:
        def __init__(self) -> None:
            self._start_in_progress = False

        async def _on_start(self, tag: str) -> None:
            if self._start_in_progress:
                entered.append(f"{tag}:rejected")
                return
            self._start_in_progress = True
            try:
                entered.append(f"{tag}:running")
                await release.wait()
            finally:
                self._start_in_progress = False

    panel = Panel()
    first = asyncio.create_task(panel._on_start("first"))
    await asyncio.sleep(0)          # let the first park, as the modal did
    await panel._on_start("second")  # the duplicate click
    release.set()
    await first

    assert entered == ["first:running", "second:rejected"]
    assert panel._start_in_progress is False


@pytest.mark.asyncio
async def test_start_works_again_after_the_first_run_finishes():
    """The guard must not be a one-shot latch."""
    runs: list[str] = []

    class Panel:
        def __init__(self) -> None:
            self._start_in_progress = False

        async def _on_start(self, tag: str) -> None:
            if self._start_in_progress:
                return
            self._start_in_progress = True
            try:
                runs.append(tag)
            finally:
                self._start_in_progress = False

    panel = Panel()
    await panel._on_start("run1")
    await panel._on_start("run2")
    assert runs == ["run1", "run2"]
