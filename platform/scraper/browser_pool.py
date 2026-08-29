"""One shared Chromium process, one isolated BrowserContext per cruise line.

WHY THIS EXISTS (added 2026-08-27)
----------------------------------
Before this, every scraper called `chromium.launch()` itself, so scanning
N cruise lines meant N full Chromium processes — each with its own
renderer/GPU/network stack. On the real target machine (4 CPUs, 17 GB)
that is the difference between "the PC stays usable" and "the PC does
not." And it wasn't even reachable: `BookingService` held ONE
`_live_scraper` slot and `get_or_create_scraper()` STOPPED the existing
scraper whenever the requested cruise line differed, so switching lines
killed the previous line's logged-in session. Concurrent multi-line
scanning was architecturally impossible, not merely slow.

A `BrowserContext` is Playwright's isolation primitive — cookies,
localStorage, sessionStorage, cache and permissions are per-context, so
two contexts in ONE browser process are as isolated as two incognito
profiles. That is exactly the property needed here: three different
travel-agent accounts on three different portals must never see each
other's session. Each context is created from that cruise line's OWN
`storage_state_<LINE>.json` (this project already wrote those per line —
see BaseScraper._storage_state_path), so isolation is preserved across
restarts too, not just within a run.

THE HEADLESS CONSTRAINT — READ BEFORE CHANGING ANYTHING HERE
------------------------------------------------------------
`headless` is a **launch-level** flag in Playwright. It cannot vary per
context. ESPRESSO must NEVER run headless (Akamai bot detection; enforced
in BaseScraper.start and pinned throughout this project's docs). So the
moment ESPRESSO shares this pool, **the entire shared browser must be
headed** — there is no way to give ESPRESSO a headed context and NCL a
headless one inside one process. This class therefore resolves headless
to False if ESPRESSO is (or may be) among its tenants, and says so out
loud in the log rather than silently downgrading. If you ever need a
genuinely headless line, give it its OWN pool instance rather than
weakening this.

DELIBERATELY NOT DONE
---------------------
This does not pool *pages* or reuse one context across cruise lines. Both
would save a little memory and both would risk exactly the session bleed
this class exists to prevent. Contexts are cheap relative to processes;
that's the trade being made.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from playwright.async_api import Browser, BrowserContext, async_playwright

from config.settings import settings
from core.models import CruiseLine
from utils.logging import get_logger

logger = get_logger(__name__)


class SharedBrowserPool:
    """Owns one Playwright driver + one Chromium process, and hands out
    one isolated BrowserContext per cruise line.

    Not a general-purpose pool: contexts are keyed BY CRUISE LINE on
    purpose, so a given line always gets its own and can never be handed
    another line's session by accident.
    """

    def __init__(self, headless: bool | None = None) -> None:
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: dict[CruiseLine, BrowserContext] = {}
        self._requested_headless = headless
        self._resolved_headless: bool | None = None
        # Guards launch and per-line context creation. Without it, two
        # coroutines starting different cruise lines at the same moment
        # could both see "no browser yet" and launch two Chromiums —
        # exactly the thing this class exists to prevent.
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    def _resolve_headless(self) -> bool:
        """Headed wins. See THE HEADLESS CONSTRAINT in the module docstring.

        A shared browser is shared by definition — we cannot know that
        ESPRESSO will never be asked for later, and switching a running
        browser's headless mode is impossible. So a shared pool is headed,
        full stop. This is a deliberate cost: it's the price of not
        running three Chromium processes.
        """
        requested = settings.browser_headless if self._requested_headless is None else self._requested_headless
        if requested:
            logger.info(
                "browser_pool.headless_overridden_to_headed",
                note="a SHARED browser must be headed because ESPRESSO can never run "
                     "headless and `headless` is a launch-level flag, not per-context",
            )
        return False

    async def start(self) -> None:
        """Launch the shared browser. Idempotent and safe to call from
        multiple coroutines."""
        async with self._lock:
            if self.is_alive:
                return
            self._playwright = await async_playwright().start()
            try:
                self._resolved_headless = self._resolve_headless()
                launch_args: dict = {"headless": self._resolved_headless}
                if settings.proxy_url:
                    launch_args["proxy"] = {"server": settings.proxy_url}
                    if settings.proxy_username:
                        launch_args["proxy"]["username"] = settings.proxy_username
                        launch_args["proxy"]["password"] = settings.proxy_password
                self._browser = await self._playwright.chromium.launch(**launch_args)
                logger.info(
                    "browser_pool.started",
                    headless=self._resolved_headless,
                    pid=getattr(self._browser, "_impl_obj", None) and "chromium",
                )
            except Exception:
                # Same cleanup discipline as BaseScraper.start's own fix
                # (2026-08-13): never leak the driver subprocess when the
                # launch itself fails.
                logger.error("browser_pool.start_failed_cleaning_up")
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
                self._browser = None
                raise

    async def acquire_context(
        self, cruise_line: CruiseLine, market: str | None = None,
    ) -> BrowserContext:
        """Get (creating if needed) this cruise line's OWN isolated context.

        Loads that line's saved `storage_state_<LINE>.json` if present, so
        a previously-completed login is restored into a context that no
        other cruise line can see.
        """
        # FAIL LOUDLY rather than leak an account. Contexts and session
        # files here are keyed by cruise_line ONLY, but NCL has per-market
        # accounts (US/CA) as of 2026-08-27. Handing a CA caller the US
        # session would silently check Canadian bookings against the wrong
        # account and report them all "Reservation is not found" — the very
        # bug that motivated per-market sessions. See _storage_state_path.
        if market is not None:
            wanted = str(market).upper()
            if wanted != settings.ncl_default_market.upper():
                raise ValueError(
                    f"SharedBrowserPool cannot isolate NCL market {wanted!r}: "
                    f"contexts and storage_state files here are keyed by "
                    f"cruise line only, so this would reuse the "
                    f"{settings.ncl_default_market} session. Use a standalone "
                    f"NclScraper(market={wanted!r}) until the pool is made "
                    f"market-aware."
                )

        if not self.is_alive:
            await self.start()

        async with self._lock:
            existing = self._contexts.get(cruise_line)
            if existing is not None:
                return existing

            context_args: dict = {}
            state_path = self._storage_state_path(cruise_line)
            if state_path and os.path.exists(state_path):
                context_args["storage_state"] = state_path

            context = await self._browser.new_context(**context_args)
            context.set_default_timeout(settings.scraper_timeout_ms)
            self._contexts[cruise_line] = context
            logger.info(
                "browser_pool.context_created",
                cruise_line=cruise_line.value,
                restored_session=bool(context_args),
                live_contexts=len(self._contexts),
            )
            return context

    async def release_context(self, cruise_line: CruiseLine, save_state: bool = True) -> None:
        """Save this line's session and close ONLY its context.

        The shared browser and every other cruise line's context stay
        untouched — that's the whole point: one line finishing (or
        crashing) must not disturb the others.
        """
        async with self._lock:
            context = self._contexts.pop(cruise_line, None)
        if context is None:
            return

        if save_state:
            try:
                state_path = self._storage_state_path(cruise_line)
                if state_path:
                    os.makedirs(os.path.dirname(state_path), exist_ok=True)
                    await context.storage_state(path=state_path)
                    logger.info("browser_pool.session_saved", cruise_line=cruise_line.value)
            except Exception as e:
                logger.warning(
                    "browser_pool.session_save_failed",
                    cruise_line=cruise_line.value, error=str(e),
                )
        try:
            await context.close()
        except Exception as e:
            logger.warning(
                "browser_pool.context_close_failed",
                cruise_line=cruise_line.value, error=str(e),
            )
        logger.info("browser_pool.context_released", cruise_line=cruise_line.value)

    async def recycle_context(self, cruise_line: CruiseLine) -> BrowserContext:
        """Throw away a stuck/dead context and build a fresh one.

        Recovery path for "this one cruise line's session is wedged" —
        deliberately does NOT restart the shared browser, so the other
        lines keep working. Does NOT save state first: a wedged context's
        state is exactly what we don't want to persist.
        """
        logger.warning("browser_pool.recycling_context", cruise_line=cruise_line.value)
        await self.release_context(cruise_line, save_state=False)
        return await self.acquire_context(cruise_line)

    async def close(self) -> None:
        """Save every live session, then tear down the whole pool."""
        for cruise_line in list(self._contexts.keys()):
            await self.release_context(cruise_line, save_state=True)
        # Each step independently guarded — same lesson as BaseScraper.stop's
        # 2026-08-13 fix: a failure closing the browser must never skip
        # stopping the driver subprocess.
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as e:
            logger.warning("browser_pool.browser_close_failed", error=str(e))
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("browser_pool.playwright_stop_failed", error=str(e))
        self._browser = None
        self._playwright = None
        self._contexts = {}
        logger.info("browser_pool.closed")

    # ── introspection (used by the GUI) ──────────────────────────

    def live_cruise_lines(self) -> list[str]:
        return sorted(cl.value for cl in self._contexts)

    @property
    def context_count(self) -> int:
        return len(self._contexts)

    @staticmethod
    def _storage_state_path(cruise_line: CruiseLine) -> Optional[str]:
        """Per-cruise-line session file — byte-identical convention to
        BaseScraper._storage_state_path, so a session saved by either the
        pooled or the standalone path is picked up by the other.

        KNOWN LIMITATION, recorded 2026-08-27: this is per-cruise-LINE
        only, while NCL now has per-MARKET accounts and per-market session
        files (`storage_state_NCL.json` for US, `storage_state_NCL_ca.json`
        for Canada — see NclScraper._storage_state_path). Contexts here are
        also keyed by cruise_line alone, so a pooled NCL run would load the
        US session no matter which market was asked for, and a US and a CA
        scraper would SHARE one context. That is a cross-account session
        leak, which is exactly what this class exists to prevent.

        Not reachable today (nothing outside MultiLineCoordinator builds a
        pool, and the coordinator is not wired into the GUI or CLI), so this
        is a latent trap rather than a live bug — but `acquire_context` now
        refuses a non-default NCL market outright instead of silently
        handing back the wrong account's session.
        """
        base = settings.browser_user_data_dir
        if not base:
            return None
        return os.path.join(base, f"storage_state_{cruise_line.value}.json")
