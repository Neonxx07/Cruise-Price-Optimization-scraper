"""Princess (POLAR Online) check - see scraper/princess.py and docs/PRINCESS_POLAR.md.

    python run_princess_check.py browser          # one window; sign in, click Book & Manage Cruises
    python run_princess_check.py check REF [REF…] # in that window: read + price own category
    python run_princess_check.py probe REF CODE [CODE]  # tick promos, save the next screen, stop

Never cancels, saves, resets or confirms anything; never goes past Pricing
Detail. Writes data/princess_check_<stamp>/ (results.json, report.md and every
page visited). Nothing is written to cruise_intel.db yet.
"""
import asyncio
import os
import sys
from datetime import date, datetime

from scraper.princess import CDP_URL, LOGIN_URL, Stop, run

PROFILE = os.path.join("data", "princess_browser_profile")


async def browser():
    from playwright.async_api import async_playwright
    port = CDP_URL.rsplit(":", 1)[1]
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE, headless=False,
            args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(LOGIN_URL)
        print("BROWSER READY - sign in, then click Book & Manage Cruises. Close the window to end.",
              flush=True)
        closed = asyncio.Event()
        ctx.on("close", lambda _: closed.set())
        await closed.wait()


async def probe(ref, codes):
    """python run_princess_check.py probe REF CODE [CODE] - learn the next screen."""
    from playwright.async_api import async_playwright

    from scraper.princess import PolarSession
    out_dir = os.path.join("data", f"princess_probe_{datetime.now():%Y%m%dT%H%M%S}")
    os.makedirs(out_dir, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = None
        for p in browser.contexts[0].pages:
            if (await p.title()).startswith("POLAR"):
                page = p
        if page is None:
            print("no POLAR tab open")
            return
        s = PolarSession(page, out_dir)
        try:
            print("landed on:", await s.probe_compare(ref, codes), "| notices", s.check.notices,
                  "| dialogs", s.dialogs, flush=True)
        except Stop as exc:
            await s.save("STOPPED_page")
            print("STOPPED:", exc, flush=True)
    print("pages:", out_dir)


def report(results, out_dir):
    lines = ["# Princess check", "", f"{datetime.now():%Y-%m-%d %H:%M}", "",
             "| Booking | Outcome | Package | Now (fare+pkgs) | Best Buy quote | Saving | Verdict / note |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        v = r.verdict or {}
        now = v.get("booking_fare_plus_packages")
        quote = v.get("quote_fare_plus_packages")
        cur = (r.landing or {}).get("currency") or ""
        saving = v.get("saving") or 0.0
        now_s = f"{now:,.2f} {cur}" if now is not None else ""
        quote_s = f"{quote:,.2f}" if quote is not None else ""
        saving_s = f"{saving:,.2f}" if saving else ""
        verdict = f"{v.get('status', '')} - {v.get('note', '')}" if v else r.why
        lines.append(f"| {r.ref} | {r.outcome} | {r.tier or ''} | {now_s} | {quote_s} | "
                     f"{saving_s} | {verdict} |")
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) >= 2 and sys.argv[1] == "probe":
        asyncio.run(probe(sys.argv[2].upper(), [c.upper() for c in sys.argv[3:]]))
        return
    if len(sys.argv) < 2 or sys.argv[1] not in ("browser", "check"):
        print(__doc__)
        return
    if sys.argv[1] == "browser":
        asyncio.run(browser())
        return
    refs = [a.strip().upper() for a in sys.argv[2:] if a.strip()]
    out_dir = os.path.join("data", f"princess_check_{datetime.now():%Y%m%dT%H%M%S}")
    try:
        results = asyncio.run(run(refs, out_dir, today=date.today()))
    except Stop as exc:
        print("STOPPED:", exc)
        return
    report(results, out_dir)
    print("report:", os.path.join(out_dir, "report.md"))


if __name__ == "__main__":
    main()
