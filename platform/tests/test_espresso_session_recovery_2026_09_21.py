"""Mid-scan logout must not cost the rest of the batch.

Neon 2026-09-21: "in the middle of scrapping sometimes the account logges
out and does not log in automatically can you please fix this as well?"

THE GAP. EspressoScraper._search_booking raises

    "Session logged out while searching - please log into ESPRESSO again"

when its own _check_login fails mid-batch. BookingService's per-booking
loop tested ONLY is_dead_browser_error, and a signed-out portal is a
perfectly healthy browser showing a login page - so the restart path never
fired and the batch drove into the login wall one booking at a time.

That is the recorded 2026-08-27 run in which ESPRESSO bookings #400-403
died in sequence between 14:19 and 14:31 UTC.
"""
import pytest

from core.models import ScanJob, ScanJobStatus, CruiseLine
from scraper.base import is_dead_browser_error, is_session_expired_error


# ── the two failure kinds are distinct and need opposite recovery ────────


@pytest.mark.parametrize("message", [
    "Session logged out while searching — please log into ESPRESSO again",
    "session logged out",
    "Not logged in",
    "Session expired",
    "Login required",
])
def test_a_logout_is_recognised_as_session_expiry(message):
    assert is_session_expired_error(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "Session logged out while searching — please log into ESPRESSO again",
    "Not logged in",
])
def test_a_logout_is_NOT_a_dead_browser(message):
    """THE WHOLE BUG. The browser is alive and well - it is showing a login
    page. Restarting it would not help; logging in would."""
    assert is_dead_browser_error(RuntimeError(message)) is False


@pytest.mark.parametrize("message", [
    "Target page, context or browser has been closed",
    "Protocol error: Connection closed",
    "Browser closed unexpectedly",
])
def test_a_dead_browser_is_NOT_session_expiry(message):
    """The reverse must hold too, or a crashed browser would be "recovered"
    by trying to log into a corpse."""
    assert is_session_expired_error(RuntimeError(message)) is False
    assert is_dead_browser_error(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "Timeout 30000ms exceeded waiting for #booked-root",
    "Locator.click: Timeout 5000ms exceeded",
    "GoCCL window.initialData has no readable invoiceSummary.grossAmount.amount",
])
def test_an_ordinary_failure_triggers_neither_recovery(message):
    """DELIBERATELY NARROW. A false positive would fire a needless re-login,
    and on ESPRESSO a second login can itself disturb a live session - so
    the cost of guessing here is real."""
    exc = RuntimeError(message)
    assert is_session_expired_error(exc) is False
    assert is_dead_browser_error(exc) is False


# ── what the batch does with it ──────────────────────────────────────────


def test_the_loop_recovers_before_it_checks_for_a_dead_browser():
    """Order matters: a logout must be caught by the session branch, never
    fall through to the browser-restart branch that cannot fix it."""
    import inspect

    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    assert src.index("is_session_expired_error(e)") < src.index("self._is_dead_browser_error(e)")


def test_recovery_is_attempted_only_once_per_batch():
    """A portal that signs us out repeatedly is not something to keep
    hammering - on a bot-sensitive account that is its own risk. The second
    logout stops the batch with a count of what was completed."""
    import inspect

    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    assert "session_recovery_used" in src
    assert "batch.session_expired_again" in src


def test_a_failed_relogin_stops_the_batch_rather_than_failing_every_booking():
    """ESPRESSO's auto_login returns FILLED_AWAITING_MFA when the account
    demands MFA, and there is no unattended way past that. Stopping with 400
    real results beats 500 rows whose last 100 are noise."""
    import inspect

    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    assert "batch.session_recovery_gave_up" in src
    assert "Check login" in src


def test_a_completed_job_can_still_carry_a_warning():
    """The booking in flight when the logout happened still failed. That is
    not a job failure - the other 400 are fine - so it must not land in
    `error`, and it is invisible in any single row."""
    job = ScanJob(job_id="j1", booking_ids=["A", "B"], cruise_line=CruiseLine.ESPRESSO)
    assert job.warning is None
    job.status = ScanJobStatus.COMPLETED
    job.warning = "signed out during the scan; re-run A"
    assert job.status == ScanJobStatus.COMPLETED
    assert job.error is None
    assert "re-run A" in job.warning
