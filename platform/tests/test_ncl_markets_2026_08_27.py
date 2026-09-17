"""NCL multi-market (US / Canada-CAD) support.

CONFIRMED BY NEON 2026-08-27. NCL's "Reservation is not found" on a
whole group of bookings did NOT mean those bookings were gone — they are
CANADIAN bookings, and Canada is a SEPARATE SeaWeb agent account (CAD).
Logged into the US account, a CAD booking is simply invisible.

25 of the 101 errors in that day's NCL run were this: sitting in the ERROR
bucket next to real defects, inflating the error rate, and telling the
operator nothing about what to actually do (re-run on the CAD login).
"""
import pytest

from config.settings import settings
from core.calculator import make_not_on_this_account_result
from core.models import BookingStatus, CruiseLine
from scraper.ncl import NclScraper, _is_wrong_account_error


# ── classifying the portal message ───────────────────────────────


@pytest.mark.parametrize("text", [
    "Reservation is not found",
    "Close\nReservation is not found",          # the real observed string
    "  reservation   is   NOT   found  ",       # whitespace/case noise
    "Reservation not found",
])
def test_not_found_wording_is_recognised(text):
    assert _is_wrong_account_error(text) is True


@pytest.mark.parametrize("text", [
    None,
    "",
    "Session expired",
    "Category grid not available",
    "An unexpected error occurred",
    # Must NOT match: a genuine failure that merely contains "found".
    "__preloaded_data not found",
    "Category 'IX' not found in grid data",
])
def test_other_errors_are_not_treated_as_wrong_account(text):
    """NCL's error element is shared with real failures. Mis-classifying
    one of those as a clean no-op would hide a genuine bug — the whole
    reason this match is deliberately narrow."""
    assert _is_wrong_account_error(text) is False


# ── the result it produces ───────────────────────────────────────


def test_result_is_not_an_error_status():
    r = make_not_on_this_account_result("3000056", CruiseLine.NCL, "US", ["US", "CA"])
    assert r.status == BookingStatus.NOT_ON_THIS_ACCOUNT
    assert r.status != BookingStatus.ERROR
    assert r.error is None


def test_result_names_the_account_to_retry_on():
    r = make_not_on_this_account_result("3000056", CruiseLine.NCL, "US", ["US", "CA"])
    assert "US account" in r.note
    assert "CA" in r.note


def test_result_asserts_nothing_about_price():
    """It must never look like a priced finding: confidence 0, no savings.
    total_optimization_savings filters on OPTIMIZATION, but a non-zero
    net_saving here would still be misleading in the GUI and exports."""
    r = make_not_on_this_account_result("3000056", CruiseLine.NCL, "US", ["US", "CA"])
    assert r.confidence == 0
    assert r.net_saving == 0.0
    assert r.old_total == 0.0
    assert r.new_total == 0.0


def test_result_warns_about_the_edit_lock_confound():
    """Recorded honestly: in the 2026-08-27 run, 3000059 and 3000060 both
    reported not-found while a CONCURRENT session held their 30-minute edit
    lock, yet both were readable on their own. So this status means "not
    visible to this account right now", and the note must say so rather
    than asserting the booking is Canadian."""
    r = make_not_on_this_account_result("3000059", CruiseLine.NCL, "US", ["US", "CA"])
    assert "edit lock" in r.note.lower()


def test_result_degrades_gracefully_with_no_other_markets_listed():
    r = make_not_on_this_account_result("1", CruiseLine.NCL, "US", [])
    assert "other market" in r.note.lower()


# ── per-market account isolation ─────────────────────────────────


def test_default_market_keeps_the_original_credential_service():
    """Existing credentials saved via save_login.py must keep working with
    no migration — the default market must NOT get a suffix."""
    s = NclScraper()
    assert s.market == settings.ncl_default_market.upper()
    assert s.credential_service == settings.ncl_credential_service


def test_canada_market_uses_a_separate_credential_service():
    s = NclScraper(market="CA")
    assert s.credential_service != settings.ncl_credential_service
    assert s.credential_service == f"{settings.ncl_credential_service}_ca"


def test_market_is_case_insensitive():
    assert NclScraper(market="ca").market == "CA"
    assert NclScraper(market="Ca").credential_service.endswith("_ca")


def test_each_market_gets_its_own_session_file():
    """THE bug this prevents: two NCL accounts sharing one
    storage_state_NCL.json would clobber each other's session on every
    switch — exactly what made a GoCCL login wipe out a working ESPRESSO
    session (see BaseScraper._storage_state_path)."""
    us = NclScraper(market="US")._storage_state_path()
    ca = NclScraper(market="CA")._storage_state_path()
    assert us is not None and ca is not None
    assert us != ca
    assert us.endswith("storage_state_NCL.json"), us
    assert ca.endswith("storage_state_NCL_ca.json"), ca


def test_both_markets_are_configured():
    assert "US" in settings.ncl_markets
    assert "CA" in settings.ncl_markets
    assert settings.ncl_default_market.upper() in settings.ncl_markets


# ── the service must not reuse the wrong account ─────────────────


@pytest.mark.asyncio
async def test_switching_market_replaces_the_live_scraper(monkeypatch):
    """Reusing a live US scraper for a CA scan would check every Canadian
    booking against the wrong account and report them all not-found — the
    same failure this whole change exists to fix."""
    from services.booking_service import BookingService

    service = BookingService()
    started: list[str] = []
    stopped: list[str] = []

    class _FakeNcl:
        cruise_line = CruiseLine.NCL
        is_alive = True

        def __init__(self, market=None):
            self.market = (market or "US").upper()

        async def start(self, headless=None):
            started.append(self.market)

        async def stop(self):
            stopped.append(self.market)

    monkeypatch.setattr(
        service, "_get_scraper",
        lambda cruise_line, market=None: _FakeNcl(market=market),
    )

    first = await service.get_or_create_scraper(CruiseLine.NCL, market="US")
    assert first.market == "US"

    second = await service.get_or_create_scraper(CruiseLine.NCL, market="CA")
    assert second.market == "CA", "a market switch must build a new scraper"
    assert second is not first
    assert stopped == ["US"], "the US session must be closed, not left dangling"
    assert started == ["US", "CA"]


@pytest.mark.asyncio
async def test_same_market_reuses_the_live_scraper(monkeypatch):
    """Must not over-trigger: NCL needs ONE continuous session per account,
    so re-requesting the same market must reuse it."""
    from services.booking_service import BookingService

    service = BookingService()

    class _FakeNcl:
        cruise_line = CruiseLine.NCL
        is_alive = True

        def __init__(self, market=None):
            self.market = (market or "US").upper()

        async def start(self, headless=None):
            pass

        async def stop(self):
            raise AssertionError("must not stop a scraper for the same market")

    monkeypatch.setattr(
        service, "_get_scraper",
        lambda cruise_line, market=None: _FakeNcl(market=market),
    )

    a = await service.get_or_create_scraper(CruiseLine.NCL, market="CA")
    b = await service.get_or_create_scraper(CruiseLine.NCL, market="CA")
    assert a is b


@pytest.mark.asyncio
async def test_omitted_market_reuses_whatever_is_live(monkeypatch):
    """Every pre-existing caller passes no market at all. Those must keep
    reusing the live session rather than tearing it down."""
    from services.booking_service import BookingService

    service = BookingService()

    class _FakeNcl:
        cruise_line = CruiseLine.NCL
        is_alive = True

        def __init__(self, market=None):
            self.market = (market or "US").upper()

        async def start(self, headless=None):
            pass

        async def stop(self):
            raise AssertionError("an omitted market must not force a restart")

    monkeypatch.setattr(
        service, "_get_scraper",
        lambda cruise_line, market=None: _FakeNcl(market=market),
    )

    a = await service.get_or_create_scraper(CruiseLine.NCL, market="CA")
    b = await service.get_or_create_scraper(CruiseLine.NCL)
    assert a is b


# ── the new status must be rendered everywhere ───────────────────


def test_status_has_an_excel_fill_and_sort_position():
    """A status missing from these maps silently falls back to the
    NO_SAVING fill — and a missing sort key is how all 13
    UPGRADE_AVAILABLE hits printed nothing in the 2026-07-31 run."""
    from services.excel_export import _FILLS, _SORT_ORDER

    key = BookingStatus.NOT_ON_THIS_ACCOUNT.value
    assert key in _FILLS
    assert key in _SORT_ORDER
    assert _SORT_ORDER[key] != _SORT_ORDER.get("ERROR")


def test_every_booking_status_is_renderable_in_excel():
    """Guards the whole enum, not just the new member, so the next status
    added cannot silently vanish from reports."""
    from services.excel_export import _FILLS, _SORT_ORDER

    for status in BookingStatus:
        assert status.value in _FILLS, f"{status.value} has no Excel fill"
        assert status.value in _SORT_ORDER, f"{status.value} has no sort position"


# ── the market must survive all the way to the scanning scraper ───


@pytest.mark.asyncio
async def test_market_reaches_the_scraper_the_scan_actually_uses(monkeypatch):
    """CONFIRMED BUG, fixed 2026-08-27. `main.py scan --market CA` built its
    LOGIN scraper on the CA account, then `_run_batch` built a SEPARATE
    scraper for the scan via `_get_scraper(job.cruise_line)` with no market
    — so the flag silently did nothing for the scan and every Canadian
    booking would have come back "Reservation is not found", looking like a
    portal fault rather than the wrong account."""
    from core.models import ScanJob, ScanJobStatus
    from services.booking_service import BookingService

    service = BookingService()
    seen: list = []


    def spy(cruise_line, market=None):
        seen.append(market)
        raise RuntimeError("stop here — we only care which market was requested")

    monkeypatch.setattr(service, "_get_scraper", spy)

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(service, "_save_job_to_db", noop)
    monkeypatch.setattr(service, "_update_job_in_db", noop)

    class _NoCache:
        async def get(self, *a, **kw):
            return None

        async def cleanup_expired(self):
            return 0

    monkeypatch.setattr(service, "cache", _NoCache(), raising=False)

    job = ScanJob(job_id="mkt", booking_ids=["1"], cruise_line=CruiseLine.NCL,
                  status=ScanJobStatus.RUNNING, progress_total=1)
    service._active_jobs[job.job_id] = job
    service._stop_flags[job.job_id] = False

    await service._run_batch(
        job, on_progress=None, bypass_cache=True, raw_dump_dir=None,
        capture_market_data=False, capture_everything=False, on_action=None,
        keep_browser_open=False, headless=True, market="CA",
    )
    assert seen == ["CA"], f"the scan requested market {seen}, not CA"


@pytest.mark.asyncio
async def test_start_scan_accepts_and_forwards_market():
    """The public entry point must expose it, or the CLI flag cannot work."""
    import inspect

    from services.booking_service import BookingService

    for fn in (BookingService.start_scan, BookingService._run_batch,
               BookingService.get_or_create_scraper, BookingService._get_scraper):
        assert "market" in inspect.signature(fn).parameters, f"{fn.__name__} lost `market`"
