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
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

# CONFIRMED REAL CRASH, fixed 2026-08-27. Running
# `python main.py scan --cruise-line NCL --bookings-file ...` died with
# `UnicodeEncodeError: 'charmap' codec can't encode character '⚓'`
# at main.py's own "⚓ Scanning N booking(s)" line — BEFORE a single
# booking was checked. Windows gives a non-interactive child process a
# cp1252 stdout, and this file's status output is full of emoji
# (⚓ 🚀 💳 ✅ ⚠), so the whole CLI was unusable for any piped/redirected
# run on this machine.
#
# This is the SAME defect class as the 2026-07-31 incident where a 💳 in
# a paid-in-full note raised UnicodeEncodeError during CSV export and
# destroyed both the CSV and the Excel report for a 72-booking run (that
# one was fixed by passing encoding="utf-8" to open(); the print path was
# missed). Fixing it at the stream instead of hunting every emoji.
#
# errors="replace" not "strict": a console that genuinely cannot render a
# glyph should print a placeholder, never abort a scan.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        # Not a real reconfigurable stream (pytest capture, a pipe wrapper).
        # Never let output plumbing stop the CLI from starting.
        pass


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


def _scraper_for(cruise_line, market: str | None = None):
    """Factory: get the right scraper for the cruise line (CLI entry points).

    `market` applies to NCL only — see NclScraper.__init__ / --market."""
    from core.models import CruiseLine
    from scraper.espresso import EspressoScraper
    from scraper.goccl import GoCCLScraper
    from scraper.ncl import NclScraper

    if cruise_line == CruiseLine.NCL:
        return NclScraper(market=market)
    if cruise_line == CruiseLine.GOCCL:
        return GoCCLScraper()
    return EspressoScraper()


def _login_base_url(cruise_line, settings):
    """Where to land a fresh browser session for a manual login check."""
    from core.models import CruiseLine

    if cruise_line == CruiseLine.NCL:
        return settings.ncl_search_url
    if cruise_line == CruiseLine.GOCCL:
        return settings.goccl_search_url
    return settings.espresso_home_url


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
    from utils.logging import setup_logging

    setup_logging(settings.log_level, settings.log_file)
    # Login is the one step a human must complete by hand (MFA etc.), so
    # it must always show a real window — no CLI flag exposes an override,
    # and none should: a hidden login window can't be logged into.
    login_headless = False
    print(f"LOGIN CHECK: browser_headless={login_headless}")

    cruise_line = CruiseLine(args.cruise_line.upper())
    scraper = _scraper_for(cruise_line, getattr(args, 'market', None))

    # Only this one login session runs visibly (if requested) — does not
    # affect settings.browser_headless, so scans started afterward keep
    # running headless as configured instead of inheriting this override.
    await scraper.start(headless=login_headless)
    try:
        base_url = _login_base_url(cruise_line, settings)
        await scraper.navigate(base_url)

        print(f"⚓ A browser session started for {cruise_line.value}.")
        print("   Please log in there now (username/password/MFA as usual).")
        print(f"   Waiting up to {args.timeout_minutes} minute(s) for login to complete...\n")

        # Require the same non-login URL on two consecutive polls (10s
        # apart) before declaring success. A single check is too eager: an
        # SSO/MFA redirect chain can land on an intermediate cruisingpower.com
        # URL that momentarily satisfies "doesn't contain login/signin"
        # before the user has actually finished authenticating — a single
        # snapshot mistakes that transient hop for real success and closes
        # the browser out from under the user mid-login (confirmed live
        # 2026-08-04: browser closed right after Neon started logging in,
        # and the saved session turned out invalid — every subsequent page
        # 404'd, including the plain homepage).
        deadline = time.monotonic() + args.timeout_minutes * 60
        poll_s = 5
        stable_url: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            if cruise_line in (CruiseLine.NCL, CruiseLine.GOCCL):
                logged_in = "login" not in scraper.page.url.lower() and "signin" not in scraper.page.url.lower()
            else:
                logged_in = await scraper._check_login()
            current_url = scraper.page.url
            if logged_in and current_url == stable_url:
                print("✅ Logged in — session saved. You can run scan/watch normally now.")
                return
            stable_url = current_url if logged_in else None
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


def remove_paid_in_full_from_watchlist(path: str, results) -> list[str]:
    """Remove bookings CONFIRMED PAID_IN_FULL from a watchlist file, so a
    booking that's already paid off stops being re-checked on every future
    run/pass.

    Only ever removes a booking whose result status is exactly
    BookingStatus.PAID_IN_FULL — never on ERROR, never on a guess, and
    never for a booking this run didn't actually check. Preserves
    comments/blank lines and the order/formatting of every other line
    untouched. Returns the list of booking IDs actually removed (empty if
    none were).
    """
    paid_in_full_ids = {r.booking_id for r in results if r.status.value == "PAID_IN_FULL"}
    if not paid_in_full_ids:
        return []

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    removed: list[str] = []
    kept_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped in paid_in_full_ids:
            removed.append(stripped)
            continue
        kept_lines.append(line)

    if removed:
        # CONFIRMED CRITICAL RISK, fixed 2026-08-26: this used to write the
        # file in place with `open(path, "w")`, which TRUNCATES to zero bytes
        # immediately and only then writes the kept lines back. A Ctrl+C,
        # process kill, power loss, or disk-full between those two moments
        # left `watchlist.txt` empty or half-written — i.e. the entire client
        # watchlist destroyed. That was a live risk, not a theoretical one:
        # this function runs inside run_persistent_watchlist_scan.py's hot
        # loop (every scan), and Ctrl+C is the documented way to stop that
        # script. Now written atomically: full content to a temp file in the
        # SAME directory (so os.replace can't cross a filesystem boundary),
        # flushed and fsync'd so the bytes are really on disk, then
        # os.replace() — which is atomic on both Windows and POSIX, so any
        # interruption leaves the ORIGINAL file fully intact.
        import os
        import tempfile

        directory = os.path.dirname(os.path.abspath(path))
        # Keep a rolling one-deep backup before replacing, so even a logic
        # error here (removing something it shouldn't) is recoverable.
        try:
            import shutil
            shutil.copy2(path, path + ".bak")
        except Exception:
            pass  # a missing backup must never block the real write

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".watchlist_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(kept_lines)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

        # AUDIT TRAIL, added 2026-08-26: nothing anywhere used to record
        # WHICH bookings were dropped or when — callers only printed the
        # list, so a wrong PAID_IN_FULL determination silently and
        # permanently removed a real client booking from all future scans
        # with no way to find out what was lost. Appended (never rewritten)
        # next to the watchlist itself.
        try:
            from datetime import datetime, timezone
            stamp = datetime.now(timezone.utc).isoformat()
            with open(path + ".removals.log", "a", encoding="utf-8") as log:
                for booking_id in removed:
                    log.write(f"{stamp}\t{booking_id}\tPAID_IN_FULL\n")
        except Exception:
            pass  # an audit-log failure must never lose the real removal

    return removed


async def _run_scan(args):
    from config.settings import settings
    from core.calculator import total_optimization_savings, unconfirmed_candidate_total
    from core.models import CruiseLine
    from models.database import init_db
    from services.booking_service import BookingService
    from services.csv_export import export_results_csv
    from services.excel_export import export_results_excel
    from utils.logging import setup_logging

    setup_logging(settings.log_level, settings.log_file)
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
    if args.capture_everything:
        print(f"   Capturing full page HTML + network traffic + action log to {args.capture_raw or 'data'}/")

    service = BookingService()

    def on_progress(job):
        print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

    def on_action(entry):
        detail = {k: v for k, v in entry.items() if k not in ("timestamp", "action", "cruise_line")}
        detail_str = " ".join(f"{k}={v}" for k, v in detail.items())
        print(f"   · {entry['action']}  {detail_str}")

    if args.headless_mode is False:
        print("   Opening a visible browser window — leave it alone, don't close it.")

    job = await service.start_scan(
        booking_ids,
        cruise_line,
        on_progress=on_progress,
        bypass_cache=getattr(args, "no_cache", False),
        raw_dump_dir=args.capture_raw,
        capture_market_data=args.capture_market_data,
        capture_everything=args.capture_everything,
        on_action=on_action if args.capture_everything else None,
        headless=args.headless_mode,
        # Without this, --market selected the LOGIN account and the scan
        # then silently used the default one (see BookingService._run_batch).
        market=getattr(args, "market", None),
    )

    # Wait for completion
    while job.status.value in ("PENDING", "RUNNING"):
        await asyncio.sleep(1)
        job = service.get_job(job.job_id) or job

    # Print results grouped by status — a clean report for watchlist runs
    print(f"\n{'='*50}")
    print(f"📊 Results: {len(job.results)} bookings checked\n")

    icons = {"OPTIMIZATION": "✅", "UPGRADE_AVAILABLE": "🆙", "TRAP": "⚠️", "NO_SAVING": "⏭", "ERROR": "❌",
             "PAID_IN_FULL": "💳", "WLT": "⏭", "SKIPPED_TODAY": "⏩",
             "NOT_ON_THIS_ACCOUNT": "🇨🇦"}
    status_order = [
        "OPTIMIZATION", "UPGRADE_AVAILABLE", "TRAP", "WLT", "PAID_IN_FULL",
        "NO_SAVING", "SKIPPED_TODAY", "NOT_ON_THIS_ACCOUNT", "ERROR",
    ]
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
            # See run_persistent_watchlist_scan.py's matching fix
            # (2026-08-12): TRAP/NO_SAVING can carry a positive net_saving
            # on paper (that's the point of those checks — catching a
            # "win" smaller than what's being given up), so only show the
            # dollar figure for statuses where it reflects a real,
            # recommended saving.
            saving = f" — ${r.net_saving:.2f}" if r.net_saving > 0 and status not in ("TRAP", "NO_SAVING") else ""
            print(f"   {r.booking_id}{saving}")
            if r.note:
                print(f"     {r.note}")
        print()

    # EXPORTS FIRST, watchlist mutation LAST — reordered 2026-08-26. This
    # path used to prune the watchlist BEFORE writing the reports, so any
    # failure in the file read/write (a non-UTF-8 hand edit, a permissions
    # problem, the atomic-write path erroring) propagated and the
    # just-completed scan's CSV and Excel were NEVER WRITTEN, even though
    # every result was sitting in memory. The `watch` path and
    # run_persistent_watchlist_scan.py already had this order right; only
    # `scan` had it backwards. Reports are the deliverable — write them
    # before touching anything mutable.

    # CSV export
    if args.output:
        try:
            csv_content = export_results_csv(job.results)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(csv_content)
            print(f"\n📁 CSV saved to: {args.output}")
        except Exception as e:
            print(f"\n⚠  CSV export FAILED (results are still in the database): {e}")

    # Excel export
    if args.excel:
        try:
            export_results_excel(job.results, args.excel)
            print(f"📊 Excel report saved to: {args.excel}")
        except Exception as e:
            print(f"⚠  Excel export FAILED (often: the file is open in Excel). "
                  f"Results are still in the database. {e}")

    # Confirmed paid-in-full bookings don't need to be re-checked next
    # time — only removes from the watchlist FILE, never the bookings.txt
    # source when --bookings was used directly (comma list has no file to
    # edit).
    if args.bookings_file:
        try:
            removed = remove_paid_in_full_from_watchlist(args.bookings_file, job.results)
            if removed:
                print(f"💳 Removed {len(removed)} paid-in-full booking(s) from {args.bookings_file}: {', '.join(removed)}\n")
        except Exception as e:
            print(f"⚠  Could not prune paid-in-full bookings from {args.bookings_file}: {e}")

    # Summary
    opts = [r for r in job.results if r.status.value == "OPTIMIZATION"]
    total_saving = total_optimization_savings(job.results)
    unconf_n, unconf_total = unconfirmed_candidate_total(job.results)
    print(f"\n💰 Total savings found: ${total_saving:.2f} across {len(opts)} booking(s)")
    if unconf_n:
        print(f"   {unconf_n} UNCONFIRMED candidate(s) worth ${unconf_total:,.2f} excluded from the total - verify before trusting")


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

    setup_logging(settings.log_level, settings.log_file)
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
    if args.headless_mode is False:
        print("   Each pass opens a visible browser window — leave it alone, don't close it.")
    print(f"   Output: {output_dir}/  (read-only checks — no rate is ever confirmed automatically)\n")

    service = BookingService()
    pass_num = 0

    try:
        while True:
            pass_num += 1
            started = datetime.now(timezone.utc)
            print(f"{'='*50}\n🔄 Pass {pass_num} — {started.isoformat()}")

            def on_progress(job):
                print(f"   [{job.progress_done}/{job.progress_total}] {job.current_booking_id or 'done'}")

            job = await service.start_scan(
                booking_ids,
                cruise_line,
                on_progress=on_progress,
                bypass_cache=True,
                raw_dump_dir=args.capture_raw,
                capture_market_data=args.capture_market_data,
                capture_everything=args.capture_everything,
                headless=args.headless_mode,
                market=getattr(args, "market", None),
            )

            while job.status.value in ("PENDING", "RUNNING"):
                await asyncio.sleep(1)
                job = service.get_job(job.job_id) or job

            csv_content = export_results_csv(job.results)
            csv_path = output_dir / f"pass{pass_num:03d}_{started.strftime('%Y-%m-%d_%H%M')}.csv"
            with open(csv_path, "w", encoding="utf-8") as f:
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
                        # FIXED 2026-08-26: `started` is already
                        # timezone-aware (datetime.now(timezone.utc)), so
                        # isoformat() emits the +00:00 offset and appending a
                        # literal "Z" on top produced
                        # "2026-08-26T16:04:47.018913+00:00Z" — not valid
                        # ISO-8601/RFC-3339, which datetime.fromisoformat()
                        # and every standard parser reject. Every alert line
                        # ever written to alerts.log was unparseable. The
                        # offset already conveys UTC; the "Z" was redundant
                        # AND wrong.
                        line = f"{started.isoformat()}  {r.status.value:12s}  {r.booking_id:12s}  ${r.net_saving:>10.2f}  {r.note}"
                        f.write(line + "\n")
                        print(f"   {icon} {r.booking_id}: {r.status.value} — ${r.net_saving:.2f}")

            # Confirmed paid-in-full bookings don't need to be re-checked on
            # a later pass of THIS SAME watch run — drop them from the
            # in-memory list too, not just the file, so an overnight watch
            # stops burning time re-confirming a booking that's already
            # paid off.
            if args.bookings_file:
                removed = remove_paid_in_full_from_watchlist(args.bookings_file, job.results)
                if removed:
                    print(f"   💳 Removed {len(removed)} paid-in-full booking(s) from {args.bookings_file}: {', '.join(removed)}")
                    removed_set = set(removed)
                    booking_ids = [b for b in booking_ids if b not in removed_set]

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
    login_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO, NCL, or GOCCL")
    login_parser.add_argument(
        "--market", default=None,
        help="NCL only: which agent account to use — US or CA. NCL runs a "
             "SEPARATE SeaWeb account per market, so Canadian (CAD) bookings "
             "are invisible on the US login and vice versa. Defaults to "
             "settings.ncl_default_market.",
    )
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
    scan_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO, NCL, or GOCCL")
    scan_parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore the NO_SAVING TTL cache and check every booking live. "
             "Without this, a booking already checked today comes back "
             "SKIPPED_TODAY — which made a 84-booking baseline run 62%% "
             "skipped and therefore useless as a measurement.",
    )
    scan_parser.add_argument(
        "--market", default=None,
        help="NCL only: which agent account to use — US or CA. NCL runs a "
             "SEPARATE SeaWeb account per market, so Canadian (CAD) bookings "
             "are invisible on the US login and vice versa. Defaults to "
             "settings.ncl_default_market.",
    )
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
    scan_parser.add_argument(
        "--capture-everything",
        action="store_true",
        help="Also capture full page HTML + structured extraction, all network traffic, and a "
             "step-by-step action log for every booking. Requires --capture-raw (defaults to "
             "'data' if not set). Increases scan time and disk use.",
    )
    scan_visibility = scan_parser.add_mutually_exclusive_group()
    scan_visibility.add_argument(
        "--headless", action="store_true",
        help="Run with no visible browser window (default — see config/settings.py "
             "browser_headless).",
    )
    scan_visibility.add_argument(
        "--visible", action="store_true",
        help="Pop a real, visible browser window for this scan so you can watch it work. "
             "Don't close the window yourself — closing it kills the scan.",
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
    watch_parser.add_argument("--cruise-line", default="ESPRESSO", help="ESPRESSO, NCL, or GOCCL")
    watch_parser.add_argument(
        "--market", default=None,
        help="NCL only: which agent account to use — US or CA. NCL runs a "
             "SEPARATE SeaWeb account per market, so Canadian (CAD) bookings "
             "are invisible on the US login and vice versa. Defaults to "
             "settings.ncl_default_market.",
    )
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
    watch_parser.add_argument(
        "--capture-everything",
        action="store_true",
        help="Also capture full page HTML + structured extraction, all network traffic, and a "
             "step-by-step action log for every booking, every pass. Requires --capture-raw "
             "(defaults to 'data' if not set). Increases scan time and disk use.",
    )
    watch_visibility = watch_parser.add_mutually_exclusive_group()
    watch_visibility.add_argument(
        "--headless", action="store_true",
        help="Run with no visible browser window (default — see config/settings.py "
             "browser_headless).",
    )
    watch_visibility.add_argument(
        "--visible", action="store_true",
        help="Pop a real, visible browser window for every pass so you can watch it work. "
             "Don't close the window yourself — closing it kills the current pass.",
    )

    args = parser.parse_args()

    # Resolve the --headless/--visible pair into one tri-state value:
    # True = force headless, False = force a visible window, None = defer
    # to settings.browser_headless. argparse's mutually_exclusive_group
    # already rejects passing both, so at most one of the two is True here.
    if args.command in ("scan", "watch"):
        if getattr(args, "visible", False):
            args.headless_mode = False
        elif getattr(args, "headless", False):
            args.headless_mode = True
        else:
            args.headless_mode = None

    if args.command in ("scan", "watch") and getattr(args, "capture_everything", False) and not args.capture_raw:
        args.capture_raw = "data"

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
