"""A confirmed paid-in-full booking must never be scanned again.

Neon 2026-09-29, non-negotiable: *"IF THE BOOKING SURELY FOR 100% SURE IS
PAID IN FULL THIS MUST BE STORED IN THE DATA BASE AND NEVER EVER BE
RESCANNED AGAIN EVEN IF THE USER PASTES OR ADDS IT IN THE LIST"*.

WHY IT IS WORTH IT. Measured over 24 hours of real scans:

    rows in the last 24h     : 1393
    bookings scanned >1 time : 495
    REDUNDANT scans          : 565   (41% of all work, ~148 minutes)

and among the repeats, PAID_IN_FULL was the single biggest waste at 439 -
because the existing TTL cache only ever stored NO_SAVING.

WHERE THE DANGER IS. The exclusion is PERMANENT, so a wrong one is
unrecoverable: the booking silently disappears from every future scan. That
is far worse than a redundant scan, so most of the tests below are about
REFUSING to write one.

The emphasis on "100% SURE" is Neon's own, and it is the right instinct -
this project has already seen a paid-in-full booking misread once, when
booking 3001001's CAD payment panel could not be parsed and a $400 saving
was reported on a reservation with two cents outstanding.
"""

import pytest
import pytest_asyncio

from services.exclusion_service import PAID_IN_FULL, ExclusionService


@pytest_asyncio.fixture
async def service(tmp_path, monkeypatch):
    """A real service against a throwaway database."""
    import models.database as db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    monkeypatch.setattr(db, "async_session", session_factory)
    import services.exclusion_service as mod
    monkeypatch.setattr(mod, "async_session", session_factory)
    return ExclusionService()


SOLID = dict(total_price=2109.0, final_payment_due=0.0,
             payment_state_readable=True, currency="CAD")


# ── it must refuse on anything less than certainty ───────────────────────


@pytest.mark.asyncio
async def test_an_unreadable_payment_panel_is_never_excluded(service):
    """THE critical guard. "We could not see the balance" must never become
    "it owes nothing" - that confusion reported a $400 saving on booking
    3001001, which had two cents outstanding. Permanently hiding such a
    booking would be the same error, made unrecoverable."""
    wrote = await service.record_paid_in_full(
        "ESPRESSO", "3001001", **{**SOLID, "payment_state_readable": False})
    assert wrote is False
    assert await service.active_for("ESPRESSO", ["3001001"]) == {}


@pytest.mark.asyncio
async def test_a_missing_final_payment_figure_is_never_excluded(service):
    """is_paid_in_full(None, ...) returns False by design - it refuses to
    guess. The exclusion must refuse on the same evidence."""
    wrote = await service.record_paid_in_full(
        "ESPRESSO", "B1", **{**SOLID, "final_payment_due": None})
    assert wrote is False


@pytest.mark.asyncio
async def test_a_zero_total_is_never_excluded(service):
    """A 0.00 total is an unread booking, not a settled one."""
    assert await service.record_paid_in_full(
        "ESPRESSO", "B2", **{**SOLID, "total_price": 0.0}) is False
    assert await service.record_paid_in_full(
        "ESPRESSO", "B3", **{**SOLID, "total_price": None}) is False


# ── and record it when the evidence IS solid ─────────────────────────────


@pytest.mark.asyncio
async def test_a_confirmed_paid_in_full_booking_is_excluded(service):
    assert await service.record_paid_in_full("ESPRESSO", "999", **SOLID) is True
    active = await service.active_for("ESPRESSO", ["999"])
    assert active["999"]["reason"] == PAID_IN_FULL


@pytest.mark.asyncio
async def test_the_evidence_is_stored_so_it_can_be_audited(service):
    """A permanent decision that cannot be checked afterwards is a permanent
    decision taken on trust."""
    import json
    await service.record_paid_in_full("ESPRESSO", "999", **SOLID)
    entry = (await service.active_for("ESPRESSO", ["999"]))["999"]
    evidence = json.loads(entry["evidence"])
    assert evidence["total_price"] == 2109.0
    assert evidence["final_payment_due"] == 0.0
    assert evidence["currency"] == "CAD"


@pytest.mark.asyncio
async def test_excluding_twice_does_not_duplicate(service):
    assert await service.record_paid_in_full("ESPRESSO", "999", **SOLID) is True
    assert await service.record_paid_in_full("ESPRESSO", "999", **SOLID) is False
    assert len(await service.list_active("ESPRESSO")) == 1


# ── identity: line + booking number ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_same_number_on_another_line_is_not_excluded(service):
    """Booking numbers are only unique WITHIN a portal."""
    await service.record_paid_in_full("ESPRESSO", "12345", **SOLID)
    assert await service.active_for("NCL", ["12345"]) == {}


# ── bulk lookup, because the list can be 723 long ────────────────────────


@pytest.mark.asyncio
async def test_the_lookup_handles_a_whole_watchlist_at_once(service):
    await service.record_paid_in_full("ESPRESSO", "A", **SOLID)
    await service.record_paid_in_full("ESPRESSO", "C", **SOLID)
    found = await service.active_for("ESPRESSO", ["A", "B", "C", "D"])
    assert set(found) == {"A", "C"}


@pytest.mark.asyncio
async def test_an_empty_list_asks_the_database_nothing(service):
    assert await service.active_for("ESPRESSO", []) == {}


# ── reversible, and auditable after the fact ─────────────────────────────


@pytest.mark.asyncio
async def test_an_exclusion_can_be_lifted(service):
    await service.record_paid_in_full("ESPRESSO", "999", **SOLID)
    assert await service.clear("ESPRESSO", "999") is True
    assert await service.active_for("ESPRESSO", ["999"]) == {}


@pytest.mark.asyncio
async def test_clearing_keeps_the_history(service):
    """The decision to exclude and the decision to undo it are both part of
    the audit trail. Rows are stamped, never deleted."""
    from sqlalchemy import select

    import services.exclusion_service as mod
    from models.database import PermanentExclusion
    await service.record_paid_in_full("ESPRESSO", "999", **SOLID)
    await service.clear("ESPRESSO", "999")
    async with mod.async_session() as s:
        rows = (await s.execute(select(PermanentExclusion))).scalars().all()
    assert len(rows) == 1 and rows[0].cleared_at is not None


@pytest.mark.asyncio
async def test_clearing_something_not_excluded_is_harmless(service):
    assert await service.clear("ESPRESSO", "nope") is False


@pytest.mark.asyncio
async def test_a_cleared_booking_can_be_excluded_again(service):
    await service.record_paid_in_full("ESPRESSO", "999", **SOLID)
    await service.clear("ESPRESSO", "999")
    assert await service.record_paid_in_full("ESPRESSO", "999", **SOLID) is True


# ── the queue honours it, before any browser action ──────────────────────


def _run_batch_code() -> str:
    """_run_batch source with comments stripped."""
    import inspect
    import io
    import tokenize

    from services.booking_service import BookingService
    src = inspect.getsource(BookingService._run_batch)
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT)


def test_the_exclusion_branch_runs_before_the_cache_branch():
    """Order INSIDE the per-booking loop. The point is never to OPEN the
    booking, and an excluded booking must not even be considered for a
    freshness answer.

    NOTE the two bulk LOOKUPS (exclusions and freshness) both run before the
    loop and are plain database reads - their order relative to each other
    does not matter. What matters is which BRANCH wins per booking.
    """
    code = _run_batch_code()
    assert "exclusions.active_for" in code
    assert code.index("permanently_excluded") < code.index("cached = fresh.get")


def test_nothing_is_scraped_before_the_exclusion_branch():
    code = _run_batch_code()
    assert code.index("permanently_excluded") < code.index("check_booking")


def test_force_rescan_does_not_override_a_permanent_exclusion():
    """"Force live recheck" clears a stale TTL. This is not a cached
    opinion about a price - it is a standing fact about the booking, and
    lifting it is an explicit act (ExclusionService.clear).

    The freshness lookup IS gated on bypass_cache; the exclusion lookup and
    the exclusion branch must not be.
    """
    import ast

    from services.booking_service import BookingService
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(BookingService._run_batch)))
    # find the `if booking_id in excluded:` branch and prove no bypass_cache
    # appears in its condition or body
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            dump = ast.dump(node.test) + ast.dump(
                ast.Module(body=node.body, type_ignores=[]))
            if "permanently_excluded" in dump:
                assert "bypass_cache" not in dump, (
                    "the permanent exclusion must not be bypassable")
                return
    raise AssertionError("could not find the exclusion branch")


# ── CANCELLED, same rule (added 2026-09-29) ──────────────────────────────
#
# Neon: "add canceled as the same rule case as paid in full i am trying to
# optimize to save resoursces and not doing useless scans".
#
# A cancelled reservation does not un-cancel, so re-opening it is as futile
# as re-opening a settled one. It does NOT weaken the reporting rule -
# reporting a cancellation is "VERY MADNATORY", and an excluded booking is
# still reported every run, just from the register rather than the portal.


@pytest.mark.asyncio
async def test_a_cancelled_booking_is_permanently_excluded(service):
    assert await service.record_cancelled("ESPRESSO", "CX1") is True
    entry = (await service.active_for("ESPRESSO", ["CX1"]))["CX1"]
    assert entry["reason"] == "CANCELLED"


@pytest.mark.asyncio
async def test_a_cancellation_records_its_detail(service):
    import json
    await service.record_cancelled("ESPRESSO", "CX2",
                                   detail="reservation status CX")
    entry = (await service.active_for("ESPRESSO", ["CX2"]))["CX2"]
    assert "CX" in json.loads(entry["evidence"])["detail"]


@pytest.mark.asyncio
async def test_a_cancellation_can_also_be_lifted(service):
    """Reversible like any other exclusion - a booking number can be
    reissued, however rarely."""
    await service.record_cancelled("ESPRESSO", "CX3")
    assert await service.clear("ESPRESSO", "CX3") is True
    assert await service.active_for("ESPRESSO", ["CX3"]) == {}


def test_an_excluded_cancellation_is_still_REPORTED_as_cancelled():
    """THE guard that keeps the mandatory-reporting rule intact. Skipping
    the SCRAPE must never become skipping the REPORT - an excluded
    cancellation has to come back as CANCELLED, not as a generic skip or,
    worse, as PAID_IN_FULL."""
    code = _run_batch_code()
    branch = code[code.index("permanently_excluded"):]
    branch = branch[:branch.index("Smart cache check")] if "Smart cache check" in branch else branch
    assert "make_cancelled_result" in branch
    assert 'reason == "CANCELLED"' in branch


def test_a_cancelled_result_is_recorded_for_next_time():
    code = _run_batch_code()
    assert "record_cancelled" in code
    assert "BookingStatus.CANCELLED" in code
