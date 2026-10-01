"""Bookings that must never be scanned again.

Neon 2026-09-29, non-negotiable: *"IF THE BOOKING SURELY FOR 100% SURE IS
PAID IN FULL THIS MUST BE STORED IN THE DATA BASE AND NEVER EVER BE
RESCANNED AGAIN EVEN IF THE USER PASTES OR ADDS IT IN THE LIST"*.

A paid-in-full booking cannot be repriced, so reopening it is pure waste.
Measured across 24 hours of real scans: **439 of 565 redundant scans** were
PAID_IN_FULL bookings checked over and over, because the existing TTL cache
(`CacheService`) only ever stored NO_SAVING.

WHERE THE DANGER IS. This is permanent, so a WRONG entry is unrecoverable -
the booking vanishes from every future scan and nobody is told. That is a
far worse failure than a redundant scan, so the bar for writing one is
deliberately high and every entry carries its own evidence.

`record_paid_in_full` refuses unless ALL of these hold:

  * the payment panel was actually READ (`payment_state_readable`). "We
    could not see the balance" must never become "it owes nothing" - that
    exact confusion produced a false $400 saving on booking 3001001, a
    reservation with two cents outstanding.
  * the booking has a real total. A 0.00 total is not a settled booking, it
    is an unread one.
  * the status really is PAID_IN_FULL.

CANCELLED BOOKINGS ARE SAFE BY CONSTRUCTION. A cancelled reservation
displays Final Payment Due 0.00 and would otherwise look settled - but
`EspressoScraper._check_booking_inner` calls `is_cancelled()` BEFORE reading
the payment panel, so a CX booking returns CANCELLED and never reaches the
paid-in-full test. Cancellations remain mandatory to report.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select, update

from models.database import PermanentExclusion, async_session
from utils.logging import get_logger

logger = get_logger(__name__)

PAID_IN_FULL = "PAID_IN_FULL"

# CANCELLED, added 2026-09-29 on Neon's instruction: "add canceled as the
# same rule case as paid in full i am trying to optimize to save resoursces
# and not doing useless scans".
#
# A cancelled reservation does not un-cancel, so re-opening it is as futile
# as re-opening a settled one. 14 cancellations were being re-scraped on
# every run.
#
# THIS DOES NOT WEAKEN THE REPORTING RULE. Neon 2026-09-22: reporting a
# cancellation is "VERY MADNATORY ... something very critical". An excluded
# booking is still REPORTED on every run - it comes back as CANCELLED from
# the register instead of from the portal. What stops is the scraping, not
# the reporting.
#
# It is safe to make permanent because the signal is definite rather than
# inferred: EspressoScraper.is_cancelled() reads the portal's own
# `'CX' == sb.reservation.status` span and checks it is actually VISIBLE at
# runtime, and it runs BEFORE the payment panel - which is what stopped
# cancelled bookings being filed as paid-in-full off their Final Payment
# Due 0.00.
CANCELLED = "CANCELLED"


class ExclusionService:
    """Permanent, auditable, reversible scan exclusions."""

    async def active_for(self, cruise_line: str,
                         booking_ids: list[str]) -> dict[str, dict]:
        """Which of these bookings are excluded, in ONE query.

        Bulk by design. The per-booking cache lookup it sits next to issues
        a query per booking, which on a 723-booking watchlist is 723 round
        trips before the first page even loads.

        Returns {booking_id: {"reason", "excluded_at", "evidence"}}. An
        empty dict means scan them all - this must fail OPEN, because a
        redundant scan costs a page load while a wrongly skipped one costs a
        real client's price drop.
        """
        if not booking_ids:
            return {}
        try:
            async with async_session() as session:
                rows = await session.execute(
                    select(PermanentExclusion).where(
                        PermanentExclusion.cruise_line == cruise_line,
                        PermanentExclusion.booking_id.in_(booking_ids),
                        PermanentExclusion.cleared_at.is_(None),
                    )
                )
                return {
                    r.booking_id: {
                        "reason": r.reason,
                        "excluded_at": r.excluded_at,
                        "evidence": r.evidence or "",
                    }
                    for r in rows.scalars()
                }
        except Exception as exc:
            logger.warning("exclusion.lookup_failed", error=str(exc)[:200])
            return {}

    async def record_paid_in_full(self, cruise_line: str, booking_id: str, *,
                                  total_price: float | None,
                                  final_payment_due: float | None,
                                  payment_state_readable: bool,
                                  currency: str | None = None) -> bool:
        """Permanently exclude a CONFIRMED paid-in-full booking.

        Returns True only if an exclusion was written. Every refusal is
        logged with its reason, because a booking that should have been
        excluded and was not is merely wasteful - while one excluded on
        thin evidence is invisible for ever.
        """
        if not payment_state_readable:
            # The single most important guard. Unreadable is not zero.
            logger.info("exclusion.refused", booking_id=booking_id,
                        reason="payment_panel_unreadable")
            return False
        if final_payment_due is None:
            logger.info("exclusion.refused", booking_id=booking_id,
                        reason="no_final_payment_figure")
            return False
        if not total_price or total_price <= 0:
            # A 0.00 total is an unread booking, not a settled one.
            logger.info("exclusion.refused", booking_id=booking_id,
                        reason="no_total_price")
            return False

        return await self._record(cruise_line, booking_id, PAID_IN_FULL, {
            "total_price": total_price,
            "final_payment_due": final_payment_due,
            "currency": currency,
            "observed_at": datetime.utcnow().isoformat(),
        })

    async def record_cancelled(self, cruise_line: str, booking_id: str, *,
                               detail: str = "") -> bool:
        """Permanently exclude a booking the portal reports as CANCELLED.

        No evidence guard is needed of the kind `record_paid_in_full` has,
        because the signal is not inferred from figures that might be
        unreadable - the scraper matched the portal's own
        `'CX' == sb.reservation.status` span and confirmed it was visible.
        A status the portal states outright is as certain as this system
        gets, and a cancellation does not reverse.

        The booking keeps being REPORTED as cancelled on every run; only
        the scraping stops.
        """
        return await self._record(cruise_line, booking_id, CANCELLED,
                                  {"detail": detail[:200],
                                   "observed_at": datetime.utcnow().isoformat()})

    async def _record(self, cruise_line: str, booking_id: str,
                      reason: str, evidence: dict) -> bool:
        """Insert one exclusion, unless the booking already has an active one."""
        try:
            async with async_session() as session:
                existing = await session.execute(
                    select(PermanentExclusion).where(
                        PermanentExclusion.cruise_line == cruise_line,
                        PermanentExclusion.booking_id == booking_id,
                        PermanentExclusion.cleared_at.is_(None),
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    return False
                session.add(PermanentExclusion(
                    booking_id=booking_id, cruise_line=cruise_line,
                    reason=reason, evidence=json.dumps(evidence, default=str)))
                await session.commit()
            logger.info("exclusion.recorded", booking_id=booking_id,
                        cruise_line=cruise_line, reason=reason)
            return True
        except Exception as exc:
            # Never fatal: failing to record costs a future redundant scan,
            # which is the harmless direction.
            logger.warning("exclusion.write_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False

    async def clear(self, cruise_line: str, booking_id: str) -> bool:
        """Lift an exclusion so the booking can be scanned again.

        The row is KEPT and stamped, not deleted - the decision to exclude
        and the decision to undo it are both part of the audit trail.
        """
        try:
            async with async_session() as session:
                result = await session.execute(
                    update(PermanentExclusion)
                    .where(PermanentExclusion.cruise_line == cruise_line,
                           PermanentExclusion.booking_id == booking_id,
                           PermanentExclusion.cleared_at.is_(None))
                    .values(cleared_at=datetime.utcnow())
                )
                await session.commit()
            cleared = bool(result.rowcount)
            if cleared:
                logger.info("exclusion.cleared", booking_id=booking_id,
                            cruise_line=cruise_line)
            return cleared
        except Exception as exc:
            logger.warning("exclusion.clear_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False

    async def list_active(self, cruise_line: str | None = None) -> list[dict]:
        """Every exclusion currently in force, newest first - so the
        operator can see what the scanner is deliberately not looking at."""
        try:
            async with async_session() as session:
                stmt = select(PermanentExclusion).where(
                    PermanentExclusion.cleared_at.is_(None))
                if cruise_line:
                    stmt = stmt.where(PermanentExclusion.cruise_line == cruise_line)
                rows = await session.execute(
                    stmt.order_by(PermanentExclusion.excluded_at.desc()))
                return [
                    {"booking_id": r.booking_id, "cruise_line": r.cruise_line,
                     "reason": r.reason, "excluded_at": r.excluded_at,
                     "evidence": r.evidence or ""}
                    for r in rows.scalars()
                ]
        except Exception as exc:
            logger.warning("exclusion.list_failed", error=str(exc)[:200])
            return []
