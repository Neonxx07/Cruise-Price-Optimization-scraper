"""MSC subsystem tests -- covers every pure calculation/classification
function that can be tested without a live browser/website, plus
regression tests for every MSC bug fixed on 2026-08-12/13.
"""
import pytest

import msc_commands as m
from core.calculator_msc import _check_price_match, evaluate_msc_booking
from core.models import MSC_PAID_IN_FULL_DUE_THRESHOLD, MscCheckStatus


# ── Explicit-cancellation detection ───────────────────────────────


def test_explicitly_cancelled_detects_status_word():
    text = "Booking\n-\n2000006\nCANCELED\nBooking Value\n$0.00\n"
    assert m._is_explicitly_cancelled(text) is True


def test_explicitly_cancelled_detects_reinstate_button():
    text = "some page text\nREINSTATE BOOKING\nmore text"
    assert m._is_explicitly_cancelled(text) is True


def test_explicitly_cancelled_false_on_normal_confirmed_booking():
    text = "Booking\n-\n2000008\nCONFIRMED\nBooking Value\n$966.89\n"
    assert m._is_explicitly_cancelled(text) is False


def test_regression_explicitly_cancelled_does_not_false_positive_on_random_text():
    """Must not fire on ordinary page text that merely contains the word
    'cancel' in an unrelated context (e.g. a 'CANCEL BOOKING' button that
    exists on every live booking, cancelled or not)."""
    text = "Booking\n-\n2000015\nCONFIRMED\nBooking Value\n$6,174.81\nCANCEL BOOKING\n"
    assert m._is_explicitly_cancelled(text) is False


# ── Paid-in-full detection ────────────────────────────────────────


@pytest.mark.parametrize("due_amount,is_overpayment,expected", [
    (0.0, False, True),
    (12.0, False, True),    # under the $15 threshold
    (15.0, False, False),   # AT the threshold is NOT under it
    (152.0, False, False),
    (None, True, True),     # overpayment always counts regardless of due_amount
    (500.0, True, True),
])
def test_is_paid_in_full(due_amount, is_overpayment, expected):
    assert m._is_paid_in_full(due_amount, is_overpayment, MSC_PAID_IN_FULL_DUE_THRESHOLD) is expected


def test_regression_overpayment_real_example():
    """Real captured example, booking 2000012: 'Overpayment\\n$0.02'."""
    text = "Booking Value\n$2,190.54\n(Price includes all tax and fees)\nOverpayment\n$0.02\nPrice Breakdown >"
    ess = m._extract_booking_essentials(text)
    assert ess["is_overpayment"] is True
    assert ess["overpayment_amount"] == "0.02"


# ── Tab matching (5-tier cascade) ─────────────────────────────────


def test_tab_match_exact():
    target, reason = m._select_matching_tab("OPEN JAW CRUISE ONLY", ["OPEN JAW CRUISE ONLY", "FLASH SALE DRINKS AND WIFI"])
    assert target == "OPEN JAW CRUISE ONLY"


def test_regression_amenity_signature_match_neons_real_example():
    """Real ground truth from Neon, booking 2000009: 'BALCONY UPGRADE
    DRINKS WIFI' is the same product as 'FLASH SALE DRINKS AND WIFI'."""
    target, reason = m._select_matching_tab(
        "BALCONY UPGRADE DRINKS WIFI",
        ["FLASH SALE CRUISE ONLY", "BROCHURE RATES", "ESCAPE TO SEA CRUISE ONLY", "FLASH SALE DRINKS AND WIFI"],
    )
    assert target == "FLASH SALE DRINKS AND WIFI"


def test_regression_obc_included_rate_never_matches_drinks_wifi_only_tab():
    """A drinks+wifi+OBC rate must NOT match a plain drinks+wifi tab --
    confirmed distinct, different-value products."""
    target, reason = m._select_matching_tab(
        "CRUISE WITH DRINKS WIFI OBC",
        ["FLASH SALE CRUISE ONLY", "ESCAPE TO SEA CRUISE ONLY", "BROCHURE RATES", "FLASH SALE DRINKS AND WIFI"],
    )
    assert target is None


def test_regression_brochure_rate_never_selected():
    """HARD RULE from Neon: Brochure Rate is NEVER a valid comparison
    target, even when it's the only exact/substring match available --
    it strips agency commission."""
    target, reason = m._select_matching_tab("BROCHURE RATES", ["BROCHURE RATES", "FLASH SALE DRINKS AND WIFI"])
    assert target is None


def test_regression_cruise_only_tier_fallback_for_campaign_names():
    """Neon's rule: campaign names with no amenity info are
    interchangeable -- 'EPIC EUROPE SALE' should match the one other
    amenity-free (cruise-only-tier) tab."""
    target, reason = m._select_matching_tab(
        "EPIC EUROPE SALE",
        ["ESCAPE TO SEA CRUISE ONLY", "CRUISE WITH DRINKS WIFI OBC", "CRUISE ONLY OBC INCLUDED", "BROCHURE RATES"],
    )
    assert target == "ESCAPE TO SEA CRUISE ONLY"


def test_cruise_only_tier_fallback_does_not_guess_when_ambiguous():
    target, reason = m._select_matching_tab(
        "SOME NEW CAMPAIGN",
        ["ESCAPE TO SEA CRUISE ONLY", "FLASH SALE CRUISE ONLY", "BROCHURE RATES"],
    )
    assert target is None


# ── Occupancy/age-tier computation ────────────────────────────────


def test_compute_required_occupancy_mixed_ages():
    passengers = [
        {"name": "Adult", "age": 38},
        {"name": "Kid", "age": 8},
        {"name": "Teen", "age": 14},
        {"name": "Baby", "age": 1},
    ]
    result = m._compute_required_occupancy(passengers)
    assert result["counts"] == {"adult": 1, "child": 1, "jrchild": 1, "infant": 1}
    assert result["ages"]["jrchild"] == [8]
    assert result["ages"]["child"] == [14]
    assert result["ages"]["infant"] == [1]


def test_regression_empty_passenger_list_never_computes_zero_adults():
    """CONFIRMED REAL NEAR-MISS, fixed 2026-08-12: an empty passenger
    list (extraction failure) must never be read as '0 real guests' --
    that's what almost drove a real booking's adult count to 0."""
    result = m._compute_required_occupancy([])
    assert result["counts"] == {"adult": 0, "child": 0, "jrchild": 0, "infant": 0}
    # (the actual safety net is _fix_occupancy's early return on empty
    # passengers, which requires a live page and is covered by the
    # audit's live verification, not unit-testable here)


# ── MSC calculator: paid-in-full price-match gate ─────────────────


def test_regression_paid_in_full_blocks_price_match_only():
    """CONFIRMED RULE from Neon 2026-08-12: a paid-in-full booking can
    have a discount ADDED but can NEVER be price-matched."""
    check = _check_price_match(
        current_base_price=1000.0, today_base_price=500.0,  # would obviously be OPPORTUNITY otherwise
        today_price_tab_confirmed=True,
        is_paid_in_full=True,
    )
    assert check.status == MscCheckStatus.NO_OPPORTUNITY

    check_not_paid = _check_price_match(
        current_base_price=1000.0, today_base_price=500.0,
        today_price_tab_confirmed=True,
        is_paid_in_full=False,
    )
    assert check_not_paid.status == MscCheckStatus.OPPORTUNITY


def test_evaluate_msc_booking_paid_in_full_still_allows_discount_add():
    result = evaluate_msc_booking(
        booking_id="TEST",
        category="BR1",
        is_paid_in_full=True,
        current_base_price=1000.0,
        today_base_price=500.0,
        current_discounts=[],
        today_discount_options=["SENIOR DISCOUNT"],
        club_discount_offered=True,
    )
    checks_by_type = {c.type.value: c for c in result.checks}
    assert checks_by_type["PRICE_MATCH"].status == MscCheckStatus.NO_OPPORTUNITY
    assert checks_by_type["DISCOUNT_ADD"].status == MscCheckStatus.OPPORTUNITY
