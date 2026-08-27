"""FIRST LIVE TEST for NCL support — a single guided, watched run.

Nothing in this codebase has ever been run against the real live
seawebagents.ncl.com portal before (confirmed 2026-08-25: zero NCL rows
exist anywhere in this project's captured data). Every fix that shipped
2026-08-26 — the same-category reprice redesign, the two dialog-handling
safety fixes, the corrected search-button/addon-table selectors — was
built from a real recorded session and code review, never from an actual
live run through this specific script. This script exists to make that
first live run as safe and as diagnosable as possible in one attempt:

- ALWAYS runs non-headless, no override — you should watch this run,
  not trust it unattended, the same rule this project already enforces
  for ESPRESSO's login and every cruise line's first-ever login.
- Checks exactly ONE booking, not a batch — if something's wrong, only
  one booking's edit-lock cycle is at risk, not several in sequence.
- Captures a full Playwright trace (screenshots + DOM + network +
  console at every step) to `data/ncl_live_test/trace.zip` — if
  anything looks wrong, `playwright show-trace data/ncl_live_test/trace.zip`
  replays exactly what happened without needing another live attempt.
- Also writes the existing raw-capture files (actions.jsonl, page
  snapshots, failure snapshots if any) to `data/ncl_live_test/`, same as
  a normal --capture-everything run.
- Prints the full result plus every log line from check_booking, and
  keeps the browser open at the end so you can look around before it
  closes.

Usage:
    python run_ncl_live_check.py <booking_id>            # full check
    python run_ncl_live_check.py <booking_id> --dry-run   # read-only, never enters edit mode

--dry-run is the SAFEST first attempt and is worth doing first: it logs
in, searches the booking, reads its state and addons, and stops right
before entering edit mode — so it exercises login, search, the
__preloaded_data read, and the addon-table parsing (all the things most
likely to be wrong on a first run) while taking ZERO risk of locking a
real client booking for 30 minutes. Only run without --dry-run once the
dry run looks right.
"""

from __future__ import annotations

import asyncio
import sys
import time

from config.settings import settings
from core.models import CruiseLine
from scraper.ncl import NclScraper
from utils.logging import setup_logging


async def _login(scraper: NclScraper) -> bool:
    """Try saved-credential auto-login first, fall back to manual.

    Auto-login never raises (see NclScraper.auto_login) — any non-"OK"
    status just means we fall through to the human typing it in, which
    is exactly the pre-existing behavior."""
    status = await scraper.auto_login()
    if status == "OK":
        print("Logged in automatically using saved credentials.")
        return True

    if status == "NO_CREDENTIALS_SAVED":
        print(
            "\nNo saved NCL credentials found — falling back to manual login.\n"
            "   (Tip: run `python save_login.py` and pick NCL to store them "
            "encrypted in your OS credential store, then this step becomes automatic.)"
        )
    else:
        print(f"\nAuto-login did not complete (status: {status}) — falling back to manual login.")

    await scraper.navigate(settings.ncl_login_url)
    print(f"\nOpened {settings.ncl_login_url}")
    print("Please log in there now.")
    return await _wait_for_login(scraper)


async def _wait_for_login(scraper: NclScraper, timeout_minutes: float = 15.0) -> bool:
    """Same two-consecutive-polls discipline as main.py's _run_login_check
    (a single URL snapshot can catch a transient redirect mid-login and
    declare success too early) — NCL's own "logged in" signal is simpler
    than ESPRESSO's (no MFA/SSO observed for this account, per the
    2026-08-26 recorded session), but the same care applies."""
    print(f"\nWaiting up to {timeout_minutes:.0f} minute(s) for you to log in...")
    deadline = time.monotonic() + timeout_minutes * 60
    poll_s = 5
    stable_url: str | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        url = scraper.page.url
        logged_in = "login" not in url.lower() and "signin" not in url.lower()
        if logged_in and url == stable_url:
            print("Logged in.")
            return True
        stable_url = url if logged_in else None
        print("   ...still waiting for login")
    return False


async def _dry_run(scraper: NclScraper, booking_id: str) -> None:
    """Everything up to (but NOT including) entering edit mode.

    Deliberately duplicates check_booking's steps 1-4 rather than adding
    a `dry_run` branch inside check_booking itself — the real flow stays
    exactly as it runs in production, with no extra conditional paths to
    reason about in the code that touches real bookings."""
    print("\nDRY RUN — will NOT enter edit mode, cannot lock the booking.\n")
    await scraper.navigate(settings.ncl_search_url)
    await scraper.wait_for("#SWXMLForm_SearchReservation_ResID", timeout=15000)
    print("Search page loaded.")

    await scraper._search_booking(booking_id)
    await scraper.wait_for(
        '.item.current, #res-switch-edit, #res-edit-save, [class*="ReservationSummary"]',
        timeout=20000,
    )
    print("Booking summary loaded.")
    await scraper.dump_page_snapshot(booking_id, "dryrun_booking_summary")

    preload = await scraper._read_preloaded_data()
    print("\n__preloaded_data read:")
    for key in ("ok", "resId", "isPaid", "isLocked", "category", "invoiceTotal", "currentPromos"):
        print(f"   {key:16} = {preload.get(key)!r}")
    if not preload.get("ok"):
        print(f"   ERROR: {preload.get('error')!r}")

    addons = await scraper._scrape_addons()
    print(f"\nAddons parsed ({len(addons)} rows) — check guest/name/qty are in the RIGHT fields:")
    for a in addons:
        print(f"   guest={a.get('guest')!r:28} name={a.get('name')!r:52} qty={a.get('qty')!r}")
    if not addons:
        print("   (none found — if this booking really has addons, _scrape_addons needs a look)")

    switch_present = await scraper.page.query_selector("#res-switch-edit")
    print(f"\n'Switch to Edit Mode' button present: {bool(switch_present)}")
    print("Stopping here — edit mode was never entered, nothing is locked.")


async def main() -> None:
    raw_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not raw_args:
        print("Usage: python run_ncl_live_check.py <booking_id> [<booking_id> ...] [--dry-run]")
        print("       (comma-separated also works: 3000007,3000006,3000004)")
        sys.exit(1)
    # Accept space- and/or comma-separated IDs, deduped, order preserved.
    booking_ids: list[str] = []
    for arg in raw_args:
        for part in arg.split(","):
            part = part.strip()
            if part and part not in booking_ids:
                booking_ids.append(part)
    dry_run = "--dry-run" in flags

    setup_logging(settings.log_level)

    print("=" * 70)
    print("   NCL LIVE CHECK — watched, non-headless")
    print("=" * 70)
    print(f"\nBookings ({len(booking_ids)}): {', '.join(booking_ids)}")
    print(f"Mode:    {'DRY RUN (read-only, never enters edit mode)' if dry_run else 'FULL CHECK (will enter + release edit mode)'}")
    print("A visible browser window will open now. Watch it.")
    if not dry_run:
        print(
            "\nEach booking is entered into edit mode and released again. If this "
            "script ends unexpectedly (e.g. you kill the process) a booking may "
            "stay locked for up to 30 minutes — check it directly in the portal "
            "if that happens. Consider running with --dry-run first."
        )

    scraper = NclScraper()
    scraper.raw_dump_dir = "data/ncl_live_test"
    scraper.trace_path = "data/ncl_live_test/trace.zip"
    scraper.capture_everything = True
    scraper.on_action = lambda entry: print(f"   [{entry.get('action')}] {entry}")

    await scraper.start(headless=False)
    try:
        logged_in = await _login(scraper)
        if not logged_in:
            print("\nNot logged in. Nothing was checked. Run this again when ready.")
            return

        results = []
        for i, booking_id in enumerate(booking_ids, 1):
            print("\n" + "=" * 70)
            print(f"   BOOKING {i}/{len(booking_ids)}: {booking_id}")
            print("=" * 70)
            if dry_run:
                await _dry_run(scraper, booking_id)
                continue

            print(f"\nRunning check_booking('{booking_id}') — watch the browser window.\n")
            result = await scraper.check_booking(booking_id, capture_market_data=True)
            results.append(result)

            print(f"\nStatus:           {result.status.value}")
            print(f"Category:         {result.price_category}")
            print(f"Invoice price:    ${result.old_total:.2f}")
            print(f"Current price:    ${result.new_total:.2f}")
            print(f"Difference:       ${result.old_total - result.new_total:.2f}")
            print(f"Net saving:       ${result.net_saving:.2f}")
            print(f"Confidence:       {result.confidence}")
            print(f"Note:             {result.note}")
            if result.error:
                print(f"Error:            {result.error}")

            # Pace between bookings, same spirit as BookingService's
            # randomized inter-booking delay -- don't hammer a real portal.
            if i < len(booking_ids):
                await asyncio.sleep(4)

        if results:
            # Same column shape as the project owner's own reference
            # report, so the two can be compared row-for-row directly.
            print("\n" + "=" * 78)
            print("   SUMMARY (compare against your own report)")
            print("=" * 78)
            header = f"{'Reservation':<13}{'Cat':<6}{'Invoice':>12}{'Current':>12}{'Diff':>9}  {'Status':<14}"
            print(header)
            print("-" * 78)
            for r in results:
                diff = r.old_total - r.new_total
                print(
                    f"{r.booking_id:<13}{(r.price_category or '?'):<6}"
                    f"{r.old_total:>12,.2f}{r.new_total:>12,.2f}{diff:>9,.2f}  {r.status.value:<14}"
                )

        # Every dialog the portal raised, and what we did with it — the
        # thing most likely to reveal a wrong assumption on a first run
        # (see NclScraper._install_dialog_handler).
        print("\n" + "-" * 70)
        print(f"Dialogs seen this session ({len(scraper.dialogs_seen)}):")
        if not scraper.dialogs_seen:
            print("   (none)")
        for d in scraper.dialogs_seen:
            print(f"   [{d['action']:9}] {d['type']:14} {d['message'][:90]!r}")

        print(f"\nRaw capture written to: {scraper.raw_dump_dir}/")
        print(f"Trace will be saved to:  {scraper.trace_path}")
        print(f"View it with:            playwright show-trace {scraper.trace_path}")

    finally:
        input("\nPress Enter to close the browser (look around first if you want)...")
        await scraper.stop()
        print("Browser closed. Trace saved.")


if __name__ == "__main__":
    asyncio.run(main())
