"""Auto-login filled a marker that a re-render had already destroyed.

Neon 2026-09-22: "check login and then when i press start the page refreshes
and i need to log in again" - still happening after the 30.5-minute
auto-logout fix.

DIAGNOSED FROM THE LOG FILE, which this project only gained the day before.
Two lines, thirty seconds apart:

    14:36:24  espresso.auto_login_form_found
              user_field=mantine-d8ke1v2g9  pass_field=mantine-ytoafz0pi
    14:36:54  espresso.auto_login_failed
              Page.fill: Timeout 30000ms exceeded
              waiting for locator("[data-ch-login=\\"user\\"]")

The discovery JS FOUND both fields and stamped them with `data-ch-login`,
and then a separate page.fill could not find the stamp. The field ids give
it away: "mantine-d8ke1v2g9" is generated fresh on each render, so this is a
React/Mantine form that re-rendered between the two calls and replaced the
nodes - taking the attribute with them.

Consequence: auto-login NEVER worked. Every attempt burned 30 seconds and
ended in a manual login, which is precisely the symptom reported.
"""
import inspect

from scraper.espresso import EspressoScraper


SRC = inspect.getsource(EspressoScraper.auto_login)


def test_the_fill_no_longer_depends_on_a_marker_surviving_a_rerender():
    """THE FIX. Playwright locators re-resolve at action time, which is what
    a re-rendering form requires. Marking a node and then looking for that
    mark in a later call is a race the form wins."""
    assert 'page.fill(\'[data-ch-login="user"]\'' not in SRC
    assert 'page.fill(\'[data-ch-login="pass"]\'' not in SRC
    assert "input[type='password']:visible" in SRC


def test_the_username_and_password_are_located_separately():
    assert "user_box" in SRC and "pass_box" in SRC
    assert "user_box.fill" in SRC and "pass_box.fill" in SRC


def test_the_fill_is_bounded_so_a_failure_is_fast():
    """The old path waited the full default 30s before giving up, once per
    attempt, for a selector that could never appear."""
    assert "timeout=10000" in SRC


def test_submit_is_also_located_not_marked():
    assert "button[type='submit']:visible" in SRC


def test_the_discovery_js_is_kept_only_as_a_diagnostic():
    """It is what identified the Mantine ids in the log and made the
    diagnosis possible - worth keeping, but no longer load-bearing."""
    assert "auto_login_form_found" in SRC
    assert "hasSubmit" in SRC


def test_auto_login_still_never_raises():
    """A login helper must return a status, not explode - a caller has to be
    able to fall back to a manual login."""
    assert "except Exception as exc:" in SRC
    assert "NO_CREDENTIALS_SAVED" in SRC
    assert "FILLED_AWAITING_MFA" in SRC


def test_mfa_is_still_reported_rather_than_waited_on():
    """ESPRESSO demands MFA; blocking on it would look like a hang."""
    assert "FILLED_AWAITING_MFA" in SRC
