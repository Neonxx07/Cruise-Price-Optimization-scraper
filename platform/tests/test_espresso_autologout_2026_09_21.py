"""ESPRESSO signs ITSELF out after 30.5 minutes.

Neon 2026-09-21: "esspresso stil going through issues and errors ... does
not log in properly and sometimes it logs out".

FOUND IN A LIVE CAPTURE from the run Neon had going at the time - the
failure snapshot for booking 3001003 - and then confirmed in 50 occurrences
across 25 captured pages. Every ESPRESSO page arms this on load:

    setTimeout(function(){
        window.location.href = window.Base.flowExecutionURL + "&_eventId=logout"
    }, 1830000);

1,830,000 ms = 30.5 minutes. The BROWSER navigates itself to logout. Nothing
in this project reset it, because nothing knew it existed.

Every navigation re-arms the timer, so an ACTIVE scan keeps resetting it.
An IDLE session does not - which is exactly the reported sequence: "Check
login" succeeds, the operator does something else for half an hour, presses
Start, and is sent back to a login screen they had already completed.
"""
import time

import pytest

from scraper.espresso import EspressoScraper


def test_the_timer_value_matches_what_the_portal_ships():
    """1,830,000 ms, read off the real page - not a guess."""
    assert EspressoScraper.ESPRESSO_AUTO_LOGOUT_MS == 1_830_000
    assert EspressoScraper.ESPRESSO_AUTO_LOGOUT_MS / 60_000 == 30.5


def test_the_keepalive_fires_well_inside_the_logout_window():
    """Headroom matters: a refresh scheduled AT the deadline races the
    portal's own timer. 20 minutes leaves 10 spare for a slow page."""
    keepalive_s = EspressoScraper.SESSION_KEEPALIVE_SECONDS
    logout_s = EspressoScraper.ESPRESSO_AUTO_LOGOUT_MS / 1000
    assert keepalive_s < logout_s
    assert logout_s - keepalive_s >= 600      # at least 10 minutes of slack


@pytest.mark.asyncio
async def test_a_busy_session_is_not_refreshed():
    """An active scan navigates constantly and re-arms the timer itself.
    Refreshing on top of that would be a wasted page load per booking."""
    s = EspressoScraper()
    s._last_navigation_at = time.monotonic()      # just navigated
    assert await s.keep_session_alive() is False


@pytest.mark.asyncio
async def test_an_idle_session_is_refreshed(monkeypatch):
    """THE FIX. A session that has been sitting past the keepalive window
    gets touched before it can sign itself out."""
    s = EspressoScraper()
    s._last_navigation_at = time.monotonic() - (EspressoScraper.SESSION_KEEPALIVE_SECONDS + 60)
    navigated = []

    async def fake_navigate(url, **kw):
        navigated.append(url)

    monkeypatch.setattr(s, "navigate", fake_navigate)
    assert await s.keep_session_alive() is True
    assert navigated, "an idle session must actually be touched"


@pytest.mark.asyncio
async def test_a_never_navigated_session_is_not_refreshed_by_default():
    """No navigation stamp means nothing is known about idleness - and a
    brand-new scraper has no session to preserve."""
    s = EspressoScraper()
    s._last_navigation_at = None
    assert await s.keep_session_alive() is False


@pytest.mark.asyncio
async def test_a_failing_keepalive_never_raises(monkeypatch):
    """It runs just before a scan starts. A failed refresh must not take
    down a session that might still be perfectly usable."""
    s = EspressoScraper()
    s._last_navigation_at = time.monotonic() - 10_000

    async def boom(url, **kw):
        raise RuntimeError("portal unreachable")

    monkeypatch.setattr(s, "navigate", boom)
    assert await s.keep_session_alive() is False      # reported, not raised


@pytest.mark.asyncio
async def test_force_refreshes_regardless_of_idle_time(monkeypatch):
    s = EspressoScraper()
    s._last_navigation_at = time.monotonic()
    calls = []

    async def fake_navigate(url, **kw):
        calls.append(url)

    monkeypatch.setattr(s, "navigate", fake_navigate)
    assert await s.keep_session_alive(force=True) is True
    assert calls


def test_the_batch_refreshes_before_judging_the_session():
    """Order matters: touching the portal must happen BEFORE the preflight
    login check, or the check judges a session the timer already killed."""
    import inspect

    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    assert src.index("keep_session_alive") < src.index('"_check_login" in type(scraper).__dict__')


# ── an IDLE tab logs itself out while another line scans ─────────────────
#
# OBSERVED 2026-09-22 in the log file:
#     14:40  last ESPRESSO activity
#     14:42  NCL auto_login OK, NCL scanning
#     15:15  NCL still going  -> ESPRESSO 35 minutes idle, past its own
#            30.5-minute limit, and signed itself out mid-session.
#
# The batch-start keepalive could not help: it only runs when THAT line's
# batch starts. A tab belonging to a different line just sits there.


def test_the_gui_keeps_idle_sessions_alive_on_a_timer():
    import inspect

    from gui.windows import MainWindow

    src = inspect.getsource(MainWindow)
    assert "_keepalive_timer" in src
    assert "_keep_sessions_alive" in src


def test_the_keepalive_interval_is_well_inside_the_logout_window():
    """A 5-minute sweep against a 30.5-minute limit leaves plenty of room
    even if a tick is missed while the UI is busy."""
    from scraper.espresso import EspressoScraper

    sweep_ms = 5 * 60 * 1000
    logout_ms = EspressoScraper.ESPRESSO_AUTO_LOGOUT_MS
    assert sweep_ms * 3 < logout_ms          # at least three chances to fire


def test_a_scanning_line_is_skipped():
    """A line mid-scan re-arms its own timer with every navigation.
    Navigating underneath it would be worse than useless."""
    import inspect

    from gui.windows import MainWindow

    src = inspect.getsource(MainWindow._keep_sessions_alive)
    assert "panel.is_busy()" in src and "continue" in src


def test_the_keepalive_sweep_never_raises():
    """It runs on a GUI timer; an exception there is a crash the user sees."""
    import inspect

    from gui.windows import MainWindow

    src = inspect.getsource(MainWindow._keep_sessions_alive)
    assert "except Exception" in src


def test_only_the_line_owning_the_live_scraper_is_touched():
    """BookingService keeps ONE _live_scraper slot shared across lines - a
    keepalive must not touch a scraper that now belongs to another tab."""
    import inspect

    from gui.windows import MainWindow

    src = inspect.getsource(MainWindow._keep_sessions_alive)
    assert "scraper.cruise_line != line" in src
    assert "is_alive" in src


# ── a login form mid-bootstrap is not a logout ───────────────────────────
#
# Neon 2026-09-22, with a screenshot of secure.cruisingpower.com/login:
# "i still get this page however it is checking bookings normaaly which is
# weiered i feel it is login in and out however it still chhecking and
# processing".
#
# Captured in the log at 17:39:31, on booking 299 of 721:
#
#   17:39:30  espresso.navigate_home   booking 3001004
#   17:39:31  login.required  "password field present - login form showing"
#   17:39:31  Attempt 1/3 failed: Not logged in - retrying in 3.0s
#   17:39:34  espresso.navigate_home   (retry)
#   17:39:38  browser.navigate_recovered
#
# /home renders a login form for a moment while it bootstraps. _check_login
# sampled it at that instant and reported a logout; the retry three seconds
# later succeeded. The session was never lost - which is precisely why the
# scan kept processing while the login page flashed on screen.


def test_both_branches_of_check_login_now_settle():
    """The SSO-host branch already waited for the page to settle. The
    password-field branch returned False immediately - same race, and it
    was the one firing mid-scan."""
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper._check_login)
    assert "sso_hop_settled" in src              # the branch that already settled
    assert "password_form_settled" in src        # the one that now does too


def test_a_transient_form_resolves_to_LOGGED_IN():
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper._check_login)
    idx = src.index("password_form_settled")
    assert "return True" in src[idx:idx + 400]


def test_a_persistent_form_is_still_a_logout():
    """A genuine logout keeps its form for the whole window. The guard must
    not swing the other way and declare a logged-out session healthy."""
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper._check_login)
    idx = src.index("password_form_settled")
    tail = src[idx:]
    assert "return False" in tail
    assert "login form still showing" in tail


def test_the_settle_window_is_bounded():
    """Unbounded waiting on a real logout would look like a hang."""
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper._check_login)
    assert "range(6)" in src
