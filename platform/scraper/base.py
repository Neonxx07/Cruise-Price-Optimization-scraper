"""Base scraper with Playwright browser management, retry, and proxy support.

All cruise line scrapers inherit from BaseScraper.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Callable, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings
from core.models import BookingResult, CruiseLine
from utils.logging import get_logger, track_background_task

logger = get_logger(__name__)


def _write_text_file(path: str, content: str) -> None:
    """Plain blocking file write, run via asyncio.to_thread() by callers
    so it doesn't stall the event loop while it completes — same bytes
    written either way, just off the async critical path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def is_dead_browser_error(exc: Exception) -> bool:
    """Whether an exception means the underlying Playwright browser/context/
    page died mid-scrape (as opposed to a normal portal-level failure like a
    bad selector or a real API error) — the signal that reusing the same
    scraper for the next booking would just fail identically every time, and
    that BookingService's restart path should run instead.

    MOVED HERE 2026-08-13 (Phase 0 correctness audit) from
    services/booking_service.py's private static method, so scraper
    implementations (NclScraper, GoCCLScraper) can share the exact same
    check rather than each hand-rolling their own copy that could drift.

    CONFIRMED REAL RISK, fixed 2026-08-13: also now matches "crash" —
    Playwright models a renderer crash as a DISTINCT condition from
    "closed" (page.is_closed() can still report False on a crashed page),
    and the previous "has been closed"/"target closed" strings alone would
    never recognize it, silently letting every remaining booking in a batch
    fail identically with no self-healing restart."""
    msg = str(exc).lower()
    return "has been closed" in msg or "target closed" in msg or "crash" in msg


def _sanitize_filename_component(value: str) -> str:
    """CONFIRMED REAL RISK 2026-08-12: dump_page_snapshot/
    dump_failure_snapshot build a filename directly from booking_id (and
    step) via a plain f-string, then os.path.join() it under
    raw_dump_dir — booking_id is watchlist/API-controlled and was never
    validated for safe characters anywhere upstream. A value containing
    '..' path-traversal segments, or a Windows absolute path (a drive
    letter or UNC prefix causes os.path.join to silently DISCARD the
    base directory entirely, per documented ntpath behavior), would let
    a dump escape the intended pages/failures directory. Never trust an
    external identifier as a path component — strip it down to a plain
    filename-safe token first."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:100] or "unknown"

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

        # Persistent, append-mode file handles for JSONL capture files,
        # keyed by absolute path — opened once on first write and kept
        # open for the life of the scan instead of open()+close() on every
        # single append (network_traffic.jsonl alone can get dozens of
        # appends per page). Captures exactly the same content either way;
        # this only removes the repeated-syscall overhead. Closed in stop().
        self._jsonl_handles: dict = {}

        # See utils.logging.track_background_task — retains a strong
        # reference to each fire-and-forget response-capture task (one
        # per network response when capture_everything=True) so it can't
        # be prematurely garbage-collected, logs any exception, and lets
        # stop() wait for in-flight captures before closing the JSONL
        # handles/browser out from under them.
        self._background_tasks: set = set()

    def _storage_state_path(self) -> Optional[str]:
        """Where the saved login session (cookies + localStorage) lives.

        Not a Chromium user-data-dir — Chromium marks the actual SSO
        session cookies (e.g. ESPRESSO's iPlanetDirectoryPro/LtpaToken2)
        as session-only, and wipes those from its own on-disk cookie store
        on a clean shutdown even inside a persistent profile. Explicitly
        snapshotting via Playwright's storage_state and reloading it next
        run bypasses that entirely, regardless of how the cookie was
        flagged.

        One file per cruise line: they used to share a single
        storage_state.json, which meant logging into any one cruise line
        silently overwrote the saved session for the other two (each
        session only ever visits one cruise line's domain, so saving
        always clobbered whatever the file held before) — confirmed in
        practice when a GoCCL login wiped out an already-working ESPRESSO
        session.
        """
        if not settings.browser_user_data_dir:
            return None
        import os
        return os.path.join(
            settings.browser_user_data_dir, f"storage_state_{self.cruise_line.value}.json",
        )

    async def start(self, headless: Optional[bool] = None) -> None:
        """Launch the browser and create a page.

        Args:
            headless: Overrides settings.browser_headless for this session
                only (e.g. a one-off visible login check) without mutating
                the shared settings — leaving it None uses the configured
                default for every other caller.
        """
        # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness audit):
        # this used to have no error handling at all — if chromium.launch()
        # (or anything after it) failed, the just-started `self._playwright`
        # driver subprocess was never stopped, since nothing called it and
        # the exception propagated straight out. services/booking_service.py's
        # mid-batch restart path calls start() on a fresh scraper after a
        # dead-browser detection specifically — a launch failure there,
        # already a real possibility (resource exhaustion after repeated
        # crashes), used to leak one Playwright driver process per failed
        # restart attempt for the remaining lifetime of the app. Every step
        # below is unchanged when it succeeds; only a failure partway
        # through now cleans up what was already started before re-raising.
        self._playwright = await async_playwright().start()
        try:
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
                def _on_response(r):
                    track_background_task(self._background_tasks, asyncio.create_task(self._capture_response(r)))
                self._page.on("response", _on_response)

            logger.info(
                "browser.started", cruise_line=self.cruise_line.value,
                headless=launch_args["headless"], restored_session=bool(context_args),
            )
        except Exception:
            logger.error("browser.start_failed_cleaning_up", cruise_line=self.cruise_line.value)
            try:
                if self._browser is not None:
                    await self._browser.close()
            except Exception:
                pass
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None
            raise

    async def stop(self) -> None:
        """Save the login session, then close the browser and cleanup.

        Safe to call more than once (every resource is set to None at
        the end, and every step below is guarded by `if self._x:` first)
        and safe to call when the browser/context is already dead
        (exactly the path booking_service.py's dead-browser recovery
        exercises on every mid-scan Playwright crash)."""
        try:
            storage_state_path = self._storage_state_path()
            if storage_state_path and self._context:
                import os
                os.makedirs(os.path.dirname(storage_state_path), exist_ok=True)
                await self._context.storage_state(path=storage_state_path)
                logger.info("browser.session_saved", cruise_line=self.cruise_line.value)
        except Exception as e:
            logger.warning("browser.session_save_error", error=str(e))

        # Let in-flight response-capture tasks (see track_background_task)
        # finish writing before their JSONL handles/browser get closed out
        # from under them — bounded wait so a stuck task can't hang
        # shutdown forever.
        if self._background_tasks:
            try:
                await asyncio.wait(list(self._background_tasks), timeout=5)
            except Exception as e:
                logger.warning("browser.background_task_wait_error", error=str(e))

        # CONFIRMED REAL BUG, fixed 2026-08-13: these three calls used to
        # share ONE try/except — if context.close() raised (most likely
        # exactly when the browser/context is already dead, which is the
        # dead-browser recovery path's whole reason for calling stop()
        # at all), browser.close()/playwright.stop() were skipped
        # entirely, leaking the Chromium process and the Playwright
        # driver subprocess. Each step now fails on its own.
        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            logger.warning("browser.context_close_error", error=str(e))
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.warning("browser.browser_close_error", error=str(e))
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("browser.playwright_stop_error", error=str(e))
        finally:
            for handle in self._jsonl_handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._jsonl_handles = {}
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

    @property
    def is_alive(self) -> bool:
        """Whether the underlying browser/page is actually still usable.

        A scraper object can outlive its browser: a mid-scrape Playwright
        crash (e.g. "Target page, context or browser has been closed",
        see BookingService._is_dead_browser_error) leaves _page/_browser
        set to closed objects rather than clearing them. Checking for
        that here — not just "was start() ever called" — is what
        BookingService.has_live_session relies on to decide whether the
        GUI's Start button can safely reuse this session.
        """
        return (
            self._page is not None
            and not self._page.is_closed()
            and self._browser is not None
            and self._browser.is_connected()
        )

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
        """Append one JSON entry as a line to raw_dump_dir/filename.

        Reuses one persistent open file handle per path instead of
        open()+close() on every call (see _jsonl_handles) — same content
        written, just without reopening the file on every single append.
        Flushed immediately so a mid-scan crash doesn't lose buffered
        lines (the previous open/close-per-write behavior was durable in
        the same way, so this preserves that guarantee)."""
        if not self.raw_dump_dir:
            return
        import json
        import os

        os.makedirs(self.raw_dump_dir, exist_ok=True)
        path = os.path.join(self.raw_dump_dir, filename)
        handle = self._jsonl_handles.get(path)
        if handle is None or handle.closed:
            handle = open(path, "a", encoding="utf-8")
            self._jsonl_handles[path] = handle
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        handle.flush()

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
        base = f"{_sanitize_filename_component(booking_id)}__{_sanitize_filename_component(step)}__{stamp}"

        try:
            html = await self.page.content()
            await asyncio.to_thread(_write_text_file, os.path.join(pages_dir, base + ".html"), html)
        except Exception:
            logger.warning("page_snapshot.html_error", booking_id=booking_id, step=step, exc_info=True)

        try:
            structured = await self.page.evaluate(_STRUCTURED_EXTRACT_JS)
            # Compact (no indent) — same fields, same values, just faster
            # to serialize and smaller on disk than pretty-printed JSON;
            # nothing captured is dropped or altered.
            content = json.dumps(structured, ensure_ascii=False)
            await asyncio.to_thread(_write_text_file, os.path.join(pages_dir, base + ".json"), content)
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
        base = f"{_sanitize_filename_component(booking_id)}__{_sanitize_filename_component(step)}__{stamp}"

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
