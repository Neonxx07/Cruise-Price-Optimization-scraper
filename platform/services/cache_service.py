"""Smart caching service.

Prevents re-checking bookings that showed NO_SAVING within the TTL window.
Ported from the Chrome extension's chrome.storage.local-based cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select

from config.settings import settings
from models.database import CacheEntry, async_session
from utils.logging import get_logger

logger = get_logger(__name__)


class CacheService:
    """TTL-based cache for booking check results."""

    def __init__(self, ttl_hours: int | None = None):
        self.ttl = timedelta(hours=ttl_hours or settings.cache_ttl_hours)

    async def get(self, cruise_line: str, booking_id: str) -> dict | None:
        """
        Check if a booking has a cached NO_SAVING result.

        Returns:
            Dict with 'hours_ago' if cached, None if not.
        """
        key = f"cache_{cruise_line}_{booking_id}"
        async with async_session() as session:
            result = await session.execute(
                select(CacheEntry).where(CacheEntry.key == key)
            )
            entry = result.scalar_one_or_none()

            if entry is None:
                return None

            if datetime.utcnow() > entry.expires_at:
                await session.execute(delete(CacheEntry).where(CacheEntry.key == key))
                await session.commit()
                return None

            hours_ago = (datetime.utcnow() - (entry.expires_at - self.ttl)).total_seconds() / 3600
            return {"hours_ago": round(hours_ago, 1)}

    async def set_no_saving(self, cruise_line: str, booking_id: str) -> None:
        """Cache a NO_SAVING result for this booking."""
        key = f"cache_{cruise_line}_{booking_id}"
        expires = datetime.utcnow() + self.ttl

        async with async_session() as session:
            # Upsert
            result = await session.execute(
                select(CacheEntry).where(CacheEntry.key == key)
            )
            entry = result.scalar_one_or_none()

            if entry:
                entry.expires_at = expires
            else:
                session.add(CacheEntry(key=key, expires_at=expires))

            await session.commit()
            logger.debug("cache.set", key=key, ttl_hours=self.ttl.total_seconds() / 3600)

    async def clear_all(self) -> int:
        """Remove all cache entries. Returns count deleted."""
        async with async_session() as session:
            result = await session.execute(delete(CacheEntry))
            await session.commit()
            return result.rowcount or 0

    async def cleanup_expired(self) -> int:
        """Remove expired cache entries. Returns count deleted."""
        async with async_session() as session:
            result = await session.execute(
                delete(CacheEntry).where(CacheEntry.expires_at < datetime.utcnow())
            )
            await session.commit()
            return result.rowcount or 0
