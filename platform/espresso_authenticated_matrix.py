"""Does an AUTHENTICATED ESPRESSO session work headless?

Neon 2026-09-23, correcting the earlier investigation:

    "The tests only opened the public /login URL without authenticating.
     That proves only: headed -> /login -> 200, headless -> /login -> 404.
     It does NOT prove that an authenticated ESPRESSO session cannot
     operate headlessly."

He is right, and the distinction is sharp. With a valid stored session the
production scraper goes STRAIGHT to the authenticated route and may never
touch /login at all. The earlier 8/8 result is therefore a

    PUBLIC_ENDPOINT_HEADLESS_TEST

and must not be quoted as an

    AUTHENTICATED_ESPRESSO_HEADLESS_TEST

This script is the second one. It loads the SAME production storage state
into four browser modes and drives the same authenticated route the scraper
uses, recording where each one diverges.

WHAT IT DOES NOT DO. No login, no credentials, no MFA automation, no
fingerprint or user-agent changes, no stealth. The storage state is only
READ - Playwright never writes it back unless asked, and this never asks -
so the production session file is left exactly as it was.

    python espresso_authenticated_matrix.py
    python espresso_authenticated_matrix.py --booking 3001007

A WARNING WORTH KNOWING. ESPRESSO allows one active session per account, so
replaying a stored session in a separate browser can disturb a session the
GUI is holding. Run this when no scan is in flight.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings  # noqa: E402
from scraper.espresso import EspressoScraper  # noqa: E402

STORAGE = Path(settings.browser_user_data_dir) / "storage_state_ESPRESSO.json"

MODES: dict[str, dict] = {
    "headed":            dict(headless=False),
    "headless-shell":    dict(headless=True),
    "chromium-headless": dict(headless=True, channel="chromium"),
    "chrome-headless":   dict(headless=True, channel="chrome"),
}

# Anything on an auth host or a login path means we were bounced out.
AUTH_MARKERS = ("/login", "/signin", "auth.cruisingpower.com", "/oauth")


async def run_mode(pw, name: str, spec: dict, booking: str | None) -> dict:
    out: dict = {"mode": name, "storage_state_loaded": False,
                 "authenticated_app": False, "booking_search_available": False,
                 "booking_opened": False, "redirects": []}
    browser = None
    t0 = time.monotonic()
    try:
        browser = await pw.chromium.launch(**spec)
        # READ ONLY. Never written back - the production session file is
        # left byte-identical.
        context = await browser.new_context(storage_state=str(STORAGE))
        cookies = await context.cookies()
        out["storage_state_loaded"] = len(cookies) > 0
        out["cookies_loaded"] = len(cookies)

        page = await context.new_page()
        statuses: list[tuple[str, int]] = []

        def _on_resp(r):
            if r.request.is_navigation_request():
                statuses.append((r.url[:90], r.status))
        page.on("response", _on_resp)
        page.on("framenavigated",
                lambda f: out["redirects"].append(f.url[:90]) if f == page.main_frame else None)

        # STRAIGHT TO THE AUTHENTICATED ROUTE, not /login. This is the whole
        # point of the correction: with a live session, production never
        # visits the public login page.
        resp = await page.goto(settings.espresso_base_url,
                               wait_until="domcontentloaded", timeout=45000)
        out["initial_http"] = resp.status if resp else None
        await asyncio.sleep(4)          # let SSO / SPA settle
        out["final_url"] = page.url
        out["title"] = (await page.title())[:60]
        out["nav_statuses"] = statuses[:6]

        bounced = any(m in page.url.lower() for m in AUTH_MARKERS)
        out["bounced_to_auth"] = bounced
        out["authenticated_app"] = bool(
            not bounced and out["initial_http"] == 200
            and "cruisingpower.com" in page.url)

        # The search box is the real proof the authenticated app rendered.
        try:
            count = await page.locator(EspressoScraper._SEARCH_INPUT_SELECTOR).count()
            out["booking_search_available"] = count > 0
        except Exception as exc:
            out["search_probe_error"] = str(exc)[:80]

        if booking and out["booking_search_available"]:
            try:
                await page.fill(EspressoScraper._SEARCH_INPUT_SELECTOR.split(",")[0], booking)
                await page.click(EspressoScraper._SEARCH_BUTTON_SELECTOR.split(",")[0])
                await asyncio.sleep(5)
                body = (await page.inner_text("body"))[:3000]
                out["booking_opened"] = booking in body
                out["after_search_url"] = page.url[:90]
            except Exception as exc:
                out["booking_error"] = str(exc)[:100]
    except Exception as exc:
        out["error"] = str(exc)[:140]
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
    out["elapsed_s"] = round(time.monotonic() - t0, 1)
    return out


async def main(booking: str | None) -> int:
    from playwright.async_api import async_playwright
    if not STORAGE.exists():
        print(f"no storage state at {STORAGE}")
        return 2
    print(f"  storage state: {STORAGE.name}  "
          f"(saved {time.strftime('%Y-%m-%d %H:%M', time.localtime(STORAGE.stat().st_mtime))})\n")

    results = {}
    async with async_playwright() as pw:
        for name, spec in MODES.items():
            r = await run_mode(pw, name, dict(spec), booking)
            results[name] = r
            print(f"  {name:20s} http={str(r.get('initial_http')):5s} "
                  f"cookies={str(r.get('cookies_loaded')):4s} "
                  f"app={str(r['authenticated_app']):5s} "
                  f"search={str(r['booking_search_available']):5s} "
                  f"booking={str(r['booking_opened']):5s}  "
                  f"-> {str(r.get('final_url'))[:58]}", flush=True)

    print(f"\n  {'Mode':20s} {'Session':>8s} {'App':>6s} {'Search':>7s} "
          f"{'Booking':>8s} {'HTTP':>6s} {'Time':>7s}")
    print("  " + "-" * 68)
    for name, r in results.items():
        yn = lambda b: "YES" if b else "NO"          # noqa: E731
        print(f"  {name:20s} {yn(r['storage_state_loaded']):>8s} "
              f"{yn(r['authenticated_app']):>6s} {yn(r['booking_search_available']):>7s} "
              f"{yn(r['booking_opened']):>8s} {str(r.get('initial_http')):>6s} "
              f"{r['elapsed_s']:>6.1f}s")

    headed = results.get("headed", {})
    if not headed.get("authenticated_app"):
        verdict = ("NOT_YET_DETERMINED - the stored session did not authenticate "
                   "even HEADED, so this run cannot separate 'headless fails' "
                   "from 'the session is stale'. Re-run with a fresh session.")
    elif all(results[m].get("authenticated_app") for m in MODES if m != "headed"):
        verdict = "SUPPORTED - every headless mode reached the authenticated app"
    elif any(results[m].get("authenticated_app") for m in MODES if m != "headed"):
        verdict = "PARTIALLY_SUPPORTED - some headless modes reached the app"
    else:
        verdict = ("NOT_SUPPORTED - headed reached the authenticated app and "
                   "no headless mode did")
    print(f"\n  AUTHENTICATED_HEADLESS_STATUS = {verdict}")

    out = Path("data") / "authenticated_matrix.json"
    out.write_text(json.dumps({"verdict": verdict, "results": results},
                              indent=2, default=str), encoding="utf-8")
    print(f"  written: {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--booking", default=None,
                    help="optional known-safe booking id to search for")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.booking)))
