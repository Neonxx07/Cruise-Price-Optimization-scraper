"""A booking that cannot be repriced still has a price. Record it.

Neon 2026-10-01, on bookings 3001013, 3001017 and 3001019: *"THIS BOOKINGS
ARE NOT SHOWING PRICES WE NEED TO SAVE ALL PRICES NOW SINCE WE ARE HAVING
AND RUNNING A DATA BASE."*

WHAT THE DATABASE SHOWED. All three stored `old 0.00 / new 0.00 / conf 0`,
indistinguishable on the row from a booking nothing could be read from:

    3001017   15 scans,  0 captures ever,  no price ever recorded
    3001019   22 scans,  last capture 2026-08-28
    3001013    4 scans,  real prices on 09-28/09-29 (one a $57
                         OPTIMIZATION), then blank

THE CAUSE. Two sentinel paths returned a result with no figures at all:

  * `skipRepriceModal` - the portal's own "Booking Restriction: Changing
    price pgm is not allowed". **559 occurrences across 154 bookings.**
  * WLT (waitlisted). **1,741 rows**, the single largest group of
    price-less rows in the database.

Neither is an error, and in both the Reservation Summary panel - carrying
Total Price - is already rendered on screen. Nothing read it.

Measured before the fix: **4,763 of 13,129 rows (36%) carried no price**,
and **224 of 1,280 bookings had never had a price recorded at all.**

KNOWN LIMITATION, recorded rather than hidden. `BookingResult.old_total`
is typed `float = 0.0`, so an unreadable price still lands as 0.0 rather
than a distinct "unknown". The note carries a figure only when one was
really read, and `confidence` stays 0, so the two remain distinguishable on
the row. Separating them properly means making old_total optional across
the model, the database column and every consumer - worth doing, too wide
to do alongside this fix.
"""

from core.calculator import make_skip_reprice_result, make_wlt_result
from core.models import BookingStatus, CruiseLine

LINE = CruiseLine.ESPRESSO


# -- restricted bookings -------------------------------------------------


def test_a_restricted_booking_records_its_price():
    result = make_skip_reprice_result("3001017", "X", LINE,
                                      old_total=2454.00, currency="USD")
    assert result.old_total == 2454.00
    assert result.currency == "USD"
    assert result.status == BookingStatus.NO_SAVING


def test_a_restricted_booking_shows_the_price_in_its_note():
    result = make_skip_reprice_result("3001017", "X", LINE, old_total=2454.00)
    assert "2,454.00" in result.note
    assert "restriction" in result.note.lower()


def test_an_unreadable_price_is_not_dressed_up_as_a_figure():
    """A missing price must not appear in the note as though it were read.
    The stored 0.0 is the model's floor, not a claim - see the module
    docstring's known limitation."""
    result = make_skip_reprice_result("3001017", "X", LINE, old_total=None)
    assert "current total" not in result.note
    assert not result.old_total


def test_a_restricted_booking_with_no_price_is_still_reported():
    """Unknown price is not a reason to drop the row."""
    result = make_skip_reprice_result("3001017", "X", LINE)
    assert result.status == BookingStatus.NO_SAVING
    assert result.booking_id == "3001017"


# -- waitlisted bookings -------------------------------------------------


def test_a_waitlisted_booking_records_its_price():
    result = make_wlt_result("3001015", "X", LINE,
                             old_total=1981.00, currency="USD")
    assert result.old_total == 1981.00
    assert result.currency == "USD"
    assert result.status == BookingStatus.WLT


def test_a_waitlisted_booking_shows_the_price_in_its_note():
    result = make_wlt_result("3001015", "X", LINE, old_total=1981.00)
    assert "1,981.00" in result.note
    assert "WLT" in result.note


def test_a_waitlisted_booking_with_an_unreadable_price_claims_nothing():
    result = make_wlt_result("3001015", "X", LINE, old_total=None)
    assert result.note == "WLT - waitlisted"
    assert not result.old_total


def test_wlt_still_works_without_the_new_arguments():
    """Called positionally elsewhere; the signature must stay compatible."""
    result = make_wlt_result("3001015", "X", LINE)
    assert result.status == BookingStatus.WLT


# -- neither path may invent a saving ------------------------------------


def test_a_restricted_booking_never_reports_a_saving():
    """old_total == new_total, so nothing downstream can read a drop."""
    result = make_skip_reprice_result("3001017", "X", LINE, old_total=2454.00)
    assert result.new_total == result.old_total
    assert not result.net_saving


def test_a_waitlisted_booking_never_reports_a_saving():
    result = make_wlt_result("3001015", "X", LINE, old_total=1981.00)
    assert result.new_total == result.old_total
    assert not result.net_saving


# -- the scraper actually asks for the price -----------------------------


def test_the_payment_panel_is_read_once_while_it_is_on_screen():
    """CORRECTED 2026-10-05. This test used to assert that
    `_read_payment_status()` is called at least THREE times - once per
    sentinel path plus the main one.

    That pinned the mechanism instead of the outcome, and the mechanism was
    wrong: the two extra calls happened AFTER `_click_categories()` had
    navigated away from the Reservation Summary, so they returned None on
    all 135 restricted and all 86 waitlisted bookings in the first live
    run. The test passed the whole time.

    The real contract is that the panel is read ONCE, while it is on
    screen, and the captured value is reused.
    """
    import ast
    import pathlib

    source = pathlib.Path("scraper/espresso.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    reads = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Await)
             and isinstance(n.value, ast.Call)
             and isinstance(n.value.func, ast.Attribute)
             and n.value.func.attr == "_read_payment_status"]

    assert reads, "the payment panel is never read at all"
    assert len(reads) == 1, (
        f"_read_payment_status is called {len(reads)} times. Extra calls "
        "after _click_categories() return None - see the module docstring.")


def test_the_sentinels_carry_the_total_back():
    """The dict returned by each sentinel must include oldTotal, or the
    builder has nothing to store."""
    import pathlib

    source = pathlib.Path("scraper/espresso.py").read_text(encoding="utf-8")
    for sentinel in ('"_skipRepriceModal": True', '"_wlt": True'):
        start = source.index(sentinel)
        window = source[start:start + 400]
        assert '"oldTotal"' in window, (
            f"{sentinel} no longer returns a price")


# -- the 2026-10-05 live run: the first version of this fix did NOT work --
#
# 0 of 135 restricted bookings and 0 of 86 WLT bookings got a price. The
# log said exactly why, on all 135:
#
#     espresso.skip_reprice  total_price=None
#     espresso.skip_reprice_price_unreadable   0 occurrences
#
# No exception - so `_read_payment_status()` ran fine and simply found
# nothing. It was pointed at the WRONG PAGE. Total Price lives on the
# Reservation Summary, and by the time these branches run,
# `_click_categories()` has already navigated to the category table:
#
#     1630  self.last_payment_status = payment_status   <- summary, price here
#     1717  await self._click_categories()              <- page changes
#     1734  WLT branch                                   <- too late
#     1768  skipRepriceModal branch                      <- too late
#
# Both now read the value captured while the summary was on screen.
#
# AND a correctness risk that came with it: `last_payment_status` was
# never cleared per booking, unlike last_market_data and
# last_feature_fields. A booking short-circuiting early - a cancellation,
# an early raise - would have inherited the PREVIOUS booking's price.
# BookingService also decides a PERMANENT paid-in-full exclusion on that
# attribute, so a stale read there is unrecoverable. It is reset now.


def _espresso_source() -> str:
    import pathlib
    return pathlib.Path("scraper/espresso.py").read_text(encoding="utf-8")


def test_the_price_is_read_before_the_page_navigates_away():
    """Both branches must use the captured value, not re-read a page that
    no longer shows the price."""
    source = _espresso_source()

    capture = source.index("self.last_payment_status = payment_status")
    categories = source.index("await self._click_categories()")
    assert capture < categories, (
        "the payment panel is now read AFTER the page navigates away")

    for marker in ('"_wlt": True', '"_skipRepriceModal": True'):
        branch = source.index(marker)
        window = source[max(0, branch - 1200):branch]
        assert "last_payment_status" in window, (
            f"the branch at {marker} no longer uses the captured price")
        assert "await self._read_payment_status()" not in window, (
            f"the branch at {marker} re-reads the panel - it will return "
            "None, as it did on all 135 restricted bookings on 2026-10-05")


def test_the_payment_status_is_cleared_for_each_booking():
    """A stale value would hand the previous booking's price to this one -
    and BookingService writes a PERMANENT exclusion off the same
    attribute.

    UPDATED 2026-10-07: the reset moved into
    BaseScraper.reset_per_booking_state, which clears every cached
    attribute for every scraper. NCL and GoCCL never had the hand-written
    version this test originally pinned, and 42 bookings in the live
    database carry another booking's payment panel as a result. The
    behaviour asserted here is unchanged.
    """
    from scraper.base import BaseScraper

    assert "last_payment_status" in BaseScraper.PER_BOOKING_STATE

    source = _espresso_source()
    reset = source.index("self.reset_per_booking_state()")
    capture = source.index("self.last_payment_status = payment_status")
    assert reset < capture, "the reset must come before the capture"
