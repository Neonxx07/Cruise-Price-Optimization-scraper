"""A successful mid-scan re-login must let the batch carry on.

Neon 2026-09-28: "the esspresso login issue still exists and i needed to
login twice".

The log showed the contradiction plainly::

    15:00:16  batch.session_expired_recovering
    15:00:30  espresso.auto_login        result=OK
    15:00:30  batch.session_recovery_gave_up

auto_login SUCCEEDED and the batch stopped anyway, twice in one afternoon
(15:00 and 16:00). Two separate bugs stacked:

1. `_check_login()` ran in the SAME SECOND as `auto_login()`, while the SSO
   chain (login -> auth -> oauth/callback -> app) was still in flight. It
   sampled a page mid-hop and returned False, so `recovered` was False on a
   session that was actually fine. The same race was fixed in check_booking
   on the 23rd; the recovery path never got it.

2. Even when `recovered` was True the code fell THROUGH into the "could not
   get back in" block, which sets job.error and breaks the batch. The
   comment there read "Fall through: ... the rest of the batch continues" -
   describing behaviour the code did not have. Same class of bug as
   release_booking's one-call-site-against-21-exits.
"""

import ast
import inspect
import io
import tokenize
from pathlib import Path

from services.booking_service import BookingService

SERVICE_PY = Path(__file__).resolve().parents[1] / "services" / "booking_service.py"


def _run_batch_code() -> str:
    """_run_batch source with comments stripped.

    The comments here discuss the bug at length. Matching prose instead of
    code is a mistake this codebase has made four times.
    """
    src = inspect.getsource(BookingService._run_batch)
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT
    )


def _recovered_branch() -> ast.If:
    """The `if recovered:` that guards a SUCCESSFUL re-login.

    Takes the NARROWEST match. ast.walk yields outer nodes first, so a naive
    search returns the enclosing `if is_session_expired_error(e):` block -
    which legitimately contains both the recovery branch AND the gave-up
    `break`, and would make the break assertion below fail against correct
    code.
    """
    tree = ast.parse(SERVICE_PY.read_text(encoding="utf-8"))
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "session_recovered" in ast.dump(ast.Module(body=node.body,
                                                       type_ignores=[]))
    ]
    if not matches:
        raise AssertionError("could not find the successful-recovery branch")
    return min(matches, key=lambda n: n.end_lineno - n.lineno)


def test_a_successful_recovery_continues_the_batch():
    """THE regression guard. Without the `continue` a good re-login still
    killed the run."""
    branch = _recovered_branch()
    body = ast.dump(ast.Module(body=branch.body, type_ignores=[]))
    assert "Continue" in body, (
        "the recovered branch must end in `continue`; falling through lands "
        "in the gave-up block, which breaks the batch")


def test_the_recovered_branch_does_not_break_the_batch():
    branch = _recovered_branch()
    body = ast.dump(ast.Module(body=branch.body, type_ignores=[]))
    assert "Break" not in body


def test_the_login_check_settles_before_it_asks():
    """auto_login returns the moment the form is submitted; the SSO chain
    runs on for seconds afterwards. Asking once, immediately, samples a page
    mid-redirect."""
    code = _run_batch_code()
    assert "_settle_navigation" in code


def test_the_login_check_polls_rather_than_asking_once():
    """One sample of an in-flight redirect chain is not an answer."""
    code = _run_batch_code()
    settle = code.index("_settle_navigation")
    window = code[settle:settle + 700]
    assert "_check_login" in window
    assert "range(" in window, "must retry, not ask a single time"


def test_a_genuine_failure_still_stops_the_batch():
    """FILLED_AWAITING_MFA has no unattended way past it. Recovery must not
    become a loop that pretends to work."""
    code = _run_batch_code()
    assert "session_recovery_gave_up" in code
    gave_up = code.index("session_recovery_gave_up")
    assert "break" in code[gave_up:gave_up + 400]


def test_back_to_back_logouts_still_stop_the_batch():
    """A session that dies again RIGHT AFTER a successful re-login is not a
    transient blip - that is a loop, and hammering a login wall on a
    bot-sensitive account is its own risk.

    REVISED 2026-09-29: the guard used to be "one recovery per batch", which
    also stopped the batch on an hourly drop 61 minutes later - measured, and
    it cost 399 unchecked bookings. The loop guard stays; the count limit
    became a rate limit.
    """
    code = _run_batch_code()
    assert "session_expired_again" in code
    assert "_RECOVERY_MIN_GAP_S" in code
    assert "_RECOVERY_MIN_BOOKINGS" in code


def test_an_interrupted_booking_is_retried_not_skipped():
    """Neon 2026-09-29: "the script just loges in and continue where it
    stopped".

    The booking never failed on its merits - it failed to a logout. Before
    this, `continue` jumped over job.results.append() entirely, so an
    interrupted booking vanished from the run with NO row of any kind.
    Confirmed in the database: 3001010 and 3001008 were interrupted on
    2026-09-29 and their only rows came from a manual re-run afterwards.
    """
    code = _run_batch_code()
    assert "retry_after_recovery_ok" in code
    retry = code.index("retry_after_recovery_ok")
    # the retry must re-run the real check, not fabricate a result
    assert "check_booking" in code[max(0, retry - 900):retry]


def test_a_booking_that_fails_twice_is_not_retried_forever():
    """One attempt. A booking that fails again after a good session is a
    booking problem, not a session problem."""
    code = _run_batch_code()
    assert "retry_after_recovery_failed" in code
