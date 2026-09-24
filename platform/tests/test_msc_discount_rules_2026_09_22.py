"""A discount is only an opportunity if the REPRICED total beats the fare held.

Neon 2026-09-22, answering directly which of two readings was right:

    "(b) reprice at today's 2,109.54 rate with the discount applied AND THE
     CUSTOMER HAS A LOWER PRICE"
    "if the final payment has passed we can only apply a discount but not
     price match if the price has dropped"
    "SPECIAL OFFER 10% ... this cannot be applied if the customer is senior
     so if seniors we check discount percentage is higher"

Adding a discount does NOT shave a percentage off the customer's existing
fare - MSC reprices at TODAY'S rate and discounts that. Booking 3000005:

    current total          1,756.42
    today, category BL3    2,109.54   (+353.12)
      after 5%  club       2,004.06   (+247.64)
      after 10% selection  1,898.59   (+142.17)
      after 15% both       1,793.11   (+ 36.69)

Every one leaves the customer worse off, and the run reported two of them as
OPPORTUNITY. Same failure as MSC's fabricated $267.01 and GoCCL's +$184 that
was really -$6: a percentage quoted against a base nobody checked.

Rules and evidence: docs/MSC_DISCOUNT_RULES.md
"""
import pytest

from core.calculator_msc import (
    best_eligible_discount,
    msc_discount_beats_current,
    special_offer_allowed,
)

CURRENT = 1756.42          # booking 3000005's real total
TODAY = 2109.54            # today, same category BL3


# ── MSC-D1 / D2: the reprice rule ────────────────────────────────────────


@pytest.mark.parametrize("pct,repriced", [
    (5.0, 2004.06), (10.0, 1898.59), (15.0, 1793.11),
])
def test_no_discount_on_3000005_beats_the_fare_already_held(pct, repriced):
    """THE REGRESSION. All three were reported as opportunities."""
    beats, why = msc_discount_beats_current(CURRENT, TODAY, pct)
    assert beats is False
    assert f"{repriced:,.2f}" in why
    assert "ABOVE" in why


def test_the_note_says_what_it_would_cost():
    """An agent has to see the consequence, not just a refusal."""
    _, why = msc_discount_beats_current(CURRENT, TODAY, 15.0)
    assert "36.69" in why          # exactly how much worse off


def test_a_discount_that_DOES_beat_the_fare_is_an_opportunity():
    """The rule must not simply suppress everything - when today's price is
    low enough, the discount is real."""
    beats, why = msc_discount_beats_current(2000.00, 2100.00, 10.0)
    assert beats is True
    assert "1,890.00" in why and "below" in why


def test_the_boundary_is_strict():
    """Landing exactly ON the current fare is not a saving."""
    beats, _ = msc_discount_beats_current(1890.00, 2100.00, 10.0)
    assert beats is False


# ── missing stays missing ────────────────────────────────────────────────


@pytest.mark.parametrize("current,today,pct", [
    (None, TODAY, 10.0),
    (CURRENT, None, 10.0),
    (CURRENT, TODAY, None),
    (CURRENT, TODAY, 0.0),
    (CURRENT, 0.0, 10.0),
])
def test_an_unknown_input_yields_None_not_a_verdict(current, today, pct):
    """UNKNOWN must stay UNKNOWN. A missing today price silently becoming
    NO_OPPORTUNITY would hide real opportunities; becoming OPPORTUNITY would
    invent them."""
    beats, why = msc_discount_beats_current(current, today, pct)
    assert beats is None
    assert why


# ── MSC-D5: SPECIAL OFFER and the senior discount are exclusive ─────


def test_special_offer_is_blocked_only_once_the_senior_discount_is_applied():
    """Neon, correcting my first reading the same day:

        "only when the senior discount is actually applied? u can choose
         between one only i usually choose whatever is higher"

    Being a senior does not block SPECIAL OFFER. Holding the senior
    discount does.
    """
    assert special_offer_allowed(False) is True
    assert special_offer_allowed(True) is False


def test_a_senior_aboard_may_still_take_the_special_offer():
    """The regression my stricter version would have caused: a 65-year-old
    on the booking suppressing a perfectly usable 15%."""
    options = [("SPECIAL OFFER 15%", 15.0), ("SENIOR DISCOUNT", 8.0)]
    label, pct = best_eligible_discount(options, senior_discount_applied=False)
    assert label == "SPECIAL OFFER 15%" and pct == 15.0


def test_special_offer_drops_out_once_the_senior_discount_is_on_the_booking():
    """F2: MSC carries ONE discount. With the senior discount applied the
    special offer is not a candidate, whatever its size."""
    options = [("SPECIAL OFFER 15%", 15.0), ("SENIOR DISCOUNT", 8.0)]
    label, pct = best_eligible_discount(options, senior_discount_applied=True)
    assert label == "SENIOR DISCOUNT" and pct == 8.0


# ── MSC-D6: one discount only, take the higher ────────────────────


def test_the_biggest_usable_discount_wins():
    options = [("SPECIAL OFFER 10%", 10.0), ("MSC CLUB", 5.0)]
    assert best_eligible_discount(options) == ("SPECIAL OFFER 10%", 10.0)


def test_the_higher_senior_percentage_wins_when_it_is_higher():
    options = [("SPECIAL OFFER 10%", 10.0), ("SENIOR DISCOUNT", 12.0)]
    assert best_eligible_discount(options)[1] == 12.0


def test_percentages_are_picked_never_summed():
    """"u can choose between one only" - 10% + 5% is not 15%."""
    options = [("SPECIAL OFFER 10%", 10.0), ("MSC CLUB", 5.0)]
    assert best_eligible_discount(options)[1] == 10.0


@pytest.mark.parametrize("options", [None, [], [("SPECIAL OFFER 10%", 0.0)]])
def test_nothing_usable_returns_None_not_zero(options):
    assert best_eligible_discount(options) is None


def test_senior_discount_applied_and_only_a_special_offer_left_has_nothing():
    """Not "0% available" - nothing usable at all."""
    assert best_eligible_discount([("SPECIAL OFFER 15%", 15.0)],
                                  senior_discount_applied=True) is None
