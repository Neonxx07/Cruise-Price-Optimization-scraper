"""The Verify button must not be painted across the results table.

Neon 2026-10-01, from a live screenshot: *"also verified selected is in a
wrong place try to fix this"* - the button was rendering ON TOP of the result
rows.

THE CAUSE was not the layout order, which was correct: the table was added
to the Results box and the Verify row after it. It was a Qt MINIMUM-SIZE
OVERFLOW. `results_table.setMinimumHeight(240)` forces the table to at least
240px, and Qt cannot shrink a widget below its minimum - so on a short
window the box ran out of room and whatever sat beneath the table was drawn
over it.

THE FIX: Verify moved to the FOOTER, beside Export report - directly
beneath the table it acts on, always visible whatever the window height,
and outside the box entirely, so it cannot compete with the table for
height again.

A FIRST ATTEMPT ALSO DROPPED the table's floor from 240px to 120px. That
was wrong and is reverted: the 240 floor is itself a fix (without it the
results table was squeezed to ~126px while empty boxes took the room), so
it traded the new bug for the old one. Moving the button out is sufficient
on its own.

The button worked throughout; this was placement only. Neon's screenshot
shows "Verified 1 booking(s) - removed from the list."
"""

import ast
import pathlib

import pytest

WINDOWS = pathlib.Path("gui/windows.py")


def _source() -> str:
    return WINDOWS.read_text(encoding="utf-8")


def _build_function() -> ast.FunctionDef:
    """The method that builds the tab's widgets."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id == "QPushButton"
                        and n.args
                        and isinstance(n.args[0], ast.Constant)
                        and n.args[0].value == "Verify selected"
                        for n in ast.walk(node))):
            return node
    pytest.fail("no method builds a 'Verify selected' button any more")


def test_the_verify_button_still_exists():
    assert 'QPushButton("Verify selected")' in _source()


def test_it_is_wired_to_the_handler():
    assert "self.verify_button.clicked.connect(self._on_verify_selected)" in _source()


def test_it_sits_in_the_footer_beside_export():
    """Both must be added to the same layout. The footer is the one place
    that cannot overlap the table."""
    source = _source()
    export_at = source.index("bottom.addWidget(self.export_button)")
    verify_at = source.index("bottom.addWidget(self.verify_button)")
    assert verify_at > export_at, "Verify is no longer in the footer row"


def test_it_is_not_inside_the_results_box():
    """Where it was painted over the rows."""
    source = _source()
    results_table_at = source.index("results_v.addWidget(self.results_table)")
    results_box_done = source.index("layout.addWidget(results_box, 1)")
    between = source[results_table_at:results_box_done]
    assert "verify_button" not in between, (
        "Verify is back inside the Results box, where it overlapped the table")


def test_the_results_table_keeps_its_height_floor():
    """The 240px minimum STAYS.

    Lowering it to 120 was my first attempt at this fix and it was wrong:
    that floor is itself a fix (without it the results table was squeezed
    to ~126px while empty boxes took the room - see
    test_tabbed_gui_2026_08_28.py::test_results_table_gets_the_space), so
    the change traded the new bug for the old one.

    The real cause was the button sitting INSIDE the Results box, competing
    with this minimum for height Qt could not take from the table. Moving
    Verify to the footer removed the competition entirely.
    """
    assert "self.results_table.setMinimumHeight(240)" in _source()


def test_the_realised_label_moved_with_it():
    """It is the Verify button's readout; splitting them would leave a
    stray number in the Results box."""
    source = _source()
    assert "bottom.addWidget(self.realised_label)" in source


def test_both_widgets_are_created_before_they_are_added():
    """A NameError here is a crash on startup, not a layout nit."""
    func = _build_function()
    source = ast.unparse(func)
    for name in ("verify_button", "realised_label"):
        created = source.index(f"self.{name} = ")
        added = source.index(f"bottom.addWidget(self.{name})")
        assert created < added, f"self.{name} is added before it is created"
