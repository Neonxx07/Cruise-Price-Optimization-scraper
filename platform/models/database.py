"""SQLAlchemy database models and engine setup.

Uses async SQLite for development, easily swappable to PostgreSQL.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index, Integer, String, Text, event,
)
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



    # ── COMMISSION: COLLECTED DATA ONLY ──────────────────────────────
    #
    # Neon 2026-10-01: *"do not totally ignore the comission include it in
    # the database infromations and collected data but seprate it totaly
    # away from our optimization process or saving process or whatever u
    # call it."*
    #
    # These are RECORDED AND NEVER READ by any status, net_saving, or
    # recommendation path. CruiseIntel reports the PRICE DIFFERENCE; the
    # agency works out commission itself. Storing it costs nothing and
    # builds the corpus (never delete captured data); letting it reach the
    # saving maths is what was asked against.
    #
    # NCL's scraper already read all three from the portal's own summary
    # and threw them away here - they only ever reached a note. Rate is
    # Commiss.Earned / invoice total, measured per booking and never
    # assumed: 13.17% on 3000049, 13.99% on another real capture.
    commission_rate = Column(Float, nullable=True)
    commission_earned = Column(Float, nullable=True)
    commission_due = Column(Float, nullable=True)

class PriceHistory(Base):
    """Tracks price over time for each booking."""

    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(20), nullable=False, index=True)
    cruise_line = Column(String(10), nullable=False)
    total = Column(Float, nullable=False)
    category = Column(String(20))
    checked_at = Column(DateTime, default=datetime.utcnow)

    # ── what actually moves a cruise price ──────────────────────────────
    #
    # ADDED 2026-09-21. A drop-prediction model trained on this table's
    # first 5,067 rows scored AUC 0.686 on a temporal split - and only
    # 0.549 once scan-cadence features were removed, i.e. barely better
    # than a coin flip. The reason was not the amount of data: the table
    # recorded price, category and a timestamp, and NONE of the drivers.
    # DAYS TO SAILING is the dominant one in cruise pricing and was stored
    # nowhere at all (0 of 5,407 market_data payloads carried it either).
    #
    # Every field below was located in REAL captured data for each line -
    # see core/booking_features.py, which does the extraction. They are all
    # NULLABLE on purpose: a booking whose sail date could not be read must
    # read back as NULL, never as a 0 that a model would treat as "sails
    # today". Existing rows keep NULL, which is the truth about them.
    sail_date = Column(String(10), index=True)        # ISO, unknown -> NULL
    days_to_sailing = Column(Integer, index=True)     # negative = already sailed
    ship_code = Column(String(10))
    ship_name = Column(String(60))
    nights = Column(Integer)
    fare_code = Column(String(20))                    # the REAL offer code
    stateroom_type = Column(String(30))
    guests_count = Column(Integer)
    final_payment_date = Column(String(10))
    net_balance_due = Column(Float)
    currency = Column(String(8))
    region = Column(String(60))
    itinerary_code = Column(String(20))
    embark_port = Column(String(60))


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

    # THE SCAN REQUEST'S OWN IDENTITY. Added 2026-10-01.
    #
    # Neon: *"it is the same list it should not scan again ... at least in
    # a frame of 2 hours."* CacheService answers a per-BOOKING question
    # from inside a running scan; nothing modelled the REQUEST, so every
    # Start opened a browser by definition.
    #
    # Deterministic and order-independent - see core/scan_signature.py.
    # Stored on the job that already exists rather than in a new table:
    # scan_jobs already records what was asked for, when it started and
    # whether it completed, which is exactly what the 2-hour rule needs.
    signature = Column(String(32), nullable=True, index=True)

    # WHICH bookings finished, not just how many. Added 2026-10-01.
    #
    # progress_done alone cannot resume a job, and it was not even
    # reliable: _update_job_in_db ran ONCE, in the finally at the end of a
    # run, so a hard death wrote nothing and reconcile_stale_jobs then
    # marked the row FAILED at progress_done = 0. Measured: jobs recorded
    # as "0 of 723" had really scanned 530, and one NCL job that finished
    # all 189 of its bookings is on record as a total failure.
    #
    # A JSON array of the booking ids that produced a result. Written as
    # the run proceeds, so an interrupted job can continue from whatever
    # is missing rather than starting again.
    completed_ids_json = Column(Text, nullable=True)

    @property
    def completed_ids(self) -> list[str]:
        if not self.completed_ids_json:
            return []
        try:
            return json.loads(self.completed_ids_json)
        except ValueError:
            return []

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


class PermanentExclusion(Base):
    """Bookings that must NEVER be scanned again.

    Neon 2026-09-29, non-negotiable: "IF THE BOOKING SURELY FOR 100% SURE
    IS PAID IN FULL THIS MUST BE STORED IN THE DATA BASE AND NEVER EVER BE
    RESCANNED AGAIN EVEN IF THE USER PASTES OR ADDS IT IN THE LIST".

    A paid-in-full booking cannot be repriced, so re-opening it is pure
    waste. Measured over 24 hours: 439 of 565 redundant scans were
    PAID_IN_FULL bookings being checked again and again, because the
    existing TTL cache only ever stored NO_SAVING.

    WHY THIS IS NOT JUST A LONGER TTL. An exclusion here is permanent, so a
    WRONG one is unrecoverable - the booking silently disappears from every
    future scan. That is the opposite failure to a redundant scan, and far
    worse. So:

      * `reason` and `evidence` record WHY, in the portal's own figures, so
        any entry can be audited rather than taken on trust.
      * `cleared_at` makes it reversible. An exclusion is never deleted -
        clearing it leaves the history intact.
      * The service that writes these refuses unless the payment panel was
        actually READ (see ExclusionService.record_paid_in_full). "We could
        not see the balance" must never become "it owes nothing", which is
        exactly the confusion that produced the false $400 on booking
        3001001.

    Cancelled bookings are safe from this by construction: EspressoScraper
    checks is_cancelled() BEFORE reading the payment panel, so a CX booking
    - which displays Final Payment Due 0.00 - returns CANCELLED and never
    reaches the paid-in-full test.
    """

    __tablename__ = "permanent_exclusions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(50), nullable=False, index=True)
    cruise_line = Column(String(20), nullable=False, index=True)
    reason = Column(String(40), nullable=False)          # e.g. PAID_IN_FULL
    evidence = Column(Text, default="")                  # the figures, as read
    excluded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Set when an exclusion is lifted. Non-NULL means it no longer applies;
    # the row stays so the decision remains auditable.
    cleared_at = Column(DateTime, nullable=True)


class MscResultRecord(Base):
    """One MSC booking evaluation. MSC's results had NOWHERE to go.

    FOUND 2026-10-01 while switching MSC on (roadmap P2.1). MSC has a
    1,337-line calculator, a session controller, a 4,053-line command
    module, documented discount rules and a GUI panel - and **zero rows in
    every table**: bookings 0, scan_jobs 0, price_history 0, market_data 0.

    The cause was not a crash. `MscLiveService.run_batch` builds
    `MscCheckOutcome` objects, hands them to the GUI and returns them.
    Nothing ever wrote one down. Every MSC scan ever run has evaporated
    when the window closed.

    WHY ITS OWN TABLE RATHER THAN `bookings`. An MSC evaluation is not one
    saving, it is FOUR independent checks (PRICE_MATCH, DISCOUNT_ADD,
    DISCOUNT_TIER_UPGRADE, VOYAGERS_SELECTION), each with its own status,
    and their `estimated_value` fields carry DIFFERENT UNITS - dollars for
    PRICE_MATCH, percentage points for DISCOUNT_TIER_UPGRADE, nothing at
    all for the other two. Flattening that into old_total/new_total/
    net_saving would invent figures MSC never produced, and quietly
    reducing fidelity is exactly what Neon's "never delete captured data"
    rule exists to stop.

    So the checks are stored whole, as JSON, with the fields that DO
    generalise promoted to columns so MSC can be queried alongside the
    other lines.
    """

    __tablename__ = "msc_results"
    __table_args__ = (
        Index("ix_msc_results_booking_created", "booking_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(50), nullable=False, index=True)

    # The OUTCOME of the attempt: "checked", or a short-circuit from
    # _check_booking_msc ("not_found", "cancelled",
    # "session_expired_after_relogin", "error"...). A booking that could
    # not be checked is a fact worth keeping, not a blank.
    status = Column(String(40), nullable=False, index=True)
    note = Column(Text, default="")

    category = Column(String(20), nullable=True)
    cancelled_or_postponed = Column(Boolean, default=False)
    is_paid_in_full = Column(Boolean, default=False)
    has_any_opportunity = Column(Boolean, default=False, index=True)

    # Comma-separated opportunity types, for querying without parsing the
    # JSON ("PRICE_MATCH,DISCOUNT_ADD"). "" when there are none.
    opportunity_types = Column(String(200), default="")

    # The full four checks, each with type, status, note, estimated_value
    # and value_unit. The unit travels WITH the value deliberately - see
    # MscCheck, where mixing dollars and percentage points in one field was
    # flagged as a landmine for any future aggregator.
    checks_json = Column(Text, default="[]")

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OptimizationOutcome(Base):
    """Did a reported optimization actually get applied?

    THE GAP, 2026-10-01. The database held 204 OPTIMIZATION rows worth
    $31,000 and **not one column recording whether any of them was acted
    on**. "We found $31k" and "we saved $31k" are different claims and the
    schema could not tell them apart.

    Two ways a row lands here.

    1. A HUMAN presses Verify in the GUI. Neon 2026-10-01: *"once it is
       verified it is removed from the least because it means it was
       optimized and it is not needed in the GUI list anymore."*

    2. The SCANNER works it out by itself. Neon, same message: *"if the
       price was originally 1500 and we found a drop to 1400, then a human
       saved and was not verified, after 3 days if the script scan this
       booking and it is saved to 1400 it should understand on its own."*

       The signal is exact and needs no new data: a LATER scan's
       `old_total` equals an EARLIER optimization's `new_total`. The quoted
       price became the price being paid, which only happens if someone
       applied it.

       Measured over the whole history before this table existed: **46
       applied repricings worth $5,485.90** that the system had no record
       of - including $943.00 on booking 3001009, quoted 2026-09-18 and
       confirmed in place by the 2026-09-21 scan.

    SCOPED TO THE FIGURES, NOT THE BOOKING. A verification stores the
    old/new pair it refers to. If the same booking is later quoted a
    DIFFERENT saving, that is a new opportunity and must appear in the list
    again - verifying $79 off today cannot silence a $300 drop next month.

    NOT an exclusion. The booking keeps being scanned; only the GUI row is
    retired. See PermanentExclusion for the "never scan this again" case,
    which is a different decision with a much stricter evidence bar.
    """

    __tablename__ = "optimization_outcomes"
    __table_args__ = (
        Index("ix_outcome_booking_line", "booking_id", "cruise_line"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(String(50), nullable=False, index=True)
    cruise_line = Column(String(20), nullable=False, index=True)

    # The opportunity this verification refers to. Matching is on these.
    old_total = Column(Float, nullable=True)
    new_total = Column(Float, nullable=True)
    net_saving = Column(Float, nullable=True)

    # APPLIED  - the reprice was done, the client is paying the new price.
    # REVIEWED - a human looked and decided no action (e.g. a TRAP).
    outcome = Column(String(20), nullable=False, default="APPLIED")
    # "human" (Verify button) or "auto" (detected by the scanner).
    verified_by = Column(String(10), nullable=False, default="human")
    evidence = Column(Text, default="")

    verified_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Set if a verification is withdrawn. The row stays, so the decision
    # remains auditable - same convention as PermanentExclusion.
    cleared_at = Column(DateTime, nullable=True)


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
