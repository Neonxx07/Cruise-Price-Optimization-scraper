"""One continuous browser session that logs in once, then repeatedly
scans watchlist.txt — re-running automatically whenever the file's
content changes — and never closes the browser in between.

ESPRESSO's session/bot-detection behavior (confirmed by Neon, not a
guess) requires the SAME browser process to stay alive from login through
every scan — closing and reopening it forces a fresh login every time and
can trip Akamai's bot-detection. The CLI's separate `login` then `scan`
commands are two different OS processes and can never share a browser.

This version fixes the previous one-shot script's real limitation: once
it finished a scan it just idled forever with no way to feed it new work.
Now it watches watchlist.txt's content hash and automatically starts a
fresh scan of the current (deduped) list whenever it changes, then goes
back to watching — so "run it again" is just "edit watchlist.txt", never
a fresh login.
"""

import asyncio
import hashlib
import sys
from datetime import datetime

from config.settings import settings
from core.calculator import total_optimization_savings
from core.models import CruiseLine
from models.database import init_db
from services.booking_service import BookingService
from services.csv_export import export_results_csv
from services.excel_export import export_results_excel
from utils.logging import setup_logging

WATCHLIST_PATH = "watchlist.txt"
POLL_INTERVAL_S = 30


def _load_watchlist(path: str) -> list[str]:
    seen: set[str] = set()
    booking_ids: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                booking_ids.append(line)
    return booking_ids


def _watchlist_hash() -> str:
    with open(WATCHLIST_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def run_one_scan(service: BookingService, booking_ids: list[str]) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    print(f"\n⚓ Starting scan of {len(booking_ids)} booking(s) — {stamp}\n")

    def on_progress(job):
        print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

    job = await service.start_scan(
        booking_ids,
        CruiseLine.ESPRESSO,
        on_progress=on_progress,
        raw_dump_dir=f"data/run_{stamp}",
        capture_market_data=True,
        capture_everything=True,
        keep_browser_open=True,
    )

    while job.status.value in ("PENDING", "RUNNING"):
        await asyncio.sleep(2)
        job = service.get_job(job.job_id) or job

    print(f"\n{'='*50}")
    print(f"📊 Results: {len(job.results)} bookings checked\n")

    icons = {"OPTIMIZATION": "✅", "UPGRADE_AVAILABLE": "🆙", "TRAP": "⚠️", "NO_SAVING": "⏭", "ERROR": "❌",
             "PAID_IN_FULL": "💳", "WLT": "⏭", "SKIPPED_TODAY": "⏩"}
    status_order = ["OPTIMIZATION", "UPGRADE_AVAILABLE", "TRAP", "WLT", "PAID_IN_FULL",
                     "NO_SAVING", "SKIPPED_TODAY", "ERROR"]
    by_status: dict[str, list] = {}
    for r in job.results:
        by_status.setdefault(r.status.value, []).append(r)
    for status in status_order:
        rows = by_status.pop(status, [])
        if not rows:
            continue
        print(f"{icons.get(status, '?')} {status} ({len(rows)})")
        for r in rows:
            # CONFIRMED REAL BUG 2026-08-12: calculator.py's TRAP/NO_SAVING
            # branches (package-trap, OBC-loss-ratio) can compute a
            # positive net_saving on paper — that's the whole point of
            # those checks, catching a "win" smaller than what's being
            # given up — but this printed "$X.XX" under a TRAP header
            # regardless, reading like a real savings figure right next to
            # a warning icon. Only show the dollar figure for statuses
            # where it actually reflects a real, recommended saving.
            saving = f" — ${r.net_saving:.2f}" if r.net_saving > 0 and status not in ("TRAP", "NO_SAVING") else ""
            print(f"   {r.booking_id}{saving}")
            if r.note:
                print(f"     {r.note}")
        print()

    csv_path = f"reports/report_{stamp}_watchlist.csv"
    xlsx_path = f"reports/report_{stamp}_watchlist.xlsx"
    csv_content = export_results_csv(job.results)
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(csv_content)
    print(f"📁 CSV saved to: {csv_path}")
    export_results_excel(job.results, xlsx_path)
    print(f"📊 Excel saved to: {xlsx_path}")

    opts = [r for r in job.results if r.status.value == "OPTIMIZATION"]
    total_saving = total_optimization_savings(job.results)
    print(f"\n💰 Total savings found: ${total_saving:.2f} across {len(opts)} booking(s)")


async def main():
    setup_logging(settings.log_level)
    await init_db()

    service = BookingService()

    print("\n⚓ Opening ESPRESSO — please log in (username/password/MFA).")
    print("   This browser stays open for every future scan; no repeat logins.\n")
    logged_in = await service.check_login(CruiseLine.ESPRESSO, timeout_minutes=15.0)
    if not logged_in:
        print("⏰ Login timed out. Run this script again when ready.")
        sys.exit(1)
    print("✅ Logged in.\n")

    # KNOWN LIMITATION, investigated 2026-08-13 (not implemented, by
    # design): a hard process kill mid-scan does NOT resume from where
    # it left off on restart — `last_hash = None` here means the very
    # first loop iteration always treats the current watchlist as
    # "changed" and rescans the ENTIRE deduped list from the start,
    # regardless of how far a previous run got. `ScanJobRecord.progress_done`
    # (models/database.py) is only ever written once, in `_run_batch`'s
    # `finally` block at the very END of a batch (services/booking_service.py)
    # — a hard-killed process leaves that row forever at
    # status="RUNNING", progress_done=0, so even a resume-aware
    # implementation would have no accurate mid-scan checkpoint to read.
    #
    # Investigated whether this is a real business requirement: no
    # evidence anywhere in this project's history that "resume after
    # crash" was ever explicitly requested. The actual existing
    # mitigation against redundant work is the NO_SAVING TTL cache
    # (12h, services/cache_service.py) -- and watch-runs specifically
    # pass bypass_cache=True anyway (this whole script exists to
    # re-check the same bookings repeatedly, so "rescan everything on
    # restart" is much closer to this script's actual intended behavior
    # than a surprising gap).
    #
    # NOT implementing a checkpoint/resume mechanism now, per this
    # audit's own explicit instruction: "do NOT implement it unless you
    # can do so without risking duplicate processing or incorrect
    # state." A safe design would need a checkpoint written ATOMICALLY
    # per-booking DURING _run_batch (not just once at the end), plus a
    # resume path that can tell "this booking's result was durably
    # persisted before the crash" apart from "this booking was
    # in-flight and its outcome is unknown" -- getting that distinction
    # wrong risks either silently skipping a real booking or double-
    # processing one. That's real design work deserving its own
    # deliberate pass, not something to bolt on as a side effect of an
    # unrelated audit.
    last_hash = None
    while True:
        current_hash = _watchlist_hash()
        if current_hash != last_hash:
            booking_ids = _load_watchlist(WATCHLIST_PATH)
            await run_one_scan(service, booking_ids)
            last_hash = current_hash
            print(f"\n🔵 Idle — watching {WATCHLIST_PATH} for changes "
                  f"(checks every {POLL_INTERVAL_S}s, browser stays open).")
        await asyncio.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
