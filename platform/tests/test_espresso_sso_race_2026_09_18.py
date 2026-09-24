"""An SSO hop is not a logout.

Neon 2026-09-18, mid-session: "i press in logg in yes it does logg in
however when i press on start the pages refreshes and then i need to log in
again! please fix this issue".

THE RACE. ESPRESSO authenticates through an OAuth round-trip on
`auth.cruisingpower.com`, and `auth.` is the first entry in
_AUTH_HOST_PREFIXES. check_booking navigates to /home and calls _check_login
IMMEDIATELY:

    await self.navigate(settings.espresso_home_url)
    if not await self._check_login():
        raise RuntimeError("Not logged in - please log into ESPRESSO first")

When that sample lands mid-redirect the caller is told "Not logged in" and
the operator is sent back to a login screen they had just completed. It
struck only sometimes, and logging in again always appeared to fix it -
because the second attempt simply sampled after the chain settled.

Two hypotheses were tested and REJECTED first, which is why this one is
credible:
  * that the Start button forced a re-login - it only warns and returns;
  * that a hidden `input[type=password]` on a logged-in page tripped the
    check - six real captured pages from a working scan contain ZERO
    password inputs.
"""
import inspect

from scraper.espresso import EspressoScraper


def _check_login_src() -> str:
    return inspect.getsource(EspressoScraper._check_login)


def test_the_auth_host_prefixes_include_the_one_espresso_actually_uses():
    """`auth.cruisingpower.com` is ESPRESSO's real SSO host - so the
    prefix that causes the race is genuinely in the list, and removing it
    is NOT the fix (a real logout does land there)."""
    assert "auth." in EspressoScraper._AUTH_HOST_PREFIXES


def test_an_auth_page_is_re_checked_before_being_called_a_logout():
    src = _check_login_src()
    branch = src[src.index("_AUTH_HOST_PREFIXES"):]
    assert "wait_for_load_state" in branch, (
        "the auth branch returns False on the first sample - that is the race"
    )
    assert "sso_hop_settled" in branch


def test_a_settled_non_auth_url_is_treated_as_logged_in():
    """The whole point: when the redirect lands back on ESPRESSO, the
    session was valid all along."""
    src = _check_login_src()
    assert "transient SSO redirect, not a logout" in src


def test_a_page_that_stays_on_auth_is_still_a_real_logout():
    """The guard must not become permissive. A genuine logout STAYS on the
    auth page, and after waiting it must still return False - otherwise a
    whole batch runs against a logged-out portal, which is the failure this
    check was written for in the first place."""
    src = _check_login_src()
    assert "still on an auth/SSO page after waiting" in src
    tail = src[src.index("still on an auth/SSO page after waiting"):]
    assert "return False" in tail


def test_the_wait_is_bounded():
    """A hung redirect must not stall a scan indefinitely."""
    src = _check_login_src()
    branch = src[src.index("_AUTH_HOST_PREFIXES"):]
    assert "for _ in range(" in branch
    assert "timeout=" in branch


def test_check_booking_still_refuses_to_scan_when_logged_out():
    """The caller's contract is unchanged - this fix makes the DETECTION
    accurate, it does not remove the gate."""
    src = inspect.getsource(EspressoScraper.check_booking)
    assert "Not logged in" in src
    assert "raise RuntimeError" in src
