"""MSC: paid in full stops everything; discounts obey the reprice rule.

Neon 2026-10-15, from the GUI:
  3001068  "on msc this is paid in full fix that" - Booking Value $544.48,
            Due Amount $5.48. msc_results: is_paid_in_full=1 AND
            has_any_opportunity=1 (VOYAGERS_SELECTION MSVG15W, never priced).
  3001066  "this booking is paid in full also" - Due Amount $6.18; same
            row shape (MSVG5W). 5 such rows in msc_results, 4 bookings:
            3001064 (x2), 3001063, 3001068, 3001066.
  3001067  Due $25, "SPECIAL OFFER 15% yes it can be applied but ... that
            is useless" - VOYAGERS_SELECTION OPPORTUNITY with PRICE_MATCH
            INSUFFICIENT_DATA (occupancy not verified): a discount nobody
            priced against today's rate.
  3001065  "this is accurate it mentioned that a discount can be applied
            and this customer doesnt have vouyagers" - DISCOUNT_ADD club 5%,
            no discount held, today's quote unverified. Must SURVIVE.

Root cause of 3001067: msc_discount_beats_current() (RULE MSC-D1/D2,
2026-09-22, booking 3000005) had no production call site.
"""
from core.calculator_msc import evaluate_msc_booking
from core.models import MscCheckStatus, MscOpportunityType

SELECTION_15 = [{"disc_cd": "MSVG15W", "program_name": "Voyagers Selection WELCOME",
                 "label": "SPECIAL OFFER 15%", "rate_pct": 15.0}]
SELECTION_10 = [{"disc_cd": "MSVG10W", "program_name": "Voyagers Selection WELCOME",
                 "label": "SPECIAL OFFER 10%", "rate_pct": 10.0}]
CLUB_HELD = [{"kind": "club", "label": "MSCCLUB5", "rate_pct": 5.0}]


def _check(result, kind):
    return next(c for c in result.checks if c.type == kind)


def test_3001068_paid_in_full_has_no_opportunity_at_all():
    r = evaluate_msc_booking(
        booking_id="3001068", category="INTERIOR", is_paid_in_full=True, due_amount=5.48,
        current_total_price=544.48, current_discounts=CLUB_HELD,
        today_discount_catalog=SELECTION_15, has_voyagers=True, senior_count=2)
    assert r.is_paid_in_full and not r.has_any_opportunity
    assert r.checks == [] and "PAID IN FULL" in r.note and "5.48" in r.note


def test_3001067_an_unpriced_special_offer_is_not_an_opportunity():
    r = evaluate_msc_booking(
        booking_id="3001067", category="BR2", due_amount=25.0,
        current_discounts=CLUB_HELD, today_discount_catalog=SELECTION_15,
        has_voyagers=True, senior_count=1, occupancy_verified=False,
        occupancy_note="occupancy was REDUCED below MSC's own pre-fill")
    sel = _check(r, MscOpportunityType.VOYAGERS_SELECTION)
    assert sel.status == MscCheckStatus.INSUFFICIENT_DATA
    assert "NOT PRICED" in sel.note and "MSVG15W" in sel.note      # the lead is still named
    assert not r.has_any_opportunity


def test_3001065_the_club_5pc_on_an_undiscounted_booking_stays_a_lead():
    r = evaluate_msc_booking(
        booking_id="3001065", category="X", current_discounts=[],
        today_discount_options=[], club_discount_offered=True, has_voyagers=False,
        occupancy_verified=False, occupancy_note="occupancy not verified")
    add = _check(r, MscOpportunityType.DISCOUNT_ADD)
    assert add.status == MscCheckStatus.OPPORTUNITY and r.has_any_opportunity
    assert "not priced" in add.note


def test_3000005_a_selection_that_reprices_above_the_fare_is_refused():
    """current 1,756.42; today 2,109.54; 10% gives 1,898.59 (+142.17)."""
    r = evaluate_msc_booking(
        booking_id="3000005", category="BL3", current_total_price=1756.42,
        today_base_price=2109.54, today_price_tab_confirmed=True,
        current_discounts=[], today_discount_options=[], club_discount_offered=False,
        today_discount_catalog=SELECTION_10, has_voyagers=True)
    sel = _check(r, MscOpportunityType.VOYAGERS_SELECTION)
    assert sel.status == MscCheckStatus.NO_OPPORTUNITY
    assert "NOT WORTH IT" in sel.note and "1,898.59" in sel.note


def test_a_known_worse_club_add_is_refused_even_though_unpriced_ones_survive():
    r = evaluate_msc_booking(
        booking_id="3000005", category="BL3", current_total_price=1756.42,
        today_base_price=2109.54, today_price_tab_confirmed=True,
        current_discounts=[], today_discount_options=[], club_discount_offered=True)
    add = _check(r, MscOpportunityType.DISCOUNT_ADD)
    assert add.status == MscCheckStatus.NO_OPPORTUNITY       # 2,004.06 > 1,756.42


def test_a_selection_that_does_beat_the_fare_is_an_opportunity_with_figures():
    r = evaluate_msc_booking(
        booking_id="X", category="BL3", current_total_price=2000.00,
        today_base_price=2100.00, today_price_tab_confirmed=True,
        current_discounts=[], today_discount_options=[], club_discount_offered=False,
        today_discount_catalog=SELECTION_10, has_voyagers=True)
    sel = _check(r, MscOpportunityType.VOYAGERS_SELECTION)
    assert sel.status == MscCheckStatus.OPPORTUNITY
    assert "1,890.00" in sel.note                              # 2,100 less 10%


def test_the_members_club_5pc_stacks_multiplicatively_on_the_reprice():
    """9.75%, not 10% (MSC's own VOYAGERS EXCLUSIVES line): 2,100 x 0.95 x
    0.95 = 1,895.25 - beats 1,900 only because the club is re-applied."""
    r = evaluate_msc_booking(
        booking_id="X", category="BL3", current_total_price=1900.00,
        today_base_price=2100.00, today_price_tab_confirmed=True,
        current_discounts=CLUB_HELD, today_discount_catalog=[
            {"disc_cd": "MSVG5W", "program_name": "Voyagers Selection WELCOME", "rate_pct": 5.0}],
        has_voyagers=True, customer_has_club_membership=True)
    sel = _check(r, MscOpportunityType.VOYAGERS_SELECTION)
    assert sel.status == MscCheckStatus.OPPORTUNITY and "1,895.25" in sel.note
