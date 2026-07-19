"""Cruise Intelligence System — CLI entry point.

Usage:
    python main.py api         Start the FastAPI server
    python main.py scan        Run a one-shot scan from command line
    python main.py --help      Show help
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn


def cmd_api(args):
    """Start the FastAPI server."""
    from config.settings import settings

    print(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    print(f"   API: http://{settings.api_host}:{settings.api_port}")
    print(f"   Docs: http://{settings.api_host}:{settings.api_port}/docs")
    print()

    uvicorn.run(
        "api.main:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
    )


def cmd_login(args):
    """Open a browser and wait for the user to log in manually."""
    asyncio.run(_run_login_check(args))


async def _run_login_check(args):
    """
    Open a real, visible browser window against the portal's persistent
    profile and wait for a human to log in. Never touches credentials —
    it only opens the page and polls for signs that login succeeded, so
    the saved session is fresh for unattended scan/watch runs afterward.
    """
    from config.settings import settings
    from core.models import CruiseLine
    from scraper.espresso import EspressoScraper
    from scraper.ncl import NclScraper
    from utils.logging import setup_logging

    setup_logging(settings.log_level)
    settings.browser_headless = getattr(args, "headless", True)
    print(f"LOGIN CHECK: browser_headless={settings.browser_headless}")

    cruise_line = CruiseLine(args.cruise_line.upper())
    scraper = NclScraper() if cruise_line == CruiseLine.NCL else EspressoScraper()

    await scraper.start()
    try:
        base_url = settings.ncl_search_url if cruise_line == CruiseLine.NCL else settings.espresso_home_url
        await scraper.navigate(base_url)

        print(f"⚓ A browser session started for {cruise_line.value}.")
        print("   Please log in there now (username/password/MFA as usual).")
        print(f"   Waiting up to {args.timeout_minutes} minute(s) for login to complete...\n")

        deadline = time.monotonic() + args.timeout_minutes * 60
        poll_s = 5
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            if cruise_line == CruiseLine.NCL:
                logged_in = "login" not in scraper.page.url.lower() and "signin" not in scraper.page.url.lower()
            else:
                logged_in = await scraper._check_login()
            if logged_in:
                print("✅ Logged in — session saved. You can run scan/watch normally now.")
                return
            print("   ...still waiting for login")

        print("⏰ Timed out waiting for login. Run 'python main.py login' again when ready.")
        raise RuntimeError("Login check timed out")
    finally:
        await scraper.stop()


def cmd_scan(args):
    """Run a one-shot scan from command line."""
    asyncio.run(_run_scan(args))


def _load_watchlist(path: str) -> list[str]:
    """Read booking IDs from a watchlist file, one per line.

    Blank lines and lines starting with '#' are ignored. Order is
    preserved and duplicates are dropped.
    """
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


async def _run_scan(args):
    from config.settings import settings
    from core.models import CruiseLine
    from models.database import init_db
    from services.booking_service import BookingService
    from services.csv_export import export_results_csv
    from services.excel_export import export_results_excel
    from utils.logging import setup_logging

    setup_logging(settings.log_level)
    await init_db()

    booking_ids: list[str] = []
    if args.bookings_file:
        booking_ids = _load_watchlist(args.bookings_file)
    elif args.bookings:
        booking_ids = [b.strip() for b in args.bookings.split(",") if b.strip()]

    if not booking_ids:
        print("❌ No booking IDs provided. Use --bookings '123456,789012' or --bookings-file watchlist.txt")
        sys.exit(1)

    cruise_line = CruiseLine(args.cruise_line.upper())
    print(f"⚓ Scanning {len(booking_ids)} booking(s) on {cruise_line.value}...")
    if args.capture_raw:
        print(f"   Capturing raw API responses to {args.capture_raw}/raw_responses.jsonl")

    service = BookingService()

    def on_progress(job):
        print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

    job = await service.start_scan(
        booking_ids,
        cruise_line,
        on_progress=on_progress,
        raw_dump_dir=args.capture_raw,
        capture_market_data=args.capture_market_data,
    )

    # Wait for completion
    while job.status.value in ("PENDING", "RUNNING"):
        await asyncio.sleep(1)
        job = service.get_job(job.job_id) or job

    # Print results grouped by status — a clean report for watchlist runs
    print(f"\n{'='*50}")
    print(f"📊 Results: {len(job.results)} bookings checked\n")

    icons = {"OPTIMIZATION": "✅", "TRAP": "⚠️", "NO_SAVING": "⏭", "ERROR": "❌",
             "PAID_IN_FULL": "💳", "WLT": "⏭", "SKIPPED_TODAY": "⏩"}
    status_order = ["OPTIMIZATION", "TRAP", "WLT", "PAID_IN_FULL", "NO_SAVING", "SKIPPED_TODAY", "ERROR"]
    by_status: dict[str, list] = {}
    for r in job.results:
        by_status.setdefault(r.status.value, []).append(r)

    for status in status_order:
        rows = by_status.pop(status, [])
        if not rows:
            continue
        icon = icons.get(status, "❓")
        print(f"{icon} {status} ({len(rows)})")
        for r in rows:
            saving = f" — ${r.net_saving:.2f}" if r.net_saving > 0 else ""
            print(f"   {r.booking_id}{saving}")
            if r.note:
                print(f"     {r.note}")
        print()

    # CSV export
    if args.output:
        csv_content = export_results_csv(job.results)
        with open(args.output, "w") as f:
            f.write(csv_content)
        print(f"\n📁 CSV saved to: {args.output}")

    # Excel export
    if args.excel:
        export_results_excel(job.results, args.excel)
        print(f"📊 Excel report saved to: {args.excel}")

    # Summary
    opts = [r for r in job.results if r.status.value == "OPTIMIZATION"]
    total_saving = sum(r.net_saving for r in opts)
    print(f"\n💰 Total savings found: ${total_saving:.2f} across {len(opts)} booking(s)")


def cmd_watch(args):
    """Run a recurring watch loop, re-checking booking IDs at an interval."""
    asyncio.run(_run_watch(args))


async def _run_watch(args):
    from config.settings import settings
    from core.models import CruiseLine
    from models.database import init_db
    from services.booking_service import BookingService
    from services.csv_export import export_results_csv
    from services.excel_export import export_results_excel
    from utils.logging import setup_logging

    setup_logging(settings.log_level)
    await init_db()

    booking_ids: list[str] = []
    if args.bookings_file:
        booking_ids = _load_watchlist(args.bookings_file)
    elif args.bookings:
        booking_ids = [b.strip() for b in args.bookings.split(",") if b.strip()]

    if not booking_ids:
        print("❌ No booking IDs provided. Use --bookings '123456,789012' or --bookings-file watchlist.txt")
        sys.exit(1)

    cruise_line = CruiseLine(args.cruise_line.upper())
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    alerts_path = output_dir / "alerts.log"

    interval_s = args.interval_minutes * 60
    deadline = time.monotonic() + args.duration_hours * 3600 if args.duration_hours else None

    print(f"⚓ Watching {len(booking_ids)} booking(s) on {cruise_line.value}")
    cadence = f"every {args.interval_minutes} min"
    cadence += f" for {args.duration_hours}h" if args.duration_hours else " until stopped (Ctrl+C)"
    if args.max_passes:
        cadence += f", max {args.max_passes} pass(es)"
    print(f"   {cadence}")
    print(f"   Output: {output_dir}/  (read-only checks — no rate is ever confirmed automatically)\n")

    service = BookingService()
    pass_num = 0

    try:
        while True:
            pass_num += 1
            started = datetime.utcnow()
            print(f"{'='*50}\n🔄 Pass {pass_num} — {started.isoformat()}Z")

            def on_progress(job):
                print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

            job = await service.start_scan(
                booking_ids,
                cruise_line,
                on_progress=on_progress,
                bypass_cache=True,
                raw_dump_dir=args.capture_raw,
                capture_market_data=args.capture_market_data,
            )

            while job.status.value in ("PENDING", "RUNNING"):
                await asyncio.sleep(1)
                job = service.get_job(job.job_id) or job

            csv_content = export_results_csv(job.results)
            csv_path = output_dir / f"pass{pass_num:03d}_{started.strftime('%Y-%m-%d_%H%M')}.csv"
            with open(csv_path, "w") as f:
                f.write(csv_content)

            excel_path = output_dir / f"pass{pass_num:03d}_{started.strftime('%Y-%m-%d_%H%M')}.xlsx"
            export_results_excel(job.results, excel_path)

            hits = [r for r in job.results if r.status.value in ("OPTIMIZATION", "TRAP")]
            errors = [r for r in job.results if r.status.value == "ERROR"]
            print(
                f"   → {len(job.results)} checked, {len(hits)} hit(s), {len(errors)} error(s). "
                f"Saved {csv_path.name} + {excel_path.name}"
            )

            if hits:
                with open(alerts_path, "a", encoding="utf-8") as f:
                    for r in hits:
                        icon = "✅" if r.status.value == "OPTIMIZATION" else "⚠️"
                        line = f"{started.isoformat()}Z  {r.status.value:12s}  {r.booking_id:12s}  ${r.net_saving:>10.2f}  {r.note}"
                        f.write(line + "\n")
                        print(f"   {icon} {r.booking_id}: {r.status.value} — ${r.net_saving:.2f}")

            if deadline and time.monotonic() >= deadline:
                print("\n⏰ Duration reached — stopping watch.")
                break
            if args.max_passes and pass_num >= args.max_passes:
                print("\n⏰ Max passes reached — stopping watch.")
                break

            print(f"   Sleeping {args.interval_minutes} min until next pass...")
            await asyncio.sleep(interval_s)

    except KeyboardInterrupt:
        print("\n\n🛑 Watch stopped by user.")

    print(f"\n📁 Per-pass CSVs and alerts.log saved in {output_dir}/")


def main():
    parser = argparse.ArgumentParser(
        prog="cruise-intel",
        description="Cruise Intelligence System — Repricing optimization tool",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # API command
    api_parser = subparsers.add_parser("api", help="Start the FastAPI server")
    api_parser.add_argument("--host", default=None, help="Host to bind to")
    api_parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    api_parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    # Login command — open a browser and wait for the user to log in manually
    login_parser = subparsers.add_parser(
        "login", help="Open a browser and wait for you to log in manually (no credentials handled)",
    )
    login_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO or NCL")
    login_parser.add_argument(
        "--timeout-minutes", type=float, default=15.0,
        help="How long to wait for login before giving up (default: 15)",
    )

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Run a one-shot scan")
    scan_parser.add_argument("--bookings", help="Comma-separated booking IDs")
    scan_parser.add_argument(
        "--bookings-file",
        help="Path to a watchlist text file, one booking ID per line "
             "(blank lines and '#' comments ignored). Overrides --bookings.",
    )
    scan_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO or NCL")
    scan_parser.add_argument("--output", "-o", help="Output CSV file path")
    scan_parser.add_argument("--excel", "-x", help="Output color-coded .xlsx report file path")
    scan_parser.add_argument(
        "--capture-raw", metavar="DIR",
        help="Append each booking's raw API response to DIR/raw_responses.jsonl "
             "(for later offline analysis / calculator development)",
    )
    scan_parser.add_argument(
        "--capture-market-data",
        action="store_true",
        help="Store ESPRESSO category table snapshots in the database for later analysis.",
    )

    # Watch command — recurring overnight scans
    watch_parser = subparsers.add_parser(
        "watch", help="Repeatedly re-check booking IDs at an interval (e.g. overnight)",
    )
    watch_parser.add_argument("--bookings", help="Comma-separated booking IDs")
    watch_parser.add_argument(
        "--bookings-file",
        help="Path to a watchlist text file, one booking ID per line "
             "(blank lines and '#' comments ignored). Overrides --bookings.",
    )
    watch_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO or NCL")
    watch_parser.add_argument(
        "--interval-minutes", type=int, default=60, help="Minutes between passes (default: 60)",
    )
    watch_parser.add_argument(
        "--duration-hours", type=float, default=8.0,
        help="Stop after this many hours (default: 8; pass 0 to run until Ctrl+C)",
    )
    watch_parser.add_argument(
        "--max-passes", type=int, default=None, help="Optional cap on number of passes",
    )
    watch_parser.add_argument(
        "--output-dir", default="watch_runs",
        help="Directory for per-pass CSVs and alerts.log (default: watch_runs)",
    )
    watch_parser.add_argument(
        "--capture-raw", metavar="DIR",
        help="Append each booking's raw API response to DIR/raw_responses.jsonl "
             "(for later offline analysis / calculator development)",
    )
    watch_parser.add_argument(
        "--capture-market-data",
        action="store_true",
        help="Store ESPRESSO category table snapshots in the database for later analysis.",
    )

    args = parser.parse_args()

    if args.command == "api":
        cmd_api(args)
    elif args.command == "login":
        cmd_login(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "watch":
        if args.duration_hours == 0:
            args.duration_hours = None
        cmd_watch(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
