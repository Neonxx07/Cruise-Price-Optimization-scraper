"""Runs ESPRESSO + MSC + NCL concurrently over ONE shared browser.

WHAT THIS SOLVES
----------------
Before this, concurrent multi-line scanning was impossible, not just
slow: `BookingService` held a single `_live_scraper` slot and
`get_or_create_scraper()` STOPPED the existing scraper whenever the
requested cruise line differed — so switching lines killed the previous
line's logged-in session. And each scraper launched its own full Chromium
process, which on a 4-core machine is the difference between "usable PC"
and "not."

DESIGN, AND WHY EACH PIECE IS THERE
-----------------------------------
- **One shared Chromium, one isolated BrowserContext per cruise line**
  (`scraper/browser_pool.py`). Contexts are Playwright's isolation
  primitive — cookies/localStorage/cache are per-context — so three
  travel-agent accounts never see each other's session. Verified
  empirically, not assumed.
- **A global concurrency semaphore** bounding bookings in flight across
  ALL lines (`settings.max_concurrent_bookings`), plus a per-line
  semaphore so one line can't hog every slot. Two-tier bounding is the
  shape scrapy-playwright uses (`max_contexts` + `max_pages_per_context`)
  and it's the right one here.
- **Per-line queues, not unlimited pages.** Each line drains its own
  booking list one at a time; the global semaphore decides how many lines
  are actually moving at any instant.
- **A resource gate** (`services/resource_governor.py`) that every worker
  awaits before starting a booking, so load — not just a fixed number —
  ultimately controls pace.
- **Single-instance guard** so a second coordinator can't fight the first
  over the same portal sessions.
- **Per-line failure isolation.** A crashed/stuck line has ITS context
  recycled; the shared browser and the other lines keep going. Only a
  whole-browser disconnect stops everything, and that's reported plainly
  rather than hanging.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not touch any calculator, any scraper's per-booking logic, or any
result/DB/export path. Every booking still goes through the exact same
`scraper.check_booking(...)` and the exact same calculators. This module
only decides WHEN and WITH WHAT BROWSER a booking runs — so no
optimization/calculation behavior can change as a result of it.

MSC NOTE
--------
MSC is not a `BaseScraper` (it's the separate `msc_commands.py`
subsystem), so it is not wired into the pooled scraper path here yet.
`register_line` accepts any callable worker, which is how MSC can be
added without this module needing to know anything about it — but that
wiring is not done, and is called out as remaining work rather than
pretended.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional

from config.settings import settings
from core.models import BookingResult, CruiseLine
from scraper.browser_pool import SharedBrowserPool
from services.resource_governor import ResourceGovernor, SingleInstanceGuard
from utils.logging import get_logger

logger = get_logger(__name__)


class LineState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass
class LineStatus:
    """Everything the GUI needs to show about ONE cruise line.

    Deliberately a plain snapshot object: the GUI reads this rather than
    inferring state from its own local assumptions, which is how the
    previous GUI drifted out of sync with the backend.
    """
    cruise_line: str
    state: LineState = LineState.IDLE
    queued: int = 0
    done: int = 0
    errors: int = 0
    total: int = 0
    current_booking_id: Optional[str] = None
    last_error: Optional[str] = None
    last_success_at: Optional[float] = None
    started_at: Optional[float] = None
    context_recycles: int = 0

    @property
    def progress_pct(self) -> float:
        return 0.0 if not self.total else round(100.0 * (self.done + self.errors) / self.total, 1)

    def as_dict(self) -> dict:
        return {
            "cruise_line": self.cruise_line,
            "state": self.state.value,
            "queued": self.queued,
            "done": self.done,
            "errors": self.errors,
            "total": self.total,
            "progress_pct": self.progress_pct,
            "current_booking_id": self.current_booking_id,
            "last_error": self.last_error,
            "context_recycles": self.context_recycles,
        }


# A worker checks ONE booking and returns its result. Signature kept
# deliberately narrow so MSC (which isn't a BaseScraper) can be plugged
# in later without changing this module.
BookingWorker = Callable[[str], Awaitable[BookingResult]]


class MultiLineCoordinator:
    """Owns the shared browser, the governor, and one queue per line."""

    def __init__(
        self,
        max_concurrent: int | None = None,
        use_single_instance_guard: bool = True,
    ) -> None:
        self.pool = SharedBrowserPool()
        self.governor = ResourceGovernor(
            max_cpu_percent=settings.max_cpu_percent,
            max_ram_percent=settings.max_ram_percent,
            sample_interval_s=settings.resource_sample_interval_s,
        )
        limit = max_concurrent or settings.max_concurrent_bookings
        self._global_sem = asyncio.Semaphore(limit)
        self.max_concurrent = limit

        self._queues: dict[CruiseLine, list[str]] = {}
        self._workers: dict[CruiseLine, BookingWorker] = {}
        self._status: dict[CruiseLine, LineStatus] = {}
        self._results: dict[CruiseLine, list[BookingResult]] = {}
        self._line_sems: dict[CruiseLine, asyncio.Semaphore] = {}
        self._tasks: dict[CruiseLine, asyncio.Task] = {}
        # Pooled scrapers created via make_scraper_worker — held so
        # teardown can close each one's page before the pool closes
        # its context (ordering matters: closing the browser first
        # loses close events and unflushed capture files).
        self._pooled_scrapers: dict[CruiseLine, object] = {}

        # Pause/stop are cooperative and checked between bookings, never
        # mid-booking: interrupting a booking mid-flight is exactly how
        # NCL's 30-minute edit lock gets left held.
        self._pause = asyncio.Event()
        self._pause.set()          # set == running
        self._stop_requested = False
        # Distinguishes an INTENTIONAL teardown from a real crash.
        # Found 2026-08-27 while testing the lock composition: closing
        # the pool in run()'s finally fires browser.on('disconnected'),
        # so every normal shutdown logged a scary 'the SHARED browser
        # died' error and set _stop_requested. Harmless to results (the
        # handler only overwrites RUNNING/PAUSED/STARTING, never DONE)
        # but it's a false alarm in the log, which is exactly the kind
        # of noise that trains an operator to ignore real alarms.
        self._shutting_down = False

        # PER-PORTAL locks, revised 2026-08-27. A single global
        # "multi_line_scanner" lock was wrong: it stopped a second
        # coordinator, but did NOT stop this coordinator from running
        # ESPRESSO at the same time as run_persistent_watchlist_scan.py,
        # or MSC at the same time as msc_session_controller.py — which is
        # the collision that actually evicts a portal session.
        #
        # Instead the coordinator takes ONE lock per cruise line it's
        # about to drive, using the SAME scope names those standalone
        # drivers use ("espresso_driver", "msc_driver", ...). That makes
        # the locks compose: whoever gets the portal first wins, whichever
        # front-end they came from, and two different portals never block
        # each other.
        self._use_guard = use_single_instance_guard
        self._guards: dict[CruiseLine, SingleInstanceGuard] = {}
        self._holds_guard = False

    @staticmethod
    def _guard_scope(cruise_line: CruiseLine) -> str:
        """Lock scope for a cruise line — must match the scope the
        standalone driver for that portal uses, or the locks won't
        compose. See msc_session_controller.py ("msc_driver") and
        run_persistent_watchlist_scan.py ("espresso_driver")."""
        return f"{cruise_line.value.lower()}_driver"

    def _acquire_line_guards(self) -> None:
        """Take one lock per registered cruise line, all-or-nothing.

        All-or-nothing matters: acquiring two of three and then failing
        would leave two portals locked by a coordinator that never runs.
        """
        acquired: list[SingleInstanceGuard] = []
        for cruise_line in self._queues:
            g = SingleInstanceGuard(self._guard_scope(cruise_line))
            if not g.acquire():
                for done in acquired:
                    done.release()
                raise RuntimeError(
                    f"Cannot start: another driver already holds the "
                    f"{cruise_line.value} portal session "
                    f"({g.holder_pid() or 'unknown'}). Close it first — "
                    f"two drivers on one portal evict each other's login."
                )
            acquired.append(g)
            self._guards[cruise_line] = g
        self._holds_guard = True

    def _release_line_guards(self) -> None:
        for g in self._guards.values():
            try:
                g.release()
            except Exception as e:
                logger.warning("coordinator.guard_release_failed", error=str(e))
        self._guards = {}
        self._holds_guard = False

    # ── registration ─────────────────────────────────────────────

    def register_line(
        self, cruise_line: CruiseLine, booking_ids: list[str], worker: BookingWorker,
    ) -> None:
        """Queue a cruise line's bookings with the callable that checks one.

        De-duplicates while preserving order — the same discipline the MSC
        batch path already applies, and worth having here too since a
        duplicate booking means a duplicate real portal visit.
        """
        seen: set[str] = set()
        deduped: list[str] = []
        for b in booking_ids:
            b = b.strip()
            if b and b not in seen:
                seen.add(b)
                deduped.append(b)

        self._queues[cruise_line] = deduped
        self._workers[cruise_line] = worker
        self._results[cruise_line] = []
        self._line_sems[cruise_line] = asyncio.Semaphore(settings.max_pages_per_context)
        self._status[cruise_line] = LineStatus(
            cruise_line=cruise_line.value, total=len(deduped), queued=len(deduped),
        )
        logger.info("coordinator.line_registered", cruise_line=cruise_line.value, bookings=len(deduped))

    # ── control ──────────────────────────────────────────────────

    def pause(self) -> None:
        """Stop starting NEW bookings; lets in-flight ones finish."""
        self._pause.clear()
        for st in self._status.values():
            if st.state == LineState.RUNNING:
                st.state = LineState.PAUSED
        logger.info("coordinator.paused")

    def resume(self) -> None:
        self._pause.set()
        for st in self._status.values():
            if st.state == LineState.PAUSED:
                st.state = LineState.RUNNING
        logger.info("coordinator.resumed")

    def request_stop(self) -> None:
        """Cooperative stop — checked between bookings so an in-flight
        booking always completes its own cleanup (releasing NCL's edit
        lock, for instance) rather than being abandoned."""
        self._stop_requested = True
        self._pause.set()  # don't leave workers blocked on the pause gate
        logger.info("coordinator.stop_requested")

    @property
    def is_paused(self) -> bool:
        return not self._pause.is_set()

    # ── status for the GUI ───────────────────────────────────────

    def status_snapshot(self) -> dict:
        return {
            "lines": [s.as_dict() for s in self._status.values()],
            "resources": self.governor.snapshot.as_dict(),
            "max_concurrent": self.max_concurrent,
            "paused": self.is_paused,
            "stopping": self._stop_requested,
            "browser_alive": self.pool.is_alive,
            "live_contexts": self.pool.context_count,
            "live_cruise_lines": self.pool.live_cruise_lines(),
        }

    def results_for(self, cruise_line: CruiseLine) -> list[BookingResult]:
        return list(self._results.get(cruise_line, []))

    def all_results(self) -> list[BookingResult]:
        out: list[BookingResult] = []
        for rs in self._results.values():
            out.extend(rs)
        return out

    # ── the run ──────────────────────────────────────────────────

    async def run(self, on_result=None, on_status=None) -> dict:
        """Drain every registered line concurrently. Returns the final
        status snapshot.

        `on_result` / `on_status` callbacks are invoked GUARDED — a display
        exception must never take down a scan (the same lesson the GUI's
        own callback boundary already learned the hard way).
        """
        # One lock per portal we're about to drive — see _acquire_line_guards.
        if self._use_guard:
            self._acquire_line_guards()

        self.governor.start()
        try:
            await self.pool.start()

            # Whole-browser loss kills every line at once — that's the
            # accepted blast radius of sharing one process. Surface it
            # loudly instead of letting workers hang on a dead browser.
            browser = getattr(self.pool, "_browser", None)
            if browser is not None:
                browser.on("disconnected", lambda _: self._on_browser_disconnected())

            self._tasks = {
                cl: asyncio.create_task(self._drain_line(cl, on_result, on_status))
                for cl in self._queues
            }
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            return self.status_snapshot()
        finally:
            # Mark BEFORE tearing down so the disconnect handler knows
            # this is us closing the browser, not the browser dying.
            self._shutting_down = True
            await self.governor.stop()
            # Close each pooled scraper's PAGE first, then the pool's
            # contexts, then the browser — Playwright's own guidance:
            # browser.close() force-quits and loses close events.
            for cl, sc in list(self._pooled_scrapers.items()):
                try:
                    await sc.stop()   # pooled stop() closes only its page
                except Exception as e:
                    logger.warning('coordinator.pooled_scraper_stop_failed',
                                   cruise_line=cl.value, error=str(e))
            self._pooled_scrapers = {}
            await self.pool.close()
            if self._use_guard and self._holds_guard:
                self._release_line_guards()

    def _on_browser_disconnected(self) -> None:
        if self._shutting_down:
            logger.info("coordinator.browser_closed_normally")
            return
        logger.error(
            "coordinator.browser_disconnected",
            note="the SHARED browser died — every cruise line is affected. "
                 "This is the accepted blast radius of one shared process.",
        )
        self._stop_requested = True
        for st in self._status.values():
            if st.state in (LineState.RUNNING, LineState.PAUSED, LineState.STARTING):
                st.state = LineState.FAILED
                st.last_error = "shared browser disconnected"

    async def _drain_line(self, cruise_line: CruiseLine, on_result, on_status) -> None:
        status = self._status[cruise_line]
        worker = self._workers[cruise_line]
        status.state = LineState.STARTING
        status.started_at = time.monotonic()

        try:
            for booking_id in list(self._queues[cruise_line]):
                if self._stop_requested:
                    status.state = LineState.STOPPED
                    break

                # Cooperative pause, checked BETWEEN bookings only.
                await self._pause.wait()
                if self._stop_requested:
                    status.state = LineState.STOPPED
                    break

                # Load gate. If it never clears we defer rather than
                # hammering a struggling machine.
                if not await self.governor.wait_until_ok(timeout_s=300.0):
                    logger.warning(
                        "coordinator.deferring_booking_high_load",
                        cruise_line=cruise_line.value, booking_id=booking_id,
                    )
                    continue

                async with self._global_sem:
                    async with self._line_sems[cruise_line]:
                        status.state = LineState.RUNNING
                        status.current_booking_id = booking_id
                        self._notify(on_status)
                        try:
                            result = await worker(booking_id)
                            self._results[cruise_line].append(result)
                            if getattr(result, "status", None) is not None and result.status.value == "ERROR":
                                status.errors += 1
                                status.last_error = result.error or result.note
                            else:
                                status.done += 1
                                status.last_success_at = time.monotonic()
                            self._notify(on_result, result)
                        except Exception as e:
                            status.errors += 1
                            status.last_error = str(e)
                            logger.error(
                                "coordinator.booking_failed",
                                cruise_line=cruise_line.value, booking_id=booking_id, error=str(e),
                            )
                            # Per-line recovery: recycle ONLY this line's
                            # context so the other lines keep working.
                            try:
                                await self.pool.recycle_context(cruise_line)
                                status.context_recycles += 1
                            except Exception as re:
                                logger.error(
                                    "coordinator.context_recycle_failed",
                                    cruise_line=cruise_line.value, error=str(re),
                                )
                        finally:
                            status.queued = max(0, status.total - status.done - status.errors)
                            status.current_booking_id = None
                            self._notify(on_status)

            if status.state not in (LineState.STOPPED, LineState.FAILED):
                status.state = LineState.DONE
        except Exception as e:
            status.state = LineState.FAILED
            status.last_error = str(e)
            logger.error("coordinator.line_failed", cruise_line=cruise_line.value, error=str(e))
        finally:
            status.current_booking_id = None
            self._notify(on_status)
            logger.info(
                "coordinator.line_finished",
                cruise_line=cruise_line.value, state=status.state.value,
                done=status.done, errors=status.errors,
            )

    async def make_scraper_worker(
        self,
        cruise_line: CruiseLine,
        capture_market_data: bool = False,
        raw_dump_dir: str | None = None,
    ) -> BookingWorker:
        """Build a worker that checks bookings through a POOLED scraper.

        This is the real integration point for the three BaseScraper-based
        lines (ESPRESSO / NCL / GoCCL). The scraper is created normally and
        then `attach_pool()`-ed, so it uses the shared browser's isolated
        context for its cruise line instead of launching its own Chromium
        — and `check_booking` itself is completely untouched, which is why
        no calculation or optimization behavior can change.

        NOT used for MSC: MSC is the separate `msc_commands.py` subsystem,
        not a BaseScraper. Register MSC with your own callable instead
        (`register_line` takes any async callable) — that wiring is not
        done yet and is listed as remaining work rather than faked.

        Deliberately does NOT reuse `BookingService`: that class holds a
        single `_live_scraper` slot and stops it whenever the cruise line
        changes, which is precisely the limitation this coordinator exists
        to get around. Leaving it untouched keeps the existing
        single-line CLI/GUI path working exactly as before.
        """
        from services.booking_service import BookingService

        scraper = BookingService()._get_scraper(cruise_line)  # factory only
        scraper.attach_pool(self.pool)
        scraper.capture_everything = False
        if raw_dump_dir:
            scraper.raw_dump_dir = raw_dump_dir
        await scraper.start()   # attaches to the pooled context; launches nothing

        async def worker(booking_id: str) -> BookingResult:
            return await scraper.check_booking(
                booking_id, capture_market_data=capture_market_data,
            )

        # Keep a handle so run()'s teardown can close the page cleanly.
        self._pooled_scrapers[cruise_line] = scraper
        return worker

    def _notify(self, callback, *args) -> None:
        """Callbacks are always guarded — a GUI exception must never kill
        a scan that is driving a real browser against real bookings."""
        if callback is None:
            return
        try:
            callback(*args) if args else callback(self.status_snapshot())
        except Exception:
            logger.exception("coordinator.callback_failed")
