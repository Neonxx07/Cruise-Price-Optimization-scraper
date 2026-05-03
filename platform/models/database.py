"""SQLAlchemy database models and engine setup.

Uses async SQLite for development, easily swappable to PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, create_engine
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


# ── Engine & Session ────────────────────────────────────────────

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Get a database session (for dependency injection)."""
    async with async_session() as session:
        yield session
