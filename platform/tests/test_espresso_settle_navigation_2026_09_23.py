"""Let /home finish before navigating away, so the next hop is not aborted.

Neon 2026-09-23: *"I AM LOGGED OUT I WILL TRY TO LLOG IN AGAIN CAN YOU FIX
THIS FUCKING ANANNOYING BUG FOREVER IT MAKES THINGS SLOWER"*.

Measured on the live run he was watching:

    navigate_retry in the run : 271
    net::ERR_ABORTED          : 268   (every one on reservations.do)

ERR_ABORTED means the navigation was cancelled by another navigation -
/home's own session bootstrap redirect, still in flight when we left.
`navigate()` waits only for `domcontentloaded`, which fires well before
that settles.

The correlation is unambiguous. Time on /home before leaving:

    aborted (270 bookings)  median  668ms
    clean   (374 bookings)  median 1068ms

The bookings that left FASTEST are the ones whose next navigation died, and
`navigate_reservations` then cost 3130ms against 1015ms - a ~2.1s penalty on
42% of bookings.

The same race produced the phantom logouts: `_check_login` sampled /home
mid-bootstrap, found the login form /home briefly renders, and declared a
logout on a healthy session. All three of that day's "logouts" recovered on
their own within seconds while the scan carried on.
"""

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from scraper.espresso import EspressoScraper

ESPRESSO_PY = Path(__file__).resolve().parents[1] / "scraper" / "espresso.py"


class _Page:
    """A page whose URL changes on a schedule, like a redirecting portal."""

    def __init__(self, urls):
        self._urls = list(urls)
        self.reads = 0

    @property
    def url(self):
        self.reads += 1
        # Advance one step per read until the list is exhausted.
        if len(self._urls) > 1:
            return self._urls.pop(0)
        return self._urls[0]


class _Scraper(EspressoScraper):
    def __init__(self, page):
        self._page_obj = page

    @property
    def page(self):
        return self._page_obj


# ── structure ────────────────────────────────────────────────────────────


def test_the_settle_runs_between_loading_home_and_checking_login():
    """Order matters. Settling AFTER the login check would leave the
    phantom-logout half of the bug in place."""
    tree = ast.parse(ESPRESSO_PY.read_text(encoding="utf-8"))
    src = ESPRESSO_PY.read_text(encoding="utf-8")
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "_check_booking_inner")
    seg = ast.get_source_segment(src, inner)
    assert seg.index("espresso_home_url") < seg.index("_settle_navigation")
    assert seg.index("_settle_navigation") < seg.index("_check_login")


def test_it_is_documented_as_never_raising():
    doc = inspect.getdoc(EspressoScraper._settle_navigation) or ""
    assert "Never raises" in doc or "never raises" in doc


# ── behaviour ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stable_url_settles_quickly():
    s = _Scraper(_Page(["https://secure.cruisingpower.com/home"]))
    assert await s._settle_navigation(timeout_ms=3000, quiet_ms=200) is True


@pytest.mark.asyncio
async def test_it_waits_through_a_redirect_chain():
    """The real shape: /home bounces through SSO before landing."""
    page = _Page([
        "https://secure.cruisingpower.com/home",
        "https://secure.cruisingpower.com/home",
        "https://auth.cruisingpower.com/as/authorization",
        "https://secure.cruisingpower.com/home",
        "https://secure.cruisingpower.com/home",
    ])
    s = _Scraper(page)
    assert await s._settle_navigation(timeout_ms=4000, quiet_ms=200) is True


@pytest.mark.asyncio
async def test_a_page_that_never_settles_times_out_without_raising():
    """A portal that long-polls or redirects forever must not hang or break
    the booking - the flow carries on exactly as it did before."""
    class Never:
        def __init__(self):
            self.n = 0

        @property
        def url(self):
            self.n += 1
            return f"https://secure.cruisingpower.com/step{self.n}"

    s = _Scraper(Never())
    assert await s._settle_navigation(timeout_ms=600, quiet_ms=200) is False


@pytest.mark.asyncio
async def test_a_page_that_raises_on_url_is_survived():
    """A closed or crashed page must not turn into an exception here."""
    class Broken:
        @property
        def url(self):
            raise RuntimeError("target page, context or browser has been closed")

    s = _Scraper(Broken())
    assert await s._settle_navigation(timeout_ms=600, quiet_ms=200) is False


@pytest.mark.asyncio
async def test_it_is_bounded_by_its_timeout():
    """It runs once per booking on a 721-booking scan; an unbounded wait
    would cost more than the retry it replaces."""
    class Never:
        def __init__(self):
            self.n = 0

        @property
        def url(self):
            self.n += 1
            return f"https://x/{self.n}"

    s = _Scraper(Never())
    loop = asyncio.get_event_loop()
    start = loop.time()
    await s._settle_navigation(timeout_ms=800, quiet_ms=200)
    elapsed = (loop.time() - start) * 1000
    assert elapsed < 2000, f"took {elapsed:.0f}ms against an 800ms budget"
