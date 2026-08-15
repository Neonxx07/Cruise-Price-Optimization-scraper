"""Two-phase manual session recorder for building a new cruise-line
integration (starting with MSC) from a real human walkthrough, instead of
guessing selectors from a DevTools export (see CONTRIBUTING.md's "adding
a new cruise line" lesson from the GoCCL build).

Phase 1 (no recording): a visible browser opens to a blank page. Neon
navigates to the MSC portal and logs in by hand. Nothing is captured yet,
specifically so a typed password never ends up in a saved trace file.

Phase 2 (starts once the "start" signal file appears): Playwright tracing
(full DOM snapshots + a click-by-click timeline + screenshots) and a
network-response JSONL log both start. Neon does his normal real
booking-lookup process in that same window.

Phase transitions are driven by signal files rather than terminal input,
since this process runs with no interactive stdin attached — an external
controller (Claude, on Neon saying "logged in" / "done" in chat) creates
each signal file at the right moment by writing to SIGNAL_DIR.

Output, under data/msc_recording_<timestamp>/:
  trace.zip              - open with `playwright show-trace trace.zip`,
                            or drag-drop at https://trace.playwright.dev
  network_traffic.jsonl  - every network request/response during phase 2
"""

import asyncio
import json
import os
from datetime import datetime

from playwright.async_api import async_playwright

STORAGE_STATE_PATH = "browser-profile/storage_state_MSC.json"
SIGNAL_DIR = "data/msc_recording_signals"
START_SIGNAL = os.path.join(SIGNAL_DIR, "start")
STOP_SIGNAL = os.path.join(SIGNAL_DIR, "stop")


async def wait_for_signal(path: str, poll_s: float = 1.0) -> None:
    while not os.path.exists(path):
        await asyncio.sleep(poll_s)


async def capture_response(response, out_path):
    try:
        request = response.request
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "url": response.url,
            "method": request.method,
            "status": response.status,
            "resource_type": request.resource_type,
        }
        if request.resource_type in ("xhr", "fetch", "document"):
            try:
                entry["request_post_data"] = request.post_data
            except Exception:
                pass
            try:
                body = await response.body()
                if len(body) <= 200_000:
                    entry["response_body"] = body.decode("utf-8", errors="replace")
                else:
                    entry["response_body_truncated"] = True
            except Exception:
                pass
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


async def main():
    os.makedirs(SIGNAL_DIR, exist_ok=True)
    for stale in (START_SIGNAL, STOP_SIGNAL):
        if os.path.exists(stale):
            os.remove(stale)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = f"data/msc_recording_{stamp}"
    os.makedirs(out_dir, exist_ok=True)
    network_path = os.path.join(out_dir, "network_traffic.jsonl")
    trace_path = os.path.join(out_dir, "trace.zip")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context_args = {}
        if os.path.exists(STORAGE_STATE_PATH):
            context_args["storage_state"] = STORAGE_STATE_PATH
        context = await browser.new_context(**context_args)
        page = await context.new_page()
        await page.goto("about:blank")

        print("PHASE 1 — no recording yet. Log in to MSC in the browser window.")
        print(f"Waiting for start signal: {START_SIGNAL}")
        await wait_for_signal(START_SIGNAL)

        print("Recording started.")
        page.on("response", lambda r: asyncio.create_task(capture_response(r, network_path)))
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)

        print(f"Waiting for stop signal: {STOP_SIGNAL}")
        await wait_for_signal(STOP_SIGNAL)

        await context.tracing.stop(path=trace_path)
        os.makedirs(os.path.dirname(STORAGE_STATE_PATH), exist_ok=True)
        await context.storage_state(path=STORAGE_STATE_PATH)
        await browser.close()

        print(f"Saved trace to: {trace_path}")
        print(f"Saved network log to: {network_path}")
        print("Session cookies saved for next time too.")


asyncio.run(main())
