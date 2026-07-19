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

from core.models import BookingResult, CruiseLine
from gui.queue_manager import BookingQueueManager, QueueStatus
from gui.scan_adapter import GuiScanAdapter
from main import _run_login_check


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CruiseIntel Desktop Scanner")
        self.setMinimumSize(980, 680)

        self.adapter = GuiScanAdapter()
        self.queue_manager = BookingQueueManager()
        self.results: list[BookingResult] = []

        self._build_ui()
        self._refresh_summary()
        self._update_queue_view(self.queue_manager.get_snapshot())

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
        self.cruise_line_selector.addItems([c.value for c in CruiseLine])
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

        self.capture_market_data_checkbox = QCheckBox("Collect market data (ESPRESSO category snapshot)")
        self.capture_market_data_checkbox.setChecked(True)
        self.capture_market_data_checkbox.setToolTip(
            "Store the ESPRESSO category table snapshot in the database for later analysis."
        )
        queue_layout.addWidget(self.capture_market_data_checkbox, 3, 0, 1, 2)

        self.clear_queue_button = QPushButton("Clear queue")
        self.clear_queue_button.clicked.connect(self._on_clear_queue)
        queue_layout.addWidget(self.clear_queue_button, 2, 2)

        layout.addLayout(queue_layout)

        self.queue_status_label = QLabel("0 pending, 0 running")
        self.queue_status_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.queue_status_label)

        self.queue_list = QListWidget()
        layout.addWidget(self.queue_list)

        self.results_table = QTableWidget(0, 4)
        self.results_table.setHorizontalHeaderLabels([
            "Booking ID", "Status", "Net Saving", "Confidence",
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setAlternatingRowColors(True)
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
        traps = sum(1 for r in self.results if r.status.value == "TRAP")
        savings = sum(r.net_saving for r in self.results)
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

        args = type(
            "Args",
            (),
            {"cruise_line": "ESPRESSO", "timeout_minutes": 15.0, "headless": False},
        )
        try:
            print("GUI: calling _run_login_check")
            await _run_login_check(args)
            print("GUI: _run_login_check returned successfully")
            self.login_status_label.setText("Login status: last checked OK")
            self.status_label.setText("Login check complete.")
        except Exception as exc:
            print("GUI: _on_login_check exception:", exc)
            traceback.print_exc()
            self.login_status_label.setText("Login status: failed")
            self.status_label.setText(f"Login check failed: {exc}")
            QMessageBox.warning(self, "Login failed", str(exc))

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

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.clear_queue_button.setEnabled(False)
        self.add_booking_button.setEnabled(False)
        self.add_bulk_button.setEnabled(False)
        self.booking_input.setEnabled(False)
        self.bulk_input.setEnabled(False)
        self.status_label.setText("Starting queue processing...")

        cruise_line = CruiseLine(self.cruise_line_selector.currentText())
        force_live_recheck = self.force_recheck_checkbox.isChecked()
        capture_market_data = self.capture_market_data_checkbox.isChecked()

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
            self._update_queue_view(self.queue_manager.get_snapshot())

    @Slot()
    def _on_stop(self) -> None:
        if self.queue_manager.stop_processing():
            self.status_label.setText("Stop requested. Waiting for current booking to finish...")
        else:
            self.status_label.setText("No active queue to stop.")

    def _append_result_row(self, result: BookingResult) -> None:
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        self.results_table.setItem(row, 0, QTableWidgetItem(result.booking_id))
        self.results_table.setItem(row, 1, QTableWidgetItem(result.status.value))
        self.results_table.setItem(row, 2, QTableWidgetItem(f"${result.net_saving:.2f}"))
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
            self.results_table.setItem(row, 2, QTableWidgetItem(f"${result.net_saving:.2f}"))
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
