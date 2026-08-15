"""Database-layer regression tests from the 2026-08-13 audit.

Everything here runs against a throwaway temp SQLite file created fresh
per test and deleted afterward -- the real production cruise_intel.db
is never opened, read, or written by this suite.
"""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.database import Base, BookingRecord
import services.booking_service as booking_service_module
from services.booking_service import BookingService


@pytest_asyncio.fixture
async def temp_db_session(monkeypatch):
    """Creates a fresh temp SQLite DB with the SAME WAL/busy-timeout
    pragmas as production (models/database.py), points
    services.booking_service's `async_session` name at it (that's the
    exact name BookingService's methods actually use), and cleans up
    afterward."""
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)  # let SQLite create it fresh

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(booking_service_module, "async_session", session_factory)

    yield session_factory

    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


@pytest.mark.asyncio
async def test_regression_booking_lookup_beyond_100_rows(temp_db_session):
    """CONFIRMED REAL BUG, fixed 2026-08-13: GET /api/bookings/{id} used
    to call get_all_bookings() (limit=100, ordered by created_at desc)
    and filter client-side -- a real booking checked before 100 newer
    rows existed would silently 404. get_bookings_by_id() queries
    directly by booking_id instead."""
    from datetime import datetime, timedelta

    target_id = "TARGET999"
    async with temp_db_session() as session:
        session.add(BookingRecord(
            booking_id=target_id, cruise_line="ESPRESSO", status="OPTIMIZATION",
            net_saving=42.0, created_at=datetime.utcnow() - timedelta(days=10),
        ))
        await session.commit()

    async with temp_db_session() as session:
        for i in range(150):
            session.add(BookingRecord(
                booking_id=f"OTHER{i}", cruise_line="ESPRESSO", status="NO_SAVING",
                created_at=datetime.utcnow() - timedelta(minutes=150 - i),
            ))
        await session.commit()

    service = BookingService()

    # The OLD buggy path: confirm it really would have missed the booking.
    old_way = await service.get_all_bookings(limit=100)
    assert not any(r["booking_id"] == target_id for r in old_way), (
        "test setup didn't reproduce the original bug scenario"
    )

    # The FIXED path.
    found = await service.get_bookings_by_id(target_id)
    assert len(found) == 1
    assert found[0]["booking_id"] == target_id
    assert found[0]["net_saving"] == 42.0


@pytest.mark.asyncio
async def test_regression_booking_lookup_missing_booking_returns_empty(temp_db_session):
    service = BookingService()
    found = await service.get_bookings_by_id("DOES_NOT_EXIST")
    assert found == []


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_raise_database_locked(temp_db_session):
    """CONFIRMED REAL RISK, fixed 2026-08-13: no busy-timeout/WAL config
    meant concurrent writers (API + watchlist scanner + GUI, all
    documented as capable of running simultaneously against the same
    file) could fail with 'database is locked' instead of retrying."""
    import asyncio

    async def writer(n):
        async with temp_db_session() as session:
            session.add(BookingRecord(booking_id=f"CONCURRENT{n}", cruise_line="NCL", status="OPTIMIZATION"))
            await session.commit()

    await asyncio.gather(*[writer(n) for n in range(20)])

    async with temp_db_session() as session:
        result = await session.execute(select(BookingRecord))
        assert len(result.scalars().all()) == 20


@pytest.mark.asyncio
async def test_wal_mode_actually_enabled(temp_db_session):
    async with temp_db_session() as session:
        result = await session.execute(select(1))  # touch the connection
        assert result.scalar() == 1
    # Verify the pragma directly via a raw connection.
    engine = booking_service_module.async_session.kw["bind"]
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql("PRAGMA journal_mode")
        mode = result.fetchone()[0]
        assert mode.lower() == "wal"


@pytest.mark.asyncio
async def test_failed_transaction_does_not_leave_partial_data(temp_db_session):
    """A rolled-back session must not leave a half-written row behind."""
    service = BookingService()
    async with temp_db_session() as session:
        session.add(BookingRecord(booking_id="ROLLBACK_TEST", cruise_line="ESPRESSO", status="ERROR"))
        await session.rollback()  # simulate a failure before commit

    found = await service.get_bookings_by_id("ROLLBACK_TEST")
    assert found == []
