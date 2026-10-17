"""The sail date was read AFTER the page had already navigated away.

Neon 2026-09-22: "make sure that ... the data base is collected arranged
data successfully?"

It was not, and the verification is what caught it. 11% of the day's
ESPRESSO rows had no sail_date, and the captured pages proved the data had
been there all along:

    booking 3001000  page says 02JAN2028   log recorded sail_date=None
    booking 3000073  page says 27MAR2027   log recorded sail_date=None
    booking 3001002  page says 05JUN2028   log recorded sail_date=None

CAUSE: read_feature_fields() was called from the batch loop AFTER
check_booking returned - and check_booking ends with release_booking(),
which clicks Exit and navigates away. The read landed on whatever page came
next. The 89% that worked were the ones that happened to still be on a
usable page.

This is the quiet kind of failure: a missing feature is a NULL column, not
an error, so nothing complained.
"""
import inspect

from scraper.base import BaseScraper
from scraper.espresso import EspressoScraper
from services.booking_service import BookingService


def test_features_are_captured_inside_check_booking():
    """On the booking page, while it is still on screen."""
    src = inspect.getsource(EspressoScraper._check_booking_inner)
    assert "self.last_feature_fields = await self.read_feature_fields()" in src


def _code_lines(func):
    """(lineno, text) for real CODE only - comments stripped.

    REWRITTEN 2026-10-07. This test used to compare raw character offsets
    into the source:

        src.index("last_feature_fields") < src.index("release_booking")

    which passed for the wrong reason. The first textual occurrence of
    `last_feature_fields` was the `= None` RESET near the top of the
    method, not the capture, and `release_booking` first appears in a
    COMMENT. When the reset moved into BaseScraper.reset_per_booking_state
    the comparison inverted, while the real capture (line 142) and the
    real release (line 450) had not moved at all.
    """
    import textwrap
    src = textwrap.dedent(inspect.getsource(func))
    return [(i, line.split("#", 1)[0])
            for i, line in enumerate(src.splitlines(), 1)]


def _first_line_containing(func, needle: str) -> int:
    for lineno, text in _code_lines(func):
        if needle in text:
            return lineno
    raise AssertionError(f"{needle!r} does not appear in {func.__name__}")


def test_the_capture_happens_before_the_booking_is_released():
    """release_booking() navigates away; reading after it is the bug."""
    capture = _first_line_containing(
        EspressoScraper._check_booking_inner,
        "self.last_feature_fields = await self.read_feature_fields()")
    release = _first_line_containing(
        EspressoScraper._check_booking_inner, "await self.release_booking(")
    assert capture < release, (
        f"the capture is at line {capture} and the release at {release} - "
        "reading after release lands on the wrong page and silently "
        "produces NULLs")


def test_the_batch_no_longer_reads_the_page_itself():
    src = inspect.getsource(BookingService._run_batch)
    assert "await reader()" not in src
    assert "last_feature_fields" in src


def test_every_scraper_has_the_attribute():
    """Declared on the base so BookingService never has to guess whether it
    exists - a line that captures nothing simply leaves it empty."""
    assert hasattr(BaseScraper, "last_feature_fields")
    assert BaseScraper.last_feature_fields is None


def test_it_is_cleared_per_booking():
    """A stale value would silently attribute the PREVIOUS booking's ship
    and sail date to this one - worse than a NULL, because it looks real.

    UPDATED 2026-10-07: the clearing moved from a hand-written
    `self.last_feature_fields = None` into
    BaseScraper.reset_per_booking_state, which does the same for every
    scraper and every cached attribute - NCL and GoCCL never had the
    hand-written version, and 42 bookings in the live database carry
    another booking's data because of it. The behaviour asserted here is
    unchanged; only where it is implemented moved.
    """
    assert "last_feature_fields" in BaseScraper.PER_BOOKING_STATE

    reset = _first_line_containing(
        EspressoScraper._check_booking_inner, "self.reset_per_booking_state()")
    capture = _first_line_containing(
        EspressoScraper._check_booking_inner,
        "self.last_feature_fields = await self.read_feature_fields()")
    assert reset < capture, "the clear happens after the capture"


def test_a_failed_capture_does_not_break_the_booking():
    """A feature is a nice-to-have; a booking result is not.

    STRUCTURAL, not a character window. The original version asserted that
    "except Exception" appeared within 400 characters of the assignment,
    which broke on 2026-09-28 the moment an explanatory comment was added
    between them - the handler was still there and still correct. Distance
    in a source file is not a property worth testing; being inside a try
    block is.
    """
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(EspressoScraper._check_booking_inner)))
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and "last_feature_fields" in ast.dump(ast.Module(body=node.body,
                                                         type_ignores=[]))
        and node.handlers
    ]
    assert guarded, "the feature capture is not inside a try/except"


def test_the_extractor_reads_the_real_espresso_shape():
    """The values that were being lost, in the format the portal uses."""
    from datetime import date

    from core.booking_features import extract

    f = extract("ESPRESSO", observed=date(2026, 9, 22), page_fields={
        "sailDate": "02JAN2028", "shipCode": "AL",
        "shipName": "Allure Of The Seas", "currency": "USD"})
    assert f.sail_date == date(2028, 1, 2)
    assert f.days_to_sailing == 467
    assert f.ship_name == "Allure Of The Seas"
