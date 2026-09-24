"""MSC opportunity-detection engine.

MSC is structurally different from ESPRESSO/NCL/GoCCL: the agent can never
commit a reprice directly (a human always has to call MSC), and — the
critical point Neon corrected early on — price and discount are
INDEPENDENT levers, not one "is the total lower" comparison. A real
opportunity can exist purely in the discount dimension even when the price
dimension is a dead end (see the confirmed real example on booking
3000005 in msc_project_knowledge.md: today's plain price was actually
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

CONFIRMED LIVE 2026-08-11, booking 3000024: Voyagers Exclusive belongs
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

CLOSED 2026-08-24 (was "KNOWN OPEN LIMITATION" — found testing against
booking 3000021, then confirmed live on bookings 3000031/3000018/
3000017): senior discount never gets an explicit "Discount Description"/
"MSC Club Discount" disclosure line the way named promos and the flat
Voyagers 5% do (confirmed in msc_commands.py's _extract_discounts
docstring). That means `current_discounts` can come back empty on a
booking that actually HAS senior discount applied. The SRN-implied-
discount math (_extract_discounts_with_implied) is the mitigation for
this, but it only has reference values for 3/4/7-night cruises
(STANDARD_NCF_BY_NIGHTS) — outside those lengths, `_check_discount_add`
now downgrades a SENIOR DISCOUNT recommendation to INSUFFICIENT_DATA
instead of a confident OPPORTUNITY (see senior_discount_verifiable /
msc_commands.py's _srn_reference_available), rather than relying on every
caller to remember to "treat it skeptically."

CONFIRMED HARD RULE, added 2026-08-18 after a real false positive
(booking 3000030 — a single 83-year-old traveling alone was recommended
for SENIOR DISCOUNT): senior discount requires AT LEAST TWO senior (65+)
passengers in the cabin, not just one. MSC's own discount dropdown lists
SENIOR DISCOUNT based on ANY passenger being 65+, regardless of party
composition — this is now corrected via senior_count/
_filter_out_ineligible_senior_discount below, replacing the old
all_seniors ("every passenger is 65+") flag, which was never the real
eligibility test in either direction (it wrongly required 100% of
passengers to be seniors, excluding a valid 2-seniors-plus-grandchildren
cabin, AND wrongly let a single senior pass since "all 1 passengers are
seniors" is trivially true).
"""

from __future__ import annotations

import re

from .calculator import round2, safe_float
from .price_scope import PriceScope, scopes_comparable
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


#: Labels MSC uses for the Voyagers Selection / "special offer" family.
#: Revealed behind the crown icon, e.g.
#:     <span class="switch-label font-weight-bold">SPECIAL OFFER 10%</span>
#: Seen at 5%, 10% and 15% across 415 occurrences of real captured data.
SPECIAL_OFFER_LABEL = "SPECIAL OFFER"


def special_offer_allowed(senior_discount_applied: bool) -> bool:
    """RULE MSC-D5: SPECIAL OFFER and the senior discount are exclusive.

    CORRECTED 2026-09-22 the same day it was written. My first reading of
    "this cannot be applied if the customer is senior" excluded SPECIAL
    OFFER whenever ANY passenger was 65+. Neon's clarification:

        "only when the senior discount is actually applied? u can choose
         between one only i usually choose whatever is higher"

    So being a senior is not what blocks it. MSC lets a booking carry ONE
    discount, and the senior discount is simply one of the candidates. The
    exclusion bites only once the senior discount is actually ON the
    booking - otherwise a senior booking is free to take SPECIAL OFFER if
    that is the better number.

    The strict version would have suppressed a legitimate 15% on every
    booking with a 65-year-old aboard.
    """
    return not senior_discount_applied


def best_eligible_discount(options: list[tuple[str, float]] | None,
                           senior_discount_applied: bool = False,
                           ) -> tuple[str, float] | None:
    """The single best discount this booking can actually USE.

    RULE MSC-D6, in Neon's words: "u can choose between one only i usually
    choose whatever is higher". MSC carries ONE discount, so this is a pick,
    not a sum - never add percentages together.

    SPECIAL OFFER is dropped only when the senior discount is already
    applied (see special_offer_allowed). `options` are (label, percent)
    pairs already confirmed available for this sailing.

    Returns None when nothing is usable - a real answer, not a zero.
    """
    usable = [
        (label, pct) for label, pct in (options or [])
        if pct and pct > 0
        and (special_offer_allowed(senior_discount_applied)
             or SPECIAL_OFFER_LABEL not in (label or "").upper())
    ]
    if not usable:
        return None
    return max(usable, key=lambda lp: lp[1])


def msc_discount_beats_current(current_total: float | None,
                               today_price: float | None,
                               discount_pct: float | None) -> tuple[bool | None, str]:
    """RULE MSC-D1/D2: does applying this discount actually help?

    THE RULE THIS ENCODES, from Neon 2026-09-22: applying a discount does
    NOT take a percentage off the customer's existing fare. MSC REPRICES the
    booking at TODAY'S rate and applies the discount to that. So a discount
    is only worth having when the repriced total lands BELOW what they
    already pay.

    Booking 3000005 is why this exists:

        current total            1,756.42
        today, same category     2,109.54   (+353.12)
          after 5%  club         2,004.06   (+247.64)
          after 10% selection    1,898.59   (+142.17)
          after 15% both         1,793.11   (+ 36.69)

    Every one of those leaves the customer WORSE OFF, and the run reported
    two of them as OPPORTUNITY. That is the same failure as MSC's fabricated
    $267.01 and GoCCL's +$184-that-was-really-minus-$6: a percentage quoted
    against a base nobody checked.

    Returns (beats, reason):
        True  - repriced total is genuinely below the current fare
        False - it is not; recommending it would cost the customer money
        None  - cannot tell, so nothing may be claimed either way
    """
    if current_total is None or today_price is None:
        return None, ("today's price or the current total could not be read, "
                      "so it is unknown whether a discount would beat the "
                      "fare already held")
    if discount_pct is None or discount_pct <= 0:
        return None, "no discount percentage to evaluate"
    if today_price <= 0 or current_total <= 0:
        return None, "a zero or negative price cannot be compared"

    repriced = round(today_price * (1 - discount_pct / 100.0), 2)
    if repriced < current_total:
        return True, (f"repricing today at {today_price:,.2f} less "
                      f"{discount_pct:g}% gives {repriced:,.2f}, below the "
                      f"current {current_total:,.2f}")
    return False, (f"repricing today at {today_price:,.2f} less "
                   f"{discount_pct:g}% still gives {repriced:,.2f}, which is "
                   f"ABOVE the {current_total:,.2f} already booked - applying "
                   f"it would cost the customer {repriced - current_total:,.2f}")


def _filter_out_ineligible_senior_discount(options: list[str] | None, senior_count: int) -> list[str] | None:
    """CONFIRMED HARD RULE, stated directly by Neon 2026-08-18 after a
    real false positive (booking 3000030 — a single 83-year-old traveling
    alone got recommended for SENIOR DISCOUNT): senior discount requires
    AT LEAST TWO passengers 65+ in the cabin. A lone senior does not
    qualify, even though "every passenger on this booking is 65+" is
    trivially true for them — that "all passengers seniors" framing
    (msc_commands.py's old all_seniors flag) was never the real
    eligibility test. Non-senior passengers alongside 2+ seniors (e.g.
    grandchildren) do NOT disqualify it — only the raw senior COUNT
    matters. MSC's own discount dropdown lists SENIOR DISCOUNT regardless
    of party composition, so this can't be read off the dropdown alone.
    Same pattern as _filter_out_disallowed_discounts: preserves the None
    (not captured) vs [] (captured, filtered to empty) distinction."""
    if options is None:
        return None
    if senior_count >= 2:
        return options
    return [o for o in options if "SENIOR" not in o.upper()]


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
    *,
    current_total_price: float | None = None,
    due_amount: float | None = None,
    today_price_tab_confirmed: bool = False,
    is_group_rate: bool = False,
    is_paid_in_full: bool = False,
    final_payment_date_passed: bool = False,
    occupancy_verified: bool = True,
    occupancy_note: str = "",
    customer_has_club_membership: bool = False,
    today_price_includes_club_discount: bool = False,
    club_entry_note: str = "",
    # Excursions/transfers/flights inside the booking total. A category
    # quote never includes these, so they are backed out before comparing.
    non_cruise_charges: float = 0.0,
    # What each side of the comparison actually covers. The backstop for
    # the whole class of bug the four specific guards each fix one case of.
    current_scope: PriceScope | None = None,
    today_scope: PriceScope | None = None,
) -> MscCheck:
    """KEYWORD-ONLY past the two prices, deliberately.

    On 2026-09-01 `occupancy_verified` / `occupancy_note` were inserted in
    the middle of this signature while the only production call site passed
    everything POSITIONALLY. That silently re-bound `is_group_rate` to
    `occupancy_verified` and `is_paid_in_full` to `occupancy_note` - so the
    paid-in-full rule and the final-payment gate, two HARD business rules,
    stopped being applied at all. Nothing raised; one pre-existing test
    caught it by luck.

    Eight boolean-ish parameters in a fixed order is a blind spot waiting to
    happen. `*` makes the same mistake a TypeError instead of a wrong
    answer, and lets parameters be added in future without re-checking every
    caller.
    """
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
    # HARD RULE, confirmed directly by Neon 2026-08-24 (bookings
    # 3000031/3000018/3000017), matching widely-reported MSC/general
    # cruise-industry practice: a fare drop only gets informally honored
    # before final payment is due — once that date passes the fare is
    # locked in. Distinct from is_paid_in_full above (a booking can be
    # past its final payment date without having actually paid yet) but
    # the same shape of hard gate, checked before any price math, discount
    # checks unaffected.
    if final_payment_date_passed:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="this booking's final payment date has already passed — MSC only price-matches/reprices before final payment is due, the fare is locked in after that (discounts can still be added/upgraded, see the other checks)",
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

    # OCCUPANCY GUARD, added 2026-09-01. Every price below compares a
    # today-quote against the booking's own total, which is only valid if
    # both cover the SAME guests. Booking 3000081 was priced as 1 adult
    # while the invoice said 2, turning a genuine "no opportunity" (2-adult
    # quote $3,610.66 ABOVE the current $3,517.34) into a fake $267.01
    # opportunity - the real answer was $81.98. Booking 3000024 did the
    # same thing with 3 dropped kids and a fake $1,929.61.
    #
    # Checked BEFORE any arithmetic: a per-guest price compared against a
    # whole-booking total is meaningless, so there is no number worth
    # computing here. Refusing is the only safe answer.
    if not occupancy_verified:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                "cannot price this booking: the guest count used for today's "
                "quote is not verified against the invoice"
                + (f" - {occupancy_note}" if occupancy_note else "")
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
                value_unit="USD",
            )
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note=f"today's base rate (${today_base_price:.2f}) is not lower than the current rate (${current_base_price:.2f})",
        )

    # THE COMPARISON MUST BE LIKE FOR LIKE, added 2026-09-01.
    #
    # `current_total_price` is the customer's real total, with whatever
    # discount they hold already baked in. If today's quote was captured
    # WITHOUT that same discount, the two are not comparable and the
    # comparison is biased against ever finding a saving. That is not a
    # theoretical concern: it is why MSC scans returned no opportunities at
    # all. Booking 3000081, against Neon's own screenshot of the listing:
    #     without the membership entered  $3,610.66 > $3,517.34 -> "no"
    #     with it entered (MSC's own card) $3,435.36 < $3,517.34 -> $81.98
    #
    # Staging now enters the customer's Voyagers membership on the dummy
    # booking so the harvested price already carries the 5% club discount.
    # When that fails - the crown control is missing, MSC rejects the
    # details, the page does not echo the number back - the resulting price
    # is a LIST price, and reporting NO_OPPORTUNITY from it would silently
    # reinstate the original bug on that booking. So it refuses instead.
    if customer_has_club_membership and not today_price_includes_club_discount:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                "this customer holds a Voyagers Club membership but today's quote "
                "was captured without it, so it cannot be compared against their "
                "current total, which already includes their discount"
                + (f" — {club_entry_note}" if club_entry_note else "")
            ),
        )

    # LIKE-FOR-LIKE BACKSTOP, added 2026-09-02. Every guard below fixes ONE
    # instance of a single recurring mistake: comparing two prices that do
    # not cover the same thing. This catches an instance nobody has written
    # a guard for yet, and names the dimension that differs rather than
    # leaving a wrong dollar figure to be diagnosed later.
    comparable, why = scopes_comparable(current_scope, today_scope)
    if not comparable:
        return MscCheck(
            type=MscOpportunityType.PRICE_MATCH,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=f"cannot compare these prices: {why}",
        )

    # NON-CRUISE ADDED SERVICES, added 2026-09-02. The booking total can
    # include excursions (ACT), transfers (TRF) and flights (AIR) - lines a
    # category quote can never contain. Comparing the two overstates the
    # saving by exactly those amounts, the same like-for-like flaw as the
    # club discount, the occupancy and the multi-cabin bugs.
    #
    # Found by the invoice reconciliation check (msc_invoice_components):
    # 4 of 100 invoices did not add up, and every gap was one of these
    # lines - a $112.00 Pisa excursion, a $48.00 backstage tour x2, airport
    # transfers. Booking 3000013, cited in msc_project_knowledge.md as a
    # real $1,292.51 price-match opportunity, carries $96.00 of excursions,
    # so that figure was overstated by $96.00.
    #
    # Subtracted rather than refused: the amount is known exactly, so the
    # comparison can be corrected instead of abandoned. Refusing would
    # throw away four real bookings for no reason.
    if current_total_price is not None and non_cruise_charges:
        current_total_price = round2(current_total_price - non_cruise_charges)

    # A ZERO TOTAL IS NOT A TOTAL. Found in the 2026-09-01 full audit:
    # bookings 3000076 and 3000012 both parse a "Value of the cruise" of
    # exactly $0.00. No real cruise costs nothing, so this means the figure
    # was not on the page (or the booking is a placeholder shell) - and
    # every comparison below would then measure today's price against zero,
    # which can only ever produce nonsense in one direction or the other.
    # Treated as "not captured", which is what it actually is.
    if current_total_price is not None and current_total_price <= 0:
        current_total_price = None

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
                value_unit="USD",
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


def _voyagers_club_addable_label(has_voyagers: bool) -> str:
    """CONFIRMED REAL GAP, closed 2026-08-24 (booking 3000019): recommending
    "Voyagers Club 5%, call MSC to add" reads as a clean, ready-to-call
    opportunity, but the flat Club discount requires the customer to
    actually HAVE an MSC Voyagers Club membership — MSC won't apply it to
    someone who doesn't. When they don't, this is not a false positive
    (Neon's own framing: "not 100% false positive... positive in a way I
    cannot describe") — it's a REAL opportunity that requires one extra
    step first. Researched 2026-08-24: joining MSC Voyagers Club directly
    is free and immediate (just needs the booking reference, open to
    anyone with a confirmed booking) — the Welcome tier alone may already
    be enough for the flat 5% (requires_club, no tier minimum seen in the
    discount catalog), so this is usually a much smaller ask than a formal
    Status Match (matching an elite tier from ~55+ other eligible loyalty
    programs, free but takes up to 72 hours) — worth confirming which path
    actually unlocks MSCCLUB5 against a real booking before assuming
    either one is required. Surfaced as an explicit caveat so whoever
    reads this can judge whether the extra step is worth it themselves,
    rather than a bare "call MSC" that leads to a confusing dead end."""
    if has_voyagers:
        return "Voyagers Club 5%"
    return (
        "Voyagers Club 5% (customer isn't currently an MSC Voyagers Club member — "
        "joining is free and just needs the booking reference, or a Status Match from "
        "an eligible loyalty program for a higher starting tier; confirm which path "
        "actually unlocks this discount before deciding it's worth the extra step)"
    )


def _check_discount_add(
    current_discounts: list[dict] | None,
    today_discount_options: list[str] | None,
    is_group_rate: bool = False,
    club_discount_offered: bool | None = None,
    has_voyagers: bool = False,
    senior_discount_verifiable: bool = False,
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
            note=(
                "no Voyagers Club discount applied yet — Group Rate bookings are only eligible for the flat "
                f"{_voyagers_club_addable_label(has_voyagers)} discount (not military/senior/promo tiers, not "
                "Voyagers Selection), call MSC to check eligibility and add"
            ),
        )

    # "implied" (SRN-math-detected, see _extract_discounts_with_implied)
    # counts the same as an explicitly disclosed "club"/"named" discount
    # here — a discount already reducing the price, regardless of
    # whether it prints a disclosure line, means don't recommend adding
    # ANOTHER one on top (confirmed real bug 2026-08-11: booking 3000024
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
        addable.append(_voyagers_club_addable_label(has_voyagers))

    # CONFIRMED REAL FALSE POSITIVES, 2026-08-24 (bookings 3000031,
    # 3000018, 3000017): senior discount never discloses itself (see
    # this module's own KNOWN OPEN LIMITATION docstring), so an empty
    # current_discounts list here only proves "no discount applied" when
    # senior_discount_verifiable is True (the SRN-vs-standard-NCF math
    # actually ran for this cruise length — see
    # msc_commands.py's _srn_reference_available). All three of these real
    # bookings had senior_count >= 2 (genuinely eligible for the option)
    # and a cruise length outside STANDARD_NCF_BY_NIGHTS, so the SRN check
    # never ran — and DISCOUNT_ADD confidently recommended adding it
    # anyway, purely because current_discounts happened to be empty.
    # Split SENIOR options into their own uncertain bucket in that case
    # instead of trusting them as confirmed-addable.
    uncertain = []
    for opt in (today_discount_options or []):
        if "SENIOR" in opt.upper() and not senior_discount_verifiable:
            uncertain.append(opt)
        else:
            addable.append(opt)

    if addable:
        note = (
            "no discount currently applied — "
            f"{', '.join(addable)} available today, call MSC to check eligibility and add"
        )
        if uncertain:
            note += (
                f" (also shows {', '.join(uncertain)} as available, but this cruise length isn't covered by "
                "the standard-fare reference table used to rule out senior discount already being silently "
                "applied — can't confirm it isn't already on top of the rest; verify the SRN line by hand)"
            )
        return MscCheck(type=MscOpportunityType.DISCOUNT_ADD, status=MscCheckStatus.OPPORTUNITY, note=note)
    if uncertain:
        return MscCheck(
            type=MscOpportunityType.DISCOUNT_ADD,
            status=MscCheckStatus.INSUFFICIENT_DATA,
            note=(
                f"{', '.join(uncertain)} shows as available today, but senior discount never discloses itself "
                "on MSC's Price Breakdown and this cruise length isn't covered by the standard-fare reference "
                "table used to rule it out via SRN math — can't confirm it isn't already silently applied; "
                "verify the SRN line by hand before recommending it"
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
            value_unit="PERCENTAGE_POINTS",
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
    senior_count: int,
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
    # CONFIRMED REAL GAP, fixed 2026-08-26: this used to test ONLY for
    # "SPECIAL OFFER". But MSC also discloses this same family of program
    # under the literal label "VOYAGERS EXCLUSIVES" (real captured format,
    # quoted in msc_commands.py's own discount-parsing docstring:
    # `Discount Description: VOYAGERS EXCLUSIVES - ... - Discount Rate: 9.75%`).
    # Such a line parses as kind="named", does NOT contain "SPECIAL OFFER",
    # and is not kind="implied" — so it cleared every guard here and this
    # check returned a confident OPPORTUNITY recommending a Voyagers
    # Selection discount on a booking that ALREADY carries the Exclusives
    # program. That's a real cross-program false positive.
    #
    # Matched on the "EXCLUSIV" stem so both the singular and plural real
    # spellings are caught. NOTE: msc_commands.py's docstring (a disclosed
    # Exclusives line WITH a printed rate) and this module's own header
    # comment (Exclusive is silent and must never be detected by
    # disclosure text) genuinely contradict each other on this program —
    # flagged for reconciliation against a real capture. Guarding on BOTH
    # labels is the safe direction either way: the cost of a false
    # "already applied" is a missed opportunity a human can still find,
    # while the cost of the old behavior was recommending a discount that
    # can't be stacked.
    _ALREADY_APPLIED_LABELS = ("SPECIAL OFFER", "VOYAGERS EXCLUSIV")
    already_applied = any(
        any(marker in (d.get("label") or "").upper() for marker in _ALREADY_APPLIED_LABELS)
        for d in current_discounts
        if d.get("kind") == "named"
    )
    if already_applied:
        return MscCheck(
            type=MscOpportunityType.VOYAGERS_SELECTION,
            status=MscCheckStatus.NO_OPPORTUNITY,
            note="a 'SPECIAL OFFER' discount is already disclosed on this booking — likely Voyagers Selection already applied (not yet confirmed against a real applied example, verify by hand if in doubt)",
        )

    # ADDED 2026-08-11, booking 3000024: an "implied" (SRN-math) entry
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
    # Corrected 2026-08-18 (booking 3000030): this note used to fire on
    # the old "all passengers 65+" rule, which wrongly flagged a LONE
    # senior as Senior-Discount-eligible. The real eligibility rule is at
    # least 2 senior (65+) passengers — see
    # _filter_out_ineligible_senior_discount's docstring — so the
    # exclusivity warning below only makes sense under that same rule.
    exclusivity_note = (
        " — this booking has 2+ senior (65+) passengers, making it Senior-Discount-eligible: confirmed "
        "UI-enforced rule is Voyagers Selection is NOT combinable with Senior Discount, so this would mean "
        "giving up Senior in exchange, not stacking both"
        if senior_count >= 2 else ""
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
    senior_count: int = 0,
    senior_discount_verifiable: bool = False,
    due_amount: float | None = None,
    today_price_tab_confirmed: bool = False,
    is_group_rate: bool = False,
    club_discount_offered: bool | None = None,
    final_payment_date_passed: bool = False,
    # Forwarded to the PRICE_MATCH occupancy guard. Defaults keep every
    # existing caller working unchanged; the MSC scraper passes the real
    # values from msc_occupancy_is_trustworthy().
    occupancy_verified: bool = True,
    occupancy_note: str = "",
    is_overpayment: bool = False,
    # Whether today's captured quote already carries the customer's own
    # Voyagers Club discount. Without this the price comparison is a list
    # price against a discounted total — the bug that hid every MSC
    # opportunity. See _check_price_match.
    customer_has_club_membership: bool = False,
    today_price_includes_club_discount: bool = False,
    club_entry_note: str = "",
    non_cruise_charges: float = 0.0,
    current_scope: PriceScope | None = None,
    today_scope: PriceScope | None = None,
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
        senior_count: How many passengers on this booking are 65+ (from
            _extract_passengers()). CONFIRMED HARD RULE, Neon 2026-08-18:
            senior discount requires AT LEAST TWO senior passengers in the
            cabin — a lone senior is not eligible even though MSC's own
            dropdown lists SENIOR DISCOUNT regardless of party
            composition. Used here to strip SENIOR DISCOUNT out of
            today_discount_options before DISCOUNT_ADD/DISCOUNT_TIER_UPGRADE
            ever see it when this booking doesn't meet that bar (see
            _filter_out_ineligible_senior_discount), and to attach the
            confirmed not-combinable-with-Senior caveat to a
            VOYAGERS_SELECTION opportunity note when it does.
        senior_discount_verifiable: Whether msc_commands.py's
            _srn_reference_available found this booking's cruise length in
            STANDARD_NCF_BY_NIGHTS, i.e. whether the SRN-vs-standard-fare
            math could actually run to rule out a silent senior discount.
            CONFIRMED REAL FALSE POSITIVES, 2026-08-24 (bookings 3000031/
            3000018/3000017 — 9/19/10 nights, none in the reference
            table): senior discount never discloses itself, so an empty
            current_discounts only proves "no discount applied" when this
            is True. When False, DISCOUNT_ADD downgrades a SENIOR DISCOUNT
            recommendation to INSUFFICIENT_DATA instead of a confident
            OPPORTUNITY — defaults to False (conservative, matching every
            other unverified signal in this function) so a caller that
            forgets to pass this gets the cautious behavior, not a guess.
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
        final_payment_date_passed: Whether msc_commands.py's
            _final_payment_date_passed found this booking's own Final
            Payment Date already behind us. CONFIRMED HARD RULE, Neon
            2026-08-24, matching widely-reported MSC/cruise-industry
            practice: a fare drop only gets honored before final payment
            is due — _check_price_match short-circuits to NO_OPPORTUNITY
            when this is True, the same shape of gate as is_paid_in_full
            (the two are related but distinct: a booking can be past its
            final payment date without having actually paid yet).
    """
    # HARD RULE, stated directly by Neon 2026-09-01: "3000071 this booking
    # has an overpayment it is not optimizable."
    #
    # An overpaid booking is off the table entirely - not merely paid in full.
    # Overpayment was already detected (msc_commands._extract_booking_essentials
    # sets is_overpayment from an "Overpayment" label or a negative Due Amount)
    # but it only ever fed `is_paid_in_full`, which softens PRICE_MATCH while
    # leaving all three discount checks free to report an opportunity.
    #
    # This is checked BEFORE anything else because it is a property of the
    # booking, not of any one lever - the same shape of gate as a cancelled
    # sailing, and the same shape as NCL's final-payment-date rule.
    #
    # It also retires a misleading data point: 3000071's $63.24 was being
    # treated as ground truth for the discount arithmetic, and two bookings
    # appearing to agree at ~3.07% of CAB gross drove that investigation. With
    # this booking excluded, that agreement is coincidence, not evidence.
    if is_overpayment:
        return MscBookingResult(
            booking_id=booking_id,
            category=category,
            cancelled_or_postponed=False,
            is_paid_in_full=True,
            checks=[],
            has_any_opportunity=False,
            note="booking is OVERPAID — not optimizable (hard rule)",
        )

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
    # genuinely already had a real discount applied (3000016, SPECIAL
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
    # Senior discount requires 2+ senior passengers (see senior_count's
    # docstring above) — same one-time filtering pattern, added 2026-08-18
    # after booking 3000030's false positive (a lone 83-year-old).
    allowed_discount_options = _filter_out_ineligible_senior_discount(allowed_discount_options, senior_count)

    checks = [
        _check_price_match(
            current_base_price,
            today_base_price,
            current_total_price=current_total_price,
            due_amount=due_amount,
            today_price_tab_confirmed=today_price_tab_confirmed,
            is_group_rate=is_group_rate,
            is_paid_in_full=is_paid_in_full,
            final_payment_date_passed=final_payment_date_passed,
            occupancy_verified=occupancy_verified,
            customer_has_club_membership=customer_has_club_membership,
            today_price_includes_club_discount=today_price_includes_club_discount,
            club_entry_note=club_entry_note,
            non_cruise_charges=non_cruise_charges,
            current_scope=current_scope,
            today_scope=today_scope,
            occupancy_note=occupancy_note,
        ),
        _check_discount_add(
            current_discounts, allowed_discount_options, is_group_rate, club_discount_offered,
            has_voyagers, senior_discount_verifiable,
        ),
        _check_discount_tier_upgrade(current_discounts, allowed_discount_options, is_group_rate),
        _check_voyagers_selection(current_discounts, today_discount_catalog, has_voyagers, senior_count, is_group_rate),
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


# ---------------------------------------------------------------------------
# How MSC's discount percentages actually arithmetise
# ---------------------------------------------------------------------------
# DERIVED 2026-09-01 from the 100 stored MSC invoices, at Neon's request
# ("do a deep research online and from the data we captured to figure out
# averages"). Until now every discount check could name a better discount but
# never say what it was WORTH, because nothing knew what the percentage
# multiplied.
#
# Two independent findings from the corpus, both exact rather than approximate:
#
# 1. STACKED DISCOUNTS COMPOUND, THEY DO NOT ADD.
#    MSC brochure fares are whole dollars, so the correct model is the one
#    that recovers a whole-dollar list fare. Across the 13 invoices carrying
#    two disclosed discounts, gross/((1-p1)(1-p2)) lands on a whole dollar
#    per guest 10 times; gross/(1-p1-p2) does so once. Examples:
#      3000008  15% + 5%  3,396.34 -> compounded 2,103.00/guest (additive 2,122.71)
#      3000078  10% + 5%  2,052.00 -> compounded 1,200.00/guest (additive 1,207.06)
#      3000079  10% + 5%  1,822.86 -> compounded 1,066.00/guest (additive 1,072.27)
#
# 2. THE BASE IS CAB + SRN. TAXES AND PORT CHARGES (PCH) ARE EXCLUDED.
#    SRN looked like a fixed per-guest tariff with six different values. It is
#    not - it is ONE tariff seen through the same compounded factor as the
#    cabin fare. On the $182.00/guest sailing family:
#      182.00 x 1.0000 = 182.00  (no discount)      x10 bookings
#      182.00 x 0.9500 = 172.90  (5%)               x10
#      182.00 x 0.9025 = 164.25  (9.75%)            x8
#      182.00 x 0.8550 = 155.61  (10% + 5%)         x6
#      182.00 x 0.8075 = 146.96  (15% + 5%)         x6
#    Five clusters, five exact hits. Booking 3000081's 277.97/guest is the
#    same relationship on a $308.00 tariff (308.00 x 0.9025 = 277.97).
#    Excluding PCH matches MSC's published promotional terms, which state
#    that government fees, taxes and port expenses are additional per guest.
#
# NOT YET USED TO PRODUCE A REPORTED SAVING - see msc_discount_delta.


def msc_discount_factor(pcts) -> float:
    """The multiplier MSC's discounts apply to a fare component.

    Compounding, not addition - see the block comment above. A 15% and a 5%
    discount together leave 0.85 x 0.95 = 0.8075 of the fare, not 0.80.
    """
    factor = 1.0
    for pct in pcts or ():
        if pct is None:
            continue
        factor *= 1.0 - (float(pct) / 100.0)
    return factor


def msc_list_fare(component_gross: float | None, pcts) -> float | None:
    """Recover a fare component's pre-discount (brochure) value.

    Discounts are baked into the figures MSC prints - the invoice's own
    discount column is $0.00 on all 217 captured CAB lines - so the list
    fare has to be divided back out. Returns None rather than a guess when
    the inputs cannot support the calculation.
    """
    if not component_gross or component_gross <= 0:
        return None
    factor = msc_discount_factor(pcts)
    if factor <= 0:
        return None
    return round(component_gross / factor, 2)


def msc_discount_delta(
    cab_gross: float | None,
    srn_gross: float | None,
    current_pcts,
    new_pcts,
) -> float | None:
    """What moving from one discount set to another is worth, in dollars.

    Both sets are absolute, not deltas: to add a cumulable 5% on top of an
    existing 9.75%, pass current=[9.75] and new=[9.75, 5.0].

    A CAUTION THAT MUST TRAVEL WITH THIS FUNCTION. The mechanism above is
    confirmed exactly against 100 invoices, but the resulting dollar figures
    do NOT yet reproduce Neon's three verified answers, and they miss high.
    Adding a cumulable 5%, against both candidate bases:

        booking     real     CAB+SRN base      CAB-only base
        3000081   $81.98   $161.37 (0.508)   $133.57 (0.6138)
        3000071   $63.24   $111.26 (0.568)   $102.61 (0.6163)
        3000083   $23.66    $85.03 (0.278)    $67.73 (0.3493)

    RETRACTED 2026-09-01, same day: 3000071's row above is not evidence.
    Neon: "3000071 this booking has an overpayment it is not optimizable."
    Its $63.24 was never a discount saving, so the apparent agreement
    between it and 3000081 at 3.069% / 3.081% of CAB gross - which is what
    made this look like a solvable arithmetic problem - was coincidence
    between two unrelated numbers. Worth remembering how convincing two
    figures agreeing to 0.012 points looked.

    That leaves TWO usable ground-truth answers, and they do not agree with
    each other on any base: $81.98 is 2.540% of 3000081's CAB+SRN while
    $23.66 is 1.391% of 3000083's. Neon also confirmed he only ever
    applies senior, Voyagers Club, Voyagers Exclusive or Voyagers Selection
    (5/10/15%) - and an exhaustive search over exactly those levers, on
    CAB+SRN / CAB-only / SRN-only, as both an addition and a swap, whole-
    booking and per-guest, produces nothing within $1.30 of either answer.

    Until that is settled, this function must not feed `estimated_value` on a
    check: a figure 63% too high on two independently verified bookings is
    precisely the fabricated-opportunity failure this module already guards
    against (bookings 3000081 $267.01, 3000024 $1,929.61).
    """
    if not cab_gross or cab_gross <= 0:
        return None
    base = cab_gross + (srn_gross or 0.0)
    cur = msc_discount_factor(current_pcts)
    new = msc_discount_factor(new_pcts)
    if cur <= 0 or new <= 0:
        return None
    list_base = base / cur
    return round(list_base * (cur - new), 2)
