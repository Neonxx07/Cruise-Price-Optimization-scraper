"""Queue manager for GUI-driven booking scans."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.models import BookingResult, BookingStatus, CruiseLine
from services.booking_service import BookingService
from models.database import init_db


class QueueStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ERROR = "ERROR"


@dataclass
class QueueItem:
    booking_id: str
    status: QueueStatus = QueueStatus.QUEUED


@dataclass
class QueueSnapshot:
    items: list[QueueItem]
    queued: int
    running: int
    done: int
    error: int


StateCallback = Callable[[QueueSnapshot], None]
ResultCallback = Callable[[BookingResult], None]


class BookingQueueManager:
    """Manage a GUI queue of booking IDs and sequential scan state."""

    def __init__(self) -> None:
        self._service = BookingService()
        self._queue: deque[QueueItem] = deque()
        self._results: list[BookingResult] = []
        self._running: bool = False
        self._stop_requested: bool = False
        self._current_job_id: str | None = None
        self._job = None
        self._on_state_change: StateCallback | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def get_snapshot(self) -> QueueSnapshot:
        items = list(self._queue)
        queued = sum(1 for item in items if item.status == QueueStatus.QUEUED)
        running = sum(1 for item in items if item.status == QueueStatus.RUNNING)
        done = sum(1 for item in items if item.status == QueueStatus.DONE)
        error = sum(1 for item in items if item.status == QueueStatus.ERROR)
        return QueueSnapshot(items=items, queued=queued, running=running, done=done, error=error)

    def add_booking(self, booking_id: str) -> bool:
        booking_id = self._normalize_id(booking_id)
        if not booking_id or self._find_item(booking_id) is not None:
            return False
        self._queue.append(QueueItem(booking_id=booking_id))
        return True

    def add_bookings_bulk(self, text: str) -> list[str]:
        booking_ids = self._parse_bulk_text(text)
        added: list[str] = []
        for booking_id in booking_ids:
            if self.add_booking(booking_id):
                added.append(booking_id)
        return added

    def remove_booking(self, booking_id: str) -> bool:
        booking_id = self._normalize_id(booking_id)
        item = self._find_item(booking_id)
        if item is None or item.status != QueueStatus.QUEUED:
            return False
        self._queue.remove(item)
        return True

    def clear_queue(self) -> bool:
        if self._running:
            return False
        self._queue.clear()
        self._results.clear()
        return True

    async def initialize(self) -> None:
        await init_db()

    async def start_processing(
        self,
        cruise_line: CruiseLine,
        on_state_change: StateCallback | None = None,
        on_result: ResultCallback | None = None,
        raw_dump_dir: str | None = None,
        force_live_recheck: bool = False,
        capture_market_data: bool = False,
    ) -> None:
        if self._running:
            raise RuntimeError("Scan queue is already running")

        queued_items = [item for item in self._queue if item.status == QueueStatus.QUEUED]
        if not queued_items:
            raise ValueError("No queued booking IDs to process")

        self._running = True
        self._stop_requested = False
        await self.initialize()

        booking_ids = [item.booking_id for item in queued_items]
        self._job = await self._service.start_scan(
            booking_ids,
            cruise_line,
            on_progress=self._on_progress,
            bypass_cache=force_live_recheck,
            raw_dump_dir=raw_dump_dir,
            capture_market_data=capture_market_data,
        )
        self._current_job_id = self._job.job_id
        self._on_state_change = on_state_change

        if on_state_change:
            on_state_change(self.get_snapshot())

        seen_booking_ids: set[str] = set()
        try:
            while self._job.status.value in ("PENDING", "RUNNING"):
                self._sync_completed_results(on_result, on_state_change, seen_booking_ids)
                await asyncio.sleep(0.5)
                if self._stop_requested and self._current_job_id:
                    await self._service.stop_scan(self._current_job_id)
            self._sync_completed_results(on_result, on_state_change, seen_booking_ids)
        finally:
            self._running = False
            self._stop_requested = False
            self._current_job_id = None
            if on_state_change:
                on_state_change(self.get_snapshot())

    def stop_processing(self) -> bool:
        if not self._running:
            return False
        self._stop_requested = True
        return True

    def _on_progress(self, job) -> None:
        if not job.current_booking_id:
            return
        item = self._find_item(job.current_booking_id)
        if item is None:
            return
        item.status = QueueStatus.RUNNING
        if self._on_state_change:
            self._on_state_change(self.get_snapshot())

    def _sync_completed_results(
        self,
        on_result: ResultCallback | None,
        on_state_change: StateCallback | None,
        seen_booking_ids: set[str],
    ) -> None:
        if not self._job:
            return
        for result in self._job.results:
            if result.booking_id in seen_booking_ids:
                continue
            seen_booking_ids.add(result.booking_id)
            item = self._find_item(result.booking_id)
            if item is None:
                continue
            item.status = QueueStatus.ERROR if result.status == BookingStatus.ERROR else QueueStatus.DONE
            self._results.append(result)
            if on_result:
                on_result(result)
            if on_state_change:
                on_state_change(self.get_snapshot())

    def _find_item(self, booking_id: str) -> QueueItem | None:
        normalized = self._normalize_id(booking_id)
        return next((item for item in self._queue if self._normalize_id(item.booking_id) == normalized), None)

    @staticmethod
    def _normalize_id(booking_id: str) -> str:
        return booking_id.strip()

    @staticmethod
    def _parse_bulk_text(text: str) -> list[str]:
        separators = ["\n", ","]
        normalized = text.replace(",", "\n")
        lines = [line.strip() for line in normalized.splitlines()]
        return [line for line in lines if line]
