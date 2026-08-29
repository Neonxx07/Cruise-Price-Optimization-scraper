"""Tests for the 2026-08-27 fixes driven by two real incidents.

1. **Pre-flight session check.** In the real 506-booking ESPRESSO run of
   2026-08-27, bookings #400-403 (3000041, 3000063, 3000062, 3000044) all
   died with "Session logged out while searching" between 14:19 and 14:31
   UTC. Nothing verified the session was usable before the batch started —
   the batch drove into the portal and found out one booking at a time.
   Neon separately hit the same class of problem on the GUI: he confirmed
   a login, pressed Start, and the portal made him log in again.

2. **Watchlist file loading.** Neon put NCL booking IDs in
   `Watchlistncl.txt`, pressed Start, and nothing ran. The GUI read NO
   watchlist file at all (verified: the only "watchlist" matches under
   `gui/` were code comments), so the queue stayed empty and `_on_start`
   returned at its "No bookings" guard. `main.py` and
   `run_persistent_watchlist_scan.py` both work from watchlist files.
"""
import pytest

from core.models import (
    BookingResult,
    BookingStatus,
    CruiseLine,
    ScanJob,
    ScanJobStatus,
)


def _ok_result(booking_id, cruise_line):
    return BookingResult(
        booking_id=booking_id, cruise_line=cruise_line,
        status=BookingStatus.NO_SAVING, confidence=50,
    )


# ── ScanJob.error ────────────────────────────────────────────────


def test_scan_job_can_carry_a_failure_reason():
    """A FAILED job used to carry its status but not its REASON, so the GUI
    could only say "SCAN FAILED" and the operator had to read the log to
    learn whether the browser died, the session logged out, or a restart
    failed. Setting an undefined attribute on a pydantic model raises, so
    this needs a real field."""
    job = ScanJob(job_id="j1", booking_ids=["A"], cruise_line=CruiseLine.NCL)
    assert job.error is None
    job.error = "session is not logged in any more"
    assert job.error == "session is not logged in any more"


# ── pre-flight session check ─────────────────────────────────────


class _FakeScraperBase:
    """Minimal stand-in for a live scraper on the keep_browser_open path."""

    cruise_line = CruiseLine.ESPRESSO

    def __init__(self):
        self.raw_dump_dir = None
        self.capture_everything = False
        self.on_action = None
        self.checked_bookings: list[str] = []

    async def check_booking(self, booking_id, **kw):
        self.checked_bookings.append(booking_id)
        raise AssertionError(
            "check_booking must NOT be reached when the pre-flight session "
            "check fails — that is the whole point of the guard"
        )


class _LoggedOutScraper(_FakeScraperBase):
    async def _check_login(self):
        return False


class _LoggedInScraper(_FakeScraperBase):
    async def _check_login(self):
        return True

    async def check_booking(self, booking_id, **kw):
        # Returns cleanly rather than raising: a raise sends the batch into
        # retry_async's exponential backoff for EVERY booking, which made
        # this file take four minutes to run.
        self.checked_bookings.append(booking_id)
        return _ok_result(booking_id, self.cruise_line)


class _BrokenProbeScraper(_FakeScraperBase):
    async def _check_login(self):
        raise RuntimeError("probe itself exploded")

    async def check_booking(self, booking_id, **kw):
        self.checked_bookings.append(booking_id)
        return _ok_result(booking_id, self.cruise_line)


async def _run_batch_with(monkeypatch, scraper, booking_ids=("A", "B", "C")):
    from services.booking_service import BookingService

    service = BookingService()

    async def fake_get_or_create(cruise_line, headless=None, market=None):
        #  is REQUIRED in this signature: BookingService now threads
        # it through so an NCL Canada scan cannot silently run on the US
        # account. A fake that omits it raises TypeError inside _run_batch,
        # which the outer handler converts into a FAILED job with no
        # `error` set — masking whatever the test was really checking.
        return scraper

    monkeypatch.setattr(service, "get_or_create_scraper", fake_get_or_create)

    class _NoCache:
        async def get(self, *a, **kw):
            return None

        async def cleanup_expired(self):
            return 0

        async def set(self, *a, **kw):
            return None

    monkeypatch.setattr(service, "cache", _NoCache(), raising=False)

    async def noop(*a, **kw):
        return None

    # raising=True on purpose. The original version of this helper patched
    # a NON-EXISTENT "_persist_result" with raising=False, which silently
    # did nothing and let these tests write 78 junk rows into the real
    # cruise_intel.db (see tests/conftest.py). Naming the real methods and
    # letting a typo RAISE is the difference between a loud test failure
    # and silent production data corruption.
    monkeypatch.setattr(service, "_save_job_to_db", noop)
    monkeypatch.setattr(service, "_update_job_in_db", noop)
    monkeypatch.setattr(service, "_save_result_to_db", noop)
    monkeypatch.setattr(service, "_save_price_history", noop)
    monkeypatch.setattr(service, "_save_market_data_to_db", noop)

    # The real inter-booking delay is a politeness pause against the live
    # portal; leaving it in made this file take 4m22s. Zero it out.
    from config.settings import settings as _settings
    monkeypatch.setattr(_settings, "scraper_interbooking_delay_min_s", 0, raising=False)
    monkeypatch.setattr(_settings, "scraper_interbooking_delay_max_s", 0, raising=False)

    job = ScanJob(
        job_id="job-preflight",
        booking_ids=list(booking_ids),
        cruise_line=scraper.cruise_line,
        status=ScanJobStatus.RUNNING,
        progress_total=len(booking_ids),
    )
    service._active_jobs[job.job_id] = job
    service._stop_flags[job.job_id] = False

    await service._run_batch(
        job, on_progress=None, bypass_cache=True, raw_dump_dir=None,
        capture_market_data=False, capture_everything=False, on_action=None,
        keep_browser_open=True, headless=None,
    )
    return job


@pytest.mark.asyncio
async def test_logged_out_session_fails_the_batch_before_any_booking(monkeypatch):
    """THE incident: rather than erroring booking after booking against a
    logged-out portal (a burst of failures on a bot-detection-sensitive
    account), refuse up front."""
    scraper = _LoggedOutScraper()
    job = await _run_batch_with(monkeypatch, scraper)

    assert job.status == ScanJobStatus.FAILED
    assert scraper.checked_bookings == [], "no booking may be attempted"
    assert job.error is not None
    assert "not logged in" in job.error.lower()
    # The message must be actionable and state the real scope of the damage.
    assert "0 of 3" in job.error
    assert "Check login" in job.error


@pytest.mark.asyncio
async def test_logged_in_session_is_not_blocked(monkeypatch):
    """The guard must not over-block — a good session proceeds to scanning."""
    scraper = _LoggedInScraper()
    await _run_batch_with(monkeypatch, scraper)
    assert scraper.checked_bookings, "a logged-in session must reach check_booking"


@pytest.mark.asyncio
async def test_a_broken_login_probe_does_not_block_the_batch(monkeypatch):
    """A failing CHECK is not a failing SESSION. If the probe itself raises,
    fail OPEN — same reasoning as the cache-read guard in this loop, which
    also fails open because a redundant live check costs a page load while a
    wrongly-skipped one costs a client's price drop."""
    scraper = _BrokenProbeScraper()
    job = await _run_batch_with(monkeypatch, scraper)
    assert scraper.checked_bookings, "a broken probe must not stop the scan"
    assert job.error is None


# ── watchlist file loading ───────────────────────────────────────


@pytest.fixture
def qm():
    from gui.queue_manager import BookingQueueManager
    return BookingQueueManager()


def test_loads_booking_ids_from_a_file(qm, tmp_path):
    f = tmp_path / "Watchlistncl.txt"
    f.write_text("12345678\n87654321\n11112222\n", encoding="utf-8")
    added, error = qm.add_bookings_from_file(str(f))
    assert error is None
    assert added == ["12345678", "87654321", "11112222"]
    assert qm.get_snapshot().queued == 3


def test_comma_separated_file_also_works(qm, tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("111,222, 333\n444\n", encoding="utf-8")
    added, error = qm.add_bookings_from_file(str(f))
    assert error is None
    assert added == ["111", "222", "333", "444"]


def test_empty_file_is_reported_explicitly_not_as_a_silent_noop(qm, tmp_path):
    """Neon's Watchlistncl.txt was 0 bytes. A silent "0 added" is
    indistinguishable from a broken Start button — say so plainly."""
    f = tmp_path / "Watchlistncl.txt"
    f.write_text("", encoding="utf-8")
    added, error = qm.add_bookings_from_file(str(f))
    assert added == []
    assert error is not None
    assert "empty" in error.lower()
    assert "Watchlistncl.txt" in error


def test_whitespace_only_file_counts_as_empty(qm, tmp_path):
    f = tmp_path / "blank.txt"
    f.write_text("\n\n   \n", encoding="utf-8")
    added, error = qm.add_bookings_from_file(str(f))
    assert added == []
    assert "empty" in error.lower()


def test_missing_file_returns_an_error_instead_of_raising(qm, tmp_path):
    added, error = qm.add_bookings_from_file(str(tmp_path / "nope.txt"))
    assert added == []
    assert error is not None
    assert "could not read" in error.lower()


def test_all_duplicates_reports_that_nothing_was_new(qm, tmp_path):
    f = tmp_path / "dupes.txt"
    f.write_text("555\n555\n", encoding="utf-8")
    added, error = qm.add_bookings_from_file(str(f))
    assert added == ["555"]
    assert error is None
    added2, error2 = qm.add_bookings_from_file(str(f))
    assert added2 == []
    assert error2 is not None and "no NEW booking" in error2


def test_last_job_error_is_none_before_any_job_runs(qm):
    assert qm.last_job_error is None
    assert qm.last_job_status is None
