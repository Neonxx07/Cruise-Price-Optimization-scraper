"""Queue manager for GUI-driven booking scans."""

from __future__ import annotations

import asyncio
import pathlib
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.models import BookingResult, BookingStatus, CruiseLine
from services.booking_service import BookingService
from models.database import init_db
from utils.logging import get_logger

logger = get_logger(__name__)


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
    # ADDED 2026-08-27: the backend already hands _on_progress a ScanJob
    # carrying current_booking_id / progress_done / progress_total, but
    # only current_booking_id was used (to flip a row's status) and the
    # counts were discarded — so the standard path showed no progress at
    # all while the MSC path already showed "checking X (3/26)".
    current_booking_id: str | None = None
    progress_done: int = 0
    progress_total: int = 0


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

    @property
    def last_job_status(self) -> str | None:
        """Terminal status of the most recent job, or None if none ran.

        ADDED 2026-08-26: the GUI had NO way to read this — `_job` is
        private and nothing outside this class touched it — so all three
        terminal states (COMPLETED, FAILED, STOPPED) were reported
        identically as "Queue processing complete." The FAILED case is the
        damaging one: `_run_batch` marks a job FAILED and breaks out of the
        loop when a browser restart fails, leaving every remaining booking
        unscanned, while the GUI told the operator the whole watchlist was
        repriced."""
        return self._job.status.value if self._job else None

    @property
    def last_job_error(self) -> str | None:
        """WHY the most recent job failed, if it recorded a reason.

        ADDED 2026-08-27 together with ScanJob.error: `last_job_status`
        told the GUI a job FAILED but not why, so the operator saw a bare
        "SCAN FAILED" and had to open the log to find out whether the
        browser died, the session logged out, or a restart failed."""
        return getattr(self._job, "error", None) if self._job else None

    def has_live_session(self, cruise_line: CruiseLine) -> bool:
        return self._service.has_live_session(cruise_line)

    def get_snapshot(self) -> QueueSnapshot:
        items = list(self._queue)
        queued = sum(1 for item in items if item.status == QueueStatus.QUEUED)
        running = sum(1 for item in items if item.status == QueueStatus.RUNNING)
        done = sum(1 for item in items if item.status == QueueStatus.DONE)
        error = sum(1 for item in items if item.status == QueueStatus.ERROR)
        job = self._job
        return QueueSnapshot(
            items=items, queued=queued, running=running, done=done, error=error,
            current_booking_id=getattr(job, "current_booking_id", None) if job else None,
            progress_done=getattr(job, "progress_done", 0) if job else 0,
            progress_total=getattr(job, "progress_total", 0) if job else 0,
        )

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

    def add_bookings_from_file(self, path: str) -> tuple[list[str], str | None]:
        """Load booking IDs from a text file (one per line, or comma
        separated) and queue them. Returns (added_ids, error_message).

        ADDED 2026-08-27: the GUI read NO watchlist file at all — the queue
        could only be filled by typing or pasting — while `main.py` and
        `run_persistent_watchlist_scan.py` both work from watchlist files.
        Neon hit this directly: he put NCL bookings in `Watchlistncl.txt`,
        pressed Start, and nothing ran, because the desktop app never looks
        at that file. Returns the error instead of raising so the caller can
        show it without a traceback."""
        try:
            raw = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [], f"Could not read {path}: {exc}"
        # An EMPTY file is called out explicitly rather than reported as
        # "0 new bookings" — Neon's Watchlistncl.txt was 0 bytes and the
        # silent no-op was indistinguishable from a broken Start button.
        if not raw.strip():
            return [], f"{pathlib.Path(path).name} is empty — it contains no booking IDs."
        added = self.add_bookings_bulk(raw)
        if not added:
            return [], (
                f"{pathlib.Path(path).name} has content but no NEW booking IDs "
                f"(they may all be queued already)."
            )
        return added, None

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

    async def check_login(self, cruise_line: CruiseLine, timeout_minutes: float = 15.0,
                          headless: bool = False) -> bool:
        """Log in via the shared, continuous browser session (see
        BookingService.check_login) — the same instance stays open for
        start_processing to reuse afterward.

        `headless` therefore decides how the SCAN runs too, not just the
        login: the GUI passes keep_browser_open=True, so every booking is
        checked in the browser this call opens.
        """
        await self.initialize()
        return await self._service.check_login(
            cruise_line, timeout_minutes=timeout_minutes, headless=headless)

    async def close_live_session(self) -> None:
        """Close the shared browser session, if one is open. Call on app exit."""
        await self._service.close_live_scraper()

    async def start_processing(
        self,
        cruise_line: CruiseLine,
        on_state_change: StateCallback | None = None,
        on_result: ResultCallback | None = None,
        raw_dump_dir: str | None = None,
        force_live_recheck: bool = False,
        capture_market_data: bool = False,
        capture_everything: bool = False,
        on_action: Callable[[dict], None] | None = None,
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
            capture_everything=capture_everything,
            on_action=on_action,
            # The GUI keeps one continuous browser session alive across
            # login + every scan — see BookingService.get_or_create_scraper.
            keep_browser_open=True,
        )
        self._current_job_id = self._job.job_id
        self._on_state_change = on_state_change

        if on_state_change:
            on_state_change(self.get_snapshot())

        seen_booking_ids: set[str] = set()
        # Whether the stop has already been relayed to BookingService for
        # this run - see the loop below.
        stop_sent = False
        try:
            while self._job.status.value in ("PENDING", "RUNNING"):
                self._sync_completed_results(on_result, on_state_change, seen_booking_ids)
                await asyncio.sleep(0.5)
                # RELAY THE STOP ONCE, not on every poll.
                #
                # CONFIRMED BUG, fixed 2026-09-22 and caught by the log file
                # added the day before. `_stop_requested` stays set until the
                # `finally` below runs, so this condition was true on EVERY
                # 0.5s tick for as long as the batch took to wind down -
                # re-calling stop_scan and emitting "batch.stop_requested"
                # twice a second. A real run logged 28 identical lines in 14
                # seconds before the process exited.
                #
                # The batch deliberately finishes its current booking before
                # stopping, so that window is normal and can be long. The
                # flag is a request; once relayed, the service owns it.
                if self._stop_requested and self._current_job_id and not stop_sent:
                    await self._service.stop_scan(self._current_job_id)
                    stop_sent = True
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

    def mark_running(self, booking_id: str) -> None:
        """Mark one queued item RUNNING without going through
        start_processing/BookingService — used by the GUI's MSC path
        (see msc_live_service.py), which drives its own batch loop instead
        of BookingService.start_scan but still wants this same queue list
        to reflect progress."""
        item = self._find_item(booking_id)
        if item is not None:
            item.status = QueueStatus.RUNNING

    def mark_done(self, booking_id: str, is_error: bool = False) -> None:
        """Mark one queued item DONE/ERROR — the MSC-path counterpart to
        mark_running above."""
        item = self._find_item(booking_id)
        if item is not None:
            item.status = QueueStatus.ERROR if is_error else QueueStatus.DONE

    def _on_progress(self, job) -> None:
        # Marks the in-flight booking RUNNING when we can find its row, but
        # ALWAYS notifies — previously an early `return` here meant a
        # progress update for a booking not in the queue list (a cache
        # skip, or a row the operator removed) dropped the whole update,
        # so the counters silently stalled.
        if job.current_booking_id:
            item = self._find_item(job.current_booking_id)
            if item is not None:
                item.status = QueueStatus.RUNNING
        if self._on_state_change:
            try:
                self._on_state_change(self.get_snapshot())
            except Exception:
                logger.exception("gui.on_progress_callback_failed")

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
            # CONFIRMED CRITICAL BUG, fixed 2026-08-26: these two callbacks
            # used to be invoked BARE. They run GUI code (_append_result_row /
            # _refresh_summary / _update_queue_view), and if any of it raised,
            # the exception propagated out of here → out of start_processing's
            # poll loop → its `finally` cleared `_running` and
            # `_current_job_id` → the GUI showed "Processing failed" and
            # RE-ENABLED Start. Meanwhile `_run_batch` is a detached asyncio
            # Task still driving the live browser: clicking Start again then
            # ran TWO concurrent batches on one Playwright page, and because
            # `_current_job_id` was cleared, Stop could no longer stop the
            # orphaned job at all.
            #
            # BookingService already wraps its own `on_progress` callback for
            # exactly this reason, and BaseScraper.log_action wraps
            # `on_action` — this was the one remaining unguarded callback
            # boundary. A display failure must never take down the scan.
            if on_result:
                try:
                    on_result(result)
                except Exception:
                    logger.exception("gui.on_result_callback_failed", booking_id=result.booking_id)
            if on_state_change:
                try:
                    on_state_change(self.get_snapshot())
                except Exception:
                    logger.exception("gui.on_state_change_callback_failed")

    def _find_item(self, booking_id: str) -> QueueItem | None:
        normalized = self._normalize_id(booking_id)
        return next((item for item in self._queue if self._normalize_id(item.booking_id) == normalized), None)

    @staticmethod
    def _normalize_id(booking_id: str) -> str:
        return booking_id.strip()

    @staticmethod
    def _parse_bulk_text(text: str) -> list[str]:
        normalized = text.replace(",", "\n")
        lines = [line.strip() for line in normalized.splitlines()]
        return [line for line in lines if line]
