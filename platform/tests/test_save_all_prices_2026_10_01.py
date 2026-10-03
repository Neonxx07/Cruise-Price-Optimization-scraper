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


def test_both_sentinel_paths_read_the_payment_panel():
    """Structural, from the AST: the builders can only record a price if
    the scraper reads one. Taken from the tree so a comment mentioning
    these calls cannot satisfy it - this project has been bitten by
    prose-matching tests six times.
    """
    import ast
    import pathlib

    source = pathlib.Path("scraper/espresso.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    reads: list[int] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Await)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "_read_payment_status"):
            reads.append(node.lineno)

    # One for paid-in-full (pre-existing), one for skipRepriceModal, one
    # for WLT. Fewer means a sentinel path stopped recording prices.
    assert len(reads) >= 3, (
        f"only {len(reads)} _read_payment_status call(s) - a sentinel path "
        "is recording no price again")


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
