"""Findings from the cross-line audit — Neon: "review everything".

The MSC work this session established that the dangerous defects are the
ones producing a confident number from incomplete data. This file pins the
equivalents found in ESPRESSO / NCL / GoCCL.

What the audit CLEARED, worth recording so it is not re-investigated:
  * All 5,722 stored rows satisfy price_drop == old_total - new_total.
  * All satisfy net_saving == price_drop + obc_change - lost_pkg_value.
  * No row claims a saving above 60% of the booking — the shape every
    MSC scope bug took ($267.01, $1,929.61) does not appear here.
  * No row claims a saving against a zero booking total.
  * The 1,123 ESPRESSO rows with old_total=0 and status NO_SAVING all read
    "Booking restriction — price program change not allowed" — a real
    terminal state with genuinely no price to capture, not a false negative.
"""
import pytest

from core.calculator import calculate_goccl
from core.models import BookingStatus


def _goccl(offer_code, *, per_person=400.0, current_gross=2000.0, guests=2,
           booking="DEMO02"):
    """Real signature, real shape — mirrored from the captures that produced
    the five stored GoCCL candidates."""
    return calculate_goccl(
        booking_id=booking,
        price_category="BALCONY",
        current_stateroom_type="BALCONY",
        current_offer_code="XXX",
        current_price_gross=current_gross,
        available_offer_codes=[{
            "offer_code": offer_code,
            "offer_name": "SAVE & SAIL: PACK & GO",
            "price_per_person": per_person,
            "stateroom_type": "BALCONY",
        }],
        guests_count=guests,
        guests_count_verified=True,
    )


def test_a_candidate_with_no_offer_code_is_not_reported_as_a_saving():
    """CONFIRMED DEFECT, found 2026-09-03. Three of the five GoCCL
    candidates ever stored carry an EMPTY offer code — DEMO02 $880,
    DEMO03 $740, DEMO01 $1,560, which is $3,180 of the $4,100 GoCCL has
    ever claimed. The code is the entire actionable content of the
    finding: it is what the reprice popup hands to
    fn_goccl_selectOfferAndContinue(). Without it there is nothing to
    select and no way to verify the number, yet it still landed in a
    total someone could plan around.
    """
    result = _goccl("")
    assert result.status is not BookingStatus.OPTIMIZATION
    assert not result.net_saving
    assert "offer code was not captured" in (result.note or "")


@pytest.mark.parametrize("empty", ["", None, "   "])
def test_every_flavour_of_missing_code_is_refused(empty):
    result = _goccl(empty, booking="DEMO03")
    assert result.status is not BookingStatus.OPTIMIZATION


def test_a_candidate_with_a_real_code_still_reports():
    """Must not disable GoCCL: DEMO02/PUG and DEMO03/PI0 are the two
    stored candidates that DO have codes, and they stay reportable."""
    result = _goccl("PUG")
    assert result.status is BookingStatus.OPTIMIZATION
    assert result.net_saving == pytest.approx(1200.0, abs=0.02)
    assert "PUG" in (result.note or "")
    assert "UNCONFIRMED" in (result.note or "")


def test_a_reported_candidate_carries_the_code_for_the_reprice_popup():
    """The popup falls back to price_category when this is unset, which
    silently matches no offer-code button at all."""
    result = _goccl("PI0", booking="DEMO03")
    assert result.new_price_category == "PI0"


def test_goccl_candidates_stay_out_of_confirmed_savings():
    """GoCCL candidates are self-declared UNCONFIRMED and must never be
    counted as confirmed money — $4,100 of the all-time total once was."""
    from core.calculator import total_optimization_savings

    result = _goccl("PUG")
    assert total_optimization_savings([result]) == 0.0
