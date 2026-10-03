"""A booking scanned twice in a day is ONE booking, not two.

THE BUG, 2026-09-30, from Neon's screenshots. The header read

    Bookings watched: 408   Optimizations: 8   Total savings: $725.00

while only 167 bookings had been checked. The database showed why:

    3001011   17:11 OPTIMIZATION $79    <- earlier run
             18:37 OPTIMIZATION $79    <- this run
    3001016  17:38 OPTIMIZATION $199
             18:39 OPTIMIZATION $199

Two SEPARATE scans hours apart, correctly recorded as history - but the GUI
loads today's earlier results on startup, appends the new run's, and counted
both. The true figures were 6 optimizations and $526.

An inflated savings total is the worst kind of wrong here: it is the number
the whole product is judged on, and it is the one Neon reports upward.

THE RE-SCAN ITSELF WAS CORRECT. OPTIMIZATION is in
`settings.never_cache_statuses` because a live saving must always be
re-confirmed rather than served from a cache. Duplicates within a day are
therefore EXPECTED, and the display has to cope with them.
"""

import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")
pytest.importorskip("qasync", reason="GUI tests need qasync")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import (  # noqa: E402
    BookingResult,
    BookingStatus,
    CruiseLine,
)
from gui.windows import CruiseLinePanel  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    """A fresh panel per test - the results table is stateful.

    Teardown mirrors test_gui_results_table.py: NOT win.close(), because
    closeEvent deliberately defers to an async shutdown that has no loop
    here. Deleting the widget is what clears it from the QApplication.
    """
    win = CruiseLinePanel(CruiseLine.ESPRESSO)
    try:
        yield win
    finally:
        win.setParent(None)
        win.deleteLater()
        qt_app.processEvents()
        qt_app.sendPostedEvents(None, 0)


def _result(booking_id, net, when, line=CruiseLine.ESPRESSO,
            status=BookingStatus.OPTIMIZATION):
    return BookingResult(
        booking_id=booking_id, cruise_line=line, status=status,
        old_total=1000.0, new_total=1000.0 - net, net_saving=net,
        confidence=5, checked_at=when)


def test_a_booking_scanned_twice_counts_once(window):
    now = datetime.utcnow()
    window.results.extend([
        _result("3001011", 79.0, now - timedelta(hours=1)),
        _result("3001011", 79.0, now),
    ])
    unique = window._latest_per_booking()
    assert len(unique) == 1


def test_the_newest_scan_wins(window):
    now = datetime.utcnow()
    window.results.extend([
        _result("3001011", 79.0, now - timedelta(hours=1)),
        _result("3001011", 120.0, now),
    ])
    unique = window._latest_per_booking()
    assert unique[0].net_saving == 120.0


def test_savings_are_not_double_counted(window):
    """$725 when the truth was $526."""
    now = datetime.utcnow()
    window.results.extend([
        _result("3001011", 79.0, now - timedelta(hours=1)),
        _result("3001011", 79.0, now),
        _result("3001016", 199.0, now - timedelta(hours=1)),
        _result("3001016", 199.0, now),
    ])
    unique = window._latest_per_booking()
    assert sum(r.net_saving for r in unique) == 278.0


def test_the_same_number_on_two_lines_stays_two_bookings(window):
    """Booking numbers are only unique WITHIN a portal."""
    now = datetime.utcnow()
    window.results.extend([
        _result("12345", 50.0, now, line=CruiseLine.ESPRESSO),
        _result("12345", 60.0, now, line=CruiseLine.NCL),
    ])
    assert len(window._latest_per_booking()) == 2


def test_different_bookings_are_all_kept(window):
    now = datetime.utcnow()
    window.results.extend([_result(str(i), 10.0, now) for i in range(5)])
    assert len(window._latest_per_booking()) == 5


def test_every_result_carries_a_timestamp_to_order_by():
    """The dedup picks the newest scan, so it depends on checked_at being
    present. It is non-nullable with a default_factory, so "no timestamp"
    cannot arise - the getattr guard in _latest_per_booking is for objects
    that are not BookingResults at all, not for missing dates."""
    from core.models import BookingResult as BR
    field = BR.model_fields["checked_at"]
    assert field.default_factory is not None
    assert field.is_required() is False


def test_identical_timestamps_do_not_lose_a_booking(window):
    """Two scans in the same instant is unlikely but must still collapse to
    one booking rather than dropping it."""
    now = datetime.utcnow()
    window.results.extend([_result("A", 10.0, now), _result("A", 20.0, now)])
    unique = window._latest_per_booking()
    assert len(unique) == 1
    assert unique[0].net_saving == 20.0


# ── the table row ────────────────────────────────────────────────────────


def test_a_rescan_replaces_the_row_rather_than_adding_one(window):
    now = datetime.utcnow()
    window._append_result_row(_result("3001011", 79.0, now - timedelta(hours=1)))
    window._append_result_row(_result("3001011", 79.0, now))
    ids = [window.results_table.item(r, 0).text()
           for r in range(window.results_table.rowCount())]
    assert ids.count("3001011") == 1


def test_the_replaced_row_shows_the_NEW_figures(window):
    now = datetime.utcnow()
    window._append_result_row(_result("X1", 79.0, now - timedelta(hours=1)))
    window._append_result_row(_result("X1", 250.0, now))
    row = window._find_result_row(_result("X1", 0.0, now))
    assert "250" in window.results_table.item(row, 3).text()


def test_the_same_number_on_another_line_gets_its_own_row(window):
    now = datetime.utcnow()
    window._append_result_row(_result("777", 10.0, now, line=CruiseLine.ESPRESSO))
    window._append_result_row(_result("777", 20.0, now, line=CruiseLine.NCL))
    assert window.results_table.rowCount() == 2
