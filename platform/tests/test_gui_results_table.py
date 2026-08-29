"""Regression tests for the 2026-08-27 GUI results-table + progress work.

Written after a manual smoke test caught TWO defects that no amount of
code reading found:

1. **Numeric sorting was a silent no-op.** The obvious implementation
   (`item.setData(Qt.ItemDataRole.EditRole, 60.0)`) does nothing on a
   QTableWidgetItem, which treats EditRole and DisplayRole as the SAME
   data — so `setText()` afterwards overwrote the number with the display
   string and sorting stayed lexicographic. "$100.00" sorted BELOW
   "$60.00", and confidence 9 outranked 95: exactly backwards for the two
   columns an operator sorts by most. Fixed with NumericTableItem.__lt__.

2. **The results table dropped almost everything it was given.** It had
   4 columns, so an ERROR row rendered as "ERROR / $0.00 / 0" with no
   cause anywhere on screen, and `note` — which carries the
   LATRIPLE/FREESRVC TRAP explanation and the addon-change summary —
   never reached the operator at all.

These run headless via QT_QPA_PLATFORM=offscreen and are skipped where
PySide6 isn't installed (the in-repo venv/ has no PySide6; C:\\cruisevenv
does).
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")
pytest.importorskip("qasync", reason="GUI tests need qasync")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from core.models import (  # noqa: E402
    BookingResult,
    BookingStatus,
    CruiseLine,
    MscBookingResult,
    MscCheck,
    MscCheckStatus,
    MscOpportunityType,
)
from gui.queue_manager import QueueItem, QueueSnapshot, QueueStatus  # noqa: E402
from gui.windows import CruiseLinePanel, NumericTableItem  # noqa: E402
from services.msc_live_service import MscCheckOutcome  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    """A fresh MainWindow per test — the results table is stateful.

    The teardown matters: leaving MainWindow instances alive in the
    QApplication crashed test_gui_shutdown.py with a Windows access
    violation inside qasync's _EventWorker when that module built its own
    QEventLoop over the same (still-populated) QApplication. Close and
    delete each window, then let Qt actually process the deferred delete.
    """
    # UPDATED 2026-08-28: the results table moved from MainWindow onto
    # CruiseLinePanel when the GUI became tabbed (one panel per cruise
    # line, each with its own queue/session). The table, its row builders
    # and every behaviour asserted below are unchanged — only their home
    # class changed. A panel is also the tighter unit to test.
    win = CruiseLinePanel(CruiseLine.NCL)
    try:
        yield win
    finally:
        # NOT win.close(): closeEvent deliberately ignores the close and
        # fires the async _shutdown_and_close() coroutine (see its comment
        # — that ordering is itself a regression fix), which has no loop to
        # run on here. Deleting the widget is what actually clears it out
        # of the QApplication.
        win.setParent(None)
        win.deleteLater()
        qt_app.processEvents()
        qt_app.sendPostedEvents(None, 0)


def _row_of(window, booking_id: str) -> int:
    """Locate a row by booking ID. Sorting is re-enabled after every
    insert, so insertion order is NOT row order — asserting on a fixed
    row index gives false failures."""
    table = window.results_table
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == booking_id:
            return row
    raise AssertionError(f"{booking_id} not in results table")


def _cell(window, booking_id: str, col: int) -> str:
    return window.results_table.item(_row_of(window, booking_id), col).text()


# ── NumericTableItem: defect 1 ────────────────────────────────────


def test_qtablewidgetitem_editrole_really_does_alias_displayrole(qt_app):
    """Documents WHY NumericTableItem exists. If a future Qt release ever
    separates these roles this test fails and the subclass can go — until
    then, setData(EditRole, ...) followed by setText() loses the number."""
    item = QTableWidgetItem()
    item.setData(Qt.ItemDataRole.EditRole, 60.0)
    item.setText("-$60.00 saved")
    assert item.data(Qt.ItemDataRole.EditRole) == "-$60.00 saved"


def test_numeric_item_sorts_by_value_not_text():
    a = NumericTableItem("$60.00", 60.0)
    b = NumericTableItem("$100.00", 100.0)
    assert a < b, "as text '$100.00' < '$60.00' — the exact bug"
    assert not (b < a)


def test_numeric_item_keeps_its_display_text():
    item = NumericTableItem("-$60.00 saved", 60.0)
    assert item.text() == "-$60.00 saved"


def test_numeric_item_against_a_plain_text_cell_does_not_raise():
    """A mixed table (an MSC batch and an NCL batch in one session) must
    not blow up mid-sort."""
    numeric = NumericTableItem("$60.00", 60.0)
    plain = QTableWidgetItem("—")
    assert isinstance(numeric < plain, bool)


def test_savings_column_sorts_numerically_in_the_real_table(window):
    for result in (
        BookingResult(booking_id="AAA", cruise_line=CruiseLine.NCL,
                      status=BookingStatus.OPTIMIZATION, net_saving=60.0, confidence=95),
        BookingResult(booking_id="BBB", cruise_line=CruiseLine.NCL,
                      status=BookingStatus.OPTIMIZATION, net_saving=100.0, confidence=9),
    ):
        window._append_result_row(result)

    window.results_table.sortItems(3, Qt.SortOrder.DescendingOrder)
    assert window.results_table.item(0, 0).text() == "BBB", "$100 must outrank $60"

    window.results_table.sortItems(4, Qt.SortOrder.DescendingOrder)
    assert window.results_table.item(0, 0).text() == "AAA", "confidence 95 must outrank 9"


# ── results table: defect 2 ───────────────────────────────────────


def test_table_has_the_nine_reporting_columns(window):
    assert window.results_table.columnCount() == 9
    headers = [window.results_table.horizontalHeaderItem(i).text()
               for i in range(9)]
    assert headers[0] == "Booking ID"
    assert "Note" in headers[8]


def test_error_row_shows_its_cause(window):
    """THE defect: a 4-column ERROR row read "ERROR / $0.00 / 0" and the
    operator had to go read the log file to learn why."""
    window._append_result_row(BookingResult(
        booking_id="11112222", cruise_line=CruiseLine.ESPRESSO,
        status=BookingStatus.ERROR,
        error="category IT not reachable: slick-viewport rendered 23 of 41 rows",
    ))
    assert "slick-viewport" in _cell(window, "11112222", 8)
    # ...and must not invent totals it never read
    assert _cell(window, "11112222", 5) == "—"
    assert _cell(window, "11112222", 6) == "—"


def test_error_field_wins_over_note(window):
    """make_error_result sets BOTH .error and .note; .error is the one
    that always carries the real cause."""
    window._append_result_row(BookingResult(
        booking_id="ERR1", cruise_line=CruiseLine.NCL, status=BookingStatus.ERROR,
        error="the real cause", note="a vaguer note",
    ))
    assert _cell(window, "ERR1", 8) == "the real cause"


def test_trap_row_surfaces_the_lost_promo_note(window):
    """The LATRIPLE/FREESRVC rule is the whole reason a TRAP row exists —
    the reason must be on screen, not only in the DB."""
    window._append_result_row(BookingResult(
        booking_id="87654321", cruise_line=CruiseLine.NCL, status=BookingStatus.TRAP,
        old_total=3200.0, new_total=3100.0, net_saving=100.0, confidence=1,
        price_category="IT", note="LOST PROTECTED PROMO: LATRIPLE. Do NOT reprice.",
        old_promos="LATRIPLE,FREESRVC", new_promos="FREESRVC",
    ))
    assert "LATRIPLE" in _cell(window, "87654321", 8)
    # and must never call it a "saving"
    assert "saved" not in _cell(window, "87654321", 3)
    assert "not recommended" in _cell(window, "87654321", 3)


def test_totals_and_category_render(window):
    window._append_result_row(BookingResult(
        booking_id="12345678", cruise_line=CruiseLine.NCL,
        status=BookingStatus.OPTIMIZATION, old_total=4820.50, new_total=4760.50,
        net_saving=60.0, confidence=95, price_category="BX", new_price_category="BX",
    ))
    assert _cell(window, "12345678", 5) == "$4,820.50"
    assert _cell(window, "12345678", 6) == "$4,760.50"
    assert _cell(window, "12345678", 7) == "BX"


def test_category_change_is_shown_as_an_arrow(window):
    window._append_result_row(BookingResult(
        booking_id="UPG1", cruise_line=CruiseLine.ESPRESSO,
        status=BookingStatus.UPGRADE_AVAILABLE, price_category="IB",
        new_price_category="BA", net_saving=0.0, confidence=80,
    ))
    assert _cell(window, "UPG1", 7) == "IB → BA"


def test_note_gets_a_tooltip_because_it_never_fits(window):
    long_note = "LATRIPLE present after — safe to optimize. " * 4
    window._append_result_row(BookingResult(
        booking_id="TIP1", cruise_line=CruiseLine.NCL,
        status=BookingStatus.OPTIMIZATION, net_saving=60.0, confidence=95,
        note=long_note,
    ))
    item = window.results_table.item(_row_of(window, "TIP1"), 8)
    assert item.toolTip() == long_note.strip()


# ── MSC rows in the same table ────────────────────────────────────


def _msc_outcome() -> MscCheckOutcome:
    checks = [
        MscCheck(type=MscOpportunityType.PRICE_MATCH,
                 status=MscCheckStatus.OPPORTUNITY,
                 note="fare dropped $180", estimated_value=180.0, value_unit="USD"),
        MscCheck(type=MscOpportunityType.VOYAGERS_SELECTION,
                 status=MscCheckStatus.NO_OPPORTUNITY, note="already enrolled"),
    ]
    result = MscBookingResult(
        cruise_line=CruiseLine.MSC, booking_id="3000030", category="BR1",
        checks=checks, has_any_opportunity=True, note="1 of 4 levers available",
    )
    return MscCheckOutcome(booking_id="3000030", status="checked",
                           result=result, note=result.note)


def test_msc_row_shows_per_check_detail(window):
    """Previously only a "1/2 checks" count survived — the per-check notes
    naming the specific discount and its rationale were dropped."""
    window._append_msc_result_row(_msc_outcome())
    detail = _cell(window, "3000030", 8)
    assert "PRICE_MATCH:OPPORTUNITY" in detail
    assert "fare dropped $180" in detail
    assert "VOYAGERS_SELECTION" in detail
    assert _cell(window, "3000030", 1) == "MSC"
    assert _cell(window, "3000030", 7) == "BR1"


def test_msc_row_does_not_invent_a_single_total(window):
    """MSC's four checks are independent levers; there is no single
    old/new total, so showing $0.00 would be a lie."""
    window._append_msc_result_row(_msc_outcome())
    assert _cell(window, "3000030", 5) == "—"
    assert _cell(window, "3000030", 6) == "—"


def test_msc_short_circuit_outcome_still_names_its_reason(window):
    """not_found / cancelled / session_expired_after_relogin carry no
    result object at all — the status and note are all there is."""
    window._append_msc_result_row(MscCheckOutcome(
        booking_id="3000024", status="not_found", result=None,
        note="booking not found in MSC portal",
    ))
    assert _cell(window, "3000024", 2) == "NOT_FOUND"
    assert "not found" in _cell(window, "3000024", 8)


def test_mixed_msc_and_standard_rows_sort_without_error(window):
    """Both row builders must produce comparable cells in columns 3-6."""
    window._append_result_row(BookingResult(
        booking_id="AAA", cruise_line=CruiseLine.NCL,
        status=BookingStatus.OPTIMIZATION, net_saving=60.0, confidence=95,
        old_total=1000.0, new_total=940.0,
    ))
    window._append_msc_result_row(_msc_outcome())
    for col in (3, 4, 5, 6):
        window.results_table.sortItems(col, Qt.SortOrder.DescendingOrder)
        assert window.results_table.rowCount() == 2
    # MSC has no dollar totals, so it sorts below a real one rather than
    # masquerading as $0.00.
    window.results_table.sortItems(5, Qt.SortOrder.DescendingOrder)
    assert window.results_table.item(0, 0).text() == "AAA"


# ── progress display ──────────────────────────────────────────────


def _snapshot(**kw) -> QueueSnapshot:
    base = dict(items=[], queued=0, running=0, done=0, error=0)
    base.update(kw)
    return QueueSnapshot(**base)


def test_queue_label_shows_progress_counts(window):
    """The ScanJob counts were already reaching the GUI on every progress
    tick and were thrown away, so a long standard-path scan showed a
    static "N pending, N running" — indistinguishable from a hang."""
    window._update_queue_view(_snapshot(
        items=[QueueItem("A", QueueStatus.DONE), QueueItem("B", QueueStatus.RUNNING),
               QueueItem("C", QueueStatus.QUEUED)],
        queued=1, running=1, done=1,
        current_booking_id="B", progress_done=1, progress_total=3,
    ))
    label = window.queue_status_label.text()
    assert "1/3 (33%)" in label
    assert "checking B" in label
    assert "1 done" in label


def test_queue_label_omits_progress_when_there_is_none(window):
    """The MSC path drives its own loop, so queue_manager._job is None and
    both counts are 0. Showing "0/0 (0%)" there would be noise, and the
    percentage would divide by zero."""
    window._update_queue_view(_snapshot(items=[QueueItem("A")], queued=1))
    label = window.queue_status_label.text()
    assert "0/0" not in label
    assert "%" not in label


def test_queue_label_reports_errors(window):
    window._update_queue_view(_snapshot(
        items=[QueueItem("A", QueueStatus.ERROR)], error=1,
        progress_done=1, progress_total=1,
    ))
    assert "1 error" in window.queue_status_label.text()


def test_progress_never_exceeds_the_total(window):
    """progress_done is set to i BEFORE a booking and i+1 after, so the
    display adds 1 for the in-flight booking — that must not read
    "4/3 (133%)" on the last one."""
    window._update_queue_view(_snapshot(
        items=[QueueItem("C", QueueStatus.RUNNING)], running=1,
        current_booking_id="C", progress_done=3, progress_total=3,
    ))
    label = window.queue_status_label.text()
    assert "3/3 (100%)" in label
    assert "133%" not in label


def test_queue_list_is_not_rebuilt_on_an_unchanged_snapshot(window):
    """Rebuilding every row twice a second threw away the operator's
    scroll position mid-scan (and, on a 100-booking watchlist, rebuilt 100
    widgets for nothing)."""
    snapshot = _snapshot(items=[QueueItem("A"), QueueItem("B")], queued=2)
    window._update_queue_view(snapshot)
    assert window.queue_list.count() == 2

    window.queue_list.clear()          # stand-in for "operator scrolled"
    window._update_queue_view(snapshot)
    assert window.queue_list.count() == 0, "rebuilt despite no change"


def test_queue_list_is_rebuilt_when_a_status_changes(window):
    window._update_queue_view(_snapshot(items=[QueueItem("A")], queued=1))
    window.queue_list.clear()
    window._update_queue_view(_snapshot(
        items=[QueueItem("A", QueueStatus.RUNNING)], running=1))
    assert window.queue_list.count() == 1


def test_queue_list_is_rebuilt_when_an_item_is_added(window):
    window._update_queue_view(_snapshot(items=[QueueItem("A")], queued=1))
    window.queue_list.clear()
    window._update_queue_view(_snapshot(items=[QueueItem("A"), QueueItem("B")], queued=2))
    assert window.queue_list.count() == 2


def test_populate_results_table_uses_the_nine_column_layout(window):
    """_populate_results_table was a duplicate row writer still hard-coded
    to the old 4 columns, writing status into "Line" and confidence into
    "Net Saving". Unreachable today, but a landmine for the next caller."""
    window.results.append(BookingResult(
        booking_id="RP1", cruise_line=CruiseLine.NCL,
        status=BookingStatus.OPTIMIZATION, old_total=1000.0, new_total=940.0,
        net_saving=60.0, confidence=95, price_category="BX",
        note="safe to optimize",
    ))
    window.msc_results.append(_msc_outcome())
    window._populate_results_table()

    assert window.results_table.rowCount() == 2
    assert _cell(window, "RP1", 1) == "NCL"            # Line, not status
    assert _cell(window, "RP1", 2) == "OPTIMIZATION"   # Status
    assert _cell(window, "RP1", 4) == "95"             # Confidence
    assert _cell(window, "RP1", 5) == "$1,000.00"
    assert _cell(window, "RP1", 8) == "safe to optimize"
    assert _cell(window, "3000030", 1) == "MSC"


def test_populate_results_table_is_idempotent(window):
    """Called twice it must not double the rows — it clears first."""
    window.results.append(BookingResult(
        booking_id="RP2", cruise_line=CruiseLine.NCL,
        status=BookingStatus.NO_SAVING, net_saving=0.0, confidence=50,
    ))
    window._populate_results_table()
    window._populate_results_table()
    assert window.results_table.rowCount() == 1
