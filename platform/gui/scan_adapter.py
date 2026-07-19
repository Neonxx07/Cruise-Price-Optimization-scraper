"""Adapter between the GUI and BookingService business logic."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.models import BookingResult, CruiseLine, ScanJob
from models.database import init_db
from services.booking_service import BookingService
from services.csv_export import export_results_csv
from services.excel_export import export_results_excel


ProgressCallback = Callable[[ScanJob], None]


class GuiScanAdapter:
    """Wraps BookingService for use by the desktop GUI."""

    def __init__(self) -> None:
        self.service = BookingService()
        self.job: ScanJob | None = None

    async def initialize(self) -> None:
        """Prepare the database and any persistence required by the GUI."""
        await init_db()

    async def start_scan(
        self,
        booking_ids: list[str],
        cruise_line: CruiseLine,
        on_progress: ProgressCallback | None = None,
        raw_dump_dir: str | None = None,
    ) -> ScanJob:
        """Start scanning booking IDs and return the scan job."""
        self.job = await self.service.start_scan(
            booking_ids,
            cruise_line,
            on_progress=on_progress,
            bypass_cache=True,
            raw_dump_dir=raw_dump_dir,
        )
        return self.job

    def get_current_job(self) -> ScanJob | None:
        return self.job

    def export_csv(self, results: list[BookingResult], path: str) -> None:
        """Export scan results to a CSV file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        csv_content = export_results_csv(results)
        Path(path).write_text(csv_content, encoding="utf-8")

    def export_excel(self, results: list[BookingResult], path: str) -> None:
        """Export scan results to an Excel file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        export_results_excel(results, path)
