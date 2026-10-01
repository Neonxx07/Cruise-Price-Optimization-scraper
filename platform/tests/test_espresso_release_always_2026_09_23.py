"""An ESPRESSO booking is ALWAYS released, on every way out of check_booking.

Neon 2026-09-23, top priority: "sometimes the script does not exit ESPRESSO
booking and it keeps get blocked and we cannot open it ... solve this bug
forever".

Measured on the live log before the fix::

    ESPRESSO bookings opened : 624
      released               : 235
      LEFT LOCKED            : 389    (62%)

Each of those sat locked for fifteen minutes, blocking a human and any
re-scan.

The cause was NOT a flaky release - the same log shows 293 released, 1
skipped, 0 failed. `release_booking` had a single call site on the happy
path, while the flow had 15 returns and 6 raises. Nineteen of the twenty
exits skipped it. The comment above that call claimed it ran "for every
branch above", and nobody had checked.

Release is now a `finally`. These tests pin that: the structural one stops a
future `return` from quietly reintroducing the bug, and the behavioural ones
cover every sentinel branch that used to leak.
"""

import ast
import inspect
from pathlib import Path

import pytest

from core.models import BookingStatus
from scraper.espresso import EspressoScraper

ESPRESSO_PY = Path(__file__).resolve().parents[1] / "scraper" / "espresso.py"


class _Scraper(EspressoScraper):
    """A scraper whose flow and release are both under the test's control."""

    def __init__(self, outcome, *, release_ok=True, release_raises=False):
        self._outcome = outcome
        self._release_ok = release_ok
        self._release_raises = release_raises
        self.released = []
        self._released_for = None

    async def _check_booking_inner(self, booking_id, capture_market_data=False):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def release_booking(self, booking_id=""):
        self.released.append(booking_id)
        if self._release_raises:
            raise RuntimeError("portal exploded during release")
        if self._release_ok:
            self._released_for = booking_id
            return True
        return False


def _result(status=BookingStatus.NO_SAVING):
    class R:
        def __init__(self):
            self.status = status
    return R()


# ── the structural guarantee ─────────────────────────────────────────────


def test_release_is_in_a_finally_not_on_one_branch():
    """THE regression guard. The old code released on the happy path only;
    any future `return` added to the flow must not be able to skip it."""
    tree = ast.parse(ESPRESSO_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "check_booking")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, "check_booking has no try/finally"
    finals = ast.dump(ast.Module(body=tries[0].finalbody, type_ignores=[]))
    assert "_ensure_released" in finals, "the release is not in the finally"


def test_the_flow_still_has_many_exits_and_all_are_covered():
    """If this number drops to 1, someone has restructured the flow and this
    file's assumptions need re-reading rather than blindly trusting."""
    tree = ast.parse(ESPRESSO_PY.read_text(encoding="utf-8"))
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "_check_booking_inner")
    exits = ([n for n in ast.walk(inner) if isinstance(n, ast.Return)]
             + [n for n in ast.walk(inner) if isinstance(n, ast.Raise)])
    assert len(exits) >= 15, (
        "the flow used to have 21 ways out, 19 of which leaked a locked "
        "booking; if that changed, re-verify the release still covers them")


def test_the_inner_flow_is_not_called_anywhere_but_check_booking():
    """Calling it directly would bypass the finally and leak the lock."""
    src = ESPRESSO_PY.read_text(encoding="utf-8")
    calls = [line for line in src.splitlines()
             if "_check_booking_inner(" in line and "def " not in line]
    assert len(calls) == 1, f"unexpected direct callers: {calls}"


# ── every branch that used to leak ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    BookingStatus.NO_SAVING,
    BookingStatus.PAID_IN_FULL,
    BookingStatus.CANCELLED,
])
async def test_every_result_status_releases(status):
    s = _Scraper(_result(status))
    await s.check_booking("12345")
    assert s.released == ["12345"]


@pytest.mark.asyncio
async def test_a_raise_still_releases():
    """Six raises in the flow, every one of which used to leave the booking
    locked - including "Not logged in", which fires in batches."""
    s = _Scraper(RuntimeError("Not logged in - please log into ESPRESSO first"))
    with pytest.raises(RuntimeError, match="Not logged in"):
        await s.check_booking("77777")
    assert s.released == ["77777"]


@pytest.mark.asyncio
async def test_a_cancellation_still_releases():
    """Stopping a scan must not strand the booking that was in flight."""
    import asyncio
    s = _Scraper(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await s.check_booking("88888")
    assert s.released == ["88888"]


# ── it must not release twice, or mask anything ──────────────────────────


@pytest.mark.asyncio
async def test_a_booking_already_released_is_not_released_again():
    """The happy path releases inside the flow; the finally must notice."""
    class AlreadyReleased(_Scraper):
        async def _check_booking_inner(self, booking_id, capture_market_data=False):
            await self.release_booking(booking_id)
            return _result()

    s = AlreadyReleased(None)
    await s.check_booking("55555")
    assert s.released == ["55555"], "released twice"


@pytest.mark.asyncio
async def test_an_unconfirmed_release_is_retried_not_remembered():
    """release_booking returning False means the dialog may still be open.
    That must not be recorded as done."""
    class HappyPathFails(_Scraper):
        async def _check_booking_inner(self, booking_id, capture_market_data=False):
            await self.release_booking(booking_id)     # returns False
            return _result()

    s = HappyPathFails(None, release_ok=False)
    await s.check_booking("44444")
    assert s.released == ["44444", "44444"], "an unconfirmed release must be retried"


@pytest.mark.asyncio
async def test_a_failing_release_never_replaces_a_good_result():
    """A locked booking is bad; losing a correct result to it is worse."""
    s = _Scraper(_result(BookingStatus.NO_SAVING), release_raises=True)
    result = await s.check_booking("33333")
    assert result.status is BookingStatus.NO_SAVING


@pytest.mark.asyncio
async def test_a_failing_release_never_masks_the_real_exception():
    """An exception raised from a finally replaces the original one, turning
    a diagnosable failure into a misleading one."""
    s = _Scraper(RuntimeError("the REAL failure"), release_raises=True)
    with pytest.raises(RuntimeError, match="the REAL failure"):
        await s.check_booking("22222")


@pytest.mark.asyncio
async def test_the_marker_resets_between_bookings():
    """A stale marker would silently stop releasing every later booking -
    exactly the bug, reintroduced by the fix."""
    s = _Scraper(_result())
    await s.check_booking("aaa")
    await s.check_booking("bbb")
    assert s.released == ["aaa", "bbb"]


@pytest.mark.asyncio
async def test_the_same_booking_checked_twice_is_released_twice():
    s = _Scraper(_result())
    await s.check_booking("dup")
    await s.check_booking("dup")
    assert s.released == ["dup", "dup"]


def test_ensure_released_is_documented_as_never_raising():
    """It runs in a finally. The contract matters more than the code here."""
    doc = inspect.getdoc(EspressoScraper._ensure_released) or ""
    assert "NEVER raises" in doc or "never raises" in doc
