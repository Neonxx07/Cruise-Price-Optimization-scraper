"""NCL: a cancelled booking is not "paid in full", and a settled one is
remembered.

TWO DEFECTS, FOUND TOGETHER because the second could not be fixed safely
without the first.

DEFECT 1 - NCL NEVER REMEMBERED A PAID-IN-FULL BOOKING.

Neon's rule is explicit: *"IF THE BOOKING SURELY FOR 100% SURE IS PAID IN
FULL THIS MUST BE STORED IN THE DATA BASE AND NEVER EVER BE RESCANNED
AGAIN EVEN IF THE USER PASTES OR ADDS IT IN THE LIST THIS IS A NON
NEGOTIOABLE RULE!"*

`ExclusionService.record_paid_in_full` reads `scraper.last_payment_status`.
**NclScraper never set it.** So the first guard - `payment_state_readable`
- rejected every NCL booking, and the live database agrees exactly:

    permanent_exclusions:  ESPRESSO 215 PAID_IN_FULL, 1 CANCELLED
                           NCL        0
    log, one day:          368 x exclusion.refused payment_panel_unreadable

122 of the 188 bookings in the 2026-10-07 NCL run were paid-in-full and
every one was re-scanned in full.

DEFECT 2 - AND THE DATA THAT WOULD HAVE BEEN MADE PERMANENT WAS WRONG.

NCL decides paid-in-full from `net_due <= 0.01`. **A cancelled booking has
no balance either.** The portal says so plainly in a field this project had
been capturing for weeks and never read - `payment.bookingFacts.Status`,
present in **1,478 of 1,478** NCL captures:

    BOOKED 1295    CLOSED 133    CANCELED 32    TERMINATED 18

Cross-checked against what was reported:

    portal=CANCELED    we said PAID_IN_FULL    (3001043, 3001046)
    portal=TERMINATED  we said PAID_IN_FULL    6
    portal=CLOSED      we said PAID_IN_FULL    41

This is the SAME defect Neon called V-VIP on ESPRESSO - *"status CX =
CANCELLED, MUST be reported"* - fixed there on 2026-09-22 and never looked
for on NCL. make_cancelled_result's own docstring describes the shape.

Fixing defect 1 without defect 2 would have written those wrong calls into
a PERMANENT, unrecoverable exclusion. CLOSED and TERMINATED are deliberately
NOT interpreted - nobody knows yet what they mean, and guessing is how a
cancelled booking became "paid in full" in the first place. They are
reported as before and merely refused permanence.
"""

import asyncio
import inspect

from core.models import BookingStatus
from scraper.ncl import NclScraper
from services.exclusion_service import ExclusionService


def _code_only(func) -> str:
    """Source with comments stripped. Every ordering test in this file is
    about CODE; matching a comment that merely names a function proves
    nothing (and silently produced an empty window the first time)."""
    import io
    import tokenize

    src = inspect.getsource(func)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type != tokenize.COMMENT:
            out.append(tok.string)
    return " ".join(out)


# -- the portal's own status is read at all ------------------------------


def test_the_status_field_is_read():
    src = inspect.getsource(NclScraper._check_booking_market)
    assert "bookingFacts" in src and "Status" in src, (
        "NCL still ignores the status the portal states outright")


def test_cancelled_is_decided_before_paid_in_full():
    """Ordering is the whole fix: a cancelled booking satisfies every
    paid-in-full test there is, so whichever runs first wins."""
    src = _code_only(NclScraper._check_booking_market)
    assert src.index("make_cancelled_result") < src.index("make_paid_in_full_result"), (
        "the paid-in-full gate runs first, so a cancelled booking is still "
        "filed as PAID_IN_FULL - that is the bug")


def test_the_capture_happens_before_the_cancelled_return():
    """A cancelled booking's payment panel is still captured data and is
    never dropped - Neon's standing rule."""
    src = _code_only(NclScraper._check_booking_market)
    assert src.index("ncl_booking_details") < src.index("make_cancelled_result")


def test_only_the_exact_cancelled_value_short_circuits():
    """CLOSED and TERMINATED must NOT be silently reinterpreted as
    cancellations - this project does not know what they mean."""
    src = _code_only(NclScraper._check_booking_market)
    start = src.index("booking_status =")
    window = src[start:src.index("make_cancelled_result", start)]
    assert '== "CANCELED"' in window
    for guess in ("CLOSED", "TERMINATED"):
        assert f'"{guess}"' not in window, (
            f"{guess} is being treated as a cancellation on a guess")


def test_the_payment_evidence_is_published_for_the_exclusion_layer():
    src = inspect.getsource(NclScraper._check_booking_market)
    assert "self.last_payment_status" in src, (
        "NCL still publishes nothing, so every exclusion is refused")
    for key in ("total_price", "final_payment_due",
                "payment_state_readable", "booking_status"):
        assert f'"{key}"' in src


# -- the exclusion guard -------------------------------------------------


def _record(**kw):
    svc = ExclusionService()
    written = {}

    async def _fake(cruise_line, booking_id, reason, evidence):
        written.update(booking_id=booking_id, reason=reason, evidence=evidence)
        return True

    svc._record = _fake
    args = dict(total_price=1000.0, final_payment_due=0.0,
                payment_state_readable=True)
    args.update(kw)
    ok = asyncio.run(svc.record_paid_in_full("NCL", "66000001", **args))
    return ok, written


def test_a_plain_booked_reservation_is_remembered():
    ok, written = _record(booking_status="BOOKED")
    assert ok is True
    assert written["reason"] == "PAID_IN_FULL"


def test_a_line_that_states_no_status_still_works():
    """ESPRESSO has no such field and must be unaffected."""
    ok, _ = _record(booking_status=None)
    assert ok is True


def test_a_cancelled_reservation_is_never_made_permanent():
    ok, written = _record(booking_status="CANCELED")
    assert ok is False
    assert not written


def test_terminated_and_closed_are_refused_rather_than_guessed_at():
    for status in ("TERMINATED", "CLOSED"):
        ok, _ = _record(booking_status=status)
        assert ok is False, f"{status} was made permanent on an assumption"


def test_the_status_check_runs_before_the_figure_checks():
    """A cancelled booking passes every figure test - readable panel, a
    final-payment figure of 0.00, a real total - so checking figures first
    would let it through."""
    src = inspect.getsource(ExclusionService.record_paid_in_full)
    assert src.index("portal_status_not_booked") < src.index("payment_panel_unreadable")


def test_case_and_whitespace_do_not_defeat_the_guard():
    ok, _ = _record(booking_status="  canceled  ")
    assert ok is False


# -- the unreadable-panel guard is still the backstop ---------------------


def test_an_unreadable_panel_is_still_refused():
    ok, _ = _record(booking_status="BOOKED", payment_state_readable=False)
    assert ok is False


def test_a_zero_total_is_still_refused():
    """"We could not see the balance" must never become "it owes nothing"
    - the 3001001 lesson."""
    ok, _ = _record(booking_status="BOOKED", total_price=0.0)
    assert ok is False


def test_a_missing_final_payment_figure_is_still_refused():
    ok, _ = _record(booking_status="BOOKED", final_payment_due=None)
    assert ok is False


# -- and the result itself ------------------------------------------------


def test_no_real_captured_non_booked_reservation_can_be_made_permanent():
    """THE REAL GUARD: run every NCL payment panel this project has ever
    captured through the exclusion rule.

    Structural tests check that the code says the right thing. This checks
    the corpus - the same shape as the cash-increase invariant, and the
    reason the 2026-10-07 defect was provable rather than suspected: the
    portal's status was sitting in 1,478 of 1,478 captures all along.
    """
    import json
    import pathlib
    import sqlite3

    db = pathlib.Path("cruise_intel.db")
    if not db.exists():  # a clean checkout has no corpus
        return

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "select payload_json from market_data "
        "where cruise_line='NCL' and capture_type='ncl_booking_details'"
    ).fetchall()
    conn.close()

    checked = 0
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        status = ((payload.get("payment") or {}).get("bookingFacts") or {}).get("Status")
        if not status or status.strip().upper() == "BOOKED":
            continue
        checked += 1
        derived = payload.get("derived") or {}
        ok, _ = _record(
            booking_status=status,
            # Deliberately the MOST permissive figures: a cancelled
            # booking really does look settled, so the status check is
            # the only thing standing between it and permanence.
            total_price=1000.0,
            final_payment_due=derived.get("amount_due") or 0.0,
            payment_state_readable=True,
        )
        assert ok is False, (
            f"a reservation the portal calls {status} would be excluded "
            "permanently")

    assert checked, "no non-BOOKED captures found - has the corpus moved?"


def test_a_cancelled_result_is_not_a_saving():
    from core.calculator import make_cancelled_result
    from core.models import CruiseLine

    r = make_cancelled_result("3001043", "IA", CruiseLine.NCL,
                              detail="NCL booking status CANCELED")
    assert r.status == BookingStatus.CANCELLED
    assert not r.net_saving
    assert r.confidence == 0
