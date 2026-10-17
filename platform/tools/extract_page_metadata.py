"""Salvage the few fields that live ONLY in data/pages/, then let it go.

WHY THIS EXISTS (2026-10-17). `data/pages/` had grown to 10 GB across 46,328
page snapshots. Measured before deleting any of it:

    categories_table       99% already in market_data
    booking_summary        98%
    offer_code_comparison 100%
    after_search           87%   (the gaps are lines that never write
                                  market_data; their outcomes are in `bookings`)

So the category grids, prices and scan outcomes are already stored in
structured form and the HTML adds nothing. But the snapshots' `labelPairs`
carry a handful of fields that exist NOWHERE in the database:

    Group ID / Group Name / Group Type / Policy Type / Option Date   (27% of pages)

That matters because group bookings are a known source of
"Booking restriction - price program change not allowed", and today nothing
records which bookings are group bookings at all. This script lifts those
fields into `booking_group_info` so the 10 GB can be deleted without losing
them.

DELIBERATELY NOT EXTRACTED
--------------------------
**Agent Name** (present on 27% of pages) is a real agency employee's name, and
**Agency** is a phone number. Both are personal data with no analytical value
here, so they are skipped rather than moved into the database.

**Total Guests** (84%) is skipped too, and that is not an oversight. On
ESPRESSO it is group-level, not per-cabin: one sampled booking read 237 guests
across 100 staterooms. `nights`/`guests_count` were left NULL on purpose for
exactly this reason -- a NULL costs signal, a fabricated value teaches
something false.

Re-runnable: rows are keyed on (booking_id, captured_at) and re-inserting is a
no-op.

OUTCOME (2026-10-17)
--------------------
Run against the full 46,328-snapshot corpus: 12,809 snapshots carried group
metadata across 885 distinct bookings, of which 348 are group bookings.
`data/pages/` was deleted afterwards and the directory no longer exists, which
is why this script's default `--pages` path will not resolve on a fresh
checkout. It is kept because the extraction is worth repeating if snapshots are
ever captured again, and because the measurements above are the record of what
was and was not lost.

Usage:
    python tools/extract_page_metadata.py            # extract, report, keep files
    python tools/extract_page_metadata.py --report   # report only, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

#: Fields worth keeping. Anything not listed here is intentionally dropped --
#: see the module docstring for why Agent Name and Total Guests are excluded.
WANTED = {
    "Group ID": "group_id",
    "Group Name": "group_name",
    "Group Type:": "group_type",
    "Policy Type:": "policy_type",
    "Option Date": "option_date",
    "Rate": "rate_name",
}

_STAMP = re.compile(r"^([0-9A-Za-z]+)__[a-z_]+__(\d{8})T(\d{6})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS booking_group_info (
    booking_id   VARCHAR(20) NOT NULL,
    captured_at  DATETIME    NOT NULL,
    group_id     VARCHAR(40),
    group_name   VARCHAR(120),
    group_type   VARCHAR(60),
    policy_type  VARCHAR(60),
    option_date  VARCHAR(20),
    rate_name    VARCHAR(120),
    PRIMARY KEY (booking_id, captured_at)
)
"""


def _clean(value: str) -> str | None:
    """Portal text arrives padded with tabs and newlines; flatten it.

    Returns None for an unrendered Angular binding. ESPRESSO serves pages
    whose bindings sometimes never resolve, leaving literal template text
    like ``{{rate.name || '--'}}`` in the DOM -- the same failure that once
    made a reservation-status widget read as a real value. Storing that
    would be worse than storing nothing.
    """
    v = " ".join(str(value).split()).strip(" :,-")
    if not v or "{{" in v or "}}" in v:
        return None
    if v in {"--", "N/A", "Pending"}:
        return None
    return v


def parse(path: str) -> dict | None:
    """Return the wanted fields from one page-snapshot JSON, or None."""
    name = os.path.basename(path)
    m = _STAMP.match(name)
    if not m:
        return None
    booking_id, day, clock = m.groups()
    try:
        captured = datetime.strptime(day + clock, "%Y%m%d%H%M%S")
    except ValueError:
        return None

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None

    row: dict[str, object] = {}
    for pair in doc.get("labelPairs") or []:
        if not isinstance(pair, dict):
            continue
        label = str(pair.get("label", "")).strip()
        column = WANTED.get(label) or WANTED.get(label.rstrip(":") + ":")
        if not column or column in row:
            continue
        value = _clean(pair.get("value", ""))
        # The portal repeats a label as its own value on empty sections.
        if value and value.rstrip(":") != label.rstrip(":"):
            row[column] = value

    if not row:
        return None
    row["booking_id"] = booking_id
    row["captured_at"] = captured.isoformat(sep=" ")
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pages", default="data/pages", help="snapshot directory")
    ap.add_argument("--db", default="cruise_intel.db")
    ap.add_argument("--report", action="store_true",
                    help="show what would be extracted; write nothing")
    args = ap.parse_args()

    if not os.path.isdir(args.pages):
        print(f"no such directory: {args.pages}", file=sys.stderr)
        return 1

    files = [f for f in os.listdir(args.pages) if f.endswith(".json")]
    print(f"scanning {len(files):,} page snapshots in {args.pages}")

    rows, scanned = [], 0
    for name in files:
        scanned += 1
        row = parse(os.path.join(args.pages, name))
        if row:
            rows.append(row)
        if scanned % 5000 == 0:
            print(f"  {scanned:,}/{len(files):,} ... {len(rows):,} carry group metadata")

    bookings = {r["booking_id"] for r in rows}
    print(f"\n  {len(rows):,} snapshots carry group metadata, "
          f"across {len(bookings):,} distinct bookings")

    if args.report:
        for r in rows[:5]:
            print("   ", {k: v for k, v in r.items() if k != "captured_at"})
        print("\n--report given: nothing written")
        return 0

    db = sqlite3.connect(args.db)
    db.execute(SCHEMA)
    cols = ["booking_id", "captured_at", "group_id", "group_name",
            "group_type", "policy_type", "option_date", "rate_name"]
    db.executemany(
        f"INSERT OR IGNORE INTO booking_group_info ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM booking_group_info").fetchone()[0]
    groups = db.execute(
        "SELECT COUNT(DISTINCT booking_id) FROM booking_group_info "
        "WHERE group_id IS NOT NULL").fetchone()[0]
    db.close()

    print(f"  booking_group_info now holds {total:,} rows")
    print(f"  bookings identified as GROUP bookings: {groups:,}")
    print("\nThe page snapshots can now be deleted; nothing they held is lost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
