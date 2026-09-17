"""Rebuild a scan export from the database, with a collision-proof name.

Neon 2026-09-16: "the export rersults extracts file name one only
scan_results so i exported for esspresso and ncl and it got replaced".

Both exports wrote to reports/scan_results.csv|.xlsx with no cruise line
and no date in the name, so the NCL export silently replaced the ESPRESSO
one. The GUI is fixed; this recovers what was overwritten.

Nothing was actually lost: every result is persisted in the bookings table
as it is produced, and the export is only a view of it. This regenerates
that view for any line and day.

    python rebuild_export.py                     # today, every line
    python rebuild_export.py ESPRESSO            # today, one line
    python rebuild_export.py ESPRESSO 2026-09-15 # a specific day

READ-ONLY against the database. Writes new, timestamped files; never
overwrites an existing one.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sqlite3  # noqa: E402

from core.models import BookingResult, BookingStatus, CruiseLine  # noqa: E402
from gui.scan_adapter import GuiScanAdapter  # noqa: E402


def _rows(cruise_line: str, day: str):
    con = sqlite3.connect(HERE / "cruise_intel.db")
    con.row_factory = sqlite3.Row
    try:
        # Latest row per booking for that line and day - a re-scan
        # supersedes an earlier result, and showing both would double
        # count the money exactly as the GUI reload once did.
        return list(con.execute(
            """SELECT * FROM bookings b
               WHERE cruise_line = ? AND date(created_at) = ?
                 AND id = (SELECT MAX(id) FROM bookings
                           WHERE booking_id = b.booking_id
                             AND cruise_line = b.cruise_line
                             AND date(created_at) = ?)
               ORDER BY id""", (cruise_line, day, day)))
    finally:
        con.close()


def _to_result(rec) -> BookingResult:
    keys = rec.keys()

    def get(name, default=None):
        return rec[name] if name in keys else default

    return BookingResult(
        booking_id=get("booking_id") or "",
        cruise_line=CruiseLine(get("cruise_line")),
        status=BookingStatus(get("status")),
        old_total=get("old_total") or 0.0,
        new_total=get("new_total") or 0.0,
        net_saving=get("net_saving") or 0.0,
        price_drop=get("price_drop") or 0.0,
        obc_change=get("obc_change") or 0.0,
        lost_pkg_value=get("lost_pkg_value") or 0.0,
        confidence=int(get("confidence") or 0),
        price_category=get("price_category"),
        new_price_category=get("new_price_category"),
        note=get("note") or "",
    )


def rebuild(cruise_line: str, day: str, out_dir: Path) -> list[str]:
    records = _rows(cruise_line, day)
    if not records:
        return []
    results = [_to_result(r) for r in records]
    out_dir.mkdir(exist_ok=True)
    # Same naming the GUI now uses, with the ORIGINAL day rather than today
    # so a recovered export is not mistaken for a fresh one.
    stamp = f"{day.replace('-', '')}_{datetime.now().strftime('%H%M%S')}"
    base = out_dir / f"{cruise_line}_scan_results_{stamp}"
    csv_path, xlsx_path = f"{base}.csv", f"{base}.xlsx"
    for path in (csv_path, xlsx_path):
        if Path(path).exists():          # never clobber - the whole point
            print(f"  refusing to overwrite existing {path}")
            return []
    # Reuse the GUI's own adapter so a rebuilt export is byte-identical in
    # shape to one produced by the Export button - same columns, same
    # formatting, same Excel fills.
    adapter = GuiScanAdapter()
    adapter.export_csv(results, csv_path)
    adapter.export_excel(results, xlsx_path)
    return [csv_path, xlsx_path]


def main() -> None:
    lines = [sys.argv[1].upper()] if len(sys.argv) > 1 else [c.value for c in CruiseLine]
    day = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")
    out = HERE / "reports"
    print(f"Rebuilding exports for {day}\n")
    total = 0
    for line in lines:
        made = rebuild(line, day, out)
        if made:
            n = len(_rows(line, day))
            print(f"  {line:<9} {n:>4} bookings")
            for f in made:
                print(f"             {Path(f).name}")
            total += 1
        else:
            print(f"  {line:<9}    - nothing stored for that day")
    print(f"\n{total} export(s) rebuilt into {out}")


if __name__ == "__main__":
    main()
