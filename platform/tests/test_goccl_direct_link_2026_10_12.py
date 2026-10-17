"""GoCCL bookings open directly at /app/bookingengine/<number>, 2026-10-12.

Neon: "https://www.goccl.com/app/bookingengine/DEMO17 also the link does
not chnage this is the booking number i just keep changing after /".
His DEMO18 recording starts at that address, and the captured DEMO08 page
there (data/goccl_watch/20260918T113403/005_win2_DEMO08.html) carries the
same #booked-root landmark the search path waits for.

Pinned: the right booking opens; a page showing ANOTHER booking number is
refused (never attribute one booking's data to another - BUGS section 2);
a signed-out session falls back to the search page.
"""

import pytest

from scraper.goccl import GoCCLScraper


def _page(shown: str | None) -> str:
    if shown is None:                       # signed out: a login page
        return "<html><body><input type='password'></body></html>"
    return ("<html><body><div id='booked-root'>Booking</div>"
            f"<script src='/data-{shown}.js'></script></body></html>")


async def _open(shown):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
    context = await browser.new_context()
    requested = []

    async def serve(route):
        url = route.request.url
        if "/data-" in url:
            ref = url.rsplit("/data-", 1)[1].split(".js")[0]
            await route.fulfill(status=200, content_type="text/javascript",
                                body="window.initialData = {bookingNumber: '" + ref + "'};")
            return
        requested.append(url)
        await route.fulfill(status=200, content_type="text/html", body=_page(shown))

    await context.route("https://www.goccl.com/**", serve)
    s = GoCCLScraper()
    s._page = await context.new_page()
    if shown is None:
        # A login page never shows #booked-root; skip the 20s wait for it.
        real_landed = s._search_landed

        async def quick(timeout=15000):
            return await real_landed(timeout=1500)
        s._search_landed = quick
    try:
        ok = await s._open_booking_directly("cp8d60")
    finally:
        await browser.close()
        await pw.stop()
    return ok, requested


@pytest.mark.asyncio
async def test_the_booking_opens_at_its_own_address():
    ok, requested = await _open("DEMO17")
    assert ok is True
    assert requested[0] == "https://www.goccl.com/app/bookingengine/DEMO17"


@pytest.mark.asyncio
async def test_a_page_showing_another_booking_is_refused():
    ok, _ = await _open("DEMO26")
    assert ok is False


@pytest.mark.asyncio
async def test_a_signed_out_session_falls_back_to_the_search_page():
    ok, _ = await _open(None)
    assert ok is False
