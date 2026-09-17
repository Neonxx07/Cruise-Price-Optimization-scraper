"""How MSC's discount percentages arithmetise, pinned to the real corpus.

Neon, 2026-09-01: "please do a deep research online and from the data we
captured to figure out averages."

Every figure below is taken from the 100 stored MSC invoices in
data/msc_control/, not constructed. Two findings, both exact:

  1. Stacked discounts COMPOUND. Recovering a whole-dollar brochure fare
     works 10 times out of 13 under compounding and once under addition.
  2. The base is CAB + SRN; PCH (taxes, fees, port) is excluded - which
     matches MSC's published promotional terms.

These tests exist because the mechanism is the part that is settled. The
dollar VALUES it produces are not yet reconciled with Neon's verified
answers (see msc_discount_delta's docstring), and the last test here pins
the deliberate decision not to report them as savings.
"""
import pytest

from core.calculator_msc import (
    msc_discount_delta,
    msc_discount_factor,
    msc_list_fare,
)


# -- compounding, not addition --------------------------------------


def test_two_discounts_compound():
    """0.85 x 0.95 = 0.8075, not 1 - 0.15 - 0.05 = 0.80."""
    assert msc_discount_factor([15.0, 5.0]) == pytest.approx(0.8075)
    assert msc_discount_factor([15.0, 5.0]) != pytest.approx(0.80)


def test_no_discount_is_the_identity():
    assert msc_discount_factor([]) == 1.0
    assert msc_discount_factor(None) == 1.0


def test_a_none_rate_is_skipped_not_treated_as_zero_percent():
    """SENIOR25 comes back from the catalog with no usable rate. It must not
    silently become a 0% discount that quietly widens the factor."""
    assert msc_discount_factor([5.0, None]) == pytest.approx(0.95)


# -- the whole-dollar brochure fare, from real invoices -------------


@pytest.mark.parametrize("booking,cab_gross,pcts,guests,expected", [
    # Stacked-discount invoices where compounding recovers a whole dollar
    # and addition does not.
    ("3000008", 3396.34, [15.0, 5.0], 2, 2103.00),
    ("3000078", 2052.00, [10.0, 5.0], 2, 1200.00),
    ("3000079", 1822.86, [10.0, 5.0], 2, 1066.00),
    ("3000074", 2525.86, [15.0, 5.0], 2, 1564.00),
    ("3000072", 2383.74, [10.0, 5.0], 2, 1394.00),
    ("3000082", 687.42, [10.0, 5.0], 2, 402.00),
    # Single-discount invoices, same relationship.
    ("3000070", 1066.76, [9.75], 2, 591.00),
    ("3000009", 1077.58, [9.75], 2, 597.00),
    ("3000075", 3768.84, [9.75], 2, 2088.00),
    ("3000080", 3263.44, [9.75], 2, 1808.00),
    ("3000077", 338.20, [5.0], 1, 356.00),
])
def test_the_recovered_list_fare_is_a_whole_dollar(
        booking, cab_gross, pcts, guests, expected):
    """MSC brochure fares are whole dollars, so recovering one is what
    tells us the model is right. Figures straight from each booking's
    stored invoice."""
    per_guest = msc_list_fare(cab_gross, pcts) / guests
    assert per_guest == pytest.approx(expected, abs=0.02), booking


def test_additive_stacking_would_fail_the_same_invoices():
    """The negative control. Without it, the test above only proves the
    numbers are self-consistent, not that compounding is what MSC does."""
    misses = 0
    for cab_gross, pcts, guests in (
        (3396.34, [15.0, 5.0], 2), (2052.00, [10.0, 5.0], 2),
        (1822.86, [10.0, 5.0], 2), (2525.86, [15.0, 5.0], 2),
        (2383.74, [10.0, 5.0], 2), (687.42, [10.0, 5.0], 2),
    ):
        additive = cab_gross / (1 - sum(pcts) / 100) / guests
        if abs(additive - round(additive)) > 0.02:
            misses += 1
    assert misses == 6, (
        "additive stacking should fail every one of these invoices; if it "
        "starts passing, the whole-dollar test has stopped discriminating"
    )


# -- SRN carries the same factor as the cabin fare -----------------


@pytest.mark.parametrize("observed,pcts,n_bookings", [
    (182.00, [], 10),            # undiscounted tariff
    (172.90, [5.0], 10),         # MSCCLUB5
    (164.25, [9.75], 8),         # VOYAGERS EXCLUSIVES
    (155.61, [10.0, 5.0], 6),    # compounded
    (146.96, [15.0, 5.0], 6),    # compounded
])
def test_srn_clusters_are_one_tariff_seen_through_discount_factors(
        observed, pcts, n_bookings):
    """SRN looked like a fixed per-guest charge with six different values.
    All five clusters on this sailing family resolve to the SAME $182.00
    tariff once the compounded discount factor is divided out - which is
    what proves the discount reaches non-commissionable fares too."""
    assert msc_list_fare(observed, pcts) == pytest.approx(182.00, abs=0.01), (
        f"{n_bookings} bookings sit at ${observed}/guest"
    )


def test_booking_3000081_srn_is_the_same_relationship_on_another_tariff():
    """277.97/guest across 14 bookings = a $308.00 tariff at 9.75%."""
    assert msc_list_fare(277.97, [9.75]) == pytest.approx(308.00, abs=0.01)


# -- the delta, and why it is not reported yet ---------------------


def test_adding_a_cumulable_discount_is_worth_the_new_rate_on_the_base():
    """Both discount sets are absolute. Adding 5% on top of 9.75% leaves
    the list fare untouched and moves the factor from 0.9025 to 0.857375."""
    got = msc_discount_delta(2671.40, 555.94, [9.75], [9.75, 5.0])
    assert got == pytest.approx(161.37, abs=0.01)


def test_a_tier_upgrade_is_worth_only_the_difference():
    """9.75% -> 10% is a quarter of a point, not ten points. The old code
    had no way to express this at all."""
    got = msc_discount_delta(2671.40, 555.94, [9.75], [10.0])
    assert got == pytest.approx(8.94, abs=0.05)
    assert got < 10.0, "a 0.25-point move must not look like a large saving"


def test_a_worse_discount_yields_a_negative_delta_not_a_saving():
    assert msc_discount_delta(2671.40, 555.94, [10.0], [5.0]) < 0


def test_missing_inputs_return_none_rather_than_a_guess():
    assert msc_discount_delta(None, 100.0, [5.0], [10.0]) is None
    assert msc_discount_delta(0.0, 100.0, [5.0], [10.0]) is None
    assert msc_list_fare(None, [5.0]) is None
    assert msc_list_fare(0.0, [5.0]) is None


def test_the_delta_is_not_wired_into_any_reported_saving():
    """THE DELIBERATE GAP, 2026-09-01. The mechanism is confirmed against
    100 invoices but its dollar values miss the verified answers high:

        3000081  real $81.98  vs $161.37    3000083  real $23.66  vs $85.03

    3000071 is deliberately NOT cited here. Its $63.24 was the second half
    of an apparent 3.07%-of-CAB agreement that drove this whole
    investigation - and Neon then said the booking is OVERPAID and not
    optimizable at all, so that figure was never a discount saving. Two
    numbers agreeing to 0.012 points looked like strong evidence and was
    coincidence. Do not reinstate it.

    Reporting these as savings would be the exact failure this module spent
    the session eliminating - a confidently wrong figure that gets acted on.
    So no check may consume msc_discount_delta until the missing factor is
    named. This test fails the moment someone wires it up, which is the
    point: that change needs a deliberate decision, not a quiet import.
    """
    import inspect

    import core.calculator_msc as mod

    src = inspect.getsource(mod)
    body = src[src.index("def _check_discount_add"):src.index(
        "# How MSC's discount percentages actually arithmetise")]
    assert "msc_discount_delta" not in body, (
        "msc_discount_delta is now feeding a check - confirm the dollar "
        "values reconcile with Neon's verified answers first, then update "
        "this test with the evidence"
    )


# -- overpayment is a hard stop -------------------------------------


def test_an_overpaid_booking_is_not_optimizable_at_all():
    """HARD RULE, Neon 2026-09-01: "3000071 this booking has an
    overpayment it is not optimizable."

    Overpayment was already detected but only fed `is_paid_in_full`, which
    softens PRICE_MATCH while leaving all three discount checks free to
    report an opportunity. It is a property of the booking, not of one
    lever, so it gates everything - the same shape as a cancelled sailing.
    """
    from core.calculator_msc import evaluate_msc_booking

    result = evaluate_msc_booking(
        booking_id="3000071", category="BP",
        current_base_price=None, today_base_price=1.0,
        current_total_price=2357.72,
        current_discounts=[], today_discount_options=["SENIOR DISCOUNT"],
        club_discount_offered=True, today_price_tab_confirmed=True,
        is_overpayment=True,
    )
    assert result.checks == [], "an overpaid booking must produce no checks"
    assert result.has_any_opportunity is False
    assert "OVERPAID" in result.note
    assert result.is_paid_in_full is True


def test_a_normally_paid_booking_still_gets_its_checks():
    """The gate must not swallow ordinary bookings."""
    from core.calculator_msc import evaluate_msc_booking

    result = evaluate_msc_booking(
        booking_id="OK1", category="BP",
        current_base_price=None, today_base_price=1000.0,
        current_total_price=2357.72,
        current_discounts=[], today_discount_options=["SENIOR DISCOUNT"],
        club_discount_offered=True, today_price_tab_confirmed=True,
        is_overpayment=False,
    )
    assert result.checks, "a normal booking lost all of its checks"
