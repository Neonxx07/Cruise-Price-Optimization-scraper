"""GUI "Open in portal" button, 2026-10-09.

Neon: "i want to add an option in the gui an open button that when i press
it it opens the booking number in the portal".

The same morning's log shows why it must use the scan's OWN browser:

    15:31:11 espresso.auto_login result=OK          <- the script's login
    15:32:22 Session logged out while searching       (Neon had also
    15:33:39 Session logged out while searching        logged in by hand)

ESPRESSO allows one session per account, so a second login knocks the
first out. The button therefore opens a NEW TAB in the line's existing,
logged-in browser context - never a separate browser - using each line's
existing search code (no new selectors). These tests pin:

  * the booking really opens in a new tab and the scan's page is restored;
  * a headless browser is refused (there is no window to show);
  * a failure still restores the scan's page;
  * keep-alive never navigates the person's tab;
  * the scan never adopts the person's tab as its own;
  * the queue refuses while a scan runs, and the GUI picks the right number.
"""

import asyncio
import os

import pytest

from core.models import CruiseLine
from scraper.espresso import EspressoScraper

HOME = "https://secure.cruisingpower.com/home"
APP = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
LOGIN = "https://secure.cruisingpower.com/login"

# A reservations page with the real search box and button selectors; the
# button reveals #sideBar, which is what _search_booking waits for.
_SEARCH_HTML = (
    "<html><body>"
    "<input data-qa='secure.espresso.input.reservation.search'>"
    "<button aria-label='Search by Reservation ID, Name or Date' "
    "onclick='showBooking()'>Search</button>"
    "<script src='/show.js'></script>"
    "</body></html>"
)
_SHOW_JS = (
    "function showBooking() {"
    "  var d = document.createElement('div'); d.id = 'sideBar';"
    "  d.textContent = 'Reservation';"
    "  document.body.appendChild(d); }"
)
_LOGIN_HTML = "<html><body><input type='text'><input type='password'></body></html>"
_HOME_HTML = "<html><body>Home</body></html>"


async def _browser():
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

    async def serve(route):
        url = route.request.url
        if url.endswith("/show.js"):
            await route.fulfill(status=200, content_type="text/javascript", body=_SHOW_JS)
            return
        if "/login" in url:
            body = _LOGIN_HTML
        elif "reservations.do" in url:
            body = _SEARCH_HTML
        else:
            body = _HOME_HTML
        await route.fulfill(status=200, content_type="text/html", body=body)

    await context.route("https://*.cruisingpower.com/**", serve)
    return pw, browser, context


def _scraper(scan_page) -> EspressoScraper:
    s = EspressoScraper()
    s._page = scan_page
    s.launched_headless = False
    return s


@pytest.mark.asyncio
async def test_the_booking_opens_in_a_new_tab_and_the_scan_page_is_restored():
    pw, browser, context = await _browser()
    try:
        scan_page = await context.new_page()
        await scan_page.goto(HOME)
        s = _scraper(scan_page)
        tab = await s.open_booking_in_new_tab("3001024")
        assert tab is not scan_page
        assert await tab.locator("#sideBar").count() == 1
        assert s.page is scan_page                       # scan keeps its tab
        assert scan_page.url == HOME                      # and was not moved
        assert tab in s._viewing_tabs
        assert s.viewing_in_progress is False
        assert len(context.pages) == 2                    # same browser, one new tab
        assert s.action_log[-1]["action"] == "booking_opened_for_viewing"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_a_headless_browser_is_refused_without_opening_a_tab():
    pw, browser, context = await _browser()
    try:
        scan_page = await context.new_page()
        s = _scraper(scan_page)
        s.launched_headless = True
        with pytest.raises(RuntimeError, match="Headless"):
            await s.open_booking_in_new_tab("3001024")
        assert len(context.pages) == 1
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_a_failed_open_still_restores_the_scan_page():
    pw, browser, context = await _browser()
    try:
        scan_page = await context.new_page()
        s = _scraper(scan_page)

        async def broken(_booking_id):
            raise RuntimeError("portal said no")
        s._open_booking_for_viewing = broken
        with pytest.raises(RuntimeError, match="portal said no"):
            await s.open_booking_in_new_tab("3001024")
        assert s.page is scan_page
        assert s.viewing_in_progress is False
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_the_scan_never_adopts_the_persons_tab():
    """_adopt_authenticated_tab takes over any logged-in tab when the scan's
    own tab is on a login page. A person's Open-in-portal tab must not be
    one of them."""
    pw, browser, context = await _browser()
    try:
        scan_page = await context.new_page()
        await scan_page.goto(LOGIN)
        persons_tab = await context.new_page()
        await persons_tab.goto(APP)
        s = _scraper(scan_page)
        s._viewing_tabs.add(persons_tab)
        assert await s._adopt_authenticated_tab() is False
        assert s.page is scan_page
        s._viewing_tabs.clear()                           # control: it would be adopted
        assert await s._adopt_authenticated_tab() is True
    finally:
        await browser.close()
        await pw.stop()


def test_keepalive_never_navigates_while_a_booking_is_being_opened():
    s = EspressoScraper()
    s._last_navigation_at = 0.0                           # long idle: would refresh
    s.viewing_in_progress = True
    assert asyncio.run(s.keep_session_alive()) is False   # never touched the page


def test_the_queue_refuses_while_a_scan_runs():
    from gui.queue_manager import BookingQueueManager
    qm = BookingQueueManager()
    qm._running = True
    with pytest.raises(RuntimeError, match="scan is running"):
        asyncio.run(qm.open_booking_in_portal(CruiseLine.ESPRESSO, "3001024"))


# -- the GUI -------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def panel():
    pytest.importorskip("PySide6", reason="GUI tests need PySide6")
    pytest.importorskip("qasync", reason="GUI tests need qasync")
    from PySide6.QtWidgets import QApplication
    from gui.windows import CruiseLinePanel
    app = QApplication.instance() or QApplication([])
    made = []

    def build(line):
        p = CruiseLinePanel(line)
        made.append(p)
        return p
    yield build
    for p in made:
        p.setParent(None)
        p.deleteLater()
    app.processEvents()
    app.sendPostedEvents(None, 0)


def test_the_selected_row_wins_over_the_booking_box(panel):
    from PySide6.QtWidgets import QTableWidgetItem
    p = panel(CruiseLine.ESPRESSO)
    p.booking_input.setText("9999999")
    assert p._booking_to_open() == "9999999"               # nothing selected
    p.results_table.setSortingEnabled(False)
    p.results_table.insertRow(0)
    p.results_table.setItem(0, 0, QTableWidgetItem("3001024"))
    p.results_table.selectRow(0)
    assert p._booking_to_open() == "3001024"


def test_the_button_exists_and_msc_is_not_wired(panel):
    assert panel(CruiseLine.ESPRESSO).open_portal_button.isEnabled()
    assert not panel(CruiseLine.MSC).open_portal_button.isEnabled()


def test_an_open_in_progress_counts_as_busy(panel):
    """The keep-alive timer skips busy panels."""
    p = panel(CruiseLine.ESPRESSO)
    assert not p.is_busy()
    p._open_in_progress = True
    assert p.is_busy()
