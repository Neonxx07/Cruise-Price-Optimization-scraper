"""Carnival fare types: what a cheaper rate actually costs the customer.

Neon 2026-09-18: "i want you to search online so we include the fair rate
promotion as best to worst for the cx benefet and we add it in our scoring
system th 1 to 5 system".

THE PROBLEM THIS SOLVES. A GoCCL scan compares fares and reports the
cheapest. But Carnival's rate codes are not the same product at different
prices - they carry materially different terms, and the cheapest is
routinely the most restrictive:

    Fun Select     cancel without penalty before final payment
    Early Saver    price protection (claim later price drops), deposit
                   returns as future cruise credit less $50pp
    Super Saver    NON-REFUNDABLE deposit, NO price protection, and the
                   stateroom LOCATION is assigned by Carnival
    Pack & Go      entire fare non-refundable, location assigned

Live consequence, 2026-09-18: a scan of seven of Neon's bookings
recommended PSV "SUPER SAVER" on four of them, including bookings currently
on an EARLY SAVER rate. Taken at face value that moves a customer off price
protection, turns a refundable-as-credit deposit into a forfeited one, and
can hand their chosen cabin back to the cruise line - to save $117.

So a price drop alone is not a recommendation. This module says what tier a
fare belongs to and what a switch gives up.

CLASSIFIED FROM THE NAME, NOT THE CODE - the same lesson as
core/princess_packages.py. The portal's own data-rate-name carries the fare
type in plain words ("SUN-SATIONAL EARLY SAVER BONUS SALE", "FUN SELECT",
"SUPER SAVER") while the 3-letter codes are per-campaign noise: OB7 and OJS
are both Early Saver, and PSV/PNS/PB4/PHY all share a P prefix while being
completely different products.

Sources (researched 2026-09-18):
  https://www.carnival.com/legal/specials-terms-conditions
  https://help.carnival.com/app/answers/detail/a_id/2705/
  https://www.cruzely.com/explained-carnivals-early-saver-super-saver-and-pack-go-rates/
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── the tiers, best to worst for the customer ────────────────────────────
#
# The 1-5 scale Neon asked for. 5 = most customer benefit. This is a
# CUSTOMER-BENEFIT ranking, not a price ranking - the cheapest fare is
# usually the lowest-scoring one, which is the entire point.

FUN_SELECT = "FUN_SELECT"
EARLY_SAVER = "EARLY_SAVER"
PROMOTIONAL = "PROMOTIONAL"
SUPER_SAVER = "SUPER_SAVER"
PACK_AND_GO = "PACK_AND_GO"

# Ordered least-to-most restrictive, mirroring princess_packages.TIERS.
TIERS = (PACK_AND_GO, SUPER_SAVER, PROMOTIONAL, EARLY_SAVER, FUN_SELECT)


@dataclass(frozen=True)
class FareTerms:
    tier: str
    score: int                      # 1-5, 5 = best for the customer
    deposit_refundable: bool | None  # None = varies / unknown
    price_protection: bool
    guest_picks_cabin: bool
    summary: str


TERMS: dict[str, FareTerms] = {
    FUN_SELECT: FareTerms(
        FUN_SELECT, 5, True, False, True,
        "cancel without penalty before final payment; deposit refundable",
    ),
    EARLY_SAVER: FareTerms(
        EARLY_SAVER, 4, False, True, True,
        "price protection on later price drops; deposit returns as future "
        "cruise credit less $50pp; guest picks the cabin",
    ),
    # Campaign sales that name no fare type. Their terms genuinely vary, so
    # this sits mid-scale and is never treated as equivalent to a known one.
    PROMOTIONAL: FareTerms(
        PROMOTIONAL, 3, None, False, True,
        "promotional sale fare; terms vary by campaign - check the offer's "
        "own disclaimer before switching",
    ),
    SUPER_SAVER: FareTerms(
        SUPER_SAVER, 2, False, False, False,
        "NON-REFUNDABLE deposit, NO price protection, and Carnival assigns "
        "the stateroom location",
    ),
    PACK_AND_GO: FareTerms(
        PACK_AND_GO, 1, False, False, False,
        "entire fare non-refundable; Carnival assigns the stateroom location",
    ),
}

# Matched against data-rate-name. Order matters: "EARLY SAVER" must be
# tested before the looser sale patterns, since real names combine them
# ("SUN-SATIONAL EARLY SAVER BONUS SALE" is an Early Saver, not a generic
# sale).
_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bFUN\s*SELECT\b", FUN_SELECT),
    (r"\bEARLY\s*SAVER\b", EARLY_SAVER),
    (r"\bSUPER\s*SAVER\b", SUPER_SAVER),
    (r"\bPACK\s*(&|AND)?\s*GO\b", PACK_AND_GO),
)


def fare_tier(offer_name: str | None) -> str | None:
    """Tier for a fare, from its NAME. None when it cannot be read.

    Returns PROMOTIONAL for a name that is clearly a sale but names no fare
    type ("SEPTEMBER SAVINGS SALE", "BRING THE BUNCH - SAVE A BUNCH SALE").
    Returns None for an unreadable name - never a default tier, because
    assuming the wrong one would understate what a switch gives up.
    """
    if not offer_name:
        return None
    name = offer_name.upper()
    for pattern, tier in _PATTERNS:
        if re.search(pattern, name):
            return tier
    if re.search(r"\b(SALE|SAVINGS|BONUS|DEAL|OFFER|BUNDLE)\b", name):
        return PROMOTIONAL
    return None


def fare_score(offer_name: str | None) -> int | None:
    """The 1-5 customer-benefit score. None when the tier is unreadable."""
    tier = fare_tier(offer_name)
    return TERMS[tier].score if tier else None


def compare_fares(current_name: str | None, new_name: str | None) -> tuple[int, str]:
    """How a switch changes the customer's position.

    Returns (delta, explanation). delta is new_score - current_score:
    negative is a DOWNGRADE in terms, whatever it does to the price.

    This deliberately returns a number and a sentence rather than a
    yes/no. Whether a downgrade is acceptable is a commercial decision -
    Neon's call, per booking - and this module's job is to make sure it is
    a decision rather than an accident.
    """
    cur, new = fare_tier(current_name), fare_tier(new_name)
    if cur is None or new is None:
        unknown = current_name if cur is None else new_name
        return 0, (
            f"fare type could not be read from {unknown!r} - cannot say "
            f"what this switch gives up"
        )
    if cur == new:
        return 0, f"same fare type ({_label(cur)}) - no change in terms"

    delta = TERMS[new].score - TERMS[cur].score
    if delta < 0:
        lost = _what_is_lost(TERMS[cur], TERMS[new])
        return delta, (
            f"DOWNGRADE {_label(cur)} -> {_label(new)}"
            + (f": loses {', '.join(lost)}" if lost else "")
        )
    return delta, f"UPGRADE {_label(cur)} -> {_label(new)}: {TERMS[new].summary}"


def _label(tier: str) -> str:
    return tier.replace("_", " ").title()


def _what_is_lost(cur: FareTerms, new: FareTerms) -> list[str]:
    lost = []
    if cur.price_protection and not new.price_protection:
        lost.append("price protection on later price drops")
    if cur.deposit_refundable and not new.deposit_refundable:
        lost.append("a refundable deposit")
    elif cur.deposit_refundable is False and new.deposit_refundable is False:
        pass
    if cur.guest_picks_cabin and not new.guest_picks_cabin:
        lost.append("the guest's choice of stateroom location "
                    "(Carnival assigns it)")
    return lost


# ── choosing a candidate, not just the cheapest one ──────────────────────


@dataclass
class Candidate:
    """One offer at the booking's own stateroom type, scored."""
    offer_code: str
    offer_name: str
    price_per_person: float
    new_gross: float
    price_drop: float
    tier: str | None
    score: int | None
    tier_delta: int
    terms_note: str
    keeps_obc: bool | None      # None = the offer says nothing either way
    obc_risk: bool              # current fare advertises OBC, this one doesn't

    @property
    def is_downgrade(self) -> bool:
        return self.tier_delta < 0


# Carnival states OBC in the tile's own disclaimer, e.g.
# "NO UPGRADES APPLY /OB CREDIT MAY APPLY". Absence is not proof there is no
# OBC - only /review gives the real figure - so this is a SCREEN, not a
# verdict, and it exists to choose which candidate is worth confirming.
_OBC_RE = re.compile(r"\bOB\s*CREDIT\b|\bONBOARD\s*CREDIT\b|\bOBC\b", re.I)


def mentions_obc(disclaimer: str | None) -> bool | None:
    if not disclaimer:
        return None
    return bool(_OBC_RE.search(disclaimer))


def rank_candidates(
    current_offer_name: str | None,
    current_disclaimer: str | None,
    offers: list[dict],
    guests_count: int,
    current_gross: float,
) -> list[Candidate]:
    """Every cheaper offer, best-for-the-customer first.

    THE FIX FOR A REAL DEFECT. calculate_goccl took
    `min(candidates, key=price_per_person)` and reported it. On Carnival the
    cheapest fare is reliably the most restrictive - across seven of Neon's
    bookings the cheapest was PSV "SUPER SAVER" four times, which forfeits
    price protection, makes the deposit non-refundable and hands the
    stateroom location back to Carnival. Offering only that candidate meant
    the only thing on the table was the one most likely to be a bad trade,
    and the 3x OBC rule would then have suppressed the booking entirely -
    reporting "no saving" on a booking that HAD a perfectly good one a few
    dollars further down the list.

    Ordering: keep the customer's terms first, then take the biggest drop.
    A same-tier or better fare always outranks a downgrade, however much
    cheaper the downgrade is - the downgrade is still returned, so nothing
    is hidden and Neon can take it deliberately.
    """
    current_has_obc = mentions_obc(current_disclaimer)
    out: list[Candidate] = []

    for offer in offers:
        try:
            pp = float(str(offer.get("price_per_person") or 0).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if pp <= 0:
            continue
        new_gross = round(pp * guests_count, 2)
        drop = round(current_gross - new_gross, 2)
        if drop <= 0:
            continue

        name = offer.get("offer_name") or ""
        delta, why = compare_fares(current_offer_name, name)
        has_obc = mentions_obc(offer.get("disclaimer"))
        out.append(Candidate(
            offer_code=str(offer.get("offer_code") or "").strip(),
            offer_name=name,
            price_per_person=pp,
            new_gross=new_gross,
            price_drop=drop,
            tier=fare_tier(name),
            score=fare_score(name),
            tier_delta=delta,
            terms_note=why,
            keeps_obc=has_obc,
            obc_risk=bool(current_has_obc) and has_obc is False,
        ))

    # Not cheapest-first. Terms first, then money.
    out.sort(key=lambda c: (c.is_downgrade, c.obc_risk, -c.price_drop))
    return out


def best_and_cheapest(candidates: list[Candidate]) -> tuple[Candidate | None, Candidate | None]:
    """The one to recommend, and the raw cheapest for comparison.

    Both are returned so a gated result never looks like "nothing here":
    when the cheapest is a downgrade and a safer fare sits just behind it,
    the note can say so and Neon decides.
    """
    if not candidates:
        return None, None
    cheapest = max(candidates, key=lambda c: c.price_drop)
    safe = [c for c in candidates if not c.is_downgrade and not c.obc_risk]
    best = max(safe, key=lambda c: c.price_drop) if safe else None
    return best, cheapest
