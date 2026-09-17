"""Rows arranged the way the work is prioritised; records picked by date.

Neon 2026-09-03: "i want the infromation and the data to be more accurate
is to be more sorted we must sort and arange everything properly".

Two unrelated defects behind that, both confirmed by measurement:

  1. SORTING. Booking ids sorted as TEXT, so four real ids ordered
     1000, 3000055, 70, 999. Status sorted ALPHABETICALLY, so clicking it
     put NO_SAVING and TRAP above OPTIMIZATION - burying the findings worth
     acting on, because "N" and "T" fall either side of "O".
  2. FRESHNESS. Every reader kept whichever record it read LAST, which is
     only the newest while append order matches chronological order.
     booking_data.jsonl and rate_check_data.jsonl each hold 4 timestamp
     inversions; they happen to fall between bookings rather than within
     one, so last-wins was right on all 118/89 bookings by luck.
     live_check_results.jsonl (154 records) carried no timestamp at all.
"""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import BookingResult, BookingStatus, CruiseLine  # noqa: E402
from gui.windows import (  # noqa: E402
    CruiseLinePanel,
    _booking_id_sort_value,
    _row_rank,
    _status_rank,
)
from msc_run_calculator import _load_last_by_id  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _panel_with(rows):
    panel = CruiseLinePanel(CruiseLine.NCL)
    for bid, net, status in rows:
        r = BookingResult(booking_id=bid, cruise_line=CruiseLine.NCL,
                          status=status, net_saving=net, confidence=4,
                          old_total=5000.0, new_total=5000.0 - max(net, 0))
        panel.results.append(r)
        panel._append_result_row(r)
    return panel


# -- booking ids are numbers ----------------------------------------


def test_numeric_booking_ids_sort_numerically():
    """The four ids that exposed this ordered 1000, 3000055, 70, 999."""
    ids = ["999", "1000", "70", "3000055", "3000068", "3000064"]
    assert sorted(ids, key=_booking_id_sort_value) == [
        "70", "999", "1000", "3000068", "3000064", "3000055"]


def test_alphanumeric_ids_sort_after_numeric_ones():
    """GoCCL ids (DEMO02, DEMO07) have no numeric value and must not sort
    ahead of real numbers."""
    assert _booking_id_sort_value("DEMO02") == float("inf")
    assert _booking_id_sort_value("70") < _booking_id_sort_value("DEMO07")


@pytest.mark.parametrize("weird", ["", None, "  ", "70-A"])
def test_an_odd_id_does_not_crash_the_sort(weird):
    assert isinstance(_booking_id_sort_value(weird), float)


def test_the_table_sorts_ids_numerically(qt_app):
    panel = _panel_with([
        ("999", 60.0, BookingStatus.OPTIMIZATION),
        ("1000", 5.0, BookingStatus.OPTIMIZATION),
        ("70", 250.0, BookingStatus.OPTIMIZATION),
        ("3000055", 10.0, BookingStatus.OPTIMIZATION),
    ])
    panel.results_table.sortItems(0, Qt.SortOrder.AscendingOrder)
    got = [panel.results_table.item(r, 0).text() for r in range(4)]
    assert got == ["70", "999", "1000", "3000055"], got


# -- status ranks by importance, not by letter ----------------------


@pytest.mark.parametrize("worse", [
    "TRAP", "NO_SAVING", "PAID_IN_FULL", "WLT", "ERROR", "SKIPPED",
    "NOT_ON_THIS_ACCOUNT",
])
def test_optimization_outranks_every_lesser_status(worse):
    assert _status_rank("OPTIMIZATION") < _status_rank(worse)


def test_msc_statuses_rank_alongside_the_others():
    """MSC rows say OPPORTUNITY/NO_OPPORTUNITY rather than
    OPTIMIZATION/NO_SAVING. A mixed table must interleave them by meaning,
    not group them by wording."""
    assert _status_rank("OPPORTUNITY") == _status_rank("OPTIMIZATION")
    assert _status_rank("NO_OPPORTUNITY") == _status_rank("NO_SAVING")


def test_an_unclassified_status_does_not_claim_the_top():
    """A status nobody has ranked yet must not silently outrank real
    findings, nor sort below hard errors."""
    assert _status_rank("SOMETHING_NEW") > _status_rank("OPTIMIZATION")
    assert _status_rank("SOMETHING_NEW") < _status_rank("ERROR")


# -- the default arrangement ----------------------------------------


def test_money_orders_rows_inside_a_status_band():
    assert _row_rank("OPTIMIZATION", 250.0) < _row_rank("OPTIMIZATION", 60.0)


def test_status_dominates_value():
    """A huge TRAP must never outrank a small real optimization - the 1e9
    multiplier exists for exactly this."""
    assert _row_rank("OPTIMIZATION", 1.0) < _row_rank("TRAP", 999_999.0)


def test_a_missing_value_does_not_promote_a_row():
    assert _row_rank("OPTIMIZATION", None) < _row_rank("TRAP", None)


def test_the_table_arrives_arranged_without_a_click(qt_app):
    """The whole point: opening the tab shows the work in priority order,
    biggest money first, errors last - no clicking required."""
    panel = _panel_with([
        ("1000", 5.0, BookingStatus.NO_SAVING),
        ("70", 250.0, BookingStatus.OPTIMIZATION),
        ("3000084", 0.0, BookingStatus.ERROR),
        ("999", 60.0, BookingStatus.OPTIMIZATION),
        ("3000055", -43.0, BookingStatus.TRAP),
        ("3000064", 120.0, BookingStatus.OPTIMIZATION),
    ])
    order = [(panel.results_table.item(r, 0).text(),
              panel.results_table.item(r, 2).text())
             for r in range(panel.results_table.rowCount())]
    assert [s for _, s in order] == [
        "OPTIMIZATION", "OPTIMIZATION", "OPTIMIZATION",
        "TRAP", "NO_SAVING", "ERROR"], order
    assert [b for b, _ in order][:3] == ["70", "3000064", "999"], (
        f"optimizations are not ordered by descending money: {order}")


# -- freshness: newest by DATE, not by read position ----------------


def _write(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return str(path)


def test_the_newest_record_wins_even_when_written_out_of_order(tmp_path):
    """CONFIRMED RISK. Concurrent scanning interleaves writes, so the last
    line for a booking need not be its newest capture. Picking by read
    position would report a stale price as today's - and for this exact
    booking the stale price is the fake $267.01 one."""
    path = _write(tmp_path / "rate_check_data.jsonl", [
        {"booking_id": "3000081", "captured_at": "2026-09-01T09:00:00",
         "today_price_same_category": "3610.66"},
        {"booking_id": "3000081", "captured_at": "2026-08-24T13:09:17",
         "today_price_same_category": "3250.33"},   # older, written LAST
    ])
    got = _load_last_by_id(path)
    assert got["3000081"]["today_price_same_category"] == "3610.66"


def test_a_stamped_record_beats_an_unstamped_one(tmp_path):
    path = _write(tmp_path / "d.jsonl", [
        {"booking_id": "A", "captured_at": "2026-09-01", "v": "new"},
        {"booking_id": "A", "v": "unstamped"},
    ])
    assert _load_last_by_id(path)["A"]["v"] == "new"


def test_two_unstamped_records_keep_the_old_last_wins_behaviour(tmp_path):
    """Captures predating the timestamp fix must not change meaning."""
    path = _write(tmp_path / "d.jsonl", [
        {"booking_id": "A", "v": "first"},
        {"booking_id": "A", "v": "second"},
    ])
    assert _load_last_by_id(path)["A"]["v"] == "second"


def test_a_record_with_no_booking_id_is_skipped(tmp_path):
    path = _write(tmp_path / "d.jsonl", [{"captured_at": "2026-09-01"}])
    assert _load_last_by_id(path) == {}


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert _load_last_by_id(str(tmp_path / "nope.jsonl")) == {}


def test_msc_live_results_are_written_with_a_timestamp():
    """154 stored records had none, so nothing downstream could tell which
    result for a booking was the current one."""
    import inspect

    import msc_commands

    src = inspect.getsource(msc_commands)
    seg = src[src.index("LIVE_CHECK_RESULTS_PATH), exist_ok=True"):][:900]
    assert "captured_at" in seg, "live_check_results is written unstamped"


def test_the_calculator_report_is_stamped():
    """calculator_results.jsonl is rewritten whole each run and carried no
    date, so a stale report was indistinguishable from a fresh one."""
    import inspect

    import msc_run_calculator

    src = inspect.getsource(msc_run_calculator)
    assert "generated_at" in src
