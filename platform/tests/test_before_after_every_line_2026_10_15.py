"""Before AND after codes are saved for every cruise line, 2026-10-15.

Neon: "MAKE SURE THAT FOR ALL THE CRUISE LINES WE ARE SAVING BEFORE AND
AFTER LIKE ESSPRESSO AND NCL LIKE DIXIN LATREW AL THESE CODES SINCE IT IS
REALLY REALLY IMPORTNANT".

Measured in cruise_intel.db the same day (old_promos / new_promos filled):
  ESPRESSO  0 of 15,346 rows   - both fare lists were in hand, only the
                                 difference (lost/gained) was stored
  NCL       977 of 2,883       - only priced bookings; paid/cancelled none
  GOCCL     0 of 158           - nothing at all
  MSC       no column          - msc_results had nowhere to put them
  PRINCESS  new line           - not mapped
"""
from core.calculator import calculate_espresso, calculate_goccl
from core.calculator_msc import evaluate_msc_booking
from scraper.princess import PrincessCheck, check_to_result


def test_espresso_keeps_both_full_fare_lists():
    raw = {"result": {
        "oldInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 2000.0}]},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 1900.0}]},
        "oldFares": [{"name": "BOGO60 NRD"}, {"name": "ALL INC 2PK"}],
        "newFares": [{"name": "BOGO60 NRD"}, {"name": "ALL INC 2PK NRD"}],
    }}
    r = calculate_espresso(raw, "3000067", None)
    assert r.old_promos == "BOGO60 NRD, ALL INC 2PK"
    assert r.new_promos == "BOGO60 NRD, ALL INC 2PK NRD"


def test_goccl_keeps_the_offer_before_and_the_candidate_after():
    r = calculate_goccl(
        booking_id="DEMO15", price_category="8D", current_stateroom_type="BALCONY",
        current_offer_code="PQU", current_price_gross=1358.0,
        available_offer_codes=[
            {"offer_code": "PQU", "offer_name": "EARLY SAVER", "stateroom_type": "BALCONY",
             "price_per_person": 667.0},
            {"offer_code": "OCS", "offer_name": "FUN SELECT", "stateroom_type": "BALCONY",
             "price_per_person": 600.0},
        ],
        guests_count=2, guests_count_verified=True, current_offer_name="EARLY SAVER")
    assert r.old_promos == "PQU EARLY SAVER"
    if r.new_price_category:
        assert r.new_promos.startswith(r.new_price_category)


def test_msc_keeps_discounts_held_and_offered_today():
    r = evaluate_msc_booking(
        booking_id="3001067", category="BR2",
        current_discounts=[{"kind": "club", "label": "MSCCLUB5", "rate_pct": 5.0}],
        today_discount_catalog=[{"disc_cd": "MSVG15W", "program_name": "Voyagers Selection WELCOME",
                                 "rate_pct": 15.0}],
        today_discount_options=["SENIOR DISCOUNT"], has_voyagers=True)
    assert r.discounts_before == "MSCCLUB5 5%"
    assert r.discounts_today == "MSVG15W 15%, SENIOR DISCOUNT"


def test_msc_unread_discounts_stay_none_and_read_empty_stays_empty():
    r = evaluate_msc_booking("X", "BP")
    assert r.discounts_before is None and r.discounts_today is None
    r = evaluate_msc_booking("X", "BP", current_discounts=[], today_discount_options=[])
    assert r.discounts_before == "" and r.discounts_today == ""


def test_msc_paid_in_full_still_keeps_its_discounts():
    r = evaluate_msc_booking("3001068", "INTERIOR", is_paid_in_full=True,
                             current_discounts=[{"kind": "club", "label": "MSCCLUB5", "rate_pct": 5}])
    assert r.checks == [] and r.discounts_before == "MSCCLUB5 5%"


def test_princess_keeps_package_before_and_quote_promos_after():
    c = PrincessCheck(ref="DEMO24", outcome="PRICED", tier="STANDARD", offer_codes=[],
                      landing={"category": "BC", "currency": "USD",
                               "status": {"cancelled": False, "waitlisted": False}},
                      verdict={"status": "NO_SAVING", "saving": 0.0, "note": "no saving",
                               "booking_fare_plus_packages": 7514.0,
                               "quote_fare_plus_packages": 8474.0,
                               "quote_codes": ["KLS", "CN6", "NTB", "KLS"],
                               "quote_descriptions": ["FALL SAVINGS LTO - STANDARD"]})
    r = check_to_result(c)
    assert r.old_promos == "[STANDARD package]"
    assert r.new_promos == "KLS, CN6, NTB (FALL SAVINGS LTO - STANDARD)"


def test_princess_offer_codes_are_part_of_before():
    c = PrincessCheck(ref="DEMO12", outcome="NOT_PRICED", why="past final payment (14OCT26)",
                      tier="PLUS", offer_codes=["PLUS2"],
                      landing={"category": "B4", "currency": "USD",
                               "status": {"cancelled": False, "waitlisted": False}})
    assert check_to_result(c).old_promos == "PLUS2 [PLUS package]"
