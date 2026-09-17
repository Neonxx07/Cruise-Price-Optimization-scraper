"""The tabbed GUI: one panel per cruise line, lines genuinely independent.

Neon: "run the new gui and make sure that now we can run alot of cruise
lines together", then "change the gui to tabs then so a tab is esspresso
another tab is ncl another tab is msc etc".

WHY THE OLD DESIGN MADE CONCURRENCY IMPOSSIBLE - this is the property the
tests below actually protect. The single window had ONE
BookingQueueManager, and its BookingService keeps a single `_live_scraper`
slot that it STOPS whenever the requested cruise line changes (see
BookingService.get_or_create_scraper). Logging into ESPRESSO and then into
NCL therefore destroyed the ESPRESSO session. No UI change alone could fix
that; the per-line state had to be separated.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")
pytest.importorskip("qasync", reason="GUI tests need qasync")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.models import BookingResult, BookingStatus, CruiseLine  # noqa: E402
from gui.windows import CruiseLinePanel, MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qt_app):
    w = MainWindow()
    try:
        yield w
    finally:
        w.setParent(None)
        w.deleteLater()
        qt_app.processEvents()


def test_there_is_a_tab_for_every_cruise_line(win):
    titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
    for line in CruiseLine:
        assert line.value in titles, f"{line.value} has no tab"
    assert set(win.panels) == set(CruiseLine)


def test_each_tab_has_its_own_queue_manager(win):
    """THE fix. Sharing one manager is what made two live sessions
    impossible - its BookingService stops the live scraper whenever the
    cruise line changes."""
    managers = {id(p.queue_manager) for p in win.panels.values()}
    assert len(managers) == len(win.panels), (
        "panels are sharing a BookingQueueManager - concurrent lines would "
        "still evict each other's session"
    )


def test_each_tab_has_its_own_booking_service_and_results(win):
    services = {id(p.queue_manager._service) for p in win.panels.values()}
    assert len(services) == len(win.panels), "panels share a BookingService"
    results = {id(p.results) for p in win.panels.values()}
    assert len(results) == len(win.panels), "panels share a results list"


def test_a_panel_knows_its_own_line_and_has_no_dropdown(win):
    for line, panel in win.panels.items():
        assert panel.cruise_line is line
        assert not hasattr(panel, "cruise_line_selector"), (
            "the dropdown is what forced one shared session slot"
        )


def test_one_line_being_busy_does_not_block_another(win):
    """Two panels must report busy independently - the whole point."""
    ncl = win.panels[CruiseLine.NCL]
    esp = win.panels[CruiseLine.ESPRESSO]

    class _Busy:
        is_running = True

        def has_live_session(self, *a, **kw):
            return True

    ncl.queue_manager = _Busy()
    assert ncl.is_busy() is True
    assert esp.is_busy() is False, "one busy line marked another as busy"


def test_login_confirmation_is_per_line(win):
    """`_login_ok_for` used to be one window-wide flag, so confirming a
    login for one line implicitly spoke for all of them."""
    ncl = win.panels[CruiseLine.NCL]
    esp = win.panels[CruiseLine.ESPRESSO]
    ncl._login_ok_for = CruiseLine.NCL
    assert esp._login_ok_for is None


def test_footer_totals_span_every_tab(win):
    """Money found on a hidden tab must still appear in the footer -
    otherwise switching tabs hides results."""
    for line, net in ((CruiseLine.NCL, 112.0), (CruiseLine.ESPRESSO, 60.0)):
        panel = win.panels[line]
        r = BookingResult(booking_id=f"X{line.value}", cruise_line=line,
                          status=BookingStatus.OPTIMIZATION,
                          net_saving=net, confidence=5)
        panel.results.append(r)
        panel._append_result_row(r)
        panel._refresh_summary()

    footer = win.global_summary_label.text()
    assert "$172.00" in footer, f"footer did not total both lines: {footer}"
    assert "2 optimization(s)" in footer


def test_footer_flags_unconfirmed_separately(win):
    """$4,100 of the all-time total was once unverified GoCCL candidates -
    they must be visible but NOT inside the confirmed figure."""
    panel = win.panels[CruiseLine.GOCCL]
    r = BookingResult(
        booking_id="DEMO01", cruise_line=CruiseLine.GOCCL,
        status=BookingStatus.OPTIMIZATION, net_saving=1560.0, confidence=1,
        note="candidate $1560 - UNCONFIRMED, run preview_fare_code to verify",
    )
    panel.results.append(r)
    panel._refresh_summary()
    footer = win.global_summary_label.text()
    assert "$0.00 confirmed savings" in footer, footer
    assert "UNCONFIRMED" in footer
    assert "1,560.00" in footer


def test_activity_from_any_tab_reaches_the_shared_log(win):
    """A concurrent line's activity must be visible without switching tabs."""
    win.panels[CruiseLine.NCL]._on_action(
        {"timestamp": "12:00:00", "action": "search", "booking_id": "3000055"}
    )
    text = win.activity_log.toPlainText()
    assert "[NCL]" in text and "3000055" in text


def test_shutdown_covers_every_panel(win):
    """With several lines able to hold browsers at once, closing only the
    visible tab would leak the rest."""
    import asyncio

    closed = []
    for line, panel in win.panels.items():
        async def _fake(_line=line):
            closed.append(_line)
        panel.shutdown = _fake

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(win._shutdown_all())
    finally:
        loop.close()
    assert set(closed) == set(CruiseLine), f"only shut down {closed}"


# -- layout regressions from the first tabbed build ----------------


def test_footer_widgets_are_not_duplicated_inside_a_panel(qt_app):
    """The first tabbed screenshot showed "Cruise-line scanners",
    "Resources", "Last completed scan" and "Activity log" rendered INSIDE
    the ESPRESSO tab AND again in the shell footer. The panel still
    constructs them (other methods write to them by name) but must not lay
    them out."""
    panel = CruiseLinePanel(CruiseLine.ESPRESSO)
    for name in ("line_status_table", "resource_label",
                 "last_scan_label", "activity_log"):
        widget = getattr(panel, name)
        assert widget is not None, f"{name} must still exist for its writers"
        assert widget.parentWidget() is None, (
            f"{name} is laid out inside the panel - it duplicates the footer"
        )


def test_action_buttons_have_room_and_do_not_clip(qt_app):
    """The four buttons sat in a 2x2 grid sharing columns with the inputs,
    so they overlapped and clipped."""
    panel = CruiseLinePanel(CruiseLine.ESPRESSO)
    for button in (panel.login_button, panel.start_button,
                   panel.stop_button, panel.add_booking_button):
        assert button.minimumWidth() >= 90, (
            f"{button.text()!r} has no minimum width and will clip"
        )


def test_results_table_gets_the_space(qt_app):
    """Nothing had a stretch factor, so the results table - the actual
    output - was squeezed to ~126px while empty boxes took the room."""
    panel = CruiseLinePanel(CruiseLine.NCL)
    assert panel.results_table.minimumHeight() >= 200
    assert panel.queue_list.height() <= 120


def test_status_tints_are_soft_not_saturated_primaries(qt_app):
    """Qt.green / Qt.red / Qt.yellow made black text genuinely hard to read.
    These now match services/excel_export.py's palette, so a row is the same
    colour on screen as in the exported spreadsheet."""
    panel = CruiseLinePanel(CruiseLine.NCL)
    for status in ("OPTIMIZATION", "TRAP", "NO_SAVING", "ERROR"):
        colour = panel._color_for_status(status)
        assert min(colour.red(), colour.green(), colour.blue()) >= 150, (
            f"{status} tint {colour.name()} is too dark to read text on"
        )

    from services.excel_export import _FILLS

    screen = panel._STATUS_TINTS["OPTIMIZATION"].lstrip("#").upper()
    excel = _FILLS["OPTIMIZATION"].fgColor.rgb.upper()
    assert screen in excel, (
        f"screen tint {screen} and Excel fill {excel} have drifted apart"
    )


# -- results must survive a restart (the "zero optimization" bug) ----


@pytest.mark.asyncio
async def test_results_are_reloaded_from_the_database(qt_app, monkeypatch):
    """CONFIRMED DEFECT, fixed 2026-08-28. Neon: "please look at the
    esspresso run there is zero optimization why is that". The run had found
    **19 optimizations worth $3,175** - scan_job 769/769 COMPLETED - and
    every row was in the DB. `self.results` is in-memory only and nothing
    ever read it back, so after a restart the table was blank and a good run
    looked like a wasted afternoon.

    The data was never lost; it just stopped being SHOWN. That is the worst
    failure mode this app has, because it is invisible.
    """
    from datetime import datetime

    from models.database import BookingRecord, async_session, init_db

    await init_db()
    async with async_session() as s:
        s.add(BookingRecord(
            booking_id="RELOAD1", cruise_line="ESPRESSO", status="OPTIMIZATION",
            old_total=4820.50, new_total=4760.50, net_saving=60.0,
            confidence=5, price_category="BX", note="optimized $60",
            obc_change=0.0, price_drop=60.0, lost_pkg_value=0.0,
            created_at=datetime.utcnow(),
        ))
        await s.commit()

    panel = CruiseLinePanel(CruiseLine.ESPRESSO)
    assert panel.results == []
    assert panel.results_table.rowCount() == 0

    loaded = await panel.load_todays_results()
    assert loaded >= 1, "nothing was restored from the database"
    assert any(r.booking_id == "RELOAD1" for r in panel.results)
    assert panel.results_table.rowCount() >= 1
    assert "60" in panel.summary_label.text()


@pytest.mark.asyncio
async def test_reload_keeps_only_the_latest_row_per_booking(qt_app):
    """A re-scan supersedes an earlier result. Showing both would
    double-count the money in the footer total."""
    from datetime import datetime

    from models.database import BookingRecord, async_session, init_db

    await init_db()
    async with async_session() as s:
        for net in (50.0, 75.0):
            s.add(BookingRecord(
                booking_id="DUPE1", cruise_line="NCL", status="OPTIMIZATION",
                old_total=1000.0, new_total=1000.0 - net, net_saving=net,
                confidence=5, created_at=datetime.utcnow(),
            ))
        await s.commit()

    panel = CruiseLinePanel(CruiseLine.NCL)
    await panel.load_todays_results()
    dupes = [r for r in panel.results if r.booking_id == "DUPE1"]
    assert len(dupes) == 1, f"booking appeared {len(dupes)} times"


@pytest.mark.asyncio
async def test_a_reload_failure_never_stops_the_gui_opening(qt_app, monkeypatch):
    """A display convenience must not be able to block startup."""
    panel = CruiseLinePanel(CruiseLine.GOCCL)

    import models.database as db

    def boom(*a, **kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "async_session", boom)
    assert await panel.load_todays_results() == 0
