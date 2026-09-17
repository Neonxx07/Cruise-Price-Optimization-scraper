"""Everything must agree with everything else.

Neon 2026-09-03: "make sure that everything is merged and working perfectly
together and matched with the gui".

THE FAILURE THIS FILE EXISTS FOR. Guards keep getting added to one path and
not the others. Already happened three times:

  * `msc_occupancy_is_trustworthy` was written, unit-tested and documented,
    and never called from anywhere — inert for a full day while its own
    tests passed.
  * `is_overpayment` was detected and stored, but only fed `is_paid_in_full`,
    so all three discount checks stayed free to report an opportunity on an
    overpaid booking.
  * The club-discount, non-cruise-charge and scope guards went into
    `_check_booking_msc` (the live path) but not into `msc_run_calculator.py`
    (the replay path), so replaying a stored capture produced a DIFFERENT
    verdict from the run that captured it. A report disagreeing with the scan
    that produced it is worse than either being wrong on its own.

Unit tests cannot catch any of these, because they call the function
directly. These tests compare the CALL SITES instead.
"""
import ast
import inspect
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUARD_ARGS = {
    "is_overpayment",
    "occupancy_verified",
    "customer_has_club_membership",
    "today_price_includes_club_discount",
    "non_cruise_charges",
    "current_scope",
    "today_scope",
}


# Always present at module scope at runtime, but absent from
# `dir(builtins)` - so a scanner that checks builtins alone reports
# __file__ as undefined. Found 2026-09-15 when gui/windows.py started
# using __file__ to locate the project root.
_MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__debug__",
}

def _kwargs_at_call(module, func_name="evaluate_msc_booking"):
    """Every keyword passed to `func_name` anywhere in this module."""
    tree = ast.parse(inspect.getsource(module))
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name != func_name:
            continue
        seen |= {kw.arg for kw in node.keywords if kw.arg}
    return seen


def test_the_live_path_passes_every_guard():
    """`_check_booking_msc` is what a real scan runs."""
    import msc_commands

    passed = _kwargs_at_call(msc_commands)
    missing = GUARD_ARGS - passed
    assert not missing, f"the live path does not pass: {sorted(missing)}"


def test_the_replay_path_passes_every_guard_the_live_path_does():
    """msc_run_calculator.py replays stored captures. If it applies fewer
    guards, its report contradicts the scan that produced the data."""
    import msc_run_calculator

    passed = _kwargs_at_call(msc_run_calculator)
    missing = GUARD_ARGS - passed
    assert not missing, (
        f"the replay path does not pass: {sorted(missing)} — it will disagree "
        f"with the live scan on the same booking"
    )


def test_the_two_paths_pass_the_same_guard_set():
    """Direct comparison, so a guard added to either side in future must be
    added to both or this fails."""
    import msc_commands
    import msc_run_calculator

    live = _kwargs_at_call(msc_commands) & GUARD_ARGS
    replay = _kwargs_at_call(msc_run_calculator) & GUARD_ARGS
    assert live == replay, (
        f"guard drift — live only: {sorted(live - replay)}, "
        f"replay only: {sorted(replay - live)}"
    )


# -- the GUI must be able to show what the calculators produce ------


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6", reason="GUI tests need PySide6")
    pytest.importorskip("qasync", reason="GUI tests need qasync")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_every_cruise_line_has_a_working_tab(qt_app):
    from core.models import CruiseLine
    from gui.windows import MainWindow

    win = MainWindow()
    try:
        titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        for line in CruiseLine:
            assert line.value in titles, f"{line.value} has no tab"
        assert set(win.panels) == set(CruiseLine)
    finally:
        win.setParent(None)
        win.deleteLater()
        qt_app.processEvents()


def test_msc_runs_through_its_own_service_not_the_booking_queue(qt_app):
    """MSC cannot use BookingQueueManager — the agent can never commit a
    reprice, and the flow is a dummy-booking rate check, not a reprice. The
    MSC panel must therefore hold an MscLiveService, and `is_busy` must
    account for it or the GUI would let a second MSC run start on top of a
    live one."""
    from core.models import CruiseLine
    from gui.windows import CruiseLinePanel

    panel = CruiseLinePanel(CruiseLine.MSC)
    assert panel.msc_service is not None
    assert hasattr(panel, "msc_results")

    class _Busy:
        is_running = True

        def stop_processing(self):
            pass

    panel.msc_service = _Busy()
    assert panel.is_busy() is True, (
        "a live MSC run does not mark the panel busy — a second run could start"
    )


def test_a_goccl_no_saving_row_renders(qt_app):
    """The GoCCL fix turns an unusable candidate into NO_SAVING. That status
    must render, and must not be tinted or totalled as a win."""
    from core.models import BookingResult, BookingStatus, CruiseLine
    from gui.windows import CruiseLinePanel

    panel = CruiseLinePanel(CruiseLine.GOCCL)
    result = BookingResult(
        booking_id="DEMO01", cruise_line=CruiseLine.GOCCL,
        status=BookingStatus.NO_SAVING, net_saving=0.0, confidence=1,
        note="a $1560 cheaper fare was seen but its offer code was not captured",
    )
    panel.results.append(result)
    panel._append_result_row(result)
    panel._refresh_summary()
    assert panel.results_table.rowCount() == 1
    assert "0.00" in panel.summary_label.text() or "$0" in panel.summary_label.text()


def test_the_savings_total_still_excludes_unconfirmed_candidates(qt_app):
    """$4,100 of the all-time total was once unverified GoCCL candidates."""
    from core.calculator import total_optimization_savings
    from core.models import BookingResult, BookingStatus, CruiseLine

    rows = [
        BookingResult(booking_id="A", cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.OPTIMIZATION, net_saving=60.0,
                      confidence=5, note="optimized $60"),
        BookingResult(booking_id="DEMO01", cruise_line=CruiseLine.GOCCL,
                      status=BookingStatus.OPTIMIZATION, net_saving=1560.0,
                      confidence=1,
                      note="candidate $1560 — offer code 'PUG' — UNCONFIRMED"),
    ]
    assert total_optimization_savings(rows) == pytest.approx(60.0, abs=0.01)


# -- nothing may import in a broken state ---------------------------


@pytest.mark.parametrize("module_name", [
    "core.calculator",
    "core.calculator_msc",
    "core.price_scope",
    "core.models",
    "msc_commands",
    "msc_run_calculator",
    "msc_audit",
    "cross_line_audit",
    "services.msc_live_service",
    "services.booking_service",
    "scraper.espresso",
    "scraper.ncl",
    "scraper.goccl",
])
def test_every_module_imports_cleanly(module_name):
    """A file that only breaks at import time looks fine until the run that
    needs it. Two of this session's bugs were exactly that: a misplaced
    patch left a NameError in a function nothing tested, and an import
    inserted at the wrong indentation broke a whole module."""
    import importlib

    if module_name in ("msc_audit", "cross_line_audit"):
        # These execute their report on import; just compile them.
        import pathlib

        src = pathlib.Path(module_name + ".py")
        if not src.exists():
            pytest.skip(f"{module_name}.py not present")
        ast.parse(src.read_text(encoding="utf-8"))
        return
    importlib.import_module(module_name)


# -- a name used but never imported ---------------------------------


_SCANNED_MODULES = [
    "core/calculator.py", "core/calculator_msc.py", "core/price_scope.py",
    "core/models.py", "core/confidence.py", "msc_commands.py",
    "msc_run_calculator.py", "msc_audit.py", "cross_line_audit.py",
    "services/booking_service.py", "services/msc_live_service.py",
    "scraper/espresso.py", "scraper/ncl.py", "scraper/goccl.py",
    "scraper/base.py", "gui/windows.py",
]


@pytest.mark.parametrize("rel_path", _SCANNED_MODULES)
def test_no_module_references_an_undefined_name(rel_path):
    """CONFIRMED TWICE, and importing the module catches NEITHER.

    1. A misplaced `.replace(..., 1)` patch left `staged.get(...)` inside a
       function with no `staged` — a guaranteed NameError on every console
       check-rates run.
    2. `msc_occupancy_is_trustworthy` was CALLED in msc_run_calculator.py
       while its import was silently skipped by a faulty guard in my own
       patch script. `import msc_run_calculator` succeeded; the NameError
       only fired once main() reached the call, mid-run, after real work.

    An import test cannot see either, because the broken name lives inside
    a function body that import never executes. This walks every function
    and checks each loaded name is actually bound somewhere visible.

    Closures are handled by treating names bound anywhere in a function's
    subtree as visible to it — a nested function legitimately reads its
    parent's locals, and not modelling that produced 40 false positives on
    the first attempt.
    """
    import builtins
    import pathlib

    src_path = pathlib.Path(rel_path)
    if not src_path.exists():
        pytest.skip(f"{rel_path} not present")
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    def bound(node, is_root=False):
        names = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                names.add(arg.arg)
            for extra in (a.vararg, a.kwarg):
                if extra:
                    names.add(extra.arg)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
                if n is not node:
                    names |= bound(n)
            elif isinstance(n, ast.Lambda) and n is not node:
                names |= bound(n)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                names |= set(n.names)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
                for t in ast.walk(n.target):
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        for t in ast.walk(item.optional_vars):
                            if isinstance(t, ast.Name):
                                names.add(t.id)
        return names

    module_scope = bound(tree, is_root=True) | set(dir(builtins)) | _MODULE_DUNDERS
    problems = []
    for func in tree.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visible = module_scope | bound(func)
        for n in ast.walk(func):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id not in visible):
                problems.append(f"{func.name}() line {n.lineno}: '{n.id}'")

    assert not problems, (
        f"{rel_path} references undefined name(s) — NameError at runtime:\n  "
        + "\n  ".join(sorted(set(problems)))
    )


# -- an MSC result must actually reach the GUI table ---------------


def test_an_msc_outcome_renders_in_the_results_table(qt_app):
    """MSC produces MscBookingResult, not BookingResult, and runs through
    MscLiveService rather than BookingQueueManager — two separate result
    shapes feeding one table. This exercises the MSC row path end to end so
    a change to either shape cannot quietly stop MSC displaying."""
    from core.models import (
        MscBookingResult,
        MscCheck,
        MscCheckStatus,
        MscOpportunityType,
    )
    from gui.windows import CruiseLinePanel
    from core.models import CruiseLine
    from services.msc_live_service import MscCheckOutcome

    panel = CruiseLinePanel(CruiseLine.MSC)
    result = MscBookingResult(
        booking_id="3000081",
        category="IR2",
        checks=[MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.OPPORTUNITY,
            note="today's quote with the club discount is below the current total",
            estimated_value=81.98,
            value_unit="USD",
        )],
        has_any_opportunity=True,
        note="opportunity found",
    )
    outcome = MscCheckOutcome(booking_id="3000081", status="checked",
                              result=result, note="")
    panel.msc_results.append(outcome)
    panel._append_msc_result_row(outcome)
    panel._refresh_summary()

    assert panel.results_table.rowCount() == 1
    rendered = " ".join(
        panel.results_table.item(0, c).text()
        for c in range(panel.results_table.columnCount())
        if panel.results_table.item(0, c) is not None)
    assert "3000081" in rendered
    assert "81.98" in rendered, f"the dollar value is not shown: {rendered}"


def test_the_msc_refusal_states_render_without_a_dollar_value(qt_app):
    """The guards return INSUFFICIENT_DATA with estimated_value None. The
    table must show those as rows, not crash on the missing number and not
    display them as $0.00 wins."""
    from core.models import (
        CruiseLine,
        MscBookingResult,
        MscCheck,
        MscCheckStatus,
        MscOpportunityType,
    )
    from gui.windows import CruiseLinePanel
    from services.msc_live_service import MscCheckOutcome

    panel = CruiseLinePanel(CruiseLine.MSC)
    for note in ("occupancy was REDUCED below MSC's own pre-fill",
                 "this booking has 2 cabins",
                 "holds a Voyagers Club membership but today's quote was "
                 "captured without it"):
        result = MscBookingResult(
            booking_id="X", category="BP",
            checks=[MscCheck(
                type=MscOpportunityType.PRICE_MATCH,
                status=MscCheckStatus.INSUFFICIENT_DATA,
                note=note, estimated_value=None,
            )],
            has_any_opportunity=False, note="no opportunity",
        )
        outcome = MscCheckOutcome(booking_id="X", status="checked",
                                  result=result, note="")
        panel.msc_results.append(outcome)
        panel._append_msc_result_row(outcome)
    panel._refresh_summary()
    assert panel.results_table.rowCount() == 3
    assert "opportunit" in panel.summary_label.text().lower()


def test_rebuilding_the_table_keeps_both_result_shapes(qt_app):
    """`_populate_results_table` has to handle BookingResult and
    MscCheckOutcome together — a sort or a reload goes through it."""
    from core.models import (
        BookingResult,
        BookingStatus,
        CruiseLine,
        MscBookingResult,
    )
    from gui.windows import CruiseLinePanel
    from services.msc_live_service import MscCheckOutcome

    panel = CruiseLinePanel(CruiseLine.MSC)
    panel.results.append(BookingResult(
        booking_id="PLAIN1", cruise_line=CruiseLine.MSC,
        status=BookingStatus.NO_SAVING, net_saving=0.0, confidence=1))
    panel.msc_results.append(MscCheckOutcome(
        booking_id="MSC1", status="checked",
        result=MscBookingResult(booking_id="MSC1", category="BP", checks=[],
                                has_any_opportunity=False, note="none"),
        note=""))
    panel._populate_results_table()
    assert panel.results_table.rowCount() == 2
