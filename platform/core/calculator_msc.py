"""MSC opportunity-detection engine.

MSC is structurally different from ESPRESSO/NCL/GoCCL: the agent can never
commit a reprice directly (a human always has to call MSC), and — the
critical point Neon corrected early on — price and discount are
INDEPENDENT levers, not one "is the total lower" comparison. A real
opportunity can exist purely in the discount dimension even when the price
dimension is a dead end (see the confirmed real example on booking
2000001 in msc_project_knowledge.md: today's plain price was actually
HIGHER, but a better discount tier — 15% replacing an existing 10%, with
the 5% Voyagers stacking on top of either — was a genuine, separate win).

Four distinct, non-exclusive checks, every time:
  1. PRICE_MATCH — today's base rate (before any discount) is lower than
     the booking's own current base rate.
  2. DISCOUNT_ADD — price isn't worth touching, but a discount the
     booking does NOT currently have could still be added to the
     existing rate.
  3. DISCOUNT_TIER_UPGRADE — the booking already has a discount, but a
     BETTER TIER of that same kind of discount is available today.
  4. VOYAGERS_SELECTION — confirmed 2026-08-11 via DiscountPaxTypeCmd's
     real backend response (see msc_project_knowledge.md): MSC runs a
     separate, per-sailing "Voyagers Selection" promo (paxType codes
     MSVG10W/MSVG15W, on-screen label "SPECIAL OFFER 10%/15%", found
     specifically inside the Voyagers Club/crown modal as a checkbox —
     NOT the main "Additional Discounts" dropdown) that STACKS on top of
     the base 5% Club discount rather than swapping a tier of it —
     confirmed via a real captured CabinSelectionConfirmCmd request that
     submitted both codes together. Kept as its own check rather than
     folded into DISCOUNT_ADD because it has distinct eligibility rules:
     requires Voyagers Club membership, single-cabin-booking only
     (confirmed live in the DOM, `multicabinDisableVoyager` div), and is
     confirmed UI-enforced as NOT combinable with Senior Discount
     (`voyagerNotAvailable` div) even though both are independently
     flagged `Cumulability:Yes` in the raw backend catalog.

This module never guesses past what the data actually supports — each
check independently reports INSUFFICIENT_DATA rather than a false
NO_OPPORTUNITY when a required input wasn't captured.

CONFIRMED LIVE 2026-08-11, booking 2000015: Voyagers Exclusive belongs
on the same "never discloses itself" list as senior discount (below) —
a Voyagers Club membership was added to this real booking (visible via
its Passenger Details gaining an "MSC Voyagers Club: ... - Gold" line
between two checks minutes apart) and the SRN line dropped from $182.00
to exactly $164.25 (= $182 x 0.95 x 0.95, i.e. two 5% discounts stacked
multiplicatively — base Club + Exclusive) — but a direct text search of
the ENTIRE page for "Discount" and "Exclusiv" found nothing. This is the
first live confirmation (not just historical/inferred) that Voyagers
Exclusive is silent the same way senior discount is; detect it the same
way — SRN math against the standard-NCF-by-length table — never by
searching for disclosure text.

KNOWN OPEN LIMITATION, found testing against booking 2000012: senior
discount never gets an explicit "Discount Description"/"MSC Club
Discount" disclosure line the way named promos and the flat Voyagers 5%
do (confirmed in msc_commands.py's _extract_discounts docstring). That
means `current_discounts` can come back empty on a booking that actually
HAS senior discount applied, which would make DISCOUNT_ADD wrongly
report "add senior discount" as an opportunity when it's already there.
Until this is fixed (likely by deriving senior discount from itemized
SRN math against the known standard-NCF-by-length table, then folding it
into current_discounts before calling this function), a caller passing
data for an all_seniors=True booking should treat a DISCOUNT_ADD
OPPORTUNITY result skeptically and verify the SRN line by hand before
recommending it.
"""

from __future__ import annotations

import re

from .calculator import round2, safe_float
from .models import (
    MSC_PAID_IN_FULL_DUE_THRESHOLD,
    MscBookingResult,
    MscCheck,
    MscCheckStatus,
    MscOpportunityType,
)

_RATE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _filter_out_disallowed_discounts(options: list[str] | None) -> list[str] | None:
    """CONFIRMED HARD POLICY, stated directly by Neon 2026-08-11:
    CruiseIntel does NOT apply military discounts from the agency side at
    all, regardless of whether MSC's dropdown lists them as generically
    available (it always does, without checking real eligibility).
    Strip any MIL-CIV/MILITARY option out before DISCOUNT_ADD/
    DISCOUNT_TIER_UPGRADE ever consider it — never surface it as a
    recommendation. Preserves None (not captured) vs [] (captured, all
    filtered out) distinction."""
    if options is None:
        return None
    return [o for o in options if "MIL-CIV" not in o.upper() and "MILITARY" not in o.upper()]


def _parse_rate_pct(label: str) -> float | None:
    """Pull a numeric percentage out of a discount label, e.g.
    'MIL-CIV-IL-DSCNT-10%' -> 10.0, 'SPECIAL OFFER 15%' -> 15.0.
    Labels with no printed number ('SENIOR DISCOUNT', 'TODAY10' — the
    latter's rate is unconfirmed, see msc_project_knowledge.md) return
    None rather than a guess."""
    m = _RATE_PCT_RE.search(label or "")
    return float(m.group(1)) if m else None


def _due_amount_context_note(estimated_value: float, due_amount: float | None) -> str:
    """CORRECTED 2026-08-11, direct instruction from Neon: an earlier
    version of this note ('_refund_framing_note') framed a paid-in-full
    booking's opportunity as extra-exciting because it would produce a
    refund — Neon corrected this as a logic error, not a bug: a refund
    goes to the CLIENT, not to CruiseIntel, so it isn't a business win the
    way reducing what a client still owes to MSC is. (This also directly
    reverses an earlier, now-wrong memory note from this same project
    that claimed the opposite — see msc_project_knowledge.md.)

    This note now stays purely factual, neither hyping nor
    de-prioritizing a paid-in-full finding — just states what would
    actually happen (refund to client vs. reduced balance) so whoever
    acts on it has accurate context, without editorializing about which
    outcome matters more. Returns '' when due_amount is unknown, rather
    than guessing."""
    if due_amount is None:
        return ""
    if due_amount < MSC_PAID_IN_FULL_DUE_THRESHOLD:
        # ADDED 2026-08-12, direct instruction from Neon: "paid in full"
        # covers more than an exact $0.00 Due Amount — a small residual
        # under MSC_PAID_IN_FULL_DUE_THRESHOLD counts too (see
        # msc_commands.py's _is_paid_in_full, the single source of truth
        # this wording must stay consistent with).
        residual_note = f" (Due Amount ${due_amount:.2f})" if due_amount > 0.01 else " (Due Amount $0.00)"
        return (
            f" — this booking is already paid in full{residual_note}; any correction here "
            "would go back to the client as a refund, not reduce a future payment"
        )
    if estimated_value >= due_amount:
        return (
            f" — this booking's remaining Due Amount is only ${due_amount:.2f}, less than this "
            f"${estimated_value:.2f} opportunity; the portion beyond ${due_amount:.2f} would go back to "
            f"the client as a refund, not to CruiseIntel"
        )
    return f" — this booking still owes ${due_amount:.2f}; this would reduce what's still owed by ${estimated_value:.2f}"


def _check_price_match(
    current_base_price: float | None,
    today_base_price: float | None,
    current_total_price: float | None = None,
    due_amount: float | None = None,
    today_price_tab_confirmed: bool = False,
    is_group_rate: bool = False,
    is_paid_in_full: bool = False,
) -> MscCheck:
    # HARD RULE, confirmed directly by Neon 2026-08-12: a paid-in-full
    # booking can still have a discount ADDED (see DISCOUNT_ADD/
    # DISCOUNT_TIER_UPGRADE/VOYAGERS_SELECTION — none of those are
    # gated by this), but it can NEVER be price-matched — MSC does not
    # allow repricing a booking that's already fully paid off. This is a
    # real business/procedural rule, not just a framing note about
    # refunds vs. reduced balances (that's _due_amount_context_note's
    # job, and it still applies to the other three checks). Checked
    # FIRST, before any price data is even looked at, so a paid-in-full
    # booking never reports a PRICE_MATCH opportunity regardless of what
    # today's price looks like.
    if is_paid_in_full:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="this booking is paid in full — MSC does not allow price-matching a fully-paid booking (discounts can still be added/upgraded, see the other checks)",
        )
    if today_base_price is None:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="today's undiscounted price for this category wasn't captured",
        )

    # CONFIRMED REAL BUG, first full 60-booking batch run 2026-08-11:
    # today_base_price was being trusted and reported as a CONFIRMED
    # dollar opportunity even when the rate-tab match had explicitly
    # FAILED (booking's own rate name wasn't found among the sailing's
    # promo tabs) or didn't apply at all (Group Rate booking — no
    # individual-search tab exists for these at all, a hard rule from
    # 2026-08-10). This is exactly the "$654 vs $26" trap already
    # documented in this project's own history (comparing across the
    # wrong rate/promo tab produces a wildly wrong price) — 5 of 10
    # PRICE_MATCH findings in the first real batch run were affected (4
    # unconfirmed tab matches + 1 Group Rate booking reported as if
    # comparable when it structurally isn't). Never report a confirmed
    # PRICE_MATCH number unless the tab match genuinely succeeded.
    if is_group_rate:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                "this is a Group Rate booking — there is no individual-search rate tab comparable to it, "
                "so today's price cannot be validly compared against it at all (confirmed rule, not just missing data)"
            ),
        )
    if not today_price_tab_confirmed:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                f"today's price (${today_base_price:.2f}) was read from a rate/promo tab that could NOT be "
                f"confirmed to match this booking's own rate program — comparing across the wrong tab has "
                f"produced wildly wrong deltas before; needs the correct tab found (or a manual check) before trusting this number"
            ),
        )

    if current_base_price is not None:
        diff = round2(current_base_price - today_base_price)
        if diff > 0.01:
            return MscCheck(
                type=MscOpportunityType.PRICE_MATCH,
                status=MscCheckStatus.OPPORTUNITY,
                note=(
                    f"today's base rate is ${diff:.2f} lower than the current locked-in rate — price-match "
                    f"and reapply the existing discount(s) on top of the new rate"
                    f"{_due_amount_context_note(diff, due_amount)}"
                ),
                estimated_value=diff,
            )
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note=f"today's base rate (${today_base_price:.2f}) is not lower than the current rate (${current_base_price:.2f})",
        )

    if current_total_price is not None:
        # Conservative fallback when the true pre-discount base isn't
        # known: current_total = current_base x discount_factor, and
        # discount_factor <= 1 whether or not a discount is actually
        # applied (factor is exactly 1 with none). So today_base <=
        # current_total mathematically GUARANTEES today_base <=
        # current_base — a real, confirmed price-match opportunity —
        # without ever needing to know the current discount rate. The
        # reverse (today_base > current_total) proves nothing either way,
        # since a discounted current_total can legitimately sit below an
        # undiscounted today_base even when today's base IS lower than
        # the current undiscounted base — reported as insufficient data
        # rather than a guessed NO.
        diff = round2(current_total_price - today_base_price)
        if diff > 0.01:
            return MscCheck(
                type=MscOpportunityType.PRICE_MATCH,
                status=MscCheckStatus.OPPORTUNITY,
                note=(
                    f"today's undiscounted rate (${today_base_price:.2f}) is already ${diff:.2f} below the "
                    f"CURRENT total (${current_total_price:.2f}) — confirmed price-match opportunity even "
                    f"before reapplying any existing discount, which would save even more"
                    f"{_due_amount_context_note(diff, due_amount)}"
                ),
                estimated_value=diff,
            )
        # today_base_price is approximately equal to or above current_total.
        # The equal case is a genuine boundary, not a confirmed $0 "win":
        # if the current total already has a discount baked in, today's
        # undiscounted rate landing exactly on it would actually mean
        # today's true base is HIGHER than current's true base (a real
        # win was masked) — but if current has NO discount at all, it
        # means the rates are just identical. Can't tell which without
        # the true base price, so this reports ambiguous either way
        # rather than guessing at either extreme.
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                f"today's undiscounted rate (${today_base_price:.2f}) is at or above the current total "
                f"(${current_total_price:.2f}) — can't rule out a price-match without knowing the booking's "
                f"own pre-discount base rate"
            ),
        )

    return MscCheck(
        type=MscOpportunityType.PRICE_MATCH,
        status=MscCheckStatus.INSUFFICIENT_DATA,
        note="need either the booking's pre-discount base price, or at minimum its current total, to compare against today's rate",
    )


def _check_discount_add(
    current_discounts: list[dict] | None,
    today_discount_options: list[str] | None,
    is_group_rate: bool = False,
    club_discount_offered: bool | None = None,
) -> MscCheck:
    if current_discounts is None:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="this booking's own current discount status wasn't confirmed (Price Breakdown capture didn't complete) — cannot safely tell whether a discount is already applied",
        )

    # Confirmed hard rule, stated directly by Neon 2026-08-11: Group
    # Rate bookings can ONLY ever get the flat 5% Voyagers Club discount
    # — none of the military/senior/promo tiers surfaced in the
    # "Additional Discounts" dropdown actually apply to them, even
    # though the dropdown shows the same generic options regardless of
    # rate program. Ignore today_discount_options entirely for these —
    # trusting it would recommend discounts that don't actually apply.
    if is_group_rate:
        # "implied" counts too — SRN math showing a reduction with no
        # disclosure line still proves SOME discount (almost certainly
        # the flat Club 5%, the only one eligible here) is already on.
        has_club = any(d.get("kind") in ("club", "implied") for d in current_discounts)
        if has_club:
            return MscCheck(
                type=MscOpportunityType.DISCOUNT_ADD,
                status=MscCheckStatus.NO_OPPORTUNITY,
                note="Voyagers Club discount already applied — Group Rate bookings are capped at this flat 5%, no other discount type is eligible",
            )
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.OPPORTUNITY,
            note="no Voyagers Club discount applied yet — Group Rate bookings are only eligible for the flat 5% Voyagers Club discount (not military/senior/promo tiers, not Voyagers Selection), call MSC to check eligibility and add",
        )

    # "implied" (SRN-math-detected, see _extract_discounts_with_implied)
    # counts the same as an explicitly disclosed "club"/"named" discount
    # here — a discount already reducing the price, regardless of
    # whether it prints a disclosure line, means don't recommend adding
    # ANOTHER one on top (confirmed real bug 2026-08-11: booking 2000015
    # got recommended "add a discount" minutes after a real 9.75% was
    # already applied, purely because it never discloses itself in text).
    already_has_any_discount = any(d.get("kind") in ("club", "named", "implied") for d in current_discounts)
    if already_has_any_discount:
        implied = next((d for d in current_discounts if d.get("kind") == "implied"), None)
        note = (
            f"an undisclosed discount is already applied ({implied['label']}) — see DISCOUNT_TIER_UPGRADE instead"
            if implied and not any(d.get("kind") in ("club", "named") for d in current_discounts)
            else "a discount is already applied — see DISCOUNT_TIER_UPGRADE instead"
        )
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note=note,
        )

    # CONFIRMED REAL GAP, closed 2026-08-11 at Neon's direct prompt: this
    # check previously ONLY ever looked at today_discount_options (the
    # military/senior/TODAY10 "Additional Discounts" dropdown) — it never
    # checked whether the flat 5% Voyagers Club discount itself could be
    # added, even though that has been the single most common real
    # finding across this entire project's history (see the corrected
    # 8-booking batch table earlier in msc_project_knowledge.md — every
    # one of those was "Add Voyagers 5%", none were dropdown-based).
    # club_discount_offered comes from the literal on-page phrase "Club
    # discount available, insert Voyagers Club to activate." — Neon's
    # direct instruction: "always look at this phrase as this is a big
    # indicator." Its ABSENCE (when captured, i.e. not None) instead
    # means this specific sailing/rate combination doesn't offer the
    # club discount pathway at all — a real, useful negative signal too.
    if today_discount_options is None and club_discount_offered is None:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="neither today's discount dropdown nor the Voyagers Club availability text was captured for this booking",
        )

    addable = []
    if club_discount_offered:
        addable.append("Voyagers Club 5%")
    if today_discount_options:
        addable.extend(today_discount_options)

    if addable:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.OPPORTUNITY,
            note=(
                "no discount currently applied — "
                f"{', '.join(addable)} available today, call MSC to check eligibility and add"
            ),
        )
    return MscCheck(
        type=MscOpportunityType.DISCOUNT_ADD,
        status=MscCheckStatus.NO_OPPORTUNITY,
        note="no discount options offered today",
    )


def _check_discount_tier_upgrade(
    current_discounts: list[dict] | None,
    today_discount_options: list[str] | None,
    is_group_rate: bool = False,
) -> MscCheck:
    if current_discounts is None:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="this booking's own current discount status wasn't confirmed (Price Breakdown capture didn't complete) — cannot safely tell whether an existing discount could be upgraded",
        )

    # Same Group Rate rule as _check_discount_add: only the flat 5%
    # Voyagers Club discount is eligible at all — there is no higher
    # tier of it to upgrade to, so a tier-upgrade is structurally
    # impossible for these regardless of what the dropdown shows.
    if is_group_rate:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="Group Rate bookings are only ever eligible for the flat 5% Voyagers Club discount — there is no higher tier to upgrade to",
        )

    if today_discount_options is None:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="today's discount dropdown options weren't captured for this booking",
        )
    named = [d for d in current_discounts if d.get("kind") == "named"]
    if not named:
        # Deliberately NOT treating "implied" (SRN-math-inferred) entries
        # as comparable here, unlike _check_discount_add — we don't know
        # the implied discount's real SOURCE (senior? Exclusive? both?),
        # only an estimated combined %, so suggesting "swap it for a
        # better tier" would be guessing at something not confidently
        # attributable. Still worth surfacing that it exists, so this
        # doesn't read as "definitely nothing here" when there is.
        implied = next((d for d in current_discounts if d.get("kind") == "implied"), None)
        note = (
            f"an undisclosed discount is already applied ({implied['label']}) but its exact source isn't "
            f"confidently known — verify by hand before assuming a tier-upgrade applies"
            if implied else "no existing named discount to upgrade — see DISCOUNT_ADD instead"
        )
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.NO_OPPORTUNITY if implied is None else MscCheckStatus.INSUFFICIENT_DATA,
            note=note,
        )
    current_best = max(safe_float(d.get("rate_pct")) for d in named)

    # Only labels with a printed percentage can be safely compared —
    # 'SENIOR DISCOUNT'/'TODAY10' have no confirmed numeric rate to
    # compare against, so a match there needs a human to check by hand
    # rather than a silent guess.
    parsed_options = [(label, _parse_rate_pct(label)) for label in today_discount_options]
    better = [(label, rate) for label, rate in parsed_options if rate is not None and rate > current_best]
    unparseable = [label for label, rate in parsed_options if rate is None]

    if better:
        best_label, best_rate = max(better, key=lambda x: x[1])
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.OPPORTUNITY,
            note=(
                f"current best named discount is {current_best:.1f}% — '{best_label}' offers {best_rate:.1f}% today, "
                f"call MSC to swap just this component, keep the base rate and any other stacked discount unchanged"
            ),
            estimated_value=round2(best_rate - current_best),
        )
    if unparseable:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                f"current best named discount is {current_best:.1f}% — today's options include "
                f"{', '.join(unparseable)} with no confirmed rate, needs a manual check to rule out an upgrade"
            ),
        )
    return MscCheck(
        type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
        status=MscCheckStatus.NO_OPPORTUNITY,
        note=f"no better tier available today (current best is {current_best:.1f}%)",
    )


def _check_voyagers_selection(
    current_discounts: list[dict] | None,
    today_discount_catalog: list[dict] | None,
    has_voyagers: bool,
    all_seniors: bool,
    is_group_rate: bool = False,
) -> MscCheck:
    # Same Group Rate rule, stated directly by Neon 2026-08-11: ONLY
    # the flat 5% Voyagers Club discount is eligible for Group Rate
    # bookings — Voyagers Selection (MSVG10W/MSVG15W) does not apply to
    # them regardless of whether the sailing's own DiscountPaxTypeCmd
    # catalog lists it as generally available.
    if is_group_rate:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="Group Rate bookings are not eligible for Voyagers Selection — only the flat 5% Voyagers Club discount applies",
        )

    if today_discount_catalog is None:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note="today's DiscountPaxTypeCmd catalog wasn't captured for this booking's sailing",
        )

    # program_name is paxDesc ("Voyagers Selection WELCOME") — the label
    # actually shown in the crown-modal checkbox is discDesc ("SPECIAL
    # OFFER 10%/15%"), which does NOT contain the word "Voyagers" at all
    # (confirmed real gap that caused three earlier live checks to miss
    # this entirely) — identify the program by paxDesc, not discDesc.
    selection_entries = [
        d for d in today_discount_catalog
        if "VOYAGERS SELECTION" in (d.get("program_name") or "").upper()
    ]
    if not selection_entries:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="no Voyagers Selection offer on this sailing today",
        )

    if not has_voyagers:
        codes = ", ".join(d.get("disc_cd", "?") for d in selection_entries)
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note=f"Voyagers Selection ({codes}) is offered on this sailing but requires MSC Voyagers Club membership, which this booking's passengers don't have",
        )

    if current_discounts is None:
        codes = ", ".join(d.get("disc_cd", "?") for d in selection_entries)
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=f"Voyagers Selection ({codes}) is offered on this sailing and this booking's passengers have Voyagers Club, but this booking's own current discount status wasn't confirmed (Price Breakdown capture didn't complete) — cannot rule out that it's already applied",
        )

    # Best-effort "already applied" check — NOT yet confirmed against a
    # real applied-Voyagers-Selection Price Breakdown (only ever seen as
    # an AVAILABLE offer so far, never confirmed post-application), so
    # this is a heuristic, not a verified fact: a disclosed named
    # discount whose label also reads "SPECIAL OFFER" is treated as
    # likely already this same promo.
    already_applied = any(
        "SPECIAL OFFER" in (d.get("label") or "").upper()
        for d in current_discounts
        if d.get("kind") == "named"
    )
    if already_applied:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="a 'SPECIAL OFFER' discount is already disclosed on this booking — likely Voyagers Selection already applied (not yet confirmed against a real applied example, verify by hand if in doubt)",
        )

    # ADDED 2026-08-11, booking 2000015: an "implied" (SRN-math) entry
    # proves SOME undisclosed discount is already on this booking, but
    # NOT confidently which one — could be senior, Exclusive, Selection
    # itself, or some combination. Confidently recommending "add
    # Selection" here risks recommending something already effectively
    # applied (or double-counting on top of it) — downgrade to
    # INSUFFICIENT_DATA rather than a confident OPPORTUNITY.
    implied = next((d for d in current_discounts if d.get("kind") == "implied"), None)
    if implied:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                f"Voyagers Selection is offered on this sailing, but this booking already has an undisclosed "
                f"discount applied ({implied['label']}) whose exact source isn't confidently known — verify by "
                f"hand whether Selection is already part of it before recommending it as a new addition"
            ),
        )

    best = max(selection_entries, key=lambda d: safe_float(d.get("rate_pct")))
    exclusivity_note = (
        " — this booking's passengers are all 65+: confirmed UI-enforced rule is Voyagers Selection "
        "is NOT combinable with Senior Discount, so this would mean giving up Senior in exchange, not stacking both"
        if all_seniors else ""
    )
    return MscCheck(
        type=MscOpportunityType.VOYAGERS_SELECTION,
        status=MscCheckStatus.OPPORTUNITY,
        note=(
            f"{best.get('disc_cd')} ({safe_float(best.get('rate_pct')):.0f}%, "
            f"'{best.get('program_name')}') available on this sailing — confirmed to stack with the base "
            f"5% Club discount, single-cabin-booking only, call MSC to check eligibility and add{exclusivity_note}"
        ),
    )


def evaluate_msc_booking(
    booking_id: str,
    category: str | None,
    cancelled_or_postponed: bool = False,
    is_paid_in_full: bool = False,
    current_base_price: float | None = None,
    today_base_price: float | None = None,
    current_total_price: float | None = None,
    current_discounts: list[dict] | None = None,
    today_discount_options: list[str] | None = None,
    today_discount_catalog: list[dict] | None = None,
    has_voyagers: bool = False,
    all_seniors: bool = False,
    due_amount: float | None = None,
    today_price_tab_confirmed: bool = False,
    is_group_rate: bool = False,
    club_discount_offered: bool | None = None,
) -> MscBookingResult:
    """Run all three opportunity checks for one booking.

    Args:
        booking_id: The booking ID.
        category: Cabin category code (or type name for Guaranteed Cabin
            bookings — see msc_commands.py's is_guaranteed handling).
        cancelled_or_postponed: From _is_placeholder_departure() — a
            departure year 2045+ means the sailing is cancelled/postponed
            and nothing here gets checked.
        is_paid_in_full: CONFIRMED 2026-08-12, direct instruction from
            Neon: a paid-in-full booking can still have a discount
            ADDED (DISCOUNT_ADD/DISCOUNT_TIER_UPGRADE/VOYAGERS_SELECTION
            are unaffected), but MSC does not allow price-matching one —
            _check_price_match short-circuits to NO_OPPORTUNITY when
            this is True, before even looking at price data.
        current_base_price: The booking's own pre-discount cruise fare
            (e.g. CAB per-cabin total) — NOT the discounted total shown
            as "Booking Value". Only pass this when it's actually been
            derived (e.g. from itemized Price Breakdown math), never the
            discounted total — comparing a discounted current price
            against an undiscounted today's price is an apples-to-oranges
            trap that will report false price-match opportunities.
        today_base_price: Today's dummy-booking price for the same
            category, with NO discount applied (Voyagers modal left
            empty, discount dropdown untouched).
        current_total_price: The booking's current DISCOUNTED total (its
            "Booking Value") — used as a conservative fallback for the
            price-match check when current_base_price isn't known. See
            _check_price_match's docstring for why this is mathematically
            safe (never a guess) but can only confirm an opportunity, not
            rule one out.
        current_discounts: Structured discounts already on this booking,
            from msc_commands.py's _extract_discounts() — a list of
            {"kind": "club"|"named", "label": str, "rate_pct": float}.
            Remember this only reflects EXPLICITLY DISCLOSED discounts;
            an empty list does not prove the booking has no discount.
        today_discount_options: The discount dropdown's text options from
            today's dummy-booking check (e.g. ["SENIOR DISCOUNT",
            "MIL-CIV-IL-DSCNT-10%"]) — pass None (not []) when this
            wasn't captured, so it's distinguishable from "captured and
            genuinely empty."
        today_discount_catalog: The real backend discount catalog for
            this sailing, from msc_commands.py's
            _extract_discount_catalog() (parsed from DiscountPaxTypeCmd's
            response body) — a list of {disc_cd, label, program_name,
            rate_pct, requires_club, cumulable, is_variable}. Pass None
            when this wasn't captured. This is what VOYAGERS_SELECTION
            checks against — it is a strictly better source than
            today_discount_options for that specific check, since the
            Voyagers Selection promo renders inside the crown modal, not
            the "Additional Discounts" dropdown that today_discount_options
            comes from.
        has_voyagers: Whether any passenger on this booking has an MSC
            Voyagers Club membership (from _extract_passengers()) —
            Voyagers Selection requires this.
        all_seniors: Whether every passenger on this booking is 65+ (from
            _extract_passengers()) — used only to attach the confirmed
            not-combinable-with-Senior caveat to a VOYAGERS_SELECTION
            opportunity note, not to suppress it.
        due_amount: The booking's remaining Due Amount (from
            msc_commands.py's _extract_booking_essentials()), used only
            to attach factual context to a PRICE_MATCH opportunity note
            about whether it would reduce what's still owed vs. produce
            a client refund. Per Neon's direct correction 2026-08-11: a
            refund is NOT specially valuable to CruiseIntel (it goes to the
            client, not the agency) — this is informational context, not
            a signal to prioritize or de-prioritize the finding itself.
        today_price_tab_confirmed: Whether msc_commands.py's
            _match_rate_tab actually found and clicked the tab matching
            this booking's own rate program before today_base_price was
            read. False means today_base_price came from whatever tab
            happened to be default-active — NOT reliable for PRICE_MATCH
            (confirmed real bug 2026-08-11: comparing across the wrong
            tab produced false confirmed opportunities in the first live
            batch run). Defaults to False (conservative) rather than
            assuming a match.
        is_group_rate: Whether this booking is on MSC's Group Rates
            program — these have NO comparable individual-search tab at
            all (confirmed rule, 2026-08-10), so PRICE_MATCH is
            structurally not computable for them regardless of
            today_price_tab_confirmed.
        club_discount_offered: Whether the literal on-page phrase "Club
            discount available, insert Voyagers Club to activate." was
            seen on today's occupancy screen. Neon's direct instruction
            2026-08-11: "always look at this phrase as this is a big
            indicator" — it's the on-page confirmation that the flat 5%
            Voyagers Club discount can genuinely be added to THIS
            sailing/rate. Pass None when not captured, False when
            captured and genuinely absent (a real negative signal — this
            specific rate doesn't offer the club pathway at all), True
            when present.
    """
    if cancelled_or_postponed:
        return MscBookingResult(
            booking_id=booking_id,
            category=category,
            cancelled_or_postponed=True,
            is_paid_in_full=is_paid_in_full,
            checks=[],
            has_any_opportunity=False,
            note="sailing is cancelled/postponed (placeholder departure date) — nothing to check",
        )

    # IMPORTANT: current_discounts is NOT defaulted to [] here on purpose.
    # None and [] are different, real facts — None means "the Price
    # Breakdown wasn't confirmed captured for this booking" (see
    # msc_commands.py's _lookup_one_booking, which now returns None
    # rather than possibly-wrong text when its render-completion poll
    # doesn't succeed), while [] means "captured, and genuinely no
    # discount was disclosed." Confirmed real bug, first live batch run
    # 2026-08-11: collapsing None into [] here made a booking that
    # genuinely already had a real discount applied (2000010, SPECIAL
    # OFFER 15% + MSCCLUB5) get reported as a false DISCOUNT_ADD/
    # VOYAGERS_SELECTION "opportunity" on 3 of 5 identical repeated
    # checks, purely because the Price Breakdown modal hadn't finished
    # rendering yet when it was captured — a dangerous class of mistake
    # (recommending a discount that's ALREADY there) this project has
    # been burned by before. Each check function below handles
    # current_discounts is None explicitly by reporting
    # INSUFFICIENT_DATA instead of guessing.
    # Military discount is never a real recommendation for this agency
    # (confirmed policy, 2026-08-11) — filtered out here, once, rather
    # than in each check, so it can never leak through DISCOUNT_ADD or
    # DISCOUNT_TIER_UPGRADE regardless of what MSC's dropdown lists.
    allowed_discount_options = _filter_out_disallowed_discounts(today_discount_options)

    checks = [
        _check_price_match(
            current_base_price, today_base_price, current_total_price, due_amount,
            today_price_tab_confirmed, is_group_rate, is_paid_in_full,
        ),
        _check_discount_add(current_discounts, allowed_discount_options, is_group_rate, club_discount_offered),
        _check_discount_tier_upgrade(current_discounts, allowed_discount_options, is_group_rate),
        _check_voyagers_selection(current_discounts, today_discount_catalog, has_voyagers, all_seniors, is_group_rate),
    ]
    has_any_opportunity = any(c.status == MscCheckStatus.OPPORTUNITY for c in checks)

    return MscBookingResult(
        booking_id=booking_id,
        category=category,
        cancelled_or_postponed=False,
        is_paid_in_full=is_paid_in_full,
        checks=checks,
        has_any_opportunity=has_any_opportunity,
        note="opportunity found" if has_any_opportunity else "no opportunity found on the data available",
    )
