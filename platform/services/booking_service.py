"""Booking service — orchestrates the full scan workflow.

This is the enterprise equivalent of background.js runBatch().
Manages the scraper lifecycle, result storage, caching, and progress tracking.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from config.settings import settings
from core.calculator import make_error_result, make_skipped_result
from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus
from models.database import BookingRecord, MarketDataRecord, PriceHistory, ScanJobRecord, async_session
from scraper.base import BaseScraper
from scraper.espresso import EspressoScraper
from scraper.goccl import GoCCLScraper
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
        # A single long-lived scraper/browser, reused across "Check login"
        # and every scan in the same app session (see get_or_create_scraper).
        # Replaying saved session cookies into a brand-new browser process
        # is exactly the pattern ESPRESSO's bot-detection (Akamai) flags —
        # keeping one continuous instance from login through every scan
        # avoids that entirely, matching the one run that worked end to end.
        self._live_scraper: BaseScraper | None = None

    def _get_scraper(self, cruise_line: CruiseLine) -> BaseScraper:
        """Factory: get the right scraper for the cruise line."""
        if cruise_line == CruiseLine.NCL:
            return NclScraper()
        if cruise_line == CruiseLine.GOCCL:
            return GoCCLScraper()
        return EspressoScraper()

    @staticmethod
    def _is_dead_browser_error(exc: Exception) -> bool:
        """Whether an exception means the underlying Playwright browser/
        context/page died mid-scrape (as opposed to a normal portal-level
        failure like a bad selector or a real API error). Seen in practice
        as e.g. "Page.goto: Target page, context or browser has been
        closed" — reusing the same scraper for the next booking would just
        fail identically every time, so this is the signal to restart it."""
        msg = str(exc).lower()
        return "has been closed" in msg or "target closed" in msg

    def has_live_session(self, cruise_line: CruiseLine) -> bool:
        """Whether a browser session is already open for this cruise line
        (i.e. check_login has run) — Start should require this, since
        starting one fresh would fall back to a hidden headless browser
        with whatever stale session is on disk."""
        return self._live_scraper is not None and self._live_scraper.cruise_line == cruise_line

    @staticmethod
    def _login_base_url(cruise_line: CruiseLine) -> str:
        """Where to land a fresh browser session for a manual login check."""
        if cruise_line == CruiseLine.NCL:
            return settings.ncl_search_url
        if cruise_line == CruiseLine.GOCCL:
            return settings.goccl_search_url
        return settings.espresso_home_url

    async def get_or_create_scraper(self, cruise_line: CruiseLine, headless: bool | None = None) -> BaseScraper:
        """Get the live, already-open scraper for this cruise line, or start
        a new one if none is open yet (or the cruise line changed)."""
        if self._live_scraper is not None:
            if self._live_scraper.cruise_line == cruise_line:
                return self._live_scraper
            await self._live_scraper.stop()
            self._live_scraper = None

        scraper = self._get_scraper(cruise_line)
        await scraper.start(headless=headless)
        self._live_scraper = scraper
        return scraper

    async def close_live_scraper(self) -> None:
        """Close the live browser session, if one is open — saves the
        final session state. Call this on app shutdown."""
        if self._live_scraper is not None:
            await self._live_scraper.stop()
            self._live_scraper = None

    async def check_login(
        self, cruise_line: CruiseLine, timeout_minutes: float = 15.0,
    ) -> bool:
        """
        Open (or reuse) the live browser, visibly, and wait for the user to
        log in. The same browser instance stays open afterward for
        start_scan to reuse — never closed and reopened, since that's what
        triggers the bot-detection replay flag.
        """
        scraper = await self.get_or_create_scraper(cruise_line, headless=False)
        base_url = self._login_base_url(cruise_line)
        await scraper.navigate(base_url)

        deadline = time.monotonic() + timeout_minutes * 60
        poll_s = 5
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            if cruise_line in (CruiseLine.NCL, CruiseLine.GOCCL):
                logged_in = "login" not in scraper.page.url.lower() and "signin" not in scraper.page.url.lower()
            else:
                logged_in = await scraper._check_login()
            if logged_in:
                logger.info("login_check.success", cruise_line=cruise_line.value)
                return True
            logger.info("login_check.waiting", cruise_line=cruise_line.value)

        logger.warning("login_check.timeout", cruise_line=cruise_line.value)
        return False

    async def start_scan(
        self,
        booking_ids: list[str],
        cruise_line: CruiseLine,
        on_progress: Callable[[ScanJob], None] | None = None,
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
        capture_everything: bool = False,
        on_action: Callable[[dict], None] | None = None,
        keep_browser_open: bool = False,
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
            capture_everything: If True, also capture full page HTML +
                structured extraction and all network traffic to
                raw_dump_dir, and record a step-by-step action log to
                raw_dump_dir/actions.jsonl.
            on_action: Optional callback invoked with each action-log entry
                as it happens (used by the GUI to show a live activity log).
            keep_browser_open: If True, reuse the live scraper (from
                get_or_create_scraper/check_login) and leave it open when
                the batch finishes, instead of starting a fresh browser and
                closing it — used by the GUI so login and every scan share
                one continuous session. CLI one-shot runs leave this False.

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
        asyncio.create_task(self._run_batch(
            job, on_progress, bypass_cache, raw_dump_dir, capture_market_data,
            capture_everything, on_action, keep_browser_open,
        ))

        return job

    async def _run_batch(
        self,
        job: ScanJob,
        on_progress: Callable[[ScanJob], None] | None = None,
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
        capture_everything: bool = False,
        on_action: Callable[[dict], None] | None = None,
        keep_browser_open: bool = False,
    ) -> None:
        """Execute the batch scan."""
        if keep_browser_open:
            scraper = await self.get_or_create_scraper(job.cruise_line)
        else:
            scraper = self._get_scraper(job.cruise_line)
        scraper.raw_dump_dir = raw_dump_dir
        scraper.capture_everything = capture_everything
        scraper.on_action = on_action

        consecutive_failures = 0

        try:
            if not keep_browser_open:
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

                    if self._is_dead_browser_error(e):
                        logger.warning("batch.browser_dead_restarting", booking_id=booking_id)
                        try:
                            await scraper.stop()
                        except Exception:
                            pass
                        scraper = self._get_scraper(job.cruise_line)
                        scraper.raw_dump_dir = raw_dump_dir
                        scraper.capture_everything = capture_everything
                        scraper.on_action = on_action
                        try:
                            await scraper.start()
                            if keep_browser_open:
                                self._live_scraper = scraper
                        except Exception as restart_error:
                            logger.error("batch.browser_restart_failed", error=str(restart_error))

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
            if keep_browser_open:
                # The live session may be in a broken state after a fatal
                # error — drop it so the next scan starts a clean one
                # rather than silently reusing something broken.
                await self.close_live_scraper()

        finally:
            if not keep_browser_open:
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
        """Persist read-only market/category snapshot data."""
        import json

        capture_types = {
            CruiseLine.ESPRESSO: "espresso_category_table",
            CruiseLine.NCL: "ncl_category_table",
            CruiseLine.GOCCL: "goccl_offer_code_comparison",
        }

        async with async_session() as session:
            session.add(MarketDataRecord(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                capture_type=capture_types.get(result.cruise_line, "category_table"),
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
