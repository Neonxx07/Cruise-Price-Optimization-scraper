"""Every ESPRESSO booking must be released, or it stays locked 15 minutes.

Neon 2026-09-16: "esspresso has a lock mechanisem we need to exit every
booking after checking or else the booking stay locked for 15 mins so can
we add a function or something to help us exit the boking so we do not
waste time?"

Retrieving a reservation locks it, and nothing ever released that lock -
the scan just navigated on to the next booking. So a 576-booking run left
576 bookings locked behind it, blocking both a human opening the same
booking and any re-scan.

EVERY SELECTOR IS FROM REAL CAPTURED MARKUP, not guessed
(data/pages/3000065__after_search__20260916T152538944341.html):

    <a id="ignoreReservationLink" class="submit button" href="#">Exit</a>
    <input type="button" id="acceptIgnoreReservation" ...>
    <input type="hidden" name="_eventId" value="linkToIgnoreReservation">

plus the page's own handler, which is where the group-booking branch comes
from:

    if (isGroupBooking) { ... 'linkToCompleteIgnoreReservationGb' }
    else                { ... 'linkToIgnoreReservation' }
"""
import inspect

import pytest

from scraper.espresso import EspressoScraper


def _src() -> str:
    return inspect.getsource(EspressoScraper.release_booking)


# -- it exists and is actually called --------------------------------


def test_the_scraper_can_release_a_booking():
    assert hasattr(EspressoScraper, "release_booking")


def test_check_booking_releases_on_the_success_path():
    """Placed after every branch - WLT, paid-in-full, skip-reprice,
    no-price-change, the calculated result and the upgrade override - so
    no successful outcome can leave a booking retrieved."""
    src = inspect.getsource(EspressoScraper.check_booking)
    assert "await self.release_booking(booking_id)" in src
    assert src.index("await self.release_booking(booking_id)") < src.rindex("return result")


def test_the_batch_releases_on_the_error_path_too():
    """A booking that FAILED is still retrieved and still locked. ESPRESSO
    failures are not rare - 141 timeouts historically - so skipping this
    would leave exactly the bookings someone wants to inspect by hand
    locked out for 15 minutes."""
    import services.booking_service as svc

    src = inspect.getsource(svc)
    assert 'hasattr(scraper, "release_booking")' in src
    err = src.index('logger.error("batch.error"')
    rel = src.index("await scraper.release_booking(booking_id)")
    assert err < rel, "the release must sit on the error path"


# -- the real controls, not invented ones ----------------------------


def test_it_uses_the_captured_exit_control():
    src = _src()
    assert "#ignoreReservationLink" in src
    assert "#acceptIgnoreReservation" in src


def test_group_bookings_use_their_own_event():
    """The page's own handler branches on isGroupBooking; using the
    non-group event for a group booking would not release it."""
    src = _src()
    assert "linkToCompleteIgnoreReservationGb" in src
    assert "linkToIgnoreReservation" in src
    assert "isGroupBooking" in src


def test_it_never_touches_cancel_reservation():
    """SAFETY. "Cancel Reservation" is a destructive control on the SAME
    page. Exit/ignore only discards unsaved changes and unlocks - the
    portal's own words: "any changes you made since you retrieved this
    reservation will not be saved". This scan never intends to save
    anything, so discarding is correct; cancelling a client's booking
    would be catastrophic."""
    import ast

    # EXECUTABLE code only. The docstring above deliberately explains what
    # is being avoided and why, so a raw text search matches this file's
    # own prose rather than any real behaviour.
    fn = ast.parse(_src().lstrip()).body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body

    strings, attrs = [], []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.append(node.value.lower())
            elif isinstance(node, ast.Attribute):
                attrs.append(node.attr.lower())

    for destructive in ("cancelreservation", "cancel reservation",
                        "_eventid=cancel", "cancelbooking"):
        for s in strings:
            assert destructive not in s, (
                f"{destructive!r} appears in executed code: {s[:80]!r}"
            )
        assert not any(destructive in a for a in attrs)


# -- it must never break a run ---------------------------------------


@pytest.mark.asyncio
async def test_a_failed_release_returns_false_rather_than_raising():
    """A lock that fails to release costs 15 minutes. A release that
    RAISES would turn a perfectly good result into an error - much worse.
    """
    scraper = EspressoScraper.__new__(EspressoScraper)   # no .page at all
    assert await scraper.release_booking("123") is False


def test_the_contract_is_documented_as_never_raising():
    src = _src()
    assert "NEVER" in src and "raises" in src
    assert "try:" in src and "except Exception" in src


def test_it_returns_a_bool_not_none():
    """The caller logs the outcome; None would make a failure look like a
    success in a truthiness check."""
    sig = inspect.signature(EspressoScraper.release_booking)
    assert sig.return_annotation == "bool"


def test_a_page_with_no_exit_control_is_not_an_error():
    """Not every page reached during a scan holds a retrieved reservation -
    a search that found nothing, for instance. That is a skip, not a
    failure."""
    src = _src()
    assert "booking_release_skipped" in src
