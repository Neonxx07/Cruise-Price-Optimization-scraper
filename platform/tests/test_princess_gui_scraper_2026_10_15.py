"""Princess in the GUI, 2026-10-15.

Neon: "we need to finish it now so we start scanning with it tomorrow and
implement in the gui". PrincessScraper wraps the read-only PolarSession walk
proven on 33 of his bookings that day; these tests pin (1) how each outcome
of that walk becomes a BookingResult, (2) that "logged in" means a POLAR
Online tab exists, and (3) that the line is registered everywhere a line is.
"""
import asyncio

from core.models import BookingStatus, CruiseLine
from scraper.princess import PrincessCheck, PrincessScraper, check_to_result


def _landing(**kw):
    base = {"category": "BC", "currency": "CAD",
            "status": {"cancelled": False, "waitlisted": False, "code": "BK"}}
    base.update(kw)
    return base


def test_a_priced_saving_is_an_optimization_with_polar_figures():
    c = PrincessCheck(ref="DEMO24", outcome="PRICED", landing=_landing(), tier="STANDARD",
                      verdict={"status": "OPTIMIZATION", "saving": 300.0,
                               "note": "saving 300.00 - KKS ...; check the payment dates on the "
                                       "screen before Save (POLAR's quote shows none)",
                               "booking_fare_plus_packages": 7110.0,
                               "quote_fare_plus_packages": 6810.0,
                               "quote_codes": ["KKS", "KKS"], "source": "Best Buy"})
    r = check_to_result(c)
    assert r.cruise_line == CruiseLine.PRINCESS and r.status == BookingStatus.OPTIMIZATION
    assert (r.old_total, r.new_total, r.price_drop, r.net_saving) == (7110.0, 6810.0, 300.0, 300.0)
    assert r.new_price_category == "KKS" and r.price_category == "BC" and r.currency == "CAD"
    assert r.confidence == 5
    assert "unconfirmed" not in r.note.lower()           # would tint the row yellow


def test_no_saving_never_carries_a_saving_or_a_new_price():
    c = PrincessCheck(ref="DEMO24", outcome="PRICED", landing=_landing(),
                      verdict={"status": "NO_SAVING", "saving": 0.0, "note": "no saving - x",
                               "booking_fare_plus_packages": 7514.0,
                               "quote_fare_plus_packages": 8474.0})
    r = check_to_result(c)
    assert r.status == BookingStatus.NO_SAVING
    assert r.net_saving == 0.0 and r.new_total == r.old_total == 7514.0


def test_paid_in_full_and_cancelled_are_their_own_statuses():
    """Hard rule 7: both are recorded, reported and never re-scanned."""
    paid = check_to_result(PrincessCheck(ref="A", outcome="NOT_PRICED", why="paid in full",
                                         landing=_landing()))
    assert paid.status == BookingStatus.PAID_IN_FULL
    cx = check_to_result(PrincessCheck(
        ref="B", outcome="NOT_PRICED", why="status CX",
        landing=_landing(status={"cancelled": True, "waitlisted": False, "code": "CX"})))
    assert cx.status == BookingStatus.CANCELLED


def test_group_waitlisted_and_unpriceable_bookings_are_no_saving_with_the_reason():
    for outcome, why in (("NOT_PRICED", "group booking G123 - out of scope for now (Neon)"),
                         ("NOT_PRICED", "past final payment (14OCT26)"),
                         ("NOT_PRICEABLE", "POLAR listed no fares for this sailing")):
        r = check_to_result(PrincessCheck(ref="X", outcome=outcome, why=why, landing=_landing()))
        assert r.status == BookingStatus.NO_SAVING and why in r.note


def test_a_safe_stop_is_an_error_that_says_nothing_was_pressed():
    r = check_to_result(PrincessCheck(ref="X", outcome="STOPPED", why="unknown pop-up"))
    assert r.status == BookingStatus.ERROR and "nothing pressed" in r.error


# -- "logged in" = a POLAR Online tab -------------------------------------------

class _Page:
    def __init__(self, title, ctx):
        self._title, self.context = title, ctx

    async def title(self):
        return self._title


class _Ctx:
    def __init__(self):
        self.pages = []


def _scraper_on(*titles):
    ctx = _Ctx()
    ctx.pages = [_Page(t, ctx) for t in titles]
    s = PrincessScraper()
    s._page = ctx.pages[0]
    return s, ctx


def test_onesource_alone_is_not_logged_in():
    s, _ = _scraper_on("OneSource - Home")
    assert asyncio.run(s._check_login()) is False


def test_a_polar_tab_is_logged_in_and_the_scraper_moves_onto_it():
    s, ctx = _scraper_on("OneSource - Home", "POLAR Online - MAIN MENU")
    assert asyncio.run(s._check_login()) is True
    assert s.page is ctx.pages[1]


def test_not_logged_in_raises_a_login_shaped_error_before_touching_polar():
    s, _ = _scraper_on("OneSource - Home")
    try:
        asyncio.run(s.check_booking("DEMO12"))
    except RuntimeError as exc:
        assert "Not logged in" in str(exc)
    else:
        raise AssertionError("expected a login error")


# -- registered like every other line -------------------------------------------

def test_the_line_is_wired_into_the_service_and_the_cli():
    import main
    from config.settings import settings
    from services.booking_service import BookingService

    assert isinstance(BookingService()._get_scraper(CruiseLine.PRINCESS), PrincessScraper)
    assert BookingService._login_base_url(CruiseLine.PRINCESS) == settings.princess_login_url
    assert isinstance(main._scraper_for(CruiseLine.PRINCESS), PrincessScraper)
    assert settings.freshness_for("PRINCESS") == 24


def test_princess_is_offered_headless_now_that_it_is_proven():
    """2026-10-15: DEMO12, DEMO24, DEMO29 windowed vs headless, automatic
    sign-in both ways - identical results, 0 differences."""
    from gui.windows import HEADLESS_CAPABLE_LINES
    assert CruiseLine.PRINCESS in HEADLESS_CAPABLE_LINES
