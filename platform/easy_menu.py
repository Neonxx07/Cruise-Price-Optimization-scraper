"""CruiseIntel Price Checker — Easy Menu.

Double-click START.bat to run this. No commands to type or remember.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import _run_login_check, _run_scan, _run_watch  # noqa: E402

WATCHLIST = "watchlist.txt"
REPORTS_DIR = Path("reports")
DATA_DIR = Path("data")


def _pause():
    input("\nPress Enter to go back to the menu...")


def _banner():
    print()
    print("=" * 50)
    print("   CRUISEINTEL PRICE CHECKER")
    print("=" * 50)


def _count_bookings() -> int:
    if not Path(WATCHLIST).exists():
        return 0
    with open(WATCHLIST, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip() and not line.strip().startswith("#"))


def menu_login():
    _banner()
    print("Opening a browser window — log into ESPRESSO like you normally would.")
    print("This window will close by itself once it sees you're logged in.\n")
    args = SimpleNamespace(cruise_line="ESPRESSO", timeout_minutes=15.0)
    asyncio.run(_run_login_check(args))
    _pause()


def _ask_headless_mode() -> bool | None:
    # ESPRESSO (cruisingpower.com) never works headless — its bot detection
    # blocks/breaks headless sessions — and easy_menu.py only ever drives
    # ESPRESSO (see WATCHLIST/cruise_line="ESPRESSO" below). Asking "run
    # invisibly?" used to be a real trap: pressing Enter silently launched a
    # headless session that scraper/base.py now force-overrides to visible
    # anyway, so the question's "invisible" answer was never actually
    # honored. Always visible, no prompt — callers below print the
    # visible-window notice.
    return False


def menu_scan():
    _banner()
    n = _count_bookings()
    if n == 0:
        print(f"You don't have any booking IDs yet. Add some first (option 4).")
        _pause()
        return
    headless_mode = _ask_headless_mode()
    print(f"\nChecking {n} booking(s) from {WATCHLIST}...\n")
    if headless_mode is False:
        print("Opening a visible browser window — leave it alone, don't close it.\n")
    REPORTS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    args = SimpleNamespace(
        bookings_file=WATCHLIST, bookings=None, cruise_line="ESPRESSO",
        output=str(REPORTS_DIR / f"report_{stamp}.csv"),
        excel=str(REPORTS_DIR / f"report_{stamp}.xlsx"),
        capture_raw=str(DATA_DIR),
        capture_market_data=False, capture_everything=False,
        headless_mode=headless_mode,
    )
    asyncio.run(_run_scan(args))
    print(f"\nDone! Open reports\\report_{stamp}.xlsx to see the results.")
    _pause()


def menu_watch():
    _banner()
    n = _count_bookings()
    if n == 0:
        print(f"You don't have any booking IDs yet. Add some first (option 4).")
        _pause()
        return
    print(f"This will keep re-checking your {n} booking(s) automatically.")
    hours_in = input("How many hours should it run? (just press Enter for 8): ").strip()
    minutes_in = input("Minutes between each check? (just press Enter for 60): ").strip()
    hours = float(hours_in) if hours_in else 8.0
    minutes = int(minutes_in) if minutes_in else 60
    headless_mode = _ask_headless_mode()
    REPORTS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)
    print(f"\nRunning every {minutes} min for {hours}h. Press Ctrl+C to stop early.")
    if headless_mode is False:
        print("Each pass opens a visible browser window — leave it alone, don't close it.")
    print()
    args = SimpleNamespace(
        bookings_file=WATCHLIST, bookings=None, cruise_line="ESPRESSO",
        interval_minutes=minutes, duration_hours=hours, max_passes=None,
        output_dir=str(REPORTS_DIR / "watch"), capture_raw=str(DATA_DIR),
        capture_market_data=False, capture_everything=False,
        headless_mode=headless_mode,
    )
    asyncio.run(_run_watch(args))
    _pause()


def menu_edit_list():
    _banner()
    if not Path(WATCHLIST).exists():
        Path(WATCHLIST).write_text("# Add one booking ID per line\n", encoding="utf-8")
    print(f"Opening {WATCHLIST} in Notepad.")
    print("Add one booking ID per line, then save and close Notepad.")
    subprocess.run(["notepad.exe", WATCHLIST])
    _pause()


def main():
    while True:
        _banner()
        n = _count_bookings()
        print(f"You currently have {n} booking(s) in your list.\n")
        print("1. Log into ESPRESSO")
        print("2. Check my bookings NOW (one time)")
        print("3. Keep checking automatically (overnight)")
        print("4. Edit my booking list")
        print("5. Exit")
        choice = input("\nType a number and press Enter: ").strip()
        if choice == "1":
            menu_login()
        elif choice == "2":
            menu_scan()
        elif choice == "3":
            menu_watch()
        elif choice == "4":
            menu_edit_list()
        elif choice == "5":
            print("Bye!")
            break
        else:
            print("Please type 1, 2, 3, 4, or 5.")


if __name__ == "__main__":
    main()
