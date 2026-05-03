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


def cmd_scan(args):
    """Run a one-shot scan from command line."""
    asyncio.run(_run_scan(args))


async def _run_scan(args):
    from config.settings import settings
    from core.models import CruiseLine
    from models.database import init_db
    from services.booking_service import BookingService
    from services.csv_export import export_results_csv
    from utils.logging import setup_logging

    setup_logging(settings.log_level)
    await init_db()

    booking_ids = [b.strip() for b in args.bookings.split(",") if b.strip()]
    if not booking_ids:
        print("❌ No booking IDs provided. Use --bookings '123456,789012'")
        sys.exit(1)

    cruise_line = CruiseLine(args.cruise_line.upper())
    print(f"⚓ Scanning {len(booking_ids)} booking(s) on {cruise_line.value}...")

    service = BookingService()

    def on_progress(job):
        print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

    job = await service.start_scan(booking_ids, cruise_line, on_progress=on_progress)

    # Wait for completion
    while job.status.value in ("PENDING", "RUNNING"):
        await asyncio.sleep(1)
        job = service.get_job(job.job_id) or job

    # Print results
    print(f"\n{'='*50}")
    print(f"📊 Results: {len(job.results)} bookings checked\n")

    for r in job.results:
        icon = {"OPTIMIZATION": "✅", "TRAP": "⚠️", "NO_SAVING": "⏭", "ERROR": "❌",
                "PAID_IN_FULL": "💳", "WLT": "⏭", "SKIPPED_TODAY": "⏩"}.get(r.status.value, "❓")
        saving = f" — ${r.net_saving:.2f}" if r.net_saving > 0 else ""
        print(f"  {icon} {r.booking_id}: {r.status.value}{saving}")
        if r.note:
            print(f"     {r.note}")

    # CSV export
    if args.output:
        csv_content = export_results_csv(job.results)
        with open(args.output, "w") as f:
            f.write(csv_content)
        print(f"\n📁 CSV saved to: {args.output}")

    # Summary
    opts = [r for r in job.results if r.status.value == "OPTIMIZATION"]
    total_saving = sum(r.net_saving for r in opts)
    print(f"\n💰 Total savings found: ${total_saving:.2f} across {len(opts)} booking(s)")


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

    # Scan command
    scan_parser = subparsers.add_parser("scan", help="Run a one-shot scan")
    scan_parser.add_argument("--bookings", required=True, help="Comma-separated booking IDs")
    scan_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO or NCL")
    scan_parser.add_argument("--output", "-o", help="Output CSV file path")

    args = parser.parse_args()

    if args.command == "api":
        cmd_api(args)
    elif args.command == "scan":
        cmd_scan(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
