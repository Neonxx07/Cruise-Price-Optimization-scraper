"""ESPRESSO_BACKGROUND_BROWSER — the window moves, the browser does not change.

True headless is refused by ESPRESSO's CDN edge (headed 200, every headless
mode 404, 8/8 trials — see test_espresso_headless_blocked_2026_09_23.py).
The desktop benefit therefore has to come from moving the WINDOW while
keeping the exact headed browser the site already accepts.

MEASURED 2026-09-23, off-screen against visible:

    visibilityState  visible   vs  visible
    document.hidden  False     vs  False
    rAF              61 fps    vs  61 fps
    viewport         1280x720  vs  1280x720
    RSS              666 MB    vs  672 MB

No throttling. And no resource win either — about 1%, inside the noise. The
benefit is an uncluttered desktop and unattended operation, which these
tests do not pretend is a performance claim.

THE TRAP THIS SUITE EXISTS FOR: `--start-minimized` passed the HTTP gate and
looked like a working background mode. It is silently ignored — the window
sits at (10, 10, 1306, 818), fully on screen. A launch flag is a request,
not a result.
"""

import inspect

import pytest

from config.settings import settings
from scraper.base import BaseScraper


class _Session:
    def __init__(self, fail_on=None):
        self.sent = []
        self._fail_on = fail_on

    async def send(self, method, params=None):
        if method == self._fail_on:
            raise RuntimeError("CDP refused")
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        return {}


class _Context:
    def __init__(self, session):
        self._session = session

    async def new_cdp_session(self, page):
        return self._session


class _Page:
    def __init__(self, session):
        self.context = _Context(session)


class _Scraper(BaseScraper):
    cruise_line = None

    def __init__(self, page):
        self._page = page

    async def check_booking(self, booking_id, capture_market_data=False):
        raise NotImplementedError


def _scraper(session=None):
    from core.models import CruiseLine
    s = _Scraper(_Page(session) if session else None)
    s.cruise_line = CruiseLine.ESPRESSO
    return s


# ── it moves the window, and only the window ─────────────────────────────


@pytest.mark.asyncio
async def test_moving_offscreen_uses_the_offscreen_coordinates():
    session = _Session()
    assert await _scraper(session).set_window_offscreen(True) is True
    method, params = session.sent[-1]
    assert method == "Browser.setWindowBounds"
    assert params["bounds"]["left"] == -32000
    assert params["bounds"]["top"] == -32000
    assert params["windowId"] == 7


@pytest.mark.asyncio
async def test_it_can_bring_the_window_back():
    """ESPRESSO's login needs MFA typed into a VISIBLE window. A background
    mode that cannot restore would make a mid-scan re-login impossible."""
    session = _Session()
    assert await _scraper(session).set_window_offscreen(False) is True
    _, params = session.sent[-1]
    assert params["bounds"]["left"] > 0 and params["bounds"]["top"] > 0


@pytest.mark.asyncio
async def test_the_window_keeps_a_real_size_in_both_states():
    """A zero-sized or tiny window would change layout and could break the
    SPA. Off-screen must stay a normal window, just elsewhere."""
    for offscreen in (True, False):
        session = _Session()
        await _scraper(session).set_window_offscreen(offscreen)
        bounds = session.sent[-1][1]["bounds"]
        assert bounds["width"] >= 1024 and bounds["height"] >= 720


# ── it must never take down a scan ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_move_is_survived():
    """A window that will not move is cosmetic. It must not raise."""
    session = _Session(fail_on="Browser.setWindowBounds")
    assert await _scraper(session).set_window_offscreen(True) is False


@pytest.mark.asyncio
async def test_a_refused_cdp_session_is_survived():
    session = _Session(fail_on="Browser.getWindowForTarget")
    assert await _scraper(session).set_window_offscreen(True) is False


@pytest.mark.asyncio
async def test_no_page_means_no_move_and_no_crash():
    assert await _scraper(None).set_window_offscreen(True) is False


# ── it must not become a way to smuggle in headless ──────────────────────


def test_it_does_not_touch_headless_or_the_browser_identity():
    """The whole point is that the browser stays byte-identical to the one
    ESPRESSO already accepts. Anything here touching headless, the user
    agent, or automation flags would turn a window move into evasion."""
    # EXECUTABLE STATEMENTS ONLY. Comments AND the docstring both discuss
    # headless at length - they explain why this exists. Matching prose
    # instead of code is a mistake this file has already made once, and the
    # wider codebase three times.
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(BaseScraper.set_window_offscreen)))
    fn = tree.body[0]
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = ast.dump(ast.Module(body=body, type_ignores=[]))
    for forbidden in ("headless", "user_agent", "userAgent", "webdriver",
                      "add_init_script", "setUserAgentOverride"):
        assert forbidden not in code, f"{forbidden} must not appear here"


def test_the_setting_is_off_by_default():
    """Enabling it requires handling the MFA-restore case, so it must not
    switch itself on."""
    assert settings.browser_background_window is False


def test_the_setting_is_separate_from_headless():
    """browser_headless stays True for NCL/GoCCL and is force-overridden for
    ESPRESSO. The background-window setting is a different axis entirely and
    must never be conflated with it."""
    assert settings.browser_headless is True
    assert hasattr(settings, "browser_background_window")
