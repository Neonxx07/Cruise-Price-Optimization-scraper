"""A scan that dies must continue, not start again. (Roadmap P0.2)

THE BUG. `_update_job_in_db` was called exactly ONCE, in `_run_batch`'s
`finally`. A hard death - the process killed, a crash, the machine going
down - wrote nothing at all, and `reconcile_stale_jobs` then marked the row
FAILED with `progress_done = 0`.

MEASURED 2026-10-01, each job against its own booking list:

    NCL      2026-09-30   recorded 0 of 189   actually scanned 189  (100%)
    ESPRESSO 2026-09-30   recorded 0 of 723   actually scanned 530   (73%)
    ESPRESSO 2026-09-18   recorded 0 of 721   actually scanned 559   (77%)
    ESPRESSO 2026-09-22   recorded 0 of 721   actually scanned 156   (21%)

Across the zero-progress jobs sampled: recorded as **0 of 4,097**, really
**2,280 scanned**. One NCL job finished *completely* and is on record as a
total failure.

So the roadmap's "43% of scheduled work ever completed" was measuring
**bookkeeping, not work** - a stored counter is a claim, not evidence. Work
is genuinely lost too (the 21% job is real), and none of it could be
resumed while progress reached the database only at the end.

NOW: `scan_jobs.completed_ids_json` records WHICH bookings produced a
result, written at the top of every iteration, and `resumable_jobs()`
returns what is left.
"""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from core.models import ScanJobStatus


@pytest_asyncio.fixture
async def service(tmp_path, monkeypatch):
    """A real BookingService against a throwaway database."""
    import models.database as db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    monkeypatch.setattr(db, "async_session", session_factory)

    import services.booking_service as mod
    monkeypatch.setattr(mod, "async_session", session_factory)
    return mod.BookingService()


@pytest_asyncio.fixture
async def add_job(service):
    """Write a scan_jobs row directly, as an interrupted run would leave it."""
    import models.database as db

    async def _add(job_id, booking_ids, completed=None, status="FAILED",
                   cruise_line="ESPRESSO", age_hours=1.0):
        async with db.async_session() as session:
            session.add(db.ScanJobRecord(
                job_id=job_id,
                booking_ids_json=json.dumps(booking_ids),
                cruise_line=cruise_line,
                status=status,
                progress_total=len(booking_ids),
                progress_done=len(completed or []),
                completed_ids_json=json.dumps(completed) if completed is not None else None,
                started_at=datetime.utcnow() - timedelta(hours=age_hours),
            ))
            await session.commit()
    return _add


# ── what is left ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_interrupted_job_offers_only_what_is_missing(service, add_job):
    await add_job("j1", ["a", "b", "c", "d"], completed=["a", "b"])

    jobs = await service.resumable_jobs()

    assert len(jobs) == 1
    assert jobs[0]["remaining"] == ["c", "d"]
    assert jobs[0]["done"] == 2
    assert jobs[0]["total"] == 4


@pytest.mark.asyncio
async def test_a_finished_job_is_not_offered(service, add_job):
    await add_job("j1", ["a", "b"], completed=["a", "b"])
    assert await service.resumable_jobs() == []


@pytest.mark.asyncio
async def test_a_completed_job_is_never_offered_even_with_gaps(service, add_job):
    """COMPLETED means the run reached its own end. Bookings may be absent
    because they were excluded or cached, not because they were missed."""
    await add_job("j1", ["a", "b", "c"], completed=["a"],
                  status=ScanJobStatus.COMPLETED.value)
    assert await service.resumable_jobs() == []


@pytest.mark.asyncio
async def test_a_job_the_process_died_inside_is_resumable(service, add_job):
    """RUNNING forever is exactly the case worth resuming - it means
    nothing ever ran the finally."""
    await add_job("j1", ["a", "b", "c"], completed=["a"], status="RUNNING")

    jobs = await service.resumable_jobs()
    assert [j["job_id"] for j in jobs] == ["j1"]
    assert jobs[0]["remaining"] == ["b", "c"]


@pytest.mark.asyncio
async def test_a_job_that_recorded_nothing_offers_everything(service, add_job):
    """The real shape of the 30 FAILED rows: no completed list at all."""
    await add_job("j1", ["a", "b", "c"], completed=None)

    jobs = await service.resumable_jobs()
    assert jobs[0]["remaining"] == ["a", "b", "c"]
    assert jobs[0]["done"] == 0


@pytest.mark.asyncio
async def test_order_is_preserved(service, add_job):
    """Resume should carry on through the queue, not reshuffle it."""
    await add_job("j1", ["e", "d", "c", "b", "a"], completed=["e", "c"])
    assert (await service.resumable_jobs())[0]["remaining"] == ["d", "b", "a"]


@pytest.mark.asyncio
async def test_remaining_for_names_one_job(service, add_job):
    await add_job("j1", ["a", "b", "c"], completed=["a"])
    await add_job("j2", ["x", "y"], completed=["x"])

    assert await service.remaining_for("j1") == ["b", "c"]
    assert await service.remaining_for("j2") == ["y"]


@pytest.mark.asyncio
async def test_an_unknown_job_returns_nothing_rather_than_raising(service):
    assert await service.remaining_for("nope") == []


# ── scoping ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_line_sees_only_its_own_jobs(service, add_job):
    await add_job("esp", ["a"], completed=[], cruise_line="ESPRESSO")
    await add_job("ncl", ["x"], completed=[], cruise_line="NCL")

    assert [j["job_id"] for j in await service.resumable_jobs("NCL")] == ["ncl"]


@pytest.mark.asyncio
async def test_ancient_jobs_are_not_offered(service, add_job):
    """Resuming a week-old scan would re-quote stale prices."""
    await add_job("old", ["a", "b"], completed=["a"], age_hours=200.0)
    assert await service.resumable_jobs(max_age_hours=72.0) == []


# ── never break the scan it is recording ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_broken_booking_list_is_skipped_not_fatal(service, add_job):
    import models.database as db

    await add_job("good", ["a", "b"], completed=["a"])
    async with db.async_session() as session:
        session.add(db.ScanJobRecord(
            job_id="bad", booking_ids_json="{not json",
            cruise_line="ESPRESSO", status="FAILED",
            progress_total=1, started_at=datetime.utcnow()))
        await session.commit()

    assert [j["job_id"] for j in await service.resumable_jobs()] == ["good"]


@pytest.mark.asyncio
async def test_a_checkpoint_failure_never_propagates(service):
    """A checkpoint exists to RECORD a scan, so it must never be able to
    end one."""
    from core.models import CruiseLine, ScanJob

    job = ScanJob(job_id="j", booking_ids=["a"], cruise_line=CruiseLine.ESPRESSO,
                  status=ScanJobStatus.RUNNING, progress_total=1,
                  started_at=datetime.utcnow())

    async def boom(_job):
        raise RuntimeError("database gone")

    service._update_job_in_db = boom
    await service._checkpoint(job)       # must not raise


@pytest.mark.asyncio
async def test_the_checkpoint_records_the_bookings_that_produced_results(service, add_job):
    """End to end: results in, completed ids on disk."""
    import models.database as db
    from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob

    await add_job("j1", ["a", "b", "c"], completed=[], status="RUNNING")
    job = ScanJob(job_id="j1", booking_ids=["a", "b", "c"],
                  cruise_line=CruiseLine.ESPRESSO, status=ScanJobStatus.RUNNING,
                  progress_total=3, started_at=datetime.utcnow())
    job.results = [
        BookingResult(cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.NO_SAVING, booking_id="a"),
        BookingResult(cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.NO_SAVING, booking_id="b"),
    ]
    job.progress_done = 2

    await service._checkpoint(job)

    async with db.async_session() as session:
        from sqlalchemy import select
        record = (await session.execute(
            select(db.ScanJobRecord).where(db.ScanJobRecord.job_id == "j1")
        )).scalar_one()
        assert record.completed_ids == ["a", "b"]

    assert await service.remaining_for("j1") == ["c"]


@pytest.mark.asyncio
async def test_a_booking_scanned_twice_is_recorded_once(service, add_job):
    """Session recovery retries the interrupted booking, so a duplicate
    result is normal and must not corrupt the remaining list."""
    from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob

    await add_job("j1", ["a", "b"], completed=[], status="RUNNING")
    job = ScanJob(job_id="j1", booking_ids=["a", "b"],
                  cruise_line=CruiseLine.ESPRESSO, status=ScanJobStatus.RUNNING,
                  progress_total=2, started_at=datetime.utcnow())
    job.results = [
        BookingResult(cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.ERROR, booking_id="a"),
        BookingResult(cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.NO_SAVING, booking_id="a"),
    ]

    await service._checkpoint(job)
    assert await service.remaining_for("j1") == ["b"]
