"""Remove junk rows that a test suite wrote into the production database.

WHY THIS EXISTS
---------------
CONFIRMED 2026-08-27. `tests/test_preflight_and_file_load_2026_08_27.py`
drove the real `BookingService._run_batch` and tried to neutralise
persistence with:

    monkeypatch.setattr(service, "_persist_result", noop, raising=False)

There is no `_persist_result` method — the real one is
`_save_result_to_db` — and `raising=False` turned the typo into a silent
no-op. The tests therefore wrote **78 junk ERROR rows** into
`cruise_intel.db` for booking IDs "A", "B" and "C" (26 each), in the same
table the savings reports, `analyze_history.py` and every forensic query
read from.

The leak itself is fixed twice over (`tests/conftest.py` now forces a
throwaway DB and asserts it, and the monkeypatch names the real methods
with `raising=True`). This script cleans up what already landed.

SAFETY
------
* Makes a timestamped backup of the DB (plus -wal/-shm) before touching it.
* DRY RUN by default — prints what it would delete and exits.
* Deletes ONLY rows whose booking_id is exactly "A", "B" or "C". Every real
  booking ID in this dataset is either numeric (ESPRESSO/NCL, e.g.
  3000055) or a 6-character alphanumeric GoCCL code (e.g. DEMO02, DEMO06)
  — never a single letter. The filter is exact-match, not a pattern.
* Refuses to run while a scan is in flight.

USAGE
    python cleanup_test_pollution.py            # dry run, shows what it would do
    python cleanup_test_pollution.py --apply    # actually delete
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sqlite3
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

DB = "cruise_intel.db"
JUNK_IDS = ("A", "B", "C")


def _recent_activity(con) -> str | None:
    """A scan writing right now must not be interrupted."""
    newest = con.execute("SELECT MAX(created_at) FROM bookings").fetchone()[0]
    if not newest:
        return None
    try:
        stamp = datetime.datetime.fromisoformat(newest)
    except ValueError:
        return None
    age_min = (datetime.datetime.utcnow() - stamp).total_seconds() / 60.0
    return newest if age_min < 10 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--force", action="store_true",
                    help="proceed even if a scan looks active (not recommended)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        return 1

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    placeholders = ",".join("?" * len(JUNK_IDS))
    rows = con.execute(
        f"SELECT booking_id,cruise_line,status,net_saving,created_at FROM bookings "
        f"WHERE booking_id IN ({placeholders}) ORDER BY created_at",
        JUNK_IDS,
    ).fetchall()

    total = con.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    print(f"{args.db}: {total} booking rows total")
    print(f"junk rows matching {JUNK_IDS}: {len(rows)}")
    if not rows:
        print("Nothing to clean.")
        return 0

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"  statuses: {by_status}")
    print(f"  window  : {rows[0]['created_at']} .. {rows[-1]['created_at']}")
    nonzero = [r for r in rows if (r["net_saving"] or 0) != 0]
    print(f"  rows with a non-zero net_saving: {len(nonzero)} (expected 0 — these are junk)")

    # A real booking ID is never a single letter; prove it before deleting.
    real_short = con.execute(
        "SELECT DISTINCT booking_id FROM bookings "
        "WHERE length(booking_id)<=2 AND booking_id NOT IN (%s)" % placeholders,
        JUNK_IDS,
    ).fetchall()
    if real_short:
        print(f"  ABORT: other very short booking IDs exist {[r[0] for r in real_short]} — "
              f"review manually before deleting anything.")
        return 1

    active = None if args.force else _recent_activity(con)
    if active:
        print(f"\nREFUSING: a scan wrote to this database at {active} (under 10 min ago).")
        print("Wait for it to finish, then re-run. (--force overrides, not recommended.)")
        return 1

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete these rows.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{args.db}.backup_{stamp}"
    con.close()
    shutil.copy2(args.db, backup)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(args.db + suffix):
            shutil.copy2(args.db + suffix, backup + suffix)
    print(f"\nBackup written: {backup}")

    con = sqlite3.connect(args.db)
    cur = con.execute(
        f"DELETE FROM bookings WHERE booking_id IN ({placeholders})", JUNK_IDS
    )
    deleted = cur.rowcount
    con.commit()
    remaining = con.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()

    print(f"Deleted {deleted} row(s). Rows now: {remaining} (was {total}).")
    print(f"PRAGMA integrity_check: {integrity}")
    if remaining != total - deleted:
        print("WARNING: row count does not reconcile — restore the backup and investigate.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
