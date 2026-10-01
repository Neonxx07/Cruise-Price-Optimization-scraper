"""Which browser modes can actually reach ESPRESSO, and what do they cost?

Neon 2026-09-23, after true headless was proven blocked:

    "How do we keep the browser environment that ESPRESSO accepts while
     removing the browser's impact on the user's desktop?"

WHAT THIS DOES NOT DO. No fingerprint changes, no user-agent edits, no
stealth scripts, no automation-signal masking. Every mode below launches the
SAME browser with the SAME identity; only the WINDOW placement differs. A
minimized or off-screen window is an ordinary OS window state, not a
disguise - the site sees exactly what it sees today.

THE COMPATIBILITY GATE is an unauthenticated GET of the ESPRESSO home URL.
That is what separated the modes before and it needs no login, no cookies
and no session, so it cannot disturb the GUI's live session:

    headed                       HTTP 200 -> redirected to /login
    headless (no channel)        HTTP 404 -> "Not found"
    headless channel=chromium    HTTP 404 -> "Not found"
    headless channel=chrome      HTTP 404 -> "Not found"

A mode PASSES only by reaching 200 AND landing on the real login page.

THE THROTTLING RISK, which is the reason this measures more than a status
code. Chromium deliberately throttles windows it believes are occluded or
minimized - background timers, rAF and rendering all slow down. ESPRESSO is
a JavaScript-heavy SPA, so a window that is hidden the wrong way could turn
a working scraper into a slow or stalled one without ever failing outright.
That is why each mode is also timed, and why no anti-throttling flags are
added up front: add them only if throttling is actually measured.

    python espresso_browser_mode_test.py            # compatibility + cost
    python espresso_browser_mode_test.py --repeat 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings  # noqa: E402

# Window geometry that keeps a REAL, fully-rendering window off the user's
# screen. -32000 is the position Windows itself uses for minimized windows.
_OFFSCREEN = "--window-position=-32000,-32000"

MODES: dict[str, dict] = {
    # ── the candidates: headed browser, window out of the user's way ───
    "visible-headed":     dict(headless=False),
    "offscreen-headed":   dict(headless=False, args=[_OFFSCREEN]),
    # `--start-minimized` IS SILENTLY IGNORED by Playwright's Chromium.
    # Kept as a mode so the fact stays measured rather than remembered:
    # VERIFIED 2026-09-23 via the Win32 window rect, it lands at
    # (10, 10, 1306, 818) with IsIconic False - byte-identical to
    # visible-headed. It passed the HTTP gate and looked like a winning
    # mode until the window state itself was checked, which is the whole
    # reason verify_window_state() exists.
    "minimized-headed":   dict(headless=False, args=["--start-minimized"]),
    # ── the controls: known-blocked, kept so drift is detectable ───────
    "headless-shell":     dict(headless=True),
    "chromium-headless":  dict(headless=True, channel="chromium"),
    "chrome-headless":    dict(headless=True, channel="chrome"),
}

# Modes that are expected to reach ESPRESSO. A regression here is real.
EXPECT_PASS = ("visible-headed", "offscreen-headed", "minimized-headed")
# Modes expected to be refused at the edge. If one of these ever PASSES,
# Akamai's posture changed - re-run the full compatibility matrix before
# changing anything in production.
EXPECT_FAIL = ("headless-shell", "chromium-headless", "chrome-headless")
# The only mode that both reaches ESPRESSO and keeps the window off the
# user's desktop. VERIFIED off-screen, not merely assumed.
BACKGROUND_MODE = "offscreen-headed"


def _rss_mb(pids) -> float:
    try:
        import psutil
    except Exception:
        return 0.0
    total = 0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_info().rss
        except Exception:
            pass
    return round(total / 1e6, 1)


def _chrome_pids() -> set[int]:
    """Every live Chromium/Chrome PID right now.

    Playwright's ASYNC Browser has no `.process` attribute (that is the sync
    API), so the browser's own process tree is not reachable from here.
    Snapshotting before and after the launch and taking the difference gives
    the same answer without depending on a private attribute.
    """
    try:
        import psutil
    except Exception:
        return set()
    out = set()
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info.get("name") or "").lower() in ("chrome.exe", "chromium.exe"):
            out.add(proc.info["pid"])
    return out


def verify_window_state(pids) -> dict:
    """Where the browser window ACTUALLY is, per Win32 - not per the flag.

    THE REASON THIS EXISTS. `--start-minimized` passed the HTTP gate and
    looked like a working background mode. It is silently ignored: the
    window sits at (10, 10, 1306, 818), fully on screen, IsIconic False.
    A launch flag is a request, not a result, and the only honest way to
    report "the window is out of the way" is to ask the OS where it is.

    Returns {} on anything but Windows, or if the window cannot be found -
    an unknown answer must never read as "it worked".
    """
    if not sys.platform.startswith("win"):
        return {}
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return {}
    u32 = ctypes.windll.user32
    screen = (u32.GetSystemMetrics(0), u32.GetSystemMetrics(1))
    found: list[dict] = []

    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rect))
            if (rect.right - rect.left) > 200:
                found.append({
                    "rect": (rect.left, rect.top, rect.right, rect.bottom),
                    "iconic": bool(u32.IsIconic(hwnd)),
                })
        return True

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    try:
        u32.EnumWindows(proto(_cb), 0)
    except Exception:
        return {}
    if not found:
        return {}
    win = found[0]
    left, top, right, bottom = win["rect"]
    on_screen = right > 0 and bottom > 0 and left < screen[0] and top < screen[1]
    return {"rect": win["rect"], "iconic": win["iconic"],
            "on_screen": on_screen, "off_desktop": not on_screen}


async def run_mode(pw, name: str, spec: dict) -> dict:
    """Launch one mode, load ESPRESSO's home URL, measure. Never raises."""
    out: dict = {"mode": name}
    before = _chrome_pids()
    t0 = time.monotonic()
    browser = None
    try:
        browser = await pw.chromium.launch(**spec)
        out["launch_s"] = round(time.monotonic() - t0, 2)

        context = await browser.new_context()
        page = await context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)[:80]))
        requests = {"n": 0}
        page.on("request", lambda r: requests.__setitem__("n", requests["n"] + 1))

        t1 = time.monotonic()
        resp = await page.goto(settings.espresso_home_url,
                               wait_until="domcontentloaded", timeout=45000)
        out["http"] = resp.status if resp else None
        out["nav_s"] = round(time.monotonic() - t1, 2)

        # Let any edge redirect or bot check settle, then look at what we got.
        await asyncio.sleep(3)
        out["url"] = page.url
        out["title"] = (await page.title())[:60]

        # A JS-timer probe. If the window state is throttling the page, this
        # is where it shows - a minimized window can stretch a 1s timer.
        t2 = time.monotonic()
        await page.evaluate("() => new Promise(r => setTimeout(r, 1000))")
        out["timer_1s_actual_ms"] = int((time.monotonic() - t2) * 1000)

        out["requests"] = requests["n"]
        out["page_errors"] = len(page_errors)

        # PASS means the real login page, not merely a 200.
        body = (await page.inner_text("body"))[:400].lower()
        out["login_page"] = ("sign in" in body or "login" in out["url"].lower())
        out["pass"] = bool(out["http"] == 200 and out["login_page"])

        pids = _chrome_pids() - before
        out["procs"] = len(pids)
        out["rss_mb"] = _rss_mb(pids)
        # Ask the OS where the window is, rather than trusting the flag.
        out.update({f"win_{k}": v for k, v in verify_window_state(pids).items()})
    except Exception as exc:
        out["error"] = str(exc)[:120]
        out["pass"] = False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
    out["total_s"] = round(time.monotonic() - t0, 2)
    return out


async def main(repeat: int, only: list[str] | None) -> int:
    from playwright.async_api import async_playwright

    names = only or list(MODES)
    results: dict[str, list[dict]] = {n: [] for n in names}

    async with async_playwright() as pw:
        for _ in range(repeat):
            for name in names:
                r = await run_mode(pw, name, dict(MODES[name]))
                results[name].append(r)
                mark = "PASS" if r.get("pass") else "FAIL"
                print(f"  {name:20s} {mark}  http={str(r.get('http')):5s} "
                      f"timer={str(r.get('timer_1s_actual_ms')):6s}ms "
                      f"rss={str(r.get('rss_mb')):7s}MB "
                      f"procs={str(r.get('procs')):3s} "
                      f"{r.get('error', '')}", flush=True)

    print(f"\n{'MODE':20s} {'RESULT':7s} {'HTTP':>5s} {'1s TIMER':>9s} "
          f"{'RSS MB':>8s} {'PROCS':>6s} {'LAUNCH':>7s} {'NAV':>6s}")
    print("  " + "-" * 76)
    summary = {}
    for name in names:
        runs = results[name]
        ok = sum(1 for r in runs if r.get("pass"))
        def med(key, default=0):
            vals = [r[key] for r in runs if isinstance(r.get(key), (int, float))]
            return statistics.median(vals) if vals else default
        summary[name] = {
            "passes": f"{ok}/{len(runs)}",
            "http": runs[-1].get("http"),
            "timer_ms": med("timer_1s_actual_ms"),
            "rss_mb": med("rss_mb"),
            "procs": med("procs"),
            "launch_s": med("launch_s"),
            "nav_s": med("nav_s"),
        }
        s = summary[name]
        print(f"  {name:20s} {s['passes']:7s} {str(s['http']):>5s} "
              f"{s['timer_ms']:>8.0f}ms {s['rss_mb']:>8.1f} {s['procs']:>6.0f} "
              f"{s['launch_s']:>6.1f}s {s['nav_s']:>5.1f}s")

    out_path = Path(__file__).with_name("data") / "browser_mode_matrix.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "runs": results},
                                   indent=2, default=str), encoding="utf-8")
    print(f"\n  written: {out_path}")

    # Exit non-zero only if the production baseline itself broke.
    baseline = summary.get("visible-headed", {}).get("passes", "0/0")
    return 0 if baseline.split("/")[0] != "0" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", nargs="*", choices=list(MODES), default=None)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.repeat, a.only)))
