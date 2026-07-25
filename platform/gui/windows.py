"""Main desktop window for CruiseIntel GUI."""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from core.models import BookingResult, CruiseLine
from gui.queue_manager import BookingQueueManager, QueueStatus
from gui.scan_adapter import GuiScanAdapter


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CruiseIntel Desktop Scanner")
        self.setMinimumSize(980, 680)

        self.adapter = GuiScanAdapter()
        self.queue_manager = BookingQueueManager()
        self.results: list[BookingResult] = []
        self._shutting_down = False

        self._build_ui()
        self._refresh_summary()
        self._update_queue_view(self.queue_manager.get_snapshot())

    def closeEvent(self, event) -> None:
        """Close the live browser session (saving its final state) before
        the app actually exits, instead of leaving it dangling."""
        if self._shutting_down:
            event.accept()
            return
        event.ignore()
        self._shutting_down = True
        self.status_label.setText("Closing browser session...")
        asyncio.ensure_future(self._shutdown_and_close())

    async def _shutdown_and_close(self) -> None:
        try:
            await self.queue_manager.close_live_session()
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

        # Two-pane layout: left = booking entry / queue / scraping log,
        # right = controls / results — matches the intended screen split
        # rather than one long stacked column.
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        bottom_layout = QHBoxLayout()
        self.export_button = QPushButton("Export report")
        self.export_button.clicked.connect(self._on_export)
        bottom_layout.addWidget(self.export_button)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #666666;")
        bottom_layout.addWidget(self.status_label, 1)

        layout.addLayout(bottom_layout)

        self.setCentralWidget(container)

    def _build_left_pane(self) -> QWidget:
        """Booking entry, the queued booking list, and the scraping activity log."""
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        booking_group = QGroupBox("Booking")
        booking_layout = QGridLayout(booking_group)
        booking_layout.setHorizontalSpacing(8)
        booking_layout.setVerticalSpacing(8)

        booking_layout.addWidget(QLabel("Booking ID:"), 0, 0)
        self.booking_input = QLineEdit()
        self.booking_input.returnPressed.connect(self._add_booking)
        booking_layout.addWidget(self.booking_input, 0, 1)
        self.add_booking_button = QPushButton("Add to queue")
        self.add_booking_button.clicked.connect(self._add_booking)
        booking_layout.addWidget(self.add_booking_button, 0, 2)

        booking_layout.addWidget(QLabel("Bulk (comma or newline separated):"), 1, 0, 1, 3)
        self.bulk_input = QTextEdit()
        self.bulk_input.setFixedHeight(80)
        booking_layout.addWidget(self.bulk_input, 2, 0, 1, 3)
        self.add_bulk_button = QPushButton("Add list")
        self.add_bulk_button.clicked.connect(self._add_bulk)
        booking_layout.addWidget(self.add_bulk_button, 3, 2)

        layout.addWidget(booking_group)

        list_group = QGroupBox("Booking list")
        list_layout = QVBoxLayout(list_group)
        self.queue_status_label = QLabel("0 pending, 0 running")
        self.queue_status_label.setStyleSheet("font-weight: bold;")
        list_layout.addWidget(self.queue_status_label)
        self.queue_list = QListWidget()
        list_layout.addWidget(self.queue_list, 1)
        self.clear_queue_button = QPushButton("Clear queue")
        self.clear_queue_button.clicked.connect(self._on_clear_queue)
        list_layout.addWidget(self.clear_queue_button)

        layout.addWidget(list_group, 1)

        process_group = QGroupBox("The scraping process")
        process_layout = QVBoxLayout(process_group)
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        process_layout.addWidget(self.activity_log)

        layout.addWidget(process_group, 1)

        return pane

    def _build_right_pane(self) -> QWidget:
        """Start/stop/login controls and the check results."""
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        controls_group = QGroupBox("Start / Stop / Check login")
        controls_layout = QGridLayout(controls_group)
        controls_layout.setHorizontalSpacing(8)
        controls_layout.setVerticalSpacing(8)

        controls_layout.addWidget(QLabel("Cruise Line:"), 0, 0)
        self.cruise_line_selector = QComboBox()
        self.cruise_line_selector.addItems([c.value for c in CruiseLine])
        controls_layout.addWidget(self.cruise_line_selector, 0, 1)
        self.login_button = QPushButton("Check login")
        self.login_button.clicked.connect(self._on_login_check)
        controls_layout.addWidget(self.login_button, 0, 2)

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self._on_start)
        controls_layout.addWidget(self.start_button, 1, 0)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._on_stop)
        self.stop_button.setEnabled(False)
        controls_layout.addWidget(self.stop_button, 1, 1)

        self.force_recheck_checkbox = QCheckBox("Force live recheck")
        controls_layout.addWidget(self.force_recheck_checkbox, 2, 0, 1, 3)

        self.capture_market_data_checkbox = QCheckBox("Collect market data (category/offer-code snapshot)")
        self.capture_market_data_checkbox.setChecked(True)
        self.capture_market_data_checkbox.setToolTip(
            "Store the category/offer-code table snapshot (ESPRESSO category table, "
            "NCL category grid, or GoCCL offer-code comparison) in the database for later analysis."
        )
        controls_layout.addWidget(self.capture_market_data_checkbox, 3, 0, 1, 3)

        self.capture_everything_checkbox = QCheckBox("Capture everything (full page HTML + network traffic)")
        self.capture_everything_checkbox.setToolTip(
            "For every page visited: save the full HTML, a best-effort structured extraction "
            "(tables + label/value pairs), and every network request/response — all read-only, "
            "written under data/pages/ and data/network_traffic.jsonl. Increases scan time and disk use."
        )
        controls_layout.addWidget(self.capture_everything_checkbox, 4, 0, 1, 3)

        layout.addWidget(controls_group)

        results_group = QGroupBox("Booking after check — status, no saving / price difference")
        results_layout = QVBoxLayout(results_group)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setFont(QFont("Arial", 10, QFont.Bold))
        results_layout.addWidget(self.summary_label)

        self.results_table = QTableWidget(0, 4)
        self.results_table.setHorizontalHeaderLabels([
            "Booking ID", "Status", "Net Saving", "Confidence",
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setAlternatingRowColors(True)
        results_layout.addWidget(self.results_table, 1)

        layout.addWidget(results_group, 1)

        return pane

    def _refresh_summary(self) -> None:
        total = len(self.results)
        optimizations = sum(1 for r in self.results if r.status.value == "OPTIMIZATION")
        traps = sum(1 for r in self.results if r.status.value == "TRAP")
        # Only OPTIMIZATION rows represent savings actually recommended.
        # NO_SAVING rows carry a negative net_saving to mean "repricing
        # would cost more, so we didn't recommend it" — summing those in
        # would make "Total savings" go deeply negative even when real
        # optimizations were found. Matches the CLI's own summary (main.py).
        savings = sum(r.net_saving for r in self.results if r.status.value == "OPTIMIZATION")
        summary_text = (
            f"Bookings watched: {total}   "
            f"Optimizations: {optimizations}   "
            f"Traps: {traps}   "
            f"Total savings: ${savings:.2f}"
        )
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
        self.status_label.setText("Opening browser for login check...")
        self.login_status_label.setText("Login status: checking...")
        QApplication.processEvents()

        # Login and scanning now share one live browser page — running
        # both at once would mean two coroutines driving the same
        # Playwright Page concurrently, so Start is blocked until this
        # finishes (mirrors the existing login_button lock during scans).
        self.login_button.setEnabled(False)
        self.start_button.setEnabled(False)

        cruise_line = CruiseLine(self.cruise_line_selector.currentText())
        try:
            print("GUI: calling queue_manager.check_login")
            # Uses the same shared browser session that Start will reuse —
            # this window never closes and reopens the browser between
            # login and scanning (see get_or_create_scraper), which is
            # what avoids ESPRESSO's bot-detection flagging replayed
            # session cookies in a fresh browser instance.
            logged_in = await self.queue_manager.check_login(cruise_line, timeout_minutes=15.0)
            print("GUI: queue_manager.check_login returned", logged_in)
            if logged_in:
                self.login_status_label.setText("Login status: last checked OK")
                self.status_label.setText("Login check complete — browser stays open for scanning.")
            else:
                self.login_status_label.setText("Login status: timed out")
                self.status_label.setText("Login check timed out — please try again.")
        except Exception as exc:
            print("GUI: _on_login_check exception:", exc)
            traceback.print_exc()
            self.login_status_label.setText("Login status: failed")
            self.status_label.setText(f"Login check failed: {exc}")
            QMessageBox.warning(self, "Login failed", str(exc))
        finally:
            self.login_button.setEnabled(True)
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
    def _on_clear_queue(self) -> None:
        if not self.queue_manager.clear_queue():
            QMessageBox.warning(self, "Cannot clear", "Cannot clear the queue while a scan is running.")
            return
        self._update_queue_view(self.queue_manager.get_snapshot())
        self.status_label.setText("Queue cleared.")

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
        if not self.queue_manager.has_live_session(cruise_line_check):
            # Starting without a live session would fall back to a hidden
            # headless browser reusing whatever stale session is on disk —
            # exactly the failure mode this whole redesign exists to avoid.
            QMessageBox.warning(
                self, "Not logged in",
                "Click \"Check login\" first and complete the login in the browser "
                "window that opens, then click Start. Starting a scan without an "
                "active login session runs it hidden in the background and will fail.",
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

        def on_state_change(snapshot) -> None:
            self._update_queue_view(snapshot)

        def on_result(result: BookingResult) -> None:
            self.results.append(result)
            self._append_result_row(result)
            self._refresh_summary()

        try:
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
            print("GUI: queue_manager.start_processing completed")
            self.status_label.setText("Queue processing complete.")
        except Exception as exc:
            print("GUI: _on_start exception:", exc)
            traceback.print_exc()
            self.status_label.setText("Queue processing failed")
            QMessageBox.critical(self, "Processing failed", str(exc))
        finally:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.clear_queue_button.setEnabled(True)
            self.add_booking_button.setEnabled(True)
            self.add_bulk_button.setEnabled(True)
            self.booking_input.setEnabled(True)
            self.bulk_input.setEnabled(True)
            self.login_button.setEnabled(True)
            self._update_queue_view(self.queue_manager.get_snapshot())

    @Slot()
    def _on_stop(self) -> None:
        if self.queue_manager.stop_processing():
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
    def _format_net_saving(net_saving: float) -> str:
        """Spell out cost increases instead of a bare negative dollar
        figure ("$-459.00") that reads as ambiguous — a negative
        net_saving means repricing would cost more, not save less."""
        if net_saving > 0:
            return f"+${net_saving:.2f} saved"
        if net_saving < 0:
            return f"-${abs(net_saving):.2f} more expensive"
        return "$0.00"

    def _append_result_row(self, result: BookingResult) -> None:
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self.results_table.setItem(row, 0, QTableWidgetItem(result.booking_id))
        self.results_table.setItem(row, 1, QTableWidgetItem(result.status.value))
        self.results_table.setItem(row, 2, QTableWidgetItem(self._format_net_saving(result.net_saving)))
        self.results_table.setItem(row, 3, QTableWidgetItem(str(result.confidence)))
        color = self._color_for_status(result.status.value)
        for col in range(4):
            item = self.results_table.item(row, col)
            if item is not None:
                item.setBackground(color)

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
            self.results_table.setItem(row, 2, QTableWidgetItem(self._format_net_saving(result.net_saving)))
            self.results_table.setItem(row, 3, QTableWidgetItem(str(result.confidence)))
            color = self._color_for_status(result.status.value)
            for col in range(4):
                item = self.results_table.item(row, col)
                if item is not None:
                    item.setBackground(color)

    def _color_for_status(self, status: str) -> Qt.GlobalColor:
        if status == "OPTIMIZATION":
            return Qt.green
        if status == "TRAP":
            return Qt.red
        if status == "NO_SAVING":
            return Qt.yellow
        if status == "ERROR":
            return Qt.lightGray
        return Qt.white

    @Slot()
    def _on_export(self) -> None:
        if not self.results:
            QMessageBox.information(self, "Nothing to export", "Run a scan first to export results.")
            return

        export_dir = Path("reports")
        export_dir.mkdir(exist_ok=True)
        csv_path = export_dir / "scan_results.csv"
        xlsx_path = export_dir / "scan_results.xlsx"
        self.adapter.export_csv(self.results, str(csv_path))
        self.adapter.export_excel(self.results, str(xlsx_path))
        QMessageBox.information(self, "Export complete", f"Saved CSV and Excel to {export_dir}")
        self.status_label.setText(f"Exported to {export_dir}")
