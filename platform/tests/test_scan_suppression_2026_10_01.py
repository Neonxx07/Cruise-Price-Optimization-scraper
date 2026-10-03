"""The same list must not be rescanned within 2 hours.

Neon 2026-10-01: *"the problem is the script is still scanning i do not
want it to scan because it is the same list it should not scan again it
should gave me the same results and that these bookings were already
scanned ... at least in a frame of 2 hours."*

WHY THE EXISTING CACHE DID NOT COVER THIS. `CacheService` is a PER-BOOKING
freshness cache: the scan starts, opens a browser, walks the list and skips
individual bookings checked recently. It cannot stop a scan from starting,
because by the time it is consulted the scan is already running.

Nothing modelled the REQUEST, so every press of Start was a fresh run by
definition. This adds that layer.

AND, SEPARATELY, WHY TODAY'S RUN RESCANNED EVERYTHING ANYWAY: the
calculator fingerprint added hours earlier (roadmap P1.2) invalidated every
cache row written before it - 735 of 752. That was correct behaviour and it
is also why the per-booking cache skipped nothing. Two different causes, one
symptom.
"""

import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from core.models import ScanJobStatus
from core.scan_signature import (
    describe_overlap,
    normalise_booking_ids,
    scan_signature,
)

LINE = "ESPRESSO"


@pytest_asyncio.fixture
async def service(tmp_path, monkeypatch):
    import models.database as db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    monkeypatch.setattr(db, "async_session", factory)

    import services.booking_service as mod
    monkeypatch.setattr(mod, "async_session", factory)
    return mod.BookingService()


@pytest_asyncio.fixture
async def completed_scan(service):
    """Record a finished scan, as a real run would leave it."""
    import models.database as db

    async def _add(booking_ids, minutes_ago=10, line=LINE,
                   status=ScanJobStatus.COMPLETED.value, bypass_cache=False):
        finished = datetime.utcnow() - timedelta(minutes=minutes_ago)
        async with db.async_session() as session:
            session.add(db.ScanJobRecord(
                job_id=f"job-{minutes_ago}-{len(booking_ids)}-{status}",
                booking_ids_json=json.dumps(list(booking_ids)),
                cruise_line=line, status=status,
                progress_total=len(booking_ids),
                progress_done=len(booking_ids),
                signature=scan_signature(line, booking_ids,
                                         bypass_cache=bypass_cache),
                started_at=finished - timedelta(minutes=5),
                completed_at=finished,
            ))
            await session.commit()
    return _add


# ── the signature itself ─────────────────────────────────────────────────


def test_the_same_set_in_a_different_order_is_the_same_scan():
    assert scan_signature(LINE, ["1", "2", "3"]) == scan_signature(LINE, ["3", "1", "2"])


def test_duplicates_are_normalised_away():
    assert scan_signature(LINE, ["1", "2", "2", "1"]) == scan_signature(LINE, ["1", "2"])


def test_whitespace_and_blanks_are_normalised():
    assert normalise_booking_ids([" 1 ", "", "2", "  "]) == ["1", "2"]


def test_a_removed_booking_changes_the_scan():
    assert scan_signature(LINE, ["1", "2", "3"]) != scan_signature(LINE, ["1", "2"])


def test_an_added_booking_changes_the_scan():
    assert scan_signature(LINE, ["1", "2"]) != scan_signature(LINE, ["1", "2", "3"])


def test_a_different_cruise_line_is_a_different_scan():
    """Booking numbers are only unique within a portal."""
    assert scan_signature("ESPRESSO", ["1"]) != scan_signature("NCL", ["1"])


def test_force_live_recheck_is_a_different_request():
    """Otherwise an ordinary scan would suppress a forced one, and the
    checkbox would silently do nothing."""
    assert (scan_signature(LINE, ["1"])
            != scan_signature(LINE, ["1"], bypass_cache=True))


def test_ids_are_compared_as_strings_not_numbers():
    """"0012" and "12" are different bookings to the portal."""
    assert scan_signature(LINE, ["0012"]) != scan_signature(LINE, ["12"])


def test_the_signature_is_stable_across_runs():
    """It is persisted, so it must not depend on anything per-process."""
    assert scan_signature(LINE, ["1", "2"]) == scan_signature(LINE, ["1", "2"])


# ── the 2-hour rule ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_identical_list_inside_the_window_is_suppressed(service, completed_scan):
    await completed_scan(["1", "2", "3"], minutes_ago=10)

    found = await service.recent_identical_scan(LINE, ["1", "2", "3"])

    assert found is not None
    assert found["bookings"] == 3
    assert found["minutes_remaining"] > 0


@pytest.mark.asyncio
async def test_a_reordered_list_is_still_suppressed(service, completed_scan):
    await completed_scan(["1", "2", "3"], minutes_ago=10)
    assert await service.recent_identical_scan(LINE, ["3", "2", "1"]) is not None


@pytest.mark.asyncio
async def test_after_the_window_a_scan_is_allowed(service, completed_scan):
    await completed_scan(["1", "2", "3"], minutes_ago=121)
    assert await service.recent_identical_scan(LINE, ["1", "2", "3"]) is None


@pytest.mark.asyncio
async def test_a_changed_list_is_not_suppressed(service, completed_scan):
    await completed_scan(["1", "2", "3"], minutes_ago=10)
    assert await service.recent_identical_scan(LINE, ["1", "2", "4"]) is None


@pytest.mark.asyncio
async def test_another_cruise_line_is_not_suppressed(service, completed_scan):
    await completed_scan(["1", "2"], minutes_ago=10, line="ESPRESSO")
    assert await service.recent_identical_scan("NCL", ["1", "2"]) is None


@pytest.mark.asyncio
async def test_a_forced_recheck_is_never_suppressed(service, completed_scan):
    """The flag the operator uses precisely to say "ignore what you know"."""
    await completed_scan(["1", "2"], minutes_ago=5)
    assert await service.recent_identical_scan(
        LINE, ["1", "2"], bypass_cache=True) is None


@pytest.mark.asyncio
async def test_the_window_is_configurable(service, completed_scan):
    await completed_scan(["1"], minutes_ago=90)
    assert await service.recent_identical_scan(LINE, ["1"], within_hours=1) is None
    assert await service.recent_identical_scan(LINE, ["1"], within_hours=4) is not None


@pytest.mark.asyncio
async def test_a_zero_window_disables_suppression(service, completed_scan):
    await completed_scan(["1"], minutes_ago=1)
    assert await service.recent_identical_scan(LINE, ["1"], within_hours=0) is None


# ── what must NEVER suppress a scan ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_scan_does_not_suppress(service, completed_scan):
    """An interrupted run is not an answer. Reusing one would hide exactly
    the work that needs redoing."""
    await completed_scan(["1", "2"], minutes_ago=10, status="FAILED")
    assert await service.recent_identical_scan(LINE, ["1", "2"]) is None


@pytest.mark.asyncio
async def test_a_stopped_scan_does_not_suppress(service, completed_scan):
    await completed_scan(["1", "2"], minutes_ago=10, status="STOPPED")
    assert await service.recent_identical_scan(LINE, ["1", "2"]) is None


@pytest.mark.asyncio
async def test_a_still_running_scan_does_not_suppress(service, completed_scan):
    await completed_scan(["1", "2"], minutes_ago=10, status="RUNNING")
    assert await service.recent_identical_scan(LINE, ["1", "2"]) is None


@pytest.mark.asyncio
async def test_no_history_at_all_allows_the_scan(service):
    assert await service.recent_identical_scan(LINE, ["1", "2"]) is None


@pytest.mark.asyncio
async def test_an_empty_queue_is_not_suppressed_by_an_empty_history(service):
    assert await service.recent_identical_scan(LINE, []) is None


@pytest.mark.asyncio
async def test_a_lookup_failure_lets_the_scan_proceed(service, monkeypatch):
    """Fails OPEN. A redundant scan costs time; a wrongly suppressed one
    costs a real client's price drop."""
    import services.booking_service as mod

    def boom():
        raise RuntimeError("database gone")

    monkeypatch.setattr(mod, "async_session", boom)
    assert await service.recent_identical_scan(LINE, ["1"]) is None


# ── it survives a restart ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_suppression_state_is_in_the_database_not_memory(service, completed_scan):
    """A fresh service object - as after an application restart - must see
    the same history."""
    import services.booking_service as mod

    await completed_scan(["1", "2", "3"], minutes_ago=10)
    restarted = mod.BookingService()

    assert await restarted.recent_identical_scan(LINE, ["1", "2", "3"]) is not None


# ── the plan the GUI shows ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_plan_says_reuse_and_when(service, completed_scan):
    await completed_scan(["1", "2"], minutes_ago=10)

    plan = await service.scan_plan(LINE, ["2", "1"])

    assert plan["action"] == "reuse"
    assert plan["scanned_at"] is not None
    assert plan["next_allowed_at"] > plan["scanned_at"]


@pytest.mark.asyncio
async def test_the_plan_describes_a_partially_changed_list(service, completed_scan):
    """A list that gained one booking should cost one booking, and the
    operator should be told that rather than left guessing."""
    await completed_scan(["1", "2", "3"], minutes_ago=10)

    plan = await service.scan_plan(LINE, ["1", "2", "3", "4"])

    assert plan["action"] == "scan"
    assert plan["overlap"]["shared"] == 3
    assert plan["overlap"]["added"] == 1
    assert plan["overlap"]["removed"] == 0


@pytest.mark.asyncio
async def test_the_plan_counts_a_removed_booking(service, completed_scan):
    await completed_scan(["1", "2", "3"], minutes_ago=10)

    plan = await service.scan_plan(LINE, ["1", "2"])

    assert plan["overlap"]["removed"] == 1
    assert plan["overlap"]["added"] == 0


def test_overlap_reports_an_identical_set():
    assert describe_overlap(["1", "2"], ["2", "1"])["identical"] is True
    assert describe_overlap(["1", "2"], ["1", "3"])["identical"] is False


def test_overlap_of_two_empty_lists_is_not_identical():
    """Nothing vs nothing is not a match worth reusing."""
    assert describe_overlap([], [])["identical"] is False


# ── the GUI honours it ───────────────────────────────────────────────────


def test_start_checks_the_plan_before_opening_a_browser():
    """Structural, from the AST. The check must happen in _on_start_guarded,
    or the suppression exists and does nothing."""
    import ast
    import pathlib

    source = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    func = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_start_guarded")

    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "scan_plan"]
    assert calls, "Start no longer checks whether this list was just scanned"


def test_start_passes_the_force_recheck_flag():
    """Otherwise ticking the box would not escape suppression."""
    import pathlib

    source = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    start = source.index("scan_plan(")
    window = source[start:start + 300]
    assert "force_recheck_checkbox.isChecked()" in window
