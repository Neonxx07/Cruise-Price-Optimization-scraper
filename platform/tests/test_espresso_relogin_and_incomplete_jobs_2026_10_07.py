"""A dropped session must not end the run, and a run that abandoned
bookings must not call itself COMPLETED.

Neon 2026-10-07: *"there s stilll bookings not scanned in this run can u
double check them and also we are still having the esspresso login bug
issue idk when u will fix it"*

ONE INCIDENT, TWO DEFECTS. Job 104 was given 769 bookings and checked 451.
The log tells the whole story without any guessing:

    14:45:59  gui.start_auto_login_ok                 <- a login DID work
    16:47:41  login.required   .../login
    16:47:47  batch.error 3001037 "Not logged in"
    16:49:31  batch.complete  status=COMPLETED checked=451 requested=769
    17:28:14  gui.scan_suppressed  minutes_remaining=81   <- Neon retrying

318 bookings were left: 76 of them permanently excluded (correct - they are
reported from the database), and **242 genuinely never looked at**.

DEFECT 1 - NOTHING EVER TRIED TO LOG BACK IN. `_check_login` is a detector
and was wired straight to a raise. `auto_login()` had existed all along and
was called from exactly one place: the GUI's Start button. Across the whole
log, auto_login returns

    OK                  19        fully unattended
    ALREADY_LOGGED_IN    8
    FILLED_AWAITING_MFA 13        a human must type a code
    NO_LOGIN_FORM        1

so roughly two thirds of session drops are recoverable with nobody at the
desk - including the 14:45 login on this very run.

DEFECT 2 - "COMPLETED" MEANT "THE LOOP ENDED". Two mechanisms key off that
word and both were misled:

  * `recent_identical_scan` refuses a rescan for two hours after a
    COMPLETED run, which is why Neon's 17:28 retry was turned away on the
    strength of a run that never touched those bookings.
  * `resumable_jobs` looks for `status != COMPLETED`, so the job most worth
    resuming was invisible to it.

Both docstrings already said an interrupted run must not count. One line
made it count anyway.

SCALE, measured in the live database: **13 ESPRESSO jobs back to
2026-09-22 were marked COMPLETED while abandoning bookings** - two of them
having checked 1 of 723. Not one ESPRESSO run in three weeks had genuinely
finished. They are relabelled INCOMPLETE; only the status column was
touched and no result was altered or removed.
"""

import asyncio
import inspect

from core.models import ScanJobStatus
from scraper.espresso import EspressoScraper


# -- a scraper stub ------------------------------------------------------
#
# _ensure_logged_in touches only _check_login, auto_login, page.url and the
# MFA flags, so the real method can be exercised on a bare instance.


class _Page:
    url = "https://secure.cruisingpower.com/login"


def _scraper(check_results, login_result="OK"):
    """`check_results` is consumed one per _check_login() call."""
    s = EspressoScraper.__new__(EspressoScraper)
    s._page = _Page()
    s._mfa_wait_exhausted = False
    s._MFA_WAIT_SECONDS = 0.3
    s._MFA_POLL_SECONDS = 0.05
    # log_action's needs: since 2026-10-08 every recovery step is also
    # reported to the GUI through it.
    s.action_log = []
    s.raw_dump_dir = None
    s.on_action = None
    s.calls = []
    results = list(check_results)

    async def _check_login():
        s.calls.append("check")
        return results.pop(0) if results else False

    logins = list(login_result) if isinstance(login_result, list) else None

    async def _auto_login():
        s.calls.append("login")
        if logins is not None:
            return logins.pop(0) if logins else "ERROR: no more"
        return login_result

    # Since 2026-10-15 a re-login is proven on the reservations page
    # (_prove_session), consuming the same result list as _check_login.
    async def _prove_session():
        s.calls.append("prove")
        return (results.pop(0) if results else False), "https://stub"

    s._check_login = _check_login
    s.auto_login = _auto_login
    s._prove_session = _prove_session
    return s


def _run(scraper):
    return asyncio.run(scraper._ensure_logged_in())


# -- the recovery itself -------------------------------------------------


def test_a_live_session_costs_nothing():
    """The happy path must not call auto_login at all - re-submitting a
    login over a live one is what ESPRESSO punishes with a single-session
    kick."""
    s = _scraper([True])
    assert _run(s) is True
    assert s.calls == ["check"]


def test_a_dropped_session_logs_back_in_and_resumes():
    """THE FIX. Before this, the same situation raised and the run died."""
    s = _scraper([False, True], login_result="OK")
    assert _run(s) is True
    assert "login" in s.calls


def test_a_successful_login_is_verified_not_believed():
    """auto_login's own docstring records a false ALREADY_LOGGED_IN that
    made it do nothing at all. A claim of success is re-checked, and if the
    re-check disagrees the recovery has failed."""
    s = _scraper([False, False], login_result="OK")
    assert _run(s) is False
    # 2026-10-15: proven on reservations, and one more login when the proof
    # fails (what Neon was doing by hand) - never more than one.
    assert s.calls == ["check", "login", "prove", "login", "prove"]


def test_a_login_that_does_not_hold_is_redone_once_unattended():
    """Neon 2026-10-15: "i need to log in manually usually twice". 14 Oct
    17:34:17 auto_login OK -> 17:35:47 the login form again; the second
    login held. Now the scraper does the second login itself."""
    s = _scraper([False, False, True], login_result="OK")
    assert _run(s) is True
    assert s.calls == ["check", "login", "prove", "login", "prove"]


def test_a_second_login_that_asks_for_mfa_waits_for_the_human():
    s = _scraper([False, False, False, True], login_result=["OK", "FILLED_AWAITING_MFA"])
    assert _run(s) is True
    assert s.calls[:5] == ["check", "login", "prove", "login", "check"]


def test_already_logged_in_is_accepted_when_it_verifies():
    s = _scraper([False, True], login_result="ALREADY_LOGGED_IN")
    assert _run(s) is True


# -- MFA: bounded, honest, and only paid for once ------------------------


def test_mfa_waits_for_the_human_and_resumes():
    """The credential is already in the form; only the code is missing.
    Waiting is strictly better than abandoning 242 bookings."""
    s = _scraper([False, False, True], login_result="FILLED_AWAITING_MFA")
    assert _run(s) is True


def test_an_unattended_mfa_challenge_gives_up_instead_of_hanging():
    s = _scraper([False] * 40, login_result="FILLED_AWAITING_MFA")
    assert _run(s) is False
    assert s._mfa_wait_exhausted is True


def test_the_mfa_wait_is_paid_for_once_per_scan_not_once_per_booking():
    """Three minutes x 318 remaining bookings is not a recovery, it is a
    hang. Once a wait has timed out the operator is demonstrably away."""
    s = _scraper([False] * 40, login_result="FILLED_AWAITING_MFA")
    _run(s)
    s.calls.clear()
    results = [False, False]

    async def _check_login():
        s.calls.append("check")
        return results.pop(0) if results else False

    s._check_login = _check_login
    assert _run(s) is False
    # one pre-check, one auto_login, and NO polling loop
    assert s.calls == ["check", "login"]


def test_a_missing_credential_is_not_retried_on_every_booking():
    """NO_CREDENTIALS_SAVED cannot be fixed by trying again."""
    s = _scraper([False], login_result="NO_CREDENTIALS_SAVED")
    assert _run(s) is False
    assert s.calls == ["check", "login"]


def test_a_crashing_login_never_propagates():
    """Recovery must not become a new way to kill a run."""
    s = _scraper([False])

    async def _boom():
        raise RuntimeError("playwright is gone")

    s.auto_login = _boom
    assert _run(s) is False


# -- the call sites actually use it --------------------------------------


def test_check_booking_recovers_instead_of_raising():
    """The defect was not a missing capability, it was a missing CALL.
    Both pre-flight checks must go through the recovery. (check_booking is
    a thin release-guaranteeing wrapper; the work is in the inner.)"""
    src = inspect.getsource(EspressoScraper._check_booking_inner)
    assert "_ensure_logged_in" in src, (
        "the scan is back to detecting a logout without recovering - "
        "that abandoned 242 bookings on 2026-10-07")
    raises = src.count("raise RuntimeError(\"Not logged in")
    assert src.count("_ensure_logged_in") >= raises, (
        "a 'Not logged in' raise still has a bare _check_login in front "
        "of it")


# -- a job that abandoned bookings is not COMPLETED ----------------------


# -- an unrecoverable session ends the run ------------------------------
#
# _ensure_logged_in rescues the common case, but it makes the HOPELESS
# case slower: a portal that is genuinely down would be handed every
# remaining booking in turn, each paying for a full login attempt. On
# 2026-10-07 that would have been 318 bookings producing nothing but ERROR
# rows for over an hour. There was no abort before this - only a cooldown
# (3 failures -> 120s) that slows the grinding without stopping it.


def test_login_shaped_errors_are_told_apart_from_ordinary_ones():
    from services.booking_service import _is_login_shaped

    assert _is_login_shaped("Not logged in — please log into ESPRESSO first")
    assert _is_login_shaped("Session logged out while searching")
    assert not _is_login_shaped(
        "Page.wait_for_selector: Timeout 25000ms exceeded")
    assert not _is_login_shaped(None)
    assert not _is_login_shaped("")


def test_an_ordinary_run_of_timeouts_does_not_abort_the_scan():
    """The give-up rule must not fire on bad luck. Timeouts recovered 100%
    of the time across 514 measured ERROR rows."""
    from services.booking_service import _is_login_shaped

    timeouts = ["Page.wait_for_selector: Timeout 60000ms exceeded"] * 10
    assert not any(_is_login_shaped(e) for e in timeouts)


def test_the_give_up_threshold_accounts_for_recovery_already_having_failed():
    from services.booking_service import BookingService

    assert BookingService._LOGIN_GIVE_UP_AFTER == 3

    src = inspect.getsource(BookingService._run_batch)
    assert "_LOGIN_GIVE_UP_AFTER" in src, "nothing ever gives up"
    assert "batch.session_unrecoverable" in src


def test_giving_up_leaves_the_bookings_unchecked_rather_than_failed():
    """The difference matters: an unchecked booking is resumed, an ERROR
    booking is a reported result. The run must break out of the loop, not
    keep producing results."""
    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    marker = src.index("batch.session_unrecoverable")
    tail = src[marker:marker + 600]
    assert "break" in tail, (
        "the run logs that the session is unrecoverable and then carries "
        "on anyway")


def test_the_operator_is_given_the_reason_not_just_a_count():
    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    marker = src.index("batch.session_unrecoverable")
    head = src[max(0, marker - 700):marker]
    assert "job.error" in head, "the reason is logged but never shown"
    assert "press" in head.lower() or "Start" in head


def test_incomplete_is_a_real_status():
    assert ScanJobStatus.INCOMPLETE.value == "INCOMPLETE"


def test_a_job_with_unchecked_bookings_is_not_marked_completed():
    from services.booking_service import BookingService

    src = inspect.getsource(BookingService._run_batch)
    marker = src.index("ScanJobStatus.STOPPED, ScanJobStatus.FAILED)")
    tail = src[marker:marker + 2000]
    assert "ScanJobStatus.INCOMPLETE" in tail, (
        "the batch still records COMPLETED however many bookings it left - "
        "that is what suppressed Neon's 17:28 retry")


def test_a_retried_booking_cannot_paper_over_one_that_was_never_reached():
    """CORRECTION to the first version of this fix, same day.

    It counted `len(job.results)`, and the retry pass appends a SECOND
    result for every booking it re-attempts. A run that retried five
    bookings and never reached five others therefore had 769 results for
    769 requested and called itself COMPLETED - reintroducing the exact
    bug, by a different route.
    """
    from services.booking_service import run_summary

    class _R:
        def __init__(self, booking_id):
            self.booking_id = booking_id
            self.status = "NO_SAVING"
            self.net_saving = 0
            self.error = None

    class _Job:
        booking_ids = [str(i) for i in range(10)]
        # 0-4 scanned, 0-4 retried (duplicates), 5-9 never reached
        results = [_R(str(i)) for i in range(5)] + [_R(str(i)) for i in range(5)]
        started_at = completed_at = None

    summary = run_summary(_Job())
    assert summary["requested"] == 10
    assert summary["checked"] == 5, "duplicate retry results were counted as coverage"
    assert summary["unfinished"] == 5
    assert summary["attempts"] == 10, "the retry pass should still be visible"


def test_a_genuinely_complete_run_still_reports_zero_unfinished():
    from services.booking_service import run_summary

    class _R:
        def __init__(self, booking_id):
            self.booking_id = booking_id
            self.status = "NO_SAVING"
            self.net_saving = 0
            self.error = None

    class _Job:
        booking_ids = [str(i) for i in range(10)]
        results = [_R(str(i)) for i in range(10)]
        started_at = completed_at = None

    assert run_summary(_Job())["unfinished"] == 0


def test_suppression_only_ever_reuses_a_genuinely_completed_run():
    """`recent_identical_scan` must keep requiring COMPLETED. With
    INCOMPLETE now written for abandoned runs, this one line is what stops
    a half-finished scan blocking its own retry for two hours."""
    from services.booking_service import BookingService

    src = inspect.getsource(BookingService.recent_identical_scan)
    assert "ScanJobStatus.COMPLETED.value" in src


def test_resumable_jobs_sees_an_incomplete_run():
    """It selects `status != COMPLETED`, so INCOMPLETE qualifies without
    any further change - the status was the whole bug."""
    from services.booking_service import BookingService

    src = inspect.getsource(BookingService.resumable_jobs)
    assert "!= ScanJobStatus.COMPLETED.value" in src


def test_the_operator_is_told_which_bookings_were_missed():
    """The previous wording for this case was "Shouldn't happen", and the
    run before it claimed completeness outright."""
    import pathlib

    src = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    assert 'job_status == "INCOMPLETE"' in src
    branch = src[src.index('job_status == "INCOMPLETE"'):][:900]
    assert "NOT checked" in branch
    assert "Press Start again" in branch
