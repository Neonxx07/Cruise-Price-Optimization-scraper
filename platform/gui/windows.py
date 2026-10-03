"""Main desktop window for CruiseIntel GUI."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import traceback
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from core.calculator import total_optimization_savings, unconfirmed_candidate_total
from core.models import BookingResult, BookingStatus, CruiseLine
from gui.queue_manager import BookingQueueManager, QueueStatus
from gui.scan_adapter import GuiScanAdapter
from services.msc_live_service import MscCheckOutcome, MscLiveService
from services.outcome_service import APPLIED, OutcomeService
from gui.monitor_tabs import DashboardPanel, LogsPanel, LogsWindow
from utils.logging import get_logger

logger = get_logger(__name__)


# How a results row should RANK, as an operator reads it — not
# alphabetically. Added 2026-09-03: sorting the Status column put
# NO_SAVING and TRAP above OPTIMIZATION purely because "N" and "T" sort
# either side of "O", so clicking Status buried the findings worth acting
# on. Lower number = more worth your attention.
_STATUS_RANK = {
    "OPPORTUNITY": 0,        # MSC rows
    "OPTIMIZATION": 0,
    "UPGRADE_AVAILABLE": 1,
    "TRAP": 2,               # real money, but the wrong direction
    "NO_OPPORTUNITY": 3,     # MSC rows
    "NO_SAVING": 3,
    "WLT": 4,
    "PAID_IN_FULL": 5,
    "NOT_ON_THIS_ACCOUNT": 6,
    "SKIPPED": 7,
    "ERROR": 8,
}


def _row_rank(status_text: str, value: float | None) -> float:
    """The table's DEFAULT arrangement: most-worth-acting-on first.

    Added 2026-09-03. Qt sorts one column at a time, so ranking by status
    alone left the money unordered inside each group — an operator still
    had to scan a block of 90 OPTIMIZATION rows by eye to find the big
    ones. Folding the value in as a tiebreaker gives ONE column that orders
    the whole table the way the work is actually prioritised: every
    OPTIMIZATION by descending dollars, then upgrades, then traps, then
    the rest.

    The 1e9 multiplier keeps the status grouping strictly dominant — no
    real booking saving approaches a billion dollars, so a large value can
    never promote a row out of its status band.
    """
    return _status_rank(status_text) * 1e9 - float(value or 0.0)


def _status_rank(status_text: str) -> float:
    """Business importance of a status, for sorting. Unknown statuses sort
    between the known bad ones and ERROR rather than at either extreme —
    a status nobody has classified yet should not silently claim the top
    of the table."""
    return float(_STATUS_RANK.get((status_text or "").upper().strip(), 7))


def _relative_scan_time(when) -> str:
    """"1h 23m ago", "Never" - how long since this booking was last scanned.

    Neon 2026-09-29: "it states that this booking was scanned 1 or 2 hours
    ago". A relative time is what tells you at a glance whether a result is
    worth acting on; the exact timestamp goes on the tooltip.

    Timestamps are stored in UTC and displayed relative, so no timezone
    conversion is needed to read this correctly - the arithmetic is between
    two UTC instants either way.
    """
    if when is None:
        return "Never"
    try:
        delta = datetime.utcnow() - when
    except TypeError:
        return "—"
    seconds = delta.total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m ago" if minutes else f"{hours}h ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago" if hours else f"{days}d ago"


def _booking_id_sort_value(booking_id: str) -> float:
    """Booking ids sort NUMERICALLY when they are numbers.

    CONFIRMED BUG 2026-09-03: as plain text, four real ids ordered
    1000, 3000055, 70, 999 — because "1" < "6" < "7" < "9" character by
    character. ESPRESSO ids run from 6 to 8 digits (3000068 alongside
    3000064), so this is not a hypothetical.

    GoCCL ids are alphanumeric (DEMO02, DEMO07) and have no numeric value;
    they sort to the end, keeping their relative text order via the
    display string, rather than all collapsing to one value.
    """
    raw = (booking_id or "").strip()
    if raw.isdigit():
        try:
            return float(raw)
        except ValueError:
            pass
    return float("inf")


# Modules whose code a running scan actually executes. Only MSC's
# msc_commands is hot-reloaded (see services/msc_live_service.py); for
# every other cruise line the scraper is imported ONCE at process start,
# so editing it does nothing until the GUI is restarted.
_RELOAD_SENSITIVE = (
    "scraper/ncl.py", "scraper/espresso.py", "scraper/goccl.py",
    "scraper/base.py", "core/calculator.py", "core/calculator_msc.py",
    "core/models.py", "services/booking_service.py",
)

_PROCESS_STARTED_AT = time.time()


def stale_modules(started_at: float | None = None) -> list[str]:
    """Source files edited since this process started.

    CONFIRMED COST, 2026-09-15. `scraper/ncl.py` was fixed on disk at
    12:17. The GUI had been running since before then, so it still held
    the old module in memory. An NCL scan started at 15:06 - nearly three
    hours after the fix - and failed all 135 bookings with the very
    UnboundLocalError that had already been repaired, then ran for almost
    two hours before being stopped.

    Nothing anywhere told the operator the running code was stale. Python
    imports a module once; a file edit is invisible to a live process, and
    a long-lived GUI makes that easy to forget.
    """
    started = _PROCESS_STARTED_AT if started_at is None else started_at
    here = Path(__file__).resolve().parent.parent
    stale = []
    for rel in _RELOAD_SENSITIVE:
        path = here / rel
        try:
            if path.exists() and path.stat().st_mtime > started:
                stale.append(rel)
        except OSError:
            continue
    return stale


class NumericTableItem(QTableWidgetItem):
    """A results-table cell that DISPLAYS formatted text but SORTS by a
    number.

    CONFIRMED BUG, found 2026-08-27 by the GUI smoke test: the obvious fix
    — `setData(Qt.ItemDataRole.EditRole, 60.0)` — silently does nothing
    here. QTableWidgetItem treats EditRole and DisplayRole as *the same
    data*, so setting one overwrites the other (verified directly:
    setData(EditRole, 60.0) then setText("-$60.00 saved") leaves
    data(EditRole) == "-$60.00 saved"). Sorting therefore stayed
    lexicographic, where "$9.00" sorts above "$85.00" and a confidence of
    9 outranks 95 — exactly backwards for the two columns an operator
    sorts by most (biggest saving, highest confidence).

    Overriding __lt__ is the supported way to do this: Qt calls it for
    every comparison during a sort, so the display string is untouched.
    """

    def __init__(self, text: str, sort_value: float) -> None:
        super().__init__(text)
        self._sort_value = float(sort_value)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # noqa: D105
        other_value = getattr(other, "_sort_value", None)
        if other_value is None:
            # Mixed with a plain text cell (an MSC row's "—"): fall back to
            # Qt's own text comparison rather than raising mid-sort.
            return super().__lt__(other)
        return self._sort_value < other_value


def _result_from_record(rec) -> BookingResult:
    """Rebuild a BookingResult from a persisted row.

    Only possible at all since the 2026-08-27 migration added the 13
    dropped columns - before that `obc_change`, `price_drop`,
    `lost_pkg_value` and the promo strings were computed and discarded, so a
    reloaded row could not have shown why a verdict was reached.
    """
    import json as _json

    def _arr(value):
        if not value:
            return []
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    return BookingResult(
        booking_id=rec.booking_id,
        cruise_line=CruiseLine(rec.cruise_line),
        status=BookingStatus(rec.status),
        note=rec.note or "",
        error=rec.error,
        price_category=rec.price_category,
        new_price_category=rec.new_price_category,
        old_total=rec.old_total or 0.0,
        new_total=rec.new_total or 0.0,
        net_saving=rec.net_saving or 0.0,
        confidence=rec.confidence or 0,
        price_drop=rec.price_drop or 0.0,
        obc_change=rec.obc_change or 0.0,
        lost_pkg_value=rec.lost_pkg_value or 0.0,
        currency=rec.currency or "UNKNOWN",
        old_promos=rec.old_promos or "",
        new_promos=rec.new_promos or "",
        lost_pkg_names=_arr(rec.lost_pkg_names),
        lost_fares=_arr(rec.lost_fares),
        re_addable_fares=_arr(rec.re_addable_fares),
        gained_fares=_arr(rec.gained_fares),
        lost_travel_protection=_arr(rec.lost_travel_protection),
    )


class CruiseLinePanel(QWidget):
    """One cruise line's complete workspace: login, queue, run, results.

    RESTRUCTURED 2026-08-28 (Neon: "change the gui to tabs then so a tab is
    esspresso another tab is ncl another tab is msc"). This was a single
    window with a cruise-line DROPDOWN, which made concurrent lines
    impossible by construction: one queue, one login flag, and one
    BookingQueueManager — whose BookingService holds a single
    `_live_scraper` slot and STOPS it whenever the selected line changes.

    Each panel now owns its OWN BookingQueueManager (and therefore its own
    BookingService and its own browser), so ESPRESSO and NCL can hold live
    sessions at the same time. That deliberately does NOT use
    SharedBrowserPool: ESPRESSO breaks if a session is logged in in one
    browser and replayed into another (DOCUMENTATION.md section L), and one
    browser per line keeps each line's session continuous end to end — the
    single most important reliability rule in this project. The cost is
    RAM (~700MB per live line, measured), which is why the shared resource
    readout stays on screen and 2 concurrent lines remains the
    recommendation.
    """
    def __init__(self, cruise_line: CruiseLine, on_activity=None,
                 on_summary_changed=None) -> None:
        super().__init__()
        self.cruise_line = cruise_line
        # The tabbed shell owns the shared activity log and footer; a panel
        # reports upward instead of holding its own copies.
        self._on_activity = on_activity
        self._on_summary_changed = on_summary_changed

        self.adapter = GuiScanAdapter()
        self.queue_manager = BookingQueueManager()
        # MSC runs through its own service, not BookingQueueManager/
        # BookingService — see msc_live_service.py's module docstring for
        # why (MscBookingResult's four-independent-check shape doesn't fit
        # BookingResult's single old_total/new_total/net_saving shape).
        self.msc_service = MscLiveService()
        self.results: list[BookingResult] = []
        # Verified optimizations leave the list - see _is_verified.
        # The SET is loaded once and reused for the whole render; a
        # per-row query is what made the old cache slow enough to freeze
        # the window (723 round trips before the first page drew).
        self._outcome_service = OutcomeService()
        self._verified_pairs: set = set()
        # The most recent answer from scan_plan - whether the last Start
        # scanned or reused, and when the next scan is allowed. Read by the
        # Dashboard tab; None until Start is pressed.
        self._last_scan_plan: dict | None = None
        self.msc_results: list[MscCheckOutcome] = []
        # Re-entrancy guard for _bulk_table_update. Depth, not a flag, so a
        # per-row helper called inside a batch does not restore sorting
        # early and re-sort the table N times.
        self._bulk_depth = 0
        self._bulk_sorted = True
        # Chromium census cadence - see _refresh_resources.
        self._CHROME_CENSUS_EVERY = 5
        self._shutting_down = False
        # Which cruise line we have CONFIRMED a successful login for.
        # ADDED 2026-08-26 — see _on_login_check: `has_live_session()` and
        # `msc_service.is_alive` only prove a browser/page is OPEN, not that
        # login actually succeeded, so they cannot be used as the Start
        # guard on their own. Set only on a real success, cleared on
        # timeout/failure and whenever the cruise-line selection changes.
        self._login_ok_for: CruiseLine | None = None
        # Guards _on_start against re-entry. See the comment there: the
        # Start button is not disabled until well after two modal dialogs,
        # and a modal spins the Qt event loop inside the asyncio one.
        self._start_in_progress = False

        self._build_ui()
        self._refresh_summary()
        self._update_queue_view(self.queue_manager.get_snapshot())

    async def shutdown(self) -> None:
        """Stop this line's scan and close its browser session.

        Extracted from the old window-level closeEvent when the GUI became
        tabbed: teardown is now PER LINE, and the shell awaits every panel.
        The ordering rule is unchanged and still load-bearing — stop the
        scan FIRST, then close the browser. Closing a browser out from
        under an in-flight `check_booking` makes it fail with "Target page,
        context or browser has been closed", which booking_service's own
        dead-browser recovery then treats as a crash and answers by
        starting a SECOND browser while the app is trying to quit.
        """
        try:
            if self.queue_manager.is_running:
                self.queue_manager.stop_processing()
                # 60s here was longer than the whole app's shutdown budget,
                # and with panels closing concurrently it no longer needs to
                # absorb three other tabs' waits. A batch stops after its
                # CURRENT booking, and a measured ESPRESSO booking is ~30s,
                # so 35s covers the realistic case; the window-level ceiling
                # catches anything worse.
                for _ in range(70):           # up to ~35s
                    if not self.queue_manager.is_running:
                        break
                    await asyncio.sleep(0.5)
        except Exception:
            logger.exception("gui.panel_stop_failed", cruise_line=self.cruise_line.value)
        try:
            await self.queue_manager.close_live_session()
        except Exception:
            logger.exception("gui.panel_close_session_failed",
                             cruise_line=self.cruise_line.value)
        try:
            if self.msc_service.is_running:
                self.msc_service.stop_processing()
            await self.msc_service.close()
        except Exception:
            pass

    def is_busy(self) -> bool:
        return bool(self.queue_manager.is_running or self.msc_service.is_running)

    def _build_ui(self) -> None:
        """One cruise line's workspace.

        REDESIGNED 2026-08-28 after Neon saw the first tabbed build:
        "the design is awful and fonts are bad". Four concrete faults were
        visible in that screenshot and each is fixed here:

        1. The four action buttons lived in a 2x2 QGridLayout sharing
           columns 2-3 with the inputs, so they overlapped and clipped
           ("Add to queue" sat on top of "Stop"). They are now in their own
           right-aligned row with real minimum widths.
        2. The panel still carried its OWN "Cruise-line scanners" table,
           "Resources", "Last completed scan" and "Activity log" - all four
           now live once in the tabbed shell's footer, so every tab showed a
           duplicate of each. They are still CONSTRUCTED (other methods
           reference them) but no longer added to this layout.
        3. `queue_status_label` was added after the activity log, so
           "0 pending, 0 running" floated inside the log box. It is now the
           queue group's title row.
        4. Nothing had a stretch factor, so the results table - the actual
           output - got squeezed to a few pixels while empty boxes took the
           space. The results table now takes all remaining height.

        Widget attribute names are deliberately UNCHANGED: every handler,
        smoke test and regression test addresses these by name.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ── row 1: session state + the actions ──────────────────
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self.login_status_label = QLabel("Login status: not checked")
        self.login_status_label.setStyleSheet("font-weight: 600;")
        header_row.addWidget(self.login_status_label)
        header_row.addStretch(1)

        self.login_button = QPushButton("Check login")
        self.login_button.setMinimumWidth(110)
        self.login_button.clicked.connect(self._on_login_check)
        header_row.addWidget(self.login_button)

        self.start_button = QPushButton("Start")
        self.start_button.setMinimumWidth(90)
        self.start_button.setStyleSheet("font-weight: 600;")
        # TWO JOBS, ONE BUTTON. Neon 2026-10-01: *"start starts loggin in
        # and starts scanning a 2 in 1 button."* "Check login" stays, at his
        # explicit request, for logging in WITHOUT starting a scan.
        self.start_button.setToolTip(
            "Logs in if needed, then scans. The session is verified for real "
            "on every press, not assumed from an earlier login — ESPRESSO "
            "drops one roughly hourly.")
        self.start_button.clicked.connect(self._on_start)
        header_row.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setMinimumWidth(90)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._on_stop)
        header_row.addWidget(self.stop_button)
        layout.addLayout(header_row)

        # No "Cruise Line:" row any more - the tab label already names it.
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 12px; font-weight: 600; color: #222;")
        layout.addWidget(self.summary_label)

        # ── bookings to check ───────────────────────────────────
        add_box = QGroupBox("Bookings to check")
        add_layout = QGridLayout(add_box)
        add_layout.setContentsMargins(10, 8, 10, 10)
        add_layout.setHorizontalSpacing(8)
        add_layout.setVerticalSpacing(6)

        add_layout.addWidget(QLabel("Booking ID:"), 0, 0)
        self.booking_input = QLineEdit()
        self.booking_input.setPlaceholderText("a single booking ID, then Enter")
        self.booking_input.returnPressed.connect(self._add_booking)
        add_layout.addWidget(self.booking_input, 0, 1)
        self.add_booking_button = QPushButton("Add to queue")
        self.add_booking_button.setMinimumWidth(130)
        self.add_booking_button.clicked.connect(self._add_booking)
        add_layout.addWidget(self.add_booking_button, 0, 2)

        add_layout.addWidget(QLabel("Or paste a list:"), 1, 0, Qt.AlignTop)
        self.bulk_input = QTextEdit()
        self.bulk_input.setFixedHeight(64)
        self.bulk_input.setPlaceholderText("comma or newline separated")
        add_layout.addWidget(self.bulk_input, 1, 1, 2, 1)

        self.add_bulk_button = QPushButton("Add list")
        self.add_bulk_button.setMinimumWidth(130)
        self.add_bulk_button.clicked.connect(self._add_bulk)
        add_layout.addWidget(self.add_bulk_button, 1, 2)

        self.load_file_button = QPushButton("Load from file...")
        self.load_file_button.setMinimumWidth(130)
        self.load_file_button.setToolTip(
            "Load booking IDs from a text file (one per line, or comma separated) "
            "\u2014 e.g. watchlist.txt or Watchlistncl.txt"
        )
        self.load_file_button.clicked.connect(self._add_from_file)
        add_layout.addWidget(self.load_file_button, 2, 2)
        add_layout.setColumnStretch(1, 1)
        layout.addWidget(add_box)

        # ── options ─────────────────────────────────────────────
        opt_box = QGroupBox("Options")
        opt_layout = QHBoxLayout(opt_box)
        opt_layout.setContentsMargins(10, 8, 10, 8)
        opt_layout.setSpacing(18)

        self.force_recheck_checkbox = QCheckBox("Force live recheck")
        self.force_recheck_checkbox.setToolTip(
            "Ignore the NO_SAVING cache and check every booking live. Without this, a "
            "booking already checked today comes back SKIPPED_TODAY."
        )
        opt_layout.addWidget(self.force_recheck_checkbox)

        self.capture_market_data_checkbox = QCheckBox("Collect market data")
        self.capture_market_data_checkbox.setChecked(True)
        self.capture_market_data_checkbox.setToolTip(
            "Store a snapshot in the database for later analysis: the category table for "
            "ESPRESSO/NCL, or the offer-code comparison for GoCCL."
        )
        opt_layout.addWidget(self.capture_market_data_checkbox)

        self.capture_everything_checkbox = QCheckBox("Capture everything")
        # ON BY DEFAULT, Neon 2026-09-16. Consistent with his standing
        # rule that captured data is the corpus this project mines to
        # improve results, not clutter. Leaving it off meant the richest
        # diagnostic source was missing exactly when something went
        # wrong: the NCL run that day wrote 135 empty market_data rows
        # and there was no page capture to explain why.
        #
        # Still a checkbox, not a constant - the cost is real and
        # measured (see the tooltip), so a fast run can turn it off.
        self.capture_everything_checkbox.setChecked(True)
        self.capture_everything_checkbox.setToolTip(
            "For every page visited: save the full HTML, a best-effort structured extraction "
            "(tables + label/value pairs), and every network request/response \u2014 all read-only, "
            "written under data/pages/ and data/network_traffic.jsonl, plus a Playwright "
            "trace (DOM snapshot per action, screenshots, console).\n\n"
            "ON by default. Measured cost on real runs: ~0.25 MB of HTML per page "
            "and 10-50 MB per trace - one 576-booking ESPRESSO run produced 452 MB, "
            "and it slows the scan. Uncheck for a fast run when you do not need the evidence."
        )
        opt_layout.addWidget(self.capture_everything_checkbox)

        # HEADLESS TOGGLE — NCL ONLY, Neon 2026-09-16. NCL earned it: a
        # headless run was measured driving the ENTIRE flow, not just opening
        # the booking. Same three bookings headless and headed returned
        # identical totals AND identical category counts (30/23/31 from
        # _form_12), including a real +$1,620 increase - so Switch to Edit
        # Mode, the SlickGrid read, the comparison and the cancel-and-release
        # all work without a display.
        #
        # Not offered for the others. ESPRESSO can NEVER be headless (Akamai
        # bot detection; scraper/base.py enforces it regardless of any
        # argument), and MSC/GoCCL have not been tested this way.
        #
        # Unchecked by default, so behaviour is unchanged until asked for.
        # This governs the SCAN as well as the login: the GUI keeps one
        # browser open across both, so the window opened at login is the one
        # every booking is checked in.
        self.headless_checkbox = QCheckBox("Run hidden (headless)")
        self.headless_checkbox.setToolTip(
            "NCL only. Runs the browser with no visible window - faster and "
            "out of the way, and proven to drive the full flow (edit mode, "
            "category grid, price comparison, release).\n\n"
            "Applies from the next login: the scan reuses that browser, so "
            "check this BEFORE clicking Check Login."
        )
        self.headless_checkbox.setVisible(self.cruise_line == CruiseLine.NCL)
        opt_layout.addWidget(self.headless_checkbox)
        opt_layout.addStretch(1)
        layout.addWidget(opt_box)

        # ── queue ───────────────────────────────────────────────
        queue_box = QGroupBox("Queue")
        queue_v = QVBoxLayout(queue_box)
        queue_v.setContentsMargins(10, 8, 10, 10)
        queue_v.setSpacing(6)

        queue_head = QHBoxLayout()
        self.queue_status_label = QLabel("0 pending, 0 running")
        self.queue_status_label.setStyleSheet("font-weight: 600;")
        queue_head.addWidget(self.queue_status_label)
        queue_head.addStretch(1)
        self.remove_selected_button = QPushButton("Remove selected")
        self.remove_selected_button.clicked.connect(self._on_remove_selected)
        queue_head.addWidget(self.remove_selected_button)
        self.clear_queue_button = QPushButton("Clear queue")
        self.clear_queue_button.clicked.connect(self._on_clear_queue)
        queue_head.addWidget(self.clear_queue_button)
        queue_v.addLayout(queue_head)

        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Kept deliberately short: the RESULTS table is the output that
        # matters and must get the remaining height. 110px of queue list
        # was squeezing results down to ~126px.
        self.queue_list.setFixedHeight(84)
        queue_v.addWidget(self.queue_list)
        layout.addWidget(queue_box)

        # ── results: the actual output, gets the space ───────────
        results_box = QGroupBox("Results")
        results_v = QVBoxLayout(results_box)
        results_v.setContentsMargins(10, 8, 10, 10)
        # "Last scanned" added 2026-09-29. Neon: "it states that this
        # booking was scanned 1 or 2 hours ago". Relative time reads at a
        # glance; the exact timestamp is on the tooltip.
        self.results_table = QTableWidget(0, 10)
        self.results_table.setHorizontalHeaderLabels([
            "Booking ID", "Line", "Status", "Net Saving / Summary",
            "Conf / Checks", "Old Total", "New Total", "Category",
            "Last scanned", "Note / Reason",
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        for col, width in enumerate((95, 80, 120, 165, 85, 95, 95, 90, 105)):
            self.results_table.setColumnWidth(col, width)
        self.results_table.setSortingEnabled(True)
        # Arrive already arranged, and STAY arranged as rows stream in:
        # with sorting enabled Qt inserts each new row in position, so the
        # findings worth acting on stay at the top during a live scan
        # rather than appearing in whatever order bookings were queued.
        # Column 2 carries the composite rank — see _row_rank.
        self.results_table.sortByColumn(2, Qt.SortOrder.AscendingOrder)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setAlternatingRowColors(True)
        # KEEP THIS AT 240. It is itself a fix: without a floor the results
        # table - the actual output - was squeezed to ~126px while empty
        # boxes took the room (see
        # tests/test_tabbed_gui_2026_08_28.py::test_results_table_gets_the_space).
        #
        # Lowering it to 120 on 2026-10-01 looked like the cure for the
        # Verify button being painted across the rows, and it was the wrong
        # one: it traded the new bug for the old one. The real cause was
        # that the button sat INSIDE this box, competing with this minimum
        # for height Qt could not take from the table. Moving Verify to the
        # footer removed the competition, so the floor can stay.
        self.results_table.setMinimumHeight(240)
        self.results_table.verticalHeader().setDefaultSectionSize(24)
        results_v.addWidget(self.results_table)

        layout.addWidget(results_box, 1)          # <- the stretch

        # ── footer ──────────────────────────────────────────────
        bottom = QHBoxLayout()
        self.export_button = QPushButton("Export report")
        self.export_button.setMinimumWidth(130)
        self.export_button.clicked.connect(self._on_export)
        bottom.addWidget(self.export_button)

        # VERIFY LIVES HERE, not inside the Results box. Neon 2026-10-01:
        # *"just add a verify button in the gui"* - and then, when it was
        # inside the box: *"verified selected is in a wrong place"*. It was
        # being painted across the table rows (see the table's minimum
        # height above). The footer is directly beneath the table it acts
        # on, is always visible whatever the window height, and has nothing
        # to collide with.
        self.verify_button = QPushButton("Verify selected")
        self.verify_button.setMinimumWidth(130)
        self.verify_button.setToolTip(
            "Mark the selected result rows as done. A verified optimization "
            "leaves this list; the booking is still scanned, because its "
            "price can drop again.")
        self.verify_button.clicked.connect(self._on_verify_selected)
        bottom.addWidget(self.verify_button)

        self.realised_label = QLabel("")
        self.realised_label.setToolTip(
            "Money actually captured - verified optimizations only, not "
            "everything found.")
        bottom.addWidget(self.realised_label)

        self.status_label = QLabel("Ready")
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)
        layout.addLayout(bottom)

        # CONSTRUCTED BUT NOT SHOWN. These four moved to the shell's footer
        # (one copy for all tabs). They are still created because
        # update_multi_line_status(), _on_action() and
        # refresh_last_scan_label() write to them by name, and giving them
        # real widgets is far less fragile than sprinkling None-checks
        # through methods that already work. The shell reassigns
        # `last_scan_label` to its own before calling the refresh.
        self.line_status_table = QTableWidget(0, 7)
        self.line_status_table.setHorizontalHeaderLabels([
            "Cruise Line", "State", "Progress", "Current booking",
            "Done", "Errors", "Last error",
        ])
        self.resource_label = QLabel()
        self.last_scan_label = QLabel()
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)
    def _latest_per_booking(self) -> list:
        """One result per booking - the most recent scan of each.

        THE BUG THIS FIXES, 2026-09-30. `self.results` is a flat list of
        every row, and the GUI loads TODAY'S earlier results on startup and
        then appends the new run's. A booking scanned twice in a day
        therefore appeared twice and was COUNTED twice:

            3001011   17:11 OPTIMIZATION $79   <- earlier run
                     18:37 OPTIMIZATION $79   <- this run
            3001016  17:38 OPTIMIZATION $199
                     18:39 OPTIMIZATION $199

        The header read "Optimizations: 8   Total savings: $725.00" when the
        truth was 6 optimizations and $526 - it had counted two bookings
        twice. An inflated savings figure is the worst kind of wrong here,
        because it is the number the whole product is judged on.

        These two were re-scanned correctly, not by mistake: OPTIMIZATION is
        in `never_cache_statuses`, because a live saving must always be
        re-confirmed rather than served from a cache. So duplicates in a
        day are EXPECTED, and the display has to cope with them.

        Deduplication is by (cruise line, booking id) - booking numbers are
        only unique within a portal. The underlying history is untouched;
        this is a display and counting concern only.
        """
        latest: dict[tuple, object] = {}
        for result in self.results:
            key = (getattr(result.cruise_line, "value", result.cruise_line),
                   result.booking_id)
            previous = latest.get(key)
            if previous is None:
                latest[key] = result
                continue
            # Keep the NEWEST. checked_at is set per scan; a skipped row
            # carries the ORIGINAL scan time, which is what we want.
            if getattr(result, "checked_at", None) and getattr(
                    previous, "checked_at", None):
                if result.checked_at >= previous.checked_at:
                    latest[key] = result
            else:
                latest[key] = result          # no timestamps: last wins
        return list(latest.values())

    def _refresh_summary(self) -> None:
        # COUNT BOOKINGS, NOT ROWS. See _latest_per_booking.
        unique = [r for r in self._latest_per_booking()
                  if not self._is_verified(r)]
        total = len(unique)
        optimizations = sum(1 for r in unique if r.status.value == "OPTIMIZATION")
        upgrades = sum(1 for r in unique if r.status.value == "UPGRADE_AVAILABLE")
        traps = sum(1 for r in unique if r.status.value == "TRAP")
        # Only OPTIMIZATION rows represent savings actually recommended.
        # NO_SAVING rows carry a negative net_saving to mean "repricing
        # would cost more, so we didn't recommend it" — summing those in
        # would make "Total savings" go deeply negative even when real
        # optimizations were found. Matches the CLI's own summary (main.py).
        savings = total_optimization_savings(unique)
        # Unconfirmed candidates are excluded from `savings` (they said so
        # themselves) but must stay VISIBLE as an action — $4,100 of the
        # all-time total was previously unverified GoCCL candidates.
        unconf_n, unconf_total = unconfirmed_candidate_total(unique)
        summary_text = (
            f"Bookings watched: {total}   "
            f"Optimizations: {optimizations}   "
            f"Upgrades available: {upgrades}   "
            f"Traps: {traps}   "
            f"Total savings: ${savings:.2f}"
        )
        if self.msc_results:
            # No dollar total here on purpose — MSC opportunities are a
            # checklist of independent, non-exclusive findings (see
            # core/models.py's MscBookingResult), not one net_saving figure
            # like the other cruise lines, and the agent can't reprice MSC
            # directly anyway (Neon has to call MSC to act on any of these).
            msc_opportunities = sum(1 for o in self.msc_results if o.result and o.result.has_any_opportunity)
            summary_text += f"   |   MSC bookings watched: {len(self.msc_results)}   MSC opportunities found: {msc_opportunities}"
        self.summary_label.setText(summary_text)

        # Roll this line's totals up into the all-lines footer. Without
        # this, the visible tab's numbers are the only ones on screen and a
        # concurrent line's findings stay hidden until you click its tab.
        if self._on_summary_changed is not None:
            try:
                self._on_summary_changed()
            except Exception:
                logger.exception("gui.summary_rollup_failed")

    def _add_booking(self) -> None:
        booking_id = self.booking_input.text().strip()
        if not booking_id:
            QMessageBox.warning(self, "Invalid booking", "Please enter a booking ID.")
            return
        if not self.queue_manager.add_booking(booking_id):
            QMessageBox.information(self, "Duplicate booking", "That booking ID is already added to the queue.")
            return
        self.booking_input.clear()
        self._update_queue_view(self.queue_manager.get_snapshot())

    @Slot()
    @asyncSlot()
    async def _on_login_check(self) -> None:
        # NO print() ON THE UI THREAD. EVER.
        #
        # THE HANG, 2026-09-28. Neon: "the gui is not responding". py-spy
        # dumped the frozen process and the main thread was sitting inside
        #
        #     print("GUI: _on_login_check entered")   windows.py:657
        #
        # at 0% CPU, stack identical across dumps, no child processes - the
        # browser had never even launched. The GUI is started from a .bat
        # via cmd.exe, so it owns a console window, and Windows QuickEdit
        # PAUSES console output the moment anyone clicks or selects in it.
        # A paused console blocks the writer. One stray click froze the
        # entire application, and Windows reported the window as hung.
        #
        # There were EIGHT of these debug prints on UI-thread paths - eight
        # places a click in a black window could freeze the app. They are
        # all logger calls now: the log goes to a rotating FILE, which
        # cannot be paused by a mouse.
        logger.info("gui.login_check_entered")
        # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness audit):
        # QApplication.processEvents() synchronously dispatches any already-
        # queued Qt events — including a queued second click on this same
        # button, or on Start — before the buttons below were disabled.
        # Disabling FIRST closes that window: a re-entrant click processed
        # during processEvents() now sees both buttons already disabled.
        self.login_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.status_label.setText("Opening browser for login check...")
        self.login_status_label.setText("Login status: checking...")
        QApplication.processEvents()

        cruise_line = self.cruise_line
        try:
            logger.info("gui.calling_check_login", cruise_line=cruise_line.value)
            if cruise_line == CruiseLine.MSC:
                # MscLiveService tries Windows-Credential-Manager auto-login
                # first (same as msc_session_controller.py's phase 1) and
                # only falls back to waiting for a manual login in the
                # visible window it opens if that doesn't complete cleanly.
                logged_in = await self.msc_service.check_login(timeout_minutes=15.0)
            else:
                # Uses the same shared browser session that Start will reuse —
                # this window never closes and reopens the browser between
                # login and scanning (see get_or_create_scraper), which is
                # what avoids ESPRESSO's bot-detection flagging replayed
                # session cookies in a fresh browser instance.
                logged_in = await self.queue_manager.check_login(
                    cruise_line, timeout_minutes=15.0,
                    headless=(self.cruise_line == CruiseLine.NCL
                              and self.headless_checkbox.isChecked()),
                )
            logger.info("gui.check_login_returned", cruise_line=cruise_line.value, logged_in=logged_in)
            if logged_in:
                # CONFIRMED CRITICAL BUG, fixed 2026-08-26: this is now the
                # ONLY place _login_ok_for is set. The Start guard used to
                # ask has_live_session()/is_alive, which are LIVENESS checks
                # (is a browser/page open) — not login truth. check_login()
                # starts the browser BEFORE polling for login, so after a
                # 15-minute TIMEOUT the scraper is still set and alive and
                # has_live_session() returned True. The label said "timed
                # out" while Start ran the whole batch against a logged-OUT
                # portal — every booking erroring, and on ESPRESSO a burst
                # of failures against a bot-detection-sensitive account.
                self._login_ok_for = cruise_line
                self.login_status_label.setText(f"Login status: OK ({cruise_line.value})")
                self.status_label.setText("Login check complete — browser stays open for scanning.")
            else:
                self._login_ok_for = None
                self.login_status_label.setText(f"Login status: NOT logged in ({cruise_line.value}) — timed out")
                self.status_label.setText("Login check timed out — please try again.")
        except Exception as exc:
            self._login_ok_for = None
            logger.exception("gui.check_login_failed", cruise_line=cruise_line.value)
            self.login_status_label.setText("Login status: failed")
            self.status_label.setText(f"Login check failed: {exc}")
            QMessageBox.warning(self, "Login failed", str(exc))
        finally:
            self.login_button.setEnabled(True)
            # GATED 2026-08-26 to match _on_start's finally, whose own
            # comment explains why an unconditional re-enable is dangerous:
            # re-enabling Start while a batch is actually running allows a
            # second concurrent scan on the same browser page.
            if not self.queue_manager.is_running and not self.msc_service.is_running:
                self.start_button.setEnabled(True)

    @Slot()
    def _add_bulk(self) -> None:
        text = self.bulk_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No input", "Paste booking IDs into the bulk input first.")
            return
        added = self.queue_manager.add_bookings_bulk(text)
        if not added:
            QMessageBox.information(self, "No new bookings", "No new booking IDs were added to the queue.")
            return
        self.bulk_input.clear()
        self._update_queue_view(self.queue_manager.get_snapshot())
        self.status_label.setText(f"Added {len(added)} booking(s) to queue.")

    @Slot()
    def _add_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load booking IDs", str(Path.cwd()),
            "Text files (*.txt *.csv);;All files (*)",
        )
        if not path:
            return
        added, error = self.queue_manager.add_bookings_from_file(path)
        if error:
            # Shown as a real message, not a silent no-op: an empty or
            # already-queued file is exactly the case that made Start look
            # broken.
            QMessageBox.warning(self, "Nothing loaded", error)
            self.status_label.setText(error)
            return
        self._update_queue_view(self.queue_manager.get_snapshot())
        self.status_label.setText(f"Loaded {len(added)} booking(s) from {Path(path).name}.")

    @Slot()
    def _remove_queue_item(self, booking_id: str) -> None:
        if self.queue_manager.remove_booking(booking_id):
            self._update_queue_view(self.queue_manager.get_snapshot())

    @Slot()
    def _on_remove_selected(self) -> None:
        booking_ids = [
            item.data(Qt.UserRole)
            for item in self.queue_list.selectedItems()
            if item.data(Qt.UserRole)
        ]
        if not booking_ids:
            QMessageBox.information(self, "Nothing selected", "Select one or more queued bookings to remove first.")
            return
        removed = sum(1 for bid in booking_ids if self.queue_manager.remove_booking(bid))
        self._update_queue_view(self.queue_manager.get_snapshot())
        self.status_label.setText(f"Removed {removed} booking(s) from queue.")

    @Slot()
    def _on_clear_queue(self) -> None:
        # ALSO blocked during an MSC batch, 2026-08-26: queue_manager's own
        # `if self._running` guard is blind to MSC (MSC runs through
        # MscLiveService, so queue_manager._running is False for the whole
        # MSC batch). Only the disabled button was stopping this.
        if self.msc_service.is_running:
            QMessageBox.warning(self, "Cannot clear", "Cannot clear the queue while an MSC scan is running.")
            return
        if not self.queue_manager.clear_queue():
            QMessageBox.warning(self, "Cannot clear", "Cannot clear the queue while a scan is running.")
            return
        # FIXED 2026-08-26: this used to clear ONLY the queue, leaving
        # `self.results`/`self.msc_results` and the whole results table
        # populated — so the summary kept reporting savings from a run the
        # operator had just discarded, and "Bookings watched" kept counting
        # them. Nothing anywhere ever cleared `self.results`, so those
        # totals only ever grew (and double-counted a booking on every
        # "Force live recheck" re-run of the same ID).
        self.results.clear()
        self.msc_results.clear()
        self.results_table.setRowCount(0)
        self._update_queue_view(self.queue_manager.get_snapshot())
        self._refresh_summary()
        self.status_label.setText("Queue and results cleared.")

    def update_multi_line_status(self, snapshot: dict) -> None:
        """Render a MultiLineCoordinator.status_snapshot() into the panel.

        Pass this straight to `MultiLineCoordinator.run(on_status=...)`.
        Everything shown here comes from the coordinator's own snapshot —
        no locally-inferred state — so the display cannot claim something
        the backend isn't actually doing.

        Defensive on every field: this renders live backend data while a
        real scan is driving real bookings, and a KeyError here must never
        be what interrupts it (the coordinator guards callbacks, but not
        crashing in the first place is better).
        """
        lines = snapshot.get("lines", []) or []
        self.line_status_table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            state = str(line.get("state", "?"))
            values = [
                str(line.get("cruise_line", "?")),
                state,
                f"{line.get('progress_pct', 0)}%  ({line.get('done', 0) + line.get('errors', 0)}/{line.get('total', 0)})",
                str(line.get("current_booking_id") or "—"),
                str(line.get("done", 0)),
                str(line.get("errors", 0)),
                str(line.get("last_error") or ""),
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                # Colour the row by state so a FAILED line is obvious at a
                # glance rather than needing to be read.
                if state == "FAILED":
                    item.setBackground(Qt.GlobalColor.red)
                elif state == "RUNNING":
                    item.setBackground(Qt.GlobalColor.green)
                elif state == "PAUSED":
                    item.setBackground(Qt.GlobalColor.yellow)
                elif state == "STOPPED":
                    item.setBackground(Qt.GlobalColor.gray)
                self.line_status_table.setItem(row, col, item)

        res = snapshot.get("resources", {}) or {}
        throttled = res.get("throttled")
        parts = [
            f"CPU {res.get('cpu_percent', '?')}%",
            f"RAM {res.get('ram_percent', '?')}%",
            f"browser {res.get('browser_rss_mb', '?')} MB",
            f"limit {snapshot.get('max_concurrent', '?')} concurrent",
            f"contexts {snapshot.get('live_contexts', 0)}",
        ]
        if not snapshot.get("browser_alive", True):
            parts.append("BROWSER DEAD")
        if snapshot.get("paused"):
            parts.append("PAUSED")
        if snapshot.get("stopping"):
            parts.append("STOPPING")
        if throttled:
            parts.append(f"THROTTLED: {res.get('reason', '')}")
        self.resource_label.setText("Resources: " + "  |  ".join(parts))
        self.resource_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;"
            + ("color: #b00; font-weight: bold;" if throttled or not snapshot.get("browser_alive", True) else "")
        )

    @Slot(str)
    def _on_cruise_line_changed(self, new_value: str) -> None:
        # RETAINED but now unreachable from the UI: there is no dropdown in
        # a per-line panel, so the selection can never change underneath a
        # confirmed login. Kept because `_login_ok_for` invalidation is the
        # safety property it encoded, and a future caller might reintroduce
        # a way to change a panel's line.
        """Invalidate the confirmed-login flag when the selection changes.

        A login is per-cruise-line; switching lines means we no longer have
        a confirmed login for what's now selected. Also keeps the visible
        status label honest instead of showing a stale "OK" for a line that
        was never authenticated (see _login_ok_for)."""
        if self._login_ok_for is not None and self._login_ok_for.value != new_value:
            self._login_ok_for = None
            self.login_status_label.setText(f"Login status: not checked for {new_value}")

    @Slot()
    @asyncSlot()
    async def _on_start(self) -> None:
        logger.info("gui.start_entered")
        # RE-ENTRANCY GUARD, added 2026-09-23 after a real scan was killed
        # by one. From the overnight run's log:
        #
        #   RuntimeError: Cannot enter into task Task-17 <_on_start at :961>
        #   while another task Task-546 <_on_start at :898> is being executed
        #   Task was destroyed but it is pending!  <Task-17 ... :961>
        #
        # Task-17 was the RUNNING batch; Task-546 was a second _on_start
        # sitting on the stale-modules QMessageBox at line 898. A modal
        # spins the Qt event loop nested inside the asyncio loop, so while
        # it is up qasync cannot wake the batch task - and the batch task
        # was then destroyed outright, mid-scan.
        #
        # The existing protection is start_button.setEnabled(False), but
        # that does not happen until ~60 lines below, AFTER two modals that
        # each spin the loop. Every one of those is a window where a second
        # click lands. A plain flag set before any dialog closes it, and it
        # cannot be defeated by a dialog the way the button state can.
        if self._start_in_progress:
            logger.warning("gui.start_reentered_ignored",
                           cruise_line=self.cruise_line.value)
            return
        self._start_in_progress = True
        try:
            await self._on_start_guarded()
        finally:
            self._start_in_progress = False

    async def _on_start_guarded(self) -> None:
        snapshot = self.queue_manager.get_snapshot()
        logger.info("gui.start_snapshot", queued=snapshot.queued,
                    running=snapshot.running, done=snapshot.done,
                    error=snapshot.error)
        if snapshot.queued == 0:
            QMessageBox.warning(self, "No bookings", "Add at least one booking ID before starting the queue.")
            return

        # STALE CODE CHECK. See stale_modules() - on 2026-09-15 a fixed
        # scraper sat on disk for three hours while this process kept
        # running the old one, and an NCL scan failed all 135 bookings with
        # an already-repaired error. Asked, not blocked: the operator may
        # have edited something irrelevant to this line and should stay in
        # control of their own run.
        stale = stale_modules()
        if stale:
            logger.warning("gui.stale_modules", cruise_line=self.cruise_line.value,
                           modules=stale)
            answer = QMessageBox.question(
                self,
                "Code changed since this window opened",
                "These files were edited after this window started, so the "
                "running scan would still use the OLD code:\n\n  "
                + "\n  ".join(stale)
                + "\n\nRestart the app to pick the changes up.\n\n"
                  "Start the scan anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.status_label.setText(
                    "Scan cancelled — restart the app to load the updated code.")
                return

        cruise_line_check = self.cruise_line
        has_session = (
            self.msc_service.is_alive if cruise_line_check == CruiseLine.MSC
            else self.queue_manager.has_live_session(cruise_line_check)
        )
        # TWO conditions now, 2026-08-26: a live browser AND a confirmed
        # successful login for THIS cruise line. `has_session` alone is
        # liveness only — check_login() opens the browser before polling, so
        # a 15-minute login TIMEOUT still leaves a live, alive scraper and
        # this guard used to pass, running the whole batch against a
        # logged-out portal while the status label read "timed out".
        needs_login = not has_session or self._login_ok_for != cruise_line_check

        # TRUST, THEN VERIFY. Added 2026-10-01, Neon: *"make it loggs in
        # and starts by this button as well meaning start starts loggin in
        # and starts scanning a 2 in 1 button."*
        #
        # Start already attempted a login when it had never had one. The
        # hole was the other branch: `_login_ok_for` is STICKY - set once
        # on a successful login and never re-checked - so after any earlier
        # login, Start skipped the login step and went straight to
        # scanning. ESPRESSO drops a session roughly hourly, so a Start
        # pressed an hour later ran the whole batch against a logged-out
        # portal. That is where the log's **339 `login.required`** events
        # come from.
        #
        # One cheap page check against the browser that is already open -
        # no navigation, no new window - turns the sticky flag into a fact.
        if not needs_login:
            try:
                live = await self.queue_manager.session_is_logged_in(
                    cruise_line_check)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gui.start_session_check_failed",
                               error=str(exc)[:200])
                live = False
            if not live:
                logger.info("gui.start_session_expired_relogging_in",
                            cruise_line=cruise_line_check.value)
                self._login_ok_for = None
                needs_login = True

        if needs_login:
            # TRY TO LOG IN RATHER THAN SENDING THE OPERATOR TO ANOTHER
            # BUTTON.
            #
            # Neon 2026-09-30, pressing Start on a fresh app:
            #
            #   16:52:35  gui.start_entered          <- pressed Start
            #   16:52:37  gui.login_check_entered    <- told to press this
            #   16:52:51  login_check.success  via=auto_login
            #   16:52:52  gui.start_entered          <- pressed Start AGAIN
            #
            # The credentials were in the keyring the whole time and
            # auto_login needed nine seconds. Refusing to start, to ask for
            # a click that runs the very thing we could have run ourselves,
            # is a dialog in place of a feature.
            #
            # THE GUARD ITSELF STAYS, and it is still the thing that decides
            # (see the 2026-08-26 note above): a browser being open is not a
            # login, and a scan must never run against a logged-out portal.
            # What changes is that a refusal is now the SECOND answer, given
            # only after logging in has actually been attempted.
            #
            # ESPRESSO can still demand MFA, which no automation can
            # complete - check_login opens the visible window and waits, and
            # if that times out the original refusal stands.
            self.status_label.setText(
                f"Not logged in to {cruise_line_check.value} — logging in…")
            logger.info("gui.start_auto_login_attempt",
                        cruise_line=cruise_line_check.value)
            logged_in = False
            try:
                if cruise_line_check == CruiseLine.MSC:
                    logged_in = await self.msc_service.check_login(
                        timeout_minutes=15.0)
                else:
                    logged_in = await self.queue_manager.check_login(
                        cruise_line_check, timeout_minutes=15.0,
                        headless=(cruise_line_check == CruiseLine.NCL
                                  and self.headless_checkbox.isChecked()),
                    )
            except Exception as exc:
                logger.error("gui.start_auto_login_failed",
                             cruise_line=cruise_line_check.value,
                             error=str(exc)[:300])
            if logged_in:
                self._login_ok_for = cruise_line_check
                self.login_status_label.setText(
                    f"Login status: OK for {cruise_line_check.value}")
                logger.info("gui.start_auto_login_ok",
                            cruise_line=cruise_line_check.value)
            else:
                self.status_label.setText("Not logged in — scan not started")
                QMessageBox.warning(
                    self, "Not logged in",
                    f"Could not log in to {cruise_line_check.value} "
                    f"automatically.\n\n"
                    "Complete the login in the browser window that opened "
                    "(ESPRESSO may demand MFA, which cannot be automated), "
                    "then press Start again — Start logs in by itself, so "
                    "\"Check login\" is only needed to log in WITHOUT "
                    "starting a scan.\n\n"
                    "A browser being open is not the same as being logged in — "
                    "starting anyway would run every booking against a "
                    "logged-out portal.",
                )
                return

        # ── ALREADY SCANNED? DON'T OPEN A BROWSER AT ALL ────────
        #
        # Neon 2026-10-01: *"the problem is the script is still scanning i
        # do not want it to scan because it is the same list it should not
        # scan again it should gave me the same results and that these
        # bookings were already scanned ... at least in a frame of 2
        # hours."*
        #
        # Checked HERE - after the login guard, before anything is
        # disabled or a browser is touched - so a suppressed scan costs one
        # database read and leaves the window exactly as it was.
        #
        # This is a different question from the per-booking freshness
        # cache, which runs INSIDE a scan and skips individual bookings.
        # That one cannot stop a scan starting; this one can.
        try:
            plan = await self.queue_manager.scan_plan(
                cruise_line_check,
                bypass_cache=self.force_recheck_checkbox.isChecked())
        except Exception as exc:  # noqa: BLE001 - fail OPEN and scan
            logger.warning("gui.scan_plan_failed", error=str(exc)[:200])
            plan = {"action": "scan"}

        # Remembered so the Dashboard can show it without re-querying.
        self._last_scan_plan = plan

        if plan.get("action") == "reuse":
            when = plan.get("scanned_at")
            when_text = when.strftime("%H:%M") if when else "earlier"
            next_at = plan.get("next_allowed_at")
            next_text = next_at.strftime("%H:%M") if next_at else "later"
            message = (
                f"Already scanned {plan.get('bookings', 0)} booking(s) at "
                f"{when_text}. Reusing those results — nothing rescanned. "
                f"Next scan available at {next_text} "
                f"({plan.get('minutes_remaining', 0)} min)."
            )
            logger.info("gui.scan_suppressed",
                        cruise_line=cruise_line_check.value,
                        scanned_at=str(when),
                        minutes_remaining=plan.get("minutes_remaining"))
            self.status_label.setText(message)
            QMessageBox.information(
                self, "Already scanned",
                message + "\n\nThe results below are the ones from that "
                "scan.\n\nTo scan anyway, tick \"Force live recheck\" — a "
                "forced re-check is treated as a different request and is "
                "never suppressed.")
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.clear_queue_button.setEnabled(False)
        self.add_booking_button.setEnabled(False)
        self.add_bulk_button.setEnabled(False)
        self.load_file_button.setEnabled(False)
        self.booking_input.setEnabled(False)
        self.bulk_input.setEnabled(False)
        # Running "Check login" while a scan is active opens a second,
        # separate browser session — ESPRESSO appears to only allow one
        # active session per account, so that second login can knock the
        # scan's already-running session out from under it, cascading
        # into timeouts for every booking still queued.
        self.login_button.setEnabled(False)
        self.status_label.setText("Starting queue processing...")



        cruise_line = self.cruise_line
        force_live_recheck = self.force_recheck_checkbox.isChecked()

        # WHAT THIS RUN WILL ACTUALLY DO, BEFORE IT OPENS A BROWSER.
        #
        # Neon 2026-09-29: "Before a run starts, show a summary: 42
        # bookings: 30 fresh (will skip), 12 to scan."
        #
        # Two bulk queries for the whole watchlist. Never fatal - a preview
        # that fails must not stop a scan, so a failure simply reports
        # everything as "to scan", which is the old behaviour.
        if not force_live_recheck:
            try:
                preview = await self.queue_manager.preview_freshness(cruise_line)
                if preview["total"] and (preview["fresh"] or preview["excluded"]):
                    parts = [f"{preview['total']} bookings"]
                    if preview["excluded"]:
                        parts.append(f"{preview['excluded']} paid in full "
                                     f"(never rescanned)")
                    if preview["fresh"]:
                        parts.append(f"{preview['fresh']} fresh within "
                                     f"{preview['window_hours']}h (will skip)")
                    parts.append(f"{preview['to_scan']} to scan")
                    self.status_label.setText(" · ".join(parts))
                    logger.info("gui.freshness_preview", **{
                        k: v for k, v in preview.items() if k != "detail"})
            except Exception as exc:
                logger.warning("gui.freshness_preview_failed",
                               error=str(exc)[:200])
        capture_market_data = self.capture_market_data_checkbox.isChecked()
        capture_everything = self.capture_everything_checkbox.isChecked()

        try:
            if cruise_line == CruiseLine.MSC:
                await self._run_msc_batch()
            else:
                await self._run_standard_batch(
                    cruise_line, force_live_recheck, capture_market_data, capture_everything,
                )
        except Exception as exc:
            logger.error("gui.start_exception", error=str(exc)[:300])
            traceback.print_exc()
            self.status_label.setText("Queue processing failed")
            QMessageBox.critical(self, "Processing failed", str(exc))
        finally:
            # CONFIRMED REAL BUG, fixed 2026-08-13 (Phase 0 correctness
            # audit): this used to unconditionally re-enable every control
            # here, including the case where start_processing() raised
            # simply because a real scan was ALREADY running (queue_manager
            # correctly refused to start a second one) — re-enabling Start/
            # Login/Add/Clear while that other, still-running scan owns the
            # live browser page reopened the exact re-entrancy window the
            # login-check fix above closes for that path. Only restore the
            # idle-state controls when the queue is genuinely not running
            # any more. Checks both services — only one is ever actually
            # running at a time (Start is disabled for the duration of
            # this whole coroutine), but this stays correct either way.
            if not self.queue_manager.is_running and not self.msc_service.is_running:
                self.start_button.setEnabled(True)
                self.stop_button.setEnabled(False)
                self.clear_queue_button.setEnabled(True)
                self.add_booking_button.setEnabled(True)
                self.add_bulk_button.setEnabled(True)
                self.load_file_button.setEnabled(True)
                self.booking_input.setEnabled(True)
                self.bulk_input.setEnabled(True)
                self.login_button.setEnabled(True)
            self._update_queue_view(self.queue_manager.get_snapshot())

    async def _run_standard_batch(
        self, cruise_line: CruiseLine, force_live_recheck: bool,
        capture_market_data: bool, capture_everything: bool,
    ) -> None:
        """ESPRESSO/NCL/GoCCL path — unchanged, via BookingQueueManager/
        BookingService."""
        line_name = cruise_line.value
        last_status: dict[str, str] = {}

        def on_state_change(snapshot) -> None:
            self._update_queue_view(snapshot)
            # ADDED 2026-08-27: mirror the MSC path, which already reported
            # "checking X (3/26)" here. The standard path left status_label
            # frozen on whatever it said before Start for the whole scan,
            # so a multi-minute booking looked identical to a hang.
            current = getattr(snapshot, "current_booking_id", None)
            if not current:
                return
            total = getattr(snapshot, "progress_total", 0) or 0
            done = getattr(snapshot, "progress_done", 0) or 0
            text = f"{line_name}: checking {current}"
            if total:
                text += f" ({min(done + 1, total)}/{total})"
            text += "..."
            if last_status.get("text") != text:
                last_status["text"] = text
                self.status_label.setText(text)

        def on_result(result: BookingResult) -> None:
            self.results.append(result)
            self._append_result_row(result)
            self._refresh_summary()

        logger.info("gui.invoking_start_processing")
        await self.queue_manager.start_processing(
            cruise_line=cruise_line,
            on_state_change=on_state_change,
            on_result=on_result,
            raw_dump_dir=str(Path("data")),
            force_live_recheck=force_live_recheck,
            capture_market_data=capture_market_data,
            capture_everything=capture_everything,
            on_action=self._on_action,
        )
        # Report the REAL terminal status, 2026-08-26. This used to always
        # say "Queue processing complete." regardless of whether the job
        # COMPLETED, FAILED (browser died — every remaining booking left
        # unscanned) or was STOPPED. An operator could believe a client's
        # whole watchlist had been repriced when half of it was never
        # touched. See BookingQueueManager.last_job_status.
        await self.refresh_last_scan_label()
        job_status = self.queue_manager.last_job_status
        snapshot = self.queue_manager.get_snapshot()
        remaining = snapshot.queued + snapshot.running
        logger.info("gui.batch_finished", job_status=job_status, remaining=remaining)
        if job_status == "FAILED":
            # Prefer the job's OWN recorded reason (added 2026-08-27) over
            # the old hard-coded "the browser session died" guess, which
            # was only ever one of several real causes — the pre-flight
            # session check now reports "not logged in any more" instead of
            # blaming a dead browser.
            reason = self.queue_manager.last_job_error
            message = reason or (
                f"SCAN FAILED — the browser session died and {remaining} booking(s) "
                f"were NOT checked. Re-run after checking login."
            )
            self.status_label.setText(message)
            QMessageBox.warning(self, "Scan failed", message)
        elif job_status == "STOPPED":
            self.status_label.setText(f"Scan stopped by you — {remaining} booking(s) not checked.")
        elif remaining:
            # Shouldn't happen, but never silently claim completeness.
            self.status_label.setText(
                f"Queue finished with status {job_status}, but {remaining} booking(s) "
                f"are still unprocessed — check the log."
            )
        else:
            self.status_label.setText("Queue processing complete — all bookings checked.")

    async def _run_msc_batch(self) -> None:
        """MSC path — via MscLiveService (see its module docstring), which
        runs the same fully-automated lookup -> stage -> confirm -> harvest
        -> evaluate flow the check_booking/check_booking_batch console
        commands use, instead of BookingService. Reuses the queue list
        widget for progress (via queue_manager.mark_running/mark_done) even
        though the scan itself doesn't go through queue_manager."""
        booking_ids = [
            item.booking_id for item in self.queue_manager.get_snapshot().items
            if item.status == QueueStatus.QUEUED
        ]

        def on_progress(booking_id: str, index: int, total: int) -> None:
            self.queue_manager.mark_running(booking_id)
            self._update_queue_view(self.queue_manager.get_snapshot())
            self.status_label.setText(f"MSC: checking {booking_id} ({index + 1}/{total})...")

        def on_result(outcome: MscCheckOutcome) -> None:
            self.queue_manager.mark_done(outcome.booking_id, is_error=(outcome.status == "error"))
            self.msc_results.append(outcome)
            self._append_msc_result_row(outcome)
            self._refresh_summary()
            self._update_queue_view(self.queue_manager.get_snapshot())

        logger.info("gui.invoking_msc_run_batch")
        await self.msc_service.run_batch(booking_ids, on_result=on_result, on_progress=on_progress)
        logger.info("gui.msc_run_batch_completed")
        self.status_label.setText(
            "MSC queue processing complete. Remember: this only FINDS opportunities — "
            "MSC must be called to actually apply any of them (the agent can't reprice MSC directly)."
        )

    @Slot()
    def _on_stop(self) -> None:
        stopped_standard = self.queue_manager.stop_processing()
        stopped_msc = self.msc_service.stop_processing()
        if stopped_standard or stopped_msc:
            self.status_label.setText("Stop requested. Waiting for current booking to finish...")
        else:
            self.status_label.setText("No active queue to stop.")

    def _on_action(self, entry: dict) -> None:
        """Append one action-log entry (from the scraper) to the activity log panel."""
        ts = entry.get("timestamp", "")
        action = entry.get("action", "")
        detail = {k: v for k, v in entry.items() if k not in ("timestamp", "action", "cruise_line")}
        detail_str = " ".join(f"{k}={v}" for k, v in detail.items())
        line = f"[{ts}] {action}  {detail_str}"
        self.activity_log.append(line)
        # Also surface it on the shell's combined log, so an operator
        # watching one tab still sees another line's activity — otherwise a
        # concurrent run is invisible unless you switch tabs.
        if self._on_activity is not None:
            try:
                self._on_activity(self.cruise_line, line)
            except Exception:
                logger.exception("gui.activity_forward_failed")

    @staticmethod
    def _format_net_saving(net_saving: float, status: str = "",
                           price_drop: float | None = None) -> str:
        """Spell out cost increases instead of a bare negative dollar
        figure ("$-459.00") that reads as ambiguous — a negative
        net_saving means repricing would cost more, not save less.

        Sign follows the price DELTA, not the savings polarity: a price
        going UP ("more expensive") reads "+" since the number the client
        owes got bigger, and a price going DOWN ("saved") reads "-" since
        it got smaller — matches how a plain price-change figure reads,
        rather than a positive-means-good/negative-means-bad framing.

        CONFIRMED REAL BUG 2026-08-12: calculator.py's TRAP/NO_SAVING
        branches (the package-trap and OBC-loss-ratio checks) can compute
        a positive net_saving on paper — that's the whole point of those
        checks, catching a "win" that's smaller than what's being given up
        — but this method used to say "-$50.00 saved" for those rows
        regardless of status, directly contradicting the TRAP/NO_SAVING
        label sitting right next to it in the previous column. Now never
        uses "saved" language for a row that isn't actually recommended."""
        # A HIGHER PRICE SAYS SO, FIRST. Neon 2026-10-01, on booking
        # 3001014: "I WANT TO TREAT THIS AS NO SAVING AND A HIGHER PRICE."
        #
        # That row's cash price rose $247 while a $300 OBC gain left
        # net_saving at +$53, so this column rendered
        #
        #     "$53.00 (not recommended — see status)"
        #
        # - a positive dollar figure on a booking that costs MORE. The
        # status had already been corrected to NO_SAVING (see
        # core.calculator: OBC is not cash), but the money column still
        # read like a win, which is most of what made the row alarming.
        #
        # When the cash price went UP, that is the number to show. The OBC
        # side of the trade is not lost - it is spelled out in the note.
        if price_drop is not None and price_drop < 0:
            return f"+${abs(price_drop):.2f} more expensive"
        if status in ("TRAP", "NO_SAVING") and net_saving > 0:
            return f"${net_saving:.2f} (not recommended — see status)"
        # NO MINUS SIGN ON A SAVING. Neon 2026-10-01 confirmed both of
        # the inconsistencies flagged after the 3001014 fix should go.
        #
        # This used to render "-$79.00 saved", the sign following the PRICE
        # DELTA (the bill got smaller) rather than the savings polarity. It
        # is internally consistent and it reads as negative savings - a
        # minus sign next to the word "saved" fights itself, and on the
        # 3001014 row it compounded an error that was already alarming.
        #
        # "More expensive" keeps its "+": there the sign and the words
        # agree, and the number IS going up.
        if net_saving > 0:
            return f"${net_saving:.2f} saved"
        if net_saving < 0:
            # WHEN CASH AND NET DISAGREE, SAY BOTH. A trap like 3001020
            # has the cash price FALLING $546 while a $1,260 perk is given
            # up, leaving net -714. Rendering only "+$714.00 more
            # expensive" is true about the outcome and false about the
            # cash, which is the same kind of half-truth that made 3001014
            # alarming - just pointing the other way.
            if price_drop is not None and price_drop > 0:
                return (f"${price_drop:.2f} cash — but "
                        f"${abs(net_saving):.2f} worse overall")
            return f"+${abs(net_saving):.2f} more expensive"
        return "$0.00"

    @contextmanager
    def _bulk_table_update(self):
        """Suspend sorting, repaints and signals for a batch of rows.

        WHY. Each _append_*_row turned sorting off and back ON around its
        own insert. That is correct for ONE row, but _populate_results_table
        and the DB reload call them in a loop, so a table of N results paid
        for N FULL RE-SORTS plus N repaints - the work grows with the square
        of the result count, which is why a long scan felt progressively
        heavier as it went.

        Re-entrant on purpose: the per-row helpers still suspend sorting
        when called on their own (a single live result arriving mid-scan),
        and become no-ops for that while a bulk update is already in
        progress. Sorting is restored exactly once, at the end.
        """
        table = self.results_table
        first = self._bulk_depth == 0
        self._bulk_depth += 1
        if first:
            self._bulk_sorted = table.isSortingEnabled()
            table.setSortingEnabled(False)
            table.setUpdatesEnabled(False)      # one repaint, not one per row
            table.blockSignals(True)            # no itemChanged storm
        try:
            yield
        finally:
            self._bulk_depth -= 1
            if self._bulk_depth == 0:
                table.blockSignals(False)
                table.setUpdatesEnabled(True)
                table.setSortingEnabled(self._bulk_sorted)

    @asyncSlot()
    async def _on_verify_selected(self) -> None:
        """Mark the selected result rows as verified and retire them.

        What "verified" means here is what Neon does: he has opened the
        booking, applied the reprice (or decided not to), and is done with
        it. An OPTIMIZATION is recorded as APPLIED and counts toward
        realised savings; anything else is recorded as REVIEWED and counts
        zero, because deciding not to reprice a TRAP saved nothing.

        Never raises. A bookkeeping failure must not take the window down.
        """
        rows = {i.row() for i in self.results_table.selectedIndexes()}
        if not rows:
            self.status_label.setText("Select a result row first, then Verify.")
            return

        wanted = set()
        for row in rows:
            cell = self.results_table.item(row, 0)
            line = self.results_table.item(row, 1)
            if cell and line:
                wanted.add((line.text().strip(), cell.text().strip()))

        done = 0
        for result in self._latest_per_booking():
            key = (getattr(result.cruise_line, "value", result.cruise_line),
                   str(result.booking_id))
            if key not in wanted or self._is_verified(result):
                continue
            status = getattr(result.status, "value", result.status)
            ok = await self._outcome_service.record(
                key[0], result.booking_id,
                old_total=getattr(result, "old_total", None),
                new_total=getattr(result, "new_total", None),
                net_saving=getattr(result, "net_saving", None),
                outcome=APPLIED if status == "OPTIMIZATION" else "REVIEWED",
                verified_by="human",
                evidence={"status": status, "verified_from": "gui"},
            )
            done += 1 if ok else 0

        await self._reload_verified()
        self.status_label.setText(
            f"Verified {done} booking(s) - removed from the list."
            if done else "Nothing new to verify in the selection.")

    async def _reload_verified(self) -> None:
        """Refresh the verified set, then redraw. Never raises."""
        try:
            self._verified_pairs = await self._outcome_service.verified_pairs()
            realised = await self._outcome_service.total_realised()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gui.verified_reload_failed", error=str(exc)[:200])
            return
        try:
            self.realised_label.setText(
                f"Realised: ${realised:,.2f}" if realised else "")
            self._populate_results_table()
            self._refresh_summary()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gui.verified_redraw_failed", error=str(exc)[:200])

    def _is_verified(self, result: BookingResult) -> bool:
        """Has this exact opportunity already been verified?

        Neon 2026-10-01: *"once it is verified it is removed from the
        least because it means it was optimized and it is not needed in
        the gui list anymore."*

        Matched on the FIGURES, not the booking: verifying $79 off today
        must not hide a $300 drop next month. See OutcomeService.
        """
        try:
            return self._outcome_service.is_verified(
                self._verified_pairs, result.booking_id,
                getattr(result, "old_total", None),
                getattr(result, "new_total", None))
        except Exception:  # noqa: BLE001 - never block a render
            return False

    def _append_result_row(self, result: BookingResult) -> None:
        # A verified opportunity is finished work; it leaves the list.
        if self._is_verified(result):
            return
        # Sorting must be off while inserting: with it enabled, each
        # setItem() call can trigger an immediate re-sort mid-insert and
        # scatter this row's cells across different rows.
        # Skipped while a bulk update owns the table - see _bulk_table_update.
        if self._bulk_depth:
            return self._append_result_row_unguarded(result)
        with self._bulk_table_update():
            return self._append_result_row_unguarded(result)

    def _find_result_row(self, result: BookingResult) -> int:
        """The existing row for this booking, or -1.

        Booking numbers are only unique WITHIN a portal, so the line is part
        of the identity - NCL 12345 and ESPRESSO 12345 are different
        bookings and must not overwrite each other.
        """
        line = getattr(result.cruise_line, "value", result.cruise_line)
        for row in range(self.results_table.rowCount()):
            id_cell = self.results_table.item(row, 0)
            line_cell = self.results_table.item(row, 1)
            if (id_cell is not None and line_cell is not None
                    and id_cell.text() == result.booking_id
                    and line_cell.text() == line):
                return row
        return -1

    def _append_result_row_unguarded(self, result: BookingResult) -> None:
        # A RE-SCAN REPLACES THE BOOKING'S ROW. Added 2026-09-30.
        #
        # The table loads today's earlier results on startup and then
        # appends the new run's, so a booking scanned twice in a day showed
        # twice - 3001011 and 3001016 both appeared with identical
        # OPTIMIZATION rows hours apart. Two rows for one booking is not
        # history in a live table, it is confusion: the operator cannot
        # tell which figure is current.
        #
        # The full history stays in `bookings` and `price_history`; this is
        # the CURRENT state of each booking.
        existing = self._find_result_row(result)
        if existing >= 0:
            self.results_table.removeRow(existing)
            row = existing
            self.results_table.insertRow(row)
        else:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
        # An ERROR result carries its cause in BOTH .error and .note
        # (make_error_result sets both); prefer .error so the reason is
        # never lost, and fall back to .note for every other status.
        reason = (result.error or result.note or "").strip()

        cells = [
            result.booking_id,
            result.cruise_line.value,
            result.status.value,
            self._format_net_saving(result.net_saving, result.status.value,
                                    getattr(result, "price_drop", None)),
            str(result.confidence),
            f"${result.old_total:,.2f}" if result.old_total else "—",
            f"${result.new_total:,.2f}" if result.new_total else "—",
            (result.price_category or "—")
            + (f" → {result.new_price_category}"
               if result.new_price_category and result.new_price_category != result.price_category
               else ""),
            _relative_scan_time(getattr(result, "checked_at", None)),
            reason,
        ]
        # Numeric columns sort as text otherwise ("$9.00" > "$85.00"),
        # which made sorting by savings or confidence produce nonsense.
        # See NumericTableItem — setData(EditRole, ...) does NOT work here.
        sort_values = {
            0: _booking_id_sort_value(result.booking_id),
            2: _row_rank(result.status.value, result.net_saving),
            3: float(result.net_saving or 0.0),
            4: float(result.confidence or 0),
            5: float(result.old_total or 0.0),
            6: float(result.new_total or 0.0),
        }
        for col, text in enumerate(cells):
            if col in sort_values:
                item = NumericTableItem(text, sort_values[col])
            else:
                item = QTableWidgetItem(text)
            # The full note is usually wider than the column — make it
            # readable on hover rather than truncated and lost.
            if col == 9 and reason:
                item.setToolTip(reason)
            # Exact timestamp behind the relative time.
            if col == 8:
                stamp = getattr(result, "checked_at", None)
                if stamp is not None:
                    item.setToolTip(str(stamp))
            self.results_table.setItem(row, col, item)

        color = self._color_for_status(result.status.value)
        for col in range(self.results_table.columnCount()):
            item = self.results_table.item(row, col)
            if item is not None:
                item.setBackground(color)

    def _append_msc_result_row(self, outcome: MscCheckOutcome) -> None:
        """MSC counterpart to _append_result_row — outcome.result is an
        MscBookingResult (four independent checks) rather than the single
        old_total/new_total/net_saving BookingResult shape, when
        outcome.status == "checked"; otherwise it's a short-circuit status
        (not_found, cancelled, session_expired_after_relogin, etc.) with no
        result to show."""
        if self._bulk_depth:
            return self._append_msc_result_row_unguarded(outcome)
        with self._bulk_table_update():
            return self._append_msc_result_row_unguarded(outcome)

    def _append_msc_result_row_unguarded(self, outcome: MscCheckOutcome) -> None:
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        if outcome.status == "checked" and outcome.result is not None:
            status_text = "OPPORTUNITY" if outcome.result.has_any_opportunity else "NO_OPPORTUNITY"
            checks_text = f"{sum(1 for c in outcome.result.checks if c.status.value == 'OPPORTUNITY')}/{len(outcome.result.checks)} checks"
        else:
            status_text = outcome.status.upper()
            checks_text = "-"
        # Widened to the 9-column table (2026-08-27). MSC genuinely has no
        # single old/new total — its four checks are independent levers —
        # so those columns show "—" rather than a misleading number, and
        # the per-check notes (which name the specific discount and its
        # rationale) go into the Note column where they're actually
        # readable. Previously only a "N/4 checks" count survived.
        result = outcome.result
        category = (result.category if result is not None else None) or "—"
        if result is not None and result.checks:
            detail = " | ".join(
                f"{c.type.value}:{c.status.value}"
                + (f" ({c.note})" if c.note else "")
                for c in result.checks
            )
        else:
            detail = outcome.note or ""

        # THE DOLLAR VALUE, added 2026-09-03. MSC rows previously put
        # `outcome.note` in the "Net Saving / Summary" column and sorted it
        # to the bottom, so a real, confirmed MSC opportunity displayed as
        # "OPPORTUNITY" with NO amount anywhere in the row — booking
        # 3000081's verified $81.98 would have been invisible. Every other
        # line shows its money here.
        #
        # The LARGEST single lever, never a sum. MSC's four checks are
        # independent alternatives (a price match and a discount add are
        # different phone calls), so adding them would overstate the
        # opportunity — the same mistake this session spent its time
        # removing everywhere else.
        best_value = None
        best_lever = ""
        if result is not None:
            valued = [c for c in result.checks
                      if c.estimated_value and c.status.value == "OPPORTUNITY"]
            if valued:
                best = max(valued, key=lambda c: c.estimated_value)
                best_value = float(best.estimated_value)
                best_lever = best.type.value
        if best_value is not None:
            summary_text = f"${best_value:,.2f} ({best_lever})"
        else:
            summary_text = outcome.note

        cells = [
            outcome.booking_id,
            "MSC",
            status_text,
            summary_text,
            checks_text,
            "—",          # MSC has no single old_total
            "—",          # ...nor a single new_total
            category,
            # MSC runs outside the freshness cache (its own subsystem), so
            # it has no "last scanned" to report. A dash is honest; "Never"
            # would imply it is eligible and simply has not been done.
            "—",
            detail,
        ]
        # Keep columns 3-6 numeric on MSC rows too, so a mixed table (an
        # MSC batch and an NCL batch in one session) sorts consistently
        # instead of falling back to text comparison whenever a plain cell
        # happens to land on the left of a comparison. MSC has no dollar
        # totals, so those sort to the bottom (-1) rather than pretending
        # to be $0.00.
        opportunity_count = float(
            sum(1 for c in result.checks if c.status.value == "OPPORTUNITY")
        ) if result is not None else -1.0
        # Column 3 now sorts by the real dollar value when there is one, so
        # a mixed table ranks MSC findings alongside every other line's
        # money instead of always sinking them to the bottom.
        sort_values = {
            0: _booking_id_sort_value(outcome.booking_id),
            2: _row_rank(status_text, best_value),
            3: (best_value if best_value is not None else -1.0),
            4: opportunity_count, 5: -1.0, 6: -1.0,
        }
        for col, text in enumerate(cells):
            if col in sort_values:
                item = NumericTableItem(text, sort_values[col])
            else:
                item = QTableWidgetItem(text)
            if col == 8 and detail:
                item.setToolTip(detail)
            self.results_table.setItem(row, col, item)

        color = self._color_for_status(status_text)
        for col in range(self.results_table.columnCount()):
            item = self.results_table.item(row, col)
            if item is not None:
                item.setBackground(color)

    def _update_queue_view(self, snapshot) -> None:
        # IMPROVED 2026-08-27: the label used to read only
        # "N pending, N running" — during a long standard-path scan that
        # meant the operator watched a static line for minutes with no way
        # to tell a working scan from a hung one. The MSC path already
        # showed "checking X (3/26)"; the counts needed for the same
        # display were already reaching the GUI on every ScanJob progress
        # tick and were simply thrown away (see QueueSnapshot).
        parts = [f"{snapshot.queued} pending", f"{snapshot.running} running"]
        if snapshot.done:
            parts.append(f"{snapshot.done} done")
        if snapshot.error:
            parts.append(f"{snapshot.error} error")
        text = ", ".join(parts)
        total = getattr(snapshot, "progress_total", 0) or 0
        if total:
            done = getattr(snapshot, "progress_done", 0) or 0
            pct = int(round(100 * min(done, total) / total))
            text += f"  —  {done}/{total} ({pct}%)"
        current = getattr(snapshot, "current_booking_id", None)
        if current:
            text += f"  —  checking {current}"
        self.queue_status_label.setText(text)

        # Rebuilding the whole list widget on every 0.5s tick threw away
        # the operator's scroll position mid-scan (and, on a 100-booking
        # watchlist, rebuilt 100 widgets 2x/second for nothing). Only
        # rebuild when the rows actually changed.
        signature = tuple((item.booking_id, item.status.value) for item in snapshot.items)
        if signature == getattr(self, "_queue_view_signature", None):
            return
        self._queue_view_signature = signature

        # PLAIN ITEMS, UPDATED IN PLACE. Measured 2026-09-28 at a real
        # 721-booking watchlist size:
        #
        #     widget per row (the old way)   352.8 ms
        #     plain text item                 32.0 ms
        #
        # The old code built a QWidget + QHBoxLayout + QLabel (+ QPushButton)
        # for EVERY row and rebuilt the whole list whenever ANY booking
        # changed state. The signature guard above stops idle rebuilds, but
        # during a scan the signature changes constantly - each booking goes
        # QUEUED -> RUNNING -> DONE - so it fired roughly 2,163 times per
        # 721-booking run. At 353 ms each, ON THE UI THREAD, that is about
        # twelve minutes of frozen window per scan. That is the "heavy and
        # not responsive" Neon reported.
        #
        # The per-row "x" button was redundant: "Remove selected" sits above
        # the list and already works off the same Qt.UserRole booking id.
        #
        # Rows are now updated IN PLACE when only statuses changed, which is
        # the common case during a scan, and rebuilt only when the set of
        # bookings itself changes.
        # Rebuild when the BOOKINGS change, or whenever the widget has got
        # out of step with what we think is in it. The count check is not
        # defensive padding: anything that clears the list behind our back
        # would otherwise leave in-place updates writing into the wrong
        # rows, or into nothing at all - a silently wrong queue display is
        # worse than a slower correct one.
        ids = [item.booking_id for item in snapshot.items]
        if (ids != getattr(self, "_queue_view_ids", None)
                or self.queue_list.count() != len(ids)):
            self._queue_view_ids = ids
            self.queue_list.clear()
            for item in snapshot.items:
                list_item = QListWidgetItem(
                    f"{item.booking_id} [{item.status.value}]")
                list_item.setData(Qt.UserRole, item.booking_id)
                self.queue_list.addItem(list_item)
            return

        for row, item in enumerate(snapshot.items):
            list_item = self.queue_list.item(row)
            if list_item is None:
                break
            text = f"{item.booking_id} [{item.status.value}]"
            if list_item.text() != text:
                list_item.setText(text)

    async def refresh_last_scan_label(self) -> None:
        """Show the most recent COMPLETED scan per cruise line.

        Deliberately only COMPLETED — a FAILED or STOPPED job is not a
        successful scan, and reporting one as "last scan" is exactly the
        kind of false reassurance this review was asked to hunt for. Never
        raises: a housekeeping display must not be able to break the GUI.
        """
        try:
            from sqlalchemy import select

            from models.database import ScanJobRecord, async_session

            async with async_session() as session:
                rows = (await session.execute(
                    select(ScanJobRecord)
                    .where(ScanJobRecord.status == "COMPLETED")
                    .order_by(ScanJobRecord.completed_at.desc())
                    .limit(200)
                )).scalars().all()
            latest: dict[str, ScanJobRecord] = {}
            for r in rows:
                if r.cruise_line not in latest:
                    latest[r.cruise_line] = r
            if not latest:
                self.last_scan_label.setText("Last completed scan: none recorded yet")
                return
            parts = []
            for line in sorted(latest):
                rec = latest[line]
                when = rec.completed_at.strftime("%Y-%m-%d %H:%M") if rec.completed_at else "?"
                parts.append(f"{line} {when} ({rec.progress_done}/{rec.progress_total})")
            self.last_scan_label.setText("Last completed scan:  " + "   |   ".join(parts))
        except Exception as exc:
            logger.warning("gui.last_scan_label_failed", error=str(exc))
            self.last_scan_label.setText("Last completed scan: (unavailable)")

    async def load_todays_results(self) -> int:
        """Reload this line's results for today from the DATABASE.

        CONFIRMED REAL DEFECT, fixed 2026-08-28. Neon: "please look at the
        esspresso run there is zero optimization why is that". The run had
        in fact found **19 optimizations worth $3,175** - its scan_job shows
        769/769 COMPLETED, 13:33 to 18:07, and every row is in the DB. The
        table was empty because `self.results` is IN-MEMORY ONLY and nothing
        ever read the results back. Close the window, restart, or open a
        second instance and the operator sees a blank table and concludes
        the scan found nothing.

        That is the worst possible failure mode for this app: it does not
        lose the data, it just stops SHOWING it, so a good run looks like a
        wasted afternoon. Loading from the DB also means the footer's
        all-lines total is real on startup instead of $0.00.

        Never raises - a display failure must not stop the GUI opening.
        Returns how many rows were loaded.
        """
        try:
            from datetime import datetime

            from sqlalchemy import select

            from models.database import BookingRecord, async_session, init_db

            await init_db()
            today = datetime.utcnow().strftime("%Y-%m-%d")
            async with async_session() as session:
                records = (await session.execute(
                    select(BookingRecord)
                    .where(BookingRecord.cruise_line == self.cruise_line.value)
                    .order_by(BookingRecord.id.desc())
                    .limit(2000)
                )).scalars().all()

            # Latest row per booking for TODAY only - a re-scan supersedes
            # an earlier result, and showing both would double-count money.
            seen: set[str] = set()
            loaded: list[BookingResult] = []
            for rec in records:
                stamp = rec.created_at.strftime("%Y-%m-%d") if rec.created_at else ""
                if stamp != today or rec.booking_id in seen:
                    continue
                seen.add(rec.booking_id)
                loaded.append(_result_from_record(rec))

            loaded.reverse()
            self.results = loaded
            # AUTO-DETECT FIRST, then load the verified set, then draw.
            # Neon 2026-10-01: a reprice a human applied without pressing
            # Verify should be recognised by the script on its own. Doing
            # it here means today's results arrive already filtered,
            # instead of a verified row flashing up and then vanishing.
            try:
                await self._outcome_service.detect_applied(
                    cruise_line=self.cruise_line.value)
            except Exception as exc:  # noqa: BLE001 - never block startup
                logger.warning("gui.autodetect_failed", error=str(exc)[:200])
            await self._reload_verified()
            self._populate_results_table()
            self._refresh_summary()
            logger.info("gui.loaded_todays_results",
                        cruise_line=self.cruise_line.value, count=len(loaded))
            return len(loaded)
        except Exception as exc:
            logger.warning("gui.load_todays_results_failed",
                           cruise_line=self.cruise_line.value, error=str(exc))
            return 0

    def _populate_results_table(self) -> None:
        """Rebuild the whole table from self.results + self.msc_results.

        CONFIRMED LANDMINE, fixed 2026-08-27: this was a second, duplicate
        row writer still hard-coded to the OLD 4-column layout — and it
        wrote into the wrong columns of the new 9-column one (status into
        "Line", savings into "Status", confidence into "Net Saving", with
        columns 4-8 left empty). It happens to be unreachable today
        (nothing calls it), which is exactly why the widening didn't break
        anything visible, but the next caller would have got a silently
        garbled table. Delegating to the real row builders means there is
        now ONE place that knows the column layout.
        """
        with self._bulk_table_update():
            self.results_table.setRowCount(0)
            for result in self.results:
                self._append_result_row(result)
            for outcome in self.msc_results:
                self._append_msc_result_row(outcome)

    # Row tints. CHANGED 2026-08-28: these were Qt.green / Qt.red /
    # Qt.yellow / Qt.magenta — fully saturated primaries that made a table
    # of results genuinely hard to read (black text on pure red) and looked
    # like a debug harness. These are the SAME palette the Excel export
    # already uses (services/excel_export.py's _FILLS), so a row is the
    # same colour on screen as in the spreadsheet the client sees.
    _STATUS_TINTS = {
        "OPTIMIZATION": "#C6EFCE",          # soft green
        # Its own strong colour - see services/excel_export.py's _FILLS for
        # why a cancellation must not share the pale blue of PAID_IN_FULL.
        "CANCELLED": "#F4B183",             # orange
        # Deliberately distinct from OPTIMIZATION: a category upgrade is a
        # different physical room/deck and always needs human review — never
        # the same one-click confidence as a confirmed same-category win.
        "UPGRADE_AVAILABLE": "#D9D2E9",     # light purple
        "TRAP": "#FFC7CE",                  # soft red
        "NO_SAVING": "#F2F2F2",             # near-white grey
        "WLT": "#DDEBF7",                   # light blue
        "PAID_IN_FULL": "#DDEBF7",
        "SKIPPED_TODAY": "#EFEFEF",
        # Deliberately NOT the ERROR tint. Nothing is broken here — the
        # booking lives on NCL's other market account (Canada/CAD), so it
        # needs a re-run on that login, not debugging. Confirmed by Neon
        # 2026-08-27 after 25 Canadian bookings all read "Reservation is
        # not found" against the US account.
        "NOT_ON_THIS_ACCOUNT": "#FFF2CC",   # light amber
        "ERROR": "#FBE4E4",                 # very light red-grey
        # MSC statuses (see _append_msc_result_row) — OPPORTUNITY means a
        # finding to call MSC about, never a same-category win the agent
        # applied itself the way ESPRESSO's OPTIMIZATION does.
        "OPPORTUNITY": "#C6EFCE",
        "NO_OPPORTUNITY": "#F2F2F2",
        "NOT_FOUND": "#FBE4E4",
        "SESSION_EXPIRED_AFTER_RELOGIN": "#FBE4E4",
        "CONFIRM_BUTTON_NOT_FOUND": "#FBE4E4",
    }

    def _color_for_status(self, status: str) -> QColor:
        return QColor(self._STATUS_TINTS.get(status, "#FFFFFF"))

    @Slot()
    def _on_export(self) -> None:
        if not self.results and not self.msc_results:
            QMessageBox.information(self, "Nothing to export", "Run a scan first to export results.")
            return

        export_dir = Path("reports")
        export_dir.mkdir(exist_ok=True)
        saved = []

        # STAMPED FILENAMES, fixed 2026-09-16 after a real loss. Every
        # panel wrote to the SAME two files - reports/scan_results.csv and
        # .xlsx - with no cruise line and no date in the name. Exporting
        # ESPRESSO and then NCL silently overwrote the ESPRESSO export,
        # with no warning and no way to tell afterwards which line the
        # surviving file belonged to.
        #
        # Named <LINE>_scan_results_<YYYYMMDD>_<HHMMSS> so two lines can
        # never collide, two runs of the SAME line on one day cannot
        # collide either, and files sort chronologically within a line.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        line = self.cruise_line.value

        if self.results:
            csv_path = export_dir / f"{line}_scan_results_{stamp}.csv"
            xlsx_path = export_dir / f"{line}_scan_results_{stamp}.xlsx"
            self.adapter.export_csv(self.results, str(csv_path))
            self.adapter.export_excel(self.results, str(xlsx_path))
            saved.append(str(csv_path))
            saved.append(str(xlsx_path))

        if self.msc_results:
            # Only the "checked" outcomes carry an MscBookingResult worth a
            # row — not_found/cancelled/session_expired_after_relogin etc.
            # have nothing to evaluate (see MscCheckOutcome's docstring).
            msc_checked = [o.result for o in self.msc_results if o.result is not None]
            if msc_checked:
                msc_csv_path = export_dir / f"MSC_scan_results_{stamp}.csv"
                self.adapter.export_msc_csv(msc_checked, str(msc_csv_path))
                saved.append(str(msc_csv_path))

        QMessageBox.information(self, "Export complete", "Saved:\n" + "\n".join(saved))
        self.status_label.setText(f"Exported to {export_dir}")


class MainWindow(QMainWindow):
    #: Grace period between asking the app to quit and forcing the
    #: process to exit. Long enough for a normal interpreter shutdown,
    #: short enough that a hang is not something the operator notices.
    FORCE_EXIT_MS = 3000

    #: Hard ceiling on shutdown. Panels close concurrently, so this is
    #: the WHOLE app's budget, not per tab. Generous enough for a batch
    #: to finish its current booking (~30s measured on ESPRESSO) and
    #: for browsers to save their session state.
    SHUTDOWN_TIMEOUT_SECONDS = 45
    """Tabbed shell: one CruiseLinePanel per cruise line.

    ADDED 2026-08-28 at Neon's request ("change the gui to tabs then so a
    tab is esspresso another tab is ncl another tab is msc etc").

    WHY TABS ACTUALLY FIX THE CONCURRENCY PROBLEM, not just the layout: the
    old single window had ONE BookingQueueManager, and its BookingService
    keeps a single `_live_scraper` slot that it STOPS whenever the cruise
    line changes (see BookingService.get_or_create_scraper). Logging into
    ESPRESSO and then into NCL therefore destroyed the ESPRESSO session, so
    two lines could not be live at once no matter what the UI looked like.
    Each tab now owns its own manager, service and browser, so several lines
    genuinely run side by side.

    DELIBERATELY NOT the SharedBrowserPool: one Chromium with isolated
    contexts uses far less RAM (measured 415MB for 2 contexts vs ~700MB per
    standalone browser), but ESPRESSO breaks when a session logged in inside
    one browser is replayed into another, and a single close-and-reopen is
    enough (DOCUMENTATION.md section L). One browser per tab keeps every
    line's session continuous from login through its last booking, which is
    the rule that matters most here. The pool stays available for lines that
    tolerate it.

    Concurrency is therefore bounded by RAM rather than a semaphore. The
    shared resource readout stays on screen and 2 concurrent lines remains
    the measured recommendation - this machine idles near 87% RAM.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CruiseIntel Desktop Scanner")
        self.setMinimumSize(1180, 820)
        self._shutting_down = False

        # ONE font stack for the whole app. Neon: "fonts are bad". The old
        # window mixed the Qt default, an explicit "Arial" on the summary,
        # and Consolas on three status lines, at 9/10/11px with inline
        # style strings scattered across ~200 lines of layout code — so
        # nothing lined up and weights were inconsistent. Segoe UI is the
        # native Windows UI face (this app is Windows-only: START_GUI.bat,
        # msvcrt, Windows Credential Manager), and monospace is now used
        # ONLY where column alignment genuinely matters: the resource and
        # last-scan readouts and the activity log.
        self.setStyleSheet("""
            QWidget { font-family: "Segoe UI", "Segoe UI Variable", Arial, sans-serif;
                      font-size: 12px; }
            QGroupBox { font-weight: 600; border: 1px solid #c8c8c8;
                        border-radius: 6px; margin-top: 10px; padding-top: 6px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px;
                               padding: 0 5px; color: #333; }
            QPushButton { padding: 5px 12px; border: 1px solid #b4b4b4;
                          border-radius: 4px; background: #fafafa; }
            QPushButton:hover:!disabled { background: #eef4fb; border-color: #7aa7d9; }
            QPushButton:disabled { color: #9a9a9a; background: #f2f2f2; }
            QTabBar::tab { padding: 7px 18px; font-weight: 600; }
            QTableWidget { gridline-color: #e2e2e2; }
            QHeaderView::section { background: #f0f0f0; padding: 5px;
                                   border: none; border-right: 1px solid #dcdcdc;
                                   border-bottom: 1px solid #dcdcdc;
                                   font-weight: 600; }
            QLineEdit, QTextEdit, QListWidget { border: 1px solid #c4c4c4;
                                               border-radius: 4px; padding: 3px; }
        """)

        container = QWidget()
        layout = QVBoxLayout(container)

        # Footer widgets are created BEFORE the panels on purpose: a panel's
        # _build_ui calls _refresh_summary, which calls back up here. Build
        # the tabs first and the callback fires against a half-built window
        # (AttributeError, caught and logged, footer silently empty at
        # startup).
        self.global_summary_label = QLabel()
        self.global_summary_label.setWordWrap(True)
        self.global_summary_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.resource_label = QLabel("Resources: (sampling...)")
        self.resource_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;")
        self.last_scan_label = QLabel("Last completed scan: (loading...)")
        self.last_scan_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;")
        # LOGS MOVED OUT OF THE MAIN SCREEN. Neon 2026-10-01, marking the
        # bottom strip "L" on a screenshot: *"Remove the large logs section
        # from the bottom of the main GUI ... create a dedicated Logs
        # tab."*
        #
        # That strip was 110px fixed - six lines of a 721-booking run -
        # unfiltered, unbounded, and repainted on every append underneath
        # the results table the operator was trying to read.
        #
        # `self.activity_log` is KEPT as a name because _append_activity
        # and the panels write to it; it is now the Logs tab's view.
        self.logs_panel = LogsPanel()
        self.activity_log = self.logs_panel
        self.dashboard_panel = DashboardPanel()
        # Built on first use. The panel above is the live sink and exists
        # from startup, so lines are collected whether or not the window
        # has ever been opened.
        self._logs_window = None

        self.tabs = QTabWidget()
        self.panels: dict[CruiseLine, CruiseLinePanel] = {}
        for line in CruiseLine:
            panel = CruiseLinePanel(
                line,
                on_activity=self._append_activity,
                on_summary_changed=self._refresh_global_summary,
            )
            self.panels[line] = panel
            self.tabs.addTab(panel, line.value)

        # Monitoring tabs last, so the cruise lines keep positions 0..n and
        # nothing that addresses a tab by index moves.
        self.tabs.addTab(self.dashboard_panel, "Dashboard")
        layout.addWidget(self.tabs, 1)

        # The compact footer stays - three lines, no log block. The totals
        # and the resource readout are the two things worth seeing whatever
        # tab is open.
        layout.addWidget(self.global_summary_label)

        # LOGS: A BUTTON, NOT A TAB. Neon 2026-10-01: *"i do not want a
        # logs tab i want a logs button when i press it it opens another
        # window that list the logs."*
        #
        # A tab was the wrong shape - logs are consulted WHILE watching a
        # scan, and as a tab they replaced the view being watched, which is
        # no better than the 110px strip they replaced. The window is
        # non-modal, so it sits beside the scanner rather than blocking it.
        status_row = QHBoxLayout()
        status_row.addWidget(self.resource_label, 1)
        self.logs_button = QPushButton("Logs")
        self.logs_button.setMinimumWidth(90)
        self.logs_button.setToolTip(
            "Open the activity log in its own window (all cruise lines). "
            "Searchable and filterable by level; the scanner stays usable "
            "while it is open.")
        self.logs_button.clicked.connect(self._on_show_logs)
        status_row.addWidget(self.logs_button)
        layout.addLayout(status_row)

        layout.addWidget(self.last_scan_label)

        self.setCentralWidget(container)

        # Live resource readout. CPU/RAM used to be shown only while a
        # MultiLineCoordinator was driving, and nothing ever constructed
        # one, so the field read "(not scanning)" permanently. With several
        # tabs able to hold browsers at once RAM is the real limit, so it is
        # sampled continuously here instead.
        # Cadence for the Chromium census inside _refresh_resources: every
        # 5th tick, i.e. ~15s, against the 3s cpu/ram refresh. See the
        # measurement recorded there for why the two are separated.
        self._resource_tick = 0
        self._chrome_census: tuple[int, float] | None = None
        self._resource_timer = QTimer(self)
        self._resource_timer.timeout.connect(self._refresh_resources)
        self._resource_timer.start(3000)

        # KEEP IDLE PORTAL SESSIONS ALIVE.
        #
        # ESPRESSO arms a 30.5-minute client-side auto-logout on every page
        # load (see EspressoScraper.keep_session_alive). A batch re-arms it
        # constantly while THAT line is scanning - but a tab belonging to a
        # DIFFERENT line just sits there. Observed 2026-09-22 in the log:
        # ESPRESSO went quiet at 14:40, NCL scanned from 14:42 onward, and
        # by 15:15 the ESPRESSO session had signed itself out mid-session -
        # 35 minutes idle, past its own limit.
        #
        # Every 5 minutes, touch any line that has a live session and is NOT
        # currently scanning. keep_session_alive is itself a no-op unless
        # the page has actually been idle past its threshold, so this costs
        # nothing on a busy tab.
        self._keepalive_timer = QTimer(self)
        self._keepalive_timer.timeout.connect(self._keep_sessions_alive)
        self._keepalive_timer.start(5 * 60 * 1000)
        self._refresh_resources()
        self._refresh_global_summary()

    def _on_show_logs(self) -> None:
        """Open (or raise) the Logs window. Never raises.

        ONE instance, reused. Building a second would leave half the
        session's history in a window the operator just closed, and the
        panel inside is the live sink that every scraper writes to.
        """
        try:
            if getattr(self, "_logs_window", None) is None:
                self._logs_window = LogsWindow(self.logs_panel, self)
            self._logs_window.show()
            self._logs_window.raise_()
            self._logs_window.activateWindow()
        except Exception:
            logger.exception("gui.logs_window_failed")

    # -- shared surfaces the panels report into --

    def _append_activity(self, cruise_line, text: str) -> None:
        try:
            label = getattr(cruise_line, "value", str(cruise_line))
            self.activity_log.append("[" + label + "] " + str(text))
        except Exception:
            logger.exception("gui.activity_append_failed")

    def _refresh_global_summary(self) -> None:
        """Totals ACROSS every tab, so switching tabs never hides money."""
        try:
            all_results: list[BookingResult] = []
            for panel in self.panels.values():
                all_results.extend(getattr(panel, "results", []))
            total = total_optimization_savings(all_results)
            unconf_n, unconf_total = unconfirmed_candidate_total(all_results)
            opt = sum(1 for r in all_results if r.status.value == "OPTIMIZATION")
            busy = [l.value for l, p in self.panels.items() if p.is_busy()]
            parts = [
                str(len(all_results)) + " checked across all lines",
                str(opt) + " optimization(s)",
                "${:,.2f} confirmed savings".format(total),
            ]
            if unconf_n:
                parts.append(
                    "+ {} UNCONFIRMED (${:,.2f}) - verify before trusting".format(
                        unconf_n, unconf_total)
                )
            if busy:
                parts.append("RUNNING: " + ", ".join(busy))
            self.global_summary_label.setText("   |   ".join(parts))
        except Exception:
            logger.exception("gui.global_summary_failed")

    @asyncSlot()
    async def _keep_sessions_alive(self) -> None:
        """Touch idle portal sessions so they do not time themselves out.

        Skips any line that is mid-scan - that line is re-arming its own
        timer with every navigation, and interrupting it with an extra
        navigation would be worse than useless. Never raises: a failed
        keepalive must not disturb a GUI that is otherwise fine.
        """
        for line, panel in self.panels.items():
            try:
                if panel.is_busy():
                    continue
                service = getattr(panel.queue_manager, "_service", None)
                scraper = getattr(service, "_live_scraper", None)
                if scraper is None or scraper.cruise_line != line:
                    continue
                if not scraper.is_alive:
                    continue
                keepalive = getattr(scraper, "keep_session_alive", None)
                if keepalive is None:
                    continue
                if await keepalive():
                    logger.info("gui.session_kept_alive", cruise_line=line.value)
            except Exception as exc:
                logger.debug("gui.keepalive_failed",
                             cruise_line=getattr(line, "value", "?"),
                             error=str(exc)[:200])

    def _refresh_resources(self) -> None:
        """CPU / RAM / Chromium footprint, sampled for real."""
        try:
            import psutil

            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent

            # THE EXPENSIVE HALF, SAMPLED FAR LESS OFTEN.
            #
            # Measured on this machine, 2026-09-18: the Chromium census
            # below costs a MEDIAN of 66 ms and a MAX of 900 ms, against
            # 4 ms for the cpu/ram read above - because process_iter walks
            # all 341 processes on the machine and memory_info() is then
            # called on each of the ~21 Chromium processes. On a 3-second
            # timer, ON THE UI THREAD, that is 1,200 full process-table
            # walks an hour and an occasional near-second freeze. It is the
            # single most expensive thing the GUI does, and it measures
            # something that barely moves between ticks.
            #
            # CPU/RAM still refresh every 3s (they are cheap and they do
            # move); the census refreshes every _CHROME_CENSUS_EVERY ticks
            # and is otherwise reused from the last sample.
            self._resource_tick += 1
            if (self._chrome_census is None
                    or self._resource_tick % self._CHROME_CENSUS_EVERY == 0):
                chrome = [p for p in psutil.process_iter(["name"])
                          if (p.info["name"] or "").lower() == "chrome.exe"]
                rss = 0.0
                for p in chrome:
                    try:
                        rss += p.memory_info().rss / 1e6
                    except Exception:
                        pass
                self._chrome_census = (len(chrome), rss)
            chrome_count, rss = self._chrome_census
            live = []
            for line, panel in self.panels.items():
                try:
                    if panel.queue_manager.has_live_session(line) or panel.msc_service.is_alive:
                        live.append(line.value)
                except Exception:
                    pass
            self.resource_label.setText(
                "Resources: CPU {:4.1f}%  RAM {:4.1f}%  Chromium {} proc / {:,.0f} MB"
                "  live sessions: {}".format(
                    cpu, ram, chrome_count, rss,
                    ", ".join(live) if live else "none")
            )
            # 93% is the ResourceGovernor's throttle threshold and this
            # machine idles near 87%, so warn there rather than cry wolf.
            self.resource_label.setStyleSheet(
                "font-family: Consolas, monospace; font-size: 11px;"
                + ("color: #b00; font-weight: bold;" if ram >= 93.0 else "")
            )
            # Same tick feeds the Dashboard. No second timer: the census
            # above is the most expensive thing this GUI does, and adding a
            # tab must not make it happen more often.
            self._refresh_dashboard(cpu, ram, chrome_count, rss, live)
        except Exception:
            self.resource_label.setText("Resources: (unavailable)")

    def _refresh_dashboard(self, cpu: float, ram: float,
                           chrome_count: int, rss: float, live: list) -> None:
        """Push current state into the Dashboard tab. Never raises.

        Reads only what the window already has in hand - no queries, no
        process walks of its own. DashboardPanel.update_fields then writes
        only the values that actually changed, because setText on an
        unchanged string still repaints.
        """
        try:
            scanning = []
            errors = 0
            found = 0
            current = None
            progress = None
            for line, panel in self.panels.items():
                try:
                    snap = panel.queue_manager.get_snapshot()
                except Exception:
                    continue
                errors += getattr(snap, "error", 0)
                found += sum(
                    1 for r in getattr(panel, "results", [])
                    if getattr(r.status, "value", r.status) == "OPTIMIZATION")
                if getattr(snap, "running", 0) or getattr(snap, "progress_total", 0) and (
                        panel.queue_manager.is_running):
                    scanning.append(line.value)
                    current = getattr(snap, "current_booking_id", None)
                    total = getattr(snap, "progress_total", 0)
                    done = getattr(snap, "progress_done", 0)
                    if total:
                        progress = f"{done}/{total}  ({done * 100 // total}%)"

            state = "SCANNING" if scanning else ("IDLE" if live else "NOT CONNECTED")
            self.dashboard_panel.update_fields({
                "state": state,
                "line": ", ".join(scanning) if scanning else "—",
                "booking": current,
                "progress": progress,
                "login": ("OK: " + ", ".join(live)) if live else "no live session",
                "browser": f"{chrome_count} Chromium proc / {rss:,.0f} MB",
                "results": f"{found} optimization(s) on screen",
                "errors": f"{errors} error(s) this run",
                "cpu": f"CPU {cpu:.1f}%   RAM {ram:.1f}%",
                "processes": str(chrome_count),
                "last_scan": self.last_scan_label.text().replace(
                    "Last completed scan: ", ""),
                "realised": self._realised_summary(),
                "next_scan": self._next_scan_summary(),
                "reused": self._reuse_summary(),
            })
        except Exception as exc:  # noqa: BLE001
            logger.debug("gui.dashboard_refresh_failed", error=str(exc)[:200])

    def _realised_summary(self) -> str | None:
        """Money actually captured, as the panels already display it."""
        for panel in self.panels.values():
            text = getattr(getattr(panel, "realised_label", None), "text", lambda: "")()
            if text:
                return text
        return None

    def _next_scan_summary(self) -> str | None:
        """When the 2-hour suppression window next lets a scan run."""
        for line, panel in self.panels.items():
            plan = getattr(panel, "_last_scan_plan", None)
            if plan and plan.get("action") == "reuse":
                when = plan.get("next_allowed_at")
                if when is not None:
                    return (f"{line.value}: {when.strftime('%H:%M')} "
                            f"({plan.get('minutes_remaining', 0)} min)")
        return None

    def _reuse_summary(self) -> str | None:
        """Whether the last Start reused previous results instead of
        scanning - the thing Neon could never tell before."""
        for line, panel in self.panels.items():
            plan = getattr(panel, "_last_scan_plan", None)
            if not plan:
                continue
            if plan.get("action") == "reuse":
                when = plan.get("scanned_at")
                stamp = when.strftime("%H:%M") if when else "earlier"
                return (f"{line.value}: reused {plan.get('bookings', 0)} "
                        f"booking(s) from {stamp} — no rescan")
            overlap = plan.get("overlap") or {}
            if overlap:
                return (f"{line.value}: {overlap.get('shared', 0)} already known, "
                        f"{overlap.get('added', 0)} new, "
                        f"{overlap.get('removed', 0)} dropped")
        return None

    async def refresh_last_scan_label(self) -> None:
        """The query is global, so borrow any panel's implementation."""
        panel = next(iter(self.panels.values()))
        panel.last_scan_label = self.last_scan_label
        await panel.refresh_last_scan_label()

    # -- shutdown: every panel, not just the visible one --

    def closeEvent(self, event) -> None:
        """Always ignore the close and tear down asynchronously.

        Reasoning unchanged from the single-window version this replaces:
        the window stays interactive during an async shutdown, so accepting
        the close on a second click let Qt's quitOnLastWindowClosed stop the
        loop before browser sessions were saved - orphaned Chromium, lost
        storage_state, forced re-login next launch. Only _shutdown_all()'s
        own quit() ends the app.

        Now tears down EVERY tab: with several lines able to hold live
        browsers at once, closing only the visible one would leak the rest.
        """
        if self._shutting_down:
            event.ignore()
            self.global_summary_label.setText(
                "Still shutting down - closing browser sessions, please wait...")
            return
        event.ignore()
        self._shutting_down = True
        self._resource_timer.stop()
        self.tabs.setEnabled(False)
        busy = [l.value for l, p in self.panels.items() if p.is_busy()]
        self.global_summary_label.setText(
            "Stopping " + (", ".join(busy) if busy else "all lines")
            + " and closing browser sessions...")
        # RETAINED, 2026-09-30. This was the only task creation in this
        # file and it had no strong reference, so the event loop's own
        # reference was all that kept SHUTDOWN alive. If it is collected
        # mid-flight the browser sessions never close - which means
        # orphaned Chromium processes and, worse, ESPRESSO bookings left
        # LOCKED, the one bug pinned as V-VIP. Held on self for the
        # window's lifetime; there is exactly one shutdown.
        self._shutdown_task = asyncio.ensure_future(self._shutdown_all())
        # Tick the message while teardown runs. A close that legitimately
        # takes 30 seconds - because a booking is mid-flight - is
        # indistinguishable from a hang if nothing on screen moves.
        self._shutdown_started = time.monotonic()
        self._shutdown_ticker = QTimer(self)
        self._shutdown_ticker.timeout.connect(self._tick_shutdown_message)
        self._shutdown_ticker.start(1000)

    def _tick_shutdown_message(self) -> None:
        """Keep the shutdown notice moving so it does not look frozen."""
        try:
            waited = int(time.monotonic() - self._shutdown_started)
            busy = [l.value for l, p in self.panels.items() if p.is_busy()]
            remaining = max(0, self.SHUTDOWN_TIMEOUT_SECONDS - waited)
            self.global_summary_label.setText(
                f"Closing browser sessions... {waited}s"
                + (f"  (waiting on {', '.join(busy)} to finish the booking "
                   f"in progress)" if busy else "")
                + f"  -  will close within {remaining}s"
            )
        except Exception:
            pass

    async def _shutdown_all(self) -> None:
        """Tear every tab down AT ONCE, with a hard ceiling.

        Neon 2026-09-22: "when i press quite or close it stucks".

        Two reasons it stuck, both fixed here. Panels were shut down
        SEQUENTIALLY, and each one waits up to 60 seconds for its scan to
        stop - so four tabs could take four minutes. And that wait is real,
        not theoretical: a batch deliberately finishes its current booking
        before stopping, and a measured ESPRESSO booking takes ~30s.

        Panels now run concurrently. The ordering rule INSIDE a panel is
        untouched and still load-bearing (stop the scan, then close the
        browser - closing one out from under an in-flight check_booking
        makes booking_service treat it as a crash and start a SECOND browser
        while the app is trying to quit). Running different panels at the
        same time does not affect that: each owns its own queue manager,
        service and browser.

        The overall ceiling means the window always closes. A browser that
        will not shut down cleanly is worth at most this wait - the session
        state is saved by then, and Chromium exits with the process.
        """
        started = time.monotonic()

        async def close_panel(line, panel):
            try:
                await panel.shutdown()
                logger.info("gui.panel_shutdown_ok", cruise_line=line.value)
            except Exception:
                logger.exception("gui.panel_shutdown_failed", cruise_line=line.value)

        try:
            await asyncio.wait_for(
                asyncio.gather(*(close_panel(line, panel)
                                 for line, panel in self.panels.items()),
                               return_exceptions=True),
                timeout=self.SHUTDOWN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            slow = [l.value for l, p in self.panels.items() if p.is_busy()]
            logger.warning("gui.shutdown_timed_out",
                           after_seconds=int(time.monotonic() - started),
                           still_busy=slow)
        try:
            self._shutdown_ticker.stop()
        except Exception:
            pass
        logger.info("gui.shutdown_complete",
                    seconds=round(time.monotonic() - started, 1))

        # QUIT, THEN MAKE SURE THE PROCESS ACTUALLY ENDS.
        #
        # Neon 2026-09-22: "same quiting issue it is not closing it is
        # still hanging" - AFTER the concurrent-teardown fix. The log showed
        # teardown was never the problem:
        #
        #   18:14:19 gui.panel_shutdown_ok  NCL / GOCCL / MSC
        #   18:14:19 browser.session_saved  ESPRESSO
        #   18:14:20 browser.stopped        ESPRESSO
        #   18:14:20 gui.shutdown_complete  seconds=0.7
        #
        # 0.7 seconds, everything saved - and the Python process was still
        # alive thirteen minutes later. So quit() returns, the window goes,
        # and the interpreter never exits: qasync's loop is stopped from
        # inside one of its own callbacks, and what is left (pending tasks,
        # the default thread-pool executor behind asyncio.to_thread, the
        # Playwright subprocess transports) keeps the process up.
        #
        # Everything that must survive is already on disk BEFORE this point
        # - browser.session_saved is logged above - so once the loop has had
        # a moment to unwind there is nothing left worth waiting for. The
        # timer is the backstop, not the plan: a clean exit cancels it.
        # ARM THE BACKSTOP FIRST, ON ITS OWN THREAD.
        #
        # My previous attempt armed it with QTimer.singleShot AFTER calling
        # app.quit() and loop.stop() - so it was scheduled on an event loop
        # that had just been stopped and could never fire. Neon had to end
        # the process from Task Manager, which is exactly what the backstop
        # existed to prevent. A watchdog that depends on the thing it is
        # watching is not a watchdog.
        #
        # threading.Timer runs on its own thread and is daemonised, so it
        # neither depends on Qt or asyncio being alive nor keeps the process
        # up if the normal path succeeds first.
        self._exit_timer = threading.Timer(
            self.FORCE_EXIT_MS / 1000.0, self._force_exit)
        self._exit_timer.daemon = True
        self._exit_timer.start()

        app = QApplication.instance()
        if app is not None:
            app.quit()
        try:
            asyncio.get_running_loop().stop()
        except Exception:
            pass

    def _force_exit(self) -> None:
        """Last resort: end a process that refuses to exit on its own.

        Reached only if the interpreter is still alive after quit(), the
        loop stop and FORCE_EXIT_MS. Session state and every result are
        already persisted by then, so there is nothing to lose - and a
        desktop app that will not close is worse than a blunt exit.
        """
        logger.warning("gui.force_exit",
                       msg="process still alive after shutdown - exiting")
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(0)
