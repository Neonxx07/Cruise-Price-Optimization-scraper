"""A finished scan must be judgeable from the log. (Roadmap P3.2)

THE GAP, 2026-10-01. `batch.complete` carried three fields::

    {"job_id": "af371b8c...", "status": "COMPLETED", "total": 306,
     "event": "batch.complete"}

Nineteen of those in the log, and not one answers "was that run healthy?".
No error count, nothing found, no indication that a run stopped because
nobody was logged in - the one failure a human can fix in ten seconds.

Neon, turning the notifications off on 2026-09-30: *"close the
notifications at all we do not need it we just need a watcher watching the
ciode script project monotiring as a third eye that everything is running
and functioning properly."* A third eye needs something to look at.

DELIBERATELY NOT A NOTIFICATION. Notifications were removed because closing
the GUI looked like a crash and they fired constantly. This writes one line
to the log, which the watchdog parses and a human can read afterwards.

DERIVED FROM `job` ALONE. `_run_batch`'s counters (`session_recoveries`,
`consecutive_failures`) are declared inside the `try`, so they are UNBOUND
if it fails early - and this runs in the `finally`. A summary that raises
there would replace a real failure with a confusing one.
"""

from datetime import datetime, timedelta

import pytest

from core.models import (
    BookingResult,
    BookingStatus,
    CruiseLine,
    ScanJob,
    ScanJobStatus,
)
from services.booking_service import run_summary

LINE = CruiseLine.ESPRESSO


def _job(booking_ids, results=(), started=None, finished=None):
    job = ScanJob(job_id="j", booking_ids=list(booking_ids), cruise_line=LINE,
                  status=ScanJobStatus.COMPLETED,
                  progress_total=len(booking_ids),
                  started_at=started or datetime(2026, 10, 1, 14, 0, 0))
    job.results = list(results)
    job.completed_at = finished
    return job


def _r(booking_id, status, net_saving=0.0, error=None):
    return BookingResult(cruise_line=LINE, status=status, booking_id=booking_id,
                         net_saving=net_saving, error=error)


# -- what it counts ------------------------------------------------------


def test_it_reports_what_was_found():
    summary = run_summary(_job(["a", "b", "c"], [
        _r("a", BookingStatus.OPTIMIZATION, 199.0),
        _r("b", BookingStatus.OPTIMIZATION, 79.0),
        _r("c", BookingStatus.NO_SAVING),
    ]))

    assert summary["optimizations"] == 2
    assert summary["savings"] == 278.0
    assert summary["statuses"]["OPTIMIZATION"] == 2
    assert summary["statuses"]["NO_SAVING"] == 1


def test_it_reports_errors():
    summary = run_summary(_job(["a", "b"], [
        _r("a", BookingStatus.ERROR, error="Timeout 60000ms"),
        _r("b", BookingStatus.NO_SAVING),
    ]))
    assert summary["errors"] == 1


def test_an_unfinished_run_says_so():
    """306 of 723 is a different event from 723 of 723, and the old line
    could not tell them apart."""
    summary = run_summary(_job([str(n) for n in range(723)],
                               [_r(str(n), BookingStatus.NO_SAVING)
                                for n in range(306)]))
    assert summary["requested"] == 723
    assert summary["checked"] == 306
    assert summary["unfinished"] == 417


def test_a_complete_run_has_nothing_unfinished():
    summary = run_summary(_job(["a"], [_r("a", BookingStatus.NO_SAVING)]))
    assert summary["unfinished"] == 0


def test_retries_never_make_unfinished_negative():
    """The retry pass appends to the work list, so more results than
    requested is normal."""
    summary = run_summary(_job(["a"], [
        _r("a", BookingStatus.ERROR, error="timeout"),
        _r("a", BookingStatus.NO_SAVING),
    ]))
    assert summary["unfinished"] == 0


# -- the login case, which is the one worth spotting ---------------------


@pytest.mark.parametrize("error", [
    "Not logged in — please log into ESPRESSO first",
    "Session logged out while searching",
    "login required",
    "please log into ESPRESSO again",
])
def test_a_login_failure_is_called_out(error):
    """The one failure a human fixes in ten seconds, so it gets its own
    field rather than hiding inside the error count."""
    summary = run_summary(_job(["a"], [
        _r("a", BookingStatus.ERROR, error=error)]))
    assert summary["login_blocked"] == 1


def test_an_ordinary_error_is_not_counted_as_a_login_problem():
    summary = run_summary(_job(["a"], [
        _r("a", BookingStatus.ERROR, error="Page.wait_for_selector: Timeout")]))
    assert summary["errors"] == 1
    assert summary["login_blocked"] == 0


# -- timing --------------------------------------------------------------


def test_duration_and_pace_are_reported():
    start = datetime(2026, 10, 1, 14, 0, 0)
    summary = run_summary(_job(
        ["a", "b"],
        [_r("a", BookingStatus.NO_SAVING), _r("b", BookingStatus.NO_SAVING)],
        started=start, finished=start + timedelta(seconds=60)))
    assert summary["duration_s"] == 60.0
    assert summary["avg_s"] == 30.0


def test_an_unfinished_job_reports_no_duration():
    """completed_at is None while a job is still running; inventing a
    duration would be a guess."""
    summary = run_summary(_job(["a"], [_r("a", BookingStatus.NO_SAVING)]))
    assert summary["duration_s"] is None
    assert summary["avg_s"] is None


def test_a_run_that_checked_nothing_does_not_divide_by_zero():
    start = datetime(2026, 10, 1, 14, 0, 0)
    summary = run_summary(_job(["a"], [], started=start,
                               finished=start + timedelta(seconds=5)))
    assert summary["checked"] == 0
    assert summary["avg_s"] is None


# -- it must never break the finally block -------------------------------


def test_an_empty_job_is_summarised_not_rejected():
    summary = run_summary(_job([], []))
    assert summary["checked"] == 0
    assert summary["optimizations"] == 0
    assert summary["savings"] == 0


def test_a_job_with_no_results_attribute_still_summarises():
    """It runs in a finally, after a failure that may have left the job
    half-built."""
    class Bare:
        booking_ids = ["a", "b"]

    summary = run_summary(Bare())
    assert summary["requested"] == 2
    assert summary["checked"] == 0


def test_a_missing_net_saving_is_treated_as_zero():
    result = _r("a", BookingStatus.OPTIMIZATION)
    result.net_saving = None
    assert run_summary(_job(["a"], [result]))["savings"] == 0


# -- it is actually wired in ---------------------------------------------


def test_batch_complete_logs_the_summary():
    """Structural, from the AST: a summary nothing logs is no summary."""
    import ast
    import pathlib

    source = pathlib.Path("services/booking_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_batch = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_batch")

    calls = [n for n in ast.walk(run_batch)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "run_summary"]
    assert calls, "batch.complete no longer carries a run summary"


def test_the_summary_call_is_guarded():
    """It runs inside a finally. Raising there would replace a real
    failure with a confusing one."""
    import ast
    import pathlib

    source = pathlib.Path("services/booking_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_batch = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_batch")

    for node in ast.walk(run_batch):
        if isinstance(node, ast.Try):
            called = [n for n in ast.walk(node)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "run_summary"]
            if called and node.handlers:
                return
    pytest.fail("run_summary is called without a try/except around it")
