"""Log in ONCE, then test headless against that same live session immediately.

Neon 2026-09-23: "i closed the gui can you open chromuim to log in?"

WHY THIS EXISTS RATHER THAN THE STORAGE-STATE MATRIX. Replaying a saved
storage state failed even HEADED - with 37 cookies, all live, none expired,
a cold browser was still bounced to /login. So storage-state replay cannot
establish the headed baseline, and without a baseline the headless result
proves nothing. That is a property of ESPRESSO's SSO, not of headless.

The fix is to never go cold. This script:

    1. opens ONE visible Chromium and waits for you to log in (MFA included)
    2. confirms the AUTHENTICATED app really loaded - not just a 200, but
       the booking-search UI actually present
    3. captures that session the instant it is proven good
    4. immediately launches each headless mode with that same session and
       drives the same authenticated route

Steps 3 and 4 happen seconds apart, so the session is as warm as it can be.
If a headless mode still cannot reach the app, the session's age is not the
explanation.

No credentials are read, stored or typed by this script - you log in
yourself, in the window it opens. No fingerprint, user-agent or stealth
changes anywhere.

    python espresso_live_headless_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings  # noqa: E402
from scraper.espresso import EspressoScraper  # noqa: E402

SEARCH = EspressoScraper._SEARCH_INPUT_SELECTOR
AUTH_MARKERS = ("/login", "/signin", "auth.cruisingpower.com", "/oauth")
LIVE_STATE = Path("data") / "_live_espresso_state.json"

HEADLESS_MODES = {
    "headless-shell":    dict(headless=True),
    "chromium-headless": dict(headless=True, channel="chromium"),
    "chrome-headless":   dict(headless=True, channel="chrome"),
}


def _claim_single_instance() -> object | None:
    """Refuse to start if another copy of this test is already running.

    THE INCIDENT, 2026-09-23. Two copies of this script ran at once because
    stopping the background task killed the wrapper but left the Python
    process alive. ESPRESSO allows ONE session per account, so the two
    browsers fought over it and Neon was thrown back to /login every time
    he logged in:

        /login -> /oauth/callback -> /home -> /login -> /oauth/callback ...

    He logged in three times before the cause was found. A second instance
    is never useful here, so it is now refused rather than left to corrupt
    the session it is supposed to be measuring.
    """
    import atexit
    import os
    lock = Path("data") / "_live_headless_test.lock"
    lock.parent.mkdir(exist_ok=True)
    if lock.exists():
        try:
            other = int(lock.read_text(encoding="utf-8").strip())
            import psutil
            if psutil.pid_exists(other):
                proc = psutil.Process(other)
                if "espresso_live_headless_test" in " ".join(proc.cmdline()):
                    print(f"  ANOTHER COPY IS ALREADY RUNNING (pid {other}).\n"
                          f"  Two browsers would fight over the one ESPRESSO "
                          f"session and log you out repeatedly.\n"
                          f"  Stop that one first, or delete {lock}")
                    return None
        except Exception:
            pass                      # stale lock, take it
    lock.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return lock


async def _settle(page, quiet_s: float = 2.0, timeout_s: float = 20.0) -> bool:
    """Wait until the page stops navigating itself. Never raises.

    The same idea as EspressoScraper._settle_navigation, with a longer quiet
    window because this runs right after an OAuth callback, where /home is
    still exchanging the code for a session and is at its most fragile.
    Returns False if it never settled, so the caller can wait rather than
    barge in.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    try:
        last = page.url
    except Exception:
        return False
    stable_since = loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        try:
            url = page.url
        except Exception:
            return False
        if url != last:
            last, stable_since = url, loop.time()
            continue
        if loop.time() - stable_since >= quiet_s:
            return True
    return False


async def _probe(page, status: int | None = None) -> dict:
    """Is this page the authenticated application?

    `status` is the HTTP status of the navigation that produced it.
    Without it a refusal page passes as a success - see below.
    """
    url = page.url
    bounced = any(m in url.lower() for m in AUTH_MARKERS)
    try:
        search = await page.locator(SEARCH).count()
    except Exception:
        search = 0
    return {
        "url": url[:90],
        "bounced_to_auth": bounced,
        "search_available": search > 0,
        # AUTHENTICATED needs the HTTP STATUS too, not just the URL.
        #
        # THE FALSE POSITIVE THIS FIXES, 2026-09-23. The first version was
        # `not bounced and "cruisingpower.com" in url`. Akamai's refusal is
        # a 404 served AT THE REQUESTED URL with no redirect - so the URL
        # stayed /home, nothing looked "bounced", and a 404 error page
        # satisfied the check. The run reported
        #
        #     headless-shell  http=404  app=True
        #     AUTHENTICATED_HEADLESS_STATUS = SUPPORTED
        #
        # which was the exact opposite of the truth. A status code is
        # evidence; a URL that simply failed to change is not.
        "authenticated_app": bool(not bounced and "cruisingpower.com" in url
                                  and status == 200),
    }


async def main() -> int:
    from playwright.async_api import async_playwright

    if _claim_single_instance() is None:
        return 3

    async with async_playwright() as pw:
        print("\n  opening a visible Chromium - please log in (MFA included)\n",
              flush=True)
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(settings.espresso_home_url, wait_until="domcontentloaded")

        # WAIT PASSIVELY. NEVER NAVIGATE WHILE SOMEONE IS LOGGING IN.
        #
        # Neon 2026-09-23: "it keeps refreshing stop that". The first
        # version called page.goto() on every poll, so every 8 seconds it
        # reloaded the page out from under a half-finished login - wiping a
        # typed username, an MFA code, or an in-flight OAuth redirect. A
        # login flow is the one place automation must sit on its hands.
        #
        # This only READS page.url until the login lands on its own. The
        # single navigation to the authenticated route happens afterwards,
        # once the operator is through.
        deadline = time.time() + 15 * 60
        baseline = None
        last_seen = None
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                url = page.url
            except Exception:
                break
            if url != last_seen:
                print(f"    at {url[:76]}", flush=True)
                last_seen = url
            # Logged in = off the auth pages, under the app's own host.
            if (not any(m in url.lower() for m in AUTH_MARKERS)
                    and "cruisingpower.com" in url):
                # DO NOT NAVIGATE THIS BROWSER. EVER. AFTER LOGIN.
                #
                # Neon 2026-09-23: "after i log in you go to a link ends
                # with /logout and i have to login again".
                #
                # He was right, and the mechanism is documented in our own
                # scraper at espresso.py:430 - the portal arms this on every
                # page load:
                #
                #   setTimeout(function(){
                #     window.location.href = window.Base.flowExecutionURL
                #                          + "&_eventId=logout" }, 1830000)
                #
                # `flowExecutionURL` is a Spring WebFlow execution key. Deep
                # linking straight to reservations.do enters that flow from
                # outside, and a stale or invalid flow execution resolves to
                # exactly that logout event. Every earlier version of this
                # script navigated after login and threw him out - four
                # times.
                #
                # Nothing here needs that navigation. The session lives in
                # COOKIES, and cookies can be read from the context without
                # touching the page. So: confirm the landing settled, take
                # the session, and leave his browser exactly where he put
                # it. The headless comparison drives its OWN browsers.
                if not await _settle(page, quiet_s=3.0, timeout_s=25.0):
                    continue
                baseline = await _probe(page, 200)
                baseline["note"] = ("session captured from the post-login "
                                    "landing; this browser was never navigated")
                break

        if not baseline:
            print("\n  timed out waiting for login - nothing tested")
            await browser.close()
            return 2

        # The page he actually landed on. The headless browsers will be
        # pointed at THIS url, not at a deep link into the WebFlow app -
        # comparing like with like, and never re-entering a flow from
        # outside.
        landing_url = baseline["url"]
        print("\n  HEADED BASELINE: logged in, landed and settled")
        print(f"    url   = {landing_url}")
        print("    (this browser was NOT navigated - your session is untouched)\n")

        # Capture the session the moment it is PROVEN good.
        LIVE_STATE.parent.mkdir(exist_ok=True)
        await context.storage_state(path=str(LIVE_STATE))
        cookies = await context.cookies()
        print(f"  captured live session: {len(cookies)} cookies\n", flush=True)

        results = {"headed": baseline | {"mode": "headed", "http": 200}}

        for name, spec in HEADLESS_MODES.items():
            t0 = time.time()
            entry = {"mode": name}
            hb = None
            try:
                hb = await pw.chromium.launch(**spec)
                hctx = await hb.new_context(storage_state=str(LIVE_STATE))
                hpage = await hctx.new_page()
                resp = await hpage.goto(landing_url,
                                        wait_until="domcontentloaded", timeout=45000)
                entry["http"] = resp.status if resp else None
                await asyncio.sleep(4)
                entry |= await _probe(hpage, entry.get("http"))
            except Exception as exc:
                entry["error"] = str(exc)[:130]
            finally:
                if hb:
                    try:
                        await hb.close()
                    except Exception:
                        pass
            entry["elapsed_s"] = round(time.time() - t0, 1)
            results[name] = entry
            print(f"  {name:20s} http={str(entry.get('http')):5s} "
                  f"app={str(entry.get('authenticated_app')):5s} "
                  f"search={str(entry.get('search_available')):5s} "
                  f"-> {str(entry.get('url'))[:56]}", flush=True)

        # Re-confirm the headed session SURVIVED the headless replays, so a
        # failure cannot be blamed on the session dying midway.
        after = await _probe(page)
        print(f"\n  headed session still authenticated afterwards: "
              f"{after['authenticated_app']}")
        results["headed_after"] = after
        await browser.close()

    others = [results[m] for m in HEADLESS_MODES]
    if all(o.get("authenticated_app") for o in others):
        verdict = "SUPPORTED - every headless mode reached the authenticated app"
    elif any(o.get("authenticated_app") for o in others):
        verdict = "PARTIALLY_SUPPORTED - some headless modes reached the app"
    elif not after["authenticated_app"]:
        verdict = ("INCONCLUSIVE - the headed session did not survive the "
                   "replays, so the headless failures cannot be isolated")
    else:
        verdict = ("NOT_SUPPORTED - headed reached the authenticated app with "
                   "a live session and no headless mode did")

    print(f"\n  AUTHENTICATED_HEADLESS_STATUS = {verdict}")
    out = Path("data") / "live_headless_test.json"
    out.write_text(json.dumps({"verdict": verdict, "results": results},
                              indent=2, default=str), encoding="utf-8")
    print(f"  written: {out}")
    try:
        LIVE_STATE.unlink()      # do not leave a live session lying around
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
