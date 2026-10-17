"""The booking's own category, read from the Current Selections bar, 2026-10-12.

Neon pasted the bar's markup - "check the category from here":

    <article data-component="booking-details-bar" data-component-name="category"
             data-detail="8D" data-metaname="BALCONY" data-upgrade-to="Upgraded to: ">

It is on the /rate and /category pages (captured live the same day for
DEMO40 and DEMO43, data-detail="8A"), and when Carnival has upgraded the
cabin it reads e.g. "6A Upgraded to: 6B" (DEMO18's rate screen). The rate
category - data-detail - is the booking's own row for the cheapest-category
rule; the upgrade is reported alongside.
"""

import glob
from pathlib import Path

import pytest

from scraper.goccl import GoCCLScraper

REAL_RATE_PAGES = sorted(glob.glob("data/goccl_live_*/pages/DEMO58__rate_page__*.html"))

_UPGRADED = (
    '<html><body><article class="booking-details-bar__category" '
    'data-component="booking-details-bar" data-component-name="category" '
    'data-detail="6A" data-metaname="OCEAN VIEW" data-upgrade-to="Upgraded to: ">'
    '<h5>Category: <span>OCEAN VIEW</span><span>6A </span>'
    '<span>Upgraded to: 6B</span></h5></article></body></html>')


async def _read(url=None, html=None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        page = await browser.new_page()
        if url:
            await page.goto(url)
        else:
            await page.set_content(html)
        s = GoCCLScraper()
        s._page = page
        try:
            return await s.read_category_bar()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_the_real_rate_page_bar_gives_the_bookings_category():
    if not REAL_RATE_PAGES:
        pytest.skip("live GoCCL capture of 2026-10-12 not on this machine")
    bar = await _read(url=Path(REAL_RATE_PAGES[-1]).resolve().as_uri())
    # Real page: "Category: BALCONY 8A Upgraded to: 8C" - booked at the 8A
    # rate, cabin 7348 upgraded to 8C. The rate category is the own row.
    assert bar == {"category": "8A", "stateroom_type": "BALCONY", "upgraded_to": "8C"}


@pytest.mark.asyncio
async def test_an_upgraded_cabin_reports_the_rate_category_and_the_upgrade():
    bar = await _read(html=_UPGRADED)
    assert bar == {"category": "6A", "stateroom_type": "OCEAN VIEW", "upgraded_to": "6B"}


@pytest.mark.asyncio
async def test_no_bar_is_none_not_a_guess():
    assert await _read(html="<html><body>no bar</body></html>") is None
