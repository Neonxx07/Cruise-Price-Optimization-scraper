"""Booking service — orchestrates the full scan workflow.

This is the enterprise equivalent of background.js runBatch().
Manages the scraper lifecycle, result storage, caching, and progress tracking.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import AsyncGenerator, Callable

from sqlalchemy import select

from core.calculator import make_error_result, make_skipped_result
from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus
from models.database import BookingRecord, PriceHistory, ScanJobRecord, async_session
from scraper.base import BaseScraper
from scraper.espresso import EspressoScraper
from scraper.ncl import NclScraper
from services.cache_service import CacheService
from utils.logging import get_logger

logger = get_logger(__name__)


class BookingService:
    """
    Orchestrates booking scans: manages scraper lifecycle, caching,
    result persistence, and progress tracking.
    """

    def __init__(self):
        self.cache = CacheService()
        self._active_jobs: dict[str, ScanJob] = {}
        self._stop_flags: dict[str, bool] = {}

    def _get_scraper(self, cruise_line: CruiseLine) -> BaseScraper:
        """Factory: get the right scraper for the cruise line."""
        if cruise_line == CruiseLine.NCL:
            return NclScraper()
        return EspressoScraper()

    async def start_scan(
        self,
        booking_ids: list[str],
        cruise_line: CruiseLine,
        on_progress: Callable[[ScanJob], None] | None = None,
    ) -> ScanJob:
        """
        Start a batch scan of booking IDs.

        Args:
            booking_ids: List of booking IDs to check.
            cruise_line: Which cruise line portal to use.
            on_progress: Optional callback for progress updates.

        Returns:
            ScanJob with results populated as they complete.
        """
        job_id = str(uuid.uuid4())
        job = ScanJob(
            job_id=job_id,
            booking_ids=booking_ids,
            cruise_line=cruise_line,
            status=ScanJobStatus.RUNNING,
            progress_total=len(booking_ids),
            started_at=datetime.utcnow(),
        )
        self._active_jobs[job_id] = job
        self._stop_flags[job_id] = False

        # Save job to DB
        await self._save_job_to_db(job)

        # Run in background
        asyncio.create_task(self._run_batch(job, on_progress))

        return job

    async def _run_batch(
        self,
        job: ScanJob,
        on_progress: Callable[[ScanJob], None] | None = None,
    ) -> None:
        """Execute the batch scan."""
        scraper = self._get_scraper(job.cruise_line)

        try:
            await scraper.start()

            for i, booking_id in enumerate(job.booking_ids):
                if self._stop_flags.get(job.job_id):
                    job.status = ScanJobStatus.STOPPED
                    logger.info("batch.stopped", job_id=job.job_id, at_index=i)
                    break

                job.current_booking_id = booking_id
                job.progress_done = i

                # Smart cache check
                cached = await self.cache.get(job.cruise_line.value, booking_id)
                if cached:
                    logger.info("batch.cached", booking_id=booking_id, hours_ago=cached["hours_ago"])
                    result = make_skipped_result(
                        booking_id, None, job.cruise_line, cached["hours_ago"],
                    )
                    job.results.append(result)
                    job.progress_done = i + 1
                    if on_progress:
                        on_progress(job)
                    continue

                logger.info("batch.checking", booking_id=booking_id, index=i + 1, total=len(job.booking_ids))

                try:
                    result = await scraper.check_booking(booking_id)
                except Exception as e:
                    logger.error("batch.error", booking_id=booking_id, error=str(e))
                    result = make_error_result(booking_id, None, job.cruise_line, str(e))

                # Cache NO_SAVING results
                if result.status == BookingStatus.NO_SAVING:
                    await self.cache.set_no_saving(job.cruise_line.value, booking_id)

                job.results.append(result)
                job.progress_done = i + 1

                # Persist result
                await self._save_result_to_db(result)
                await self._save_price_history(result)

                if on_progress:
                    on_progress(job)

                # Small delay between bookings
                await asyncio.sleep(0.5)

            if job.status != ScanJobStatus.STOPPED:
                job.status = ScanJobStatus.COMPLETED

        except Exception as e:
            logger.error("batch.fatal", job_id=job.job_id, error=str(e))
            job.status = ScanJobStatus.FAILED

        finally:
            await scraper.stop()
            job.completed_at = datetime.utcnow()
            job.current_booking_id = None
            await self._update_job_in_db(job)
            self._stop_flags.pop(job.job_id, None)
            logger.info(
                "batch.complete",
                job_id=job.job_id,
                status=job.status.value,
                total=len(job.results),
            )

    async def stop_scan(self, job_id: str) -> bool:
        """Signal a running scan to stop after the current booking."""
        if job_id in self._stop_flags:
            self._stop_flags[job_id] = True
            logger.info("batch.stop_requested", job_id=job_id)
            return True
        return False

    def get_job(self, job_id: str) -> ScanJob | None:
        """Get a scan job by ID (in-memory)."""
        return self._active_jobs.get(job_id)

    async def get_all_bookings(
        self, cruise_line: str | None = None, limit: int = 100,
    ) -> list[dict]:
        """Fetch all booking records from the database."""
        async with async_session() as session:
            query = select(BookingRecord).order_by(BookingRecord.created_at.desc()).limit(limit)
            if cruise_line:
                query = query.where(BookingRecord.cruise_line == cruise_line)
            result = await session.execute(query)
            records = result.scalars().all()
            return [
                {
                    "booking_id": r.booking_id,
                    "cruise_line": r.cruise_line,
                    "status": r.status,
                    "net_saving": r.net_saving,
                    "old_total": r.old_total,
                    "new_total": r.new_total,
                    "confidence": r.confidence,
                    "price_category": r.price_category,
                    "new_price_category": r.new_price_category,
                    "note": r.note,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    async def get_price_history(self, booking_id: str) -> list[dict]:
        """Fetch price history for a booking."""
        async with async_session() as session:
            result = await session.execute(
                select(PriceHistory)
                .where(PriceHistory.booking_id == booking_id)
                .order_by(PriceHistory.checked_at.asc())
            )
            records = result.scalars().all()
            return [
                {
                    "total": r.total,
                    "category": r.category,
                    "cruise_line": r.cruise_line,
                    "checked_at": r.checked_at.isoformat() if r.checked_at else None,
                }
                for r in records
            ]

    # ── DB Persistence ──────────────────────────────────────────

    async def _save_result_to_db(self, result: BookingResult) -> None:
        """Save a booking result to the database."""
        import json
        async with async_session() as session:
            record = BookingRecord(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                status=result.status.value,
                old_total=result.old_total,
                new_total=result.new_total,
                net_saving=result.net_saving,
                confidence=result.confidence,
                price_category=result.price_category,
                new_price_category=result.new_price_category,
                note=result.note,
                error=result.error,
                lost_pkg_names=json.dumps(result.lost_pkg_names),
            )
            session.add(record)
            await session.commit()

    async def _save_price_history(self, result: BookingResult) -> None:
        """Record a price snapshot."""
        if result.old_total <= 0:
            return
        async with async_session() as session:
            session.add(PriceHistory(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                total=result.old_total,
                category=result.price_category,
            ))
            await session.commit()

    async def _save_job_to_db(self, job: ScanJob) -> None:
        """Save a new scan job."""
        import json
        async with async_session() as session:
            record = ScanJobRecord(
                job_id=job.job_id,
                booking_ids_json=json.dumps(job.booking_ids),
                cruise_line=job.cruise_line.value,
                status=job.status.value,
                progress_total=job.progress_total,
                started_at=job.started_at,
            )
            session.add(record)
            await session.commit()

    async def _update_job_in_db(self, job: ScanJob) -> None:
        """Update a scan job status."""
        async with async_session() as session:
            result = await session.execute(
                select(ScanJobRecord).where(ScanJobRecord.job_id == job.job_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = job.status.value
                record.progress_done = job.progress_done
                record.completed_at = job.completed_at
                await session.commit()
