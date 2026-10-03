"""Dashboard and Logs tabs; the bottom log strip is gone.

Neon 2026-10-01, marking the bottom panel "L" on a screenshot:

    "Remove the large logs section from the bottom of the main GUI ...
     create a dedicated Logs tab"
    "Add a dedicated Dashboard tab"

WHAT THE OLD STRIP WAS: a `QTextEdit` pinned to 110px under every screen -
six lines of a 721-booking run - unfiltered, unbounded for the life of the
process, and repainted on every append directly beneath the results table
the operator was trying to read.

PERFORMANCE IS PART OF THE REQUIREMENT, not an afterthought. This GUI has
frozen twice in recorded incidents (a `print()` to a paused console, then a
logging call), so a monitoring view is exactly the kind of thing that
quietly repaints forever. The constraints these tests hold:

  * neither tab starts a timer - both are driven by the window's existing
    3-second resource tick, so adding them did not change sampling rates;
  * the log view is BOUNDED, unlike the strip it replaces;
  * the dashboard writes only values that CHANGED, because setText with an
    identical string still schedules a repaint.
"""

import os

import pytest

# Headless, and skipped where PySide6 is absent - the in-repo venv/ has
# none, C:\cruisevenv does. Same guard the other GUI tests use.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")
pytest.importorskip("qasync", reason="GUI tests need qasync")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.monitor_tabs import (  # noqa: E402
    _MAX_LOG_LINES,
    DashboardPanel,
    LogsPanel,
    classify_line,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


# -- log line classification --------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("espresso.booking_release_failed error=Timeout", "ERROR"),
    ("All 3 attempts failed", "ERROR"),
    ("browser.navigate_retry retrying", "WARNING"),
    ("Locator timeout 4000ms", "WARNING"),
    ("espresso.search booking_id=3001018", "INFO"),
    ("result booking_id=3001012 status=NO_SAVING", "INFO"),
])
def test_lines_are_classified_for_colour_and_filtering(text, expected):
    assert classify_line(text) == expected


def test_an_ordinary_line_is_info_not_a_false_error():
    """A noisy classifier would paint a normal run red and be ignored."""
    assert classify_line("release_booking via=ignoreReservationLink confirmed=True") == "INFO"


# -- the Logs tab --------------------------------------------------------


def test_the_log_view_is_bounded(qt_app):
    """The strip it replaces grew without limit for the life of the
    process; a 721-booking run writes thousands of lines."""
    panel = LogsPanel()
    for n in range(_MAX_LOG_LINES + 500):
        panel.append(f"line {n}")

    assert len(panel._lines) == _MAX_LOG_LINES


def test_appending_never_raises(qt_app):
    """A logging view must not be able to take down the window it reports
    on - the exact failure mode of the two recorded GUI freezes."""
    panel = LogsPanel()
    panel.append(None)
    panel.append(12345)
    panel.append("ok")
    assert len(panel._lines) == 3


def test_a_level_filter_hides_lines_without_losing_them(qt_app):
    panel = LogsPanel()
    panel.append("something failed badly")
    panel.append("ordinary progress")

    panel.level_boxes["ERROR"].setChecked(False)

    assert len(panel._lines) == 2, "filtering must not discard history"
    assert "failed badly" not in panel.view.toPlainText()
    assert "ordinary progress" in panel.view.toPlainText()


def test_re_enabling_a_level_brings_the_lines_back(qt_app):
    panel = LogsPanel()
    panel.append("something failed badly")
    panel.level_boxes["ERROR"].setChecked(False)
    panel.level_boxes["ERROR"].setChecked(True)

    assert "failed badly" in panel.view.toPlainText()


def test_text_search_filters(qt_app):
    panel = LogsPanel()
    panel.append("booking 111 done")
    panel.append("booking 222 done")

    panel.search_box.setText("111")

    text = panel.view.toPlainText()
    assert "111" in text and "222" not in text


def test_search_is_case_insensitive(qt_app):
    panel = LogsPanel()
    panel.append("ESPRESSO navigate")
    panel.search_box.setText("espresso")
    assert "ESPRESSO" in panel.view.toPlainText()


def test_pause_stops_autoscroll_but_keeps_collecting(qt_app):
    """A log that scrolls while you read it is a log you cannot read -
    which is what made the old strip useless during a scan."""
    panel = LogsPanel()
    panel.pause_button.setChecked(True)
    panel.append("still collected")

    assert panel._paused is True
    assert "still collected" in panel.view.toPlainText()


def test_clear_empties_the_view_only(qt_app):
    panel = LogsPanel()
    panel.append("gone from view")
    panel.clear()
    assert panel.view.toPlainText().strip() == ""
    assert len(panel._lines) == 0


def test_the_logs_panel_starts_no_timer(qt_app):
    """It is fed by appends, not by polling."""
    from PySide6.QtCore import QTimer

    panel = LogsPanel()
    assert not panel.findChildren(QTimer)


# -- the Dashboard tab ---------------------------------------------------


def test_the_dashboard_shows_the_fields_that_matter(qt_app):
    panel = DashboardPanel()
    for key in ("state", "line", "booking", "progress", "login", "browser",
                "last_scan", "next_scan", "reused", "errors", "cpu"):
        assert key in panel._values, f"the dashboard has no {key} field"


def test_updating_writes_the_value(qt_app):
    panel = DashboardPanel()
    panel.update_fields({"state": "SCANNING", "booking": "3001018"})

    assert panel._values["state"].text() == "SCANNING"
    assert panel._values["booking"].text() == "3001018"


def test_an_unchanged_value_is_not_rewritten(qt_app):
    """setText with an identical string still schedules a repaint, and
    this runs every 3 seconds for the life of the window."""
    panel = DashboardPanel()
    panel.update_fields({"state": "IDLE"})

    writes = []
    original = panel._values["state"].setText
    panel._values["state"].setText = lambda t: (writes.append(t), original(t))

    panel.update_fields({"state": "IDLE"})        # same
    panel.update_fields({"state": "SCANNING"})    # different

    assert writes == ["SCANNING"], f"unchanged value was repainted: {writes}"


def test_a_missing_value_renders_as_a_dash_not_none(qt_app):
    panel = DashboardPanel()
    panel.update_fields({"booking": None})
    assert panel._values["booking"].text() == "—"


def test_updating_never_raises(qt_app):
    panel = DashboardPanel()
    panel.update_fields({"not_a_field": "x"})
    panel.update_fields(None or {})


def test_dashboard_values_do_not_wrap(qt_app):
    """THE CLIPPING BUG. Word wrap on a single-line value makes a
    QGridLayout row report a height-for-width it is not given, so
    "CPU 42.0%  RAM 57.6%" rendered cut off mid-character. Neon:
    *"dashboard still looks like shit"* - and it did."""
    panel = DashboardPanel()
    for key, label in panel._values.items():
        assert label.wordWrap() is False, f"{key} wraps and will clip"


def test_the_dashboard_starts_no_timer(qt_app):
    """It is driven by the window's existing resource tick. A timer here
    would double the most expensive thing the GUI does."""
    from PySide6.QtCore import QTimer

    panel = DashboardPanel()
    assert not panel.findChildren(QTimer)


# -- the main window ----------------------------------------------------


def test_the_window_has_a_dashboard_tab(qt_app):
    from core.models import CruiseLine
    from gui.windows import MainWindow

    window = MainWindow()
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]

    assert any("Dashboard" in t for t in titles), titles
    # The cruise lines keep the leading positions, so anything addressing a
    # tab by index still lands on the same line.
    for index, line in enumerate(CruiseLine):
        assert window.tabs.tabText(index) == line.value


def test_logs_are_a_button_not_a_tab(qt_app):
    """REVERSED 2026-10-01. The first version made Logs a tab. Neon: *"i
    do not want a logs tab i want a logs button when i press it it opens
    another window that list the logs."*

    He is right about the shape: logs are consulted WHILE watching a scan,
    and a tab replaced the view being watched - no better than the 110px
    strip it came from."""
    from gui.windows import MainWindow

    window = MainWindow()
    titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]

    assert not any("Logs" in t for t in titles), f"Logs is a tab again: {titles}"
    assert hasattr(window, "logs_button")
    assert window.logs_button.text() == "Logs"


def test_the_logs_button_opens_a_window(qt_app):
    from gui.windows import MainWindow

    window = MainWindow()
    assert window._logs_window is None, "built before it was needed"

    window._on_show_logs()

    assert window._logs_window is not None
    assert window._logs_window.isVisible()


def test_the_logs_window_is_reused_not_rebuilt(qt_app):
    """A second window would leave half the session's history in the one
    just closed - and the panel inside is the live sink."""
    from gui.windows import MainWindow

    window = MainWindow()
    window._on_show_logs()
    first = window._logs_window
    window._on_show_logs()

    assert window._logs_window is first


def test_the_logs_window_is_non_modal(qt_app):
    """It sits beside the scanner; it must not block it."""
    from gui.windows import MainWindow

    window = MainWindow()
    window._on_show_logs()
    assert window._logs_window.isModal() is False


def test_closing_the_logs_window_keeps_the_log(qt_app):
    """Qt would otherwise destroy the panel, silently throwing away the
    log for the rest of the session."""
    from core.models import CruiseLine
    from gui.windows import MainWindow

    window = MainWindow()
    window._append_activity(CruiseLine.ESPRESSO, "before closing")
    window._on_show_logs()
    window._logs_window.close()

    window._append_activity(CruiseLine.ESPRESSO, "after closing")
    assert "before closing" in window.logs_panel.toPlainText()
    assert "after closing" in window.logs_panel.toPlainText()


def test_the_bottom_log_strip_is_gone(qt_app):
    """The 110px fixed-height QTextEdit under every screen."""
    import pathlib

    source = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    assert "setFixedHeight(110)" not in source
    assert 'QLabel("Activity log (all lines):")' not in source


def test_activity_still_reaches_the_logs_tab(qt_app):
    """`self.activity_log` is kept as a name because _append_activity and
    every panel write to it. It must now be the Logs view."""
    from core.models import CruiseLine
    from gui.windows import MainWindow

    window = MainWindow()
    window._append_activity(CruiseLine.ESPRESSO, "a thing happened")

    assert window.activity_log is window.logs_panel
    assert "a thing happened" in window.logs_panel.view.toPlainText()


def test_the_window_still_has_one_resource_timer(qt_app):
    """Adding two tabs must not add sampling. The Chromium census is the
    most expensive thing this GUI does (median 66ms, max 900ms on the UI
    thread)."""
    from gui.windows import MainWindow

    window = MainWindow()
    assert window._resource_timer.isActive()
    assert window._resource_timer.interval() == 3000


def test_the_logs_panel_answers_toplaintext_like_the_widget_it_replaced(qt_app):
    """`MainWindow.activity_log` is a compatibility name for the old
    QTextEdit. Dropping this method broke the shared-log guarantee in
    test_tabbed_gui_2026_08_28.py - that one tab's activity is visible
    without switching to it."""
    panel = LogsPanel()
    panel.append("[NCL] search booking_id=3000055")
    assert "3000055" in panel.toPlainText()
