"""SQLAlchemy database models and engine setup.

Uses async SQLite for development, easily swappable to PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, Index, String, Text, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


# ── Base ────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Tables ──────────────────────────────────────────────────────


class BookingRecord(Base):
    """Stores the result of each booking check."""

    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(20), nullable=False, index=True)
    cruise_line = Column(String(10), nullable=False)
    status = Column(String(20), nullable=False)
    old_total = Column(Float, default=0)
    new_total = Column(Float, default=0)
    net_saving = Column(Float, default=0)
    confidence = Column(Integer, default=0)
    price_category = Column(String(20))
    new_price_category = Column(String(20))
    note = Column(Text)
    error = Column(Text)
    lost_pkg_names = Column(Text)  # JSON array
    created_at = Column(DateTime, default=datetime.utcnow)


class PriceHistory(Base):
    """Tracks price over time for each booking."""

    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(20), nullable=False, index=True)
    cruise_line = Column(String(10), nullable=False)
    total = Column(Float, nullable=False)
    category = Column(String(20))
    checked_at = Column(DateTime, default=datetime.utcnow)


class ScanJobRecord(Base):
    """Tracks batch scan jobs."""

    __tablename__ = "scan_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), nullable=False, unique=True, index=True)
    booking_ids_json = Column(Text, nullable=False)  # JSON array
    cruise_line = Column(String(10), nullable=False)
    status = Column(String(20), default="PENDING")
    progress_done = Column(Integer, default=0)
    progress_total = Column(Integer, default=0)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    @property
    def booking_ids(self) -> list[str]:
        return json.loads(self.booking_ids_json)

    @booking_ids.setter
    def booking_ids(self, value: list[str]):
        self.booking_ids_json = json.dumps(value)


class CacheEntry(Base):
    """Smart cache for NO_SAVING results."""

    __tablename__ = "cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), nullable=False, unique=True, index=True)
    value_json = Column(Text, default="{}")
    expires_at = Column(DateTime, nullable=False)


class MarketDataRecord(Base):
    """Read-only market/category table captures from ESPRESSO scans."""

    __tablename__ = "market_data"
    __table_args__ = (
        Index("ix_market_data_booking_created_at", "booking_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(20), nullable=False, index=True)
    cruise_line = Column(String(10), nullable=False)
    capture_type = Column(String(50), nullable=False, default="espresso_category_table")
    current_category = Column(String(20))
    execution_token = Column(String(100))
    selection_json = Column(Text)
    category_table_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Engine & Session ────────────────────────────────────────────

# CONFIRMED REAL RISK, fixed 2026-08-13: this architecture genuinely
# supports multiple concurrent writers against the same on-disk SQLite
# file — the API server, the persistent watchlist scanner, and the GUI
# can all run against the same settings.database_url at once — but no
# busy-timeout or WAL config was ever set. Default SQLite/aiosqlite
# behavior under write contention is to fail IMMEDIATELY with
# "database is locked" rather than wait, and (per finding elsewhere in
# this audit) that error could previously abort an entire batch scan.
# Two smallest-safe changes, both SQLite-only (guarded so a future
# Postgres deployment — this module's own docstring says "easily
# swappable to PostgreSQL" — is never touched by either):
#   1. `connect_args={"timeout": 30}` — passed through to aiosqlite ->
#      sqlite3.connect's own `timeout` kwarg, which sets SQLite's
#      busy-timeout so a contending writer RETRIES for up to 30s
#      instead of raising instantly.
#   2. WAL journal mode — lets readers proceed without blocking on a
#      concurrent writer at all (the far more common case: the API
#      reading while the watchlist scanner writes), only genuinely
#      simultaneous WRITERS ever need the busy-timeout retry above.
_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    engine = create_async_engine(settings.database_url, echo=settings.debug, connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
else:
    engine = create_async_engine(settings.database_url, echo=settings.debug)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
