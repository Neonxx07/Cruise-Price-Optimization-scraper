"""ESPRESSO must actually USE the credential that save_login.py stores.

Neon 2026-09-16: "there is a bug with espresso logging in because when i
try it is not logging in automatically although i have entered the
passwords using the command".

He was right, and the cause was blunt: EspressoScraper had NO auto_login
method at all. save_login.py offers ESPRESSO as option 2 and writes the
credential to Windows Credential Manager; clear_login.py can remove it -
and nothing in the codebase ever read it back. config/settings.py said so
outright ("not-yet-built work"). Saving the password did nothing.

MFA cannot be automated and this does not pretend otherwise: the normal
outcome is FILLED_AWAITING_MFA - credential typed in, human finishes.
"""
import inspect

import pytest

from config.settings import settings
from scraper.espresso import EspressoScraper
from scraper.ncl import NclScraper


# -- the method has to exist and be reachable ------------------------


def test_espresso_has_an_auto_login_at_all():
    """THE BUG. This is what was missing."""
    assert hasattr(EspressoScraper, "auto_login")
    assert callable(EspressoScraper.auto_login)


def test_it_reads_the_same_credential_store_save_login_writes_to():
    """save_login.py option 2 stores under settings.espresso_credential_service.
    Reading anywhere else would look identical from the outside and still
    never log in."""
    assert EspressoScraper.credential_service.fget(
        EspressoScraper.__new__(EspressoScraper)
    ) == settings.espresso_credential_service

    import save_login

    assert any(svc == settings.espresso_credential_service
               for _label, svc in save_login.CRUISE_LINES.values())


def test_the_service_calls_auto_login_generically():
    """BookingService gates on `hasattr(scraper, "auto_login")`, so adding
    the method is the whole wiring. Pinned because a future refactor to an
    explicit per-line list would silently drop ESPRESSO again - the exact
    'written but never called' failure this project keeps hitting."""
    import services.booking_service as svc

    src = inspect.getsource(svc)
    assert 'hasattr(scraper, "auto_login")' in src
    assert src.count('hasattr(scraper, "auto_login")') >= 2, (
        "both the login-check path and the batch path must reach it"
    )


# -- it must never crash a run ---------------------------------------


@pytest.mark.asyncio
async def test_missing_credentials_return_a_status_not_an_exception():
    """Contract shared with NclScraper.auto_login and msc_commands.
    auto_login: never raise, so the caller can fall back to a manual
    prompt instead of killing the run."""
    scraper = EspressoScraper.__new__(EspressoScraper)

    import keyring

    real = keyring.get_password
    keyring.get_password = lambda *a, **k: None
    try:
        assert await scraper.auto_login() == "NO_CREDENTIALS_SAVED"
    finally:
        keyring.get_password = real


@pytest.mark.asyncio
async def test_an_unexpected_error_returns_a_status_not_an_exception():
    scraper = EspressoScraper.__new__(EspressoScraper)

    import keyring

    real = keyring.get_password
    keyring.get_password = lambda *a, **k: "x"
    try:
        # no .page attribute at all -> whatever breaks, it must be caught
        status = await scraper.auto_login()
        assert isinstance(status, str)
        assert status.startswith("ERROR") or status in (
            "NO_LOGIN_FORM", "NO_CREDENTIALS_SAVED")
    finally:
        keyring.get_password = real


# -- MFA honesty ------------------------------------------------------


def test_it_does_not_claim_success_before_the_session_is_real():
    """"OK" is returned only after _check_login confirms a real session.
    ESPRESSO authenticates through an SSO hop, and a URL that merely looks
    right is what caused the original double-login bug."""
    src = inspect.getsource(EspressoScraper.auto_login)
    assert "FILLED_AWAITING_MFA" in src
    ok_at = src.index('return "OK"')
    check_at = src.rindex("await self._check_login()", 0, ok_at)
    assert check_at < ok_at, "OK must follow a real _check_login"


def test_the_credential_is_never_logged():
    """A password must not reach a log line, a status string or a
    screenshot filename."""
    src = inspect.getsource(EspressoScraper.auto_login)
    for line in src.splitlines():
        if "logger." in line or "return f" in line:
            assert "password" not in line.lower().replace("password\"", "").replace(
                "'password'", ""), line


# -- selectors are discovered, not invented ---------------------------


def test_the_form_is_found_by_SHAPE_not_a_guessed_selector():
    """No ESPRESSO login page has ever been captured - capture starts after
    login - so there are no real selectors to use. Rather than invent
    `#username`, the form is located structurally: the visible password
    input (a selector _check_login already relies on for this exact site)
    and the nearest preceding visible text input. Same discipline as
    _VX_FIND_JS for NCL's grid."""
    src = inspect.getsource(EspressoScraper.auto_login)
    assert 'input[type="password"]' in src
    assert "DOCUMENT_POSITION_PRECEDING" in src
    for invented in ("#username", "#userName", "#loginForm", "#j_username"):
        assert invented not in src, f"{invented} is a guessed selector"


def test_it_captures_the_login_page_for_future_grounding():
    """So the next change can be made against real markup instead of
    discovery."""
    src = inspect.getsource(EspressoScraper.auto_login)
    assert "dump_page_snapshot" in src


def test_it_matches_the_ncl_auto_login_contract():
    """Same shape as the one that already works, so callers need no
    special-casing."""
    esp = inspect.signature(EspressoScraper.auto_login)
    ncl = inspect.signature(NclScraper.auto_login)
    assert list(esp.parameters) == list(ncl.parameters) == ["self"]
    assert esp.return_annotation == ncl.return_annotation == "str"
