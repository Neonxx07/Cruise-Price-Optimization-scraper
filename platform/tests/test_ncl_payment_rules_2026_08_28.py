"""Three NCL business rules Neon stated on 2026-08-28, from three real
bookings in that day's run.

1. **3000057** - "this booking is paid in full and the due amount is 43$
   only why it did not show that it is paid in full". NCL decided
   paid-in-full from the single boolean `d.bi.IsPaid`, which is false while
   ANY balance remains. $43 on $1,818 is 2.4% - inside the project's
   existing 5% tolerance, so ESPRESSO would have caught it months ago. NCL
   never read a balance to apply the rule to.

2. **3000057 again** - "i did optimize with the 100 but we actually get 43
   not 100 because the customer has paid in full". A reprice reduces the
   OUTSTANDING balance, so the recoverable amount is capped by what is
   still owed. Reporting $100 overstates the recovery by $57.

3. **3000053** - "from now on we are adding a new rule if the final
   payment date has passed on ncl we do not optimize the booking at all
   because it will cause a penality".

Plus **3000054** - "we will lose 48$ comission" - a lower fare means less
commission, which was not modelled at all.

Field names come from REAL captured pages, never guessed:
    FINAL PAYMENT | 01/09/2027 | $2,028.10
    Gross Due $1,793.10   Com.Due $250.90   Net Due $1,542.20
"""
from datetime import datetime

import pytest

from core.calculator import (
    calculate_ncl,
    is_paid_in_full,
    ncl_commission_loss,
    ncl_final_payment_passed,
    realizable_saving,
)
from core.models import BookingStatus


# -- rule 1: paid in full by TOLERANCE, not a boolean ----------------


def test_3000057_is_paid_in_full_by_the_existing_tolerance():
    """$43 outstanding on $1,818 = 2.4%, inside the 5% rule that ESPRESSO
    has used for months. The rule was never the problem - NCL had no
    balance to feed it."""
    assert is_paid_in_full(43.0, 1818.0) is True


def test_a_real_balance_is_still_not_paid_in_full():
    """The gate must not swallow bookings that genuinely owe money."""
    assert is_paid_in_full(370.84, 1818.0) is False


# -- rule 2: collectable is capped by the balance --------------------


def test_only_the_outstanding_balance_is_collectable():
    """Neon's exact numbers: a $100 drop with $43 still owed yields $43."""
    assert realizable_saving(100.0, 43.0) == 43.0


def test_a_drop_smaller_than_the_balance_is_fully_collectable():
    assert realizable_saving(40.0, 1000.0) == 40.0


def test_an_unknown_balance_caps_nothing():
    """None means "could not read". Capping on a guess would understate a
    real saving; the caller reports the unknown instead."""
    assert realizable_saving(100.0, None) == 100.0


def test_a_zero_balance_collects_nothing():
    assert realizable_saving(100.0, 0.0) == 0.0


def test_optimization_note_states_what_is_actually_collectable():
    r = calculate_ncl("3000057", "BB", 1818.0, 1718.0, [], "", "",
                      new_addons=[], amount_due=43.0)
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 100.0          # the price movement is unchanged
    assert "COLLECTABLE ONLY $43" in r.note
    assert "still owes $43" in r.note


def test_no_collectable_note_when_the_balance_covers_the_drop():
    """Must not clutter every result - only fire when it actually caps."""
    r = calculate_ncl("X", "BB", 1818.0, 1718.0, [], "", "",
                      new_addons=[], amount_due=5000.0)
    assert "COLLECTABLE ONLY" not in r.note


# -- rule 3: final payment date passed = do not optimize ------------


def test_a_passed_final_payment_date_is_detected():
    assert ncl_final_payment_passed("01/09/2025") is True


def test_a_future_final_payment_date_is_not():
    assert ncl_final_payment_passed("01/09/2027") is False


@pytest.mark.parametrize("value", [None, "", "not a date", "TBD"])
def test_an_unreadable_date_never_fires_the_gate(value):
    """Blocking a booking because a field could not be parsed would be its
    own kind of wrong - the caller reports the unknown instead."""
    assert ncl_final_payment_passed(value) is False


def test_us_date_order_is_used():
    """NCL prints MM/DD/YYYY (confirmed: FINAL PAYMENT 01/09/2027). Read as
    DMY, 01/09 would be 1 September and the gate would fire eight months
    early."""
    ref = datetime(2026, 5, 1)
    # 03/04/2026 is 4 March under MDY (past) and 3 April under DMY (future).
    assert ncl_final_payment_passed("03/04/2026", today=ref) is True


def test_3000053_is_blocked_outright_when_the_date_has_passed():
    r = calculate_ncl("3000053", "BX", 2368.0, 2268.0, [], "", "",
                      new_addons=[], final_payment_date="01/09/2025")
    assert r.status == BookingStatus.TRAP
    assert r.status != BookingStatus.OPTIMIZATION
    assert "FINAL PAYMENT DATE" in r.note
    assert "penalty" in r.note.lower()
    assert r.confidence == 1


def test_the_same_booking_is_optimizable_before_that_date():
    r = calculate_ncl("3000053", "BX", 2368.0, 2268.0, [], "", "",
                      new_addons=[], final_payment_date="01/09/2027")
    assert r.status == BookingStatus.OPTIMIZATION


def test_the_final_payment_gate_outranks_a_large_saving():
    """A penalty applies regardless of how good the drop looks."""
    r = calculate_ncl("BIG", "BX", 10000.0, 5000.0, [], "", "",
                      new_addons=[], final_payment_date="01/01/2020")
    assert r.status == BookingStatus.TRAP


# -- commission ------------------------------------------------------


def test_commission_loss_uses_the_bookings_own_rate():
    """Neon's $48 on booking 3000054, whose drop was $300 -> 16%. The
    captured example gives 13.99% (Com.Due 250.90 / Gross Due 1793.10), so
    the rate genuinely varies and must be read per booking."""
    assert ncl_commission_loss(300.0, 0.16) == 48.0
    assert ncl_commission_loss(1793.10, 0.1399) == pytest.approx(250.85, abs=0.1)


def test_an_unknown_commission_rate_costs_nothing_rather_than_a_guess():
    assert ncl_commission_loss(300.0, None) == 0.0
    assert ncl_commission_loss(300.0, 0.0) == 0.0


def test_commission_appears_in_the_note_with_the_agency_net():
    r = calculate_ncl("3000054", "BF", 2257.0, 1957.0, [], "", "",
                      new_addons=[], commission_rate=0.16)
    assert "COSTS $48 OF COMMISSION" in r.note
    assert "net gain to the agency is $252" in r.note


def test_no_commission_note_when_the_rate_is_unknown():
    r = calculate_ncl("X", "BF", 2257.0, 1957.0, [], "", "", new_addons=[])
    assert "COMMISSION" not in r.note


# -- the rules must not fight each other ----------------------------


def test_a_clean_booking_is_untouched_by_all_of_this():
    """Every new rule is conditional. A booking with a future payment date,
    a balance larger than the drop and no known commission must look
    exactly as it did before."""
    r = calculate_ncl("CLEAN", "BB", 3798.0, 3778.0, [], "", "",
                      new_addons=[], amount_due=3798.0,
                      final_payment_date="01/09/2027")
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 20.0
    assert r.confidence == 5
    for phrase in ("COLLECTABLE ONLY", "COMMISSION", "FINAL PAYMENT"):
        assert phrase not in r.note


def test_the_protected_promo_gate_still_outranks_the_payment_gates():
    """LATRIPLE remains the strongest rule - it must not be displaced."""
    r = calculate_ncl("PROT", "BB", 2000.0, 1600.0, [],
                      old_promos="LATRIPLE", new_promos="",
                      new_addons=[], amount_due=50.0, commission_rate=0.14)
    assert r.status == BookingStatus.TRAP
    assert "LATRIPLE" in r.note
    assert r.confidence == 1


# -- FITOBC / LATDBLX joined the hard-gate list (Neon, 2026-08-28) --


def test_fitobc_and_latdblx_are_now_protected():
    """Neon confirmed these are "the same" case as LATRIPLE. FITOBC is an
    on-board-credit promo lost on 4 of that day's 36 optimizations, worth
    $1,532 combined; LATDBLX is Latitudes double points."""
    from core.calculator import NCL_NEVER_LOSE_PROMOS

    assert NCL_NEVER_LOSE_PROMOS == frozenset(
        {"LATRIPLE", "FREESRVC", "FITOBC", "LATDBLX"}
    )


def test_3000046_is_now_a_hard_trap_not_a_456_dollar_win():
    """The booking Neon queried first. It reported "$456 saved" while
    forfeiting FITOBC, whose value a promo code never exposes on the page -
    which is exactly why it has to be gated rather than priced."""
    r = calculate_ncl(
        "3000046", "B6", 3222.80, 2716.80,
        [{"guest": "MS DONN", "name": "Free $50 On-Board Credit Certificate"}],
        old_promos="FITOBC,EASYFARE", new_promos="FLATOFF,EASYFARE",
        new_addons=[],
    )
    assert r.status == BookingStatus.TRAP
    assert r.status != BookingStatus.OPTIMIZATION
    assert "FITOBC" in r.note
    assert r.confidence == 1
    assert r.lost_fares == ["FITOBC"]


def test_3000048_latdblx_is_gated_even_for_a_tiny_saving():
    """A $10 win is not worth Latitudes double points."""
    r = calculate_ncl("3000048", "T1", 519.0, 509.0, [],
                      old_promos="LATDBLX,DISC50", new_promos="DISC50",
                      new_addons=[])
    assert r.status == BookingStatus.TRAP
    assert "LATDBLX" in r.note


def test_keeping_fitobc_is_still_a_valid_optimization():
    """The gate must fire on LOSS, not on presence - otherwise every
    FITOBC booking becomes permanently unoptimizable."""
    r = calculate_ncl("KEEP", "B6", 3222.80, 2716.80, [],
                      old_promos="FITOBC,EASYFARE",
                      new_promos="FITOBC,EASYFARE,FLATOFF",
                      new_addons=[])
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 506.0


def test_latrew_and_latitude_are_deliberately_not_gated():
    """They appear 37 and 11 times in the same run and are plausibly the
    same loyalty family, but the owner has NOT ruled on them. Over-gating
    silently destroys real savings, so they stay a warning, not a block."""
    from core.calculator import NCL_NEVER_LOSE_PROMOS

    assert "LATREW" not in NCL_NEVER_LOSE_PROMOS
    assert "LATITUDE" not in NCL_NEVER_LOSE_PROMOS

    r = calculate_ncl("LR", "BF", 2000.0, 1800.0, [],
                      old_promos="LATREW,DISC50", new_promos="DISC50",
                      new_addons=[])
    assert r.status == BookingStatus.OPTIMIZATION
    assert "LOSES PROMO(S): LATREW" in r.note      # warned, not blocked


# -- 3000049 / 3000052: the commission-rate bug Neon caught --------


def test_commission_rate_is_earned_over_total_not_comdue_over_grossdue():
    """MY OWN BUG, caught by Neon on booking 3000049.

    Its Invoice and Payments panel reads:
        Funds Avail. $3,795.00   Commiss.Earned $521.28
        Gross Due $163.00        Com.Due $163.00      Net Due $0.00
    on a $3,958.00 booking.

    I derived the rate as Com.Due / Gross Due, which is not a rate at all -
    it is the COMPOSITION of the outstanding balance. Here both are $163.00,
    so it returned **100%** and a $610 drop would have been costed at $610 of
    commission. It only looked plausible on an earlier capture
    (250.90 / 1793.10 = 13.99%) by coincidence, because most of that balance
    was still client money.

    The real rate is Commiss.Earned / booking total = 13.17%, which puts the
    same $610 drop at $80.34.
    """
    total, commiss_earned = 3958.00, 521.28
    gross_due, com_due = 163.00, 163.00

    wrong = com_due / gross_due
    right = commiss_earned / total
    assert wrong == 1.0, "the old formula really did return 100%"
    assert round(right, 4) == 0.1317
    assert ncl_commission_loss(610.0, right) == pytest.approx(80.34, abs=0.01)
    assert ncl_commission_loss(610.0, wrong) == 610.0     # what it used to do


def test_3000049_is_paid_in_full_and_never_reaches_the_calculator():
    """$163 outstanding on $3,958 is 4.12% - inside the 5% tolerance. It was
    reported as a clean $610 OPTIMIZATION at confidence 5."""
    assert is_paid_in_full(163.00, 3958.00) is True


def test_only_163_of_the_610_was_ever_collectable():
    assert realizable_saving(610.0, 163.0) == 163.0


def test_a_balance_that_is_entirely_commission_is_flagged():
    """Net Due $0.00 with Com.Due == Gross Due means the cruise line is
    fully paid and the only outstanding money is the agency's own
    commission - repricing cannot save the client anything, it just shrinks
    what the agency collects."""
    r = calculate_ncl("3000049", "BF", 3958.0, 3348.0, [],
                      new_addons=[], amount_due=163.0,
                      commission_rate=0.1317,
                      balance_is_all_commission=True)
    assert "entire outstanding balance is COMMISSION" in r.note
    assert "COLLECTABLE ONLY $163" in r.note
    assert "COSTS $80 OF COMMISSION" in r.note


def test_the_all_commission_warning_does_not_fire_normally():
    r = calculate_ncl("X", "BF", 3958.0, 3348.0, [], new_addons=[],
                      amount_due=3958.0, commission_rate=0.1317)
    assert "entire outstanding balance is COMMISSION" not in r.note
