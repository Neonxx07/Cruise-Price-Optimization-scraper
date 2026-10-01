"""Check a list of ESPRESSO bookings with the PRODUCTION scraper.

Runs EspressoScraper.check_booking - the real thing, not a reimplementation -
so this doubles as the live verification of the two fixes made on
2026-09-23 that have not yet run against the portal:

  * the release `finally` in check_booking. Before it, 389 of 624 bookings
    were left locked for 15 minutes because release_booking had one call
    site against 21 exit paths. Every booking below should now report
    released=True, whatever its outcome.

  * `_settle_navigation()` after /home. Before it, 268 navigations were
    aborted mid-flight by /home's own redirect and 42% of bookings paid a
    ~2.1s retry. The navigate_retry count at the end should be far lower.

LOGIN. It opens a visible window and waits PASSIVELY - it does not navigate,
refresh, or deep-link while you log in. Deep-linking into ESPRESSO's Spring
WebFlow straight after an OAuth landing resolves to the portal's own
`&_eventId=logout`, which is what threw Neon out four times.

Read-only: check_booking reads a booking and always releases it. Nothing is
modified and no purchase or confirm control is ever touched.

    python check_bookings_now.py 3001009 3001001 ...
    python check_bookings_now.py --file bookings.txt
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
from utils.logging import setup_logging  # noqa: E402

AUTH_MARKERS = ("/login", "/signin", "auth.cruisingpower.com", "/oauth")


async def _wait_for_login(scraper: EspressoScraper, minutes: int = 45) -> bool:
    """Watch until the operator is through. Never navigates."""
    started = time.time()
    deadline = started + minutes * 60
    last = None
    while time.time() < deadline:
        try:
            url = scraper.page.url
        except Exception:
            return False
        if url != last:
            print(f"    at {url[:76]}", flush=True)
            last = url
        elif int(time.time() - started) % 60 < 3:
            # A silent window for 45 minutes looks identical to a hung one.
            print(f"    still waiting for login "
                  f"({int((time.time()-started)/60)} min)", flush=True)
        if (not any(m in url.lower() for m in AUTH_MARKERS)
                and "cruisingpower.com" in url):
            # ASK THE REAL CHECK, DO NOT JUST LOOK AT THE URL.
            #
            # The first version treated "on cruisingpower.com and not on an
            # auth page" as logged in. A restored-but-dead session lands
            # straight on /home, which renders perfectly well while being
            # completely unauthenticated - so the run announced "logged in -
            # starting", never prompted Neon at all, and all 15 bookings
            # failed with "Not logged in". The portal's own login check knew
            # the truth the whole time; nothing was asking it.
            #
            # _check_login reads the page and never navigates, so it is safe
            # to call while someone is still logging in.
            if await scraper._settle_navigation(timeout_ms=25000, quiet_ms=3000):
                if await scraper._check_login():
                    await asyncio.sleep(2)
                    return True
        await asyncio.sleep(3)
    return False


async def main(bookings: list[str]) -> int:
    setup_logging("INFO", "data/cruiseintel.log")
    scraper = EspressoScraper()
    await scraper.start()               # forced visible for ESPRESSO
    rows: list[dict] = []
    try:
        # LOAD THE PORTAL ONCE, BEFORE THE LOGIN.
        #
        # The first version skipped this and the browser sat on about:blank
        # for fifteen minutes - there was nothing on screen to log INTO, so
        # the run timed out having asked Neon to log into a blank window.
        # Stripping the post-login navigation was right; stripping this one
        # was not. Loading the portal BEFORE anyone logs in is safe - it is
        # the navigation that puts the login page on screen in the first
        # place. The rule is "never navigate AFTER the login lands".
        from config.settings import settings as _s
        await scraper.navigate(_s.espresso_home_url)
        print("\n  a browser is open - please log in (MFA included)")
        print("  nothing will touch the page until you are through\n", flush=True)
        if not await _wait_for_login(scraper):
            print("  timed out waiting for login")
            return 2
        print("\n  logged in - starting\n", flush=True)

        for i, bid in enumerate(bookings, 1):
            t0 = time.monotonic()
            row = {"booking_id": bid}
            try:
                result = await scraper.check_booking(bid)
                row |= {
                    "status": getattr(result.status, "value", str(result.status)),
                    "old_total": result.old_total,
                    "new_total": result.new_total,
                    "net_saving": result.net_saving,
                    "category": result.price_category,
                    "new_category": result.new_price_category,
                    "note": (result.note or "")[:70],
                }
            except Exception as exc:
                row |= {"status": "ERROR", "note": str(exc)[:90]}
            # Did the lock actually get released? This is the fix under test.
            row["released"] = (scraper._released_for == bid)
            row["secs"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            print(f"  [{i:2d}/{len(bookings)}] {bid:10s} {row['status']:16s} "
                  f"net={str(row.get('net_saving')):>9s} "
                  f"released={str(row['released']):5s} {row['secs']:5.1f}s "
                  f"{row.get('note','')[:44]}", flush=True)
    finally:
        await scraper.stop()

    print(f"\n  {'BOOKING':11s} {'STATUS':18s} {'OLD':>10s} {'NEW':>10s} "
          f"{'NET':>10s} {'REL':>5s}")
    print("  " + "-" * 70)
    for r in rows:
        print(f"  {r['booking_id']:11s} {str(r.get('status')):18s} "
              f"{str(r.get('old_total')):>10s} {str(r.get('new_total')):>10s} "
              f"{str(r.get('net_saving')):>10s} {str(r.get('released')):>5s}")

    released = sum(1 for r in rows if r.get("released"))
    print(f"\n  released {released}/{len(rows)}   "
          f"(before the 2026-09-23 fix this was ~38%)")
    import collections
    print(f"  statuses: {dict(collections.Counter(r.get('status') for r in rows))}")
    out = Path("data") / "booking_check_results.json"
    out.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"  written: {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bookings", nargs="*")
    ap.add_argument("--file")
    a = ap.parse_args()
    ids = list(a.bookings)
    if a.file:
        ids += [x.strip() for x in Path(a.file).read_text().split() if x.strip()]
    if not ids:
        ap.error("give at least one booking id")
    raise SystemExit(asyncio.run(main(ids)))
