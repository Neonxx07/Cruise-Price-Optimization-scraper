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
    src = inspect.getsource(EspressoScraper.check_booking)
    assert "self.last_feature_fields = await self.read_feature_fields()" in src


def test_the_capture_happens_before_the_booking_is_released():
    """release_booking() navigates away; reading after it is the bug."""
    src = inspect.getsource(EspressoScraper.check_booking)
    assert src.index("last_feature_fields") < src.index("release_booking")


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
    and sail date to this one - worse than a NULL, because it looks real."""
    src = inspect.getsource(EspressoScraper.check_booking)
    assert "self.last_feature_fields = None" in src
    assert src.index("self.last_feature_fields = None") < src.index(
        "self.last_feature_fields = await self.read_feature_fields()")


def test_a_failed_capture_does_not_break_the_booking():
    """A feature is a nice-to-have; a booking result is not."""
    src = inspect.getsource(EspressoScraper.check_booking)
    idx = src.index("last_feature_fields = await")
    assert "except Exception" in src[idx:idx + 400]


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
