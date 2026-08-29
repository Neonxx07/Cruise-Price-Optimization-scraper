"""Regression tests for the 2026-08-27 full-project forensic review.

Every bug here was found by querying the REAL cruise_intel.db (4,508+ rows
of live ESPRESSO/NCL/GoCCL history), not by reading code.

1. **14 of 26 computed fields were never persisted** — including
   `obc_change` (the field the entire OBC rule turns on) and
   `old_promos`/`new_promos` (added specifically to make a LATRIPLE TRAP
   verdict auditable). The system could not audit its own money decisions.

2. **89 rows are TRAP/NO_SAVING at confidence 4-5** — e.g. booking 3000061
   TRAP net=$297 conf=5, 3000043 TRAP net=$588 conf=5, 3000042 TRAP
   net=$768 conf=4. `calc_confidence` never receives the final status, so a
   "do not do this" verdict outranks genuine wins in any confidence-sorted
   report.

3. **24 scan_jobs rows stuck at RUNNING**, oldest 2026-07-19, one with
   progress_total=623, all with progress_done=0 and completed_at=NULL —
   owning processes died before the `finally` could write a terminal
   status, and nothing reconciled them afterwards.

4. **Tests wrote 78 junk rows into the production database** (booking IDs
   "A"/"B"/"C", 26 each) because a monkeypatch named a method that does not
   exist and used `raising=False`. See tests/conftest.py.

5. **SharedBrowserPool keys sessions by cruise line only**, while NCL now
   has per-market (US/CA) accounts — a latent cross-account session leak.
"""
import asyncio
import json
import os

import pytest

from core.calculator import calculate_espresso, calculate_ncl
from core.models import BookingResult, BookingStatus, CruiseLine, ScanJobStatus


# ── 1. persistence completeness ──────────────────────────────────


def test_every_booking_result_field_has_a_database_column():
    """The guard that would have caught all 14 dropped fields at once. A
    field computed on every check and silently discarded is worse than a
    missing feature — the report looks complete and is not."""
    from models.database import BookingRecord

    result_fields = set(BookingResult.model_fields)
    columns = {c.name for c in BookingRecord.__table__.columns}
    # checked_at maps onto the DB's own created_at.
    missing = result_fields - columns - {"checked_at"}
    assert not missing, f"fields computed but never persisted: {sorted(missing)}"


def test_the_money_audit_fields_specifically_exist():
    """Named explicitly so a future refactor cannot quietly drop the ones
    that make a verdict auditable."""
    from models.database import BookingRecord

    columns = {c.name for c in BookingRecord.__table__.columns}
    for field in ("obc_change", "price_drop", "lost_pkg_value",
                  "old_promos", "new_promos", "lost_fares", "currency"):
        assert field in columns, f"{field} is not persisted"


def test_new_columns_are_nullable_so_old_rows_stay_honest():
    """Pre-existing rows must read back NULL ("not recorded"), never 0.0 —
    a fabricated zero is indistinguishable from a real measured "no OBC
    change" and would corrupt every historical audit."""
    from models.database import BookingRecord

    for name in ("obc_change", "price_drop", "lost_pkg_value"):
        col = BookingRecord.__table__.columns[name]
        assert col.nullable, f"{name} must be nullable"
        assert col.default is None, f"{name} must not fabricate a default"


# ── 2. confidence must never rank a rejection like a win ─────────


def _espresso_raw(old_total, new_total, obc_old="0", obc_new="0"):
    return {
        "oldInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": str(old_total)},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": obc_old},
        ]},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": str(new_total)},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": obc_new},
        ]},
    }


def test_espresso_obc_rejection_cannot_score_above_two():
    """THE 89-row bug. A $160 drop that forfeits $150 of OBC is a ~1.1x
    margin -> NO_SAVING, but the fare direction alone used to score it 4-5.
    Real examples in the DB: 3000061 TRAP conf=5, 3000043 TRAP conf=5."""
    r = calculate_espresso(
        _espresso_raw(1000.0, 840.0, obc_old="150", obc_new="0"), "E-OBC", "IB",
    )
    assert r.status in (BookingStatus.NO_SAVING, BookingStatus.TRAP)
    assert r.confidence <= 2, (
        f"a rejection scored {r.confidence} — it will sort next to real wins"
    )


def test_espresso_real_optimization_keeps_its_high_confidence():
    """The cap must not flatten genuine wins — otherwise it destroys the
    ranking it was added to protect."""
    r = calculate_espresso(_espresso_raw(1000.0, 800.0), "E-OK", "IB")
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.confidence >= 3


@pytest.mark.parametrize("status_maker", ["trap", "no_saving"])
def test_no_cruise_line_ranks_a_rejection_above_two(status_maker):
    """Cross-line invariant: ESPRESSO and NCL must agree that a rejection is
    not a high-confidence result."""
    if status_maker == "trap":
        r = calculate_ncl(
            "N-T", "BF", 1817.60, 1760.60,
            [{"guest": "A", "name": "Free $100 On-Board Credit Certificate"}],
            "", "", new_addons=[],
        )
    else:
        r = calculate_ncl(
            "N-N", "BF", 2257.00, 2197.00,
            [{"guest": "A", "name": "Free $50 On-Board Credit Certificate"}],
            "", "", new_addons=[],
        )
    assert r.status in (BookingStatus.TRAP, BookingStatus.NO_SAVING)
    assert r.confidence <= 2


# ── 3. stale scan_jobs reconciliation ────────────────────────────


@pytest.mark.asyncio
async def test_stale_running_jobs_are_reconciled_to_failed():
    """24 real rows were stuck RUNNING with completed_at=NULL, the oldest
    from 2026-07-19. A RUNNING row that no process owns is a lie about
    system state, and the GUI polls while a job reads PENDING/RUNNING."""
    from datetime import datetime, timedelta

    from models.database import ScanJobRecord, async_session, init_db
    from services.booking_service import BookingService

    await init_db()
    old = datetime.utcnow() - timedelta(hours=48)
    async with async_session() as s:
        s.add(ScanJobRecord(
            job_id="stale-1", booking_ids_json="[]", cruise_line="ESPRESSO",
            status="RUNNING", progress_done=0, progress_total=623, started_at=old,
        ))
        await s.commit()

    n = await BookingService().reconcile_stale_jobs(max_age_hours=12.0)
    assert n >= 1

    from sqlalchemy import select
    async with async_session() as s:
        rec = (await s.execute(
            select(ScanJobRecord).where(ScanJobRecord.job_id == "stale-1")
        )).scalar_one()
        assert rec.status == ScanJobStatus.FAILED.value
        assert rec.completed_at is not None


@pytest.mark.asyncio
async def test_a_recent_running_job_is_left_alone():
    """CRITICAL not to over-reach: multiple processes legitimately coexist
    (four live gui.main processes were observed during this review), so
    blanket-failing every RUNNING row would kill a healthy concurrent scan."""
    from datetime import datetime

    from models.database import ScanJobRecord, async_session, init_db
    from services.booking_service import BookingService
    from sqlalchemy import select

    await init_db()
    async with async_session() as s:
        s.add(ScanJobRecord(
            job_id="fresh-1", booking_ids_json="[]", cruise_line="NCL",
            status="RUNNING", progress_done=3, progress_total=167,
            started_at=datetime.utcnow(),
        ))
        await s.commit()

    await BookingService().reconcile_stale_jobs(max_age_hours=12.0)
    async with async_session() as s:
        rec = (await s.execute(
            select(ScanJobRecord).where(ScanJobRecord.job_id == "fresh-1")
        )).scalar_one()
        assert rec.status == "RUNNING", "a live concurrent scan was killed"


# ── 4. tests must be unable to reach production data ─────────────


def test_the_suite_is_pointed_at_a_throwaway_database():
    """78 junk rows reached the real DB because one monkeypatch named a
    method that does not exist. This is the alarm."""
    from config.settings import settings

    url = settings.database_url.replace("\\", "/").lower()
    assert "cruise_intel.db" not in url, (
        f"tests are pointed at the production database: {settings.database_url}"
    )
    assert "cruiseintel_test_" in url


def test_the_methods_tests_neutralise_actually_exist():
    """The root cause was a typo'd method name silenced by raising=False.
    Pin the real names so a rename breaks a test instead of leaking writes."""
    from services.booking_service import BookingService

    for name in ("_save_result_to_db", "_save_job_to_db", "_update_job_in_db",
                 "_save_price_history", "_save_market_data_to_db"):
        assert hasattr(BookingService, name), f"{name} no longer exists"
    assert not hasattr(BookingService, "_persist_result"), (
        "a method by this name now exists — the old patch would silently "
        "start working and mask the lesson"
    )


# ── 5. browser pool cannot leak an NCL account ───────────────────


@pytest.mark.asyncio
async def test_pool_refuses_a_non_default_ncl_market():
    """Contexts and storage_state files in the pool are keyed by cruise
    line ONLY, but NCL has per-market accounts. Silently handing a CA
    caller the US session would report every Canadian booking as
    "Reservation is not found" — the bug per-market sessions exist to fix."""
    from scraper.browser_pool import SharedBrowserPool

    pool = SharedBrowserPool()
    # No browser needed: the guard must refuse BEFORE launching Chromium.
    # Paying for a browser start and then raising would be wasteful and
    # would leave a stray process behind on the failure path.
    with pytest.raises(ValueError, match="market"):
        await pool.acquire_context(CruiseLine.NCL, market="CA")


def test_pool_and_scraper_agree_on_the_default_session_filename():
    """A session saved by the standalone scraper must be found by the
    pooled path and vice versa — they are documented as byte-identical."""
    from scraper.browser_pool import SharedBrowserPool
    from scraper.ncl import NclScraper

    pooled = SharedBrowserPool._storage_state_path(CruiseLine.NCL)
    standalone = NclScraper()._storage_state_path()
    assert pooled is not None and standalone is not None
    assert os.path.basename(pooled) == os.path.basename(standalone)


# ── 6. cross-line invariants on the calculators ──────────────────


def test_no_calculator_reports_an_optimization_worth_nothing():
    """An OPTIMIZATION with net_saving <= 0 is a contradiction. The live DB
    has 0 of these; this keeps it that way."""
    cases = [
        calculate_ncl("A", "BF", 1000.0, 1000.0, [], "", "", new_addons=[]),
        calculate_ncl("B", "BF", 1000.0, 1100.0, [], "", "", new_addons=[]),
        calculate_espresso(_espresso_raw(1000.0, 1000.0), "C", "IB"),
        calculate_espresso(_espresso_raw(1000.0, 1200.0), "D", "IB"),
    ]
    for r in cases:
        if r.status == BookingStatus.OPTIMIZATION:
            assert r.net_saving > 0, f"{r.booking_id}: OPTIMIZATION with net {r.net_saving}"


def test_no_calculator_reports_an_optimization_that_costs_more():
    """new_total >= old_total can never be an optimization. 0 such rows in
    the live DB — pinned."""
    for r in (calculate_ncl("E", "BF", 1000.0, 1100.0, [], "", "", new_addons=[]),
              calculate_espresso(_espresso_raw(1000.0, 1100.0), "F", "IB")):
        assert r.status != BookingStatus.OPTIMIZATION


# ── 7. dead-transport vs page-level error classification ─────────


@pytest.mark.parametrize("msg", [
    "Target closed",
    "Protocol error: Target.detachFromTarget",
    "WebSocket closed unexpectedly",
    "Connection closed while reading from the driver",
    "connect ECONNREFUSED 127.0.0.1:9222",
    "Browser has been closed",
    "Page crashed",
])
def test_dead_transport_errors_trigger_the_restart_path(msg):
    """WIDENED 2026-08-27. Five of these were previously unmatched
    ("protocol error", "websocket", "connection closed", "econnrefused",
    "browser closed"), so when the browser died that way BookingService's
    restart never fired and every remaining booking failed one at a time
    against a corpse."""
    from scraper.base import is_dead_browser_error

    assert is_dead_browser_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "net::ERR_CONNECTION_RESET at https://secure.cruisingpower.com",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_INTERNET_DISCONNECTED",
    "Timeout 25000ms exceeded waiting for #sideBar",
    "NCL portal error: Reservation is not found",
    "Category 'IX' not found in grid data",
])
def test_page_level_errors_must_not_be_treated_as_a_dead_browser(msg):
    """CRITICAL asymmetry. A `net::ERR_*` means ONE navigation failed while
    the browser is healthy. Classifying it as dead triggers a browser
    restart, and on ESPRESSO a single close-and-reopen breaks the session
    outright (DOCUMENTATION.md section L) — so a false positive here does
    real damage, unlike a false negative which merely wastes a retry."""
    from scraper.base import is_dead_browser_error

    assert is_dead_browser_error(Exception(msg)) is False


# ── 8. calculate -> save -> reload round trip (PERMANENT) ────────


@pytest.mark.asyncio
async def test_every_field_survives_calculate_save_reload():
    """The permanent guard the review asked for. A column existing is not
    the same as a value SURVIVING — this drives the real save path and
    reads every field back out of SQLite.

    Uses a real calculator output rather than a hand-built object, so a
    field the calculator populates but the save path drops still fails."""
    from sqlalchemy import select

    from models.database import BookingRecord, async_session, init_db
    from services.booking_service import BookingService

    await init_db()

    computed = calculate_ncl(
        "ROUNDTRIP1", "BF", 1817.60, 1760.60,
        [{"guest": "MS A", "name": "Free $100 On-Board Credit Certificate"}],
        old_promos="LATRIPLE,FREESRVC", new_promos="FREESRVC",
        new_addons=[],
    )
    # Sanity: the case must actually exercise the interesting fields.
    assert computed.obc_change == -100.0
    assert computed.old_promos and computed.lost_fares

    await BookingService()._save_result_to_db(computed)

    async with async_session() as s:
        rec = (await s.execute(
            select(BookingRecord).where(BookingRecord.booking_id == "ROUNDTRIP1")
        )).scalars().first()

    assert rec is not None, "the result was not persisted at all"
    assert rec.obc_change == computed.obc_change, "obc_change did not survive"
    assert rec.price_drop == computed.price_drop
    assert rec.lost_pkg_value == computed.lost_pkg_value
    assert rec.old_promos == computed.old_promos
    assert rec.new_promos == computed.new_promos
    assert rec.currency == computed.currency
    assert json.loads(rec.lost_fares) == computed.lost_fares
    assert rec.net_saving == computed.net_saving
    assert rec.status == computed.status.value
    assert rec.confidence == computed.confidence


# ── 9. unconfirmed candidates must not inflate savings ───────────


def test_self_declared_unconfirmed_rows_are_excluded_from_savings():
    """CONFIRMED MONEY-REPORTING BUG: $4,100.00 of the $13,053.81 all-time
    reported savings (31.4%) came from 5 GoCCL rows whose own note says
    "UNCONFIRMED, run preview_fare_code to verify", at confidence 1.
    DEMO02 was counted twice ($880 + $500), and DEMO01/DEMO02 were
    re-scanned the same day as NO_SAVING."""
    from core.calculator import total_optimization_savings, unconfirmed_candidate_total

    results = [
        BookingResult(booking_id="REAL", cruise_line=CruiseLine.ESPRESSO,
                      status=BookingStatus.OPTIMIZATION, net_saving=100.0, confidence=5),
        BookingResult(booking_id="DEMO01", cruise_line=CruiseLine.GOCCL,
                      status=BookingStatus.OPTIMIZATION, net_saving=1560.0, confidence=1,
                      note="candidate $1560 — UNCONFIRMED, run preview_fare_code to verify"),
    ]
    assert total_optimization_savings(results) == 100.0
    assert unconfirmed_candidate_total(results) == (1, 1560.0)


def test_a_low_confidence_but_confirmed_win_still_counts():
    """The exclusion must key on the self-declared note, NOT on confidence
    or cruise line — otherwise it would silently drop real ESPRESSO wins
    that merely scored low (several real rows sit at confidence 2)."""
    from core.calculator import total_optimization_savings

    results = [BookingResult(
        booking_id="LOWCONF", cruise_line=CruiseLine.ESPRESSO,
        status=BookingStatus.OPTIMIZATION, net_saving=50.0, confidence=2,
        note="optimized $50 — re-add: BONUS OBC NRD",
    )]
    assert total_optimization_savings(results) == 50.0


# ── 10. addon scrape failure semantics ───────────────────────────


def test_addon_scrape_failure_is_not_treated_as_no_addons():
    """`_scrape_addons` returned [] for an exception, a missing table, AND a
    booking with genuinely no addons. With the before/after OBC diff that is
    wrong in BOTH directions — a failed BEFORE list hides a real forfeited
    certificate and reports a clean OPTIMIZATION (the 3000055 false
    positive by another route)."""
    r = calculate_ncl("AF1", "BF", 2000.0, 1600.0, None, "", "",
                      new_addons=None, addon_scrape_failed=True)
    assert r.confidence <= 2, "an unverifiable loss must not be high confidence"
    assert "could not be read" in r.note


def test_genuinely_empty_addons_is_not_penalised():
    """An empty LIST is a real answer ("this booking has no addons") and must
    keep full confidence — otherwise the fix would suppress real wins."""
    r = calculate_ncl("AF2", "BF", 2000.0, 1600.0, [], "", "",
                      new_addons=[], addon_scrape_failed=False)
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.confidence == 5
    assert "could not be read" not in r.note
