"""The /review walk, against a fake Carnival wizard, 2026-10-12.

The selectors and the click order are the ones walked live on 2026-09-18
(data/goccl_safe_review/, goccl_safe_review.py) and recorded by Neon on
DEMO18 on 2026-10-12: candidate tile button -> Continue -> a category row
-> Keep Same Stateroom -> the review-changes request. The fake pages
reproduce those controls at real goccl.com addresses; the review request
answers with DEMO11's real invoice.

THE CATEGORY RULE (Neon 2026-10-12): "i always in category choose the
cheapest but you must always make sure that keep same state room button is
eenabled ... not lose anything on the booking". On DEMO18 he chose 6A at
$574 for cabin R162 (category 6B, $579) and Carnival kept the cabin. So:

  * rows are tried cheapest first, never above the booking's own row;
  * a row is used only once Keep Same Stateroom enables for it;
  * the cabin on /review must be the booking's own - else STATEROOM_CHANGED;
  * "Confirm Changes" is never clicked.
"""

from pathlib import Path

import pytest

from scraper.goccl import GoCCLScraper

BASE = "https://www.goccl.com/app/bookingengine/DEMO11"
DEMO11_REVIEW = (Path(__file__).parent / "fixtures" / "goccl"
                 / "demo11_review_changes_summary_2026_09_18.json").read_text(encoding="utf-8")
CABIN = "R162"

# A SINGLE-PAGE APP, LIKE THE REAL ONE. Carnival's wizard never reloads
# between /rate, /category and /review: window.initialData was identical on
# all five steps of the live 2026-09-18 capture, which is only possible in
# one document. go() changes the address with pushState and swaps the
# content in place; window.fullReload switches to real page loads, to prove
# that case fails safe instead of hanging. choose(cat) enables Keep Same
# Stateroom only for the categories listed in #app's data-enable.
_WIZARD_JS = """
window.confirmClicks = 0;
function go(path) {
  var url = '/app/bookingengine/DEMO11/' + path;
  if (window.fullReload) { location.href = url; return; }
  fetch(url).then(function (r) { return r.text(); }).then(function (html) {
    history.pushState({}, '', url);
    var doc = new DOMParser().parseFromString(html, 'text/html');
    document.body.innerHTML = doc.body.innerHTML;
  });
}
function choose(cat) {
  var allowed = (document.getElementById('app').dataset.enable || '').split(',');
  var keep = document.querySelector('button[data-comp="continue-to-review"]');
  keep.disabled = allowed.indexOf(cat) < 0;
  window.lastChosen = cat;
}
function keepSame() {
  fetch('/app/bookingengine/api/v1.0/manage/demo11/review-changes')
    .then(function () { go('review'); });
}
function discardAll() {
  var box = document.createElement('div');
  box.setAttribute('role', 'dialog');
  box.textContent = 'Are you sure? ';
  var yes = document.createElement('button');
  yes.textContent = 'Yes, Discard Changes';
  yes.onclick = function () { window.discarded = true; box.remove(); };
  box.appendChild(yes);
  document.body.appendChild(box);
}
"""

_RATE = """<html><body>
<div class="rate-code-tile" data-rate-code="PHY" data-rate-name="SAVE SALE">
  <button class="rate-code-tile__price-button" data-rate-meta-name="BALCONY"
          data-rate-meta-price="514">BALCONY</button>
</div>
<button data-comp="cancel">Discard Changes</button>
<button data-comp="goto-next-page" onclick="go('category')">Continue</button>
<script src="/wizard.js"></script></body></html>"""


def _category(rows=(("8C", 514),), enable=("8C",)) -> str:
    """rows: (category, price). enable: categories for which Carnival
    enables Keep Same Stateroom."""
    body = "".join(
        f'<tr data-cat="{cat}" data-cat-price="{price}"><td>{cat}</td>'
        f'<td><button onclick="choose(\'{cat}\')">Select ${price}</button></td></tr>'
        for cat, price in rows)
    return (f'<html><body><div id="app" data-enable="{",".join(enable)}"></div>'
            f'<table>{body}</table>'
            '<button data-comp="cancel">Discard Changes</button>'
            '<button data-comp="continue-to-review" disabled onclick="keepSame()">'
            'Keep Same Stateroom</button>'
            '<script src="/wizard.js"></script></body></html>')


def _review(room=CABIN) -> str:
    label = ('<span data-component="offer-details-bar-stateroom-label">'
             f'Stateroom #{room}</span>' if room else '')
    return ('<html><body>' + label +
            '<button data-component="confirm-booking-cta" onclick="discardAll()">'
            'Discard All Changes</button>'
            '<button data-component="confirm-booking-cta" '
            'onclick="window.confirmClicks++">Confirm Changes</button>'
            '<script src="/wizard.js"></script></body></html>')


# --- harness and tests below ---

async def _wizard(category_html=None, review_html=None, review_status=200):
    """A browser on the fake /rate page. Returns (pw, browser, scraper, calls)."""
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
    calls = {"review_api": 0}

    async def serve(route):
        url = route.request.url
        if url.endswith("/wizard.js"):
            await route.fulfill(status=200, content_type="text/javascript", body=_WIZARD_JS)
        elif "review-changes" in url:
            calls["review_api"] += 1
            await route.fulfill(status=review_status, content_type="application/json",
                                body=DEMO11_REVIEW if review_status < 400
                                else '{"code":"999999","details":[{"code":"5108"}]}')
        elif url.endswith("/category"):
            await route.fulfill(status=200, content_type="text/html",
                                body=category_html or _category())
        elif url.endswith("/review"):
            await route.fulfill(status=200, content_type="text/html",
                                body=review_html or _review())
        else:
            await route.fulfill(status=200, content_type="text/html", body=_RATE)

    await context.route("https://www.goccl.com/**", serve)
    page = await context.new_page()
    await page.goto(f"{BASE}/rate")
    scraper = GoCCLScraper()
    scraper._page = page
    scraper.last_initial_data = {"stateroom": {"code": CABIN}}
    scraper._KEEP_SAME_WAIT_S = 2.0
    return pw, browser, scraper, calls


async def _close(pw, browser):
    await browser.close()
    await pw.stop()


async def _walk(category_html=None, review_html=None, review_status=200,
                offer="PHY", own="8C", full_reload=False):
    pw, browser, s, calls = await _wizard(category_html, review_html, review_status)
    try:
        if full_reload:
            await s.page.evaluate("window.fullReload = true")
        path, review = await s.confirm_candidate_on_review(offer, "BALCONY", own)
        extra = {"confirm_clicks": await s.page.evaluate("window.confirmClicks || 0"),
                 "url": s.page.url, "choice": s.last_review_choice}
    finally:
        await _close(pw, browser)
    return path, review, calls, extra


@pytest.mark.asyncio
async def test_the_walk_reaches_review_and_returns_carnivals_own_figures():
    path, review, calls, extra = await _walk()
    assert path == "REVIEW_REACHED"
    assert review.gross == 1552
    assert review.reconciles() == (True, "")
    assert calls["review_api"] == 1
    assert extra["url"].endswith("/review")
    assert extra["confirm_clicks"] == 0                      # never commits
    assert extra["choice"]["stateroom_on_review"] == CABIN


@pytest.mark.asyncio
async def test_demo50_the_cheapest_row_that_keeps_the_cabin_is_used():
    """Neon's DEMO18: own 6B $579, 6A $574 kept the cabin -> 6A."""
    page = _category(rows=(("6D", 594), ("6B", 579), ("6A", 574)), enable=("6A", "6B"))
    path, _review_inv, _calls, extra = await _walk(category_html=page, own="6B")
    assert path == "REVIEW_REACHED"
    assert extra["choice"]["category"] == "6A"
    assert extra["choice"]["row_price"] == 574


@pytest.mark.asyncio
async def test_a_cheaper_row_that_would_move_the_cabin_is_skipped():
    page = _category(rows=(("8A", 500), ("8B", 507), ("8C", 514)), enable=("8B", "8C"))
    path, _r, _c, extra = await _walk(category_html=page)
    assert path == "REVIEW_REACHED"
    assert extra["choice"]["category"] == "8B"


@pytest.mark.asyncio
async def test_rows_dearer_than_the_bookings_own_are_never_tried():
    page = _category(rows=(("8C", 514), ("8D", 520)), enable=("8D",))
    path, review, calls, _e = await _walk(category_html=page)
    assert (path, review) == ("REVIEW_PATH_BLOCKED", None)
    assert calls["review_api"] == 0                          # no review forced


@pytest.mark.asyncio
async def test_keep_same_stateroom_never_enabling_stops_the_walk():
    page = _category(rows=(("8B", 507), ("8C", 514)), enable=())
    path, review, calls, _e = await _walk(category_html=page)
    assert (path, review) == ("REVIEW_PATH_BLOCKED", None)
    assert calls["review_api"] == 0


@pytest.mark.asyncio
async def test_a_different_cabin_on_review_is_never_a_saving():
    path, review, _c, extra = await _walk(review_html=_review(room="R999"))
    assert (path, review) == ("STATEROOM_CHANGED", None)
    assert extra["confirm_clicks"] == 0


@pytest.mark.asyncio
async def test_a_missing_cabin_label_is_reached_but_flagged():
    path, review, _c, _e = await _walk(review_html=_review(room=None))
    assert path == "REVIEW_REACHED"
    assert any("stateroom label not shown" in w for w in review.warnings)


@pytest.mark.asyncio
async def test_an_empty_category_table_is_reported():
    path, review, _c, _e = await _walk(category_html=_category(rows=(), enable=()))
    assert (path, review) == ("OWN_CATEGORY_NOT_OFFERED", None)


@pytest.mark.asyncio
async def test_a_candidate_not_on_the_rate_screen_stops_before_any_click():
    path, review, _c, extra = await _walk(offer="PSV")
    assert (path, review) == ("CANDIDATE_NOT_ON_SCREEN", None)
    assert extra["url"].endswith("/rate")


@pytest.mark.asyncio
async def test_a_full_page_reload_fails_safe_instead_of_hanging():
    path, review, _c, _e = await _walk(full_reload=True)
    assert review is None or review.gross == 1552            # never another figure
    assert path in ("REVIEW_NOT_CAPTURED", "REVIEW_REACHED")


@pytest.mark.asyncio
async def test_discard_answers_the_are_you_sure_box():
    """Recorded by Neon on DEMO18 2026-10-12: Discard All Changes opens
    "Are you sure? ... Yes, Discard Changes", and nothing is discarded until
    it is pressed. Unanswered, the booking stayed parked mid-edit."""
    pw, browser, s, _calls = await _wizard()
    try:
        path, _review_inv = await s.confirm_candidate_on_review("PHY", "BALCONY", "8C")
        assert path == "REVIEW_REACHED"
        assert await s.discard_changes() is True
        discarded = await s.page.evaluate("window.discarded === true")
        box_gone = await s.page.locator('[role="dialog"]').count() == 0
        confirm_clicks = await s.page.evaluate("window.confirmClicks || 0")
    finally:
        await _close(pw, browser)
    assert discarded and box_gone
    assert confirm_clicks == 0


@pytest.mark.asyncio
async def test_a_refused_review_is_reported_not_parsed():
    path, review, _c, _e = await _walk(review_status=409)
    assert (path, review) == ("REVIEW_REFUSED", None)
