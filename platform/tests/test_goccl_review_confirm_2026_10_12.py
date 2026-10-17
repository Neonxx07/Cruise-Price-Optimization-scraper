"""GoCCL candidates confirmed on Carnival's /review screen, 2026-10-12.

Neon 2026-10-12: "lets build goccl carnival optimization script".
ROADMAP P2.2: "Confirmed live 2026-09-18 ... /review was never wired into
production."

Evidence:
  * data/goccl_safe_review/ - the live walk of 2026-09-18. DEMO11 and DEMO39
    reached /review on the safe path; DEMO42 stopped REVIEW_PATH_BLOCKED.
  * DEMO11's real review-changes invoice (summary only, no guest data) is
    fixtures/goccl/demo11_review_changes_summary_2026_09_18.json: gross
    1,552.00, POBC 0.00, against the booking's 1,574.00. The teaser estimate
    had said 1,542.00 - a real $22 saving overstated as $32.
  * data/goccl_watch/20260918T113403/005_win2_DEMO08.initialData.json - the
    booking page carries its own invoice in the same schema (1,365.81, POBC 50).

Decision rules pinned here: cash decides (hard rule 12, OBC is not cash),
OBC given up needs 3x the cash (OBC_LOSS_MIN_RATIO), and anything short of
a reconciled invoice stays an UNCONFIRMED candidate with its reason.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.calculator import (
    GOCCL_CANDIDATE_CONFIDENCE,
    GOCCL_CONFIRMED_CONFIDENCE,
    GOCCL_CONFIRMED_OBC_UNKNOWN_CONFIDENCE,
    apply_goccl_review,
)
from core.goccl_review import ReviewInvoice, parse_review_changes
from core.models import BookingResult, BookingStatus, CruiseLine
from scraper.goccl import GoCCLScraper

FIXTURES = Path(__file__).parent / "fixtures" / "goccl"
DEMO11_REVIEW = json.loads(
    (FIXTURES / "demo11_review_changes_summary_2026_09_18.json").read_text(encoding="utf-8"))
DEMO08_PAGE = Path("data/goccl_watch/20260918T113403/005_win2_DEMO08.initialData.json")


def _estimate(code="PHY", old=1574.00, new=1542.00) -> BookingResult:
    return BookingResult(
        cruise_line=CruiseLine.GOCCL, status=BookingStatus.OPTIMIZATION,
        booking_id="DEMO11", price_category="8C", new_price_category=code,
        old_total=old, new_total=new, price_drop=old - new, net_saving=old - new,
        confidence=GOCCL_CANDIDATE_CONFIDENCE,
        note=(f"candidate ${round(old - new)} — offer code '{code}' (SALE) — "
              f"UNCONFIRMED [TERMS: same fare tier]"),
    )


def _invoice(gross, obc=None, final="2027-01-27", nxt="2027-01-27",
             deposit=None) -> ReviewInvoice:
    """final/nxt default to DEMO11's real review dates (both 2027-01-27)."""
    items = {"CRUS": Decimal(str(gross))}
    if obc is not None:
        items["POBC"] = Decimal(str(obc))
    return ReviewInvoice(gross=Decimal(str(gross)),
                         obc=None if obc is None else Decimal(str(obc)), items=items,
                         final_due=final, next_due=nxt, deposit_on_review=deposit)


# -- the decision ----------------------------------------------------------

def test_demo11_real_invoice_confirms_22_not_the_estimated_32():
    result = apply_goccl_review(_estimate(), _invoice("1574.00", "0.00"),
                                parse_review_changes(DEMO11_REVIEW), "REVIEW_REACHED")
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.new_total == 1552.00 and result.price_drop == 22.00
    assert result.net_saving == 22.00
    assert result.confidence == GOCCL_CONFIRMED_CONFIDENCE
    assert "CONFIRMED on Carnival's review" in result.note
    assert "[TERMS: same fare tier]" in result.note         # fare terms carried


def test_more_onboard_credit_never_turns_a_cash_increase_into_a_saving():
    """Hard rule 12: OBC is not cash."""
    result = apply_goccl_review(_estimate(), _invoice("1574.00", "0"),
                                _invoice("1600.00", "200"), "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING
    assert result.net_saving == 0.0
    assert "OBC is not cash" in result.note


def test_onboard_credit_given_up_needs_three_times_the_cash():
    small = apply_goccl_review(_estimate(), _invoice("1574.00", "50"),
                               _invoice("1552.00", "0"), "REVIEW_REACHED")
    assert small.status == BookingStatus.NO_SAVING and "3x" in small.note
    large = apply_goccl_review(_estimate(), _invoice("1774.00", "50"),
                               _invoice("1574.00", "0"), "REVIEW_REACHED")
    assert large.status == BookingStatus.OPTIMIZATION
    assert large.price_drop == 200.00 and large.obc_change == -50.00
    assert large.net_saving == 150.00                        # project formula


def test_a_missing_obc_line_is_confirmed_price_but_flagged():
    result = apply_goccl_review(_estimate(), _invoice("1574.00", "0"),
                                _invoice("1552.00"), "REVIEW_REACHED")
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.confidence == GOCCL_CONFIRMED_OBC_UNKNOWN_CONFIDENCE
    assert "OBC_UNVERIFIED" in result.note


@pytest.mark.parametrize("path, words", [
    ("REVIEW_PATH_BLOCKED", "Keep Same Stateroom"),
    ("STATEROOM_CHANGED", "different stateroom"),
])
def test_a_cabin_that_cannot_be_kept_is_a_no_not_a_candidate(path, words):
    """Changed 2026-10-12 (REVIEW_PATH_BLOCKED was an unconfirmed candidate).
    Live: DEMO26's PSV Super Saver never enabled Keep Same Stateroom and was
    still shown as a $308 saving. Neon: the cabin must be kept."""
    result = apply_goccl_review(_estimate(), _invoice("1574.00", "0"), None, path)
    assert result.status == BookingStatus.NO_SAVING
    assert result.net_saving == 0.0 and result.new_total == 1574.00
    assert words in result.note and "cabin must be kept" in result.note


@pytest.mark.parametrize("path, words", [
    ("OWN_CATEGORY_NOT_OFFERED", "own category"),
    ("CANDIDATE_NOT_ON_SCREEN", "not on the rate screen"),
    ("REVIEW_REFUSED", "refused"),
])
def test_a_walk_that_stops_leaves_an_unconfirmed_candidate_with_its_reason(path, words):
    est = _estimate()
    result = apply_goccl_review(est, _invoice("1574.00", "0"), None, path)
    assert result.status == est.status and result.confidence == GOCCL_CANDIDATE_CONFIDENCE
    assert result.new_total == est.new_total                 # no figure invented
    assert "not confirmed" in result.note and words in result.note


def test_an_invoice_that_does_not_add_up_is_not_trusted():
    bad = parse_review_changes(DEMO11_REVIEW)
    bad.gross = bad.gross + Decimal("1")
    result = apply_goccl_review(_estimate(), _invoice("1574.00", "0"), bad, "REVIEW_REACHED")
    assert result.confidence == GOCCL_CANDIDATE_CONFIDENCE
    assert "does not add up" in result.note


# -- the payment schedule may not move (Neon 2026-10-12) -------------------
#
# "if a deposit shows up or a Due Date ... changes it is a big x a big no".
# His three good repricings that day kept the final payment date:
#   DEMO15 $1,358 -> $1,334 (24),  final Jun 21 2027 before and after
#   DEMO40 56, DEMO43 56.22,       final Jan 27 2027 before and after

def test_cd1l69_saves_24_with_the_final_payment_date_unchanged():
    result = apply_goccl_review(
        _estimate(old=1358.00, new=1334.00),
        _invoice("1358.00", "0", final="2027-06-21", nxt="2027-06-21"),
        _invoice("1334.00", "0", final="2027-06-21", nxt="2027-06-21", deposit=False),
        "REVIEW_REACHED")
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.price_drop == 24.00


def test_a_moved_final_payment_date_is_a_hard_no():
    result = apply_goccl_review(
        _estimate(), _invoice("1574.00", "0", final="2027-01-27"),
        _invoice("1400.00", "0", final="2027-01-12", nxt="2027-01-12"), "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING and result.net_saving == 0.0
    assert "final payment would fall due EARLIER" in result.note
    assert "174.00 less" in result.note                       # visible, not hidden


def test_a_deposit_on_the_review_is_a_hard_no():
    """DEMO18's refused PB4 review: Deposit $300 due Aug 07 2026."""
    result = apply_goccl_review(
        _estimate(), _invoice("1574.00", "0"),
        _invoice("1500.00", "0", deposit=True), "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING
    assert "DEPOSIT would be added" in result.note


def test_a_new_earlier_payment_date_is_a_hard_no():
    result = apply_goccl_review(
        _estimate(), _invoice("1574.00", "0"),
        _invoice("1500.00", "0", nxt="2026-08-07", deposit=None), "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING
    assert "2026-08-07" in result.note


def test_an_unreadable_schedule_is_never_assumed_unchanged():
    result = apply_goccl_review(
        _estimate(), _invoice("1574.00", "0"),
        _invoice("1500.00", "0", final=None, nxt=None), "REVIEW_REACHED")
    assert result.confidence == GOCCL_CANDIDATE_CONFIDENCE
    assert "payment schedule could not be compared" in result.note


def test_the_real_review_and_booking_pages_carry_the_dates():
    review = parse_review_changes({**DEMO11_REVIEW, "paymentsSchedule": {
        "finalPaymentDueDate": {"iso8601": "2027-01-27"},
        "closestPaymentDueBy": {"iso8601": "2027-01-27"}}})
    assert (review.final_due, review.next_due) == ("2027-01-27", "2027-01-27")
    if DEMO08_PAGE.exists():
        s = GoCCLScraper()
        s.last_initial_data = json.loads(DEMO08_PAGE.read_text(encoding="utf-8"))
        original = s.original_invoice()
        assert (original.final_due, original.next_due) == ("2027-03-21", "2027-03-21")


# -- Neon's two DEMO18 sessions, 2026-10-12 (recordings + screenshots) ----
#
# OCEAN VIEW teasers on both days: GO3 734, OCS 574, PB4 604, PNS 924,
# PSV SUPER SAVER 554. Two guests.

_DEMO50_OFFERS = [
    {"offer_code": c, "offer_name": n, "stateroom_type": "OCEAN VIEW", "price_per_person": p}
    for c, n, p in (("GO3", "BUNDLE & SAVE: UPGRADE YOUR FUN", 734),
                    ("OCS", "LOOK FORWARD TO FUN & EARLY SAVER SAVINGS", 574),
                    ("PB4", "OCTOBER SAVINGS SALE", 604),
                    ("PNS", "FUN SELECT", 924),
                    ("PSV", "SUPER SAVER", 554))
]


def _demo50(current_code, current_name, gross):
    from core.calculator import calculate_goccl
    return calculate_goccl(
        booking_id="DEMO18", price_category="6B", current_stateroom_type="OCEAN VIEW",
        current_offer_code=current_code, current_price_gross=gross,
        available_offer_codes=_DEMO50_OFFERS, guests_count=2,
        guests_count_verified=True, current_offer_name=current_name)


def test_demo50_first_session_finds_neons_46_on_ocs_not_the_super_saver():
    """On PEH (Early Saver) at $1,194 Neon tried PSV, backed out, and
    confirmed OCS at $1,148 - $46."""
    r = _demo50("PEH", "PLAN ON FUN EARLY SAVER BONUS SALE", 1194.00)
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.new_price_category == "OCS"
    assert r.price_drop == 46.00


def test_demo50_second_session_a_super_saver_only_saving_is_no_saving():
    """Already on OCS at $1,148, the only cheaper fare was PSV SUPER SAVER
    ($1,108). Neon did not take it: it loses price protection and the cabin
    choice. "the main focus in carnival is not lose anything"."""
    r = _demo50("OCS", "LOOK FORWARD TO FUN & EARLY SAVER SAVINGS", 1148.00)
    assert r.status == BookingStatus.NO_SAVING
    assert "PSV" in r.note and "nothing may be lost" in r.note
    assert r.new_total == 1148.00                       # no figure claimed


def test_demo58_the_same_code_at_todays_price_is_the_candidate():
    """Live 2026-10-12 (data/goccl_live_20261012T132759): DEMO40 on OCS
    'FUN ON THE WAY EARLY SAVER SALE' at $1,556; today's BALCONY tiles
    PSV 655, OCS 665 (renamed 'LOOK FORWARD TO FUN & EARLY SAVER SAVINGS'),
    PB4 695, GO3 825, PNS 1015, 2 guests. The same-code filter hid OCS and
    the scan said the Super Saver was the only cheaper fare."""
    from core.calculator import calculate_goccl
    offers = [{"offer_code": c, "offer_name": n, "stateroom_type": "BALCONY",
               "price_per_person": p}
              for c, n, p in (("PSV", "SUPER SAVER", 655),
                              ("OCS", "LOOK FORWARD TO FUN & EARLY SAVER SAVINGS", 665),
                              ("PB4", "OCTOBER SAVINGS SALE", 695),
                              ("GO3", "BUNDLE & SAVE: UPGRADE YOUR FUN", 825),
                              ("PNS", "FUN SELECT", 1015))]
    r = calculate_goccl(
        booking_id="DEMO40", price_category="8A", current_stateroom_type="BALCONY",
        current_offer_code="OCS", current_price_gross=1556.00,
        available_offer_codes=offers, guests_count=2, guests_count_verified=True,
        current_offer_name="FUN ON THE WAY EARLY SAVER SALE")
    assert r.status == BookingStatus.OPTIMIZATION       # a candidate for /review
    assert r.new_price_category == "OCS"


def test_the_bookings_own_invoice_is_read_from_its_page_data():
    if not DEMO08_PAGE.exists():
        pytest.skip("captured DEMO08 page data not on this machine")
    s = GoCCLScraper()
    s.last_initial_data = json.loads(DEMO08_PAGE.read_text(encoding="utf-8"))
    original = s.original_invoice()
    assert original.gross == Decimal("1365.81")
    assert original.obc == Decimal("50")
    assert original.reconciles() == (True, "")
