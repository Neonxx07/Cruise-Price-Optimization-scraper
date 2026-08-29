"""Split an NCL watchlist into per-account files, from real scan results.

WHY THIS EXISTS
---------------
CONFIRMED BY NEON 2026-08-27: NCL runs a SEPARATE SeaWeb agent account per
market. Canadian (CAD) bookings return "Reservation is not found" when
checked against the US login — they are not missing, they are on the other
account. In the 2026-08-27 run, 25 of 101 errors were exactly this.

Rather than ask anyone to hand-classify 167 booking IDs, this reads what the
scans ALREADY recorded in cruise_intel.db and writes:

    Watchlistncl_us.txt   bookings that were readable on the US account
    Watchlistncl_ca.txt   bookings to retry on the Canada (CAD) account
    Watchlistncl_recheck.txt   bookings that errored for an unrelated reason

Read-only against the database (opened with mode=ro) — same convention as
analyze_history.py and msc_run_calculator.py. Opens no browser and touches
no portal.

IMPORTANT CAVEAT, do not skip
-----------------------------
A booking held under ANOTHER session's 30-minute edit lock can ALSO report
"not found". That really happened on 2026-08-27: bookings 3000059 and
3000060 read as not-found while a concurrent session held them, yet both
were perfectly readable on their own. So the CA list is a list of
CANDIDATES to try on the CAD login — not proof of nationality. Any booking
that then succeeds on the CAD account is confirmed Canadian; any that fails
on BOTH accounts needs a human look.

USAGE
    python split_ncl_watchlist_by_account.py                  # uses Watchlistncl.txt
    python split_ncl_watchlist_by_account.py --watchlist path/to/list.txt
    python split_ncl_watchlist_by_account.py --since 2026-08-27
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

DB = "cruise_intel.db"

# Statuses that prove the booking WAS readable on the account that scanned
# it — a real answer was produced, whatever that answer was.
_READABLE = {
    "OPTIMIZATION", "TRAP", "NO_SAVING", "PAID_IN_FULL",
    "WLT", "UPGRADE_AVAILABLE", "SKIPPED_TODAY",
}


def _load_watchlist(path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in line.replace(",", " ").split():
            if part and part not in seen:
                seen.add(part)
                ids.append(part)
    return ids


def _classify(conn, booking_ids: list[str], since: str | None):
    """Latest-status-wins per booking, from real recorded results."""
    where = "cruise_line = 'NCL'"
    params: list = []
    if since:
        where += " AND created_at >= ?"
        params.append(since)

    latest: dict[str, tuple[str, str]] = {}
    for bid, status, err, note in conn.execute(
        f"""SELECT booking_id, status, COALESCE(error,''), COALESCE(note,'')
            FROM bookings WHERE {where} ORDER BY created_at""",
        params,
    ):
        latest[bid] = (status, f"{err} {note}")

    us, ca, recheck, unknown = [], [], [], []
    for bid in booking_ids:
        entry = latest.get(bid)
        if entry is None:
            unknown.append(bid)
            continue
        status, text = entry
        low = text.lower()
        if status in _READABLE:
            us.append(bid)
        elif status == "NOT_ON_THIS_ACCOUNT" or "reservation is not found" in low \
                or "reservation not found" in low:
            ca.append(bid)
        else:
            recheck.append(bid)
    return us, ca, recheck, unknown


def _write(path: Path, ids: list[str], header: str) -> None:
    path.write_text(
        "# " + header + "\n" + "\n".join(ids) + ("\n" if ids else ""),
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watchlist", default="Watchlistncl.txt")
    ap.add_argument("--db", default=DB)
    ap.add_argument(
        "--since", default=None,
        help="Only consider results recorded on/after this UTC date "
             "(e.g. 2026-08-27). Older results may predate today's fixes.",
    )
    args = ap.parse_args()

    wl = Path(args.watchlist)
    if not wl.exists():
        print(f"Watchlist not found: {wl}")
        return 1
    booking_ids = _load_watchlist(wl)
    if not booking_ids:
        print(f"{wl.name} is empty — no booking IDs to split.")
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    us, ca, recheck, unknown = _classify(conn, booking_ids, args.since)

    stem = wl.stem
    _write(Path(f"{stem}_us.txt"), us,
           "READABLE on the US account (a real result was produced).")
    _write(Path(f"{stem}_ca.txt"), ca,
           "CANDIDATES for the Canada (CAD) account — reported not-found on US. "
           "NOTE: another session's 30-min edit lock can also cause not-found, "
           "so a failure here is not proof of nationality.")
    _write(Path(f"{stem}_recheck.txt"), recheck,
           "Errored for an UNRELATED reason (timeout, grid, session) — retry "
           "on the account that already sees them.")

    total = len(booking_ids)
    print(f"\n{wl.name}: {total} booking ID(s)\n")
    print(f"  {len(us):>4}  readable on US        -> {stem}_us.txt")
    print(f"  {len(ca):>4}  try on CANADA (CAD)   -> {stem}_ca.txt")
    print(f"  {len(recheck):>4}  unrelated errors      -> {stem}_recheck.txt")
    print(f"  {len(unknown):>4}  never scanned yet     (not written to any file)")
    if unknown:
        print(f"        e.g. {', '.join(unknown[:8])}"
              f"{' ...' if len(unknown) > 8 else ''}")

    if ca:
        print("\nNext step — save the CAD credentials once:")
        print("    python save_login.py        # choose 'NCL — Canada account (CAD)'")
        print("then scan that list on the CAD account:")
        print(f"    python main.py scan --bookings-file {stem}_ca.txt "
              f"--cruise-line NCL --market CA --visible")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
