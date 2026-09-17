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
from utils.logging import get_logger

logger = get_logger(__name__)


# ── Base ────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Tables ──────────────────────────────────────────────────────


class BookingRecord(Base):
    """Stores the result of each booking check."""

    __tablename__ = "bookings"
    __table_args__ = (
        # The GUI's hottest query, added 2026-09-15 after measuring it:
        # "today's results for this cruise line" was a full table SCAN at
        # 48 ms, and it runs once per panel on every startup and reload -
        # four times over, before a single booking is scanned. Indexed it
        # drops to roughly a millisecond. Mirrors the composite already on
        # market_data, which was added for the same reason.
        Index("ix_bookings_line_created_at", "cruise_line", "created_at"),
    )

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

    # ADDED 2026-08-27 after a forensic audit found that 14 of
    # BookingResult's 26 fields were COMPUTED AND THEN DISCARDED at
    # persistence. That made the system unable to audit its own money
    # decisions after the fact:
    #
    #   * `obc_change` is the field the whole OBC rule turns on (a $57
    #     "saving" that forfeits $100 of OBC is a $43 LOSS — real bookings
    #     3000055 and 3000054). It was never stored, so no query could
    #     ever check whether the rule had been applied correctly.
    #   * `old_promos` / `new_promos` were added specifically so a
    #     LATRIPLE/FREESRVC TRAP verdict would be AUDITABLE. Dropping them
    #     meant that auditability never actually existed.
    #   * `price_drop`, `lost_pkg_value`, `lost_fares` are the components
    #     net_saving is derived from — without them a reported net figure
    #     cannot be reconciled or independently re-derived.
    #   * `currency` exists precisely to avoid assuming USD; dropping it
    #     restored the silent assumption it was created to remove.
    #
    # Nullable with no default so pre-existing rows read back as NULL —
    # honestly "not recorded", never a fabricated 0.0 that would look like
    # a real measurement of no OBC change. See _migrate_sqlite_add_columns.
    price_drop = Column(Float, nullable=True)
    obc_change = Column(Float, nullable=True)
    lost_pkg_value = Column(Float, nullable=True)
    currency = Column(String(10), nullable=True)
    old_promos = Column(Text, nullable=True)
    new_promos = Column(Text, nullable=True)
    lost_fares = Column(Text, nullable=True)           # JSON array
    re_addable_fares = Column(Text, nullable=True)     # JSON array
    gained_fares = Column(Text, nullable=True)         # JSON array
    lost_travel_protection = Column(Text, nullable=True)  # JSON array
    old_cruise_fare = Column(Float, nullable=True)
    new_cruise_fare = Column(Float, nullable=True)
    fare_change_pct = Column(Float, nullable=True)

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
    # The WHOLE capture, added 2026-09-16. This table was built around
    # ESPRESSO's shape - a category table under "rows" - and every other
    # payload was silently reduced to it. NCL's capture carries no "rows"
    # at all: it holds the payment state (amount due, final payment date,
    # commission rate), so all 135 of its rows in the 2026-09-16 run were
    # written EMPTY and mislabelled "ncl_category_table".
    #
    # Worse, those discarded fields are exactly the ones needed to rank a
    # booking by urgency and to warn that a finding is about to expire -
    # the gap that let $3,945 of found savings lapse during an 18-day scan
    # gap. Keeping the raw payload means a future question can be answered
    # from history instead of needing another live run.
    payload_json = Column(Text)
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


def _ensure_sqlite_indexes(sync_conn) -> list[str]:
    """Create indexes that `Base.metadata.create_all` will not.

    create_all only ever CREATES tables - it never alters an existing one,
    which is the same reason _migrate_sqlite_add_columns exists for
    columns. An index declared in __table_args__ therefore never appears
    on a database that already had the table, so it has to be issued
    explicitly. IF NOT EXISTS makes this idempotent and safe to run on
    every startup.
    """
    from sqlalchemy import text

    wanted = {
        "ix_bookings_line_created_at":
            "CREATE INDEX IF NOT EXISTS ix_bookings_line_created_at "
            "ON bookings (cruise_line, created_at)",
    }
    created = []
    existing = {row[0] for row in sync_conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index'"))}
    for name, ddl in wanted.items():
        if name not in existing:
            sync_conn.execute(text(ddl))
            created.append(name)
    return created


def _migrate_sqlite_add_columns(sync_conn) -> list[str]:
    """Add any newly-declared columns to EXISTING tables.

    `Base.metadata.create_all` only ever CREATES missing tables — it will
    not alter one that already exists. So adding a column to a model was
    silently a no-op against a live database, and every write of that field
    failed or was dropped. This project's DB is a long-lived file with real
    client history (4,500+ booking rows), so dropping and recreating is not
    an option.

    Idempotent: reads the live schema and only issues ALTER TABLE ADD
    COLUMN for what is genuinely absent. SQLite's ADD COLUMN is a cheap
    metadata-only operation. Returns what it added, for logging.
    """
    from sqlalchemy import inspect as sa_inspect, text

    added: list[str] = []
    inspector = sa_inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all will make it, with every column
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            col_type = column.type.compile(dialect=sync_conn.dialect)
            # No DEFAULT and no NOT NULL: existing rows must read back as
            # NULL ("not recorded"), never a fabricated 0.0 that would be
            # indistinguishable from a real measured zero.
            sync_conn.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
            )
            added.append(f"{table.name}.{column.name}")
    return added


async def init_db():
    """Create all tables, then add any columns missing from existing ones."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_migrate_sqlite_add_columns)
        indexes = await conn.run_sync(_ensure_sqlite_indexes)
        if indexes:
            logger.info("database.indexes_created", indexes=indexes)
    if added:
        logger.info("db.migrated_added_columns", columns=added, count=len(added))
    return added
