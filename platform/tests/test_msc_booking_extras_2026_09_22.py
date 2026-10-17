"""OBC / perks at the bottom of an MSC booking page.

Every fixture below is copied from real captured pages in
``data/msc_control/booking_data.jsonl``, not invented. Booking 3000077 is
the arithmetic anchor: 338.20 + 25.00 + 75.00 + 62.70 = 500.90, which is the
stateroom total MSC itself prints, so the $25 shipboard credit is provably
inside the booking total.
"""

from decimal import Decimal

import pytest

from core.msc_booking_extras import (
    included_perks,
    parse_applied_discounts,
    parse_obs_lines,
    purchased_extras_total,
    senior_discount_applied,
)

# Booking 3000077, verbatim - a PURCHASED $25 credit alongside a free perk.
PURCHASED = "\n".join([
    "Item\tDescription\tCommission\tBonus Commission\tCommission Percentage\tNet Price\tGross Price",
    "CAB\tDELUXE BALCONY DECK 9-10\t$57.49\t$0.00\t17%\t$280.71\t$338.20",
    "OBS\tShipboard Credit 25 USD - Non Refundable\t$0.00\t$0.00\t-\t$25.00\t$25.00",
    "OBS\tFANTASTICA EXPERIENCE BENEFITS\t$0.00\t$0.00\t-\t$0.00\t$0.00",
    "PCH\tTaxes, Fees & Port Expenses\t$0.00\t$0.00\t-\t$75.00\t$75.00",
    "SRN\tNon commissionable fares\t$0.00\t$0.00\t-\t$62.70\t$62.70",
    "Total Adult 1\t$57.49\t$0.00\t-\t$443.41\t$500.90",
    "MSC Club Discount: MSCCLUB5 - Discount Type: Percentage - Discount Rate: 5.0%",
])

# Booking 3000070, verbatim - perks bundled free with an AUREA fare.
FREE_PERKS = "\n".join([
    "CAB\tDELUXE BALCONY AUREA\t$90.67\t$0.00\t17%\t$442.71\t$533.38",
    "OBS\tShipboard credit 50 Eur/Usd - 40 Gbp\t$0.00\t$0.00\t-\t$0.00\t$0.00",
    "OBS\tMINERAL WATER AND COFFEE IN DINING ROOM\t$0.00\t$0.00\t-\t$0.00\t$0.00",
    "OBS\tAUREA EXPERIENCE BENEFITS\t$0.00\t$0.00\t-\t$0.00\t$0.00",
    "PCH\tTaxes, Fees & Port Expenses\t$0.00\t$0.00\t-\t$30.00\t$30.00",
    "Discount Description: VOYAGERS EXCLUSIVES - Discount Type: Percentage - Discount Rate: 9.75%",
])

# Booking 3000079 - the only senior-discount booking in 133.
SENIOR = (
    "Discount Description: SENIOR DISCOUNT - Discount Type: Percentage - Discount Rate: 10.0%\n"
    "MSC Club Discount: MSCCLUB5 - Discount Type: Percentage - Discount Rate: 5.0%"
)


# ── purchased credit is inside the total ─────────────────────────────────


def test_a_purchased_shipboard_credit_is_found_and_priced():
    obs = parse_obs_lines(PURCHASED)
    assert len(obs) == 2
    credit = obs[0]
    assert credit.description == "Shipboard Credit 25 USD - Non Refundable"
    assert credit.gross == Decimal("25.00")
    assert credit.is_priced and not credit.is_included_perk


def test_purchased_extras_total_matches_the_printed_arithmetic():
    """338.20 + 25.00 + 75.00 + 62.70 = 500.90. The $25 is IN the total, so a
    cabin-only quote for today is $25 short of comparable."""
    assert purchased_extras_total(PURCHASED) == Decimal("25.00")
    assert Decimal("338.20") + purchased_extras_total(PURCHASED) + Decimal(
        "75.00") + Decimal("62.70") == Decimal("500.90")


def test_free_perks_contribute_nothing_to_the_total():
    assert purchased_extras_total(FREE_PERKS) == Decimal("0")
    assert Decimal("533.38") + Decimal("30.00") + Decimal("79.42") == Decimal("642.80")


# ── free perks are value, and must not be mistaken for purchases ─────────


def test_included_perks_are_listed_but_never_charged():
    perks = included_perks(FREE_PERKS)
    assert "AUREA EXPERIENCE BENEFITS" in perks
    assert "Shipboard credit 50 Eur/Usd - 40 Gbp" in perks
    assert purchased_extras_total(FREE_PERKS) == Decimal("0")


def test_a_purchased_credit_is_not_reported_as_an_included_perk():
    assert included_perks(PURCHASED) == ["FANTASTICA EXPERIENCE BENEFITS"]


# ── missing is not zero ──────────────────────────────────────────────────


def test_an_unreadable_obs_line_is_never_scored_as_free():
    row = "OBS\tShipboard Credit\t-\t-\t-\t-\t-"
    line = parse_obs_lines(row)[0]
    assert line.gross is None
    assert line.is_unreadable
    assert not line.is_priced and not line.is_included_perk


@pytest.mark.parametrize("text", [None, "", "CAB\tBALCONY\t$1.00\t$1.00"])
def test_no_obs_lines_is_an_empty_answer_not_an_error(text):
    assert parse_obs_lines(text) == []
    assert purchased_extras_total(text) == Decimal("0")


def test_a_description_merely_starting_with_OBS_is_not_an_obs_line():
    assert parse_obs_lines("OBSERVATION LOUNGE\tsomething\t$5.00") == []


# ── the applied-discount block ───────────────────────────────────────────


def test_both_printed_discount_forms_are_read():
    assert parse_applied_discounts(PURCHASED)[0].name == "MSCCLUB5"
    assert parse_applied_discounts(FREE_PERKS)[0].name == "VOYAGERS EXCLUSIVES"


def test_voyagers_exclusives_rate_is_taken_from_the_page_not_assumed():
    """9.75%, not the 10% a catalogue would guess - and 9.75% is exactly
    1 - 0.95 x 0.95, MSC's own merge of two 5% discounts."""
    d = parse_applied_discounts(FREE_PERKS)[0]
    assert d.rate == 9.75
    assert round((1 - 0.95 * 0.95) * 100, 2) == 9.75


def test_loyalty_and_promotional_slots_are_distinguished():
    club, = [d for d in parse_applied_discounts(SENIOR) if d.is_loyalty]
    promo, = [d for d in parse_applied_discounts(SENIOR) if d.is_promotional]
    assert club.name == "MSCCLUB5" and club.rate == 5.0
    assert promo.name == "SENIOR DISCOUNT" and promo.rate == 10.0


# ── the signal MSC-D5 actually needs ─────────────────────────────────────


def test_senior_discount_applied_is_read_off_the_page():
    assert senior_discount_applied(SENIOR) is True
    assert senior_discount_applied(PURCHASED) is False
    assert senior_discount_applied(FREE_PERKS) is False


def test_a_booking_with_a_club_discount_alone_does_not_block_special_offer():
    """The correction Neon made: only the senior discount being APPLIED
    blocks SPECIAL OFFER. A 5% club discount does not."""
    from core.calculator_msc import best_eligible_discount
    applied = senior_discount_applied(PURCHASED)
    assert best_eligible_discount(
        [("SPECIAL OFFER 15%", 15.0)], senior_discount_applied=applied,
    ) == ("SPECIAL OFFER 15%", 15.0)
