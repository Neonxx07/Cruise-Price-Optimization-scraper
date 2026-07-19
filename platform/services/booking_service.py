"""Booking service — orchestrates the full scan workflow.

This is the enterprise equivalent of background.js runBatch().
Manages the scraper lifecycle, result storage, caching, and progress tracking.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime
from typing import AsyncGenerator, Callable

from sqlalchemy import select

from config.settings import settings
from core.calculator import make_error_result, make_skipped_result
from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus
from models.database import BookingRecord, MarketDataRecord, PriceHistory, ScanJobRecord, async_session
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
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
    ) -> ScanJob:
        """
        Start a batch scan of booking IDs.

        Args:
            booking_ids: List of booking IDs to check.
            cruise_line: Which cruise line portal to use.
            on_progress: Optional callback for progress updates.
            bypass_cache: If True, skip the NO_SAVING TTL cache and always
                check live (used by recurring/"watch" runs, where the whole
                point is to re-check the same bookings over time).
            raw_dump_dir: If set, append each booking's raw API response to
                raw_dump_dir/raw_responses.jsonl for later offline analysis.

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
        asyncio.create_task(self._run_batch(job, on_progress, bypass_cache, raw_dump_dir, capture_market_data))

        return job

    async def _run_batch(
        self,
        job: ScanJob,
        on_progress: Callable[[ScanJob], None] | None = None,
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
    ) -> None:
        """Execute the batch scan."""
        scraper = self._get_scraper(job.cruise_line)
        scraper.raw_dump_dir = raw_dump_dir

        consecutive_failures = 0

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
                cached = None if bypass_cache else await self.cache.get(job.cruise_line.value, booking_id)
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
                    result = await scraper.check_booking(booking_id, capture_market_data=capture_market_data)
                except Exception as e:
                    logger.error("batch.error", booking_id=booking_id, error=str(e))
                    result = make_error_result(booking_id, None, job.cruise_line, str(e))

                if capture_market_data and scraper.last_market_data:
                    await self._save_market_data_to_db(result, scraper.last_market_data)

                if result.status == BookingStatus.ERROR:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                # Cache NO_SAVING results (skipped in bypass mode — see above)
                if not bypass_cache and result.status == BookingStatus.NO_SAVING:
                    await self.cache.set_no_saving(job.cruise_line.value, booking_id)

                job.results.append(result)
                job.progress_done = i + 1

                # Persist result
                await self._save_result_to_db(result)
                await self._save_price_history(result)

                if on_progress:
                    on_progress(job)

                # A burst of failures usually means the portal session/token
                # state needs time to recover, not faster retries.
                if consecutive_failures >= settings.scraper_cooldown_after_failures:
                    logger.warning(
                        "batch.cooldown",
                        consecutive_failures=consecutive_failures,
                        cooldown_s=settings.scraper_cooldown_seconds,
                    )
                    await asyncio.sleep(settings.scraper_cooldown_seconds)
                    consecutive_failures = 0
                else:
                    # Randomized pacing between bookings — a real agent
                    # doesn't click through reservations every 0.5s.
                    await asyncio.sleep(random.uniform(
                        settings.scraper_interbooking_delay_min_s,
                        settings.scraper_interbooking_delay_max_s,
                    ))

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

    async def _save_market_data_to_db(self, result: BookingResult, market_data: dict) -> None:
        """Persist read-only ESPRESSO market/category snapshot data."""
        import json

        async with async_session() as session:
            session.add(MarketDataRecord(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                capture_type="espresso_category_table",
                current_category=market_data.get("currentCategory"),
                execution_token=market_data.get("executionToken"),
                selection_json=market_data.get("selectionJSON"),
                category_table_json=json.dumps(market_data.get("rows", []), ensure_ascii=False),
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
