"""Base scraper with Playwright browser management, retry, and proxy support.

All cruise line scrapers inherit from BaseScraper.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings
from core.models import BookingResult, CruiseLine
from utils.logging import get_logger

logger = get_logger(__name__)

# Generic, site-agnostic extraction — since the exact markup for cabin
# details, add-ons, dining, gratuities, insurance, itinerary etc. varies
# by portal and page, this pulls every table and every label/value-style
# pair it can find, plus the full visible text, rather than hardcoding
# selectors for fields we haven't verified against a live portal.
_STRUCTURED_EXTRACT_JS = """
(() => {
    const tables = Array.from(document.querySelectorAll('table')).map(t => {
        const headers = Array.from(t.querySelectorAll('th')).map(th => th.textContent.trim());
        const rows = Array.from(t.querySelectorAll('tbody tr, tr')).map(tr =>
            Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim())
        ).filter(r => r.length);
        return { headers, rows };
    });
    const labelPairs = [];
    document.querySelectorAll('[class*="label"], dt').forEach(el => {
        const valueEl = el.nextElementSibling;
        if (valueEl) {
            const label = el.textContent.trim();
            const value = valueEl.textContent.trim();
            if (label && value) labelPairs.push({ label, value });
        }
    });
    return {
        url: location.href,
        title: document.title,
        tables,
        labelPairs,
        bodyText: document.body ? document.body.innerText : '',
    };
})()
"""


class BaseScraper(ABC):
    """
    Abstract base for all cruise line scrapers.

    Manages a Playwright browser instance with:
    - Headless/headed mode
    - User data dir for authenticated sessions
    - Proxy support (design-ready)
    - Automatic cleanup
    """

    cruise_line: CruiseLine

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        # When set, subclasses append raw API responses here (for later
        # offline analysis / calculator development) — read-only capture
        # of data already fetched, nothing new is requested because of it.
        self.raw_dump_dir: Optional[str] = None
        self.last_market_data: dict | None = None

        # Action log: every navigate/search/click/API-call step, so a scan
        # can be replayed/audited after the fact. Always recorded in
        # memory; written to raw_dump_dir/actions.jsonl when set.
        self.action_log: list[dict] = []
        self.on_action: Optional[Callable[[dict], None]] = None

        # When True, also capture full page HTML + a best-effort structured
        # extraction of every page visited, and every network request/
        # response the browser makes — all read-only, nothing new is
        # requested because of it. Written under raw_dump_dir.
        self.capture_everything: bool = False

    def _storage_state_path(self) -> Optional[str]:
        """Where the saved login session (cookies + localStorage) lives.

        Not a Chromium user-data-dir — Chromium marks the actual SSO
        session cookies (e.g. ESPRESSO's iPlanetDirectoryPro/LtpaToken2)
        as session-only, and wipes those from its own on-disk cookie store
        on a clean shutdown even inside a persistent profile. Explicitly
        snapshotting via Playwright's storage_state and reloading it next
        run bypasses that entirely, regardless of how the cookie was
        flagged.
        """
        if not settings.browser_user_data_dir:
            return None
        import os
        return os.path.join(settings.browser_user_data_dir, "storage_state.json")

    async def start(self, headless: Optional[bool] = None) -> None:
        """Launch the browser and create a page.

        Args:
            headless: Overrides settings.browser_headless for this session
                only (e.g. a one-off visible login check) without mutating
                the shared settings — leaving it None uses the configured
                default for every other caller.
        """
        self._playwright = await async_playwright().start()

        launch_args: dict = {
            "headless": settings.browser_headless if headless is None else headless,
        }

        # Proxy support (design-ready)
        if settings.proxy_url:
            launch_args["proxy"] = {
                "server": settings.proxy_url,
            }
            if settings.proxy_username:
                launch_args["proxy"]["username"] = settings.proxy_username
                launch_args["proxy"]["password"] = settings.proxy_password

        self._browser = await self._playwright.chromium.launch(**launch_args)

        context_args: dict = {}
        storage_state_path = self._storage_state_path()
        if storage_state_path:
            import os
            if os.path.exists(storage_state_path):
                context_args["storage_state"] = storage_state_path
        self._context = await self._browser.new_context(**context_args)
        self._page = await self._context.new_page()

        self._page.set_default_timeout(settings.scraper_timeout_ms)

        if self.capture_everything:
            self._page.on("response", lambda r: asyncio.create_task(self._capture_response(r)))

        logger.info(
            "browser.started", cruise_line=self.cruise_line.value,
            headless=launch_args["headless"], restored_session=bool(context_args),
        )

    async def stop(self) -> None:
        """Save the login session, then close the browser and cleanup."""
        try:
            storage_state_path = self._storage_state_path()
            if storage_state_path and self._context:
                import os
                os.makedirs(os.path.dirname(storage_state_path), exist_ok=True)
                await self._context.storage_state(path=storage_state_path)
                logger.info("browser.session_saved", cruise_line=self.cruise_line.value)
        except Exception as e:
            logger.warning("browser.session_save_error", error=str(e))

        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("browser.cleanup_error", error=str(e))
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            logger.info("browser.stopped", cruise_line=self.cruise_line.value)

    @property
    def page(self) -> Page:
        """Get the active page, raising if not started."""
        if self._page is None:
            raise RuntimeError("Scraper not started — call start() first")
        return self._page

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> None:
        """Navigate to a URL and wait for load."""
        logger.debug("navigate", url=url)
        await self.page.goto(url, wait_until=wait_until)

    async def wait_for(self, selector: str, timeout: int | None = None) -> None:
        """Wait for an element to appear on page."""
        t = timeout or settings.scraper_timeout_ms
        await self.page.wait_for_selector(selector, timeout=t)

    async def evaluate(self, expression: str):
        """Run JavaScript in the page context."""
        return await self.page.evaluate(expression)

    def dump_raw(self, booking_id: str, raw: dict) -> None:
        """Append a raw API response to raw_dump_dir/raw_responses.jsonl, if set."""
        if not self.raw_dump_dir:
            return
        from datetime import datetime

        entry = {
            "booking_id": booking_id,
            "cruise_line": self.cruise_line.value,
            "captured_at": datetime.utcnow().isoformat(),
            "raw": raw,
        }
        self._append_jsonl("raw_responses.jsonl", entry)

    def _append_jsonl(self, filename: str, entry: dict) -> None:
        """Append one JSON entry as a line to raw_dump_dir/filename."""
        if not self.raw_dump_dir:
            return
        import json
        import os

        os.makedirs(self.raw_dump_dir, exist_ok=True)
        path = os.path.join(self.raw_dump_dir, filename)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def log_action(self, action: str, **detail) -> None:
        """Record one step of the automated browser flow (audit trail).

        Always kept in-memory on self.action_log; also appended to
        raw_dump_dir/actions.jsonl when set, and forwarded to on_action
        (used by the GUI to show a live activity log) if set.
        """
        from datetime import datetime

        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "cruise_line": self.cruise_line.value,
            "action": action,
            **detail,
        }
        self.action_log.append(entry)
        self._append_jsonl("actions.jsonl", entry)
        if self.on_action:
            try:
                self.on_action(entry)
            except Exception:
                logger.warning("action_log.callback_error", exc_info=True)

    async def _capture_response(self, response) -> None:
        """Record metadata (and body, for xhr/fetch/document) for every
        network response — read-only observation of traffic the browser
        already made, nothing new is requested because of it."""
        try:
            from datetime import datetime

            request = response.request
            entry: dict = {
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
                        entry["response_body_size"] = len(body)
                except Exception:
                    pass
            self._append_jsonl("network_traffic.jsonl", entry)
        except Exception:
            logger.warning("network_capture.error", exc_info=True)

    async def dump_page_snapshot(self, booking_id: str, step: str) -> None:
        """Save full page HTML + a best-effort structured extraction
        (tables, label/value pairs, visible text) for the current page.
        Only runs when capture_everything is on and raw_dump_dir is set."""
        if not self.capture_everything or not self.raw_dump_dir:
            return
        import json
        import os
        from datetime import datetime

        pages_dir = os.path.join(self.raw_dump_dir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        base = f"{booking_id}__{step}__{stamp}"

        try:
            html = await self.page.content()
            with open(os.path.join(pages_dir, base + ".html"), "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            logger.warning("page_snapshot.html_error", booking_id=booking_id, step=step, exc_info=True)

        try:
            structured = await self.page.evaluate(_STRUCTURED_EXTRACT_JS)
            with open(os.path.join(pages_dir, base + ".json"), "w", encoding="utf-8") as f:
                json.dump(structured, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning("page_snapshot.json_error", booking_id=booking_id, step=step, exc_info=True)

        self.log_action("page_snapshot", booking_id=booking_id, step=step, file=base)

    async def dump_failure_snapshot(self, booking_id: str, step: str, error: str) -> None:
        """Capture a screenshot + HTML when a scrape step fails.

        Unlike dump_page_snapshot, this always runs (not gated on
        capture_everything) — a failure is exactly the moment you need to
        see what the browser was actually looking at (stuck on a login
        page, a rate-limit interstitial, a blank page, etc.), and that
        can't be inspected after the fact in headless mode any other way.
        """
        if not self.raw_dump_dir:
            return
        import json
        import os
        from datetime import datetime

        failures_dir = os.path.join(self.raw_dump_dir, "failures")
        os.makedirs(failures_dir, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        base = f"{booking_id}__{step}__{stamp}"

        try:
            await self.page.screenshot(path=os.path.join(failures_dir, base + ".png"), full_page=True)
        except Exception:
            logger.warning("failure_snapshot.screenshot_error", booking_id=booking_id, step=step, exc_info=True)

        try:
            html = await self.page.content()
            with open(os.path.join(failures_dir, base + ".html"), "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            logger.warning("failure_snapshot.html_error", booking_id=booking_id, step=step, exc_info=True)

        try:
            with open(os.path.join(failures_dir, base + ".json"), "w", encoding="utf-8") as f:
                json.dump({"url": self.page.url, "error": error}, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.warning("failure_snapshot.meta_error", booking_id=booking_id, step=step, exc_info=True)

        self.log_action("failure_snapshot", booking_id=booking_id, step=step, error=error, file=base)

    async def fill_and_submit(self, selector: str, value: str, submit_selector: str) -> None:
        """Fill an input and click submit."""
        await self.page.fill(selector, value)
        await self.page.click(submit_selector)

    @abstractmethod
    async def check_booking(self, booking_id: str, capture_market_data: bool = False) -> BookingResult:
        """Check a single booking for optimization opportunities."""
        ...

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()
