"""MSC results had nowhere to go. (Roadmap P2.1)

FOUND 2026-10-01 while switching MSC on. MSC has a 1,337-line calculator, a
session controller, a 4,053-line command module, documented discount rules
and a GUI panel - and **zero rows in every table**::

    bookings 0 · scan_jobs 0 · price_history 0 · market_data 0

Not a crash, and not a login problem: the log shows `msc_live.auto_login_ok`
and a real session. `MscLiveService.run_batch` built `MscCheckOutcome`
objects, handed them to the GUI and returned them. **Nothing ever wrote one
down.** Every MSC scan ever run evaporated when the window closed.

(The `msc.voyagers_club_entry_failed` errors in the log are a separate,
already-fixed matter - the 2026-09-22 fix tries a real click briefly and
then dispatches through JS. Those log lines predate it.)

WHY A DEDICATED TABLE. An MSC evaluation is not one saving, it is FOUR
independent checks, and their `estimated_value` fields carry DIFFERENT
UNITS - dollars for PRICE_MATCH, percentage points for
DISCOUNT_TIER_UPGRADE, nothing for DISCOUNT_ADD or VOYAGERS_SELECTION.
Flattening that into old_total/new_total/net_saving would invent figures MSC
never produced. Quietly reducing fidelity is what "never delete captured
data" exists to prevent, so the checks are stored whole.
"""

import json

import pytest
import pytest_asyncio

from core.models import (
    MscBookingResult,
    MscCheck,
    MscCheckStatus,
    MscOpportunityType,
)
from services.msc_live_service import MscCheckOutcome, persist_outcome


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    import models.database as mod
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(mod.Base.metadata.create_all)
    monkeypatch.setattr(mod, "async_session", factory)
    return mod


async def _rows(db):
    from sqlalchemy import select
    async with db.async_session() as session:
        return (await session.execute(select(db.MscResultRecord))).scalars().all()


def _outcome(booking_id="3000026", status="checked", **kwargs):
    checks = kwargs.pop("checks", None)
    if status != "checked":
        return MscCheckOutcome(booking_id=booking_id, status=status,
                               note=kwargs.get("note", ""))
    result = MscBookingResult(
        booking_id=booking_id,
        category=kwargs.get("category", "IB"),
        checks=checks if checks is not None else [],
        has_any_opportunity=kwargs.get("has_any_opportunity", False),
        is_paid_in_full=kwargs.get("is_paid_in_full", False),
        cancelled_or_postponed=kwargs.get("cancelled_or_postponed", False),
    )
    return MscCheckOutcome(booking_id=booking_id, status="checked",
                           result=result, note=kwargs.get("note", ""))


# -- it gets written at all --------------------------------------------


@pytest.mark.asyncio
async def test_a_checked_booking_is_stored(db):
    assert await persist_outcome(_outcome()) is True

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0].booking_id == "3000026"
    assert rows[0].status == "checked"


@pytest.mark.asyncio
async def test_a_booking_that_could_not_be_checked_is_still_stored(db):
    """A not-found booking is a fact worth keeping, not a blank."""
    await persist_outcome(_outcome(status="not_found"))

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0].status == "not_found"
    assert rows[0].has_any_opportunity is False


@pytest.mark.asyncio
async def test_an_errored_booking_is_stored(db):
    await persist_outcome(MscCheckOutcome(
        booking_id="X", status="error", note="session expired"))

    rows = await _rows(db)
    assert rows[0].status == "error"
    assert "session expired" in rows[0].note


# -- fidelity: the four checks survive whole ---------------------------


@pytest.mark.asyncio
async def test_every_check_is_stored_with_its_own_unit(db):
    """The landmine this guards: PRICE_MATCH is DOLLARS and
    DISCOUNT_TIER_UPGRADE is PERCENTAGE POINTS. Storing the numbers without
    their units would let a future aggregator add them together."""
    await persist_outcome(_outcome(checks=[
        MscCheck(type=MscOpportunityType.PRICE_MATCH,
                 status=MscCheckStatus.OPPORTUNITY,
                 note="cheaper today", estimated_value=240.0, value_unit="USD"),
        MscCheck(type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
                 status=MscCheckStatus.OPPORTUNITY,
                 note="better tier", estimated_value=5.0,
                 value_unit="PERCENTAGE_POINTS"),
        MscCheck(type=MscOpportunityType.DISCOUNT_ADD,
                 status=MscCheckStatus.NO_OPPORTUNITY, note="none"),
        MscCheck(type=MscOpportunityType.VOYAGERS_SELECTION,
                 status=MscCheckStatus.INSUFFICIENT_DATA,
                 note="options not captured"),
    ], has_any_opportunity=True))

    stored = json.loads((await _rows(db))[0].checks_json)
    assert len(stored) == 4

    by_type = {c["type"]: c for c in stored}
    assert by_type["PRICE_MATCH"]["value_unit"] == "USD"
    assert by_type["PRICE_MATCH"]["estimated_value"] == 240.0
    assert by_type["DISCOUNT_TIER_UPGRADE"]["value_unit"] == "PERCENTAGE_POINTS"
    assert by_type["DISCOUNT_TIER_UPGRADE"]["estimated_value"] == 5.0
    assert by_type["VOYAGERS_SELECTION"]["status"] == "INSUFFICIENT_DATA"


@pytest.mark.asyncio
async def test_insufficient_data_is_not_recorded_as_no_opportunity(db):
    """They are different claims - one is checked and found nothing, the
    other is could not check."""
    await persist_outcome(_outcome(checks=[
        MscCheck(type=MscOpportunityType.DISCOUNT_ADD,
                 status=MscCheckStatus.INSUFFICIENT_DATA, note="no dropdown"),
    ]))

    rows = await _rows(db)
    stored = json.loads(rows[0].checks_json)
    assert stored[0]["status"] == "INSUFFICIENT_DATA"
    assert rows[0].has_any_opportunity is False


@pytest.mark.asyncio
async def test_opportunity_types_are_queryable_without_parsing_json(db):
    await persist_outcome(_outcome(checks=[
        MscCheck(type=MscOpportunityType.PRICE_MATCH,
                 status=MscCheckStatus.OPPORTUNITY, note="x"),
        MscCheck(type=MscOpportunityType.DISCOUNT_ADD,
                 status=MscCheckStatus.OPPORTUNITY, note="y"),
        MscCheck(type=MscOpportunityType.DISCOUNT_TIER_UPGRADE,
                 status=MscCheckStatus.NO_OPPORTUNITY, note="z"),
    ], has_any_opportunity=True))

    row = (await _rows(db))[0]
    assert row.opportunity_types == "PRICE_MATCH,DISCOUNT_ADD"
    assert row.has_any_opportunity is True


@pytest.mark.asyncio
async def test_a_booking_with_no_opportunity_records_an_empty_list(db):
    await persist_outcome(_outcome(checks=[
        MscCheck(type=MscOpportunityType.PRICE_MATCH,
                 status=MscCheckStatus.NO_OPPORTUNITY, note="same price"),
    ]))

    row = (await _rows(db))[0]
    assert row.opportunity_types == ""
    assert row.has_any_opportunity is False
    assert len(json.loads(row.checks_json)) == 1


@pytest.mark.asyncio
async def test_the_booking_flags_are_kept(db):
    await persist_outcome(_outcome(is_paid_in_full=True,
                                   cancelled_or_postponed=True,
                                   category="OB"))
    row = (await _rows(db))[0]
    assert row.is_paid_in_full is True
    assert row.cancelled_or_postponed is True
    assert row.category == "OB"


# -- it must never end a scan ------------------------------------------


@pytest.mark.asyncio
async def test_a_database_failure_never_raises(db, monkeypatch):
    """A bookkeeping failure must not take down the scan it is recording -
    but it must not vanish either."""
    import models.database as mod

    def boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr(mod, "async_session", boom)
    assert await persist_outcome(_outcome()) is False


@pytest.mark.asyncio
async def test_history_accumulates_rather_than_overwriting(db):
    """Every scan is a row. Price movement over time is the whole point of
    keeping them."""
    await persist_outcome(_outcome())
    await persist_outcome(_outcome())
    assert len(await _rows(db)) == 2


# -- the batch loop actually calls it ----------------------------------


def test_run_batch_persists_every_outcome():
    """Structural, from the AST - a table nothing writes to is worse than
    no table."""
    import ast
    import pathlib

    source = pathlib.Path("services/msc_live_service.py").read_text(encoding="utf-8")
    run_batch = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_batch")

    calls = [n for n in ast.walk(run_batch)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "persist_outcome"]
    assert calls, "run_batch no longer stores MSC results - they evaporate again"
