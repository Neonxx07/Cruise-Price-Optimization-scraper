"""The Dashboard and Logs tabs.

Neon 2026-10-01, with a screenshot marking the bottom log panel "L":
*"Remove the large logs section from the bottom of the main GUI ... create
a dedicated Logs tab"*, and separately *"Add a dedicated Dashboard tab"*.

KEPT IN THEIR OWN MODULE because `gui/windows.py` is already 2,400 lines
and holds the scanning workflow. Two monitoring views that read state and
write nothing do not belong in the middle of it.

PERFORMANCE IS A REQUIREMENT HERE, NOT A NICETY. This GUI has frozen twice
in recorded incidents - once on a `print()` to a paused console, once on a
logging call - and a monitoring view is exactly the kind of thing that
quietly repaints forever. So:

  * NO timer of their own. Both panels are refreshed by the window's
    existing 3-second resource tick, so the tab count does not change how
    often anything samples.
  * The dashboard only touches widgets whose text actually CHANGED.
    `QLabel.setText` with an identical string still triggers a repaint.
  * The log view is BOUNDED (`_MAX_LOG_LINES`). The old bottom panel grew
    without limit for the life of the process; a 721-booking run writes
    thousands of lines into it.
  * Filtering re-renders from an in-memory deque rather than asking Qt to
    search a document.
"""

from __future__ import annotations

import re
from collections import deque

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

#: Lines kept in the Logs tab. ~2,000 covers a full ESPRESSO run's visible
#: activity; the complete record is in data/cruiseintel.log, which is what an
#: incident is actually diagnosed from. Holding every line in a QTextEdit
#: is what makes a long-running window slow.
_MAX_LOG_LINES = 2000

#: Matched against the whole line, so a level mentioned inside a message
#: ("error_type=...") does not recolour an INFO line.
_LEVEL_PATTERNS = (
    ("ERROR", re.compile(r"\b(error|failed|failure|traceback|alarm)\b", re.I),
     "#C0392B"),
    ("WARNING", re.compile(r"\b(warn|warning|retry|retrying|timeout)\b", re.I),
     "#B9770E"),
    ("DEBUG", re.compile(r"\bdebug\b", re.I), "#7F8C8D"),
)
_DEFAULT_LEVEL = "INFO"
_DEFAULT_COLOUR = "#1C2833"


def classify_line(text: str) -> str:
    """ERROR / WARNING / DEBUG / INFO for one log line.

    Deliberately crude: these lines are free-form strings assembled by the
    scrapers, not structured records. The structured level lives in
    data/cruiseintel.log; this only has to be good enough to colour a line
    and drive a filter.
    """
    for level, pattern, _colour in _LEVEL_PATTERNS:
        if pattern.search(text):
            return level
    return _DEFAULT_LEVEL


def _colour_for(level: str) -> str:
    for name, _pattern, colour in _LEVEL_PATTERNS:
        if name == level:
            return colour
    return _DEFAULT_COLOUR


class LogsPanel(QWidget):
    """Every line the panels report, with filtering, in its own tab.

    Replaces the fixed 110px strip that sat under every screen. That strip
    was unreadable (six lines of a 721-booking run), unfiltered, unbounded,
    and repainted on every append while the operator was trying to read the
    results table above it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The full history, independent of what is currently displayed -
        # changing a filter must not lose lines.
        self._lines: deque[tuple[str, str]] = deque(maxlen=_MAX_LOG_LINES)
        self._paused = False
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Filter:"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "text to match — booking id, event name, anything")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._rerender)
        controls.addWidget(self.search_box, 1)

        self.level_boxes: dict[str, QCheckBox] = {}
        for level in ("ERROR", "WARNING", "INFO", "DEBUG"):
            box = QCheckBox(level)
            box.setChecked(level != "DEBUG")
            box.setStyleSheet(f"color: {_colour_for(level)}; font-weight: 600;")
            box.stateChanged.connect(self._rerender)
            self.level_boxes[level] = box
            controls.addWidget(box)

        # A log that scrolls while you are reading it is a log you cannot
        # read. This is why the old bottom strip was useless during a scan.
        self.pause_button = QPushButton("Pause")
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip(
            "Stop auto-scrolling so a line can be read while a scan runs. "
            "Lines are still collected.")
        self.pause_button.toggled.connect(self._on_pause)
        controls.addWidget(self.pause_button)

        clear_button = QPushButton("Clear")
        clear_button.setToolTip(
            "Clear this view only. data/cruiseintel.log is untouched.")
        clear_button.clicked.connect(self.clear)
        controls.addWidget(clear_button)
        outer.addLayout(controls)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.NoWrap)
        self.view.setStyleSheet(
            "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 11px;")
        outer.addWidget(self.view, 1)

        self.count_label = QLabel("0 lines")
        self.count_label.setStyleSheet("color: #566573; font-size: 11px;")
        outer.addWidget(self.count_label)

    # ── writing ──────────────────────────────────────────────────────

    def append(self, text: str) -> None:
        """Add one line. Never raises - a logging view must not be able to
        take down the window it reports on."""
        try:
            line = str(text)
            level = classify_line(line)
            self._lines.append((level, line))
            if self._passes_filter(level, line):
                self._write(level, line)
            self._update_count()
        except Exception:  # noqa: BLE001
            pass

    def _write(self, level: str, line: str) -> None:
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(_colour_for(level)))
        cursor.insertText(line + "\n", fmt)
        if not self._paused:
            self.view.moveCursor(QTextCursor.End)

    # ── filtering ────────────────────────────────────────────────────

    def _passes_filter(self, level: str, line: str) -> bool:
        box = self.level_boxes.get(level)
        if box is not None and not box.isChecked():
            return False
        needle = self.search_box.text().strip().lower()
        return not needle or needle in line.lower()

    def _rerender(self) -> None:
        """Rebuild from the deque. Cheaper and more predictable than asking
        Qt to hide lines inside an existing document."""
        try:
            self.view.clear()
            for level, line in self._lines:
                if self._passes_filter(level, line):
                    self._write(level, line)
            self._update_count()
        except Exception:  # noqa: BLE001
            pass

    def _on_pause(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("Resume" if paused else "Pause")

    def _update_count(self) -> None:
        shown = sum(1 for level, line in self._lines
                    if self._passes_filter(level, line))
        total = len(self._lines)
        suffix = "" if shown == total else f" of {total}"
        cap = f"  (keeping the last {_MAX_LOG_LINES})" if total >= _MAX_LOG_LINES else ""
        self.count_label.setText(f"{shown} line(s){suffix}{cap}")

    def toPlainText(self) -> str:  # noqa: N802 - matches QTextEdit
        """The visible text, as QTextEdit returns it.

        `MainWindow.activity_log` is a documented compatibility name for
        the widget this panel replaced, so it should answer the same
        questions where that is cheap. Dropping this broke
        test_tabbed_gui_2026_08_28.py's shared-log check, which is the
        only guarantee that one tab's activity is visible without
        switching to it.
        """
        return self.view.toPlainText()

    def clear(self) -> None:
        self._lines.clear()
        self.view.clear()
        self._update_count()


class LogsWindow(QDialog):
    """The logs, in their own window, opened by a button.

    Neon 2026-10-01: *"i do not want a logs tab i want a logs button when
    i press it it opens another window that list the logs."*

    A tab was the wrong shape for this. Logs are something you consult
    WHILE watching a scan - as a tab they replaced the view you were
    watching, which is no better than the 110px strip they came from.

    NON-MODAL on purpose, so the scan window stays usable with this open
    beside it on a second monitor. One instance is reused, so pressing the
    button twice raises the existing window rather than growing a second
    copy with half the history in it.
    """

    def __init__(self, panel: "LogsPanel", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CruiseIntel — Logs")
        self.setModal(False)
        self.resize(1000, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)

    def closeEvent(self, event) -> None:
        """Hide rather than destroy.

        The panel inside is the live log sink - `MainWindow.activity_log`
        points at it and every scraper writes to it. Letting Qt delete it
        on close would silently throw away the log for the rest of the
        session.
        """
        event.ignore()
        self.hide()


class DashboardPanel(QWidget):
    """One screen answering "what is the system doing right now?".

    Driven by the window's existing 3-second resource tick - it starts no
    timer of its own, so adding this tab did not change how often anything
    is sampled.
    """

    #: Field key -> (group, label). Order is the display order.
    _FIELDS = (
        ("state", "Scanning", "State"),
        ("line", "Scanning", "Cruise line"),
        ("booking", "Scanning", "Current booking"),
        ("progress", "Scanning", "Progress"),
        ("elapsed", "Scanning", "Elapsed"),
        ("login", "Session", "Login"),
        ("browser", "Session", "Browser"),
        ("last_scan", "Scans", "Last completed"),
        ("next_scan", "Scans", "Next scan allowed"),
        ("reused", "Scans", "Reused / cached"),
        ("results", "Results", "Found"),
        ("realised", "Results", "Realised savings"),
        ("errors", "Health", "Errors / warnings"),
        ("cpu", "Health", "CPU / RAM"),
        ("processes", "Health", "Chromium processes"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: dict[str, QLabel] = {}
        self._last: dict[str, str] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        """A compact two-column board, not a stack of stretched boxes.

        THE FIRST VERSION LOOKED BROKEN and Neon said so. Two real faults,
        both mine:

          * `setWordWrap(True)` on single-line values. In a QGridLayout a
            wrapping label reports a height-for-width the grid does not
            give it, so "CPU 42.0%  RAM 57.6%" and "0 Chromium proc / 0 MB"
            were CLIPPED mid-character. These values are short and fixed
            shape - wrapping bought nothing and cost legibility.
          * Five full-width group boxes stacked vertically, each holding
            two or three rows, leaving the bottom half of the tab empty
            while the content was cramped.

        Now: cards in a 2-column grid, values on one line, elided if a
        window gets narrow enough to need it.
        """
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        board = QGridLayout()
        board.setHorizontalSpacing(12)
        board.setVerticalSpacing(12)
        outer.addLayout(board)

        groups: dict[str, QGridLayout] = {}
        order: list[str] = []
        for key, group, label in self._FIELDS:
            if group not in groups:
                box = QGroupBox(group)
                box.setStyleSheet(
                    "QGroupBox { font-size: 11px; font-weight: 600; "
                    "color: #34495E; border: 1px solid #D5DBDB; "
                    "border-radius: 6px; margin-top: 8px; padding-top: 10px; } "
                    "QGroupBox::title { subcontrol-origin: margin; "
                    "left: 10px; padding: 0 4px; }")
                grid = QGridLayout(box)
                grid.setContentsMargins(12, 6, 12, 10)
                grid.setHorizontalSpacing(12)
                grid.setVerticalSpacing(6)
                groups[group] = grid
                order.append(group)
                index = len(order) - 1
                board.addWidget(box, index // 2, index % 2)

            grid = groups[group]
            row = grid.rowCount()
            name = QLabel(label)
            name.setStyleSheet("color: #7F8C8D; font-size: 11px;")
            name.setMinimumWidth(120)
            grid.addWidget(name, row, 0, Qt.AlignLeft | Qt.AlignVCenter)

            value = QLabel("—")
            value.setStyleSheet(
                "font-family: Consolas, 'Cascadia Mono', monospace; "
                "font-size: 12px; font-weight: 600; color: #1C2833;")
            # NO WORD WRAP - see the docstring. A grid row sized for one
            # line clips a wrapping label instead of growing for it.
            value.setWordWrap(False)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(value, row, 1, Qt.AlignLeft | Qt.AlignVCenter)
            grid.setColumnStretch(1, 1)
            self._values[key] = value

        board.setColumnStretch(0, 1)
        board.setColumnStretch(1, 1)
        outer.addStretch(1)

        self.hint = QLabel(
            "Refreshed every 3 seconds from the window's existing sampler — "
            "this view starts no timer of its own.")
        self.hint.setStyleSheet("color: #95A5A6; font-size: 10px;")
        outer.addWidget(self.hint)

    def update_fields(self, values: dict) -> None:
        """Write only what CHANGED.

        `QLabel.setText` with an identical string still schedules a
        repaint, and this runs every 3 seconds for the life of the window.
        Never raises - a monitoring view must not break the thing it
        monitors.
        """
        try:
            for key, text in values.items():
                label = self._values.get(key)
                if label is None:
                    continue
                rendered = "—" if text in (None, "") else str(text)
                if self._last.get(key) == rendered:
                    continue
                self._last[key] = rendered
                label.setText(rendered)
        except Exception:  # noqa: BLE001
            pass
