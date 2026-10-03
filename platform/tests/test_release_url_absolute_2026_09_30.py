"""The booking release must navigate to an ABSOLUTE URL.

FOUND 2026-09-30, by reading data/cruiseintel.log rather than the code::

    espresso.booking_release_failed                        22
      x21  Page.goto: Protocol error (Page.navigate): Cannot navigate
           to invalid URL
           Call log:
             - navigating to "/espres...

`window.Base.flowExecutionURL` is ROOT-RELATIVE - the portal's own links
read `/espresso/protected/reservations.do?execution=e3s1` - and
`page.goto()` rejects anything that is not absolute. So `release_booking`'s
second path, the fallback taken whenever the Exit link is not on the page,
threw on EVERY attempt. Those bookings stayed locked for the full 15
minutes. Last occurrence 2026-09-30 18:43, still live when found.

This is the V-VIP "always release the booking" rule's last mile. The
2026-09-23 `finally` fix was doing its job - release was being CALLED every
time, which is exactly why these 21 failures are visible at all. It was the
release itself that could not complete.

The first test runs the real JS in a real browser. Chromium headless is
fine here: the headless ban is about ESPRESSO's CDN refusing to serve a
headless client, not about executing JavaScript, and this page is local.
"""

import ast
import pathlib

import pytest

ESPRESSO = pathlib.Path("scraper/espresso.py")

# The portal's real shape, from Neon's 2026-09-30 recording:
#   <a href="/espresso/protected/reservations.do?execution=e1s4&_eventId=...">
RELATIVE_FLOW_URL = "/espresso/protected/reservations.do?execution=e3s1"
PAGE_URL = "https://secure.cruisingpower.com/espresso/protected/dashboard.do"


def _release_js() -> str:
    """The exact JS `release_booking` hands to page.evaluate().

    Pulled from the AST, so a comment mentioning these strings can never
    satisfy the test - this project has been bitten by prose-matching
    tests six times.
    """
    tree = ast.parse(ESPRESSO.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "release_booking":
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "evaluate"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and "flowExecutionURL" in str(call.args[0].value)):
                return call.args[0].value
    pytest.fail("release_booking no longer evaluates a flowExecutionURL script")


# -- the real thing, in a real browser ------------------------------------


@pytest.mark.asyncio
async def test_the_release_script_returns_an_absolute_url():
    """Run the shipped JS against a relative flowExecutionURL and require a
    URL that page.goto() would actually accept."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("playwright not installed")

    script = _release_js()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<html><body></body></html>"))
            await page.goto(PAGE_URL)
            await page.evaluate(
                "u => { window.Base = { flowExecutionURL: u }; }",
                RELATIVE_FLOW_URL)

            result = await page.evaluate(script)

            assert result, "the script returned nothing"
            assert result.startswith("https://"), (
                f"relative URL survived - page.goto() would reject it: {result}")
            assert "secure.cruisingpower.com" in result
            assert "_eventId=linkToIgnoreReservation" in result

            # And prove page.goto() accepts it, which is the whole point.
            await page.goto(result)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_a_group_booking_releases_with_its_own_event():
    """Group bookings use a different event, taken from the portal's own
    handler. The absolute-URL fix must not have disturbed that."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("playwright not installed")

    script = _release_js()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<html><body></body></html>"))
            await page.goto(PAGE_URL)
            await page.evaluate(
                "u => { window.Base = { flowExecutionURL: u };"
                "       window.isGroupBooking = true; }",
                RELATIVE_FLOW_URL)

            result = await page.evaluate(script)

            assert result.startswith("https://")
            assert "_eventId=linkToCompleteIgnoreReservationGb" in result
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_no_flow_url_returns_nothing_rather_than_a_bad_url():
    """A page without window.Base must yield None, so release_booking falls
    through to "no exit control" instead of navigating somewhere wrong."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover
        pytest.skip("playwright not installed")

    script = _release_js()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body="<html><body></body></html>"))
            await page.goto(PAGE_URL)
            assert await page.evaluate(script) is None
        finally:
            await browser.close()


# -- a guard that runs even with no browser ------------------------------


def test_the_script_resolves_against_the_document_base():
    """Structural, from the AST - so it still guards the fix on a machine
    with no Chromium, and cannot be satisfied by a comment."""
    script = _release_js()
    assert "new URL(" in script, "the relative URL is no longer being resolved"
    assert "document.baseURI" in script or "location.href" in script


def test_an_absolute_url_is_what_playwright_requires():
    """Documents WHY, with no browser needed: this is the exact input that
    produced 21 failures, and what it has to become."""
    from urllib.parse import urljoin

    assert not RELATIVE_FLOW_URL.startswith("http"), (
        "the portal's flowExecutionURL is relative - that is the bug")
    resolved = urljoin(PAGE_URL, RELATIVE_FLOW_URL)
    assert resolved == (
        "https://secure.cruisingpower.com/espresso/protected/"
        "reservations.do?execution=e3s1")
