"""ESPRESSO re-login judged before the sign-in had landed, 2026-10-08.

Neon 2026-10-08: "fix the esspresso login bug ... make sure that the
autologin now loops and the gui detects that the log in issue hapened and
it should log iin on its own".

The live log, 15:29-17:04 UTC, one 769-booking scan:

    espresso.relogin_verified ok=True        x2    the loop DOES work
    16:30:45 auto_login FILLED_AWAITING_MFA
    16:30:55 relogin_mfa_completed waited_s=10.1    x8 in a row - no human
                                                    types a code in 10s
    16:45:27 ... 16:48:28 relogin_mfa_timeout        one real 3-min wait
    16:48 - 17:02 relogin_mfa_skipped                x8 - flag never cleared
    auto_login NO_LOGIN_FORM url=/oauth/callback?code=...   x5
    batch.retry_after_recovery_failed               6 bookings
    17:03:41 recovered - a retry happened to land after the redirect

Three defects, all in how a login was JUDGED, not in the login itself:
  1. after submitting, it was judged ~1s later - before the OAuth chain
     (login -> auth.cruisingpower.com -> /oauth/callback -> app) landed -
     so plain successful logins were reported as MFA;
  2. a /oauth/callback page has no password box because nothing needs
     typing; that was reported as NO_LOGIN_FORM and treated as final;
  3. one MFA timeout set a flag that no later success ever cleared.

The pages below replay that chain with fake cruisingpower.com URLs served
by Playwright, so the real auto_login runs against a real browser.
"""

import asyncio

import pytest

from gui.windows import CruiseLinePanel
from scraper.espresso import EspressoScraper

APP = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
CALLBACK = "https://secure.cruisingpower.com/oauth/callback?code=abc&state=x"
LOGIN = "https://secure.cruisingpower.com/login"
MFA = "https://auth.cruisingpower.com/access/mfa"

# The callback sits ~4s exchanging its code before redirecting - long
# enough that a 1.5s "quiet" settle returns while it is still there.
_CALLBACK_HTML = f"""<html><body>Signing you in...
<script>setTimeout(() => location.href = '{APP}', 4000)</script></body></html>"""
_APP_HTML = "<html><body><h1>Reservations</h1></body></html>"
_MFA_HTML = "<html><body>Enter code <input type='text' name='otp'></body></html>"


def _login_html(after_submit: str) -> str:
    return f"""<html><body><form id=f>
      <input type='text' name='u'><input type='password' name='p'>
      <button type='submit'>Sign in</button></form>
      <script>document.getElementById('f').onsubmit = e => {{
          e.preventDefault(); setTimeout(() => location.href = '{after_submit}', 500); }};
      </script></body></html>"""


async def _portal(login_goes_to: str = CALLBACK):
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
        if "/oauth/callback" in url:
            body = _CALLBACK_HTML
        elif "/login" in url:
            body = _login_html(login_goes_to)
        elif "auth.cruisingpower.com" in url:
            body = _MFA_HTML
        else:
            body = _APP_HTML
        await route.fulfill(status=200, content_type="text/html", body=body)

    await context.route("https://*.cruisingpower.com/**", serve)
    page = await context.new_page()
    scraper = EspressoScraper()
    scraper._page = page
    return pw, browser, scraper


@pytest.fixture
def saved_credential(monkeypatch):
    import keyring
    monkeypatch.setattr(keyring, "get_password",
                        lambda service, key: {"username": "u", "password": "p"}[key])


@pytest.mark.asyncio
async def test_a_callback_page_is_waited_for_not_called_a_missing_form(saved_credential):
    pw, browser, s = await _portal()
    try:
        await s.page.goto(CALLBACK)
        result = await s.auto_login()
        landed = s.page.url
    finally:
        await browser.close()
        await pw.stop()
    assert result == "ALREADY_LOGGED_IN", result      # was NO_LOGIN_FORM
    assert landed == APP


@pytest.mark.asyncio
async def test_a_login_that_lands_in_seconds_is_ok_not_mfa(saved_credential):
    pw, browser, s = await _portal(login_goes_to=CALLBACK)
    try:
        await s.page.goto(LOGIN)
        result = await s.auto_login()
    finally:
        await browser.close()
        await pw.stop()
    assert result == "OK", result                     # was FILLED_AWAITING_MFA


@pytest.mark.asyncio
async def test_a_real_mfa_challenge_is_still_reported_as_mfa(saved_credential):
    pw, browser, s = await _portal(login_goes_to=MFA)
    s._SSO_LANDING_SECONDS = 4.0
    try:
        await s.page.goto(LOGIN)
        result = await s.auto_login()
    finally:
        await browser.close()
        await pw.stop()
    assert result == "FILLED_AWAITING_MFA", result


# -- the loop recovers, and says so --------------------------------------

def _stub(check_results):
    s = EspressoScraper.__new__(EspressoScraper)
    s._page = type("P", (), {"url": APP})()
    s.action_log, s.raw_dump_dir, s.on_action = [], None, None
    results = list(check_results)

    async def _check_login():
        return results.pop(0) if results else False

    # A re-login is proven on reservations since 2026-10-15.
    async def _prove_session():
        return (results.pop(0) if results else False), APP
    s._check_login = _check_login
    s._prove_session = _prove_session
    return s


def test_a_working_session_clears_the_mfa_timeout_and_tells_the_gui_once():
    s = _stub([True, True])
    s._mfa_wait_exhausted = True
    s._login_trouble = True
    assert asyncio.run(s._ensure_logged_in()) is True
    assert s._mfa_wait_exhausted is False
    assert asyncio.run(s._ensure_logged_in()) is True
    actions = [e["action"] for e in s.action_log]
    assert actions == ["login_recovered"]


def test_a_lost_session_is_reported_to_the_gui():
    s = _stub([False, True])

    async def _auto_login():
        return "OK"
    s.auto_login = _auto_login
    assert asyncio.run(s._ensure_logged_in()) is True
    assert [e["action"] for e in s.action_log] == ["login_session_lost", "login_recovered"]


@pytest.mark.parametrize("action, entry, expect", [
    ("login_session_lost", {}, "logging back in automatically"),
    ("login_recovered", {}, "logged back in automatically"),
    ("login_awaiting_mfa", {"waiting_s": 180}, "waiting 3 min"),
    ("login_awaiting_mfa", {"waiting_s": 0}, "each booking retries"),
    ("login_mfa_timeout", {}, "keeps retrying"),
    ("login_relogin_failed", {"result": "NO_LOGIN_FORM"}, "NO_LOGIN_FORM"),
])
def test_the_gui_names_each_login_event(action, entry, expect):
    message = CruiseLinePanel._login_status_message(
        action, {"cruise_line": "ESPRESSO", **entry})
    assert expect in message and message.startswith("ESPRESSO")


def test_ordinary_actions_leave_the_status_line_alone():
    assert CruiseLinePanel._login_status_message("search_booking", {}) is None
