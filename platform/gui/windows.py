"""Main desktop window for CruiseIntel GUI."""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from core.calculator import total_optimization_savings
from core.models import BookingResult, CruiseLine
from gui.queue_manager import BookingQueueManager, QueueStatus
from gui.scan_adapter import GuiScanAdapter
from services.msc_live_service import MscCheckOutcome, MscLiveService
from utils.logging import get_logger

logger = get_logger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CruiseIntel Desktop Scanner")
        self.setMinimumSize(980, 680)

        self.adapter = GuiScanAdapter()
        self.queue_manager = BookingQueueManager()
        # MSC runs through its own service, not BookingQueueManager/
        # BookingService — see msc_live_service.py's module docstring for
        # why (MscBookingResult's four-independent-check shape doesn't fit
        # BookingResult's single old_total/new_total/net_saving shape).
        self.msc_service = MscLiveService()
        self.results: list[BookingResult] = []
        self.msc_results: list[MscCheckOutcome] = []
        self._shutting_down = False
        # Which cruise line we have CONFIRMED a successful login for.
        # ADDED 2026-08-26 — see _on_login_check: `has_live_session()` and
        # `msc_service.is_alive` only prove a browser/page is OPEN, not that
        # login actually succeeded, so they cannot be used as the Start
        # guard on their own. Set only on a real success, cleared on
        # timeout/failure and whenever the cruise-line selection changes.
        self._login_ok_for: CruiseLine | None = None

        self._build_ui()
        self._refresh_summary()
        self._update_queue_view(self.queue_manager.get_snapshot())

    def closeEvent(self, event) -> None:
        """Close the live browser session (saving its final state) before
        the app actually exits, instead of leaving it dangling."""
        # CONFIRMED REAL REGRESSION RISK, fixed 2026-08-26: this used to
        # `event.accept()` on the SECOND close attempt. But the window stays
        # visible and fully interactive during the async shutdown, so
        # double-clicking the X accepted the close, Qt's
        # quitOnLastWindowClosed stopped the event loop, and
        # _shutdown_and_close() NEVER reached close_live_session() — which is
        # exactly the 2026-08-13 bug documented below (browser killed
        # mid-booking, storage_state never written, orphaned Chromium,
        # forced re-login next launch).
        #
        # Now the close is ALWAYS ignored here; only
        # _shutdown_and_close()'s own QApplication.quit() ends the app, so
        # the teardown sequence can't be short-circuited. The UI is also
        # disabled so no new scan can be launched mid-teardown (the
        # _on_start finally-block would otherwise re-enable Start once the
        # stop completed).
        if self._shutting_down:
            event.ignore()
            self.status_label.setText("Still shutting down — closing the browser session, please wait...")
            return
        event.ignore()
        self._shutting_down = True
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(False)
        self.status_label.setText("Stopping scan and closing browser session...")
        asyncio.ensure_future(self._shutdown_and_close())

    async def _shutdown_and_close(self) -> None:
        """CONFIRMED REAL BUG, fixed 2026-08-13: this used to close the
        browser immediately, even with a scan still running. That made
        _run_batch's in-flight `scraper.check_booking(...)` call fail
        with "Target page, context or browser has been closed" —
        booking_service.py's OWN dead-browser recovery then interpreted
        that as a crash and started a SECOND, brand-new browser to keep
        working through the remaining queue, racing against this
        function which had already asked to quit the application.

        Correct sequence: stop the scan first, wait (bounded — never
        freeze the GUI indefinitely) for it to actually finish the
        booking it's mid-flight on, THEN close the browser, THEN quit."""
        try:
            if self.queue_manager.is_running or self.msc_service.is_running:
                self.status_label.setText("Stopping scan (finishing current booking)...")
                self.queue_manager.stop_processing()
                self.msc_service.stop_processing()
                # Bounded wait, not indefinite — 30s is generously more
                # than one booking's real worst-case (network timeout +
                # retry), matching this project's own scraper timeout
                # settings, without risking a permanent freeze if
                # something is truly stuck.
                for _ in range(150):
                    if not self.queue_manager.is_running and not self.msc_service.is_running:
                        break
                    await asyncio.sleep(0.2)
                else:
                    logger.warning("gui.shutdown_scan_stop_timeout")
            self.status_label.setText("Closing browser session...")
            await self.queue_manager.close_live_session()
            if self.msc_service.is_alive:
                await self.msc_service.stop()
        except Exception:
            traceback.print_exc()
        QApplication.instance().quit()

    def _build_ui(self) -> None:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(12)
        layout.setContentsMargins(12, 12, 12, 12)

        self.login_status_label = QLabel("Login status: not checked")
        self.login_status_label.setStyleSheet("color: #444444; font-weight: bold;")
        layout.addWidget(self.login_status_label)

        top_layout = QGridLayout()
        top_layout.setHorizontalSpacing(12)
        top_layout.setVerticalSpacing(10)

        top_layout.addWidget(QLabel("Booking ID:"), 0, 0)
        self.booking_input = QLineEdit()
        self.booking_input.returnPressed.connect(self._add_booking)
        top_layout.addWidget(self.booking_input, 0, 1)

        self.add_booking_button = QPushButton("Add to queue")
        self.add_booking_button.clicked.connect(self._add_booking)
        top_layout.addWidget(self.add_booking_button, 0, 2)

        top_layout.addWidget(QLabel("Cruise Line:"), 1, 0)
        self.cruise_line_selector = QComboBox()
        # CONFIRMED REAL BUG 2026-08-12, RE-ENABLED 2026-08-14: MSC used to
        # be listed here despite having no scraper in the BookingService/
        # BaseScraper pipeline at all (it's driven by the separate
        # msc_commands.py subsystem) — BookingService._get_scraper would
        # silently fall through to EspressoScraper for it, so it was
        # excluded from this dropdown entirely. MSC is back now that
        # _on_login_check/_on_start below branch to MscLiveService (see
        # msc_live_service.py) instead of ever routing it through
        # BookingService — every cruise line in the dropdown now has a
        # real, working path.
        self.cruise_line_selector.addItems([c.value for c in CruiseLine])
        # ADDED 2026-08-26: switching the selector must invalidate the
        # confirmed-login flag. Without this, logging into ESPRESSO then
        # switching to NCL left `_login_ok_for == ESPRESSO` while the label
        # still read "OK" — and while `has_live_session` DOES catch the
        # cruise-line mismatch, relying on that alone left the on-screen
        # status lying about which line was actually authenticated.
        self.cruise_line_selector.currentTextChanged.connect(self._on_cruise_line_changed)
        top_layout.addWidget(self.cruise_line_selector, 1, 1)

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self._on_start)
        top_layout.addWidget(self.start_button, 1, 2)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._on_stop)
        self.stop_button.setEnabled(False)
        top_layout.addWidget(self.stop_button, 0, 3)

        self.login_button = QPushButton("Check login")
        self.login_button.clicked.connect(self._on_login_check)
        top_layout.addWidget(self.login_button, 1, 3)

        layout.addLayout(top_layout)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setFont(QFont("Arial", 10, QFont.Bold))
        layout.addWidget(self.summary_label)

        queue_layout = QGridLayout()
        queue_layout.setHorizontalSpacing(12)
        queue_layout.setVerticalSpacing(10)

        queue_layout.addWidget(QLabel("Bulk booking IDs (comma or newline separated):"), 0, 0, 1, 2)
        self.bulk_input = QTextEdit()
        self.bulk_input.setFixedHeight(100)
        queue_layout.addWidget(self.bulk_input, 1, 0, 1, 2)

        self.add_bulk_button = QPushButton("Add list")
        self.add_bulk_button.clicked.connect(self._add_bulk)
        queue_layout.addWidget(self.add_bulk_button, 1, 2)

        self.force_recheck_checkbox = QCheckBox("Force live recheck")
        queue_layout.addWidget(self.force_recheck_checkbox, 2, 0, 1, 2)

        self.capture_market_data_checkbox = QCheckBox("Collect market data (category/offer snapshot)")
        self.capture_market_data_checkbox.setChecked(True)
        self.capture_market_data_checkbox.setToolTip(
            "Store a snapshot in the database for later analysis: the category table for "
            "ESPRESSO/NCL, or the offer-code comparison for GoCCL."
        )
        queue_layout.addWidget(self.capture_market_data_checkbox, 3, 0, 1, 2)

        self.capture_everything_checkbox = QCheckBox("Capture everything (full page HTML + network traffic)")
        self.capture_everything_checkbox.setToolTip(
            "For every page visited: save the full HTML, a best-effort structured extraction "
            "(tables + label/value pairs), and every network request/response — all read-only, "
            "written under data/pages/ and data/network_traffic.jsonl. Increases scan time and disk use."
        )
        queue_layout.addWidget(self.capture_everything_checkbox, 4, 0, 1, 2)

        self.remove_selected_button = QPushButton("Remove selected")
        self.remove_selected_button.clicked.connect(self._on_remove_selected)
        queue_layout.addWidget(self.remove_selected_button, 2, 2)

        self.clear_queue_button = QPushButton("Clear queue")
        self.clear_queue_button.clicked.connect(self._on_clear_queue)
        queue_layout.addWidget(self.clear_queue_button, 3, 2)

        layout.addLayout(queue_layout)

        layout.addWidget(QLabel("Activity log (every automated browser action):"))
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setFixedHeight(120)
        self.activity_log.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        layout.addWidget(self.activity_log)

        self.queue_status_label = QLabel("0 pending, 0 running")
        self.queue_status_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.queue_status_label)

        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.queue_list)

        self.results_table = QTableWidget(0, 4)
        # Columns 3/4 are repurposed for MSC rows (see _append_msc_result_row)
        # since MSC's four-independent-check result doesn't have a single
        # net_saving/confidence figure the way ESPRESSO/NCL/GoCCL do.
        self.results_table.setHorizontalHeaderLabels([
            "Booking ID", "Status", "Net Saving / Summary", "Confidence / Checks",
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setSortingEnabled(True)
        layout.addWidget(self.results_table)

        bottom_layout = QGridLayout()
        bottom_layout.setHorizontalSpacing(12)
        bottom_layout.setVerticalSpacing(10)

        self.export_button = QPushButton("Export report")
        self.export_button.clicked.connect(self._on_export)
        bottom_layout.addWidget(self.export_button, 0, 0)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #666666;")
        bottom_layout.addWidget(self.status_label, 0, 1)

        layout.addLayout(bottom_layout)

        self.setCentralWidget(container)

    def _refresh_summary(self) -> None:
        total = len(self.results)
        optimizations = sum(1 for r in self.results if r.status.value == "OPTIMIZATION")
        upgrades = sum(1 for r in self.results if r.status.value == "UPGRADE_AVAILABLE")
        traps = sum(1 for r in self.results if r.status.value == "TRAP")
        # Only OPTIMIZATION rows represent savings actually recommended.
        # NO_SAVING rows carry a negative net_saving to mean "repricing
        # would cost more, so we didn't recommend it" — summing those in
        # would make "Total savings" go deeply negative even when real
        # optimizations were found. Matches the CLI's own summary (main.py).
        savings = total_optimization_savings(self.results)
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
        print("GUI: _on_login_check entered")
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

        cruise_line = CruiseLine(self.cruise_line_selector.currentText())
        try:
            print("GUI: calling check_login")
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
                logged_in = await self.queue_manager.check_login(cruise_line, timeout_minutes=15.0)
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

    @Slot(str)
    def _on_cruise_line_changed(self, new_value: str) -> None:
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
        print("GUI: _on_start entered")
        snapshot = self.queue_manager.get_snapshot()
        print(f"GUI: start snapshot queued={snapshot.queued} running={snapshot.running} done={snapshot.done} error={snapshot.error}")
        if snapshot.queued == 0:
            QMessageBox.warning(self, "No bookings", "Add at least one booking ID before starting the queue.")
            return

        cruise_line_check = CruiseLine(self.cruise_line_selector.currentText())
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
        if not has_session or self._login_ok_for != cruise_line_check:
            QMessageBox.warning(
                self, "Not logged in",
                "Click \"Check login\" first and complete the login in the browser "
                "window that opens, then click Start.\n\n"
                f"(No confirmed login for {cruise_line_check.value} in this session. "
                "A browser being open is not the same as being logged in — starting "
                "anyway would run every booking against a logged-out portal.)",
            )
            return

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.clear_queue_button.setEnabled(False)
        self.add_booking_button.setEnabled(False)
        self.add_bulk_button.setEnabled(False)
        self.booking_input.setEnabled(False)
        self.bulk_input.setEnabled(False)
        # Running "Check login" while a scan is active opens a second,
        # separate browser session — ESPRESSO appears to only allow one
        # active session per account, so that second login can knock the
        # scan's already-running session out from under it, cascading
        # into timeouts for every booking still queued.
        self.login_button.setEnabled(False)
        self.status_label.setText("Starting queue processing...")

        cruise_line = CruiseLine(self.cruise_line_selector.currentText())
        force_live_recheck = self.force_recheck_checkbox.isChecked()
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
            print("GUI: _on_start exception:", exc)
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
        def on_state_change(snapshot) -> None:
            self._update_queue_view(snapshot)

        def on_result(result: BookingResult) -> None:
            self.results.append(result)
            self._append_result_row(result)
            self._refresh_summary()

        print("GUI: invoking queue_manager.start_processing")
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
        job_status = self.queue_manager.last_job_status
        snapshot = self.queue_manager.get_snapshot()
        remaining = snapshot.queued + snapshot.running
        logger.info("gui.batch_finished", job_status=job_status, remaining=remaining)
        if job_status == "FAILED":
            message = (
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

        print("GUI: invoking msc_service.run_batch")
        await self.msc_service.run_batch(booking_ids, on_result=on_result, on_progress=on_progress)
        print("GUI: msc_service.run_batch completed")
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
        self.activity_log.append(f"[{ts}] {action}  {detail_str}")

    @staticmethod
    def _format_net_saving(net_saving: float, status: str = "") -> str:
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
        if status in ("TRAP", "NO_SAVING") and net_saving > 0:
            return f"${net_saving:.2f} (not recommended — see status)"
        if net_saving > 0:
            return f"-${net_saving:.2f} saved"
        if net_saving < 0:
            return f"+${abs(net_saving):.2f} more expensive"
        return "$0.00"

    def _append_result_row(self, result: BookingResult) -> None:
        # Sorting must be off while inserting: with it enabled, each
        # setItem() call can trigger an immediate re-sort mid-insert and
        # scatter this row's cells across different rows.
        self.results_table.setSortingEnabled(False)
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self.results_table.setItem(row, 0, QTableWidgetItem(result.booking_id))
        self.results_table.setItem(row, 1, QTableWidgetItem(result.status.value))
        self.results_table.setItem(row, 2, QTableWidgetItem(self._format_net_saving(result.net_saving, result.status.value)))
        self.results_table.setItem(row, 3, QTableWidgetItem(str(result.confidence)))
        color = self._color_for_status(result.status.value)
        for col in range(4):
            item = self.results_table.item(row, col)
            if item is not None:
                item.setBackground(color)
        self.results_table.setSortingEnabled(True)

    def _append_msc_result_row(self, outcome: MscCheckOutcome) -> None:
        """MSC counterpart to _append_result_row — outcome.result is an
        MscBookingResult (four independent checks) rather than the single
        old_total/new_total/net_saving BookingResult shape, when
        outcome.status == "checked"; otherwise it's a short-circuit status
        (not_found, cancelled, session_expired_after_relogin, etc.) with no
        result to show."""
        self.results_table.setSortingEnabled(False)
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        if outcome.status == "checked" and outcome.result is not None:
            status_text = "OPPORTUNITY" if outcome.result.has_any_opportunity else "NO_OPPORTUNITY"
            checks_text = f"{sum(1 for c in outcome.result.checks if c.status.value == 'OPPORTUNITY')}/{len(outcome.result.checks)} checks"
        else:
            status_text = outcome.status.upper()
            checks_text = "-"
        self.results_table.setItem(row, 0, QTableWidgetItem(outcome.booking_id))
        self.results_table.setItem(row, 1, QTableWidgetItem(status_text))
        self.results_table.setItem(row, 2, QTableWidgetItem(outcome.note))
        self.results_table.setItem(row, 3, QTableWidgetItem(checks_text))
        color = self._color_for_status(status_text)
        for col in range(4):
            item = self.results_table.item(row, col)
            if item is not None:
                item.setBackground(color)
        self.results_table.setSortingEnabled(True)

    def _update_queue_view(self, snapshot) -> None:
        self.queue_status_label.setText(f"{snapshot.queued} pending, {snapshot.running} running")
        self.queue_list.clear()
        for item in snapshot.items:
            widget = QWidget()
            widget_layout = QHBoxLayout(widget)
            widget_layout.setContentsMargins(4, 2, 4, 2)
            label = QLabel(f"{item.booking_id} [{item.status.value}]")
            label.setMinimumWidth(320)
            widget_layout.addWidget(label)
            if item.status == QueueStatus.QUEUED:
                remove_button = QPushButton("x")
                remove_button.setFixedSize(24, 24)
                remove_button.clicked.connect(lambda _, bid=item.booking_id: self._remove_queue_item(bid))
                widget_layout.addWidget(remove_button)
            list_item = QListWidgetItem(self.queue_list)
            list_item.setData(Qt.UserRole, item.booking_id)
            list_item.setSizeHint(widget.sizeHint())
            self.queue_list.addItem(list_item)
            self.queue_list.setItemWidget(list_item, widget)

    def _populate_results_table(self) -> None:
        self.results_table.setRowCount(0)
        for result in self.results:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            self.results_table.setItem(row, 0, QTableWidgetItem(result.booking_id))
            self.results_table.setItem(row, 1, QTableWidgetItem(result.status.value))
            self.results_table.setItem(row, 2, QTableWidgetItem(self._format_net_saving(result.net_saving, result.status.value)))
            self.results_table.setItem(row, 3, QTableWidgetItem(str(result.confidence)))
            color = self._color_for_status(result.status.value)
            for col in range(4):
                item = self.results_table.item(row, col)
                if item is not None:
                    item.setBackground(color)

    def _color_for_status(self, status: str) -> Qt.GlobalColor:
        if status == "OPTIMIZATION":
            return Qt.green
        # Deliberately distinct from OPTIMIZATION: a category upgrade is a
        # different physical room/deck, always needs human review before
        # switching — never the same one-click confidence as a confirmed
        # same-category win.
        if status == "UPGRADE_AVAILABLE":
            return Qt.cyan
        if status == "TRAP":
            return Qt.red
        if status == "NO_SAVING":
            return Qt.yellow
        if status == "ERROR":
            return Qt.lightGray
        # MSC statuses (see _append_msc_result_row) — OPPORTUNITY here means
        # a finding to call MSC about, never a same-category win the agent
        # applied itself the way ESPRESSO's OPTIMIZATION does.
        if status == "OPPORTUNITY":
            return Qt.green
        if status == "NO_OPPORTUNITY":
            return Qt.white
        if status in ("NOT_FOUND", "SESSION_EXPIRED_AFTER_RELOGIN", "CONFIRM_BUTTON_NOT_FOUND"):
            return Qt.lightGray
        return Qt.white

    @Slot()
    def _on_export(self) -> None:
        if not self.results and not self.msc_results:
            QMessageBox.information(self, "Nothing to export", "Run a scan first to export results.")
            return

        export_dir = Path("reports")
        export_dir.mkdir(exist_ok=True)
        saved = []

        if self.results:
            csv_path = export_dir / "scan_results.csv"
            xlsx_path = export_dir / "scan_results.xlsx"
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
                msc_csv_path = export_dir / "msc_scan_results.csv"
                self.adapter.export_msc_csv(msc_checked, str(msc_csv_path))
                saved.append(str(msc_csv_path))

        QMessageBox.information(self, "Export complete", "Saved:\n" + "\n".join(saved))
        self.status_label.setText(f"Exported to {export_dir}")
