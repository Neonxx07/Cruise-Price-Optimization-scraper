"""The payment schedule rule - HARD RULE, Neon 2026-10-15.

  "we can never add deposit or payment dates changes unless regarding the
   dates only situation we can save if the final payment is further not
   nearer from the original final payment date"

core/payment_schedule_rule.py, used by GoCCL (apply_goccl_review) and
Princess (calculator_princess.decide).

Evidence - DEMO16 (GoCCL), GUI 2026-10-15: OPTIMIZATION "$868.00 saved",
$1,396.00 -> $528.00, 4C -> FSA (CARNIVAL PLAYERS CLUB FUNMATCH). Neon's
review screen for FSA:
    Cruise Charges 369.50 (Cruise Rate 200.00 + RCFE 169.50), Government
    Taxes & Fees 158.50, TARGETED FUNPLAY AMOUNT 50.00, Gross Amount 528.00,
    Payments Applied 100.00, Payment Schedule: Deposit $200.00 due Jul28,
    2026; Final Payment $428.00 due Jul12, 2027; Perks: OBC $200.00.
The booking itself (data/goccl_forensic/20260918T143128/DEMO16/
initialData.json): GRSS 1,396.00, POBC 50, finalPaymentDueDate 2027-07-12,
closestPaymentDueBy 2027-07-12 - no deposit pending.
FSA ADDS a deposit, so it is NO_SAVING. It showed as a candidate because
the invoice-sum check ran first and failed on TFPA (578 vs 528), and that
"unconfirmed" return skipped the deposit check entirely.
"""
from datetime import date
from decimal import Decimal

import pytest

from core.calculator import GOCCL_CANDIDATE_CONFIDENCE, apply_goccl_review
from core.calculator_princess import PrincessBooking, PrincessQuote, decide
from core.goccl_review import ReviewInvoice, parse_review_changes
from core.models import BookingResult, BookingStatus, CruiseLine
from core.payment_schedule_rule import check_payment_schedule
from core.princess_packages import STANDARD


# -- the rule itself ---------------------------------------------------------

def test_an_unchanged_schedule_is_allowed():
    v = check_payment_schedule("2027-07-12", "2027-07-12", "2027-07-12", "2027-07-12")
    assert v.allowed is True and not v.final_moved_later


def test_the_only_allowed_change_is_a_later_final_payment():
    v = check_payment_schedule("2027-07-12", "2027-08-01", "2027-07-12", "2027-08-01")
    assert v.allowed is True and v.final_moved_later
    assert "LATER" in v.reason


def test_an_earlier_final_payment_is_refused():
    v = check_payment_schedule("2027-07-12", "2027-06-01", "2027-07-12", "2027-06-01")
    assert v.allowed is False and "EARLIER" in v.reason


@pytest.mark.parametrize("kwargs", [
    # a payment now falls due before the final payment (DEMO16 FSA)
    dict(orig_next="2027-07-12", new_next="2026-07-28"),
    # the deposit line was seen on the new schedule
    dict(orig_next="2027-07-12", new_next="2027-07-12", deposit_on_new=True),
])
def test_adding_a_deposit_is_refused(kwargs):
    v = check_payment_schedule("2027-07-12", "2027-07-12", **kwargs)
    assert v.allowed is False and "DEPOSIT would be added" in v.reason


def test_a_later_final_payment_does_not_excuse_an_added_deposit():
    v = check_payment_schedule("2027-07-12", "2027-08-01", "2027-07-12", "2026-11-01")
    assert v.allowed is False and "DEPOSIT" in v.reason


@pytest.mark.parametrize("new_next", ["2026-11-01", "2026-12-15"])
def test_a_pending_deposit_may_not_move_either_way(new_next):
    """Only the FINAL date may move, and only later."""
    v = check_payment_schedule("2027-07-12", "2027-07-12", "2026-12-01", new_next)
    assert v.allowed is False


def test_an_unread_final_date_is_unknown_never_assumed_unchanged():
    assert check_payment_schedule(None, "2027-07-12").allowed is None
    assert check_payment_schedule("2027-07-12", None).allowed is None


def test_dates_work_as_date_objects_too():
    v = check_payment_schedule(date(2027, 7, 12), date(2027, 6, 1))
    assert v.allowed is False


# -- GoCCL: DEMO16 -----------------------------------------------------------

def _item(code, name, amount, indented=False):
    return {"code": code, "name": name, "isIndented": indented,
            "price": {"amount": amount, "currencyCode": "USD"}}


def _demo49_fsa_review(next_due="2026-07-28", final_due="2027-07-12"):
    """Neon's FSA review for DEMO16, figures only (no guest data)."""
    return parse_review_changes({
        "agentInvoice": {"summary": {
            "lineItems": [
                _item("CNRF", "Cruise Charges", 369.50),
                _item("CRUS", "Cruise Rate", 200.00, indented=True),
                _item("RCFE", "Required Cruise Fees & Expenses", 169.50, indented=True),
                _item("GTFE", "Government Taxes & Fees", 158.50),
                _item("TFPA", "TARGETED FUNPLAY AMOUNT", 50.00),
            ],
            "commissions": [_item("COMM", "Commission", 60.00)],
            "perks": [_item("POBC", "Total Onboard Credit", 200.00),
                      _item("NOBC", "All Departments Onboard Credit", 200.00)],
            "summaryTotal": _item("GRSS", "Gross Amount", 528.00),
        }},
        "paymentsSchedule": {
            "finalPaymentDueDate": {"iso8601": final_due},
            "closestPaymentDueBy": {"iso8601": next_due},
        },
    })


def _demo49_booking():
    return ReviewInvoice(gross=Decimal("1396.00"), obc=Decimal("50"),
                         items={"CRUS": Decimal("1396.00"), "POBC": Decimal("50")},
                         final_due="2027-07-12", next_due="2027-07-12")


def _demo49_estimate():
    return BookingResult(
        cruise_line=CruiseLine.GOCCL, status=BookingStatus.OPTIMIZATION,
        booking_id="DEMO16", price_category="4C", new_price_category="FSA",
        old_total=1396.00, new_total=528.00, price_drop=868.00, net_saving=868.00,
        confidence=GOCCL_CANDIDATE_CONFIDENCE,
        note="candidate $868 — offer code 'FSA' (CARNIVAL PLAYERS CLUB FUNMATCH UP TO BL) — UNCONFIRMED")


def test_tfpa_funplay_is_a_credit_outside_the_gross():
    review = _demo49_fsa_review()
    assert review.reconciles() == (True, "")      # 369.50 + 158.50 = 528.00
    assert review.funplay == Decimal("50.00")
    assert "TFPA" not in review.line_codes


def test_demo49_fsa_adds_a_deposit_so_it_is_no_saving():
    result = apply_goccl_review(_demo49_estimate(), _demo49_booking(),
                                _demo49_fsa_review(), "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING
    assert result.net_saving == 0.0 and result.price_drop == 0.0
    assert "DEPOSIT would be added" in result.note and "2026-07-28" in result.note
    assert "868.00 less" in result.note                 # visible, never hidden


def test_the_deposit_refusal_comes_before_the_invoice_sum_check():
    """The exact DEMO16 failure: an invoice that does not add up must not
    shield a deposit from the rule."""
    review = _demo49_fsa_review()
    review.gross = review.gross + Decimal("1")          # break the sum
    assert review.reconciles()[0] is False
    result = apply_goccl_review(_demo49_estimate(), _demo49_booking(), review,
                                "REVIEW_REACHED")
    assert result.status == BookingStatus.NO_SAVING
    assert "DEPOSIT" in result.note


def test_a_later_final_payment_still_confirms_and_says_so():
    review = _demo49_fsa_review(next_due="2027-08-01", final_due="2027-08-01")
    result = apply_goccl_review(_demo49_estimate(), _demo49_booking(), review,
                                "REVIEW_REACHED")
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.price_drop == 868.00
    assert "final payment moves LATER" in result.note
    assert "casino FunPlay credit +50.00 (not cash)" in result.note
    assert result.net_saving == 868.00 + 150.00        # OBC change only; FunPlay never


def test_giving_up_funplay_needs_three_times_the_cash():
    with_fp = _demo49_booking()
    with_fp.items["TFPA"] = Decimal("100")
    review = parse_review_changes({
        "agentInvoice": {"summary": {
            "lineItems": [_item("CRUS", "Cruise Rate", 1050.00)],
            "perks": [_item("POBC", "Total Onboard Credit", 50.00)],
            "summaryTotal": _item("GRSS", "Gross Amount", 1050.00)}},
        "paymentsSchedule": {"finalPaymentDueDate": {"iso8601": "2027-07-12"},
                             "closestPaymentDueBy": {"iso8601": "2027-07-12"}}})
    small = apply_goccl_review(_demo49_estimate(), with_fp, review, "REVIEW_REACHED")
    assert small.status == BookingStatus.OPTIMIZATION   # 346 cash >= 3 x 100
    review.items["CRUS"] = review.gross = Decimal("1150.00")
    big_loss = apply_goccl_review(_demo49_estimate(), with_fp, review, "REVIEW_REACHED")
    assert big_loss.status == BookingStatus.NO_SAVING   # 246 cash < 3 x 100
    assert "FunPlay" in big_loss.note and "3x" in big_loss.note


# -- Princess: the same rule, as a WATCH note (Neon 2026-10-15: "it is a
#    thing we keep an eye on but it is not as madnatory as carnival") --------

TODAY = date(2026, 10, 15)


def _booking(**kw):
    base = dict(booking_id="DEMO33", gross=7110.0, category="DF4", currency="USD",
                tier=STANDARD, gross_payment_due=6910.0, final_due=date(2026, 11, 28))
    base.update(kw)
    return PrincessBooking(**base)


def _quote(**kw):
    base = dict(promo_code="KKS", promo_description="CYBER SUMMER LTO - STANDARD",
                category="DF4", currency="USD", tier=STANDARD,
                fare_pp=[3405.0, 3405.0], gross_total=6810.0, final_due=date(2026, 11, 28))
    base.update(kw)
    return PrincessQuote(**base)


@pytest.mark.parametrize("quote_kw, words", [
    (dict(deposit_due=date(2026, 10, 30)), "DEPOSIT"),
    (dict(final_due=date(2026, 11, 1)), "EARLIER"),
])
def test_princess_a_payment_change_is_flagged_never_blocking(quote_kw, words):
    v = decide(_booking(), _quote(**quote_kw), today=TODAY)
    assert v.status == "OPTIMIZATION" and v.saving == 300.0
    assert v.note.startswith("WATCH PAYMENT DATES") and words in v.note


def test_princess_a_later_final_payment_is_named():
    v = decide(_booking(), _quote(final_due=date(2026, 12, 15)), today=TODAY)
    assert v.status == "OPTIMIZATION" and "LATER" in v.note


def test_princess_an_unread_quote_schedule_is_a_reminder_not_a_downgrade():
    """Live POLAR Pricing Detail shows no payment dates (DEMO12 2026-10-15)."""
    v = decide(_booking(), _quote(final_due=None), today=TODAY)
    assert v.status == "OPTIMIZATION" and v.saving == 300.0
    assert "check the payment dates" in v.note
    assert "unconfirmed" not in v.note.lower()          # would tint the GUI row


def test_princess_a_paid_deposit_in_the_past_is_not_a_pending_one():
    v = decide(_booking(deposit_due=date(2026, 9, 1)),
               _quote(deposit_due=date(2026, 9, 1)), today=TODAY)
    assert v.status == "OPTIMIZATION" and "WATCH" not in v.note
