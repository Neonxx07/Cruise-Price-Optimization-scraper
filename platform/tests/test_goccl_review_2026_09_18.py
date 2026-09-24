"""The review invoice - the first CONFIRMED GoCCL price.

Reached on the SAFE path only (Neon, section 13): "Keep Same Stateroom"
when enabled, never selecting a different cabin. Booking DEMO11 on
2026-09-18 was the first to get there, via

    GET /app/bookingengine/api/v1.0/manage/zm58d2/review-changes

Every figure below is that booking's real response.
"""
from decimal import Decimal

import pytest

from core.goccl_review import (
    COMMISSION,
    CRUISE_CHARGES,
    CRUISE_RATE,
    GROSS,
    ONBOARD_CREDIT,
    ReviewInvoice,
    confirmed_saving,
    money,
    parse_review_changes,
)


def _price(amount, code="USD"):
    return {"amount": amount, "currencyCode": code,
            "amountFormatted": f"{amount:,.2f}"}


def _item(code, name, amount, **extra):
    return {"code": code, "name": name, "price": _price(amount), **extra}


#: DEMO11's real review-changes payload, trimmed to what is parsed.
REAL = {
    "agentInvoice": {
        "summary": {
            "lineItems": [
                _item("CNRF", "Cruise Charges", 1224.46, isTotal=True),
                _item("CRUS", "Cruise Rate", 720.00, isIndented=True),
                _item("PTCH", "Non-Comm Cruise Amount", 238.00, isIndented=True),
                _item("RCFE", "Required Cruise Fees & Expenses", 266.46, isIndented=True),
                _item("GTFE", "Government Taxes & Fees", 327.54, isTotal=True),
                _item("INSU", "Carnival Vacation Protection", 0.00, isTotal=True),
                _item("PKGS", "Pre/Post Packages", 0.00),
                _item("TADD", "Transportation Add-On", 0.00, isTotal=True),
            ],
            "commissions": [_item("COMM", "Commission", 108.00, isTotal=True)],
            "perks": [_item("POBC", "Total Onboard Credit", 0.00, isTotal=True)],
            "summaryTotal": _item("GRSS", "Gross Amount", 1552.00, isTotal=True),
        }
    },
    "paymentsSchedule": {
        "netBalanceDue": _price(1219.00),
        "balanceDue": _price(1327.00),
        "hasDebt": True,
    },
}


# ── the real payload ─────────────────────────────────────────────────────


def test_the_real_review_response_parses_completely():
    inv = parse_review_changes(REAL)
    assert inv.gross == Decimal("1552.00")
    assert inv.commission == Decimal("108.00")
    assert inv.obc == Decimal("0.00")
    assert inv.cruise_rate == Decimal("720.00")
    assert inv.currency == "USD"
    assert inv.warnings == []


def test_the_real_invoice_reconciles_to_the_cent():
    """The NON-indented items sum to GRSS exactly:
    CNRF 1,224.46 + GTFE 327.54 + INSU/PKGS/TADD 0 = 1,552.00.
    (CRUS/PTCH/RCFE are isIndented - they compose CNRF.)"""
    ok, why = parse_review_changes(REAL).reconciles()
    assert ok, why


def test_cruise_charges_is_a_SUBTOTAL_not_an_addend():
    """CNRF carries isTotal=true and equals CRUS+PTCH+RCFE. Including it in
    the sum double-counts 1,224.46 - the first naive reconciliation did
    exactly that and reported a false mismatch."""
    inv = parse_review_changes(REAL)
    assert inv.cruise_charges_subtotal == Decimal("1224.46")
    assert (inv.items[CRUISE_RATE] + inv.items["PTCH"] + inv.items["RCFE"]
            == inv.items[CRUISE_CHARGES])
    naive = sum(inv.items[c] for c in
                ("CNRF", "CRUS", "PTCH", "RCFE", "GTFE", "INSU", "PKGS", "TADD"))
    assert naive != inv.gross          # the trap: double-counts CNRF
    assert naive - inv.items[CRUISE_CHARGES] == inv.gross


def test_a_broken_subtotal_is_caught():
    bad = {"agentInvoice": {"summary": {
        "lineItems": [
            _item("CNRF", "Cruise Charges", 999.00, isTotal=True),
            _item("CRUS", "Cruise Rate", 720.00, isIndented=True),
            _item("PTCH", "Non-Comm Cruise Amount", 238.00, isIndented=True),
            _item("RCFE", "Required Cruise Fees & Expenses", 266.46, isIndented=True),
            _item("GTFE", "Government Taxes & Fees", 327.54),
        ],
        "summaryTotal": _item("GRSS", "Gross Amount", 1326.54),
    }}}
    ok, why = parse_review_changes(bad).reconciles()
    assert ok is False
    assert "CNRF" in why


def test_a_total_that_does_not_match_its_parts_is_caught():
    """A misparse must surface here, never inside a saving."""
    bad = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 100.00)],
        "summaryTotal": _item("GRSS", "Gross Amount", 999.00),
    }}}
    ok, why = parse_review_changes(bad).reconciles()
    assert ok is False
    assert "GRSS reads" in why


# ── THE REASON THIS EXISTS: the estimate was wrong ───────────────────────


def test_the_confirmed_price_differs_from_the_per_person_estimate():
    """DEMO11, measured. The scan estimated the new gross by multiplying the
    per-person quote by occupancy:

        514.00 x 3 guests = 1,542.00   ->  claimed saving 32.00

    The portal's own review says:

        GRSS               = 1,552.00   ->  real saving    22.00

    A $10 error that overstated the saving by 31%. Taxes and fees do not
    scale uniformly with occupancy, so the multiplication can never be
    exact - which is why a candidate stays UNCONFIRMED until /review.
    """
    estimate = Decimal("514.00") * 3
    inv = parse_review_changes(REAL)
    assert estimate == Decimal("1542.00")
    assert inv.gross == Decimal("1552.00")

    booking_gross = Decimal("1574.00")
    estimated_saving = booking_gross - estimate
    confirmed, _ = confirmed_saving(booking_gross, inv, original_obc=Decimal("0"))
    assert estimated_saving == Decimal("32.00")
    assert confirmed == Decimal("22.00")
    assert estimated_saving - confirmed == Decimal("10.00")


def test_commission_is_read_not_computed():
    """108.00 is 15% of the 720.00 cruise rate here, but the rate varies by
    booking (16% was observed on DEMO08), so it is READ from COMM."""
    inv = parse_review_changes(REAL)
    assert inv.commission == Decimal("108.00")
    assert inv.commission == inv.cruise_rate * Decimal("0.15")   # true HERE only


# ── missing is not zero ──────────────────────────────────────────────────


def test_a_real_zero_obc_is_distinguishable_from_a_missing_one():
    """POBC 0.00 means "no onboard credit". An absent POBC means "we could
    not read it". Collapsing them would let OBC_UNVERIFIED masquerade as a
    booking with none."""
    present = parse_review_changes(REAL)
    assert present.obc == Decimal("0")
    assert "OBC_UNVERIFIED" not in " ".join(present.warnings)

    stripped = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 720.00)],
        "summaryTotal": _item("GRSS", "Gross Amount", 720.00),
    }}}
    absent = parse_review_changes(stripped)
    assert absent.obc is None
    assert any("OBC_UNVERIFIED" in w for w in absent.warnings)


def test_a_missing_commission_is_reported_not_assumed():
    stripped = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 720.00)],
        "summaryTotal": _item("GRSS", "Gross Amount", 720.00),
    }}}
    inv = parse_review_changes(stripped)
    assert inv.commission is None
    assert any("COMMISSION_UNVERIFIED" in w for w in inv.warnings)


@pytest.mark.parametrize("body", [None, {}, {"agentInvoice": None},
                                  {"agentInvoice": {}}, "not a dict"])
def test_a_malformed_response_yields_no_figures_and_says_so(body):
    inv = parse_review_changes(body)
    assert inv.gross is None
    assert inv.warnings


def test_money_keeps_a_real_zero_and_rejects_a_missing_one():
    assert money({"price": {"amount": 0}}) == Decimal("0")
    assert money({"price": {"amount": None}}) is None
    assert money({}) is None
    assert money(None) is None


def test_amounts_are_decimal_not_float():
    """Cents matter: 0.1 + 0.2 must not become 0.30000000000000004."""
    inv = parse_review_changes(REAL)
    assert isinstance(inv.gross, Decimal)
    assert isinstance(inv.commission, Decimal)


# ── the saving refuses to guess ──────────────────────────────────────────


def test_no_confirmed_saving_without_a_review_gross():
    inv = ReviewInvoice()
    saving, why = confirmed_saving(Decimal("1574.00"), inv)
    assert saving is None
    assert "PRICE_UNVERIFIED" in why


def test_no_confirmed_saving_without_the_bookings_own_gross():
    saving, why = confirmed_saving(None, parse_review_changes(REAL))
    assert saving is None
    assert "PRICE_UNVERIFIED" in why


def test_a_non_reconciling_invoice_produces_no_saving():
    bad = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 100.00)],
        "summaryTotal": _item("GRSS", "Gross Amount", 999.00),
    }}}
    saving, why = confirmed_saving(Decimal("1574.00"), parse_review_changes(bad))
    assert saving is None
    assert "PRICE_CONFLICT" in why


def test_an_obc_change_moves_the_saving_and_is_never_inferred():
    """OBC is applied as a CHANGE between two read figures. It is never
    derived by subtracting totals."""
    body = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 1552.00)],
        "perks": [_item("POBC", "Total Onboard Credit", 25.00)],
        "summaryTotal": _item("GRSS", "Gross Amount", 1552.00),
    }}}
    inv = parse_review_changes(body)
    saving, why = confirmed_saving(Decimal("1574.00"), inv,
                                   original_obc=Decimal("75.00"))
    # gross saving 22.00, but 50.00 of onboard credit is given up
    assert saving == Decimal("-28.00")
    assert "onboard-credit change" in why


def test_an_unexpected_total_code_warns_about_schema_drift():
    body = {"agentInvoice": {"summary": {
        "lineItems": [_item("CRUS", "Cruise Rate", 100.00)],
        "summaryTotal": _item("XXXX", "Something Else", 100.00),
    }}}
    inv = parse_review_changes(body)
    assert any("schema may have changed" in w for w in inv.warnings)


def test_the_code_constants_match_the_portals_own_names():
    assert (CRUISE_RATE, COMMISSION, ONBOARD_CREDIT, GROSS, CRUISE_CHARGES) == \
        ("CRUS", "COMM", "POBC", "GRSS", "CNRF")


# ── TS45C7: the booking that proved the fail-safe works ──────────────────
#
# Second booking to reach /review on the safe path. It carried TWO codes
# never seen on DEMO11 - ADMN "Administrative Fee" and NOBC "All Departments
# Onboard Credit" - and the first version of this parser, which summed a
# HARDCODED list of addends, therefore failed to balance and returned
# PRICE_CONFLICT instead of a number.
#
# That refusal was correct and valuable: the estimate for this booking
# claimed a $184 saving, while the portal's own review says the new gross is
# 3,076.00 against a booking gross of 3,070.00 - a $6 price INCREASE. A
# parser that had guessed past the unknown code would have reported a
# $184 saving that does not exist.

TS45C7 = {
    "agentInvoice": {"summary": {
        "lineItems": [
            _item("CNRF", "Cruise Charges", 2736.42, isTotal=True),
            _item("CRUS", "Cruise Rate", 2130.00, isIndented=True),
            _item("PTCH", "Non-Comm Cruise Amount", 338.00, isIndented=True),
            _item("RCFE", "Required Cruise Fees & Expenses", 268.42, isIndented=True),
            _item("GTFE", "Government Taxes & Fees", 239.58, isTotal=True),
            _item("INSU", "Carnival Vacation Protection", 0.00, isTotal=True),
            _item("PKGS", "Pre/Post Packages", 0.00),
            _item("TADD", "Transportation Add-On", 0.00, isTotal=True),
            _item("ADMN", "Administrative Fee", 100.00, isTotal=True),
        ],
        "commissions": [_item("COMM", "Commission", 319.50, isTotal=True)],
        "perks": [
            _item("POBC", "Total Onboard Credit", 50.00, isTotal=True),
            _item("NOBC", "All Departments Onboard Credit", 50.00),
        ],
        "summaryTotal": _item("GRSS", "Gross Amount", 3076.00, isTotal=True),
    }}
}


def test_an_unfamiliar_line_code_is_handled_structurally():
    """ADMN had never been seen. The sum works because it reads the
    isIndented flag rather than a hardcoded list of codes."""
    inv = parse_review_changes(TS45C7)
    assert inv.items["ADMN"] == Decimal("100.00")
    ok, why = inv.reconciles()
    assert ok, why


def test_the_gross_is_the_sum_of_NON_indented_items():
    """CNRF 2,736.42 + GTFE 239.58 + INSU 0 + PKGS 0 + TADD 0 + ADMN 100
    = 3,076.00. The indented CRUS/PTCH/RCFE are what CNRF is made of."""
    inv = parse_review_changes(TS45C7)
    assert inv.indented == {"CRUS", "PTCH", "RCFE"}
    addends = [c for c in inv.line_codes if c not in inv.indented]
    assert sum(inv.items[c] for c in addends) == Decimal("3076.00")
    assert sum(inv.items[c] for c in inv.indented) == inv.items["CNRF"]


def test_this_booking_is_a_price_INCREASE_not_a_saving():
    """THE HEADLINE. The per-person estimate claimed +$184; the portal says
    the new gross is HIGHER than the booking's own."""
    inv = parse_review_changes(TS45C7)
    booking_gross = Decimal("3070.00")
    estimate = Decimal("1443.00") * 2          # per-person x 2 guests
    assert booking_gross - estimate == Decimal("184.00")   # what was claimed
    saving, _ = confirmed_saving(booking_gross, inv)
    assert saving == Decimal("-6.00")                      # what is true


def test_the_two_obc_lines_are_not_double_counted():
    """POBC "Total Onboard Credit" is the total; NOBC "All Departments
    Onboard Credit" is a component. Both read 50.00 - adding them would
    invent 50.00 of credit."""
    inv = parse_review_changes(TS45C7)
    assert inv.obc == Decimal("50.00")          # POBC only
    assert inv.items["NOBC"] == Decimal("50.00")
    assert inv.obc != inv.items["POBC"] + inv.items["NOBC"]


def test_perks_and_commission_are_never_addends_of_the_gross():
    """They describe the gross, they do not compose it."""
    inv = parse_review_changes(TS45C7)
    assert "COMM" not in inv.line_codes
    assert "POBC" not in inv.line_codes
    assert "NOBC" not in inv.line_codes


def test_an_unbalanced_sum_names_the_unrecognised_codes():
    """Schema drift must be diagnosable, not just fatal (brief s.28)."""
    drifted = {"agentInvoice": {"summary": {
        "lineItems": [
            _item("CRUS", "Cruise Rate", 100.00),
            _item("ZZZZ", "Some New Fee", 25.00),
        ],
        "summaryTotal": _item("GRSS", "Gross Amount", 200.00),
    }}}
    ok, why = parse_review_changes(drifted).reconciles()
    assert ok is False
    assert "ZZZZ" in why
