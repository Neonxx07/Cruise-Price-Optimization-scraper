"""Three bugs Neon reported from the 2026-08-28 tabbed-GUI run.

1. **ESPRESSO double login.** "i log in twice in one time i press on check
   log in and start i log in again". `EspressoScraper._check_login` was
   purely a URL test:

       if "login" in url or "signin" in url: return False
       return "cruisingpower.com" in url

   ESPRESSO authenticates through an OAuth SSO hop on
   `auth.cruisingpower.com` - which contains NEITHER "login" NOR "signin"
   and IS on cruisingpower.com. So it returned True while the login form
   was still on screen: "Check login" reported OK within ~10s before the
   human typed anything, and Start then hit the real login wall.

2. **NCL never auto-logged in from the GUI.** `auto_login()` was wired into
   `_run_batch`'s fresh-browser path, but the GUI runs with
   keep_browser_open=True and authenticates through
   `BookingService.check_login`, which never called it.

3. **Promo losses were invisible.** Only LATRIPLE/FREESRVC were ever
   examined, so any other lost promo never reached the verdict. The real
   2026-08-28 NCL run: **11 of 36 OPTIMIZATIONs lost at least one promo** -
   FITOBC x4 (an on-board-credit promo), AF15OFF x4, DISC35 x2, plus
   DASHSALE, SHX50, 34CHO, LATDBLX. Booking 3000046 reported "$456 saved"
   while losing FITOBC.
"""
import pytest

from core.calculator import calculate_ncl, ncl_lost_promos
from core.models import BookingStatus, CruiseLine


# -- 1. ESPRESSO login detection --------------------------------------


class _FakePage:
    def __init__(self, url, password_fields=0):
        self.url = url
        self._n = password_fields

    def locator(self, selector):
        page = self

        class _Loc:
            async def count(self):
                return page._n if "password" in selector else 0

        return _Loc()


def _espresso_with(url, password_fields=0):
    from scraper.espresso import EspressoScraper

    s = EspressoScraper()
    s._page = _FakePage(url, password_fields)
    return s


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    # THE bug: the real SSO host, which says neither "login" nor "signin".
    "https://auth.cruisingpower.com/oauth2/authorize?client_id=espresso",
    "https://auth.cruisingpower.com/as/authorization.oauth2",
    "https://secure.cruisingpower.com/login",
    "https://secure.cruisingpower.com/signin",
    "https://idp.cruisingpower.com/sso/saml",
])
async def test_auth_pages_are_not_treated_as_logged_in(url):
    assert await _espresso_with(url)._check_login() is False


@pytest.mark.asyncio
async def test_a_password_field_means_not_logged_in_whatever_the_url_says():
    """The decisive check. A login form can be served on an app-looking URL,
    and the URL test alone cannot see it."""
    s = _espresso_with("https://secure.cruisingpower.com/home", password_fields=1)
    assert await s._check_login() is False


@pytest.mark.asyncio
async def test_espresso_in_the_path_is_not_mistaken_for_sso():
    """REAL false positive caught before it shipped: a first version matched
    the bare substring "sso" against the whole URL, and "sso" appears inside
    **"espresso"** - so /espresso/protected/reservations.do was classed as a
    login page and every booking would have failed "Not logged in"."""
    s = _espresso_with("https://secure.cruisingpower.com/espresso/protected/reservations.do")
    assert await s._check_login() is True


@pytest.mark.asyncio
async def test_a_real_logged_in_page_still_passes():
    """Must not over-block - refusing a good session would make the GUI
    unusable, which is worse than the bug being fixed."""
    for url in ("https://secure.cruisingpower.com/home",
                "https://secure.cruisingpower.com/espresso/protected/reservations.do"):
        assert await _espresso_with(url)._check_login() is True


@pytest.mark.asyncio
async def test_a_failing_password_probe_does_not_report_logged_out():
    """A broken probe is not a logged-out session; failing closed here would
    refuse a perfectly good login."""
    from scraper.espresso import EspressoScraper

    class _Boom:
        url = "https://secure.cruisingpower.com/home"

        def locator(self, selector):
            raise RuntimeError("probe exploded")

    s = EspressoScraper()
    s._page = _Boom()
    assert await s._check_login() is True


# -- 2. auto-login on the GUI path ------------------------------------


@pytest.mark.asyncio
async def test_check_login_tries_auto_login_before_waiting_for_a_human(monkeypatch):
    """The GUI reaches the portal through check_login, so auto_login has to
    be attempted HERE or a saved NCL credential is never used."""
    from services.booking_service import BookingService

    service = BookingService()
    calls = []

    class _Scraper:
        cruise_line = CruiseLine.NCL
        market = "US"

        class _P:
            url = "https://seawebagents.ncl.com/tva/search/"

        page = _P()

        async def navigate(self, url):
            calls.append(("navigate", url))

        async def auto_login(self):
            calls.append(("auto_login", None))
            return "OK"

        async def _check_login(self):
            return True

    async def fake_get(cruise_line, headless=None, market=None):
        return _Scraper()

    monkeypatch.setattr(service, "get_or_create_scraper", fake_get)

    ok = await service.check_login(CruiseLine.NCL, timeout_minutes=0.01)
    assert ok is True, "a successful auto-login must satisfy check_login"
    assert ("auto_login", None) in calls, "auto_login was never attempted"


@pytest.mark.asyncio
async def test_a_failed_auto_login_falls_through_to_the_manual_wait(monkeypatch):
    """ESPRESSO has no auto-login at all (MFA), and a rejected NCL
    credential must not block a human from logging in by hand."""
    from services.booking_service import BookingService

    service = BookingService()

    class _Scraper:
        cruise_line = CruiseLine.NCL
        market = "US"

        class _P:
            url = "https://seawebagents.ncl.com/Security/login/"

        page = _P()

        async def navigate(self, url):
            return None

        async def auto_login(self):
            return "NO_CREDENTIALS_SAVED"

        async def _check_login(self):
            return False

    monkeypatch.setattr(
        service, "get_or_create_scraper",
        lambda cruise_line, headless=None, market=None: _wrap(_Scraper()),
    )

    async def _wrap(v):
        return v

    result = await service.check_login(CruiseLine.NCL, timeout_minutes=0.01)
    assert result is False    # timed out waiting, did not crash


# -- 3. promo losses ---------------------------------------------------


def test_every_lost_promo_is_detected_not_just_the_protected_two():
    assert ncl_lost_promos("FITOBC,EASYFARE", "FLATOFF,EASYFARE") == ["FITOBC"]
    assert ncl_lost_promos("AF15OFF,DISC35", "DISC50") == ["AF15OFF", "DISC35"]
    assert ncl_lost_promos("EASYFARE", "EASYFARE") == []
    assert ncl_lost_promos(None, None) == []


def test_the_real_3000046_is_now_a_hard_trap_not_a_warning():
    """SUPERSEDED 2026-08-28, same day. This originally asserted the booking
    stayed an OPTIMIZATION carrying a "LOSES PROMO(S): FITOBC" warning -
    correct at the time, because FITOBC was not yet gated.

    Neon then confirmed FITOBC is "the same" case as LATRIPLE, so it moved
    into NCL_NEVER_LOSE_PROMOS and a loss is now a hard TRAP decided before
    any status is assigned. The warning path still exists and is covered by
    test_latrew_and_latitude_are_deliberately_not_gated - it applies to
    promos that are NOT gated.

    Kept rather than deleted because this booking is the concrete example
    Neon raised ("$456 saved" while forfeiting an OBC promo), and the
    stronger outcome is worth pinning to it.
    """
    r = calculate_ncl(
        "3000046", "B6", 3222.80, 2716.80,
        [{"guest": "MS DONN", "name": "Free $50 On-Board Credit Certificate"}],
        old_promos="FITOBC,EASYFARE", new_promos="FLATOFF,EASYFARE",
        new_addons=[],
    )
    assert r.status == BookingStatus.TRAP
    assert r.status != BookingStatus.OPTIMIZATION
    assert "FITOBC" in r.note
    assert r.confidence == 1
    assert r.lost_fares == ["FITOBC"]


def test_a_clean_optimization_is_not_penalised():
    """No promo lost -> no warning, full confidence. The fix must not
    devalue every result."""
    r = calculate_ncl("CLEAN", "BB", 1818.0, 1718.0, [],
                      old_promos="EASYFARE", new_promos="EASYFARE",
                      new_addons=[])
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.confidence == 5
    assert "LOSES PROMO" not in r.note


def test_the_protected_gate_still_wins():
    """LATRIPLE/FREESRVC remain a hard TRAP - the general warning must not
    downgrade them to a mere note."""
    r = calculate_ncl("PROT", "BB", 2000.0, 1600.0, [],
                      old_promos="LATRIPLE,FITOBC", new_promos="FLATOFF",
                      new_addons=[])
    assert r.status == BookingStatus.TRAP
    assert r.confidence == 1
    assert "LATRIPLE" in r.note


def test_promos_gained_only_are_not_reported_as_a_loss():
    r = calculate_ncl("GAIN", "BB", 2000.0, 1900.0, [],
                      old_promos="EASYFARE", new_promos="EASYFARE,FLATOFF",
                      new_addons=[])
    assert "LOSES PROMO" not in r.note
    assert r.confidence == 5
