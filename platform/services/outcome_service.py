"""Did a reported optimization actually get applied?

Neon 2026-10-01, specifying both halves:

  *"we need to implement if this was not verified by a human, the script if
  it runs again and find that this price is now saved and is the new price
  should add it to data base. meaning if the price was originally 1500 and
  we found a drop to 1400, then a human saved and was not verified, after 3
  days for example, if the script scan this booking and it is saved to 1400
  it should understand on its own that is a priority to add it. that smart.
  and next is just add a verify button in the gui, and once it is verified
  it is removed from the least because it means it was optimized and it is
  not needed in the gui list anymore."*

THE GAP THIS CLOSES. The database held 204 OPTIMIZATION rows worth $31,000
and no column recording whether a single one was acted on. "We found $31k"
and "we saved $31k" are different claims and nothing could tell them apart.

THE AUTO-DETECTION SIGNAL is exact and needs no new data collection: a
LATER scan's `old_total` equals an EARLIER optimization's `new_total`. The
price we quoted became the price being paid, which only happens if someone
applied it.

Measured across the full history before this existed: **46 applied
repricings worth $5,485.90** the system had no record of - the largest
$943.00 on booking 3001009, quoted 2026-09-18 and confirmed in place by the
2026-09-21 scan.

WHAT THIS IS NOT. Not an exclusion. A verified booking keeps being scanned,
because its price can drop again; only the GUI row is retired. See
`ExclusionService` for "never scan this again", a different decision with a
much stricter evidence bar.

SCOPED TO THE FIGURES, NOT THE BOOKING. Verifying $79 off today must not
silence a $300 drop next month, so a verification stores the old/new pair
it refers to and only matches that opportunity.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select, update

from models.database import BookingRecord, OptimizationOutcome, async_session
from utils.logging import get_logger

logger = get_logger(__name__)

#: Two totals are the same opportunity within this many currency units.
#: Portal figures carry two decimals; this absorbs float representation
#: only, never a real price difference.
MATCH_TOLERANCE = 0.01

APPLIED = "APPLIED"
REVIEWED = "REVIEWED"


def same_amount(a: float | None, b: float | None,
                tolerance: float = MATCH_TOLERANCE) -> bool:
    """Whether two totals refer to the same figure.

    A missing total is never equal to anything - "unknown" must not match
    "unknown" and silently retire a row nobody has looked at.
    """
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tolerance


class OutcomeService:
    """Records and detects what happened to a reported optimization."""

    # ── reading ──────────────────────────────────────────────────────

    async def verified_pairs(self, cruise_line: str | None = None) -> set:
        """Every live verification, as (booking_id, old, new) rounded keys.

        Bulk, not per booking: the GUI filters a whole result table with
        this, and the per-row round trip is what made the old cache slow
        (723 queries before the first page could render).
        """
        try:
            async with async_session() as session:
                query = select(OptimizationOutcome).where(
                    OptimizationOutcome.cleared_at.is_(None))
                if cruise_line:
                    query = query.where(
                        OptimizationOutcome.cruise_line == cruise_line)
                rows = (await session.execute(query)).scalars().all()
        except Exception as exc:  # noqa: BLE001 - never block the GUI
            logger.warning("outcome.read_failed", error=str(exc)[:200])
            return set()
        return {self._key(r.booking_id, r.old_total, r.new_total) for r in rows}

    @staticmethod
    def _key(booking_id: str, old_total, new_total) -> tuple:
        """Identity of one opportunity. Rounded so float noise cannot
        produce two keys for the same portal figures."""
        def r(v):
            return None if v is None else round(float(v), 2)
        return (str(booking_id), r(old_total), r(new_total))

    def is_verified(self, verified: set, booking_id: str,
                    old_total, new_total) -> bool:
        """Whether THIS opportunity has been verified. Pass the set from
        `verified_pairs` so a table render stays one query."""
        return self._key(booking_id, old_total, new_total) in verified

    async def list_verified(self, cruise_line: str | None = None) -> list[dict]:
        try:
            async with async_session() as session:
                query = select(OptimizationOutcome).where(
                    OptimizationOutcome.cleared_at.is_(None)).order_by(
                    OptimizationOutcome.verified_at.desc())
                if cruise_line:
                    query = query.where(
                        OptimizationOutcome.cruise_line == cruise_line)
                rows = (await session.execute(query)).scalars().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome.list_failed", error=str(exc)[:200])
            return []
        return [{
            "booking_id": r.booking_id, "cruise_line": r.cruise_line,
            "old_total": r.old_total, "new_total": r.new_total,
            "net_saving": r.net_saving, "outcome": r.outcome,
            "verified_by": r.verified_by, "verified_at": r.verified_at,
        } for r in rows]

    async def total_realised(self, cruise_line: str | None = None) -> float:
        """Money actually captured - APPLIED only.

        REVIEWED rows are excluded deliberately: a human looking at a TRAP
        and deciding not to reprice saved nothing, and counting it would
        reproduce exactly the "found vs saved" confusion this module
        exists to end.
        """
        return round(sum(
            float(r["net_saving"] or 0)
            for r in await self.list_verified(cruise_line)
            if r["outcome"] == APPLIED and (r["net_saving"] or 0) > 0
        ), 2)

    # ── writing ──────────────────────────────────────────────────────

    async def record(self, cruise_line: str, booking_id: str, *,
                     old_total=None, new_total=None, net_saving=None,
                     outcome: str = APPLIED, verified_by: str = "human",
                     evidence: dict | None = None) -> bool:
        """Record one verification. Idempotent per opportunity.

        Never raises: a bookkeeping failure must not take down a scan or
        the GUI.
        """
        try:
            already = await self.verified_pairs(cruise_line)
            if self.is_verified(already, booking_id, old_total, new_total):
                return False

            async with async_session() as session:
                session.add(OptimizationOutcome(
                    booking_id=str(booking_id), cruise_line=cruise_line,
                    old_total=old_total, new_total=new_total,
                    net_saving=net_saving, outcome=outcome,
                    verified_by=verified_by,
                    evidence=json.dumps(evidence or {}, default=str),
                    verified_at=datetime.utcnow(),
                ))
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome.record_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False

        logger.info("outcome.recorded", booking_id=booking_id,
                    cruise_line=cruise_line, outcome=outcome,
                    verified_by=verified_by, net_saving=net_saving)
        return True

    async def clear(self, cruise_line: str, booking_id: str,
                    old_total=None, new_total=None) -> bool:
        """Withdraw a verification so the row returns to the list.

        Stamps `cleared_at` rather than deleting - the decision stays
        auditable, the same convention exclusions use.
        """
        try:
            async with async_session() as session:
                query = update(OptimizationOutcome).where(
                    OptimizationOutcome.booking_id == str(booking_id),
                    OptimizationOutcome.cruise_line == cruise_line,
                    OptimizationOutcome.cleared_at.is_(None),
                ).values(cleared_at=datetime.utcnow())
                result = await session.execute(query)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome.clear_failed", booking_id=booking_id,
                           error=str(exc)[:200])
            return False
        return bool(result.rowcount)

    # ── auto-detection ───────────────────────────────────────────────

    async def detect_applied(self, cruise_line: str | None = None,
                             record: bool = True) -> list[dict]:
        """Find optimizations whose quoted price later became the real one.

        Neon's rule, in code: an OPTIMIZATION quoted `new_total`, and a
        LATER scan of the same booking opened at that very figure. Nobody
        pressed Verify, but the saving plainly happened.

        Three guards against claiming a saving that was not ours:

          * the opportunity must be real - `old_total != new_total`,
            otherwise every flat booking "confirms" itself;
          * the later `old_total` must be a genuine figure, not 0.00, which
            is what an unread payment panel looks like;
          * the confirming scan must come strictly AFTER the quote.

        Returns what it found, newest quote first. `record=False` makes it
        a dry run, which is how it was first measured against history.
        """
        try:
            async with async_session() as session:
                query = select(BookingRecord).order_by(
                    BookingRecord.booking_id, BookingRecord.created_at)
                if cruise_line:
                    query = query.where(BookingRecord.cruise_line == cruise_line)
                rows = (await session.execute(query)).scalars().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("outcome.detect_failed", error=str(exc)[:200])
            return []

        by_booking: dict = {}
        for row in rows:
            by_booking.setdefault((row.booking_id, row.cruise_line), []).append(row)

        already = await self.verified_pairs(cruise_line)
        found: list[dict] = []

        for (booking_id, line), history in by_booking.items():
            for index, quote in enumerate(history):
                if quote.status != "OPTIMIZATION":
                    continue
                if not quote.new_total or quote.new_total <= 0:
                    continue
                # A "saving" from X to X is not an opportunity.
                if same_amount(quote.old_total, quote.new_total):
                    continue
                if self.is_verified(already, booking_id,
                                    quote.old_total, quote.new_total):
                    continue

                for later in history[index + 1:]:
                    if not later.old_total or later.old_total <= 0:
                        continue
                    if not same_amount(later.old_total, quote.new_total):
                        continue
                    found.append({
                        "booking_id": booking_id, "cruise_line": line,
                        "old_total": quote.old_total,
                        "new_total": quote.new_total,
                        "net_saving": quote.net_saving,
                        "quoted_at": quote.created_at,
                        "confirmed_at": later.created_at,
                    })
                    break
                else:
                    continue
                break

        if record:
            for hit in found:
                await self.record(
                    hit["cruise_line"], hit["booking_id"],
                    old_total=hit["old_total"], new_total=hit["new_total"],
                    net_saving=hit["net_saving"],
                    outcome=APPLIED, verified_by="auto",
                    evidence={
                        "detected": "quoted price became the booking price",
                        "quoted_at": str(hit["quoted_at"]),
                        "confirmed_at": str(hit["confirmed_at"]),
                    },
                )
            if found:
                logger.info("outcome.auto_detected", count=len(found),
                            total=round(sum(float(h["net_saving"] or 0)
                                            for h in found), 2))

        return sorted(found, key=lambda h: str(h["quoted_at"]), reverse=True)
