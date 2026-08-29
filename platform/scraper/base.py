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


# Signatures that mean the browser PROCESS or its CDP transport is gone —
# not that one page load failed. See is_dead_browser_error for why
# `net::ERR_*` is excluded from this list.
_DEAD_TRANSPORT_SIGNATURES = (
    "has been closed",
    "target closed",
    "crash",
    "protocol error",      # CDP protocol gone
    "websocket",           # the CDP websocket dropped
    "connection closed",
    "econnrefused",        # cannot reach the browser at all
    "browser closed",
    "browser has disconnected",
)


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
    fail identically with no self-healing restart.

    WIDENED 2026-08-27 (forensic review) after checking this against the
    transient-error set that Playwright/Browserless production guidance
    names for connection retry. Five real dead-transport signatures were
    NOT matched: "protocol error", "websocket", "connection closed",
    "econnrefused", and "browser closed". When the browser dies with one of
    those, BookingService's restart path never fires and EVERY remaining
    booking in the batch fails one at a time against a corpse — which is
    the shape of the cascading-failure runs already seen on both lines.

    DELIBERATELY STILL EXCLUDED: page-level network errors, i.e. anything
    matching `net::ERR_*` (ERR_CONNECTION_RESET, ERR_NAME_NOT_RESOLVED,
    ERR_INTERNET_DISCONNECTED...). Those mean ONE navigation failed while
    the browser is perfectly healthy. Treating them as a dead browser would
    trigger a restart, and on ESPRESSO a single close-and-reopen is enough
    to break the session outright (DOCUMENTATION.md section L) — so a
    misclassification here does real damage rather than merely wasting
    time. A transient page error must stay an ordinary per-booking failure
    handled by the existing retry, not a browser teardown.
    """
    msg = str(exc).lower()
    # Page-level network failure: browser is alive, this navigation is not.
    if "net::err" in msg:
        return False
    return any(s in msg for s in _DEAD_TRANSPORT_SIGNATURES)


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
        # Structure-drift checks (see check_structure_drift) only need to
        # run once per browser session, not once per booking — the page
        # layout doesn't change between bookings within the same run.
        # Tracks which `name`s have already been checked this session.
        self._structure_checked: set[str] = set()

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

        # ADDED 2026-08-26, for the first live NCL run: when set, wraps
        # the whole session in Playwright's own built-in tracing (no new
        # dependency) — a single .zip file capturing a full DOM/network/
        # console/screenshot timeline, viewable afterward with
        # `playwright show-trace <path>` without needing another live
        # session against the real portal to diagnose a failure. Off by
        # default (None) — zero behavior change for every existing
        # caller/cruise line unless explicitly opted into.
        self.trace_path: Optional[str] = None

        # ADDED 2026-08-27 — concurrent multi-cruise-line scanning.
        # When set to a SharedBrowserPool, start() attaches to that pool's
        # per-cruise-line isolated BrowserContext instead of launching its
        # OWN Chromium process, and stop() releases just that context
        # rather than tearing the browser down. This is what makes
        # ESPRESSO + MSC + NCL run at once without three full browser
        # processes (see scraper/browser_pool.py for the isolation and
        # headless-constraint rationale).
        #
        # None = the original standalone behavior, completely unchanged:
        # this scraper owns its own playwright driver + browser + context.
        # Every existing caller (CLI, the MSC subsystem, one-shot scans)
        # keeps that path untouched.
        self._pool = None  # type: ignore[var-annotated]
        self._owns_browser: bool = True

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

    def attach_pool(self, pool) -> None:
        """Use a SharedBrowserPool's isolated context instead of launching
        our own browser. Must be called BEFORE start().

        See the `_pool` note in __init__ for why. Kept as an explicit
        opt-in method rather than a constructor arg so every existing
        `EspressoScraper()` / `NclScraper()` / `GoCCLScraper()` call site
        keeps working with zero changes.
        """
        self._pool = pool
        self._owns_browser = False

    async def start(self, headless: Optional[bool] = None) -> None:
        """Launch the browser and create a page.

        When a SharedBrowserPool is attached (see attach_pool), no browser
        is launched here at all — this scraper gets that pool's isolated
        per-cruise-line context and only creates its own page in it. The
        `headless` argument is then ignored, because headless is a
        launch-level flag owned by the pool (a shared browser is always
        headed; ESPRESSO can never be headless).

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
        # POOLED PATH (2026-08-27): attach to the shared browser's isolated
        # context for this cruise line. Deliberately returns early — none
        # of the launch/proxy/storage_state logic below applies, because
        # the pool already owns all of it (including loading this line's
        # own storage_state, which is why session isolation is preserved).
        if self._pool is not None:
            self._context = await self._pool.acquire_context(self.cruise_line)
            self._browser = getattr(self._pool, "_browser", None)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(settings.scraper_timeout_ms)
            if self.capture_everything:
                def _on_response_pooled(r):
                    track_background_task(
                        self._background_tasks, asyncio.create_task(self._capture_response(r)),
                    )
                self._page.on("response", _on_response_pooled)
            logger.info(
                "browser.started_pooled",
                cruise_line=self.cruise_line.value,
                note="using shared browser's isolated context — no separate Chromium launched",
            )
            return

        self._playwright = await async_playwright().start()
        try:
            resolved_headless = settings.browser_headless if headless is None else headless

            # PINNED, confirmed 2026-08-14: ESPRESSO (secure.cruisingpower.com)
            # never works headless — its Akamai bot-detection reliably blocks
            # or breaks headless sessions. This used to be a bug reachable
            # from every entry point (CLI --headless, easy_menu.py's default
            # "just press Enter" answer, and any GUI scan that didn't
            # explicitly pop a visible window) — any of those would silently
            # launch ESPRESSO headless and produce broken/failed scans.
            # Enforced here, in the one place every scraper subclass launches
            # its browser through, so no caller can accidentally bypass it.
            if self.cruise_line == CruiseLine.ESPRESSO and resolved_headless:
                logger.warning(
                    "browser.espresso_headless_forced_visible",
                    note="cruisingpower.com never works headless — overriding to a visible window",
                )
                resolved_headless = False

            launch_args: dict = {
                "headless": resolved_headless,
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

            if self.trace_path:
                await self._context.tracing.start(screenshots=True, snapshots=True, sources=True)
                logger.info("browser.tracing_started", cruise_line=self.cruise_line.value, path=self.trace_path)

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
        # POOLED PATH (2026-08-27): the pool owns the browser, the context
        # and this cruise line's storage_state. Closing only OUR page here
        # is essential — calling the standalone teardown below would close
        # the shared browser and kill every OTHER cruise line's live,
        # logged-in session mid-scan. Session saving and context lifetime
        # are the pool's job (see SharedBrowserPool.release_context).
        if self._pool is not None:
            if self._background_tasks:
                try:
                    await asyncio.wait(list(self._background_tasks), timeout=5)
                except Exception as e:
                    logger.warning("browser.background_task_wait_error", error=str(e))
            try:
                if self._page is not None and not self._page.is_closed():
                    await self._page.close()
            except Exception as e:
                logger.warning("browser.pooled_page_close_error", error=str(e))
            for handle in self._jsonl_handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._jsonl_handles = {}
            self._page = None
            self._context = None
            self._browser = None
            logger.info("browser.stopped_pooled", cruise_line=self.cruise_line.value)
            return

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
        # Tracing must be stopped/saved BEFORE the context closes — kept
        # as its own try/except so a tracing failure can never skip the
        # context/browser/playwright cleanup below (same lesson as the
        # 2026-08-13 fix on the three calls right after this one).
        if self.trace_path and self._context:
            try:
                import os
                os.makedirs(os.path.dirname(self.trace_path) or ".", exist_ok=True)
                await self._context.tracing.stop(path=self.trace_path)
                logger.info("browser.tracing_saved", cruise_line=self.cruise_line.value, path=self.trace_path)
            except Exception as e:
                logger.warning("browser.tracing_stop_error", error=str(e))

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

    async def navigate(self, url: str, wait_until: str = "domcontentloaded", attempts: int = 3) -> None:
        """Navigate to a URL and wait for load, retrying a transient hang.

        ADDED 2026-08-26 at the project owner's report that "sometimes the
        website hangs and gives errors" (NCL specifically, but this
        applies to every portal here — ESPRESSO's own error history in
        DOCUMENTATION.md includes isolated `Failed to fetch`/`ERR_ABORTED`
        navigation blips that cascaded into the NEXT booking also failing).

        A page navigation is a GET — idempotent — so retrying one is safe,
        unlike the mutating confirm/cancel-edit clicks, which must NEVER
        be retried (a retried submit could double-submit against a real
        booking). That distinction is the whole reason this retry lives
        here on `navigate` rather than being wrapped around wider flows.

        A dead browser is re-raised immediately without burning retries —
        BookingService's restart path can only trigger on an exception
        that actually propagates, and retrying a closed browser just
        delays recovery (same lesson as the 2026-08-13 fixes in
        ncl.py/goccl.py's own exception handling).
        """
        last_error: Exception | None = None
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                logger.debug("navigate", url=url, attempt=attempt)
                await self.page.goto(url, wait_until=wait_until)
                if attempt > 1:
                    logger.info("browser.navigate_recovered", url=url, attempt=attempt)
                return
            except Exception as e:
                if is_dead_browser_error(e):
                    raise
                last_error = e
                if attempt < attempts:
                    logger.warning(
                        "browser.navigate_retry", url=url, attempt=attempt,
                        of=attempts, error=str(e)[:200],
                    )
                    await asyncio.sleep(delay)
                    delay *= 1.5
        logger.error("browser.navigate_failed", url=url, attempts=attempts, error=str(last_error)[:200])
        raise last_error  # type: ignore[misc]

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

    # Persistent ACROSS runs (unlike raw_dump_dir, which is per-scan) --
    # a baseline needs to survive to be compared against every future
    # run, not just this one.
    STRUCTURE_BASELINE_DIR = "data/structure_baselines"

    async def check_structure_drift(self, name: str, selector: str = "body") -> dict:
        """Capture an ARIA-accessibility-tree snapshot of `selector` and
        compare it against a saved baseline for `name`. On the first call
        for a given name, saves the current snapshot AS the baseline and
        reports "baseline_created" — there's nothing to compare against
        yet. Never raises and never blocks/fails a scan; purely an early-
        warning signal, logged so it shows up without needing to be
        actively watched for.

        ADDED 2026-08-25, motivated by a real, recurring incident class:
        ESPRESSO's search form was rebuilt on Mantine at some point,
        silently breaking the old `#reservationid`/`#searchReservationBtn`
        selectors (see EspressoScraper._SEARCH_INPUT_SELECTOR's docstring
        — both the old ID selector and the new `data-qa` one are now tried
        together as a result, discovered only after the fact). An ARIA
        snapshot is based on role + accessible name, which tends to
        survive exactly the kind of framework rewrite that breaks CSS
        selectors/ids — this won't fix a break by itself, but it's a free
        (no LLM, no extra dependency — aria_snapshot() is a built-in
        Playwright method) way to notice a structural change is coming
        before a live scan mysteriously starts failing.

        Chosen granularity deliberately: pass a `selector` scoped to a
        stable container (e.g. the search form), not the whole page body
        by default in practice — the page body includes booking-specific
        content that differs on every single call and would never
        register as "unchanged," making the comparison useless."""
        import os

        if name in self._structure_checked:
            return {"status": "skipped_already_checked_this_session"}
        self._structure_checked.add(name)

        try:
            # `.first` is REQUIRED, not defensive. CONFIRMED BUG, found
            # 2026-08-27 from a real run: this project deliberately uses
            # comma-OR selectors so a portal redesign can't break a
            # scrape (e.g. EspressoScraper._SEARCH_BUTTON_SELECTOR is
            # '#searchReservationBtn, [aria-label="Search by Reservation
            # ID, Name or Date"]' — see its docstring). `page.click(sel)`
            # is NON-strict and just clicks the first match, so the scrape
            # works fine — but `locator(sel).aria_snapshot()` IS strict and
            # raises a strict-mode violation the moment two elements
            # match. That exception landed here, got logged as a warning
            # and swallowed (this method never blocks a scan by design),
            # so `espresso_search_button.yaml` was NEVER created while
            # `espresso_search_input.yaml` was — the drift alarm silently
            # covered only half of what it was wired to watch, and nothing
            # surfaced that because "capture_failed" looks like ordinary
            # noise. Snapshotting the first match is the correct behaviour
            # here: it's the same element page.click would act on.
            snapshot = await self.page.locator(selector).first.aria_snapshot()
        except Exception as e:
            # Escalated from warning to error: a capture failure means this
            # baseline is not being watched AT ALL, which is exactly the
            # silent-coverage-gap that hid the bug above.
            logger.error("structure_watch.capture_failed", name=name, selector=selector, error=str(e))
            self.log_action("structure_watch_capture_failed", name=name, error=str(e))
            return {"status": "capture_failed", "error": str(e)}

        baseline_dir = self.STRUCTURE_BASELINE_DIR
        os.makedirs(baseline_dir, exist_ok=True)
        baseline_path = os.path.join(baseline_dir, f"{_sanitize_filename_component(name)}.yaml")

        if not os.path.exists(baseline_path):
            await asyncio.to_thread(_write_text_file, baseline_path, snapshot)
            logger.info("structure_watch.baseline_created", name=name, path=baseline_path)
            return {"status": "baseline_created", "path": baseline_path}

        with open(baseline_path, encoding="utf-8") as f:
            baseline = f.read()

        if snapshot.strip() == baseline.strip():
            return {"status": "unchanged"}

        logger.warning(
            "structure_watch.structure_changed", name=name, path=baseline_path,
            note="page structure differs from the saved baseline -- a portal redesign may have broken a selector",
        )
        self.log_action("structure_changed", name=name, path=baseline_path)
        return {"status": "changed", "baseline": baseline, "current": snapshot}

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
