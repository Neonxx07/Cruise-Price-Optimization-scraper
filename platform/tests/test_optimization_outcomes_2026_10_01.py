"""A reported optimization has to be verifiable, by a human or by itself.

Neon 2026-10-01: *"if the price was originally 1500 and we found a drop to
1400, then a human saved and was not verified, after 3 days for example, if
the script scan this booking and it is saved to 1400 it should understand on
its own that is a priority to add it. that smart. and next is just add a
verify button in the gui, and once it is verified it is removed from the
least because it means it was optimized."*

THE GAP. Before this, the database held 204 OPTIMIZATION rows worth $31,000
and no column saying whether any was acted on. "We found $31k" and "we saved
$31k" are different claims.

WHAT THE DETECTOR FOUND on the real history the moment it existed: **46
applied repricings worth $5,485.90** nobody had recorded - largest $943.00
on booking 3001009, quoted 2026-09-18 and confirmed by the 2026-09-21 scan.

The tests that matter most here are the REFUSALS. A verification retires a
row from the GUI, so a wrong one hides money.
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from services.outcome_service import APPLIED, REVIEWED, OutcomeService, same_amount

LINE = "ESPRESSO"


@pytest_asyncio.fixture
async def service(tmp_path, monkeypatch):
    """A real service against a throwaway database."""
    import models.database as db
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    monkeypatch.setattr(db, "async_session", session_factory)
    import services.outcome_service as mod
    monkeypatch.setattr(mod, "async_session", session_factory)
    return OutcomeService()


@pytest_asyncio.fixture
async def add_scan(service):
    """Append one scan row for a booking, oldest first."""
    import models.database as db
    base = datetime(2026, 9, 1, 12, 0, 0)
    counter = {"n": 0}

    async def _add(booking_id, status, old_total, new_total, net_saving=0.0,
                   line=LINE, day=None):
        counter["n"] += 1
        async with db.async_session() as session:
            session.add(db.BookingRecord(
                booking_id=str(booking_id), cruise_line=line, status=status,
                old_total=old_total, new_total=new_total, net_saving=net_saving,
                created_at=base + timedelta(days=day if day is not None
                                            else counter["n"]),
            ))
            await session.commit()
    return _add


# ── Neon's exact example ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_1500_to_1400_case_is_detected_without_a_human(service, add_scan):
    """His words: originally 1500, we found 1400, a human saved it and never
    pressed Verify. Three days later the scan opens at 1400."""
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    found = await service.detect_applied()

    assert len(found) == 1
    hit = found[0]
    assert hit["booking_id"] == "B1"
    assert (hit["old_total"], hit["new_total"]) == (1500.0, 1400.0)
    assert hit["net_saving"] == 100.0


@pytest.mark.asyncio
async def test_a_detected_application_is_written_to_the_database(service, add_scan):
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    await service.detect_applied()

    rows = await service.list_verified()
    assert len(rows) == 1
    assert rows[0]["verified_by"] == "auto"
    assert rows[0]["outcome"] == APPLIED
    assert await service.total_realised() == 100.0


@pytest.mark.asyncio
async def test_detection_is_idempotent(service, add_scan):
    """A nightly scan runs this repeatedly; it must not count twice."""
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    await service.detect_applied()
    again = await service.detect_applied()

    assert again == []
    assert len(await service.list_verified()) == 1
    assert await service.total_realised() == 100.0


@pytest.mark.asyncio
async def test_a_dry_run_records_nothing(service, add_scan):
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    found = await service.detect_applied(record=False)

    assert len(found) == 1
    assert await service.list_verified() == []


# ── the refusals: never claim a saving that was not ours ─────────────────


@pytest.mark.asyncio
async def test_a_price_that_never_moved_is_not_an_application(service, add_scan):
    """old == new is not an opportunity, and must not confirm itself."""
    await add_scan("B1", "OPTIMIZATION", 1400.0, 1400.0, 0.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    assert await service.detect_applied() == []


@pytest.mark.asyncio
async def test_an_unread_payment_panel_does_not_confirm(service, add_scan):
    """A later scan with old_total 0.00 means "we could not read it", not
    "the price is now zero" - the same confusion that produced the false
    $400 on booking 3001001."""
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "ERROR", 0.0, 0.0, 0.0, day=4)

    assert await service.detect_applied() == []


@pytest.mark.asyncio
async def test_an_earlier_scan_cannot_confirm_a_later_quote(service, add_scan):
    """Confirmation must come strictly after the quote, or history would
    'confirm' a price that merely used to be in effect."""
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=1)
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=4)

    assert await service.detect_applied() == []


@pytest.mark.asyncio
async def test_a_quote_still_waiting_is_not_claimed(service, add_scan):
    """The real state of Neon's 12 bookings on 2026-10-01: quoted, not yet
    applied. Every later scan still opens at the ORIGINAL price."""
    await add_scan("B1", "OPTIMIZATION", 7328.84, 7129.84, 199.0, day=1)
    await add_scan("B1", "OPTIMIZATION", 7328.84, 7129.84, 199.0, day=2)
    await add_scan("B1", "OPTIMIZATION", 7328.84, 7129.84, 199.0, day=3)

    assert await service.detect_applied() == []


@pytest.mark.asyncio
async def test_a_different_booking_cannot_confirm_this_one(service, add_scan):
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B2", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)

    assert await service.detect_applied() == []


@pytest.mark.asyncio
async def test_the_same_figures_on_another_line_do_not_confirm(service, add_scan):
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4, line="NCL")

    assert await service.detect_applied(cruise_line=LINE) == []


# ── the Verify button ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verifying_retires_that_opportunity(service):
    await service.record(LINE, "3001011", old_total=2468.78, new_total=2389.78,
                         net_saving=79.0)

    verified = await service.verified_pairs(LINE)
    assert service.is_verified(verified, "3001011", 2468.78, 2389.78)


@pytest.mark.asyncio
async def test_a_new_opportunity_on_a_verified_booking_still_shows(service):
    """Verifying $79 off today must not silence a $300 drop next month."""
    await service.record(LINE, "3001011", old_total=2468.78, new_total=2389.78,
                         net_saving=79.0)

    verified = await service.verified_pairs(LINE)
    assert not service.is_verified(verified, "3001011", 2389.78, 2089.78)


@pytest.mark.asyncio
async def test_verifying_twice_records_once(service):
    assert await service.record(LINE, "3001011", old_total=2468.78,
                                new_total=2389.78, net_saving=79.0) is True
    assert await service.record(LINE, "3001011", old_total=2468.78,
                                new_total=2389.78, net_saving=79.0) is False
    assert len(await service.list_verified()) == 1


@pytest.mark.asyncio
async def test_a_verification_can_be_withdrawn(service):
    await service.record(LINE, "3001011", old_total=2468.78, new_total=2389.78,
                         net_saving=79.0)
    assert await service.clear(LINE, "3001011") is True

    verified = await service.verified_pairs(LINE)
    assert not service.is_verified(verified, "3001011", 2468.78, 2389.78)


@pytest.mark.asyncio
async def test_a_human_verification_blocks_the_detector(service, add_scan):
    """No double entry when a human got there first."""
    await add_scan("B1", "OPTIMIZATION", 1500.0, 1400.0, 100.0, day=1)
    await add_scan("B1", "NO_SAVING", 1400.0, 1400.0, 0.0, day=4)
    await service.record(LINE, "B1", old_total=1500.0, new_total=1400.0,
                         net_saving=100.0)

    assert await service.detect_applied() == []
    assert len(await service.list_verified()) == 1


# ── the savings ledger ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reviewed_but_not_applied_counts_as_zero_saved(service):
    """A human reading a TRAP and deciding not to reprice saved nothing.
    Counting it would recreate the found-vs-saved confusion exactly."""
    await service.record(LINE, "3001020", old_total=3804.66, new_total=3258.66,
                         net_saving=-714.0, outcome=REVIEWED)

    assert await service.total_realised() == 0.0
    assert len(await service.list_verified()) == 1


@pytest.mark.asyncio
async def test_realised_savings_add_up(service):
    await service.record(LINE, "A", old_total=1500.0, new_total=1400.0,
                         net_saving=100.0)
    await service.record(LINE, "B", old_total=2000.0, new_total=1950.0,
                         net_saving=50.0)

    assert await service.total_realised() == 150.0


@pytest.mark.asyncio
async def test_a_cleared_verification_leaves_the_ledger(service):
    await service.record(LINE, "A", old_total=1500.0, new_total=1400.0,
                         net_saving=100.0)
    await service.clear(LINE, "A")

    assert await service.total_realised() == 0.0


# ── amount matching ──────────────────────────────────────────────────────


def test_float_noise_is_the_same_amount():
    assert same_amount(1400.0, 1400.004)
    assert same_amount(2389.78, 2389.78)


def test_a_real_difference_is_not_the_same_amount():
    assert not same_amount(1400.0, 1401.0)
    assert not same_amount(2389.78, 2389.80)


def test_unknown_never_matches_unknown():
    """Two missing totals must not retire a row nobody has looked at."""
    assert not same_amount(None, None)
    assert not same_amount(None, 1400.0)
    assert not same_amount(1400.0, None)
