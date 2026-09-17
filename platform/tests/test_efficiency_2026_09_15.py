"""Lighter and faster - all three changes measured, not assumed.

Neon 2026-09-15: "make sure to make the projct more efficent and faster as
well as lighter".

Measured first. The project's on-disk footprint was 856 MB, and almost none
of it was anything the scanner needs:

    data/pages/          452.8 MB   1,888 HTML files, ALL from ONE day
    data/ncl_live_test/  194.4 MB   34 one-off debugging trace zips
    data/failures/        82.8 MB   662 files, no retention of any kind
    data/raw_responses     73.1 MB
    cruise_intel.db        39.8 MB

The database was NOT slow in the way it looked - 6,236 rows, worst query
48 ms. But that query is the GUI's hottest, run once per panel on every
startup, so four times before a single booking is scanned.

RETENTION IS OFF. Neon, asked about clearing the 750 MB: "do not delete
the data because we use it to use it make the project better and the
resu,lts better". Captured failures are the corpus this project mines, not
clutter - so the pruner exists but nothing calls it, full-page screenshots
were restored (a viewport shot loses everything below the fold, which on
these portals is often the price breakdown), and the only efficiency change
that survives is the database index, which costs no data at all.
"""
import os
import sqlite3
import tempfile

import pytest

from scraper.base import (
    MAX_FAILURE_SNAPSHOTS_PER_BOOKING,
    _prune_failure_snapshots,
)


def _snapshot(dirpath, booking, stamp):
    for ext in (".png", ".html", ".json"):
        open(os.path.join(dirpath, f"{booking}__search_failed__{stamp}{ext}"),
             "w").close()


# -- failure snapshots: bounded, and grouped ------------------------


def test_only_the_newest_snapshots_per_booking_are_kept():
    """Nothing ever deleted these, so every failed booking added ~1 MB
    forever - and a booking that fails tends to fail on every scan. Real
    examples in the directory: booking 3000041 with three snapshots from
    one afternoon, 3000066 with three from a single run."""
    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002", "20260103T000003",
                      "20260104T000004", "20260105T000005"):
            _snapshot(d, "3000066", stamp)
        removed = _prune_failure_snapshots(d, "3000066", keep=3)
        kept = sorted({f.rsplit("__", 1)[1][:15] for f in os.listdir(d)})
        assert removed == 6                      # 2 snapshots x 3 files
        assert kept == ["20260103T000003", "20260104T000004",
                        "20260105T000005"]


def test_another_bookings_snapshots_are_untouched():
    """Pruning is per booking - one noisy booking must not evict the
    evidence for a different one."""
    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002",
                      "20260103T000003", "20260104T000004"):
            _snapshot(d, "3000066", stamp)
        _snapshot(d, "9999999", "20260101T000009")
        _prune_failure_snapshots(d, "3000066", keep=3)
        assert any(f.startswith("9999999") for f in os.listdir(d))


def test_a_snapshot_is_removed_as_a_SET_not_file_by_file():
    """The .png, .html and .json share a stem and only mean anything
    together - a screenshot with no URL or error beside it is not
    diagnostic."""
    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002",
                      "20260103T000003", "20260104T000004"):
            _snapshot(d, "B1", stamp)
        _prune_failure_snapshots(d, "B1", keep=3)
        stems = {}
        for f in os.listdir(d):
            stems.setdefault(os.path.splitext(f)[0], set()).add(
                os.path.splitext(f)[1])
        for stem, exts in stems.items():
            assert exts == {".png", ".html", ".json"}, f"{stem} orphaned: {exts}"


def test_nothing_is_removed_below_the_limit():
    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002"):
            _snapshot(d, "B1", stamp)
        assert _prune_failure_snapshots(d, "B1", keep=3) == 0
        assert len(os.listdir(d)) == 6


def test_a_missing_directory_is_not_an_error():
    """This runs inside an exception handler after a scrape already
    failed. It must never raise and mask the original error."""
    assert _prune_failure_snapshots("/no/such/dir", "B1", keep=3) == 0


def test_unrelated_files_are_left_alone():
    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002",
                      "20260103T000003", "20260104T000004"):
            _snapshot(d, "B1", stamp)
        open(os.path.join(d, "README.txt"), "w").close()
        _prune_failure_snapshots(d, "B1", keep=3)
        assert "README.txt" in os.listdir(d)


def test_nothing_is_deleted_by_default():
    """Neon 2026-09-15: "do not delete the data because we use it to use
    it make the project better and the resu,lts better". I had shipped a
    pruner that removed all but the newest 3 snapshots per booking; that
    is exactly the deletion he ruled out. The function stays available for
    a deliberate cleanup, but its default keeps everything."""
    assert MAX_FAILURE_SNAPSHOTS_PER_BOOKING is None

    with tempfile.TemporaryDirectory() as d:
        for stamp in ("20260101T000001", "20260102T000002", "20260103T000003",
                      "20260104T000004", "20260105T000005"):
            _snapshot(d, "B1", stamp)
        assert _prune_failure_snapshots(d, "B1") == 0
        assert len(os.listdir(d)) == 15, "snapshots were deleted by default"


# -- screenshots: viewport, not full page ---------------------------


def test_the_pruner_is_NOT_called_automatically():
    """The inverse of what this test originally asserted. Deleting a
    booking's older failure snapshots destroys the evidence that makes the
    next fix possible - and this project has repeatedly needed exactly
    that history (booking 3000081 was diagnosed by comparing six captures
    taken on different days)."""
    import inspect

    import scraper.base

    src = inspect.getsource(scraper.base.BaseScraper.dump_failure_snapshot)
    assert "_prune_failure_snapshots(" not in src, (
        "failure snapshots are being pruned automatically again"
    )


def test_failure_screenshots_capture_the_whole_page():
    """Restored after the same instruction. A viewport shot is 8x smaller
    but loses everything below the fold, and on these portals the price
    breakdown is often exactly what is below the fold."""
    import inspect

    import scraper.base

    src = inspect.getsource(scraper.base)
    seg = src[src.index("failures_dir = os.path.join"):][:1500]
    assert "full_page=True" in seg


# -- the database index ---------------------------------------------


@pytest.mark.asyncio
async def test_the_composite_index_is_created_on_an_existing_database(tmp_path):
    """`Base.metadata.create_all` only ever CREATES tables - it never
    alters one that already exists, which is why an index declared in
    __table_args__ never appears on a live database. Same reason
    _migrate_sqlite_add_columns exists for columns."""
    from models.database import _ensure_sqlite_indexes

    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY, "
                "cruise_line TEXT, created_at TEXT)")
    con.commit()
    con.close()

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        created = _ensure_sqlite_indexes(conn)
        assert "ix_bookings_line_created_at" in created
        # idempotent - startup runs it every time
        assert _ensure_sqlite_indexes(conn) == []
    engine.dispose()

    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_bookings_line_created_at" in names
    con.close()


def test_the_guis_hottest_query_uses_the_index(tmp_path):
    """The query is "today's results for this cruise line", run once per
    panel on every startup - four times before anything is scanned. It was
    a full table SCAN at 48 ms; indexed it is about 4 ms."""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bookings (id INTEGER PRIMARY KEY, "
                "cruise_line TEXT, created_at TEXT, net_saving REAL)")
    con.executemany(
        "INSERT INTO bookings (cruise_line, created_at, net_saving) "
        "VALUES (?,?,?)",
        [("ESPRESSO", f"2026-09-{(i % 28) + 1:02d}", 1.0) for i in range(4000)])
    con.execute("CREATE INDEX ix_bookings_line_created_at "
                "ON bookings (cruise_line, created_at)")
    con.commit()
    plan = con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM bookings "
        "WHERE cruise_line=? AND created_at>=? ORDER BY created_at DESC",
        ("ESPRESSO", "2026-09-01")).fetchall()
    con.close()
    assert any("ix_bookings_line_created_at" in str(row) for row in plan), plan
