"""Don't re-open a booking the database already knows about.

Neon 2026-09-29: *"the price usually changes every 24 hours for example or
12 hours or 6 hours ... if it was scanned already we need a information
about this ... so it does not open the booking and scans over again"*.

MEASURED over 24 hours of real scans:

    rows in the last 24h     : 1393
    bookings scanned >1 time : 495
    REDUNDANT scans          : 565   (41% of all work, ~148 minutes)

and of 55 repeats where the total could be compared across both scans,
**55 were identical and 0 had changed**.

The old cache had one writer (`set_no_saving`) gated on
`status == NO_SAVING`, which is why that 41% survived it: PAID_IN_FULL alone
accounted for 439 of the repeats. It also stored only a timestamp, leaving
`value_json` unused, so a skipped booking showed "scanned 1.4h ago" with
every price column blank.
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from config.settings import settings
from services.cache_service import CacheService


@pytest_asyncio.fixture
async def cache(tmp_path, monkeypatch):
    import models.database as db
    import services.cache_service as mod
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'f.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    monkeypatch.setattr(mod, "async_session", factory)
    return CacheService()


# ── per-line windows ─────────────────────────────────────────────────────


def test_each_line_gets_its_own_window():
    """One global TTL could not express "24 hours for example or 12 hours or
    6 hours"."""
    assert settings.freshness_for("ESPRESSO") == 12
    assert settings.freshness_for("NCL") == 6
    assert settings.freshness_for("GOCCL") == 24


def test_an_unknown_line_falls_back_to_the_default():
    """A new cruise line must work before anyone remembers to configure it."""
    assert settings.freshness_for("SOMETHING_NEW") == settings.cache_ttl_hours


def test_the_window_lookup_is_case_insensitive():
    assert settings.freshness_for("espresso") == 12


def test_windows_are_configurable_not_hard_coded():
    """Requirement: editable in settings, not buried in the scanner."""
    assert isinstance(settings.freshness_hours, dict)
    assert "ESPRESSO" in settings.freshness_hours


# ── which outcomes may be cached at all ──────────────────────────────────


@pytest.mark.parametrize("status", ["NO_SAVING", "PAID_IN_FULL", "WLT",
                                    "TRAP", "NOT_ON_THIS_ACCOUNT"])
def test_ordinary_outcomes_are_cacheable(status):
    """PAID_IN_FULL alone was 439 of the redundant scans."""
    assert CacheService().is_cacheable(status)


@pytest.mark.parametrize("status", ["OPTIMIZATION", "ERROR", "CANCELLED",
                                    "UNKNOWN"])
def test_these_outcomes_are_never_cached(status):
    """A live saving must always be re-confirmed; a failure is not an
    outcome; and every cancellation must be reported on every run."""
    assert not CacheService().is_cacheable(status)


@pytest.mark.asyncio
async def test_an_optimization_is_not_remembered(cache):
    assert await cache.set_result("ESPRESSO", "OPT", status="OPTIMIZATION") is False
    assert await cache.get("ESPRESSO", "OPT") is None


@pytest.mark.asyncio
async def test_an_error_is_not_remembered(cache):
    """Caching a failure turns one bad page load into a booking nobody
    looks at again until the window expires."""
    assert await cache.set_result("ESPRESSO", "E", status="ERROR") is False


# ── fresh / stale / never scanned ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recent_scan_is_fresh(cache):
    await cache.set_result("ESPRESSO", "A", status="NO_SAVING")
    entry = await cache.get("ESPRESSO", "A")
    assert entry is not None
    assert entry["hours_ago"] < 0.1


@pytest.mark.asyncio
async def test_a_booking_never_scanned_is_not_fresh(cache):
    assert await cache.get("ESPRESSO", "NEVER") is None


@pytest.mark.asyncio
async def test_an_expired_entry_is_not_fresh(cache):
    """Window edge: an entry past its expiry must be rescanned."""
    import services.cache_service as mod
    from models.database import CacheEntry
    await cache.set_result("ESPRESSO", "OLD", status="NO_SAVING")
    async with mod.async_session() as s:
        from sqlalchemy import update
        await s.execute(update(CacheEntry).values(
            expires_at=datetime.utcnow() - timedelta(minutes=1)))
        await s.commit()
    assert await cache.get("ESPRESSO", "OLD") is None


@pytest.mark.asyncio
async def test_the_same_booking_on_another_line_is_separate(cache):
    """Booking numbers are only unique within a portal."""
    await cache.set_result("ESPRESSO", "12345", status="NO_SAVING")
    assert await cache.get("NCL", "12345") is None


# ── the stored figures travel with the entry ─────────────────────────────


@pytest.mark.asyncio
async def test_the_prices_are_stored_and_returned(cache):
    """Requirement: "Show the stored price data for skipped bookings just
    like freshly scanned ones." The old cache stored only a timestamp."""
    await cache.set_result("ESPRESSO", "P", status="NO_SAVING",
                           payload={"old_total": 6627.0, "new_total": 5924.0,
                                    "net_saving": 703.0, "currency": "USD"})
    entry = await cache.get("ESPRESSO", "P")
    assert entry["status"] == "NO_SAVING"
    assert entry["data"]["old_total"] == 6627.0
    assert entry["data"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_the_scan_time_is_reported_for_display(cache):
    """The GUI shows "1h 23m ago" and the exact timestamp on hover."""
    await cache.set_result("ESPRESSO", "T", status="WLT")
    entry = await cache.get("ESPRESSO", "T")
    assert isinstance(entry["scanned_at"], datetime)
    assert entry["hours_ago"] >= 0


# ── bulk lookup ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_whole_watchlist_is_looked_up_at_once(cache):
    """723 round trips before the first page loads is the thing being
    removed."""
    await cache.set_result("ESPRESSO", "A", status="NO_SAVING")
    await cache.set_result("ESPRESSO", "C", status="PAID_IN_FULL")
    found = await cache.get_many("ESPRESSO", ["A", "B", "C", "D"])
    assert set(found) == {"A", "C"}
    assert found["C"]["status"] == "PAID_IN_FULL"


@pytest.mark.asyncio
async def test_an_empty_list_asks_nothing(cache):
    assert await cache.get_many("ESPRESSO", []) == {}


# ── force rescan ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_booking_can_be_forced_to_rescan(cache):
    await cache.set_result("ESPRESSO", "F", status="NO_SAVING")
    assert await cache.clear_one("ESPRESSO", "F") is True
    assert await cache.get("ESPRESSO", "F") is None


@pytest.mark.asyncio
async def test_forcing_a_booking_that_was_never_cached_is_harmless(cache):
    assert await cache.clear_one("ESPRESSO", "nope") is False


# ── robustness ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rescan_refreshes_rather_than_duplicating(cache):
    await cache.set_result("ESPRESSO", "R", status="NO_SAVING")
    await cache.set_result("ESPRESSO", "R", status="PAID_IN_FULL")
    entry = await cache.get("ESPRESSO", "R")
    assert entry["status"] == "PAID_IN_FULL"


@pytest.mark.asyncio
async def test_unreadable_stored_json_does_not_break_the_lookup(cache):
    """A corrupt row must cost that booking a rescan, not the whole batch.

    CORRECTED 2026-10-01. The assertion used to be
    `entry is not None and entry["data"] == {}` - which contradicted this
    docstring. Returning an entry means the booking is SKIPPED, served from
    a row whose contents could not be read, and displayed with every price
    column blank. That is the exact "scanned 1.4h ago, no figures" symptom
    the freshness work set out to remove.

    The calculator fingerprint added for roadmap P1.2 made the behaviour
    match the intent: an unparseable row carries no fingerprint, so it is a
    miss and the booking is scanned again. The lookup still does not throw,
    which is what the test name is about, and the rest of the batch is
    unaffected.
    """
    import services.cache_service as mod
    from sqlalchemy import update

    from models.database import CacheEntry
    await cache.set_result("ESPRESSO", "J", status="NO_SAVING")
    async with mod.async_session() as s:
        await s.execute(update(CacheEntry).values(value_json="{not json"))
        await s.commit()

    # No exception, and the booking is re-scanned rather than skipped blank.
    assert await cache.get("ESPRESSO", "J") is None
