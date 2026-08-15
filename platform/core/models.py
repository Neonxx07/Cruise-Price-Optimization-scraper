"""Core data models for the Cruise Intelligence System.

All data structures used across the system — booking results, invoice items,
scan jobs. Uses Pydantic for validation and serialization.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────


class CruiseLine(str, Enum):
    ESPRESSO = "ESPRESSO"
    NCL = "NCL"
    GOCCL = "GOCCL"
    MSC = "MSC"


class BookingStatus(str, Enum):
    OPTIMIZATION = "OPTIMIZATION"
    TRAP = "TRAP"
    NO_SAVING = "NO_SAVING"
    ERROR = "ERROR"
    WLT = "WLT"
    PAID_IN_FULL = "PAID_IN_FULL"
    SKIPPED_TODAY = "SKIPPED_TODAY"
    # A strictly-higher-tier category is available at or below the current
    # price (e.g. Interior -> Outside for the same or less money) — a pure
    # upgrade, never a downgrade or sideways move. Always human-reviewed
    # before acting, same as WLT/PAID_IN_FULL — never auto-selected.
    UPGRADE_AVAILABLE = "UPGRADE_AVAILABLE"


class ScanJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


# ── Booking Result ──────────────────────────────────────────────


class BookingResult(BaseModel):
    """The complete result of analyzing a single booking."""

    cruise_line: CruiseLine
    status: BookingStatus
    note: str = ""
    error: Optional[str] = None

    booking_id: str
    price_category: Optional[str] = None
    new_price_category: Optional[str] = None

    # ADDED 2026-08-13 (Phase 0 correctness audit): CONFIRMED, no currency
    # field existed anywhere in this model before — every dollar figure was
    # implicitly assumed USD with zero verification, including the invoice-
    # JSON-derived VACATION_TOTAL/OBC_TOTAL that actually drive net_saving
    # (ESPRESSO's payment-status page text has a currency label to check;
    # the invoice JSON itself has none observed). "UNKNOWN" is the honest
    # default — only ESPRESSO's scraper currently sets this to a real
    # detected code (see scraper/espresso.py), read from the SAME page text
    # this project's own is_paid_in_full() already trusts. NCL/GoCCL/MSC
    # have no currency-detection mechanism implemented yet (confirmed — no
    # currency-shaped field was found anywhere in their scraping code), so
    # their results stay "UNKNOWN" rather than silently defaulting to "USD".
    # This field is intentionally NOT yet used to gate or filter
    # total_optimization_savings() — recording the fact honestly is this
    # phase's job; deciding what to DO about a mismatched/unknown currency
    # is Intelligence-layer work, explicitly out of scope here.
    currency: str = "UNKNOWN"

    old_total: float = 0.0
    new_total: float = 0.0
    price_drop: float = 0.0
    obc_change: float = 0.0
    # CONFIRMED INTENDED SEMANTICS (2026-08-13 audit): a raw, SIGNED net
    # figure (price_drop + obc_change - lost_pkg_value for ESPRESSO,
    # price_drop - lost_addon_value for NCL) — NOT pre-gated to "only
    # when actually recommended." A NO_SAVING row can carry a NEGATIVE
    # value (a real price increase); a TRAP or NO_SAVING row can just as
    # legitimately carry a POSITIVE value (the package-trap/OBC-loss-
    # ratio checks exist specifically to catch a "win" that's smaller
    # than what's being given up — that number is still a real positive
    # net_saving, just not one that should ever be acted on). NEVER sum
    # or display this across multiple results without first filtering to
    # status == OPTIMIZATION — use core.calculator.total_optimization_savings()
    # rather than re-implementing that filter.
    net_saving: float = 0.0

    lost_pkg_value: float = 0.0
    lost_pkg_names: list[str] = Field(default_factory=list)
    lost_fares: list[str] = Field(default_factory=list)
    re_addable_fares: list[str] = Field(default_factory=list)
    gained_fares: list[str] = Field(default_factory=list)

    confidence: int = 0
    old_cruise_fare: float = 0.0
    new_cruise_fare: float = 0.0
    fare_change_pct: float = 0.0

    checked_at: datetime = Field(default_factory=datetime.utcnow)


# ── MSC Booking Result ──────────────────────────────────────────
#
# MSC does not fit the single old_total/new_total/net_saving shape above.
# Confirmed directly by Neon 2026-08-09/10: price and discount are
# independent levers on MSC, and a booking must be checked for THREE
# distinct, non-exclusive opportunity types rather than one "is the total
# lower" comparison — see calculator_msc.py for the full rationale. This
# model exists to hold all three findings side by side instead of
# collapsing them into one status the way ESPRESSO/NCL/GoCCL do.


# ADDED 2026-08-12, direct instruction from Neon: a booking counts as
# "paid in full" not only at exactly $0.00 Due Amount, but also when
# Due Amount is a small non-zero residual under this threshold, or when
# MSC shows "Overpayment" instead of a Due Amount at all. Shared between
# msc_commands.py (_is_paid_in_full, applied live before every check)
# and calculator_msc.py (_due_amount_context_note's wording) so both
# stay in sync — this is a deliberate business-judgment number Neon
# gave directly, not a parsing-tolerance fudge factor; don't change it
# without asking him first.
MSC_PAID_IN_FULL_DUE_THRESHOLD = 15.00


class MscOpportunityType(str, Enum):
    PRICE_MATCH = "PRICE_MATCH"
    DISCOUNT_ADD = "DISCOUNT_ADD"
    DISCOUNT_TIER_UPGRADE = "DISCOUNT_TIER_UPGRADE"
    # A fourth, independent lever confirmed 2026-08-11: MSC's own
    # DiscountPaxTypeCmd backend catalog can offer a per-sailing "Voyagers
    # Selection" promo (paxType codes MSVG10W/MSVG15W, on-screen label
    # "SPECIAL OFFER 10%/15%") that STACKS on top of the base 5% Club
    # discount rather than replacing a tier of it — confirmed via a real
    # captured CabinSelectionConfirmCmd request submitting both codes
    # together. Kept separate from DISCOUNT_ADD/DISCOUNT_TIER_UPGRADE
    # rather than folded in, since it has its own eligibility rules
    # (requires Club membership, single-cabin-booking only, confirmed
    # UI-enforced as not combinable with Senior Discount) that don't fit
    # either existing check's shape.
    VOYAGERS_SELECTION = "VOYAGERS_SELECTION"


class MscCheckStatus(str, Enum):
    OPPORTUNITY = "OPPORTUNITY"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    # Confirmed data wasn't available to make this specific check safely —
    # e.g. today's discount dropdown options weren't captured, or the
    # booking's own pre-discount base price isn't known. Never silently
    # defaults to NO_OPPORTUNITY — that would look identical to a real
    # "checked and found nothing," which is a different, false claim.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class MscCheck(BaseModel):
    type: MscOpportunityType
    status: MscCheckStatus
    note: str = ""
    # CONFIRMED REAL INCONSISTENCY, flagged 2026-08-13 audit, not yet
    # fixed (would need a judgment call on how to split/rename, not a
    # pure bug fix): units are NOT the same across check types. For
    # PRICE_MATCH this is a DOLLAR amount (see calculator_msc.py's
    # _check_price_match, `estimated_value=diff`). For
    # DISCOUNT_TIER_UPGRADE this is a PERCENTAGE-POINT difference
    # (`best_rate - current_best`, e.g. 5.0 meaning "5 points better"),
    # never a dollar figure. Nothing in this codebase currently
    # aggregates/displays this field directly (verified — only `.note`,
    # a human-readable string, is ever surfaced), so this is dormant
    # today, not an active bug. But it IS a landmine for any future
    # "total estimated opportunity value" feature — such a feature must
    # NOT sum this field across check types without first checking
    # `type`.
    estimated_value: Optional[float] = None


class MscBookingResult(BaseModel):
    """One booking's full three-check MSC evaluation. `checks` always has
    exactly one entry per MscOpportunityType (unless the booking is
    cancelled/postponed, in which case it's empty and nothing is checked)."""

    cruise_line: CruiseLine = CruiseLine.MSC
    booking_id: str
    category: Optional[str] = None
    cancelled_or_postponed: bool = False
    is_paid_in_full: bool = False
    checks: list[MscCheck] = Field(default_factory=list)
    has_any_opportunity: bool = False
    note: str = ""

    checked_at: datetime = Field(default_factory=datetime.utcnow)


# ── MSC Discount Price-Test ─────────────────────────────────────
#
# ADDED 2026-08-13, forensic investigation of bookings 3000026/74242969:
# confirmed that evaluate_msc_booking()'s DISCOUNT_ADD/DISCOUNT_TIER_UPGRADE
# checks can detect a discount is ELIGIBLE (Senior, Voyagers Club, a named
# promo) but never determine what it's actually WORTH in dollars — MSC's
# own backend represents Senior's rate as a non-literal, dynamically-
# computed value (discRate='0', isInv=true in DiscountPaxTypeCmd), and the
# flat "Voyagers Club 5%" figure _check_discount_add reports is a
# hardcoded assumption, never read from the live page. The only
# authoritative way to know a discount's real dollar effect is to select
# it on MSC's own occupancy screen and read back MSC's own recalculated
# price — these models represent the result of doing exactly that.


class MscDiscountApplicationMethod(str, Enum):
    # The "Additional Discounts" dropdown on the occupancy screen (Senior,
    # Military, TODAY10, etc.) — same generic <select>-search technique
    # the existing select_option_label command already uses.
    DROPDOWN_OPTION = "DROPDOWN_OPTION"
    # The Voyagers Club member-lookup control (.club-btn -> fill name/DOB/
    # card number -> search) — same selectors already used manually and
    # observed working in a real session (see msc_project_knowledge.md).
    VOYAGERS_CLUB_INSERT = "VOYAGERS_CLUB_INSERT"


class MscDiscountCandidate(BaseModel):
    """One discount to test. `label` must match real on-page text exactly
    (the dropdown option text, e.g. 'SENIOR DISCOUNT') — never a guessed
    or hardcoded percentage standing in for it."""

    label: str
    method: MscDiscountApplicationMethod
    # Only used when method == VOYAGERS_CLUB_INSERT. Real passenger
    # identity is required by MSC's own member-lookup form — never
    # invented; must come from this booking's own already-scraped
    # passenger data (see msc_commands.py's _extract_passengers).
    voyagers_first_name: Optional[str] = None
    voyagers_last_name: Optional[str] = None
    voyagers_dob: Optional[str] = None
    voyagers_card_number: Optional[str] = None
    cabin_number: str = "1"


class MscDiscountTestStatus(str, Enum):
    """EXPANDED 2026-08-13 after the first live test on 3000026 exposed
    two real bugs: (1) a hard requirement on rate-tab DOM presence that
    doesn't hold once a discount changes what MSC renders after Confirm,
    and (2) every early-return path collapsing to one generic
    INSUFFICIENT_DATA that discarded whatever had already been verified.
    Each value below corresponds to a distinct, real failure point in
    test_discount_candidate — deliberately NOT a superset of every status
    name ever proposed for this feature: NO_OPPORTUNITY/OPPORTUNITY_DETECTED/
    EXPERIMENT_NOT_REQUIRED belong to the cheap detection layer
    (evaluate_msc_booking), not this live-experiment layer, and are not
    duplicated here."""

    DISCOUNT_APPLICATION_FAILED = "DISCOUNT_APPLICATION_FAILED"
    CONFIRM_FAILED = "CONFIRM_FAILED"
    RECALCULATION_TIMEOUT = "RECALCULATION_TIMEOUT"
    POST_PRICE_NOT_FOUND = "POST_PRICE_NOT_FOUND"
    POST_PRICE_INVALID = "POST_PRICE_INVALID"
    IDENTITY_VALIDATION_FAILED = "IDENTITY_VALIDATION_FAILED"
    OCCUPANCY_MISMATCH = "OCCUPANCY_MISMATCH"
    # The post-test re-lookup of the real booking did NOT match the
    # captured baseline — a genuine safety alarm, not a normal negative
    # result. A caller must stop processing this booking and surface this
    # loudly rather than silently moving on.
    RESTORATION_FAILED = "RESTORATION_FAILED"
    # Full pipeline ran, MSC's own recalculated price was read and
    # validated, and restoration was verified — AND the resulting price is
    # meaningfully lower than baseline. `actual_savings` is only ever
    # populated (and only ever positive) in this state.
    CONFIRMED_OPTIMIZATION = "CONFIRMED_OPTIMIZATION"
    # Same full validation as CONFIRMED_OPTIMIZATION, but the verified
    # price was NOT lower than baseline — a real, fully-trustworthy
    # result, not a failure. Kept distinct from CONFIRMED_OPTIMIZATION so
    # a caller can never mistake "we tested it and it doesn't help" for
    # "we couldn't test it".
    CONFIRMED_NO_SAVINGS = "CONFIRMED_NO_SAVINGS"
    # Could not even begin a meaningful test (session expired, booking not
    # found, staging failed, occupancy auto-fix never converged) — never
    # guess a savings figure in this state.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


class MscDiscountTestResult(BaseModel):
    """The result of actually testing one discount candidate against a
    real MSC booking and reading MSC's own recalculated price — never a
    computed estimate. See core/calculator_msc.py's evaluate_msc_booking
    for the cheap, no-browser-interaction detection layer this complements
    (never replaces): that layer decides WHICH candidates are worth the
    cost of running this live test on.

    CONFIRMED REAL BUG, fixed 2026-08-13: every field below used to be
    silently reset to its default the instant ANY step failed, because
    the original code built a brand-new object from scratch on every
    early return. A booking whose discount selection and Confirm click
    both genuinely succeeded, but whose post-Confirm price couldn't be
    read, showed application_success=False and price_before=None in the
    persisted record — indistinguishable from a discount that was never
    even attempted. Every field here must now be threaded through from
    whatever was actually established before the failure, all the way to
    the final return — see test_discount_candidate's _evidence dict."""

    booking_id: str
    candidate_label: str
    method: MscDiscountApplicationMethod
    status: MscDiscountTestStatus

    price_before: Optional[float] = None
    price_after: Optional[float] = None
    # Only ever set when status is CONFIRMED_OPTIMIZATION (always > 0) or
    # CONFIRMED_NO_SAVINGS (<= 0). Enforced by the orchestration function
    # that builds this object, not by a validator here — kept simple and
    # auditable in one place.
    actual_savings: Optional[float] = None
    currency: str = "UNKNOWN"

    # CONFIRMED REAL GAP, added 2026-08-13 after a live retest of 3000026
    # produced a DIFFERENT price ($2,559.00) than a human had separately
    # observed live ($2,565.26) for the same discount on the same booking
    # — and this model had no way to tell which of
    # _wait_for_post_discount_price's two strategies ("category_listing"
    # vs "price_breakdown") actually produced price_after, making the
    # discrepancy undiagnosable after the fact. Never omit this again.
    price_source: Optional[str] = None

    # Occupancy actually used for this price test (from
    # _stage_booking_for_confirm's own occupancy_fix) — price identity
    # includes occupancy; a price obtained under the wrong headcount is
    # not comparable to baseline. See IDENTITY_VALIDATION_FAILED.
    occupancy: Optional[dict] = None
    category: Optional[str] = None
    # True only when a rate/promo tab was found AND confirmed matching;
    # False when no tabs existed at all (price was still read via a
    # fallback marker — see _wait_for_post_discount_price) — None when
    # never reached. Distinct from a hard requirement: absence of tab
    # confirmation is a lower-confidence signal, not an automatic failure.
    rate_tab_confirmed: Optional[bool] = None

    application_attempted: bool = False
    application_success: bool = False
    confirm_attempted: bool = False
    confirm_success: bool = False
    restoration_attempted: bool = False
    restoration_verified: bool = False
    reason: str = ""

    tested_at: datetime = Field(default_factory=datetime.utcnow)


# ── Scan Job ────────────────────────────────────────────────────


class ScanJob(BaseModel):
    """Tracks a batch scan request."""

    job_id: str
    booking_ids: list[str]
    cruise_line: CruiseLine
    status: ScanJobStatus = ScanJobStatus.PENDING
    results: list[BookingResult] = Field(default_factory=list)

    progress_done: int = 0
    progress_total: int = 0
    current_booking_id: Optional[str] = None

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
