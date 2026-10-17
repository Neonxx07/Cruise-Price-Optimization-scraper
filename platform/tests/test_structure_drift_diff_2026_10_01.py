"""A drift alert must say WHAT changed, or it gets ignored. (Roadmap P3.3)

FOUND 2026-10-01. `structure_watch` had fired **six times** - 09-28, 09-30
and 10-01, two elements each - and nobody acted on any of them. The warning
carried only a name and a file path:

    structure_watch.structure_changed
      name: espresso_search_input
      path: data/structure_baselines/espresso_search_input.yaml
      note: page structure differs from the saved baseline

"Something changed" with no diff gives the reader nothing to do except open
a YAML file and guess what the portal looks like now.

**It was right every time.** The ESPRESSO search box really had changed. The
baseline, captured 2026-08-27, reads::

    - textbox "Search by Reservation ID, Name or Date"

and Neon's own 2026-09-30 browser recording shows the live placeholder is
now "Find by Reservation ID, Name, Date, etc...".

That matters because the search BUTTON's fallback selector is that exact old
string::

    '#searchReservationBtn, [aria-label="Search by Reservation ID, Name or Date"]'

so the fallback had rotted while the primary `#searchReservationBtn` quietly
carried everything. **Catching a dead fallback before the primary dies too is
the entire point of this check** - and it did catch it, six times, into a
void.

A detector whose output cannot be acted on gets ignored, and an ignored
detector is worse than none: it reads as noise, and the next real change
hides inside it.

(An earlier roadmap entry of mine claimed structure_watch "has never once
captured a baseline". Wrong - the baselines were written in August and the
check has been working ever since. Measured, not assumed, is the rule.)
"""

import ast
import pathlib

import pytest

BASE = pathlib.Path("scraper/base.py")
BASELINE_DIR = pathlib.Path("data/structure_baselines")

# The real strings, from the baseline file and Neon's 2026-09-30 recording.
OLD_SNAPSHOT = '- textbox "Search by Reservation ID, Name or Date"'
NEW_SNAPSHOT = '- textbox "Find by Reservation ID, Name, Date, etc..."'


def _check_structure_drift():
    for node in ast.walk(ast.parse(BASE.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "check_structure_drift"):
            return node
    pytest.fail("scraper/base.py no longer defines check_structure_drift")


# -- the alert carries a diff --------------------------------------------


def test_the_change_alert_computes_a_diff():
    """Structural, from the AST, so a comment mentioning difflib cannot
    satisfy it."""
    func = _check_structure_drift()
    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "unified_diff"]
    assert calls, "the drift alert no longer says what changed"


def test_the_diff_is_logged_not_just_computed():
    """Computing a diff and not logging it would be the same dead end."""
    func = _check_structure_drift()
    for node in ast.walk(func):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "warning"):
            if any(kw.arg == "diff" for kw in node.keywords):
                return
    pytest.fail("structure_watch.structure_changed no longer logs the diff")


def test_the_result_carries_the_diff_for_callers():
    func = _check_structure_drift()
    source = ast.unparse(func)
    assert "'diff': diff" in source or '"diff": diff' in source


def test_the_diff_is_truncated():
    """An aria snapshot of a whole page could be enormous; a log line that
    large is its own problem."""
    func = _check_structure_drift()
    source = ast.unparse(func)
    assert "diff[:1500]" in source


# -- what the diff actually shows ----------------------------------------


def test_the_real_espresso_drift_is_legible():
    """The exact change that was reported six times and ignored."""
    import difflib

    diff = "\n".join(difflib.unified_diff(
        OLD_SNAPSHOT.splitlines(), NEW_SNAPSHOT.splitlines(),
        fromfile="espresso_search_input (baseline)",
        tofile="espresso_search_input (now)", lineterm="", n=1))

    assert "Search by Reservation ID" in diff      # what it was
    assert "Find by Reservation ID" in diff        # what it is
    assert diff.count("\n") >= 3                   # a real unified diff


def test_an_identical_snapshot_produces_no_diff():
    import difflib

    assert not list(difflib.unified_diff(
        OLD_SNAPSHOT.splitlines(), OLD_SNAPSHOT.splitlines(), lineterm=""))


# -- the rotted fallback this caught -------------------------------------


def test_the_search_button_fallback_still_matches_the_old_text():
    """NOT a failing assertion - a RECORD of live selector rot.

    The button's fallback selector matches on
    `[aria-label="Search by Reservation ID, Name or Date"]`, the exact
    string the baseline shows and the portal has moved away from. The
    primary `#searchReservationBtn` still works (confirmed in Neon's
    2026-09-30 recording: css `#searchReservationBtn`, role button named
    "SEARCH"), so nothing is broken today.

    The replacement is deliberately NOT guessed here. In August the
    accessible name came from an `img` alt, not from an `aria-label` at
    all, so the right new fallback is a question for a live page, and this
    project's rule is never to guess a selector.

    When the fallback is updated, update this test with it.
    """
    source = pathlib.Path("scraper/espresso.py").read_text(encoding="utf-8")
    assert 'aria-label="Search by Reservation ID, Name or Date"' in source, (
        "the stale fallback was changed - good; update this test to pin the "
        "new one, and confirm it against a live page rather than a guess")
    assert "#searchReservationBtn" in source, (
        "the PRIMARY selector is gone and only the rotted fallback remains")


def test_the_baselines_that_exist_are_still_on_disk():
    """They were captured 2026-08-27/28 and are the evidence for all of the
    above. Skipped rather than failed where the data directory is absent."""
    if not BASELINE_DIR.exists():
        pytest.skip("structure baselines not present in this checkout")
    names = {p.name for p in BASELINE_DIR.glob("*.yaml")}
    assert "espresso_search_input.yaml" in names
    assert "espresso_search_button.yaml" in names
