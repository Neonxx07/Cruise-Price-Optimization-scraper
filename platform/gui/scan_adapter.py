"""Result export adapter for the desktop GUI."""

from __future__ import annotations

from pathlib import Path

from core.models import BookingResult, MscBookingResult
from services.csv_export import export_msc_results_csv, export_results_csv
from services.excel_export import export_results_excel


class GuiScanAdapter:
    """Wraps result export for use by the desktop GUI. Scanning itself is
    driven directly through BookingQueueManager (see queue_manager.py) for
    ESPRESSO/NCL/GoCCL, or MscLiveService (see msc_live_service.py) for MSC —
    not through this adapter."""

    def export_csv(self, results: list[BookingResult], path: str) -> None:
        """Export scan results to a CSV file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        csv_content = export_results_csv(results)
        Path(path).write_text(csv_content, encoding="utf-8")

    def export_excel(self, results: list[BookingResult], path: str) -> None:
        """Export scan results to an Excel file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        export_results_excel(results, path)

    def export_msc_csv(self, results: list[MscBookingResult], path: str) -> None:
        """Export MSC booking results to a CSV file — see
        export_msc_results_csv's docstring for why MSC needs its own
        column layout instead of export_csv's."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        csv_content = export_msc_results_csv(results)
        Path(path).write_text(csv_content, encoding="utf-8")
