"""A CANCELLED booking must be reported as cancelled. Never as "paid in full".

Neon 2026-09-22, in capitals: "PLEASE PLEASE PLEASE MAKE SURE TO PIN THIS AS
V V V VIP THING WHICH IS IF THE SCANNER OR SCRIPT CATCHES THIS IT MEANS THAT
THE BOOKING IS CANCELED AND IT IS VERY MADNATORY TO REPORT IT AS IT SOMETHING
VERY CRITICAL":

    <span ng-show="'CX' == sb.reservation.status" class="ng-binding">N/A</span>
    booking 3001005 — Total Price (USD) : N/A

WHAT WAS HAPPENING. A cancelled reservation's payment panel still renders

    Total Price (USD): 0.00      Payments Received (USD): 200.00
    Final Payment Due (USD): 0.00

and is_paid_in_full(0.00, 0.00) is True. So 3001005 was filed as
"Fully paid — repricing unavailable" on 15, 16, 21 AND 22 September, and 87
stored results across 27 distinct bookings carry that same zero-total
signature.

A cancellation is not a pricing outcome. It is an account fact somebody has
to act on, and it was being hidden inside the quietest status we have.
"""
import inspect

import pytest

from core.calculator import is_paid_in_full, make_cancelled_result
from core.models import BookingStatus, CruiseLine
from scraper.espresso import EspressoScraper


def test_cancelled_is_its_own_status():
    assert BookingStatus.CANCELLED.value == "CANCELLED"


def test_the_exact_trap_that_caught_3001005():
    """THE REGRESSION. A cancelled booking's own figures satisfy the
    paid-in-full rule, which is why the CX check has to come first."""
    assert is_paid_in_full(0.00, 0.00) is True


def _check_booking_code() -> str:
    """check_booking with comments stripped.

    The comments deliberately name the calls they are explaining, so a plain
    substring search reads prose and gets the ordering wrong - which is
    exactly how this test first failed.
    """
    src = inspect.getsource(EspressoScraper.check_booking)
    return chr(10).join(
        line for line in src.splitlines() if not line.strip().startswith("#"))


def test_cancelled_is_checked_BEFORE_paid_in_full():
    code = _check_booking_code()
    assert code.index("is_cancelled()") < code.index("is_paid_in_full(")


def test_cancelled_is_checked_before_the_payment_panel_is_even_read():
    code = _check_booking_code()
    assert code.index("is_cancelled()") < code.index("_read_payment_status()")


def test_the_result_says_cancelled_in_plain_words():
    r = make_cancelled_result("3001005", "ZI", CruiseLine.ESPRESSO)
    assert r.status is BookingStatus.CANCELLED
    assert "CANCELLED" in r.note
    assert r.net_saving == 0.0
    assert r.confidence == 0


def test_the_result_never_reads_as_a_saving_or_as_paid():
    r = make_cancelled_result("3001005", "ZI", CruiseLine.ESPRESSO)
    assert "Fully paid" not in r.note
    assert "repricing unavailable" not in r.note
    assert r.old_total == 0.0 and r.new_total == 0.0


# ── the detection must use the RUNTIME state, not the markup ─────────────


def test_detection_does_not_match_the_template_alone():
    """That span is in EVERY booking page - it is an Angular template that
    Angular hides when the status is not CX. The page also carries the
    OPPOSITE guard ("'CX' != ...") on each price link of a perfectly healthy
    booking. Matching markup would flag everything."""
    src = inspect.getsource(EspressoScraper.is_cancelled)
    assert "offsetParent" in src          # actually visible
    assert "ng-hide" in src               # and not Angular-hidden
    assert "==" in src                    # the POSITIVE guard only


def test_an_unreadable_page_is_NOT_called_cancelled():
    """A false CANCELLED would hide a live booking from the watchlist. The
    probe errs toward saying nothing; the payment-readability guard
    downstream still refuses to invent a saving."""
    src = inspect.getsource(EspressoScraper.is_cancelled)
    assert "return False" in src
    assert "except Exception" in src


def test_the_detection_is_logged_loudly():
    """Mandatory to report - so it cannot be a debug line."""
    code = _check_booking_code()
    idx = code.index("is_cancelled()")
    assert "logger.warning" in code[idx:idx + 400]
    assert "booking_cancelled" in code[idx:idx + 400]


# ── it has to be VISIBLE once reported ───────────────────────────────────


def test_cancelled_has_its_own_colour_not_shared_with_paid_in_full():
    """It used to be filed as PAID_IN_FULL - the same pale blue as WLT and
    SKIPPED_TODAY, invisible in a sheet of 700 rows."""
    from services.excel_export import _FILLS

    assert "CANCELLED" in _FILLS
    cancelled = _FILLS["CANCELLED"].fgColor.rgb
    for other in ("PAID_IN_FULL", "WLT", "SKIPPED_TODAY", "NO_SAVING"):
        assert _FILLS[other].fgColor.rgb != cancelled


def test_cancelled_sorts_near_the_top_of_the_report():
    """A cancelled booking at the bottom of a 700-row sheet is exactly the
    failure this ordering prevents."""
    from services.excel_export import _SORT_ORDER

    assert _SORT_ORDER["CANCELLED"] <= 1
    for lower in ("PAID_IN_FULL", "NO_SAVING", "WLT", "ERROR"):
        assert _SORT_ORDER["CANCELLED"] < _SORT_ORDER[lower]


def test_the_gui_tints_it_distinctly_too():
    from gui.windows import CruiseLinePanel

    tints = CruiseLinePanel._STATUS_TINTS
    assert "CANCELLED" in tints
    assert tints["CANCELLED"] != tints.get("PAID_IN_FULL")


@pytest.mark.parametrize("status", list(BookingStatus))
def test_every_status_including_cancelled_renders(status):
    from services.excel_export import _FILLS, _SORT_ORDER

    assert status.value in _FILLS
    assert status.value in _SORT_ORDER
