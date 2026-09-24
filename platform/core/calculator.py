"""Price comparison and optimization engine.

Ported from calculator.js — the core business logic of the system.
Contains both ESPRESSO (Royal Caribbean / Celebrity) and NCL (Norwegian)
calculation engines.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .confidence import calc_confidence
from .goccl_fare_types import best_and_cheapest, rank_candidates
from .models import BookingResult, BookingStatus, CruiseLine


# ── Utility Functions ───────────────────────────────────────────


def safe_float(value) -> float:
    """Safely parse any value to float, defaulting to 0.

    CONFIRMED REAL RISK, flagged 2026-08-13 audit: this collapses "genuinely
    zero" and "missing/malformed, couldn't be read at all" into the same
    0.0 — for a SUM over many optional line items (package amounts, promo
    values), one bad entry silently contributing $0 to a running total is
    a defensible, conservative degradation, which is why this function is
    kept as-is and still used for those callers. It must NOT be used for a
    single required top-level figure that directly becomes old_total/
    new_total (see safe_float_or_none below, and _get_total's docstring)
    — there, a parse failure needs to be distinguishable from a real $0,
    since a wrongly-defaulted-to-zero total can silently produce a fake
    "optimization" or a fake "trap" instead of surfacing as unknown."""
    try:
        result = float(value)
        return 0.0 if result != result else result  # NaN check
    except (TypeError, ValueError):
        return 0.0


def safe_float_or_none(value) -> float | None:
    """Like safe_float, but a missing/malformed value returns None instead
    of silently becoming 0.0 — for the specific, narrow set of callers
    (see _get_total) where a REQUIRED figure's parse failure must never be
    indistinguishable from a real, legitimate zero amount. A genuine 0
    input still returns 0.0 here, never None — this only changes what
    happens when the value can't be read at all."""
    if value is None:
        return None
    try:
        result = float(value)
        return None if result != result else result  # NaN check
    except (TypeError, ValueError):
        return None


def round2(x) -> float:
    """Round to 2 decimal places, ROUND-HALF-UP.

    CONFIRMED INTENDED CONVENTION (2026-08-13 audit): this module's own
    docstring says it was "ported from calculator.js", whose round2 is
    `Math.round(x*100)/100`. The old Python implementation,
    `round(x*100)/100`, used Python's builtin `round()`, which uses
    round-HALF-TO-EVEN ("banker's rounding") — a real, silent divergence
    from the reference implementation this file claims to be a port of.

    Uses Python's `ROUND_HALF_UP` (ties round away from zero), which
    matches Excel/Google Sheets — the actual reconciliation partner for
    this tool's spreadsheet exports. Note this is NOT bit-identical to
    JS's `Math.round` on a NEGATIVE tie specifically: JS rounds a tie
    toward +infinity (`Math.round(-2.5) === -2`), while ROUND_HALF_UP
    rounds away from zero (`round2(-2.5) == -3`, matching Excel's
    `ROUND(-2.5,0)`). Real dollar inputs here are never negative before
    subtraction, and a subtraction landing exactly on a negative half-
    cent tie is vanishingly rare — documenting this precisely rather
    than silently picking one behavior, since it's the one place this
    fix doesn't have a single unambiguous "correct" answer.

    Also fixes a separate, compounding bug: the old implementation
    multiplied a float by 100 and rounded that float, which inherits
    binary floating-point representation error (e.g. 1.005 * 100 ==
    100.49999999999999, not 100.5 — round(100.49999999999999) == 100,
    silently returning 1.00 instead of the correct 1.01). Converting via
    `Decimal(str(value))` — the string round-trip, NOT `Decimal(value)`
    directly — sidesteps this: Python's float-to-str conversion already
    produces the shortest decimal string that round-trips to the same
    float, so `Decimal(str(1.005))` is the clean decimal 1.005, not its
    messy binary expansion.

    Verified byte-identical to the old implementation for every
    "normal" 2-decimal-or-fewer dollar value real scraped prices
    actually take (1332.46, 50.00, 37.50, 0.01, etc. — see
    test_calculator.py) — this only changes the answer for the rare
    exact-half-cent tie / float-representation-error inputs the old
    implementation got wrong."""
    value = safe_float(x)
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def total_optimization_savings(results) -> float:
    """The one safe way to sum `net_saving` across many BookingResults.

    KNOWN LIMITATION, flagged 2026-08-13 (Phase 0 correctness audit): this
    does NOT filter by BookingResult.currency. Every result summed here is
    still implicitly treated as USD regardless of whether its currency was
    actually verified ("UNKNOWN" is the honest default for NCL/GoCCL/MSC
    today — see core/models.py). Deliberately not filtered in this phase,
    to avoid silently dropping results from an existing total (a real
    behavior change) before there's a considered policy for what to DO
    with an unknown-currency result — that decision belongs to the
    Intelligence layer, not this fix. Recording currency honestly on each
    result is this phase's job; do not assume this sum is currency-safe.

    CONFIRMED INTENDED SEMANTICS (2026-08-13 audit): `net_saving` is
    deliberately a raw, signed net-difference figure —
    `price_drop + obc_change - lost_pkg_value` (ESPRESSO) or
    `price_drop - lost_addon_value` (NCL) — not a value that's already
    gated to "only when recommended." This is confirmed intentional, not
    an oversight: DOCUMENTATION.md's own GUI section already documents
    that NO_SAVING rows can carry a *negative* net_saving (a price
    increase), and this project's own two trap checks below
    (package-trap, OBC-loss-ratio) can just as legitimately produce a
    *positive* net_saving on a TRAP/NO_SAVING row — that's the entire
    point of those checks: catching a "win" that's smaller than what's
    being given up, not a case where no such number exists at all.

    The ONLY safe way to read this field in aggregate is to filter to
    OPTIMIZATION status first — summing across all statuses would
    silently count a rejected trap or a correctly-declined OBC trade as
    if it were a real dollar win. Every real consumer in this codebase
    (main.py, run_persistent_watchlist_scan.py, gui/windows.py,
    services/excel_export.py) already applied exactly this filter
    independently before this helper existed — this just gives all four
    one shared, impossible-to-forget implementation instead of four
    separately-maintained copies of the same filter. Extracting this
    does not change any of their existing results.

    CONFIRMED REAL MONEY-REPORTING BUG, fixed 2026-08-27. Querying the
    live DB found that **$4,100.00 of the $13,053.81 all-time reported
    savings — 31.4% — came from 5 GoCCL rows whose own note reads
    "UNCONFIRMED, run preview_fare_code to verify gross total" at
    confidence 1.** They were counted as realised savings anyway, because
    the GoCCL candidate flow reuses the OPTIMIZATION status (a known open
    item) and this sum filtered on status alone.

    Two of those bookings were re-scanned the same day and came back
    NO_SAVING — "cheapest candidate offer code isn't actually lower once
    guest count is applied" — so at least some were demonstrably false.
    DEMO02 was also counted TWICE ($880 and $500).

    A row that says "verify this before trusting it" is not a realised
    saving. That is not a business judgment call, it is a reporting error,
    so unconfirmed candidates are now excluded from the headline figure.
    They are NOT discarded — `unconfirmed_candidate_total()` reports them
    separately, so the opportunity stays visible without inflating money
    already banked.
    """
    return sum(
        r.net_saving for r in results
        if r.status.value == "OPTIMIZATION" and not _is_unconfirmed_candidate(r)
    )


def _is_unconfirmed_candidate(result) -> bool:
    """Whether a result declares ITSELF unverified.

    Matches on the note wording the producing code writes ("UNCONFIRMED")
    rather than on cruise line or confidence, so it stays correct if GoCCL
    gets its own status later, and cannot accidentally exclude a real
    ESPRESSO/NCL win that merely scored low.
    """
    return "unconfirmed" in (getattr(result, "note", "") or "").lower()


def unconfirmed_candidate_total(results) -> tuple[int, float]:
    """(count, dollars) of self-declared-unconfirmed OPTIMIZATION rows.

    Reported alongside the confirmed total so an unverified opportunity is
    visible as an ACTION ("go verify these") instead of silently padding a
    savings figure. See total_optimization_savings."""
    rows = [
        r for r in results
        if r.status.value == "OPTIMIZATION" and _is_unconfirmed_candidate(r)
    ]
    return len(rows), sum(r.net_saving for r in rows)


def norm_str(s: str | None) -> str:
    """Normalize a string: strip + uppercase."""
    return (s or "").strip().upper()


# ── ESPRESSO Fee Detection ──────────────────────────────────────

ESPRESSO_FEE_TYPES = frozenset([
    "VACATION_TOTAL", "OBC_TOTAL", "PORT_CHARGE", "PORT_EXPENSES",
    "GOVERNMENT_TAX", "TAXES_AND_FEES", "NCF", "NCCF", "CRUISE",
    "CRUISEFARE", "GRATUITIES", "TAX", "FEE",
    # Found mining a real 278-booking run (2026-07-31): these are invoice
    # structure/summary rows (subtotals, running totals, balance, deposit),
    # not real packages/perks. They weren't observed causing a live
    # misclassification (their names stay stable between old/new invoices)
    # but are excluded defensively now that we know they exist.
    "VACATION_SUBTOTAL", "VACATION_WITHOUT_COMPONENTS_SUBTOTAL",
    "VACATION_WITHOUT_COMPONENTS_TOTAL", "VACATION_NETTOTAL",
    "TAX_TOTAL", "COMPONENT", "BALANCE", "DEPOSIT", "DEPOSIT_TOTAL",
])

_FEE_NAME_PREFIX_RE = re.compile(r"^(NCCF|NCF|PORT|TAX|FEE|GOVERNMENT|GRATUIT)")

# Invoice item types seen in real data that aren't handled above and aren't
# expected either — logged once per distinct value so a future data-mining
# pass isn't the only way to notice a new one appearing.
_KNOWN_NON_FEE_TYPES = frozenset(["", "CRUISE_PROMO"])
_logged_unknown_types: set[str] = set()

# Minimum ratio of (price drop) to (OBC lost) before a repricing that
# forfeits OBC is treated as a genuine optimization rather than a wash.
OBC_LOSS_MIN_RATIO = 3.0


def _is_espresso_fee(item: dict) -> bool:
    """Check if an invoice item is a standard fee (not a package)."""
    item_type = norm_str(item.get("type", ""))
    if item_type in ESPRESSO_FEE_TYPES:
        return True
    name = norm_str(item.get("name", "") or item.get("normalizedName", ""))
    if _FEE_NAME_PREFIX_RE.match(name):
        return True
    if " OBC" in name or name.endswith("OBC") or name.startswith("OBC "):
        return True
    if item_type and item_type not in _KNOWN_NON_FEE_TYPES and item_type not in _logged_unknown_types:
        # A genuinely new invoice item type we haven't seen and classified
        # before — surface it instead of silently guessing, so a future gap
        # like this one gets noticed from a single log line instead of
        # needing another full manual data-mining pass.
        _logged_unknown_types.add(item_type)
        import logging
        logging.getLogger(__name__).warning(
            "espresso.unknown_invoice_type type=%r name=%r", item_type, item.get("name"),
        )
    return False


def _get_promo_value_by_name(items: list[dict]) -> dict[str, float]:
    """Sum CRUISE_PROMO-type invoice line amounts by normalized name, across
    every passenger. Confirmed against real data: CRUISE_PROMO lines are
    always tagged with a per-passenger paxId, never "total" (0 of 1,616
    real instances checked), so this is the only way to recover the real
    dollar value of a lost fare/promo code — oldFares/newFares only ever
    carries the name, never an amount. Amounts are typically negative
    (a discount), so losing one costs the client abs(amount) more."""
    values: dict[str, float] = {}
    for item in items:
        if norm_str(item.get("type", "")) != "CRUISE_PROMO":
            continue
        name = norm_str(item.get("name", "") or item.get("normalizedName", ""))
        if not name:
            continue
        values[name] = values.get(name, 0.0) + safe_float(item.get("amount", 0))
    return values


def _get_total(items: list[dict], fee_type: str) -> float | None:
    """Get the total-row amount for a specific fee type.

    CONFIRMED REAL RISK, fixed 2026-08-13: previously used safe_float,
    which silently returned 0.0 both when no matching row exists at all
    (a real, legitimate "this fee type doesn't apply here" — e.g. no
    OBC_TOTAL row when a booking genuinely has no OBC) AND when a matching
    row IS found but its `amount` field is missing/malformed (real data
    corruption — a wrong invoice value from the portal). Those are
    different facts. Now: no matching row -> 0.0 (unchanged, still a
    legitimate zero). A matching row whose amount can't be parsed -> None
    (NEW — the caller must treat this as unknown, never as a real $0),
    since VACATION_TOTAL/OBC_TOTAL feed directly into old_total/new_total/
    net_saving and a silently-wrong zero here can fabricate a fake
    optimization or a fake trap."""
    for item in items:
        if item.get("paxId") == "total" and norm_str(item.get("type", "")) == fee_type:
            return safe_float_or_none(item.get("amount"))
    return 0.0


def _get_cruise_fare(items: list[dict]) -> float:
    """Extract cruise fare from invoice items."""
    # Try direct match first
    for item in items:
        if item.get("paxId") == "total" and (item.get("type", "") or "") in (
            "CRUISE", "CRUISEFARE", "cruise"
        ):
            return safe_float(item.get("amount", 0))

    # Fallback: largest non-fee total
    skip = frozenset([
        "VACATION_TOTAL", "OBC_TOTAL", "TAXES_AND_FEES",
        "PORT_CHARGE", "PORT_EXPENSES", "GOVERNMENT_TAX", "NCF", "NCCF",
    ])
    best = 0.0
    for item in items:
        if item.get("paxId") != "total":
            continue
        if norm_str(item.get("type", "")) in skip:
            continue
        amount = safe_float(item.get("amount", 0))
        if amount > best:
            best = amount
    return best


def _get_packages(items: list[dict]) -> list[dict]:
    """Get all package (non-fee) items with positive amounts."""
    return [
        item
        for item in items
        if item.get("paxId") == "total"
        and safe_float(item.get("amount", 0)) > 0
        and not _is_espresso_fee(item)
    ]


# ── Travel Protection Detection ─────────────────────────────────
#
# CONFIRMED against real data (2026-08-25): mining all 532 captured
# ESPRESSO invoice responses (data/raw_responses.jsonl) for every distinct
# invoice-item name found exactly ONE real travel-protection-shaped line
# item — "GRP TVL PRTC" (64 occurrences, almost certainly "Group Travel
# Protection"). Before this fix it fell through to _get_packages() and
# was indistinguishable from an ordinary drink-package/Wi-Fi perk in
# lost_pkg_names — losing insurance/trip-protection coverage is a
# materially different, more consequential kind of loss (it can affect
# cancellation/refund eligibility, not just a bundled perk), so it
# deserves to be called out on its own rather than blended in. The
# additional patterns below are curated synonyms for the same real-world
# product category (industry-standard naming — CSA/Allianz/"CFAR" are
# common travel-protection product names/abbreviations), following this
# project's established practice of building a little defensively around
# one confirmed real example rather than coding to only the single exact
# string seen so far — same shape as MSC's negative-Due-Amount handling.
# None of these extras have been confirmed against a real captured
# invoice; if one is ever seen, note it here as confirmed.
_TRAVEL_PROTECTION_PATTERNS = [
    re.compile(r"\bTVL\s*PRTC\b", re.IGNORECASE),           # CONFIRMED real: "GRP TVL PRTC"
    re.compile(r"travel\s*protect", re.IGNORECASE),          # unconfirmed synonym
    re.compile(r"trip\s*protect", re.IGNORECASE),            # unconfirmed synonym
    re.compile(r"trip\s*insur", re.IGNORECASE),               # unconfirmed synonym
    re.compile(r"cancel(?:l)?ation\s*waiver", re.IGNORECASE), # unconfirmed synonym
    re.compile(r"\bCFAR\b", re.IGNORECASE),                   # "Cancel For Any Reason" — unconfirmed
]


def _is_travel_protection(item: dict) -> bool:
    """Whether an invoice item is a travel-protection/trip-insurance
    product rather than an ordinary package/perk — see the confirmed
    real example and rationale above."""
    name = item.get("name", "") or item.get("normalizedName", "") or ""
    return any(p.search(name) for p in _TRAVEL_PROTECTION_PATTERNS)


# ── Re-Addable Fare Detection ──────────────────────────────────

_READDABLE_PATTERNS = [
    re.compile(r"email", re.IGNORECASE),
    re.compile(r"bonus", re.IGNORECASE),
    re.compile(r"promo", re.IGNORECASE),
    re.compile(r"loyalty", re.IGNORECASE),
    re.compile(r"coupon", re.IGNORECASE),
    # Found mining a real 278-booking run (2026-07-31): an entire "SAV/SAVE"
    # family of fare codes (SAVEUPTO100 NRD, WEEKENDSAV NRD, BOOKNOWSAVNRD,
    # CANADA SAV NRD — 140+ combined occurrences) was falling through to
    # "truly lost" despite reading as the same kind of marketing promo as
    # the patterns above.
    re.compile(r"sav", re.IGNORECASE),
]

# BOGO60/BOGO75 NRD is the single most common lost fare in real data (536
# occurrences in one 278-booking run) and is deliberately NOT classified
# re-addable or truly-lost here — whether a buy-one-get-one offer can
# realistically be re-applied after a reprice is a real-world judgment call
# this project doesn't have an answer for yet, not a coding gap. Until
# confirmed, it's priced (via _get_promo_value_by_name below) and treated as
# truly lost, the conservative default — never silently ignored.


def _is_re_addable(fare_name: str) -> bool:
    """Check if a fare can likely be re-added after repricing."""
    return any(p.search(fare_name) for p in _READDABLE_PATTERNS)


# ── ESPRESSO Calculator ────────────────────────────────────────


def calculate_espresso(raw_data: dict, booking_id: str, price_category: str | None = None) -> BookingResult:
    """
    Analyze an ESPRESSO booking response and determine optimization status.

    This is the main ESPRESSO calculation engine, ported from calculateESPRESSO()
    in the original calculator.js.

    Args:
        raw_data: Raw API response from ESPRESSO reprice modal.
        booking_id: The booking ID.
        price_category: Current price category code.

    Returns:
        BookingResult with status, savings, confidence, and details.
    """
    try:
        data = raw_data.get("result", raw_data)
        old_items = (data.get("oldInvoice") or {}).get("invoiceItems", [])
        new_items = (data.get("newInvoice") or {}).get("invoiceItems", [])

        old_total = _get_total(old_items, "VACATION_TOTAL")
        new_total = _get_total(new_items, "VACATION_TOTAL")
        old_obc = _get_total(old_items, "OBC_TOTAL")
        new_obc = _get_total(new_items, "OBC_TOTAL")

        # CONFIRMED REAL RISK, fixed 2026-08-13: these four figures directly
        # become old_total/new_total/net_saving — a None here means a
        # VACATION_TOTAL/OBC_TOTAL row was FOUND but its amount could not be
        # parsed (real data corruption, not "this fee doesn't apply" — see
        # _get_total's docstring). Silently treating that as $0 could
        # fabricate a fake OPTIMIZATION (missing new_total looks like a
        # 100%-off price) or a fake TRAP. Never guess here — report ERROR,
        # the existing "don't trust this result" channel, exactly like any
        # other malformed-response failure this function already raises for.
        if old_total is None or new_total is None or old_obc is None or new_obc is None:
            raise ValueError(
                "invoice total/OBC amount could not be parsed from the portal response "
                "(VACATION_TOTAL or OBC_TOTAL row present but its amount field was missing "
                "or malformed) — refusing to guess a $0 substitute"
            )

        price_drop = round2(old_total - new_total)
        obc_change = round2(new_obc - old_obc)

        # Package loss detection
        old_pkgs = _get_packages(old_items)
        new_pkg_names = set(
            norm_str(i.get("name", "") or i.get("normalizedName", ""))
            for i in _get_packages(new_items)
        )
        new_pkg_names.discard("")

        lost_pkgs = [
            i for i in old_pkgs
            if norm_str(i.get("name", "") or i.get("normalizedName", ""))
            and norm_str(i.get("name", "") or i.get("normalizedName", "")) not in new_pkg_names
        ]
        # lost_pkg_value stays a sum over ALL lost items (travel protection
        # included) — the financial net_saving math is unchanged by this
        # split. Only the NAMES are separated, so a human reviewing the
        # result sees travel-protection loss called out distinctly from an
        # ordinary lost drink/Wi-Fi package — see _is_travel_protection.
        lost_pkg_value = round2(sum(safe_float(i.get("amount", 0)) for i in lost_pkgs))
        lost_pkg_names = [
            i.get("name", "") or i.get("normalizedName", "")
            for i in lost_pkgs
            if (i.get("name") or i.get("normalizedName")) and not _is_travel_protection(i)
        ]
        lost_travel_protection = [
            f"{i.get('name', '') or i.get('normalizedName', '')} (${safe_float(i.get('amount', 0)):.2f})"
            for i in lost_pkgs
            if (i.get("name") or i.get("normalizedName")) and _is_travel_protection(i)
        ]

        # Fare analysis (moved before `net` — a truly-lost fare's real
        # dollar cost now needs to fold into lost_pkg_value first)
        old_fare_names = [f.get("name", "") for f in (data.get("oldFares") or []) if f.get("name")]
        new_fare_names = [f.get("name", "") for f in (data.get("newFares") or []) if f.get("name")]
        new_fare_set = set(norm_str(f) for f in new_fare_names)
        old_fare_set = set(norm_str(f) for f in old_fare_names)
        all_lost_fares = [f for f in old_fare_names if norm_str(f) not in new_fare_set]
        re_addable_fares = [f for f in all_lost_fares if _is_re_addable(f)]
        truly_lost_fares = [f for f in all_lost_fares if not _is_re_addable(f)]
        gained_fares = [f for f in new_fare_names if norm_str(f) not in old_fare_set]

        # A truly-lost fare (e.g. a BOGO discount) used to contribute $0 to
        # net_saving — its real dollar value lives in CRUISE_PROMO invoice
        # lines, tracked separately from the name-only oldFares list, and
        # was never being cross-referenced. Confirmed against real data:
        # losing a BOGO60/75 NRD fare is worth $394-$2,833 (avg ~$1,756).
        old_promo_values = _get_promo_value_by_name(old_items)
        priced_lost_fares = []
        for fare_name in truly_lost_fares:
            promo_amount = old_promo_values.get(norm_str(fare_name))
            if promo_amount:
                priced_lost_fares.append((fare_name, abs(round2(promo_amount))))
        lost_fare_value = round2(sum(v for _, v in priced_lost_fares))
        if lost_fare_value:
            lost_pkg_value = round2(lost_pkg_value + lost_fare_value)
            lost_pkg_names = lost_pkg_names + [
                f"{name} (${value:.2f})" for name, value in priced_lost_fares
            ]

        net = round2(price_drop + obc_change - lost_pkg_value)

        # Status determination
        re_add_note = (" — re-add: " + ", ".join(re_addable_fares)) if re_addable_fares else ""
        # Surfaced regardless of status/branch below — a client losing trip
        # insurance/travel-protection coverage is worth flagging even on a
        # NO_SAVING or TRAP result, not just an OPTIMIZATION.
        protection_note = (
            f" — ALSO LOSES TRAVEL PROTECTION: {', '.join(lost_travel_protection)} "
            "(confirm this doesn't affect cancellation/trip-insurance coverage before repricing)"
        ) if lost_travel_protection else ""

        if net > 0 and lost_pkg_value > 0 and net < lost_pkg_value:
            # Net saving is positive on paper, but it's smaller than the
            # value of a package being given up to get it — the client is
            # trading a perk worth more than the "win" itself. Confirmed
            # against a real case: $50 net saving from losing a $594
            # all-inclusive drink package is not a real optimization.
            status = BookingStatus.TRAP
            note = f"trap - losing ${round(lost_pkg_value)} perk for only ${round(net)} net{re_add_note}"
        elif net > 0 and obc_change < 0 and price_drop < abs(obc_change) * OBC_LOSS_MIN_RATIO:
            # Net is positive on paper, but a chunk of it is OBC being
            # forfeited rather than a real fare reduction — confirmed
            # against a real case: a $300 price drop that cost $250 of
            # OBC (net $50) is only a ~1.2x margin, not a safe trade.
            # Only worth recommending once the price drop clears the OBC
            # being given up by OBC_LOSS_MIN_RATIO.
            status = BookingStatus.NO_SAVING
            note = (
                f"no saving — ${round(price_drop)} drop costs ${round(abs(obc_change))} OBC "
                f"(need {OBC_LOSS_MIN_RATIO:.0f}x){re_add_note}"
            )
        elif net > 0:
            status = BookingStatus.OPTIMIZATION
            note = f"optimized ${round(net)}{re_add_note}"
        elif price_drop > 0 and net <= 0:
            status = BookingStatus.TRAP
            note = f"trap - do not reprice{re_add_note}"
        else:
            status = BookingStatus.NO_SAVING
            extra = (" — can re-add: " + ", ".join(re_addable_fares)) if re_addable_fares else ""
            note = f"no saving{extra}"

        note = note + protection_note

        # Confidence scoring
        old_cruise = _get_cruise_fare(old_items)
        new_cruise = _get_cruise_fare(new_items)
        conf = calc_confidence(old_cruise, new_cruise, net, old_total, lost_pkg_value, obc_change)

        # CONFIRMED REAL BUG, found 2026-08-27 by querying the live DB:
        # 89 rows are TRAP or NO_SAVING while carrying confidence 4 or 5.
        # Real examples — booking 3000061 TRAP net=$297 conf=5, booking
        # 3000043 TRAP net=$588 conf=5, booking 3000042 TRAP net=$768
        # conf=4. calc_confidence() never receives the final `status`: it
        # scores only fare direction, net %, and package/OBC stability
        # (see core/confidence.py), so a booking with a clean fare drop
        # scores high EVEN WHEN the surrounding rules concluded "do not do
        # this." Anything ranking by confidence — the GUI's sortable
        # Conf column, the Excel report — then puts a $588 trap at the top
        # next to genuine wins.
        #
        # A rejection is not a high-confidence opportunity. Capped rather
        # than overwritten so a legitimately LOW score stays low, and
        # matching the convention already used for NCL's protected-promo
        # gate (scored 1) and NCL's own OBC rejections (scored 2).
        if status in (BookingStatus.TRAP, BookingStatus.NO_SAVING):
            conf.score = min(conf.score, 2)

        return BookingResult(
            cruise_line=CruiseLine.ESPRESSO,
            status=status,
            note=note,
            booking_id=booking_id,
            price_category=price_category,
            old_total=old_total,
            new_total=new_total,
            price_drop=price_drop,
            obc_change=obc_change,
            net_saving=net,
            lost_pkg_value=lost_pkg_value,
            lost_pkg_names=lost_pkg_names,
            lost_travel_protection=lost_travel_protection,
            lost_fares=truly_lost_fares,
            re_addable_fares=re_addable_fares,
            gained_fares=gained_fares,
            confidence=conf.score,
            old_cruise_fare=conf.old_cruise_fare,
            new_cruise_fare=conf.new_cruise_fare,
            fare_change_pct=conf.fare_change_pct,
        )

    except Exception as e:
        return BookingResult(
            cruise_line=CruiseLine.ESPRESSO,
            status=BookingStatus.ERROR,
            error=str(e),
            booking_id=booking_id,
            price_category=price_category,
        )


# ── NCL Addon Valuation ────────────────────────────────────────

NCL_ADDON_VALUES: dict[str, int] = {
    "wi-fi": 150, "wifi": 150, "internet": 150,
    "dining": 80, "specialty dining": 80, "restaurant": 80,
    "beverage": 200, "bar": 200, "drink": 200, "open bar": 200,
    "excursion": 50, "shore": 50,
}

_DOLLAR_PATTERN = re.compile(r"(?:\$|usd\s*)\s*([\d,]+(?:\.\d{1,2})?)")


def _ncl_addon_value(addon_name: str | None) -> float:
    """Estimate dollar value of an NCL addon by its name.

    CONFIRMED REAL BUG, fixed 2026-08-13: the old regex `\\$(\\d+)` only
    matched a literal '$' immediately followed by digits — no decimal
    point, no thousands separator. A real addon literally named
    "$149.99 Beverage Package" matched only "149", silently discarding
    the ".99" and UNDERSTATING the addon's real value by up to $0.99.
    Since lost_addon_value is SUBTRACTED in `net = price_drop -
    lost_addon_value`, understating it OVERSTATES net — the opposite of
    conservative (the wrong direction for a value meant to represent
    what the client is giving up). Now also accepts a comma thousands
    separator ("$1,249.99") and a bare "USD 149.99" prefix (no $ sign at
    all), while still falling back to the keyword-based
    NCL_ADDON_VALUES estimate table when no dollar figure appears in the
    name at all. Returns float, not int, to preserve cents."""
    lower = (addon_name or "").lower()
    match = _DOLLAR_PATTERN.search(lower)
    if match:
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            pass  # regex guarantees digits/commas/one decimal point, but never trust a parse blindly
    for key, val in NCL_ADDON_VALUES.items():
        if key in lower:
            return float(val)
    return 0.0


# ── NCL promo-loss hard rules ───────────────────────────────────
#
# HARD RULE, stated directly by the project owner 2026-08-26:
#
#   "if LATRIPLE is before only we do not optimize the booking, and we
#    optimize if it is after, or if a booking does not have LATRIPLE
#    before but does have it after then we optimize also"
#
# Restated as the single condition that actually matters: a booking must
# NEVER be recommended for repricing when LATRIPLE is present BEFORE and
# absent AFTER — i.e. when the reprice would LOSE it. Every other
# combination is fine:
#   before=Y after=N -> BLOCK (this is the whole point of the rule)
#   before=Y after=Y -> allow (kept, nothing lost)
#   before=N after=Y -> allow (gained -- explicitly called out as OK)
#   before=N after=N -> allow (never involved)
#
# This is deliberately a HARD gate, not a value subtraction: unlike a
# lost addon (which has a dollar figure that can be netted off), the
# instruction here is categorical -- don't recommend it at all. Real
# confirmed example, booking 3000003: promos went from
# "...EASYFARE, LATRIPLE, MAP10OFF..." to "...EASYFARE | LATREW |
# MAP10OFF..." -- LATRIPLE lost, LATREW gained -- while the fare dropped
# $72. Under this rule that $72 is NOT a recommendable saving.
#
# WHAT LATRIPLE ACTUALLY IS (researched 2026-08-26, moderate-high
# confidence — it's an internal B2B promo marker with no official NCL
# documentation, so this comes from corroborating travel-agent/cruiser
# discussion rather than a primary source): LATRIPLE = **triple Latitudes
# points** (NCL's loyalty program is "Latitudes Rewards"), applying to
# guests 1-2 on the reservation. LATREW, which replaced it on the real
# 3000003 example, is the BASELINE marker for standard 1-point-per-night
# accrual. So a LATRIPLE -> LATREW swap is a silent downgrade from 3x to
# 1x loyalty points.
#
# Why that justifies a hard block rather than a dollar subtraction:
# the loss has no invoice line item to net off, it's irreversible once
# the booking is repriced, and Latitudes tiers gate real recurring
# benefits (free bags, priority, dining/excursion credits). Trading ~14
# extra points per guest on a 7-night sailing for a small fare drop is a
# decision the CLIENT would have to consent to — not something to
# recommend automatically.
#
# Kept as a named list + helper (rather than inline) so more codes can be
# added if the project owner identifies others that must never be lost.
#
# FREESRVC ADDED 2026-08-26 on the project owner's explicit instruction
# ("this is the same case like latriple FREESRVC add rule please"), after
# it was flagged as a candidate and researched.
#
# WHAT FREESRVC IS — this one is BETTER documented than LATRIPLE, from a
# real NCL travel-agent promo flyer rather than inference: **"Free
# Pre-Paid Service Charges"**, guests 1-2, Balcony and above, new FIT
# bookings only (excludes BX/MX guarantee categories and Studio/Inside/
# Oceanview). Service charges run roughly $20-25 per person per night, so
# on a real sailing this is plausibly $300-500 of value — an order of
# magnitude larger than the fare drops being traded for it.
#
# Confirmed real behavior, both live-verified 2026-08-26: on bookings
# 3000007 and 3000006 the reprice REPLACED "FREE PREPAID SERVICE
# CHARGES" with a "Free $50 / $37.50 On-Board Credit Certificate" — i.e.
# gave up the larger prepaid-service-charge benefit for a much smaller
# OBC certificate, while the fare dropped only $20 and $60 respectively.
# Both were reported as OPTIMIZATION before this rule; both are now TRAP.
#
# Same enforcement shape as LATRIPLE and for the same reason: no invoice
# line item to net off, and irreversible once repriced — so it's a hard
# gate, not a value subtraction.
# Promos NCL bookings must NEVER give up on a reprice, whatever the saving
# looks like. A booking that would lose one of these is a hard TRAP, decided
# BEFORE any status is assigned (see calculate_ncl) and scored confidence 1
# so it can never sort near a real opportunity.
#
# Each entry is here because the project owner said so - never inferred:
#   LATRIPLE   2026-08-26  "if LATRIPLE is before only we do not optimize"
#   FREESRVC   2026-08-27  "this is the same cse like latriple FREESRVC"
#   FITOBC     2026-08-28  an ON-BOARD-CREDIT promo. Found by querying that
#                          day's run: lost on 4 of 36 OPTIMIZATIONs worth
#                          $1,532 combined (3000046 $456, 3000047 $410,
#                          3000045 $391.20, 3000051 $275). Unlike an addon
#                          named "Free $100 On-Board Credit Certificate", a
#                          promo CODE carries no readable value on the page,
#                          so the loss could never be priced - which is
#                          exactly why it has to be gated instead.
#   LATDBLX    2026-08-28  Latitudes DOUBLE points. Same family as the
#                          already-documented LATRIPLE -> LATREW downgrade;
#                          lost on booking 3000048 for a $10 "saving".
#
# NOT included, and deliberately so: LATREW / LATITUDE also appear in the
# data (37 and 11 times on 2026-08-28) and are plausibly in the same
# loyalty family, but the owner has not ruled on them - and over-gating
# silently destroys real savings. Ask before adding.
NCL_NEVER_LOSE_PROMOS: frozenset[str] = frozenset({
    "LATRIPLE", "FREESRVC", "FITOBC", "LATDBLX",
})


def _split_promo_codes(promos: str | None) -> set[str]:
    """Parse an NCL promo string into a set of uppercased codes.

    Real formats confirmed 2026-08-26 from live data: the booking header
    reads comma-separated ("DISC50, EASYFARE, LATRIPLE, SHX50") while the
    category-grid row reads pipe-separated ("DISC50 | EASYFARE | LATREW").
    Both are handled, along with stray whitespace/empties."""
    if not promos:
        return set()
    text = promos.replace("|", ",")
    return {part.strip().upper() for part in text.split(",") if part.strip()}


def ncl_lost_promos(old_promos: str | None, new_promos: str | None) -> list[str]:
    """EVERY promo present before a reprice and gone after it.

    ADDED 2026-08-28 from real run data. Only LATRIPLE and FREESRVC were
    ever looked at (NCL_NEVER_LOSE_PROMOS), so any OTHER promo loss was
    completely invisible to the verdict. Querying the 2026-08-28 NCL run
    showed that is not a rare edge case: **11 of 36 OPTIMIZATIONs lost at
    least one promo**, and the lost ones carry real money -

        FITOBC   x4   (an ON-BOARD CREDIT promo - exactly the OBC case)
        AF15OFF  x4   (15% off)
        DISC35   x2   (swapped for DISC50, which is better)
        DASHSALE, SHX50, 34CHO, LATDBLX (Latitudes double points)

    Booking 3000046 is the concrete example Neon queried: reported
    "$456 saved" while losing FITOBC, whose value is nowhere in that figure.

    Deliberately returns the RAW list and prices nothing. NCL promo codes
    carry no readable value on the page (unlike an addon named
    "Free $100 On-Board Credit Certificate"), and inventing a value per
    code is precisely the estimation mistake the free-upgrade incident
    proved cannot be trusted. The caller warns instead.
    """
    before = _split_promo_codes(old_promos)
    after = _split_promo_codes(new_promos)
    return sorted(before - after)


def ncl_lost_protected_promos(old_promos: str | None, new_promos: str | None) -> list[str]:
    """Protected promo codes present BEFORE but missing AFTER.

    Returns a sorted list (empty when nothing protected is lost). See
    NCL_NEVER_LOSE_PROMOS for the rule and its rationale."""
    before = _split_promo_codes(old_promos)
    after = _split_promo_codes(new_promos)
    return sorted((before & NCL_NEVER_LOSE_PROMOS) - after)


# ── NCL Calculator ──────────────────────────────────────────────


_NCL_OBC_CERT_RE = re.compile(
    r"On-Board Credit Certificate|OBC Certificate", re.IGNORECASE
)


def ncl_lost_addons(
    before: list[dict] | None, after: list[dict] | None
) -> list[dict]:
    """Addons present BEFORE a reprice and gone AFTER it.

    Keyed on (guest, name) — the same key `_summarize_addon_change` uses in
    scraper/ncl.py — because the same perk legitimately appears once per
    guest and a name-only key would collapse a 2-guest loss into one.
    """
    before = before or []
    after = after or []
    after_keys = {(a.get("guest", ""), a.get("name", "")) for a in after}
    lost: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for a in before:
        key = (a.get("guest", ""), a.get("name", ""))
        # DE-DUPLICATE on the full key. The portal can render the same
        # line twice for one guest, and counting it twice would double the
        # subtracted value ($300 for one $150 certificate) — the behaviour
        # the pre-existing test_ncl_duplicate_addons_counted_once protects.
        # Note this dedupes per (guest, name), so the SAME perk held by TWO
        # DIFFERENT guests is still correctly counted twice.
        if key in seen or key in after_keys:
            continue
        seen.add(key)
        lost.append(a)
    return lost


def ncl_price_lost_addons(lost: list[dict]) -> dict:
    """Split lost addons into REAL-valued and ESTIMATE-ONLY.

    CRITICAL distinction, and the reason this isn't one flat sum: an addon
    named "Free $100 On-Board Credit Certificate" carries its REAL value in
    its own name, which `_ncl_addon_value` parses. An addon named
    "Unlimited Open Bar Package" does not — `_ncl_addon_value` falls back to
    the NCL_ADDON_VALUES *estimate* table for those.

    Only real, name-embedded figures are allowed to drive the dollar
    verdict. Estimated constants are surfaced by NAME as an unpriced loss
    instead. That is the direct lesson of the free-upgrade false-positive
    incident (see A0 in the upgrade-backlog memory): a well-calibrated
    estimate was still wrong often enough to flip the sign on close cases,
    and only a real figure can be trusted for a money decision.
    """
    obc_value = 0.0
    obc_names: list[str] = []
    priced_value = 0.0
    priced_names: list[str] = []
    unpriced_names: list[str] = []

    for a in lost:
        name = a.get("name", "") or ""
        guest = a.get("guest", "") or ""
        label = f"{guest}: {name}" if guest else name
        has_real_value = bool(_DOLLAR_PATTERN.search(name.lower()))
        value = _ncl_addon_value(name) if has_real_value else 0.0

        if _NCL_OBC_CERT_RE.search(name):
            if value > 0:
                obc_value += value
                obc_names.append(f"{label} (${value:g})")
            else:
                # An OBC certificate with no readable amount is still a
                # real OBC loss — name it rather than silently dropping it.
                unpriced_names.append(label)
        elif value > 0:
            priced_value += value
            priced_names.append(f"{label} (${value:g})")
        else:
            unpriced_names.append(label)

    return {
        "obc_value": round2(obc_value),
        "obc_names": obc_names,
        "priced_value": round2(priced_value),
        "priced_names": priced_names,
        "unpriced_names": unpriced_names,
    }


def ncl_final_payment_passed(final_payment_date, today=None) -> bool:
    """Whether NCL's FINAL PAYMENT date is already in the past.

    HARD RULE, stated by Neon 2026-08-28: "from now on we are adding a new
    rule if the final payment date has passed on ncl we do not optimize the
    booking at all because it will cause a penality".

    Read from the real payment-schedule table on the booking summary
    ("Explanation | Payment Due Date | Amount" -> "FINAL PAYMENT |
    01/09/2027 | $2,028.10"), confirmed present in captured pages.

    Returns False when the date is missing or unparseable - the gate must
    never fire on a value we could not read, because refusing a booking on
    a guess is its own kind of wrong. The caller reports the unknown.
    """
    if not final_payment_date:
        return False
    if isinstance(final_payment_date, str):
        parsed = None
        try:
            import dateparser

            parsed = dateparser.parse(
                final_payment_date, settings={"DATE_ORDER": "MDY"},
            )
        except Exception:
            parsed = None
        if parsed is None:
            return False
        final_payment_date = parsed
    reference = today or datetime.utcnow()
    try:
        return final_payment_date.date() < reference.date()
    except Exception:
        return False


def realizable_saving(price_drop: float, amount_due: float | None) -> float:
    """How much of a price drop the agency can ACTUALLY collect.

    STATED BY NEON 2026-08-28 for booking 3000057: "i did optimize with
    the 100 but we actually get 43 not 100 because the customer has paid in
    full" - the booking showed a $100 drop with only $43 still outstanding.

    A reprice reduces what is still OWED. Once the balance reaches zero
    there is nothing further to collect, so the benefit is capped by the
    amount due. Reporting the full $100 overstates a real recovery by
    $57 - more than the true figure itself.

    `amount_due is None` means we could not read it, so NOTHING is capped
    and the caller says so. Never invent a cap.
    """
    if amount_due is None:
        return round2(price_drop)
    return round2(min(price_drop, max(0.0, amount_due)))


def ncl_commission_loss(price_drop: float, commission_rate: float | None) -> float:
    """Commission the agency gives up by lowering the fare.

    RAISED BY NEON 2026-08-28 on booking 3000054: "it is good and worth it
    nut the problem is i do not feel like it as well as we will lose 48$
    comission".

    The rate is READ per booking, never assumed: NCL's own summary prints
    "Com.Due $250.90" against "Gross Due $1,793.10" (13.99% on that real
    capture), and Neon's $48 against a $300 drop implies ~16% on his -
    i.e. it genuinely varies, so a hardcoded rate would be wrong on most
    bookings. Returns 0.0 when the rate is unknown rather than guessing.
    """
    if not commission_rate or commission_rate <= 0:
        return 0.0
    return round2(price_drop * commission_rate)


def calculate_ncl(
    booking_id: str,
    price_category: str | None,
    invoice_total: float,
    new_res_total: float,
    addons: list[dict] | None = None,
    old_promos: str = "",
    new_promos: str = "",
    new_addons: list[dict] | None = None,
    addon_scrape_failed: bool = False,
    amount_due: float | None = None,
    final_payment_date: str | None = None,
    commission_rate: float | None = None,
    balance_is_all_commission: bool = False,
) -> BookingResult:
    """
    Analyze an NCL booking and determine optimization status.

    Ported from calculateNCL() in the original calculator.js.

    Args:
        booking_id: The booking ID.
        price_category: Current category code.
        invoice_total: Current invoice total.
        new_res_total: New total after category switch.
        addons: List of addon dicts with 'name' and 'qty'.
        old_promos: Current promo codes string.
        new_promos: New promo codes string.

    Returns:
        BookingResult with status, savings, and details.
    """
    try:
        old_total = round2(invoice_total)
        new_total = round2(new_res_total)
        price_drop = round2(old_total - new_total)

        # CONFIRMED REAL FALSE POSITIVES, fixed 2026-08-27 - reported by
        # Neon on two live bookings:
        #   3000055  $57 "saving" while LOSING a $100 OBC certificate
        #   3000054  $60 "saving" while LOSING a $50  OBC certificate
        # Both came back GREEN as OPTIMIZATION at confidence 5/5.
        # 3000055 is really a $43 net LOSS.
        #
        # Root cause: OBC loss was inferred from a PROMO SUBSTRING -
        #     lost_fobc = "FOBC" in old_promos and "FOBC" not in new_promos
        # - and the certificate was priced ONLY if that fired. Neither
        # booking's promo string contained "FOBC", so lost_addon_value
        # stayed 0.00 and the entire price drop counted as clean net.
        # Meanwhile the scraper's OWN before/after addon diff had already
        # identified the lost certificate correctly and put it in the note,
        # where it affected no decision. (This is the "lost_fobc string
        # defect" previously logged as a known open issue - it was live.)
        #
        # Now driven by the REAL before/after addon diff, with OBC loss
        # routed into obc_change so the project's single canonical OBC rule
        # (OBC_LOSS_MIN_RATIO, shared with ESPRESSO) actually applies.
        # `new_addons is None` means the caller has NO "after" list to
        # compare against — the no-price-change and price-increase early
        # returns, where the booking was never touched, so nothing CAN have
        # been forfeited. Without this guard the diff treats "after" as
        # empty and prices the ENTIRE before-list as lost, inventing a $100
        # OBC loss on a booking nobody repriced (caught by
        # test_no_after_list_means_no_loss_is_invented). An empty LIST is
        # different from None and is a real answer: everything was lost.
        # `addon_scrape_failed` means we TRIED to read the addon tables and
        # could not (see NclScraper._scrape_addons). That is different from
        # having no "after" list because the booking was never touched.
        # Both suppress loss pricing — you cannot price a loss from data you
        # do not have — but a FAILURE must additionally be surfaced, because
        # a real forfeited OBC certificate could be hiding behind it and the
        # result would otherwise look like a clean, confident win.
        if new_addons is None or addon_scrape_failed:
            priced = {
                "obc_value": 0.0, "obc_names": [],
                "priced_value": 0.0, "priced_names": [],
                "unpriced_names": [],
            }
        else:
            priced = ncl_price_lost_addons(ncl_lost_addons(addons, new_addons))
        obc_lost = priced["obc_value"]
        lost_addon_value = priced["priced_value"]
        lost_addon_names = priced["obc_names"] + priced["priced_names"]
        unpriced_lost = priced["unpriced_names"]

        # Negative, matching ESPRESSO's sign convention: obc_change is the
        # CHANGE in OBC, so forfeiting OBC is a negative change.
        obc_change = round2(-obc_lost)

        net = round2(price_drop + obc_change - lost_addon_value)

        # Everything actually forfeited, priced. Used by the confidence
        # arms below, which previously looked only at lost_addon_value and
        # therefore ignored OBC entirely.
        total_lost_value = round2(obc_lost + lost_addon_value)

        # HARD GATE 2, Neon 2026-08-28: NCL charges a PENALTY for
        # repricing after the final payment date, so such a booking is not
        # optimizable at all regardless of how good the drop looks.
        final_payment_passed = ncl_final_payment_passed(final_payment_date)

        # Cap the win at what is still collectable, and price the
        # commission given up. See realizable_saving / ncl_commission_loss.
        realizable = realizable_saving(price_drop, amount_due)
        capped_by_balance = (
            amount_due is not None and realizable < price_drop - 0.01
        )
        commission_loss = ncl_commission_loss(price_drop, commission_rate)

        # HARD GATE, project owner's rule 2026-08-26 — checked BEFORE any
        # status is assigned, so a protected-promo loss can never come
        # back as OPTIMIZATION regardless of how good the fare drop
        # looks. See NCL_NEVER_LOSE_PROMOS for the full rule.
        lost_protected = ncl_lost_protected_promos(old_promos, new_promos)

        # Status determination
        if lost_protected:
            status = BookingStatus.TRAP
            note = (
                f"NCL do NOT reprice — would LOSE {', '.join(lost_protected)} "
                f"(present before, gone after) for only ${round(net)}; "
                f"this promo must never be given up on a reprice"
            )
        elif final_payment_passed:
            # Not a "trap" in the perk-loss sense - an eligibility block.
            # Reported as TRAP so it can never surface as a recommended
            # win, with an unmistakable reason.
            status = BookingStatus.TRAP
            note = (
                f"NCL do NOT reprice - the FINAL PAYMENT DATE "
                f"({final_payment_date}) has already PASSED; repricing now "
                f"incurs a penalty. Rule set by the project owner "
                f"2026-08-28."
            )
        elif net > 0 and lost_addon_value > 0 and net < lost_addon_value:
            # Positive on paper but smaller than the perk being given up to
            # get it - the same package-trap rule ESPRESSO already applies.
            status = BookingStatus.TRAP
            note = (
                f"NCL trap - losing ${round(lost_addon_value)} of perks for "
                f"only ${round(net)} net: {', '.join(lost_addon_names)}"
            )
        elif net > 0 and obc_change < 0 and price_drop < abs(obc_change) * OBC_LOSS_MIN_RATIO:
            # THE 3000054 case: a $60 drop that forfeits $50 of OBC is a
            # 1.2x margin, not a safe trade. Uses the project's single
            # canonical OBC_LOSS_MIN_RATIO so NCL and ESPRESSO cannot drift.
            status = BookingStatus.NO_SAVING
            note = (
                f"NCL no saving - ${round(price_drop)} drop costs "
                f"${round(abs(obc_change))} OBC (need "
                f"{OBC_LOSS_MIN_RATIO:.0f}x): {', '.join(lost_addon_names)}"
            )
        elif net > 0:
            status = BookingStatus.OPTIMIZATION
            addon_note = (
                " - verify addons: " + ", ".join(lost_addon_names)
            ) if lost_addon_names else ""
            # Perks whose value is only an ESTIMATE are never priced into
            # net (see ncl_price_lost_addons), so an optimization that loses
            # one must say so plainly instead of presenting an unqualified
            # dollar win.
            unpriced_note = (
                " - ALSO LOSES (value not readable, verify by hand): "
                + ", ".join(unpriced_lost)
            ) if unpriced_lost else ""
            note = f"NCL optimized ${round(net)}{addon_note}{unpriced_note}"
        elif price_drop > 0 and net <= 0:
            status = BookingStatus.TRAP
            lost_desc = (
                ", ".join(lost_addon_names)
                or ", ".join(unpriced_lost)
                or "addon loss"
            )
            note = (
                f"NCL trap - ${round(price_drop)} price drop wiped out by "
                f"${round(obc_lost + lost_addon_value)} lost: {lost_desc}"
            )
        else:
            status = BookingStatus.NO_SAVING
            note = "NCL no saving"

        if status == BookingStatus.OPTIMIZATION and capped_by_balance:
            note += (
                f" - COLLECTABLE ONLY ${round(realizable)}: the client still "
                f"owes ${round(amount_due or 0)}, and a reprice can only "
                f"reduce the outstanding balance. The ${round(net)} above is "
                f"the price movement, not what is recoverable."
            )
        if status == BookingStatus.OPTIMIZATION and balance_is_all_commission:
            # Booking 3000049: Gross Due $163.00, Com.Due $163.00,
            # Net Due $0.00. The client owes nothing to the cruise line -
            # the whole remaining balance is the agency's own commission, so
            # a reprice cannot save them anything and only shrinks that.
            note += (
                " - WARNING: the entire outstanding balance is COMMISSION "
                "(Com.Due equals Gross Due, Net Due is zero). Repricing "
                "cannot save the client anything here; it only reduces what "
                "the agency collects."
            )

        if status == BookingStatus.OPTIMIZATION and commission_loss > 0:
            note += (
                f" - COSTS ${round(commission_loss)} OF COMMISSION "
                f"(at the booking's own {round((commission_rate or 0) * 100)}% "
                f"rate), so the net gain to the agency is "
                f"${round(realizable - commission_loss)}."
            )

        # ANY lost promo must be visible on an OPTIMIZATION, not just the
        # two hard-gated ones. See ncl_lost_promos for the real numbers.
        lost_promos = [
            p for p in ncl_lost_promos(old_promos, new_promos)
            if p not in NCL_NEVER_LOSE_PROMOS
        ]
        if lost_promos and status == BookingStatus.OPTIMIZATION:
            gained_promos = sorted(
                _split_promo_codes(new_promos) - _split_promo_codes(old_promos)
            )
            note += (
                " - LOSES PROMO(S): " + ", ".join(lost_promos)
                + (" (gains " + ", ".join(gained_promos) + ")" if gained_promos else "")
                + ". Promo codes carry no readable value on the page, so this is"
                + " NOT reflected in the figure above - check what they are worth"
                + " before repricing."
            )

        if addon_scrape_failed and status == BookingStatus.OPTIMIZATION:
            note += (
                " - WARNING: the addon list could not be read, so any OBC or "
                "package loss is UNKNOWN and is NOT reflected in this figure. "
                "Verify by hand before repricing."
            )

        # Confidence scoring (simplified for NCL)
        if final_payment_passed:
            # Not a confidence question - a flat "not eligible".
            confidence = 1
        elif lost_protected:
            # Not a confidence question — this is a hard "don't do it."
            # Scored lowest so it can never sort near a real opportunity
            # in any report that ranks by confidence.
            confidence = 1
        elif addon_scrape_failed:
            # An unverifiable loss cannot be a high-confidence win.
            confidence = 2
        elif lost_promos and status == BookingStatus.OPTIMIZATION:
            # Same reasoning: a win with an unquantified promo loss behind it
            # must not outrank a clean one in a confidence-sorted report.
            # A flat 3 rather than min(confidence, 3) because this arm is
            # part of the chain that ASSIGNS confidence - reading it here
            # raised UnboundLocalError, which calculate_ncl's own
            # except-block then turned into a silent ERROR result (caught
            # immediately by driving the real 3000046 numbers through it).
            confidence = 3
        elif status in (BookingStatus.TRAP, BookingStatus.NO_SAVING) and (
            obc_lost > 0 or lost_addon_value > 0
        ):
            # FIXED 2026-08-27 alongside the OBC false positives. OBC loss
            # is now carried in `obc_change`, not `lost_addon_value`, so the
            # ratio arms below saw "no perk lost" and scored these 5/5 —
            # bookings 3000055 (a $43 net LOSS) and 3000054 (a 1.2x OBC
            # margin) both came out TRAP/NO_SAVING at confidence 5, which
            # would sort them right next to genuine opportunities in any
            # report ranked by confidence. A "don't do this" verdict is not
            # a high-confidence win; scored low for the same reason
            # lost_protected is scored 1.
            confidence = 2
        elif price_drop > 0 and total_lost_value == 0:
            confidence = 5
        elif price_drop > 0 and total_lost_value < price_drop:
            confidence = 4
        elif price_drop > 0 and total_lost_value >= price_drop:
            confidence = 2
        else:
            confidence = 2

        return BookingResult(
            cruise_line=CruiseLine.NCL,
            status=status,
            note=note,
            booking_id=booking_id,
            price_category=price_category,
            old_total=old_total,
            new_total=new_total,
            price_drop=price_drop,
            obc_change=obc_change,
            net_saving=net,
            lost_pkg_value=lost_addon_value,
            lost_pkg_names=lost_addon_names,
            confidence=confidence,
            # Recorded so a protected-promo TRAP verdict is auditable and
            # so exports can show both promo columns separately, matching
            # the project owner's own report — see BookingResult's fields.
            old_promos=old_promos or "",
            new_promos=new_promos or "",
            # Protected losses stay in lost_fares (the hard-gate audit
            # trail); every OTHER lost promo goes in re_addable_fares so the
            # export and the DB show what a reprice would give up. Both
            # columns are persisted as of 2026-08-27.
            lost_fares=lost_protected,
            re_addable_fares=lost_promos,
        )

    except Exception as e:
        return BookingResult(
            cruise_line=CruiseLine.NCL,
            status=BookingStatus.ERROR,
            error=str(e),
            booking_id=booking_id,
            price_category=price_category,
        )


# ── GoCCL Calculator ─────────────────────────────────────────────

# GoCCL's automatic discovery only reads the *offer-code comparison*
# screen — average-per-person prices grouped by stateroom type, not the
# per-category, full-cabin-total GROSS AMOUNT that only appears on the
# review screen after a human-reviewed preview_fare_code() run. So unlike
# ESPRESSO/NCL (whose automatic check_booking reads a confirmed new
# total), GoCCL's automatic scan can only surface a *candidate* — a
# cheaper offer code at the same stateroom type — never a confirmed net
# saving. Confidence is capped at 1 star specifically to signal that.
GOCCL_CANDIDATE_CONFIDENCE = 1


def calculate_goccl(
    booking_id: str,
    price_category: str | None,
    current_stateroom_type: str,
    current_offer_code: str,
    current_price_gross: float,
    available_offer_codes: list[dict],
    guests_count: int = 2,
    guests_count_verified: bool = False,
    # The booking's CURRENT fare, needed to say what a switch would cost the
    # customer (core/goccl_fare_types.py). Optional so existing callers and
    # replayed historical data keep working - when the name is absent the
    # tier simply reads as unknown and no terms claim is made, rather than a
    # tier being assumed.
    current_offer_name: str | None = None,
    current_disclaimer: str | None = None,
) -> BookingResult:
    """
    Analyze a GoCCL (Carnival) booking's offer-code comparison and surface
    the cheapest candidate offer code at the SAME stateroom type — GoCCL's
    real comparison axis, since category/stateroom stays fixed and only
    the fare/offer code varies.

    This is an ESTIMATE, not a confirmed price: available_offer_codes
    entries carry an "Average Per Person" price, while the booking's
    current_price_gross is the full per-cabin total (guests x per-person
    + taxes/fees/OBC). Multiplying per-person by guests_count approximates
    the new gross but can't account for OBC changes, which GoCCL only
    exposes after actually clicking through to a candidate (see
    scraper/goccl.py's preview_fare_code — reserved for one human-reviewed
    candidate at a time, never run unattended for every candidate found here).

    CONFIRMED REAL BUG, fixed 2026-08-13: `guests_count` used to always be
    `settings.goccl_default_guests_count` (a global default of 2), with no
    way to tell a genuinely-2-guest booking apart from "we never checked."
    Real captured data (booking DEMO01) shows this default directly
    flipping OPTIMIZATION/NO_SAVING classification via the guests_count
    multiplication below — for any booking whose real occupancy isn't 2,
    the dollar magnitude (and potentially the classification itself) can
    be wrong. No real per-booking guest-count field has been confirmed
    anywhere in GoCCL's `window.initialData` (no live capture with such a
    field exists to verify a selector against — see scraper/goccl.py), so
    this function does NOT guess one. Instead, `guests_count_verified`
    makes the existing assumption explicit rather than silently invisible:
    when False (today's only real caller path), every note this function
    returns that depends on `guests_count` says so plainly, so a human
    reviewing the result knows the dollar figure — and, for the NO_SAVING
    branch below, the absence of one — rests on an unverified assumption,
    not a confirmed fact. This does not change old_total, new_total,
    price_drop, net_saving, or status for any existing caller (all of
    which already pass no argument here, i.e. `guests_count_verified`
    already defaulted to being unverified in every real call site before
    this parameter existed) — it only makes the pre-existing assumption
    visible in the note text instead of silent.

    Args:
        booking_id: The booking ID.
        price_category: Current category code (unchanged by this comparison).
        current_stateroom_type: e.g. "BALCONY".
        current_offer_code: The booking's current offer/rate code.
        current_price_gross: Current booking's full gross total.
        available_offer_codes: Offer-code comparison rows, each a dict with
            "offer_code", "offer_name", "stateroom_type", "price_per_person".
        guests_count: Number of guests on the booking (per-person -> gross).
        guests_count_verified: Whether `guests_count` was actually read from
            this specific booking's own data (True) or is an assumed
            default (False). Never invent a True here — only set it when
            the caller genuinely confirmed the real occupancy.

    Returns:
        BookingResult with status, an estimated net_saving, and confidence
        capped at GOCCL_CANDIDATE_CONFIDENCE — a candidate to verify by hand,
        not a ready-to-act recommendation.
    """
    try:
        candidates = [
            o for o in available_offer_codes
            if o.get("stateroom_type") == current_stateroom_type
            and o.get("offer_code") != current_offer_code
            and safe_float(o.get("price_per_person", 0)) > 0
        ]

        if not candidates:
            return BookingResult(
                cruise_line=CruiseLine.GOCCL,
                status=BookingStatus.NO_SAVING,
                note=f"no saving — no cheaper offer code found for {current_stateroom_type}",
                booking_id=booking_id,
                price_category=price_category,
                old_total=round2(current_price_gross),
                new_total=round2(current_price_gross),
            )

        guest_note = (
            "" if guests_count_verified
            else f" [guest count UNVERIFIED — assumed {guests_count}; confirm real occupancy before trusting this figure]"
        )

        # PICK THE BEST CANDIDATE, NOT THE CHEAPEST. Changed 2026-09-18.
        #
        # This used to be a bare min() on price_per_person. On Carnival the
        # cheapest fare is reliably the most RESTRICTIVE one: across seven of
        # Neon's real bookings the cheapest was PSV "SUPER SAVER" four
        # times, which carries a non-refundable deposit, no price protection,
        # and hands the choice of stateroom location back to Carnival.
        #
        # Offering only that candidate meant the single option on the table
        # was the one most likely to be a bad trade - and once the 3x OBC
        # rule (OBC_LOSS_MIN_RATIO, shared with ESPRESSO and NCL) was applied
        # to it, the booking would come back "no saving" while a perfectly
        # good option sat a few dollars further down the list.
        #
        # Measured on the real ZM57P7 BALCONY column:
        #     PSV SUPER SAVER  saves 117.04  but loses the cabin choice,
        #                      price protection and a refundable deposit
        #     OB7 EARLY SAVER  saves  97.04  and ADDS price protection
        # $20 buys all of that back. rank_candidates orders on terms first
        # and money second; the downgrade is still returned, ranked last, so
        # nothing is hidden and taking it stays a deliberate choice.
        ranked = rank_candidates(
            current_offer_name=current_offer_name,
            current_disclaimer=current_disclaimer,
            offers=candidates,
            guests_count=guests_count,
            current_gross=round2(current_price_gross),
        )
        best, cheapest_candidate = best_and_cheapest(ranked)
        chosen = best or cheapest_candidate

        # What this switch does to the customer's terms, and - when the
        # recommendation is NOT the cheapest fare - what declining the
        # cheaper one costs. Both are stated, so the choice stays visible
        # rather than being quietly made here.
        terms_note = ""
        if chosen is not None:
            if chosen.tier_delta or chosen.is_downgrade:
                terms_note = f" [TERMS: {chosen.terms_note}]"
            if (best is not None and cheapest_candidate is not None
                    and best.offer_code != cheapest_candidate.offer_code):
                gap = round2(cheapest_candidate.price_drop - best.price_drop)
                terms_note += (
                    f" [a cheaper '{cheapest_candidate.offer_code}' saves "
                    f"${round(cheapest_candidate.price_drop)} "
                    f"(${round(gap)} more) but {cheapest_candidate.terms_note}]"
                )
            if chosen.obc_risk:
                terms_note += (
                    " [OBC RISK: the current fare advertises onboard credit and "
                    "this one does not — confirm on the review screen; the "
                    f"{OBC_LOSS_MIN_RATIO:.0f}x rule applies to any OBC given up]"
                )

        old_total = round2(current_price_gross)
        if chosen is not None:
            cheapest = {
                "offer_code": chosen.offer_code,
                "offer_name": chosen.offer_name,
                "price_per_person": chosen.price_per_person,
            }
            estimated_new_total = chosen.new_gross
            price_drop = chosen.price_drop
        else:
            # No cheaper offer at all. Fall back to the raw minimum purely so
            # the existing "isn't actually lower" branch below can report the
            # real numbers rather than nothing.
            cheapest = min(candidates, key=lambda o: safe_float(o.get("price_per_person", 0)))
            estimated_new_total = round2(
                safe_float(cheapest.get("price_per_person", 0)) * guests_count)
            price_drop = round2(old_total - estimated_new_total)

        if price_drop <= 0:
            return BookingResult(
                cruise_line=CruiseLine.GOCCL,
                status=BookingStatus.NO_SAVING,
                note=(
                    "no saving — cheapest candidate offer code isn't actually lower "
                    f"once guest count is applied{guest_note}"
                ),
                booking_id=booking_id,
                price_category=price_category,
                old_total=old_total,
                new_total=estimated_new_total,
            )

        # A CANDIDATE WITHOUT AN OFFER CODE CANNOT BE ACTED ON, added
        # 2026-09-03 by the cross-line audit. Three of the five GoCCL
        # candidates ever stored (DEMO02 $880, DEMO03 $740, DEMO01 $1,560 —
        # $3,180 of the $4,100 GoCCL has ever claimed) carry an EMPTY offer
        # code. The code is the whole point of the finding: it is what the
        # reprice popup passes to fn_goccl_selectOfferAndContinue(), so
        # without it there is nothing to select and no way to verify the
        # figure. Reporting a dollar amount nobody can use is worse than
        # reporting nothing, because it goes into a total and gets planned
        # around.
        offer_code = str(cheapest.get("offer_code") or "").strip()
        if not offer_code:
            return BookingResult(
                cruise_line=CruiseLine.GOCCL,
                status=BookingStatus.NO_SAVING,
                note=(
                    f"a ${round(price_drop)} cheaper fare was seen but its offer "
                    f"code was not captured ({cheapest.get('offer_name', '') or 'unnamed offer'}) "
                    f"— nothing to select or verify, so not reported as a saving"
                    f"{guest_note}"
                ),
                booking_id=booking_id,
                price_category=price_category,
                old_total=old_total,
                new_total=estimated_new_total,
            )

        return BookingResult(
            cruise_line=CruiseLine.GOCCL,
            status=BookingStatus.OPTIMIZATION,
            note=(
                f"candidate ${round(price_drop)} — offer code '{offer_code}' "
                f"({cheapest.get('offer_name', '')}) — UNCONFIRMED, run preview_fare_code to verify "
                f"gross total + OBC before repricing{guest_note}{terms_note}"
            ),
            booking_id=booking_id,
            price_category=price_category,
            # The candidate offer code — carried here (not a real category)
            # so the UI's "Open Reprice Popup" action can pass it straight
            # to preview_fare_code()/fn_goccl_selectOfferAndContinue()
            # without a separate field or a second round-trip. Previously
            # left unset, which meant the popup fell back to price_category
            # (the unchanged category code) and the auto-select silently
            # failed to match any offer-code button.
            new_price_category=cheapest.get("offer_code"),
            old_total=old_total,
            new_total=estimated_new_total,
            price_drop=price_drop,
            net_saving=price_drop,
            confidence=GOCCL_CANDIDATE_CONFIDENCE,
        )

    except Exception as e:
        return BookingResult(
            cruise_line=CruiseLine.GOCCL,
            status=BookingStatus.ERROR,
            error=str(e),
            booking_id=booking_id,
            price_category=price_category,
        )


# ── ESPRESSO Free-Upgrade Detection ─────────────────────────────
#
# HARD PROJECT RULE: never suggest downgrading the customer. Not a
# tolerance, not a judgment call — a flat rule. This module went through
# THREE rejected designs before landing here:
#   1. Compared the current category's price against the cheapest ANY
#      available category, no type filter — produced nonsense (a $73k
#      Suite "beaten" by a $6k Ocean View). Self-caught, wrong.
#   2. Compared against the cheapest available category of the SAME broad
#      room-type label (e.g. Veranda vs Veranda) — found real matches, but
#      even one coarse label can span different decks/locations/views a
#      "cheaper" swap wouldn't reveal, which is still a real downgrade
#      wearing a same-type disguise. Rejected explicitly — do not resurrect.
#   3. REJECTED 2026-08-01, CONFIRMED WRONG AGAINST REAL DATA: compared a
#      strictly-higher-tier candidate's category-table price directly
#      against the booking's real invoice TOTAL. Produced 6 false
#      UPGRADE_AVAILABLE results in one run (bookings 3000002, 3000035,
#      3000036, 3000037, 3000038, 3000039) that were manually checked and
#      found not to exist. Root cause: ESPRESSO's own on-page disclaimer
#      confirms the category table's price is PER-PERSON, TRIPLE-OCCUPANCY
#      — not a total — so comparing it to a whole-booking total is an
#      apples-to-oranges comparison that makes almost anything look
#      falsely cheaper. Every one of the 6, when actually confirmed via a
#      real allocate()+repriceModalCheck() round trip (see below), turned
#      out to cost MORE than staying put (by $401-$5,459). Do not resurrect
#      a design that compares a table price directly to a total.
#   4. CURRENT VERSION: a real, ESPRESSO-confirmed number, not an estimate.
#      find_upgrade_candidates() below is a FREE, UNIT-SAFE pre-filter —
#      it only ever compares the table's per-person rate for a candidate
#      against the table's per-person rate for the CURRENT category (same
#      table, same booking, same units both sides) — never against the
#      total. It decides nothing on its own; it only narrows down which
#      candidates are worth spending a real confirmation round trip on
#      (measured against 155 real captured category tables: cuts the
#      round-trip count by 95.8%, from 862 down to 36). The actual
#      accept/reject decision is made by scraper/espresso.py's
#      _confirm_candidate_total(), which runs the exact same
#      allocate()+repriceModalCheck() sequence already trusted for
#      OPTIMIZATION/TRAP, and reads back ESPRESSO's own rendered
#      sb.summary.price.allocationPrice — a real whole-dollar total,
#      confirmed live 2026-08-01 to update correctly even when
#      repriceModalCheck itself returns "skipRepriceModal" (that key means
#      "this booking can't commit a reprice," not "no price was computed" —
#      the allocation price still reflects the real total either way).
#      make_upgrade_available_result() is only ever called with that real
#      confirmed number, never a table estimate.

# Tier ranking for ESPRESSO's category-table room-type labels. Web-verified
# 2026-07-31: Royal Caribbean's public 4-tier hierarchy (Interior/Ocean
# View/Balcony/Suite) matches exactly. Celebrity's public hierarchy is more
# granular (Concierge Class/AquaClass sit between Veranda and Suite) but
# neither label has been observed in this portal's category table yet — if
# one appears, confirm how it's actually labeled here before trusting this
# ranking for it. "SUITE/DELUXE" vs "SUITE" is ESPRESSO-internal
# terminology with no public source to confirm ordering — deliberately
# ranked EQUAL until confirmed, so neither is ever treated as an upgrade
# over the other.
ESPRESSO_ROOM_TYPE_RANK: dict[str, int] = {
    "INTERIOR": 1,
    "OUTSIDE": 2,
    "BALCONY STATEROOM": 3,
    "VERANDA": 3,
    "SUITE/DELUXE": 4,
    "SUITE": 4,
}

_ROW_PRICE_RE = re.compile(r"([\d,]+\.\d{2})")
_ROW_TYPE_RE = re.compile(r"\n\t([A-Za-z /]+?)\t\n")


def _room_type_from_row(row: dict) -> str | None:
    m = _ROW_TYPE_RE.search(row.get("rowText", "") or "")
    return norm_str(m.group(1)) if m else None


def _price_from_row(row: dict) -> float | None:
    """TRIED price_parser here 2026-08-25, REVERTED after real-data
    verification caught a serious regression: rowText is a messy
    multi-field blob containing OTHER bare numbers (e.g. "WLT(0)",
    "AVL(2)") alongside the real price, and Price.fromstring() on the
    whole string grabbed one of those instead of the price for several
    real rows (confirmed: "RS...WLT(0)...8,782.00" parsed as $0.00, not
    $8,782.00). The exact-".XX"-decimal-shape requirement below isn't
    fragility here — it's the thing that disambiguates the real price
    from those other embedded whole numbers, which is why it stays as a
    plain regex rather than being "hardened" to accept whole dollars too
    (unlike the GoCCL/MSC cases, where the fix target was already an
    isolated, single-purpose line/field with nothing else to confuse it
    with). Confirmed zero real whole-dollar prices across all 83,296
    ESPRESSO category rows captured so far, so this specific fragility
    class was never actually live here in the first place."""
    m = _ROW_PRICE_RE.search(row.get("rowText", "") or "")
    return safe_float(m.group(1).replace(",", "")) if m else None


def find_upgrade_candidates(
    current_category: str | None,
    category_rows: list[dict],
) -> list[dict]:
    """FREE, UNIT-SAFE pre-filter only — decides nothing by itself. Returns
    AVAILABLE categories in a strictly higher room-type tier than the
    current one, whose per-person table rate is <= the current category's
    OWN per-person table rate (same table, same booking — the only
    apples-to-apples comparison the table data supports). Sorted cheapest
    (by table rate) first, so a caller confirming candidates one at a time
    checks the most promising one first.

    This does NOT mean any of these are real upgrades — the table's price
    is per-person/triple-occupancy, not a total (see module docstring
    above). Every candidate returned here still needs a real
    allocate()+repriceModalCheck() confirmation (scraper/espresso.py's
    _confirm_candidate_total()) before it can ever be surfaced as
    UPGRADE_AVAILABLE. This function exists purely to avoid spending a
    real round trip on candidates that are obviously not competitive even
    at the coarse per-person level.
    """
    if not current_category or not category_rows:
        return []

    current_row = next(
        (r for r in category_rows if r.get("category") == current_category), None,
    )
    if current_row is None:
        return []
    current_type = _room_type_from_row(current_row)
    current_rank = ESPRESSO_ROOM_TYPE_RANK.get(current_type) if current_type else None
    current_pp = _price_from_row(current_row)
    if current_rank is None or current_pp is None:
        return []

    candidates = []
    for row in category_rows:
        if row.get("status") != "AVL":
            continue
        rtype = _room_type_from_row(row)
        rank = ESPRESSO_ROOM_TYPE_RANK.get(rtype) if rtype else None
        if rank is None or rank <= current_rank:
            continue
        pp = _price_from_row(row)
        if pp is None or pp > current_pp:
            continue
        candidates.append({"category": row.get("category"), "room_type": rtype, "table_per_person_price": pp})

    candidates.sort(key=lambda c: c["table_per_person_price"])
    return candidates


# ── Helper Constructors ────────────────────────────────────────


def make_wlt_result(booking_id: str, price_category: str | None, cruise_line: CruiseLine) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.WLT,
        note="WLT - waitlisted", booking_id=booking_id, price_category=price_category,
    )


def make_paid_in_full_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, old_total: float = 0,
) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.PAID_IN_FULL,
        note="💳 Fully paid — repricing unavailable",
        booking_id=booking_id, price_category=price_category, old_total=old_total,
    )


def make_not_on_this_account_result(
    booking_id: str,
    cruise_line: CruiseLine,
    market: str,
    other_markets: list[str] | None = None,
) -> BookingResult:
    """The booking is not visible to the agent account we are logged into.

    CONFIRMED BY NEON 2026-08-27: NCL runs a SEPARATE SeaWeb account per
    market, and Canadian (CAD) bookings return "Reservation is not found"
    when checked against the US login. That is not an error — nothing is
    broken and nothing needs debugging; the booking simply needs the other
    account. 25 of the 101 errors in that day's NCL run were this, buried
    in the ERROR bucket alongside real defects.

    `confidence` is deliberately 0: this result asserts nothing about
    price. `net_saving` stays 0.0 so it can never contribute to a savings
    total (which filters on OPTIMIZATION anyway).
    """
    others = [m for m in (other_markets or []) if m and m.upper() != market.upper()]
    hint = (
        f" Re-run it on the {'/'.join(others)} account."
        if others else " Re-run it on the other market's account."
    )
    return BookingResult(
        cruise_line=cruise_line,
        status=BookingStatus.NOT_ON_THIS_ACCOUNT,
        note=(
            f"Not on the {market.upper()} account — the portal reports the "
            f"reservation as not found.{hint} (A booking held under another "
            f"session's edit lock can also look not-found, so confirm before "
            f"treating this as a market mismatch.)"
        ),
        booking_id=booking_id,
        confidence=0,
    )


def make_no_price_change_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, price: float = 0,
) -> BookingResult:
    """The category's price-quote total exactly matches the current price
    — confirmed via the page's own displayed price (sb.summary.price.price
    vs sb.summary.price.allocationPrice), not the reprice-modal API, which
    returns a short, non-JSON body in exactly this scenario and was
    previously misdiagnosed downstream as an expired token."""
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.NO_SAVING,
        note=f"no saving — price unchanged (${price:,.2f})",
        booking_id=booking_id, price_category=price_category,
        old_total=price, new_total=price,
    )


def make_skip_reprice_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine,
) -> BookingResult:
    """ESPRESSO's API explicitly returned skipRepriceModal — a deliberate
    'this booking has a restriction that blocks repricing' response, not
    an error (confirmed against the portal's own 'Booking Restriction:
    Changing price pgm is not allowed' message). No point retrying."""
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.NO_SAVING,
        note="Booking restriction — price program change not allowed",
        booking_id=booking_id, price_category=price_category,
    )


def make_skipped_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, hours_ago: float,
) -> BookingResult:
    h = round(hours_ago, 1)
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.SKIPPED_TODAY,
        note=f"Checked {h}h ago — no saving cached",
        booking_id=booking_id, price_category=price_category,
    )


def make_cancelled_result(booking_id, price_category, cruise_line,
                          detail: str = "") -> "BookingResult":
    """A CANCELLED booking. Never a saving, never "paid in full".

    ESPRESSO renders "N/A" for every price when sb.reservation.status is
    'CX', while its payment panel still reads Total Price 0.00 and Final
    Payment Due 0.00 - which is_paid_in_full() accepts. Booking 3001005 was
    therefore filed as "Fully paid - repricing unavailable" four times.

    Confidence is deliberately 0: this is not a graded opportunity, it is a
    fact about the account that needs a human.
    """
    return BookingResult(
        cruise_line=cruise_line,
        status=BookingStatus.CANCELLED,
        booking_id=booking_id,
        price_category=price_category,
        old_total=0.0,
        new_total=0.0,
        net_saving=0.0,
        confidence=0,
        note=("BOOKING IS CANCELLED — the portal reports this reservation as "
              "cancelled (status CX) and shows no prices for it. Not a "
              "repricing opportunity; check the account."
              + (f" {detail}" if detail else "")),
    )


def make_error_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, error_msg: str,
) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.ERROR,
        note=error_msg, error=error_msg,
        booking_id=booking_id, price_category=price_category,
    )


def make_upgrade_available_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine,
    old_total: float, upgrade: dict,
) -> BookingResult:
    """A strictly-higher-tier category is available for the same or less
    money than the client is already paying. `upgrade["price"]` must be a
    REAL, ESPRESSO-confirmed total (from scraper/espresso.py's
    _confirm_candidate_total()) — never a category-table estimate; see the
    ESPRESSO Free-Upgrade Detection module docstring for why. Always a
    candidate for human review before switching (a category change is a
    different physical room/deck, never auto-selected), but unlike other
    candidate signals in this project, there's no scenario where acting on
    this one harms the customer — it's an upgrade by construction, and
    confirmed real, not estimated."""
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.UPGRADE_AVAILABLE,
        note=(
            f"confirmed free upgrade — {upgrade['room_type'].title()} category "
            f"'{upgrade['category']}' at ${upgrade['price']:.2f} vs current "
            f"${old_total:.2f} — review with client before switching"
        ),
        booking_id=booking_id, price_category=price_category,
        new_price_category=upgrade["category"],
        old_total=round2(old_total), new_total=upgrade["price"],
        price_drop=round2(old_total - upgrade["price"]),
        net_saving=round2(old_total - upgrade["price"]),
    )


# ── ESPRESSO Paid-in-Full Detection ─────────────────────────────
#
# A handful of dollars (or an outright credit balance, i.e. a negative
# amount due) still counts as paid in full — rounding, taxes, small
# adjustments. $25 flat floor, or a percentage of total price for larger
# bookings, whichever is more generous. Confirmed against real data:
# booking 3000040's $23 due (due the same day the scan ran) is exactly the
# case this exists for — it had been slipping through as a false "$77
# OPTIMIZATION" because the reprice API call returned a normal-length
# response, so the old, purely-reactive paid-status check never even ran
# for it.
#
# WIDENED 2026-08-04 from 1.5% to 5%: booking 3000001 ($370.84 due on
# $8,892.68, 4.17%) was reported by Neon as one that should have been
# caught and wasn't — the 1.5% rule was working exactly as designed
# ($370.84 is real money still owed, final payment isn't even due for
# another 9 months), but was stricter than what "paid in full" means in
# practice for this project. Checked the real percent-still-due
# distribution across 468 captured bookings before picking a number: it's
# a smooth continuum with no natural gap near 4.17%, so there's no
# "objectively correct" cutoff to discover here the way there was for the
# free-upgrade fix — this is a business-risk-tolerance choice, not a fact.
# 5% was chosen as the smallest round number that clears 3000001 with a
# little headroom; it also newly classifies ~37 additional bookings out of
# 468 (~8%) as paid-in-full compared to the old 1.5% rule, i.e. that many
# fewer bookings get scored for repricing at all. If that's too aggressive
# or not aggressive enough, adjust this one constant — nothing else
# depends on the specific number.
PAID_IN_FULL_TOLERANCE_FLAT = 25.0
PAID_IN_FULL_TOLERANCE_PCT = 0.05


def is_paid_in_full(final_payment_due: float | None, total_price: float) -> bool:
    """final_payment_due should come from the portal's own "Final Payment
    Due (USD)" figure (already reconciled for taxes/credits/adjustments —
    confirmed present on 590/590 real bookings whenever Total Price is),
    not re-derived from total_price minus payments_received."""
    if final_payment_due is None:
        return False  # couldn't read the field — don't guess, fall through
    tolerance = max(PAID_IN_FULL_TOLERANCE_FLAT, total_price * PAID_IN_FULL_TOLERANCE_PCT)
    return final_payment_due <= tolerance
