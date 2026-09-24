"""A cheaper Carnival fare is often a worse product.

Neon 2026-09-18: "i want you to search online so we include the fair rate
promotion as best to worst for the cx benefet and we add it in our scoring
system th 1 to 5 system".

THE LIVE CASE THAT MOTIVATED THIS. A scan of seven of Neon's bookings
recommended PSV "SUPER SAVER" on four of them. Super Saver has a
non-refundable deposit, no price protection, and Carnival - not the guest -
assigns the stateroom location. Several of those bookings sit on an EARLY
SAVER rate today, which has price protection and lets the guest keep their
cabin. Reporting "$117 saving" on that swap, with nothing said about the
terms, is the same shape of error as every fabricated saving in this
project's history: two numbers compared that do not cover the same thing.
"""
import pytest

from core.goccl_fare_types import (
    EARLY_SAVER,
    FUN_SELECT,
    PACK_AND_GO,
    PROMOTIONAL,
    SUPER_SAVER,
    TERMS,
    TIERS,
    compare_fares,
    fare_score,
    fare_tier,
)


# ── every offer name seen live on Neon's seven bookings ─────────────────


@pytest.mark.parametrize("code,name,expected", [
    ("PNS", "FUN SELECT", FUN_SELECT),
    ("PSV", "SUPER SAVER", SUPER_SAVER),
    ("OB7", "SUN-SATIONAL EARLY SAVER BONUS SALE", EARLY_SAVER),
    ("OJS", "SUNSHINE AHEAD EARLY SAVER BONUS SALE", EARLY_SAVER),
    ("O7O", "SAVE & SAIL: MORE TIME MORE PERKS SALE", PROMOTIONAL),
    ("GO2", "BUNDLE & SAVE: UPGRADE YOUR FUN", PROMOTIONAL),
    ("PB4", "SEPTEMBER SAVINGS SALE", PROMOTIONAL),
    ("PHY", "BRING THE BUNCH - SAVE A BUNCH SALE", PROMOTIONAL),
])
def test_every_real_offer_name_classifies(code, name, expected):
    assert fare_tier(name) == expected, code


def test_the_tier_comes_from_the_NAME_not_the_CODE():
    """OB7 and OJS are different campaign codes for the SAME fare type, and
    PSV/PNS/PB4/PHY share a P prefix while being completely different
    products. Keying on the code would be keying on noise - the same lesson
    as core/princess_packages.py."""
    assert fare_tier("SUN-SATIONAL EARLY SAVER BONUS SALE") == fare_tier(
        "SUNSHINE AHEAD EARLY SAVER BONUS SALE")
    assert fare_tier("FUN SELECT") != fare_tier("SUPER SAVER")


def test_a_named_fare_type_beats_a_generic_sale_match():
    """Real names combine both - "SUN-SATIONAL EARLY SAVER BONUS SALE"
    contains SALE and BONUS as well as EARLY SAVER. The fare type must win,
    or every Early Saver would be mis-filed as a generic promotion."""
    assert fare_tier("SUN-SATIONAL EARLY SAVER BONUS SALE") == EARLY_SAVER


def test_an_unreadable_name_is_unknown_not_a_default_tier():
    """Assuming a tier would understate what a switch gives up."""
    assert fare_tier("") is None
    assert fare_tier(None) is None
    assert fare_tier("XYZZY") is None
    assert fare_score(None) is None


# ── the 1-5 customer-benefit scale ───────────────────────────────────────


def test_the_scale_runs_1_to_5_best_for_the_customer_highest():
    assert fare_score("FUN SELECT") == 5
    assert fare_score("SUN-SATIONAL EARLY SAVER BONUS SALE") == 4
    assert fare_score("SEPTEMBER SAVINGS SALE") == 3
    assert fare_score("SUPER SAVER") == 2
    assert {t.score for t in TERMS.values()} == {1, 2, 3, 4, 5}


def test_the_scale_ranks_BENEFIT_not_price():
    """THE WHOLE POINT. On booking DEMO08 the cheapest INTERIOR was OB7 at
    1,392 and the most expensive was PNS FUN SELECT at 2,362 - and FUN
    SELECT is the BEST fare for the customer, not the worst. A ranking that
    tracked price would be upside down."""
    cheapest, dearest = "SUPER SAVER", "FUN SELECT"
    assert fare_score(cheapest) < fare_score(dearest)


def test_the_tiers_are_ordered_worst_to_best():
    assert TIERS == (PACK_AND_GO, SUPER_SAVER, PROMOTIONAL, EARLY_SAVER, FUN_SELECT)
    assert [TERMS[t].score for t in TIERS] == [1, 2, 3, 4, 5]


# ── what a switch actually costs ─────────────────────────────────────────


def test_early_saver_to_super_saver_is_flagged_as_a_downgrade():
    """The real recommendation made on DEMO09: currently SUNSHINE AHEAD
    EARLY SAVER, cheapest alternative SUPER SAVER."""
    delta, why = compare_fares("SUNSHINE AHEAD EARLY SAVER BONUS SALE", "SUPER SAVER")
    assert delta == -2
    assert "DOWNGRADE" in why
    assert "price protection" in why
    assert "stateroom location" in why


def test_the_downgrade_names_what_is_lost_in_plain_words():
    """A note a human can act on - the same standard as the ESPRESSO
    dual-rate note."""
    _, why = compare_fares("SUN-SATIONAL EARLY SAVER BONUS SALE", "SUPER SAVER")
    assert "Carnival assigns it" in why


def test_a_same_tier_switch_is_not_a_downgrade():
    """DEMO10's $353 candidate: PROMOTIONAL -> PROMOTIONAL. The cleanest of
    the seven, and the guard must not muddy it."""
    delta, why = compare_fares("SAVE & SAIL: MORE TIME MORE PERKS SALE",
                               "BRING THE BUNCH - SAVE A BUNCH SALE")
    assert delta == 0
    assert "same fare type" in why
    assert "DOWNGRADE" not in why


def test_moving_UP_a_tier_is_reported_as_such():
    delta, why = compare_fares("SUPER SAVER", "FUN SELECT")
    assert delta == 3
    assert "UPGRADE" in why


def test_an_unreadable_tier_returns_no_verdict_rather_than_a_false_zero():
    """Silence beats a confident wrong answer - this must not look like
    "no change in terms"."""
    delta, why = compare_fares("XYZZY", "SUPER SAVER")
    assert delta == 0
    assert "could not be read" in why
    assert "no change in terms" not in why


def test_super_saver_terms_are_recorded_accurately():
    """Researched 2026-09-18 from Carnival's own terms and corroborating
    sources: non-refundable deposit, no price protection, and the stateroom
    LOCATION assigned by Carnival (the guest still picks the type)."""
    t = TERMS[SUPER_SAVER]
    assert t.deposit_refundable is False
    assert t.price_protection is False
    assert t.guest_picks_cabin is False


def test_early_saver_keeps_price_protection_and_the_cabin():
    t = TERMS[EARLY_SAVER]
    assert t.price_protection is True
    assert t.guest_picks_cabin is True


def test_a_promotional_fares_terms_are_marked_UNKNOWN_not_assumed():
    """Campaign sales name no fare type and their terms genuinely vary.
    Claiming a refundability either way would be inventing a fact."""
    assert TERMS[PROMOTIONAL].deposit_refundable is None
    assert "vary" in TERMS[PROMOTIONAL].summary


# ── ranking candidates: terms first, then money ──────────────────────────


from core.goccl_fare_types import best_and_cheapest, mentions_obc, rank_candidates

# ZM57P7's REAL BALCONY column, captured live 2026-09-18. 1 guest, current
# gross 1,362.04 on OCS "SAVE & SAIL: MORE TIME MORE PERKS SALE".
ZM57P7_OFFERS = [
    {"offer_code": "GO2", "offer_name": "BUNDLE & SAVE: UPGRADE YOUR FUN",
     "disclaimer": "NO UPGRADES APPLY", "price_per_person": "1,585.00"},
    {"offer_code": "OB7", "offer_name": "SUN-SATIONAL EARLY SAVER BONUS SALE",
     "disclaimer": "NO UPGRADES APPLY", "price_per_person": "1,265.00"},
    {"offer_code": "PB4", "offer_name": "SEPTEMBER SAVINGS SALE",
     "disclaimer": "NO UPGRADES APPLY", "price_per_person": "1,325.00"},
    {"offer_code": "PNS", "offer_name": "FUN SELECT",
     "disclaimer": "UPGRADES MAY APPLY", "price_per_person": "1,965.00"},
    {"offer_code": "PSV", "offer_name": "SUPER SAVER",
     "disclaimer": "NO UPGRADES APPLY", "price_per_person": "1,245.00"},
]
CURRENT = "SAVE & SAIL: MORE TIME MORE PERKS SALE"


def _ranked():
    return rank_candidates(CURRENT, None, ZM57P7_OFFERS, 1, 1362.04)


def test_the_cheapest_fare_is_not_recommended_when_it_downgrades():
    """THE REAL CASE. calculate_goccl took min(price_per_person) and
    returned PSV SUPER SAVER at $117.04 - forfeiting the guest's choice of
    stateroom, price protection and a refundable deposit. OB7 EARLY SAVER
    saves $97.04, keeps the cabin, and ADDS price protection. $20 buys all
    of that back."""
    best, cheapest = best_and_cheapest(_ranked())
    assert cheapest.offer_code == "PSV"
    assert cheapest.is_downgrade
    assert best.offer_code == "OB7"
    assert not best.is_downgrade
    assert round(cheapest.price_drop - best.price_drop, 2) == 20.00


def test_the_downgrade_is_still_returned_never_hidden():
    """Whether to take a downgrade is Neon's commercial call. The ranking
    orders it last; it must not remove it."""
    codes = [c.offer_code for c in _ranked()]
    assert "PSV" in codes
    assert codes[-1] == "PSV"


def test_a_more_expensive_offer_is_not_a_candidate_at_all():
    """PNS FUN SELECT at 1,965 is the best fare type on the page but costs
    more than the booking - it is not a saving and must not be ranked as
    one."""
    assert "PNS" not in [c.offer_code for c in _ranked()]


def test_ordering_is_terms_first_then_size_of_drop():
    ranked = _ranked()
    assert [c.offer_code for c in ranked] == ["OB7", "PB4", "PSV"]
    # OB7 (97.04) outranks PB4 (37.04) on money, both outrank the downgrade
    assert ranked[0].price_drop > ranked[1].price_drop
    assert ranked[-1].price_drop > ranked[0].price_drop   # the downgrade IS cheaper


def test_when_the_cheapest_keeps_the_terms_nothing_is_given_up():
    """DEMO10's real BALCONY column: PHY at 514.00pp is both the cheapest
    AND the same fare type, so the safe answer and the cheap answer agree
    and the ranking must not invent a cost."""
    offers = [
        {"offer_code": "PHY", "offer_name": "BRING THE BUNCH - SAVE A BUNCH SALE",
         "disclaimer": "NO UPGRADES APPLY", "price_per_person": "514.00"},
        {"offer_code": "PSV", "offer_name": "SUPER SAVER",
         "disclaimer": "NO UPGRADES APPLY", "price_per_person": "524.00"},
    ]
    best, cheapest = rank_candidates(CURRENT, None, offers, 3, 1895.00), None
    best, cheapest = best_and_cheapest(best)
    assert best.offer_code == cheapest.offer_code == "PHY"
    assert best.price_drop == cheapest.price_drop == 353.00


def test_obc_is_screened_from_the_offers_own_disclaimer():
    """Carnival states it in the tile: "NO UPGRADES APPLY /OB CREDIT MAY
    APPLY". A SCREEN, not a verdict - only /review gives the real figure."""
    assert mentions_obc("NO UPGRADES APPLY /OB CREDIT MAY APPLY") is True
    assert mentions_obc("NO UPGRADES APPLY") is False
    assert mentions_obc(None) is None


def test_losing_an_advertised_obc_pushes_a_candidate_down_the_ranking():
    """DEMO08's real disclaimers: GO2 and OB7 advertise OB CREDIT, PB4 does
    not. Moving off an OBC-bearing fare onto one that says nothing is
    flagged and deprioritised, so the 3x rule is applied to a candidate that
    was CHOSEN for its terms rather than one that merely happened to be
    cheapest."""
    offers = [
        {"offer_code": "OB7", "offer_name": "SUN-SATIONAL EARLY SAVER BONUS SALE",
         "disclaimer": "NO UPGRADES APPLY /OB CREDIT MAY APPLY",
         "price_per_person": "1,392.00"},
        {"offer_code": "PB4", "offer_name": "SEPTEMBER SAVINGS SALE",
         "disclaimer": "NO UPGRADES APPLY", "price_per_person": "1,300.00"},
    ]
    ranked = rank_candidates("SUN-SATIONAL EARLY SAVER BONUS SALE",
                             "NO UPGRADES APPLY /OB CREDIT MAY APPLY",
                             offers, 1, 2000.00)
    by_code = {c.offer_code: c for c in ranked}
    assert by_code["PB4"].obc_risk is True
    assert by_code["OB7"].obc_risk is False
    best, cheapest = best_and_cheapest(ranked)
    assert cheapest.offer_code == "PB4"     # genuinely the biggest drop
    assert best.offer_code == "OB7"         # but OB7 keeps the OBC and the tier


def test_no_cheaper_offer_at_all_returns_nothing_rather_than_a_false_pick():
    best, cheapest = best_and_cheapest(
        rank_candidates(CURRENT, None, ZM57P7_OFFERS, 1, 100.00))
    assert best is None and cheapest is None
