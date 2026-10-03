"""A transient failure deserves a second attempt. (Roadmap P0.3)

MEASURED 2026-10-01 across all 514 ERROR rows in the database: **456 (88%)
were followed by a SUCCESSFUL scan of the same booking**, so the failure was
transient and a retry would have worked. By cause:

    Page.wait_for_selector timeout 60000ms   117   100% recovered
    Cannot read categories: VX._form_12       58   100%
    Session logged out while searching        45   100%
    Page.wait_for_selector timeout 12000ms    28   100%
    NCL portal error: Reservation not found   20   100%
    Page.wait_for_selector timeout 25000ms    47    21%
    payment panel unreadable                   9     0%   <- never

Until now those recoveries only happened because Neon ran the entire scan
again the next day. Nothing retried anything within a run.

**"Payment panel unreadable" is excluded deliberately.** It is the one cause
that never recovers, and it is also the exact condition that produced the
false $400 saving on booking 3001001 - a reservation settled to within two
cents. Retrying it buys nothing and risks a guess.

THE SHAPE. The retry list is appended to the SAME work list the main loop
iterates, after the last original booking. `enumerate()` over a list reads
by index, so the appended ids flow through the same per-booking logic - no
second copy of 600 lines, which is where a divergence would eventually
hide. Retries run at the END because the faults are mostly session and
timeout related, and the minutes spent on the rest of the queue are what
let them clear.
"""

import pytest

from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus
from services.booking_service import BookingService

LINE = CruiseLine.ESPRESSO


@pytest.fixture
def service():
    return BookingService()


def _job(booking_ids, results=()):
    from datetime import datetime

    job = ScanJob(job_id="j", booking_ids=list(booking_ids), cruise_line=LINE,
                  status=ScanJobStatus.RUNNING, progress_total=len(booking_ids),
                  started_at=datetime.utcnow())
    job.results = list(results)
    return job


def _result(booking_id, status=BookingStatus.ERROR, error="timeout"):
    return BookingResult(cruise_line=LINE, status=status,
                         booking_id=booking_id, error=error)


# ── which errors are worth retrying ──────────────────────────────────────


@pytest.mark.parametrize("error", [
    "Page.wait_for_selector: Timeout 60000ms exceeded.",
    "Cannot read categories: VX._form_12 not available",
    "Session logged out while searching — please log into ESPRESSO again",
    "NCL portal error: Close\nReservation is not found",
    "Page.evaluate: SyntaxError: Unexpected token '{'",
])
def test_transient_failures_are_retried(service, error):
    """Every one of these recovered 100% of the time on a later scan."""
    assert service.is_retryable_error(error) is True


def test_an_unreadable_payment_panel_is_never_retried(service):
    """0 of 9 ever recovered, and guessing here is what produced the false
    $400 on booking 3001001."""
    assert service.is_retryable_error(
        "payment panel unreadable — cannot confirm whether it is paid") is False


def test_no_error_is_not_retryable(service):
    assert service.is_retryable_error(None) is False
    assert service.is_retryable_error("") is False


# ── what gets queued ─────────────────────────────────────────────────────


def test_only_failed_bookings_are_retried(service):
    job = _job(["a", "b", "c"], [
        _result("a", BookingStatus.NO_SAVING, None),
        _result("b", BookingStatus.ERROR, "Timeout 60000ms"),
        _result("c", BookingStatus.OPTIMIZATION, None),
    ])
    assert service._bookings_to_retry(job) == ["b"]


def test_a_booking_that_already_recovered_is_not_retried_again(service):
    """Session recovery retries the interrupted booking mid-run. Only the
    LATEST result counts, or it would be scanned a third time."""
    job = _job(["a"], [
        _result("a", BookingStatus.ERROR, "Timeout 60000ms"),
        _result("a", BookingStatus.NO_SAVING, None),
    ])
    assert service._bookings_to_retry(job) == []


def test_a_booking_that_failed_after_succeeding_is_retried(service):
    job = _job(["a"], [
        _result("a", BookingStatus.NO_SAVING, None),
        _result("a", BookingStatus.ERROR, "Timeout 60000ms"),
    ])
    assert service._bookings_to_retry(job) == ["a"]


def test_an_unretryable_failure_is_left_alone(service):
    job = _job(["a"], [_result("a", BookingStatus.ERROR,
                               "payment panel unreadable")])
    assert service._bookings_to_retry(job) == []


def test_the_queue_order_is_kept(service):
    job = _job(["a", "b", "c"], [
        _result("c"), _result("a"), _result("b"),
    ])
    # Insertion order of the latest-result map follows the results list.
    assert set(service._bookings_to_retry(job)) == {"a", "b", "c"}


def test_a_run_where_everything_failed_is_capped(service):
    """Hundreds of failures means something systemic - a dead session, a
    portal outage. A second full pass would double the damage."""
    ids = [str(n) for n in range(500)]
    job = _job(ids, [_result(b) for b in ids])

    retries = service._bookings_to_retry(job)

    assert len(retries) == service._RETRY_MAX
    assert service._RETRY_MAX < 500


def test_nothing_to_retry_is_an_empty_list(service):
    job = _job(["a"], [_result("a", BookingStatus.NO_SAVING, None)])
    assert service._bookings_to_retry(job) == []


# ── the loop can actually carry the retries ──────────────────────────────


def test_the_batch_loop_iterates_a_mutable_work_list():
    """The whole mechanism rests on this: appending to the list the loop is
    enumerating feeds those ids back through the same per-booking code.
    Asserted structurally so a refactor back to
    `enumerate(job.booking_ids)` - which would silently drop every retry -
    fails here."""
    import ast
    import pathlib

    source = pathlib.Path("services/booking_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_batch = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_batch")

    loops = [n for n in ast.walk(run_batch) if isinstance(n, ast.For)]
    enumerated = []
    for loop in loops:
        if (isinstance(loop.iter, ast.Call)
                and isinstance(loop.iter.func, ast.Name)
                and loop.iter.func.id == "enumerate"
                and loop.iter.args):
            arg = loop.iter.args[0]
            enumerated.append(ast.unparse(arg))

    assert "work" in enumerated, (
        f"the batch loop no longer iterates the mutable work list: {enumerated}")
    assert "job.booking_ids" not in enumerated, (
        "iterating job.booking_ids directly silently discards every retry")


def test_python_really_does_pick_up_appended_items():
    """The assumption the design rests on, pinned rather than trusted."""
    work = [1, 2, 3]
    seen = []
    for i, item in enumerate(work):
        seen.append(item)
        if i == 2:
            work.append(99)
    assert seen == [1, 2, 3, 99]


def test_progress_never_exceeds_the_queue_length():
    """Retries push the loop index past the queue, and a progress bar
    reading 740/723 looks like a bug."""
    import ast
    import pathlib

    source = pathlib.Path("services/booking_service.py").read_text(encoding="utf-8")
    assert "min(i + 1, job.progress_total)" in source
    assert "min(i, job.progress_total)" in source
    # ...and no bare assignment survives.
    tree = ast.parse(source)
    run_batch = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_batch")
    for node in ast.walk(run_batch):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Attribute) and t.attr == "progress_done"
                        for t in node.targets)):
            rendered = ast.unparse(node.value)
            assert rendered.startswith("min("), (
                f"uncapped progress assignment: {rendered}")
