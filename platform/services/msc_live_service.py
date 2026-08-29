"""In-process MSC automation service — connects msc_commands.py's fully
automated check_booking flow to the desktop GUI.

MSC is deliberately NOT driven through BaseScraper/BookingService (see
config/settings.py's "MSC" section and services/booking_service.py's
_get_scraper, which raises rather than silently misrouting MSC through
ESPRESSO's automation) — MscBookingResult's shape (three-to-four
independent opportunity checks per booking) doesn't fit the single
old_total/new_total/net_saving shape every other cruise line uses, and a
standalone scraper/msc.py was a deliberate non-goal. This service instead
wraps the SAME automated flow msc_session_controller.py's console script
exposes (msc_commands._check_booking_msc: lookup -> stage -> confirm ->
harvest -> evaluate, zero human clicks) with its own persistent, in-process
Playwright browser, so the GUI can drive it directly without needing that
separate script + its file-based command.txt/result.txt protocol running
alongside it.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import random
import time
from dataclasses import dataclass
from typing import Callable

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import msc_commands
from config.settings import settings
from core.models import MscBookingResult
from utils.logging import get_logger

logger = get_logger(__name__)

# Kept alongside the other cruise lines' per-line storage_state files
# (see scraper/base.py's _storage_state_path — "storage_state_{cruise_line}.json")
# and byte-identical to msc_session_controller.py's own STORAGE_STATE_PATH,
# so a session logged in through either path is usable by the other.
STORAGE_STATE_PATH = os.path.join(settings.browser_user_data_dir, "storage_state_MSC.json")


@dataclass
class MscCheckOutcome:
    """One booking's outcome from a GUI-driven MSC batch — either a full
    MscBookingResult (status == "checked") or a short-circuit status from
    _check_booking_msc (not_found, cancelled, session_expired_after_relogin,
    etc.) with no result to show."""

    booking_id: str
    status: str
    result: MscBookingResult | None = None
    note: str = ""


def _summarize_outcome(outcome: dict) -> str:
    status = outcome.get("status")
    if status != "checked":
        return status.replace("_", " ")
    result: MscBookingResult = outcome["result"]
    if not result.has_any_opportunity:
        return "no opportunity found"
    hits = [c.type.value for c in result.checks if c.status.value == "OPPORTUNITY"]
    return "opportunity: " + ", ".join(hits)


class MscLiveService:
    """Manages one persistent, in-process MSC browser session and runs
    bookings through msc_commands._check_booking_msc sequentially — the
    GUI equivalent of msc_session_controller.py's main loop, minus the
    file-based command protocol (calls the same underlying function
    directly instead)."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._state: dict = {}
        self._running = False
        self._stop_requested = False
        # Single-instance guard (see ensure_started). Held for the
        # lifetime of the live MSC session and released in stop().
        self._guard = None
        self._holds_guard = False

    @property
    def is_alive(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def is_running(self) -> bool:
        return self._running

    async def ensure_started(self) -> None:
        """Launch the persistent browser if one isn't already open. Always
        visible (headless=False), matching msc_session_controller.py —
        MSC's login/relogin flow has only ever been exercised against a
        real, visible window; never made hidden here."""
        if self.is_alive:
            return

        # SESSION-SAFETY GUARD, added 2026-08-27 — deliberately the SAME
        # lock scope ("msc_driver") that msc_session_controller.py takes.
        # These two are alternative front-ends to the same MSC portal
        # session and the same storage_state_MSC.json, so only ONE may run
        # at a time. Sharing the scope name is what makes the GUI refuse to
        # start while the console controller holds it, and vice versa.
        #
        # Acquired BEFORE launching the browser so a refusal costs nothing.
        from services.resource_governor import SingleInstanceGuard

        if self._guard is None:
            self._guard = SingleInstanceGuard("msc_driver")
        if not self._guard.acquire():
            holder = self._guard.holder_pid() or "unknown"
            raise RuntimeError(
                "Another MSC driver is already running "
                f"({holder}) — most likely msc_session_controller.py or another "
                "GUI instance. Two MSC drivers fight over the same portal "
                "session (MSC allows one active login per account) and can "
                "clobber each other's saved session. Close the other one first."
            )
        self._holds_guard = True

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=False)
        context_args: dict = {}
        if os.path.exists(STORAGE_STATE_PATH):
            context_args["storage_state"] = STORAGE_STATE_PATH
        self._context = await self._browser.new_context(**context_args)
        self._page = await self._context.new_page()
        self._state = {"context": self._context, "page": self._page, "pages": [self._page]}
        logger.info("msc_live.browser_started", restored_session=bool(context_args))

    async def stop(self) -> None:
        """Close the browser, saving the session first — mirrors
        BookingService.close_live_scraper for the other cruise lines."""
        if self._context is not None:
            try:
                os.makedirs(os.path.dirname(STORAGE_STATE_PATH), exist_ok=True)
                await self._context.storage_state(path=STORAGE_STATE_PATH)
            except Exception:
                logger.exception("msc_live.storage_state_save_failed")
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        # Release the MSC driver lock LAST — only after the browser and
        # session are actually torn down, so another driver can't start
        # while this one is still holding the portal session open.
        if self._guard is not None and self._holds_guard:
            self._guard.release()
            self._holds_guard = False
        self._state = {}

    async def check_login(self, timeout_minutes: float = 15.0) -> bool:
        """Try automatic login (Windows Credential Manager creds, same as
        msc_session_controller.py's phase 1); if that doesn't complete
        cleanly, leave the visible window open and poll for a human to
        finish logging in by hand, same polling shape as
        BookingService.check_login uses for the other cruise lines."""
        await self.ensure_started()
        importlib.reload(msc_commands)  # see run_batch's comment on why
        login_result = await msc_commands.auto_login(self._page)
        if login_result == "OK":
            logger.info("msc_live.auto_login_ok")
            return True

        logger.info("msc_live.auto_login_incomplete", result=login_result)
        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            try:
                body = await self._page.inner_text("body")
            except Exception:
                continue
            if "SIGN OUT" in body:
                return True
        return False

    def stop_processing(self) -> bool:
        if not self._running:
            return False
        self._stop_requested = True
        return True

    async def run_batch(
        self,
        booking_ids: list[str],
        on_result: Callable[[MscCheckOutcome], None] | None = None,
        on_progress: Callable[[str, int, int], None] | None = None,
    ) -> list[MscCheckOutcome]:
        """Check each booking in order through the exact same automated
        function (msc_commands._check_booking_msc) the check_booking/
        check_booking_batch console commands use — data collection
        (lookup/stage/confirm/harvest) and calculation (evaluate_msc_booking)
        both happen inside that one call, with no human click in between."""
        if self._running:
            raise RuntimeError("MSC queue is already running")
        if not self.is_alive:
            raise RuntimeError('No live MSC session — click "Check login" first')

        self._running = True
        self._stop_requested = False
        outcomes: list[MscCheckOutcome] = []
        try:
            for i, booking_id in enumerate(booking_ids):
                if self._stop_requested:
                    break
                if on_progress:
                    on_progress(booking_id, i, len(booking_ids))
                try:
                    # CONFIRMED REAL BUG, 2026-08-24: unlike
                    # msc_session_controller.py's console loop (which
                    # reloads msc_commands before every single command),
                    # nothing here ever refreshed this module — a GUI
                    # process left running across a code fix silently kept
                    # executing the OLD msc_commands.py from whenever it
                    # was first imported, while _check_booking_msc's own
                    # internal reload of core.models/core.calculator_msc
                    # (see msc_commands.py) picked up the NEW calculator.
                    # That mismatch is exactly what broke a real GUI
                    # session left open across the senior-discount fix
                    # (2026-08-18): the stale _check_booking_msc still
                    # called evaluate_msc_booking(all_seniors=...), a
                    # keyword the freshly-reloaded function no longer
                    # accepted, so every booking in that run silently
                    # errored instead of getting a real (or fixed) answer.
                    # Reloading here, every booking, closes the gap the
                    # same way the console flow already does — a running
                    # GUI now needs zero restarts to pick up an
                    # msc_commands.py fix.
                    importlib.reload(msc_commands)
                    outcome_dict = await msc_commands._check_booking_msc(self._state, booking_id)
                except Exception as e:
                    logger.error("msc_live.check_failed", booking_id=booking_id, error=str(e))
                    outcome = MscCheckOutcome(booking_id=booking_id, status="error", note=str(e))
                else:
                    outcome = MscCheckOutcome(
                        booking_id=booking_id,
                        status=outcome_dict.get("status", "unknown"),
                        result=outcome_dict.get("result"),
                        note=_summarize_outcome(outcome_dict),
                    )
                outcomes.append(outcome)
                if on_result:
                    on_result(outcome)
                # Same randomized inter-booking pacing as the other cruise
                # lines (see BookingService._run_batch) — MSC's own backend
                # has shown session/token degradation under fast back-to-back
                # requests too.
                if i < len(booking_ids) - 1 and not self._stop_requested:
                    await asyncio.sleep(random.uniform(
                        settings.scraper_interbooking_delay_min_s,
                        settings.scraper_interbooking_delay_max_s,
                    ))
        finally:
            self._running = False
            self._stop_requested = False
        return outcomes
