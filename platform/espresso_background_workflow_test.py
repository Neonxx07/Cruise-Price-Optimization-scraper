"""Does the FULL authenticated ESPRESSO workflow work with the window off-screen?

Neon 2026-09-23: *"u did not log me in to check you are just openening the
log in page"*.

Correct, and the distinction matters. The earlier matrix
(espresso_browser_mode_test.py) only proved which browsers the CDN edge
ACCEPTS - headed 200, every headless mode 404. That is a necessary gate and
it is where headless dies, but it proves nothing about the authenticated
workflow: search, booking load, category read, payment state, pricing,
release.

This script closes that gap. It runs the REAL production scraper -
EspressoScraper.check_booking, not a reimplementation - twice against the
SAME booking:

    1. window visible      (today's production behaviour)
    2. window off-screen   (ESPRESSO_BACKGROUND_BROWSER)

and compares the BookingResults field by field. The window is moved at
RUNTIME between the two, which is exactly what production would do, because
ESPRESSO's login needs MFA typed into a visible window.

    python espresso_background_workflow_test.py <booking_id>

It will open a visible browser and wait for you to log in if the saved
session has expired. Nothing is modified on the reservation: check_booking
reads, and always releases the booking afterwards.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scraper.espresso import EspressoScraper  # noqa: E402
from utils.logging import get_logger, setup_logging  # noqa: E402

logger = get_logger("bg_test")

# The fields that decide whether two runs agree. A background mode that
# changes any of these is not acceptable, however tidy the desktop looks.
COMPARE = ("status", "old_total", "new_total", "net_saving",
           "price_category", "new_price_category", "currency")


async def _wait_for_login(scraper: EspressoScraper, minutes: int) -> bool:
    """Give the operator time to complete MFA in the visible window."""
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if await scraper._check_login():
            return True
        print("    waiting for login... (complete MFA in the browser window)",
              flush=True)
        await asyncio.sleep(5)
    return False


def _snapshot(result) -> dict:
    out = {}
    for field in COMPARE:
        value = getattr(result, field, None)
        out[field] = value.value if hasattr(value, "value") else value
    return out


async def run(booking_id: str, login_minutes: int) -> int:
    setup_logging("INFO", "data/cruiseintel.log")
    scraper = EspressoScraper()
    # headless=False is not optional and is not a choice this script makes -
    # BaseScraper.start force-overrides ESPRESSO to visible regardless.
    await scraper.start(headless=False)
    runs: dict[str, dict] = {}
    try:
        from config.settings import settings as _s
        await scraper.navigate(_s.espresso_home_url)
        await scraper._settle_navigation()
        if not await scraper._check_login():
            print("\n  Not logged in. A browser window is open - please log in.\n")
            if not await _wait_for_login(scraper, login_minutes):
                print("  timed out waiting for login")
                return 2
        print("  logged in\n")

        for label, offscreen in (("visible", False), ("offscreen", True)):
            moved = await scraper.set_window_offscreen(offscreen)
            print(f"  [{label}] window move issued: {moved}")
            t0 = time.monotonic()
            try:
                result = await scraper.check_booking(booking_id)
                runs[label] = _snapshot(result)
                runs[label]["elapsed_s"] = round(time.monotonic() - t0, 1)
                runs[label]["error"] = None
            except Exception as exc:
                runs[label] = {"error": str(exc)[:160],
                               "elapsed_s": round(time.monotonic() - t0, 1)}
            print(f"  [{label}] {runs[label]}\n")
            # Always hand the desktop back before the next step or on exit.
            await scraper.set_window_offscreen(False)
    finally:
        await scraper.set_window_offscreen(False)
        await scraper.stop()

    print("\n  FIELD-BY-FIELD COMPARISON")
    ok = True
    for field in COMPARE + ("error",):
        a, b = runs.get("visible", {}).get(field), runs.get("offscreen", {}).get(field)
        same = a == b
        ok = ok and same
        print(f"    {field:20s} visible={str(a):24s} offscreen={str(b):24s} "
              f"{'MATCH' if same else '*** DIFFERS ***'}")
    print(f"\n  elapsed  visible={runs.get('visible',{}).get('elapsed_s')}s  "
          f"offscreen={runs.get('offscreen',{}).get('elapsed_s')}s")
    print(f"\n  RESULT: {'PASS - identical' if ok else 'FAIL - results diverge'}")

    out = Path("data") / "background_workflow_test.json"
    out.write_text(json.dumps(runs, indent=2, default=str), encoding="utf-8")
    print(f"  written: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("booking_id")
    ap.add_argument("--login-minutes", type=int, default=5)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.booking_id, a.login_minutes)))
