"""Scan freshness: don't re-open a booking the database already knows about.

Neon 2026-09-29: *"the price usually changes every 24 hours for example or
12 hours or 6 hours now if i am scanning the same bookings list ... it scans
over again ... instead it states that this booking was scanned 1 or 2 hours
ago and it is collected in the data base prices infromation"*.

MEASURED over 24 hours of real scans, which is what justifies the change:

    rows in the last 24h     : 1393
    bookings scanned >1 time : 495
    REDUNDANT scans          : 565   (41% of all work, ~148 minutes)

and of 55 repeats where the total could be compared across both scans,
**55 were identical and 0 had changed**.

WHAT THIS USED TO BE, and why 41% survived it. The old CacheService had a
single writer, `set_no_saving`, and BookingService gated it on
`status == NO_SAVING`. Every other outcome was re-scanned on every run -
PAID_IN_FULL alone accounted for 439 of those repeats. It also stored only a
timestamp, leaving `value_json` unused, so a skipped booking showed
"scanned 1.4h ago" with no prices attached and the row looked empty.

Three things changed:

  * **Per-line windows.** `settings.freshness_hours` - ESPRESSO 12h, NCL 6h,
    GoCCL 24h - instead of one global TTL.
  * **Every cacheable outcome**, not just NO_SAVING.
    `settings.never_cache_statuses` keeps OPTIMIZATION, ERROR, CANCELLED and
    UNKNOWN out: a live saving must always be re-confirmed, a failure is not
    an outcome, and every cancellation must be reported on every run.
  * **The result travels with the entry.** `value_json` now carries the
    stored figures, so a skipped booking displays exactly like a scanned one.

SEPARATE FROM PERMANENT EXCLUSIONS. This is a TTL: it expires, and "Force
live recheck" bypasses it. `ExclusionService` is the permanent, non-TTL
register for bookings confirmed paid in full, and nothing here can override
it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from config.settings import settings
from core.calculator_version import CALCULATOR_FINGERPRINT
from models.database import CacheEntry, async_session
from utils.logging import get_logger

logger = get_logger(__name__)


def _key(cruise_line: str, booking_id: str) -> str:
    return f"cache_{cruise_line}_{booking_id}"


class CacheService:
    """Freshness cache for booking scan results, keyed by line + booking."""

    def __init__(self, ttl_hours: int | None = None):
        # An explicit ttl_hours overrides the per-line table entirely; it is
        # what the tests and any caller wanting one fixed window use.
        self._forced_ttl = ttl_hours
        self.ttl = timedelta(hours=ttl_hours or settings.cache_ttl_hours)

    def window_for(self, cruise_line: str) -> timedelta:
        """The freshness window that applies to this cruise line."""
        if self._forced_ttl is not None:
            return timedelta(hours=self._forced_ttl)
        return timedelta(hours=settings.freshness_for(cruise_line))

    def is_cacheable(self, status: str) -> bool:
        """Whether an outcome may be served from cache at all."""
        return (status or "").upper() not in {
            s.upper() for s in settings.never_cache_statuses}

    # ── reading ──────────────────────────────────────────────────────────

    async def get(self, cruise_line: str, booking_id: str) -> dict | None:
        """One booking's fresh entry, or None.

        Kept for callers that check a single booking. `get_many` is what the
        batch loop should use - see its docstring.
        """
        found = await self.get_many(cruise_line, [booking_id])
        return found.get(booking_id)

    async def get_many(self, cruise_line: str,
                       booking_ids: list[str]) -> dict[str, dict]:
        """Fresh entries for a whole watchlist, in ONE query.

        The previous code issued a SELECT (and sometimes a DELETE + commit)
        per booking from inside the scan loop. On a 723-booking list that is
        723 round trips before the first page loads, and every one of them
        was a chance to trip the SQLite lock that once marked a whole job
        FAILED.

        Returns {booking_id: {"hours_ago", "scanned_at", "status", "data"}}.
        Fails OPEN - an empty dict means scan everything, because a
        redundant scan costs a page load while a wrongly skipped one costs a
        real client's price drop.
        """
        if not booking_ids:
            return {}
        window = self.window_for(cruise_line)
        now = datetime.utcnow()
        keys = {_key(cruise_line, b): b for b in booking_ids}
        out: dict[str, dict] = {}
        try:
            async with async_session() as session:
                rows = await session.execute(
                    select(CacheEntry).where(CacheEntry.key.in_(list(keys))))
                for entry in rows.scalars():
                    booking_id = keys.get(entry.key)
                    if booking_id is None or entry.expires_at is None:
                        continue
                    if now > entry.expires_at:
                        continue          # stale; the sweeper removes it
                    scanned_at = entry.expires_at - window
                    payload = {}
                    try:
                        payload = json.loads(entry.value_json or "{}")
                    except ValueError:
                        payload = {}

                    # A VERDICT THE CURRENT CALCULATOR DISAGREES WITH IS
                    # NOT FRESH.
                    #
                    # TRAP and NO_SAVING are both cacheable, for up to 24
                    # hours. So when the calculator changes, every stored
                    # row is an answer the code no longer gives - and it
                    # keeps being served until the TTL runs out, silently,
                    # looking perfectly normal.
                    #
                    # Hit twice in two days: the ALL INC 2PK NRD double
                    # count (8 entries cleared by hand) and booking
                    # 3001014, a price INCREASE shown as a green
                    # OPTIMIZATION (1 entry). Clearing by hand is a step
                    # someone will forget.
                    #
                    # Entries written before this existed carry no
                    # fingerprint, so they expire once - which is correct,
                    # nothing knows which calculator produced them.
                    if payload.get("calc") != CALCULATOR_FINGERPRINT:
                        logger.info("cache.stale_calculator",
                                    booking_id=booking_id,
                                    stored=payload.get("calc"),
                                    current=CALCULATOR_FINGERPRINT)
                        continue
                    out[booking_id] = {
                        "hours_ago": round(
                            (now - scanned_at).total_seconds() / 3600, 1),
                        "scanned_at": scanned_at,
                        "status": payload.get("status"),
                        "data": payload,
                    }
        except Exception as exc:
            logger.warning("cache.bulk_read_failed", error=str(exc)[:200])
            return {}
        return out

    # ── writing ──────────────────────────────────────────────────────────

    async def set_result(self, cruise_line: str, booking_id: str, *,
                         status: str, payload: dict | None = None) -> bool:
        """Remember an outcome, with the figures needed to display it.

        Returns True if an entry was written. Refuses statuses listed in
        `settings.never_cache_statuses`.
        """
        if not self.is_cacheable(status):
            return False
        window = self.window_for(cruise_line)
        expires = datetime.utcnow() + window
        body = dict(payload or {})
        body["status"] = status
        body["scanned_at"] = datetime.utcnow().isoformat()
        # WHICH CALCULATOR PRODUCED THIS VERDICT. Read back in get_many: a
        # mismatch means the logic has changed since, so the entry is not
        # fresh however recent it is. See core/calculator_version.py.
        body["calc"] = CALCULATOR_FINGERPRINT
        key = _key(cruise_line, booking_id)
        try:
            async with async_session() as session:
                existing = await session.execute(
                    select(CacheEntry).where(CacheEntry.key == key))
                entry = existing.scalar_one_or_none()
                if entry:
                    entry.expires_at = expires
                    entry.value_json = json.dumps(body, default=str)
                else:
                    session.add(CacheEntry(key=key, expires_at=expires,
                                           value_json=json.dumps(body,
                                                                 default=str)))
                await session.commit()
            return True
        except Exception as exc:
            logger.warning("cache.write_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False

    async def set_no_saving(self, cruise_line: str, booking_id: str) -> None:
        """Back-compatible shim for the original single-status writer."""
        await self.set_result(cruise_line, booking_id, status="NO_SAVING")

    # ── housekeeping ─────────────────────────────────────────────────────

    async def clear_all(self) -> int:
        """Remove all cache entries. Returns count deleted."""
        async with async_session() as session:
            result = await session.execute(delete(CacheEntry))
            await session.commit()
            return result.rowcount or 0

    async def clear_one(self, cruise_line: str, booking_id: str) -> bool:
        """Drop one booking's entry, so the next run scans it live.

        This is what a per-booking "Force rescan" does. It does NOT touch
        permanent exclusions - see ExclusionService.clear for those.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    delete(CacheEntry).where(
                        CacheEntry.key == _key(cruise_line, booking_id)))
                await session.commit()
                return bool(result.rowcount)
        except Exception as exc:
            logger.warning("cache.clear_one_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False

    async def cleanup_expired(self) -> int:
        """Remove expired cache entries. Returns count deleted."""
        async with async_session() as session:
            result = await session.execute(
                delete(CacheEntry).where(CacheEntry.expires_at < datetime.utcnow())
            )
            await session.commit()
            return result.rowcount or 0
