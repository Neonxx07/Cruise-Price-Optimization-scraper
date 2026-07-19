"""Periodic task scheduler using APScheduler.

Runs background jobs like periodic price checks and cache cleanup.
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from services.booking_service import BookingService
from services.cache_service import CacheService
from utils.logging import get_logger

logger = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_booking_service = BookingService()
_cache_service = CacheService()


async def _cleanup_expired_cache():
    """Remove expired cache entries."""
    count = await _cache_service.cleanup_expired()
    if count > 0:
        logger.info("scheduler.cache_cleanup", removed=count)


async def _periodic_check():
    """
    Periodic price check for watched bookings.

    This is a placeholder — in production, you would:
    1. Query a "watchlist" table for bookings to re-check
    2. Run them through the scraper
    3. Store updated results
    4. Send notifications if savings are found
    """
    logger.info("scheduler.periodic_check", msg="Periodic check triggered (no watchlist configured)")


def start_scheduler() -> AsyncIOScheduler:
    """Initialize and start the background scheduler."""
    global _scheduler

    if _scheduler is not None:
        return _scheduler

    _scheduler = AsyncIOScheduler()

    # Cache cleanup every 6 hours
    _scheduler.add_job(
        _cleanup_expired_cache,
        trigger=IntervalTrigger(hours=6),
        id="cache_cleanup",
        name="Cleanup expired cache entries",
        replace_existing=True,
    )

    # Periodic price check (configurable interval)
    if settings.scheduler_enabled:
        _scheduler.add_job(
            _periodic_check,
            trigger=IntervalTrigger(minutes=settings.scheduler_interval_minutes),
            id="periodic_check",
            name="Periodic price check",
            replace_existing=True,
        )
        logger.info(
            "scheduler.started",
            interval_minutes=settings.scheduler_interval_minutes,
        )

    _scheduler.start()
    logger.info("scheduler.initialized")
    return _scheduler


def stop_scheduler() -> None:
    """Shutdown the scheduler gracefully."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler.stopped")
