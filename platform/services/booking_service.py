"""Booking service — orchestrates the full scan workflow.

This is the enterprise equivalent of background.js runBatch().
Manages the scraper lifecycle, result storage, caching, and progress tracking.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from config.settings import settings
from core.booking_features import extract as extract_booking_features
from core.calculator import (
    make_cancelled_result,
    make_error_result,
    make_paid_in_full_result,
    make_skipped_result,
)
from core.models import BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus
from core.scan_signature import describe_overlap, scan_signature
from core.price_change import compare as compare_price
from models.database import BookingRecord, MarketDataRecord, PriceHistory, ScanJobRecord, async_session
from scraper.base import (
    BaseScraper,
    is_dead_browser_error,
    is_session_expired_error,
)
from scraper.espresso import EspressoScraper
from scraper.goccl import GoCCLScraper
from scraper.ncl import NclScraper
from services.cache_service import CacheService
from services.exclusion_service import ExclusionService
from utils.logging import get_logger, track_background_task

logger = get_logger(__name__)


#: Error text meaning "this run ended because nobody was logged in". The
#: single most useful thing a run summary can say, because it is the one
#: failure a human can fix in ten seconds.
_LOGIN_SHAPED = ("not logged in", "logged out", "login required",
                 "please log into")


def run_summary(job) -> dict:
    """What a finished scan actually did, as one structured record.

    THE GAP, 2026-10-01. `batch.complete` logged three fields - job_id,
    status, total - so the log could not answer "was that run healthy?".
    Nineteen batch.complete events in the log and not one of them says how
    many bookings errored, what was found, or whether it stopped because
    nobody was logged in.

    That is what Neon asked the watchdog to be: *"a watcher watching the
    code script project monitoring as a third eye that everything is
    running and functioning properly."* A third eye needs something to
    look at.

    Derived entirely from `job`, which is the only thing guaranteed to
    exist in `_run_batch`'s `finally` - counters declared inside the `try`
    are unbound if it failed early, and a summary that raises in a finally
    would replace a real failure with a confusing one.

    Deliberately NOT a notification. Notifications were turned off on
    2026-09-30 because closing the GUI looked like a crash and they fired
    constantly. This writes one line to the log, where the watchdog and a
    human can both read it after the fact.
    """
    from collections import Counter

    results = list(getattr(job, "results", []) or [])
    statuses = Counter(
        getattr(r.status, "value", r.status) for r in results)

    optimizations = [r for r in results
                     if getattr(r.status, "value", r.status) == "OPTIMIZATION"]
    errors = [r for r in results
              if getattr(r.status, "value", r.status) == "ERROR"]

    login_blocked = sum(
        1 for r in errors
        if any(token in str(getattr(r, "error", "") or "").lower()
               for token in _LOGIN_SHAPED))

    started = getattr(job, "started_at", None)
    finished = getattr(job, "completed_at", None)
    duration_s = None
    if started and finished:
        duration_s = round((finished - started).total_seconds(), 1)

    requested = len(getattr(job, "booking_ids", []) or [])
    checked = len(results)

    return {
        "requested": requested,
        "checked": checked,
        # Not the same number whenever a run stopped early - saying so is
        # the point. A run that reports 306 of 723 is a different event
        # from one that reports 723 of 723.
        "unfinished": max(requested - checked, 0),
        "statuses": dict(statuses.most_common()),
        "optimizations": len(optimizations),
        "savings": round(sum(float(r.net_saving or 0) for r in optimizations), 2),
        "errors": len(errors),
        "login_blocked": login_blocked,
        "duration_s": duration_s,
        "avg_s": (round(duration_s / checked, 1)
                  if duration_s and checked else None),
    }


class BookingService:
    """
    Orchestrates booking scans: manages scraper lifecycle, caching,
    result persistence, and progress tracking.
    """

    # MID-SCAN SESSION RECOVERY LIMITS. Tuned to the measured behaviour of
    # the portal, not guessed: ESPRESSO drops a session roughly HOURLY, so a
    # long watchlist must be able to recover repeatedly. What must NOT
    # happen is a hot loop against a portal that will not keep us in.
    #
    # A recovery is allowed unless the last one was BOTH very recent AND
    # only a booking or two ago - that combination is a loop; an hourly
    # drop is not.
    _RECOVERY_MIN_GAP_S = 180        # under 3 minutes apart...
    _RECOVERY_MIN_BOOKINGS = 3       # ...and within 3 bookings = a loop
    _RECOVERY_MAX = 12               # a full working day of hourly drops


    def __init__(self):
        self.cache = CacheService()
        self.exclusions = ExclusionService()
        self._active_jobs: dict[str, ScanJob] = {}
        self._stop_flags: dict[str, bool] = {}
        # A single long-lived scraper/browser, reused across "Check login"
        # and every scan in the same app session (see get_or_create_scraper).
        # Replaying saved session cookies into a brand-new browser process
        # is exactly the pattern ESPRESSO's bot-detection (Akamai) flags —
        # keeping one continuous instance from login through every scan
        # avoids that entirely, matching the one run that worked end to end.
        self._live_scraper: BaseScraper | None = None
        # See utils.logging.track_background_task — retains a strong
        # reference to the fire-and-forget _run_batch task per scan so
        # it can't be prematurely garbage-collected, and logs any
        # exception that escapes it.
        self._background_tasks: set = set()

    def _get_scraper(self, cruise_line: CruiseLine, market: str | None = None) -> BaseScraper:
        """Factory: get the right scraper for the cruise line.

        CONFIRMED REAL BUG 2026-08-12: this used to fall through to
        EspressoScraper for anything that wasn't NCL/GOCCL — including
        CruiseLine.MSC, which the GUI dropdown lists as a selectable
        option (it iterates the whole CruiseLine enum) but which has NO
        scraper here at all. MSC is driven entirely by the separate
        msc_commands.py/msc_session_controller.py subsystem, not this
        BaseScraper hierarchy. Selecting "MSC" here would have silently
        opened ESPRESSO's portal and run ESPRESSO's automation against
        MSC booking IDs instead. Failing loudly is strictly safer than
        the previous silent misroute — this does not change behavior for
        any cruise line that ever worked correctly through this factory."""
        if cruise_line == CruiseLine.NCL:
            # `market` selects the NCL agent account (US/CA). NCL runs a
            # separate SeaWeb account per market, so a Canadian booking is
            # invisible on the US login — see NclScraper.__init__.
            return NclScraper(market=market)
        if cruise_line == CruiseLine.GOCCL:
            return GoCCLScraper()
        if cruise_line == CruiseLine.MSC:
            raise ValueError(
                "MSC is not driven through this scraper pipeline — use the separate MSC "
                "subsystem (msc_session_controller.py / msc_commands.py) instead."
            )
        return EspressoScraper()

    @staticmethod
    def _is_dead_browser_error(exc: Exception) -> bool:
        """Delegates to scraper.base.is_dead_browser_error — moved there
        2026-08-13 so NclScraper/GoCCLScraper's check_booking() can share
        the exact same detection instead of each risking their own drifted
        copy. Kept as a method here since it's still called from within
        this class below."""
        return is_dead_browser_error(exc)

    def has_live_session(self, cruise_line: CruiseLine) -> bool:
        """Whether a browser session is already open for this cruise line
        (i.e. check_login has run) — Start should require this, since
        starting one fresh would fall back to a hidden headless browser
        with whatever stale session is on disk.

        Also confirms the browser/page behind it is still actually alive
        (not just that _live_scraper is a non-None object) — a scraper
        that crashed mid-scan (see _is_dead_browser_error) would otherwise
        report a live session that's really a dead browser underneath.
        """
        return (
            self._live_scraper is not None
            and self._live_scraper.cruise_line == cruise_line
            and self._live_scraper.is_alive
        )

    @staticmethod
    def _login_base_url(cruise_line: CruiseLine) -> str:
        """Where to land a fresh browser session for a manual login check.

        Same MSC fallthrough bug as _get_scraper — see its docstring."""
        if cruise_line == CruiseLine.NCL:
            return settings.ncl_search_url
        if cruise_line == CruiseLine.GOCCL:
            return settings.goccl_search_url
        if cruise_line == CruiseLine.MSC:
            raise ValueError(
                "MSC is not driven through this scraper pipeline — use the separate MSC "
                "subsystem (msc_session_controller.py / msc_commands.py) instead."
            )
        return settings.espresso_home_url

    async def get_or_create_scraper(
        self, cruise_line: CruiseLine, headless: bool | None = None,
        market: str | None = None,
    ) -> BaseScraper:
        """Get the live, already-open scraper for this cruise line, or start
        a new one if none is open yet (or the cruise line changed).

        A MARKET change counts as a change too (added 2026-08-27): NCL's
        US and CA logins are different accounts with different sessions, so
        reusing a live US scraper for a CA scan would silently check every
        Canadian booking against the wrong account and report them all as
        not-found."""
        if self._live_scraper is not None:
            same_line = self._live_scraper.cruise_line == cruise_line
            wanted = (market or "").upper()
            current = (getattr(self._live_scraper, "market", "") or "").upper()
            same_market = (not wanted) or wanted == current
            if same_line and same_market and self._live_scraper.is_alive:
                return self._live_scraper
            await self._live_scraper.stop()
            self._live_scraper = None

        scraper = self._get_scraper(cruise_line, market=market)
        await scraper.start(headless=headless)
        self._live_scraper = scraper
        return scraper

    async def close_live_scraper(self) -> None:
        """Close the live browser session, if one is open — saves the
        final session state. Call this on app shutdown."""
        if self._live_scraper is not None:
            await self._live_scraper.stop()
            self._live_scraper = None

    async def check_login(
        self, cruise_line: CruiseLine, timeout_minutes: float = 15.0,
        market: str | None = None, headless: bool = False,
    ) -> bool:
        """
        Open (or reuse) the live browser and wait for the user to log in. The
        same browser instance stays open afterward for start_scan to reuse —
        never closed and reopened, since that's what triggers the
        bot-detection replay flag.

        `headless` was hardcoded False here until 2026-09-16, which is why the
        GUI always showed a window: the browser a GUI scan runs in is the one
        THIS method opens, and start_scan's own `headless` argument never
        applies because the GUI passes keep_browser_open=True.

        NCL ONLY. Neon asked for the choice on NCL, and NCL earned it —
        headless was proven to drive the entire flow (Switch to Edit Mode, the
        SlickGrid category read, the price comparison, cancel-and-release):
        headless and headed returned identical totals and identical category
        counts (30/23/31 from _form_12) across the same three bookings.
        ESPRESSO can NEVER be headless whatever is passed here — its Akamai
        bot detection breaks it, and scraper/base.py enforces that
        independently of this argument.
        """
        scraper = await self.get_or_create_scraper(
            cruise_line, headless=headless, market=market,
        )
        base_url = self._login_base_url(cruise_line)
        await scraper.navigate(base_url)

        # AUTO-LOGIN FIRST. CONFIRMED GAP, fixed 2026-08-28. Neon: "ncl did
        # not autologin at all". `auto_login()` was wired into _run_batch's
        # fresh-browser path on 2026-08-27, but the GUI uses
        # keep_browser_open=True and reaches the portal through THIS method,
        # which never called it - so the desktop app sat waiting for a
        # manual NCL login even with credentials saved in Windows
        # Credential Manager.
        #
        # Best-effort by contract: auto_login never raises (see
        # NclScraper.auto_login) and returns a status string. On anything
        # other than OK we simply fall through to the manual poll below,
        # which is the pre-existing behaviour.
        #
        # UPDATED 2026-09-16. ESPRESSO now has an auto_login too. Neon:
        # "there is a bug with espresso logging in ... although i have
        # entered the passwords using the command" - and he was right,
        # EspressoScraper simply had no auto_login method, so the
        # credential save_login.py stored was read by nothing at all.
        #
        # It returns "FILLED_AWAITING_MFA" on the normal path rather than
        # "OK", because ESPRESSO requires MFA and a fully unattended login
        # is not possible. That deliberately falls through to the manual
        # poll below: the username and password are already typed in, and
        # the human only completes MFA. The poll then sees the real
        # session. `hasattr` means no wiring change was needed here.
        if hasattr(scraper, "auto_login"):
            try:
                status = await scraper.auto_login()
                logger.info(
                    "login_check.auto_login", cruise_line=cruise_line.value,
                    status=status,
                )
                if status == "OK" and await self._verify_login(scraper, cruise_line):
                    logger.info("login_check.success", cruise_line=cruise_line.value,
                                via="auto_login")
                    return True
            except Exception as exc:
                logger.warning("login_check.auto_login_failed",
                               cruise_line=cruise_line.value, error=str(exc))

        # Require the same non-login URL on two consecutive polls before
        # declaring success — see the matching comment in main.py's
        # _run_login_check for why a single check is unsafe (a momentary
        # SSO/MFA redirect hop can look like "logged in" for one poll).
        deadline = time.monotonic() + timeout_minutes * 60
        poll_s = 5
        stable_url: str | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            # WEAKNESS FIXED 2026-08-27: this branch tested login by
            # looking for the substring "login"/"signin" in the URL, while
            # ESPRESSO got a real `_check_login()`. NclScraper HAS a real
            # `_check_login()` (it checks the page, not the address bar) and
            # it simply wasn't being used, so an NCL "login OK" was only
            # ever as trustworthy as the portal's URL naming — and any
            # redirect to an auth host whose path doesn't literally say
            # "login" would have reported success while logged out, which
            # is the same class of bug as the has_live_session-vs-login
            # confusion fixed on 2026-08-26.
            #
            # GoCCLScraper genuinely has no `_check_login()` override
            # (verified), so it keeps the URL heuristic — but explicitly and
            # with the reason stated, rather than being lumped in with NCL
            # as if both were equally unverifiable.
            logged_in = await self._verify_login(scraper, cruise_line)
            current_url = scraper.page.url
            if logged_in and current_url == stable_url:
                logger.info("login_check.success", cruise_line=cruise_line.value)
                return True
            stable_url = current_url if logged_in else None
            logger.info("login_check.waiting", cruise_line=cruise_line.value)

        logger.warning("login_check.timeout", cruise_line=cruise_line.value)
        return False

    async def session_is_logged_in(self, cruise_line: CruiseLine) -> bool:
        """Is the live session ACTUALLY logged in, right now?

        `has_live_session` answers a different question - whether a browser
        is open and alive. A browser can be perfectly alive and sitting on
        a login wall, which is exactly what ESPRESSO does when it drops a
        session (roughly hourly; see the auto-logout note in espresso.py).

        THE GAP THIS CLOSES. The GUI's Start guard tested
        `has_live_session() and _login_ok_for == line`. `_login_ok_for` is
        STICKY - set once on a successful login and never re-checked - so
        once it was set, Start skipped the login step entirely and began
        scanning. If the session had expired in the meantime, every booking
        ran against a logged-out portal: **339 `login.required` events** in
        the log.

        Cheap: one page check against the already-open browser, no
        navigation, no new window. Never raises - an unanswerable question
        is reported as "not logged in", so the caller logs in again rather
        than scanning blind.
        """
        if not self.has_live_session(cruise_line):
            return False
        try:
            return await self._verify_login(self._live_scraper, cruise_line)
        except Exception as exc:  # noqa: BLE001
            logger.warning("login.verify_failed", cruise_line=cruise_line.value,
                           error=str(exc)[:200])
            return False

    @staticmethod
    async def _verify_login(scraper, cruise_line: CruiseLine) -> bool:
        """One definition of "logged in", shared by the auto-login path and
        the manual poll so they can never disagree.

        Prefers the scraper's OWN `_check_login()` when it defines one
        (ESPRESSO and NCL both do). GoCCLScraper genuinely does not, so it
        keeps the URL heuristic - stated explicitly with the reason rather
        than lumping it in with lines that can be checked properly.
        """
        if "_check_login" in type(scraper).__dict__:
            try:
                return await scraper._check_login()
            except Exception as exc:
                logger.warning("login_check.probe_failed",
                               cruise_line=cruise_line.value, error=str(exc))
                return False
        url = (scraper.page.url or "").lower()
        return "login" not in url and "signin" not in url

    async def start_scan(
        self,
        booking_ids: list[str],
        cruise_line: CruiseLine,
        on_progress: Callable[[ScanJob], None] | None = None,
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
        capture_everything: bool = False,
        on_action: Callable[[dict], None] | None = None,
        keep_browser_open: bool = False,
        headless: bool | None = None,
        market: str | None = None,
    ) -> ScanJob:
        """
        Start a batch scan of booking IDs.

        Args:
            booking_ids: List of booking IDs to check.
            cruise_line: Which cruise line portal to use.
            on_progress: Optional callback for progress updates.
            bypass_cache: If True, skip the NO_SAVING TTL cache and always
                check live (used by recurring/"watch" runs, where the whole
                point is to re-check the same bookings over time).
            raw_dump_dir: If set, append each booking's raw API response to
                raw_dump_dir/raw_responses.jsonl for later offline analysis.
            capture_everything: If True, also capture full page HTML +
                structured extraction and all network traffic to
                raw_dump_dir, and record a step-by-step action log to
                raw_dump_dir/actions.jsonl.
            on_action: Optional callback invoked with each action-log entry
                as it happens (used by the GUI to show a live activity log).
            keep_browser_open: If True, reuse the live scraper (from
                get_or_create_scraper/check_login) and leave it open when
                the batch finishes, instead of starting a fresh browser and
                closing it — used by the GUI so login and every scan share
                one continuous session. CLI one-shot runs leave this False.
            headless: Only applies when keep_browser_open is False (a
                reused live scraper already has its own headless state from
                whatever started it). None defers to settings.browser_headless
                (headless); pass False to pop a real, visible browser window
                for this scan so it can be watched instead of trusted blind.

        Returns:
            ScanJob with results populated as they complete.
        """
        job_id = str(uuid.uuid4())
        job = ScanJob(
            job_id=job_id,
            booking_ids=booking_ids,
            cruise_line=cruise_line,
            status=ScanJobStatus.RUNNING,
            progress_total=len(booking_ids),
            started_at=datetime.utcnow(),
        )
        # THE REQUEST'S IDENTITY, so the same Start is not run twice in a
        # row. See core/scan_signature.py and recent_identical_scan.
        # Attached to the job rather than threaded through _save_job_to_db's
        # signature, because every caller already builds the job.
        job.signature = scan_signature(cruise_line.value, booking_ids,
                                       bypass_cache=bypass_cache)
        self._active_jobs[job_id] = job
        self._stop_flags[job_id] = False

        # Save job to DB
        await self._save_job_to_db(job)

        # Run in background — track_background_task retains a strong
        # reference (see its docstring: a fire-and-forget task with none
        # is a real GC/lost-exception risk) and logs any exception that
        # escapes _run_batch's own try/except (which already handles the
        # normal failure paths — this is a last-resort net for anything
        # that somehow gets past that).
        task = asyncio.create_task(self._run_batch(
            job, on_progress, bypass_cache, raw_dump_dir, capture_market_data,
            capture_everything, on_action, keep_browser_open, headless, market,
        ))
        track_background_task(self._background_tasks, task)

        return job

    async def _run_batch(
        self,
        job: ScanJob,
        on_progress: Callable[[ScanJob], None] | None = None,
        bypass_cache: bool = False,
        raw_dump_dir: str | None = None,
        capture_market_data: bool = False,
        capture_everything: bool = False,
        on_action: Callable[[dict], None] | None = None,
        keep_browser_open: bool = False,
        market: str | None = None,
        headless: bool | None = None,
    ) -> None:
        """Execute the batch scan."""
        # MOVED INSIDE THE TRY, 2026-08-26: acquiring the scraper used to
        # happen BEFORE the try/except/finally below, so anything it raised
        # (a browser relaunch failure inside get_or_create_scraper →
        # scraper.start(), or _get_scraper's ValueError for an unsupported
        # cruise line) escaped _run_batch entirely — leaving job.status
        # permanently RUNNING with no completed_at. The GUI polls while
        # status is PENDING/RUNNING, so that meant a GUI stuck at "Starting
        # queue processing…" FOREVER, with Stop doing nothing. Now inside
        # the try, so the except sets FAILED and the finally records it.
        scraper = None
        consecutive_failures = 0

        # ADDED 2026-08-26: CacheService.cleanup_expired() existed but had
        # ZERO callers anywhere in the project, so eviction only ever
        # happened lazily inside get() — i.e. only for a key someone
        # happened to look up. The live DB confirmed the result: all 414
        # cache rows were expired, the newest by over a week, growing
        # monotonically forever. Once per batch is cheap and bounds it.
        # Guarded because a housekeeping failure must never stop a scan.
        try:
            purged = await self.cache.cleanup_expired()
            if purged:
                logger.info("batch.cache_cleanup", purged=purged)
        except Exception as e:
            logger.warning("batch.cache_cleanup_failed", error=str(e))

        try:
            reconciled = await self.reconcile_stale_jobs()
            if reconciled:
                logger.warning("batch.reconciled_stale_jobs", count=reconciled)
        except Exception as e:
            logger.warning("batch.reconcile_stale_jobs_failed", error=str(e))

        try:
            # CONFIRMED BUG, fixed 2026-08-27: `market` was not threaded
            # into this method at all, so `main.py scan --cruise-line NCL
            # --market CA` built its LOGIN scraper with the CA account and
            # then scanned with a freshly-built US one. The flag silently
            # did nothing for the actual scan, and every Canadian booking
            # would have come back "Reservation is not found" — looking
            # like a portal problem rather than the wrong account.
            if keep_browser_open:
                scraper = await self.get_or_create_scraper(job.cruise_line, market=market)
            else:
                scraper = self._get_scraper(job.cruise_line, market=market)
            scraper.raw_dump_dir = raw_dump_dir
            scraper.capture_everything = capture_everything
            scraper.on_action = on_action

            if not keep_browser_open:
                await scraper.start(headless=headless)

            # PRE-FLIGHT SESSION CHECK, added 2026-08-27 after a real
            # failure: in the 506-booking ESPRESSO run of 2026-08-27,
            # bookings #400-403 (3000041, 3000063, 3000062, 3000044) all
            # died with "Session logged out while searching" between
            # 14:19 and 14:31 UTC. Nothing checked the session was usable
            # before the batch started — the batch simply drove into the
            # portal and found out one booking at a time.
            #
            # This only runs on the keep_browser_open (GUI) path, where a
            # login was explicitly confirmed moments earlier, so a failure
            # here means the session died IN BETWEEN — precisely the
            # "pressed Start and it made me log in again" symptom.
            #
            # Deliberately FAIL-CLOSED. Scanning a whole watchlist against
            # a logged-out portal produces a burst of failures on a
            # bot-detection-sensitive account (see the Akamai notes in
            # check_login and DOCUMENTATION.md section L) and reports every
            # client as ERROR. Refusing up-front with an actionable message
            # is strictly better than discovering it 400 bookings in.
            # RE-ARM THE PORTAL'S OWN AUTO-LOGOUT BEFORE JUDGING THE
            # SESSION. ESPRESSO arms a 30.5-minute client-side timer on
            # every page load that navigates the BROWSER to logout; found
            # 2026-09-21 in a live capture, confirmed in 50 occurrences
            # across 25 pages. The reported sequence - "Check login"
            # succeeds, the operator does something else, presses Start and
            # is told to log in again - is that timer firing in between.
            # Touching the portal first costs one navigation and removes a
            # whole class of false "not logged in" failures.
            keepalive = getattr(scraper, "keep_session_alive", None)
            if keepalive is not None:
                try:
                    if await keepalive():
                        logger.info("batch.session_refreshed_before_start",
                                    job_id=job.job_id)
                except Exception as exc:
                    logger.warning("batch.keepalive_failed",
                                   job_id=job.job_id, error=str(exc)[:200])

            if "_check_login" in type(scraper).__dict__:
                try:
                    session_ok = await scraper._check_login()
                except Exception as e:
                    # A failing CHECK is not a failing session — do not
                    # block the batch on a broken probe.
                    logger.warning(
                        "batch.preflight_login_check_errored",
                        job_id=job.job_id, error=str(e),
                    )
                    session_ok = True

                # CONFIRMED REAL BUG, fixed 2026-08-27. On the CLI path
                # (keep_browser_open=False) NOTHING ever authenticated —
                # scraper.start() restores storage_state and the batch
                # simply hoped the replayed cookies were still valid.
                # Proven live: `main.py scan --cruise-line NCL` logged
                # `restored_session=True` and then failed all 3 pilot
                # bookings with a bare
                # `Timeout ... waiting for #SWXMLForm_SearchReservation_ResID`
                # — the search field does not exist because the page was
                # the login screen. `run_ncl_live_check.py`, the script that
                # DID work, calls `auto_login()` explicitly; the batch path
                # never did.
                #
                # Only on the fresh-browser path: when keep_browser_open is
                # set, a human just logged in through check_login and
                # re-authenticating underneath them is wrong (and on
                # ESPRESSO a second login can knock out the live session —
                # see DOCUMENTATION.md section L).
                if not session_ok and not keep_browser_open and hasattr(scraper, "auto_login"):
                    logger.info("batch.attempting_auto_login", job_id=job.job_id,
                                cruise_line=job.cruise_line.value)
                    try:
                        # auto_login never raises by contract (see
                        # NclScraper.auto_login) but do not depend on that.
                        status = await scraper.auto_login()
                        logger.info("batch.auto_login_result", job_id=job.job_id, status=status)
                        session_ok = await scraper._check_login()
                    except Exception as e:
                        logger.error("batch.auto_login_failed", job_id=job.job_id, error=str(e))

                if not session_ok:
                    job.status = ScanJobStatus.FAILED
                    if keep_browser_open:
                        job.error = (
                            f"{job.cruise_line.value} session is not logged in any more — "
                            f"0 of {len(job.booking_ids)} bookings were checked. "
                            f"Click \"Check login\", complete the login, then Start again."
                        )
                    else:
                        job.error = (
                            f"Could not log in to {job.cruise_line.value} — 0 of "
                            f"{len(job.booking_ids)} bookings were checked. The saved "
                            f"session is stale and auto-login did not succeed. Save "
                            f"credentials with save_login.py, or run the scan from the "
                            f"GUI where you can log in by hand."
                        )
                    logger.error(
                        "batch.preflight_login_failed",
                        job_id=job.job_id, cruise_line=job.cruise_line.value,
                        booking_count=len(job.booking_ids),
                    )
                    return

            # One re-login attempt per batch - see the session-expiry
            # branch below. The booking that was interrupted keeps its ERROR
            # row (it failed to the SESSION, not to anything about itself)
            # and is named in the job warning so it can be re-run; the
            # REMAINING bookings are what recovery is really for.
            # RECOVERIES ARE NO LONGER ONE-SHOT. See the block that uses
            # these: ESPRESSO drops a session roughly HOURLY, and a
            # 723-booking watchlist runs for many hours, so "once per batch"
            # guaranteed the scan died partway through every single time.
            session_recoveries = 0
            last_recovery_at: float | None = None
            last_recovery_index = -1
            interrupted_by_logout: list[str] = []

            # PERMANENTLY EXCLUDED BOOKINGS, LOOKED UP ONCE FOR THE WHOLE
            # LIST, BEFORE ANY BROWSER ACTION.
            #
            # Neon 2026-09-29, non-negotiable: a booking confirmed paid in
            # full must NEVER be rescanned, "EVEN IF THE USER PASTES OR ADDS
            # IT IN THE LIST". It cannot be repriced, so opening it is pure
            # waste - 439 of 565 redundant scans in one measured day.
            #
            # ONE query for the entire watchlist, not one per booking: the
            # TTL cache below still does the latter, which on a 723-booking
            # list is 723 round trips before the first page loads.
            #
            # Deliberately NOT bypassed by "Force live recheck". That toggle
            # is for a stale TTL, and this is a standing fact about the
            # booking rather than a cached opinion about its price. Lifting
            # one is an explicit act - ExclusionService.clear().
            excluded: dict[str, dict] = {}
            try:
                excluded = await self.exclusions.active_for(
                    job.cruise_line.value, list(job.booking_ids))
                if excluded:
                    logger.info("batch.exclusions_loaded",
                                job_id=job.job_id, count=len(excluded),
                                of=len(job.booking_ids))
            except Exception as exc:
                # Fails OPEN: a redundant scan costs a page load, a wrongly
                # skipped booking costs a real client's price drop.
                logger.warning("batch.exclusion_lookup_failed",
                               error=str(exc)[:200])

            # FRESHNESS, ALSO LOOKED UP ONCE FOR THE WHOLE LIST.
            #
            # The per-booking CacheService.get() this replaces issued a
            # SELECT (and on an expired row a DELETE + commit) from inside
            # the loop - 723 round trips on a full watchlist, each one a
            # chance to hit the SQLite lock that once marked an entire job
            # FAILED.
            fresh: dict[str, dict] = {}
            if not bypass_cache:
                try:
                    fresh = await self.cache.get_many(
                        job.cruise_line.value, list(job.booking_ids))
                    if fresh:
                        logger.info(
                            "batch.freshness_loaded", job_id=job.job_id,
                            fresh=len(fresh), of=len(job.booking_ids),
                            window_hours=settings.freshness_for(
                                job.cruise_line.value))
                except Exception as exc:
                    logger.warning("batch.freshness_lookup_failed",
                                   error=str(exc)[:200])

            # ONE RETRY PASS, APPENDED TO THE SAME LOOP.
            #
            # `work` starts as the queue and is extended ONCE, after the
            # last original booking, with the failures worth re-attempting
            # (see _bookings_to_retry). enumerate() over a LIST reads by
            # index, so the appended ids are picked up by this same loop -
            # no second copy of 600 lines of per-booking logic, which is
            # where a divergence would eventually hide.
            #
            # Measured 2026-10-01: 456 of 514 ERROR rows (88%) were
            # followed by a successful scan of the same booking. Those
            # recoveries only ever happened because Neon ran the whole
            # scan again the next day.
            work: list[str] = list(job.booking_ids)
            retry_queued = False

            for i, booking_id in enumerate(work):
                if self._stop_flags.get(job.job_id):
                    job.status = ScanJobStatus.STOPPED
                    logger.info("batch.stopped", job_id=job.job_id, at_index=i)
                    break

                job.current_booking_id = booking_id
                # Capped: retries are appended to `work`, so i can run past
                # the queue length, and a progress bar reading 740/723
                # would look like a bug.
                job.progress_done = min(i, job.progress_total)

                # CHECKPOINT. Written at the TOP of the iteration, so it
                # records everything finished so far whichever branch the
                # PREVIOUS booking took - cached, excluded, skipped, errored
                # or scanned. Five places append a result; checkpointing at
                # each of them would be five chances to forget one, and a
                # sixth branch added later would silently stop being
                # recorded.
                #
                # Before this, progress reached the database once, in the
                # finally at the end of the run - so a hard death recorded
                # nothing at all. See _checkpoint.
                await self._checkpoint(job)

                # NEVER RESCAN A CONFIRMED PAID-IN-FULL BOOKING.
                #
                # Checked before the TTL cache and before any browser
                # action. Not subject to bypass_cache - see the lookup
                # above for why.
                if booking_id in excluded:
                    entry = excluded[booking_id]
                    when = entry.get("excluded_at")
                    logger.info("batch.permanently_excluded",
                                booking_id=booking_id,
                                reason=entry.get("reason"),
                                excluded_at=str(when))
                    # REPORT THE RIGHT STATUS, NOT A GENERIC SKIP.
                    #
                    # A cancellation is "VERY MADNATORY ... something very
                    # critical" to report (Neon 2026-09-22), and adding it
                    # to this register must not quietly demote it. An
                    # excluded booking is still REPORTED every run - it just
                    # comes back from the database instead of the portal.
                    # What stops is the scraping, not the reporting.
                    reason = entry.get("reason")
                    if reason == "CANCELLED":
                        result = make_cancelled_result(
                            booking_id, None, job.cruise_line)
                        explain = ("A cancelled reservation does not "
                                   "un-cancel.")
                    else:
                        result = make_paid_in_full_result(
                            booking_id, None, job.cruise_line, 0.0)
                        explain = ("A paid-in-full booking cannot be "
                                   "repriced.")
                    result.note = (
                        f"{result.note or ''} Not rescanned - confirmed "
                        f"{reason} and permanently excluded"
                        + (f" on {when:%Y-%m-%d}" if when else "")
                        + f". {explain}"
                    ).strip()
                    if when is not None:
                        # "Last scanned" must show when it was CONFIRMED,
                        # not now - checked_at defaults to utcnow().
                        result.checked_at = when
                    job.results.append(result)
                    job.progress_done = min(i + 1, job.progress_total)
                    if on_progress:
                        on_progress(job)
                    continue

                # Smart cache check.
                #
                # GUARDED 2026-08-26: this was the ONE unguarded await left in
                # the per-booking loop. The 2026-08-12 fix wrapped every step
                # BELOW the scrape in its own try/except so "one booking fails,
                # the rest continue" — but this line sits above that and was
                # bare. CacheService.get() does a SELECT and, on an expired
                # entry, a DELETE + commit; any failure (SQLite lock from a
                # concurrent process — a real scenario, see the cross-process
                # note in DOCUMENTATION.md) propagated to the outer handler,
                # marked the WHOLE job FAILED and abandoned every remaining
                # booking. Fails OPEN (cached=None → check it live), which is
                # the safe direction: a redundant live check costs a page load,
                # a skipped one costs a real client's price drop.
                cached = fresh.get(booking_id)
                if cached:
                    logger.info("batch.cached", booking_id=booking_id,
                                hours_ago=cached["hours_ago"],
                                status=cached.get("status"))
                    result = make_skipped_result(
                        booking_id, None, job.cruise_line, cached["hours_ago"],
                    )
                    # CARRY THE STORED FIGURES ONTO THE SKIPPED ROW.
                    #
                    # Neon's requirement: "Show the stored price data for
                    # skipped bookings just like freshly scanned ones." The
                    # old cache stored only a timestamp - value_json existed
                    # and was never written - so a skipped booking read
                    # "scanned 1.4h ago" with every price column blank, and
                    # the row looked like nothing had happened.
                    data = cached.get("data") or {}
                    for field in ("old_total", "new_total", "net_saving",
                                  "price_category", "currency"):
                        value = data.get(field)
                        if value is not None:
                            setattr(result, field, value)
                    # THE TIME SHOWN MUST BE THE ORIGINAL SCAN, NOT NOW.
                    #
                    # BookingResult.checked_at defaults to utcnow(), so a
                    # skipped row would otherwise claim it had just been
                    # scanned - the exact opposite of what this feature is
                    # for. The GUI's "Last scanned" column reads this field.
                    if cached.get("scanned_at") is not None:
                        result.checked_at = cached["scanned_at"]
                    prior = cached.get("status")
                    if prior:
                        result.note = (
                            f"Not rescanned - {prior} {cached['hours_ago']}h ago"
                            f" (within the "
                            f"{settings.freshness_for(job.cruise_line.value)}h "
                            f"window for {job.cruise_line.value})"
                        )
                    job.results.append(result)
                    job.progress_done = min(i + 1, job.progress_total)
                    if on_progress:
                        on_progress(job)
                    continue

                logger.info("batch.checking", booking_id=booking_id, index=i + 1, total=len(job.booking_ids))

                try:
                    result = await scraper.check_booking(booking_id, capture_market_data=capture_market_data)
                except Exception as e:
                    logger.error("batch.error", booking_id=booking_id, error=str(e))
                    result = make_error_result(booking_id, None, job.cruise_line, str(e))

                    # RELEASE THE LOCK ON THE ERROR PATH TOO, added
                    # 2026-09-16. check_booking releases it on every
                    # successful branch, but a booking that FAILED is
                    # still retrieved and therefore still locked - and
                    # ESPRESSO holds that lock for 15 minutes. Failures
                    # are not rare (141 ESPRESSO timeouts historically),
                    # so skipping this would leave exactly the bookings
                    # someone wants to look at by hand locked out.
                    #
                    # Best-effort and non-fatal: release_booking never
                    # raises, and this must not replace the real error.
                    if hasattr(scraper, "release_booking"):
                        try:
                            await scraper.release_booking(booking_id)
                        except Exception:
                            pass

                    # SESSION EXPIRY IS NOT A DEAD BROWSER. Added 2026-09-21.
                    #
                    # Neon: "in the middle of scrapping sometimes the
                    # account logges out and does not log in automatically".
                    # EspressoScraper raises "Session logged out while
                    # searching" when its own _check_login fails mid-batch,
                    # and nothing recognised it: the loop tested only
                    # _is_dead_browser_error, and a signed-out portal is a
                    # perfectly healthy browser showing a login page. So the
                    # batch carried on into the login wall one booking at a
                    # time - the recorded 2026-08-27 run where ESPRESSO
                    # bookings #400-403 died in sequence.
                    #
                    # Recovery is attempted ONCE per batch. If it works the
                    # remaining bookings continue; if it does not, the batch
                    # STOPS here rather than turning the rest of the
                    # watchlist into identical ERROR rows against a login
                    # screen. Stopping with 400 real results and a clear
                    # reason beats 500 rows where the last 100 are noise -
                    # and on a bot-sensitive account, hammering a login wall
                    # is itself a risk.
                    if is_session_expired_error(e):
                        # A LOOP IS THE DANGER, NOT A SECOND LOGOUT.
                        #
                        # Neon 2026-09-29: "make a bug fix for mid scan
                        # that if this happens the script just loges in and
                        # continue where it stopped".
                        #
                        # The old rule was ONE recovery per batch, on the
                        # reasoning that "a session that dies again right
                        # after a successful re-login is not a transient
                        # blip". True - but the real interval is not "right
                        # after". Measured on the 723-booking run of
                        # 2026-09-29:
                        #
                        #   13:40:31  session_expired_recovering
                        #   13:40:43  session_recovered      <- worked
                        #   14:41:20  session_expired_again  <- 61 min later
                        #                                      batch STOPPED
                        #                                      399 unchecked
                        #
                        # ESPRESSO drops a session roughly hourly, so a
                        # long watchlist is GUARANTEED to hit a second
                        # logout. One-shot recovery meant every long scan
                        # died partway through and Neon pressed Start again
                        # by hand.
                        #
                        # What actually needs preventing is a hot loop -
                        # re-logging-in over and over against a portal that
                        # will not keep us in. So the guard is now about
                        # RATE, not count: recover freely when the logouts
                        # are far apart, stop when they are not.
                        now = time.monotonic()
                        too_soon = (
                            last_recovery_at is not None
                            and (now - last_recovery_at) < self._RECOVERY_MIN_GAP_S
                            and (i - last_recovery_index) < self._RECOVERY_MIN_BOOKINGS
                        )
                        if too_soon or session_recoveries >= self._RECOVERY_MAX:
                            logger.error(
                                "batch.session_expired_again",
                                booking_id=booking_id, job_id=job.job_id,
                                recoveries=session_recoveries,
                                seconds_since_last=(
                                    None if last_recovery_at is None
                                    else int(now - last_recovery_at)),
                                reason="too_soon" if too_soon else "max_recoveries")
                            job.error = (
                                f"{job.cruise_line.value} signed out again at "
                                f"booking {booking_id} "
                                + ("immediately after a re-login - the portal "
                                   "is refusing to keep this session, so the "
                                   "scan stopped rather than hammering the "
                                   "login wall."
                                   if too_soon else
                                   f"after {session_recoveries} recoveries.")
                                + f" Stopped with {len(job.results)} of "
                                f"{len(job.booking_ids)} bookings checked."
                            )
                            break
                        session_recoveries += 1
                        last_recovery_at = now
                        last_recovery_index = i
                        logger.warning("batch.session_expired_recovering",
                                       booking_id=booking_id, job_id=job.job_id)
                        status = None
                        try:
                            if hasattr(scraper, "auto_login"):
                                status = await scraper.auto_login()
                            logger.info("batch.session_recovery_login",
                                        job_id=job.job_id, status=status)
                        except Exception as relogin_error:
                            logger.error("batch.session_recovery_failed",
                                         job_id=job.job_id,
                                         error=str(relogin_error)[:200])

                        # LET THE LOGIN LAND BEFORE ASKING IF IT WORKED.
                        #
                        # THE BUG, from Neon's run of 2026-09-28: "the
                        # esspresso login issue still exists and i needed to
                        # login twice". The log showed the contradiction
                        # plainly:
                        #
                        #   15:00:30 espresso.auto_login result=OK
                        #   15:00:30 batch.session_recovery_gave_up
                        #
                        # auto_login SUCCEEDED and the batch stopped anyway.
                        # _check_login ran in the same second, while the SSO
                        # redirect chain (login -> auth -> oauth/callback ->
                        # app) was still in flight, so it sampled a page
                        # mid-hop and said "not logged in". The scan then
                        # died on a session that was actually fine.
                        #
                        # Same race as the one fixed in check_booking on the
                        # 23rd; the recovery path never got the treatment.
                        # Settle first, then poll - an SSO chain takes
                        # seconds, and asking once is asking too early.
                        recovered = False
                        try:
                            if hasattr(scraper, "_settle_navigation"):
                                await scraper._settle_navigation(
                                    timeout_ms=20000, quiet_ms=1500)
                            if "_check_login" in type(scraper).__dict__:
                                for attempt in range(6):        # ~15s
                                    recovered = await scraper._check_login()
                                    if recovered:
                                        break
                                    await asyncio.sleep(2.5)
                            else:
                                recovered = status in ("OK", "ALREADY_LOGGED_IN")
                        except Exception:
                            recovered = False

                        if recovered:
                            logger.info("batch.session_recovered",
                                        job_id=job.job_id, booking_id=booking_id)

                            # RETRY THE BOOKING THAT WAS INTERRUPTED.
                            #
                            # Neon 2026-09-29: "if this happens the script
                            # just loges in and continue where it stopped".
                            #
                            # It used to keep its ERROR row and move on, so
                            # every logout cost one real booking and the
                            # operator had to re-run it by hand. The booking
                            # never failed on its merits - it failed to a
                            # logout, which is exactly what we have just
                            # fixed. On the run of 2026-09-29, booking
                            # 3001010 got an ERROR row at 13:40 and came
                            # back NO_SAVING when re-checked by hand an hour
                            # later; nothing was wrong with it.
                            #
                            # One attempt only. If it fails again the
                            # original ERROR row stands and the batch
                            # continues - a booking that fails twice is a
                            # booking problem, not a session problem.
                            retried = None
                            try:
                                retried = await scraper.check_booking(
                                    booking_id,
                                    capture_market_data=capture_market_data)
                            except Exception as retry_error:
                                logger.warning(
                                    "batch.retry_after_recovery_failed",
                                    booking_id=booking_id,
                                    error=str(retry_error)[:200])
                            if retried is not None:
                                logger.info("batch.retry_after_recovery_ok",
                                            booking_id=booking_id,
                                            status=getattr(retried.status,
                                                           "value", None))
                                result = retried
                                # Fall through to the normal recording path
                                # so it is stored, cached and counted like
                                # any other result.
                            else:
                                interrupted_by_logout.append(booking_id)
                                job.warning = (
                                    f"{job.cruise_line.value} signed out during "
                                    f"the scan and was logged back in "
                                    f"automatically. Re-run "
                                    f"{', '.join(interrupted_by_logout)} - "
                                    f"{'it' if len(interrupted_by_logout) == 1 else 'they'} "
                                    f"failed to the logout, not to the booking."
                                )
                                # NOTHING TO RECORD, so skip the rest of the
                                # loop body for this booking.
                                #
                                # The comment here used to claim "THIS
                                # BOOKING keeps its ERROR row". It does not,
                                # and never did: `continue` jumps over
                                # job.results.append() further down, so an
                                # interrupted booking vanished from the run
                                # with no row of any kind. Confirmed in the
                                # database on 2026-09-29 - bookings 3001010
                                # and 3001008 were interrupted by logouts
                                # and their ONLY rows came from Neon
                                # re-running them by hand afterwards.
                                #
                                # That is now the fallback rather than the
                                # normal case: the retry above usually gets
                                # a real result, and only a booking that
                                # fails twice ends up deferred to
                                # job.warning.
                                continue

                        else:
                            # Could not get back in. ESPRESSO's auto_login can
                            # only return FILLED_AWAITING_MFA when the account
                            # demands MFA - there is no unattended way past
                            # that, and pretending otherwise would just
                            # produce a louder failure.
                            #
                            # THIS IS AN `else` DELIBERATELY. It used to be a
                            # bare fall-through after the recovered branch's
                            # `continue`, which meant a successful retry
                            # could not reach the recording code without
                            # landing in here and breaking the batch.
                            job.error = (
                                f"{job.cruise_line.value} signed out during the "
                                f"scan at booking {booking_id} and could not be "
                                f"logged back in automatically"
                                + (f" (auto-login said {status})" if status else "")
                                + f". Stopped with {len(job.results)} of "
                                f"{len(job.booking_ids)} bookings checked - click "
                                f"\"Check login\", complete the login, then Start "
                                f"again to do the rest."
                            )
                            logger.error("batch.session_recovery_gave_up",
                                         job_id=job.job_id, status=status)
                            break

                    if self._is_dead_browser_error(e):
                        logger.warning("batch.browser_dead_restarting", booking_id=booking_id)
                        try:
                            await scraper.stop()
                        except Exception:
                            pass
                        # `market=` is REQUIRED here too: a browser crash
                        # part-way through a Canada scan must not silently
                        # resume on the US account.
                        scraper = self._get_scraper(job.cruise_line, market=market)
                        scraper.raw_dump_dir = raw_dump_dir
                        scraper.capture_everything = capture_everything
                        scraper.on_action = on_action
                        try:
                            await scraper.start()
                            if keep_browser_open:
                                self._live_scraper = scraper
                        except Exception as restart_error:
                            # CONFIRMED REAL BUG, fixed 2026-08-13: this used
                            # to only log the failure and let the for-loop
                            # continue to the NEXT booking with `scraper`
                            # still pointing at an object whose start() never
                            # completed — every remaining booking would then
                            # fail with a generic "Scraper not started" error
                            # that _is_dead_browser_error can't recognize, so
                            # the restart path could never re-trigger for the
                            # rest of this batch. scraper.start() (see
                            # scraper/base.py) now cleans up its own partial
                            # state on failure, so there's no leaked process
                            # from this specific attempt — but the batch
                            # genuinely cannot continue without a working
                            # browser. Stop here rather than silently
                            # grinding through identical failures, and make
                            # sure a stale, never-started scraper is never
                            # left as the "live" one for a future scan to
                            # pick up.
                            logger.error("batch.browser_restart_failed", error=str(restart_error))
                            if keep_browser_open:
                                # Whatever self._live_scraper currently
                                # references (the old, already-.stop()'d
                                # dead scraper, or nothing) is not a working
                                # session — never leave a stale/dead
                                # reference for a future scan to mistake
                                # for a live one.
                                self._live_scraper = None
                            job.status = ScanJobStatus.FAILED
                            job.results.append(result)
                            job.progress_done = min(i + 1, job.progress_total)
                            break

                # CONFIRMED REAL BUG 2026-08-12: everything from here down
                # through on_progress() used to run with no exception
                # guard at all — a single DB write failure (e.g. SQLite
                # "database is locked" under concurrent writers), a cache
                # error, or an on_progress callback raising would propagate
                # out of this entire for-loop into the outer except at the
                # bottom of this function, marking the WHOLE job FAILED and
                # abandoning every remaining booking — directly violating
                # this project's own "one booking fails, the rest continue"
                # design intent (already honored for the scrape itself via
                # the try/except a few lines up). Each step below now fails
                # on its own without taking the batch down with it.
                # `capture_market_data` is a TELEMETRY switch - it gates
                # ESPRESSO's category-table snapshot, which is for later
                # analysis. NCL's booking-detail snapshot is different in
                # kind: those fields (Gross Due, Net Due, Commiss.Earned,
                # FINAL PAYMENT date) now DRIVE decisions - paid-in-full,
                # the collectable cap, the final-payment gate, commission -
                # so they must be recorded on every scan regardless of the
                # checkbox. Neon 2026-08-28: "make sure that our scanner
                # also scans the infromation and all the details o the
                # booking from now on". Not persisting them is exactly why
                # the re-audit could not tell that 3000049 and 3000052
                # were paid in full.
                _md = scraper.last_market_data
                _always = bool(_md) and _md.get("capture_type") == "ncl_booking_details"
                if (capture_market_data or _always) and _md:
                    try:
                        await self._save_market_data_to_db(result, scraper.last_market_data)
                    except Exception as e:
                        logger.error("batch.market_data_save_failed", booking_id=booking_id, error=str(e))

                if result.status == BookingStatus.ERROR:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                job.results.append(result)
                job.progress_done = min(i + 1, job.progress_total)

                # ESPRESSO states sailDate/shipCode/shipName in the booking
                # page's own embedded JSON. Read via the scraper's
                # read_feature_fields(), which matches them INSIDE the page
                # and returns ~200 bytes.
                #
                # It used to call page.content() here, pulling the whole DOM
                # over CDP. Real booking pages average 422 KB and the scan
                # already serialises that twice per booking for the two page
                # snapshots - so this made it three times, ~630 MB across a
                # 500-booking watchlist, to obtain six short strings.
                # Taken from what the scraper captured WHILE ON THE
                # BOOKING PAGE. Calling read_feature_fields() here instead
                # was the 2026-09-22 bug: check_booking ends with
                # release_booking(), which navigates away, so the read
                # landed on the wrong page and silently produced NULLs.
                feature_fields = getattr(scraper, "last_feature_fields", None)

                # The price DRIVERS for this scan, captured from whatever
                # the scraper already has in hand - no extra page loads and
                # no extra requests. Failure here must never cost the result
                # itself, so it is best-effort and the price row is written
                # either way.
                features = None
                try:
                    features = extract_booking_features(
                        result.cruise_line.value,
                        page_fields=feature_fields,
                        market_data=getattr(scraper, "last_market_data", None),
                        initial_data=getattr(scraper, "last_initial_data", None),
                    )
                except Exception as feat_error:
                    logger.warning("batch.feature_extract_failed",
                                   booking_id=booking_id,
                                   error=str(feat_error)[:200])

                # DID THE PRICE MOVE SINCE LAST TIME?
                #
                # Read BEFORE this scan's row is written, or the "previous"
                # total would be the one we just captured.
                #
                # price_history has kept a row per scan all along - 6,606 of
                # them - and nothing ever compared two consecutive rows. The
                # whole product exists to catch a price going down.
                try:
                    change = compare_price(
                        await self._previous_total(result), result.old_total)
                    if change.direction != "UNKNOWN":
                        logger.info("price_change", booking_id=booking_id,
                                    direction=change.direction,
                                    delta=change.delta,
                                    previous=change.previous,
                                    current=change.current)
                    if change.is_drop:
                        # Surfaced on the row itself so it stands out
                        # without anyone reading the log.
                        result.note = (f"PRICE DROP {abs(change.delta):,.2f} "
                                       f"since the last scan. "
                                       + (result.note or "")).strip()
                except Exception as exc:
                    logger.warning("price_change.failed", booking_id=booking_id,
                                   error=str(exc)[:200])

                # Persist result
                try:
                    await self._save_result_to_db(result)
                    await self._save_price_history(result, features)
                    persisted = True
                except Exception as e:
                    persisted = False
                    # CONFIRMED GAP, fixed 2026-08-26: this used to log only
                    # booking_id + error, so a real OPTIMIZATION whose INSERT
                    # failed (SQLite lock, disk full) left no recoverable
                    # record of WHAT was lost. The CLI/GUI still have it in
                    # job.results, but the API path relies purely on the DB.
                    # Log the whole finding so it can be re-entered by hand.
                    logger.error(
                        "batch.persist_failed", booking_id=booking_id, error=str(e),
                        status=result.status.value, net_saving=result.net_saving,
                        old_total=result.old_total, new_total=result.new_total,
                        price_category=result.price_category, note=result.note,
                    )

                # Cache NO_SAVING results (skipped in bypass mode — see above).
                #
                # MOVED BELOW THE PERSIST, 2026-08-26: this used to run BEFORE
                # the DB write, so if the write failed the cache entry still
                # survived — suppressing the booking for the full 12h TTL
                # while there was no DB record of it at all. The cache was
                # actively protecting a hole in the data. Only cache a result
                # that actually made it to disk.
                #
                # AND gated on `old_total > 0`: ESPRESSO's
                # make_skip_reprice_result() returns status=NO_SAVING for a
                # booking whose price was never read at ALL (a
                # "price program change not allowed" restriction). 862 of the
                # 2,345 NO_SAVING rows in the real DB are this class — every
                # one with old_total=0. Caching those as "checked, no saving"
                # suppressed a booking for 12h on the basis of a comparison
                # that never happened. A real NO_SAVING always has a real
                # old_total to compare against.
                # A CONFIRMED PAID-IN-FULL BOOKING IS EXCLUDED FOR GOOD.
                #
                # Written only when the scraper actually READ the payment
                # panel. ExclusionService refuses otherwise, and that guard
                # is the whole safety story: "we could not see the balance"
                # must never become "it owes nothing". That confusion is
                # what reported a $400 saving on booking 3001001, which had
                # two cents outstanding.
                #
                # Cancelled bookings cannot reach here - is_cancelled() runs
                # BEFORE the payment panel is read, so a CX reservation
                # (which displays Final Payment Due 0.00) returns CANCELLED.
                if result.status == BookingStatus.CANCELLED and persisted:
                    # Same rule as paid in full, added on Neon's
                    # instruction 2026-09-29: "add canceled as the same rule
                    # case as paid in full ... to save resoursces and not
                    # doing useless scans".
                    try:
                        await self.exclusions.record_cancelled(
                            job.cruise_line.value, booking_id,
                            detail=(result.note or "")[:200])
                    except Exception as exc:
                        logger.warning("batch.exclusion_record_failed",
                                       booking_id=booking_id,
                                       error=str(exc)[:200])

                if result.status == BookingStatus.PAID_IN_FULL and persisted:
                    payment = getattr(scraper, "last_payment_status", None) or {}
                    try:
                        await self.exclusions.record_paid_in_full(
                            job.cruise_line.value, booking_id,
                            total_price=payment.get("total_price")
                            or (result.old_total or None),
                            final_payment_due=payment.get("final_payment_due"),
                            payment_state_readable=bool(
                                payment.get("payment_state_readable")),
                            currency=payment.get("currency") or result.currency,
                        )
                    except Exception as exc:
                        logger.warning("batch.exclusion_record_failed",
                                       booking_id=booking_id,
                                       error=str(exc)[:200])

                # REMEMBER EVERY CACHEABLE OUTCOME, NOT JUST NO_SAVING.
                #
                # The old gate was `status == NO_SAVING`, which is why 41%
                # of a day's scanning was redundant: PAID_IN_FULL (439
                # repeats), WLT (133), TRAP and NOT_ON_THIS_ACCOUNT were
                # never remembered and got re-opened every single run.
                #
                # CacheService.is_cacheable keeps OPTIMIZATION, ERROR,
                # CANCELLED and UNKNOWN out - a live saving must always be
                # re-confirmed, a failure is not an outcome, and every
                # cancellation must be reported on every run.
                if not bypass_cache and persisted:
                    try:
                        await self.cache.set_result(
                            job.cruise_line.value, booking_id,
                            status=result.status.value,
                            payload={
                                "old_total": result.old_total,
                                "new_total": result.new_total,
                                "net_saving": result.net_saving,
                                "price_category": result.price_category,
                                "currency": result.currency,
                            },
                        )
                    except Exception as e:
                        logger.error("batch.cache_save_failed",
                                     booking_id=booking_id, error=str(e))

                if on_progress:
                    try:
                        on_progress(job)
                    except Exception as e:
                        logger.error("batch.on_progress_failed", booking_id=booking_id, error=str(e))

                # A burst of failures usually means the portal session/token
                # state needs time to recover, not faster retries.
                if consecutive_failures >= settings.scraper_cooldown_after_failures:
                    logger.warning(
                        "batch.cooldown",
                        consecutive_failures=consecutive_failures,
                        cooldown_s=settings.scraper_cooldown_seconds,
                    )
                    await asyncio.sleep(settings.scraper_cooldown_seconds)
                    consecutive_failures = 0
                else:
                    # Randomized pacing between bookings — a real agent
                    # doesn't click through reservations every 0.5s.
                    await asyncio.sleep(random.uniform(
                        settings.scraper_interbooking_delay_min_s,
                        settings.scraper_interbooking_delay_max_s,
                    ))

                # QUEUE THE RETRY PASS, once, after the last ORIGINAL
                # booking. Appending to `work` feeds these ids back through
                # this same loop (see the comment at `work =` above).
                #
                # Done at the END rather than inline because the failures
                # worth retrying are mostly transient session and timeout
                # faults, and the minutes spent on the rest of the queue
                # are exactly what lets them clear. Retrying immediately
                # would hit the same dead session.
                if (not retry_queued
                        and i == len(job.booking_ids) - 1
                        and not self._stop_flags.get(job.job_id)):
                    retry_queued = True
                    retries = self._bookings_to_retry(job)
                    if retries:
                        work.extend(retries)
                        logger.info("batch.retry_pass", job_id=job.job_id,
                                    count=len(retries))

            # Also excludes FAILED now (2026-08-13 fix) — a batch that broke
            # out of the loop above because the browser restart failed must
            # stay FAILED, not be silently overwritten back to COMPLETED
            # just because the for-loop exited without raising.
            if job.status not in (ScanJobStatus.STOPPED, ScanJobStatus.FAILED):
                job.status = ScanJobStatus.COMPLETED

        except Exception as e:
            logger.error("batch.fatal", job_id=job.job_id, error=str(e))
            job.status = ScanJobStatus.FAILED
            if keep_browser_open:
                # The live session may be in a broken state after a fatal
                # error — drop it so the next scan starts a clean one
                # rather than silently reusing something broken.
                await self.close_live_scraper()

        finally:
            # CONFIRMED REAL CORRUPTION, fixed 2026-08-26: these statements
            # used to run bare, so if `scraper.stop()` raised (a dead browser
            # or a Playwright teardown error — exactly the situation this
            # path exists to clean up after), NOTHING below it ran: the job
            # was never marked complete/failed in the DB and its stop flag
            # was never popped. The live DB shows the damage — 18 of 52
            # scan_jobs rows are stuck at status='RUNNING', progress_done=0,
            # completed_at=NULL, including one with progress_total=278. A
            # stuck-RUNNING job also makes the GUI poll forever (it waits on
            # PENDING/RUNNING). Each step is now independently guarded so
            # the job status and the stop-flag cleanup ALWAYS happen.
            # `scraper` is None if acquisition itself failed (see the note
            # where it's now acquired inside the try).
            if not keep_browser_open and scraper is not None:
                try:
                    await scraper.stop()
                except Exception as e:
                    logger.warning("batch.scraper_stop_failed", job_id=job.job_id, error=str(e))
            try:
                job.completed_at = datetime.utcnow()
                job.current_booking_id = None
                await self._update_job_in_db(job)
            except Exception as e:
                logger.error("batch.job_status_update_failed", job_id=job.job_id, error=str(e))
            self._stop_flags.pop(job.job_id, None)
            # ONE LINE THAT SAYS WHETHER THE RUN WAS HEALTHY. See
            # run_summary - this used to carry three fields and could not
            # answer that. Guarded: a summary must never be the thing that
            # breaks a finally block.
            try:
                detail = run_summary(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("batch.summary_failed", job_id=job.job_id,
                               error=str(exc)[:200])
                detail = {}
            logger.info(
                "batch.complete",
                job_id=job.job_id,
                status=job.status.value,
                total=len(job.results),
                **detail,
            )

    async def stop_scan(self, job_id: str) -> bool:
        """Signal a running scan to stop after the current booking."""
        if job_id in self._stop_flags:
            self._stop_flags[job_id] = True
            logger.info("batch.stop_requested", job_id=job_id)
            return True
        return False

    def get_job(self, job_id: str) -> ScanJob | None:
        """Get a scan job by ID (in-memory)."""
        return self._active_jobs.get(job_id)

    async def get_all_bookings(
        self, cruise_line: str | None = None, limit: int = 100,
    ) -> list[dict]:
        """Fetch all booking records from the database."""
        async with async_session() as session:
            query = select(BookingRecord).order_by(BookingRecord.created_at.desc()).limit(limit)
            if cruise_line:
                query = query.where(BookingRecord.cruise_line == cruise_line)
            result = await session.execute(query)
            records = result.scalars().all()
            return [
                {
                    "booking_id": r.booking_id,
                    "cruise_line": r.cruise_line,
                    "status": r.status,
                    "net_saving": r.net_saving,
                    "old_total": r.old_total,
                    "new_total": r.new_total,
                    "confidence": r.confidence,
                    "price_category": r.price_category,
                    "new_price_category": r.new_price_category,
                    "note": r.note,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    async def get_bookings_by_id(self, booking_id: str) -> list[dict]:
        """Fetch every check result for one specific booking ID.

        CONFIRMED REAL BUG, fixed 2026-08-13: the API route this backs
        used to call get_all_bookings() (limit=100, no cruise_line
        filter, ordered by created_at desc) and filter the result
        CLIENT-SIDE for a matching booking_id. Once the `bookings` table
        grows past 100 total rows since a given booking was last
        checked, that booking silently falls outside the 100-row window
        and the route 404s a real, previously-checked booking. Queries
        directly by booking_id instead — same correct pattern
        get_price_history (just below) already used for PriceHistory."""
        async with async_session() as session:
            result = await session.execute(
                select(BookingRecord)
                .where(BookingRecord.booking_id == booking_id)
                .order_by(BookingRecord.created_at.desc())
            )
            records = result.scalars().all()
            return [
                {
                    "booking_id": r.booking_id,
                    "cruise_line": r.cruise_line,
                    "status": r.status,
                    "net_saving": r.net_saving,
                    "old_total": r.old_total,
                    "new_total": r.new_total,
                    "confidence": r.confidence,
                    "price_category": r.price_category,
                    "new_price_category": r.new_price_category,
                    "note": r.note,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    async def get_price_history(self, booking_id: str) -> list[dict]:
        """Fetch price history for a booking."""
        async with async_session() as session:
            result = await session.execute(
                select(PriceHistory)
                .where(PriceHistory.booking_id == booking_id)
                .order_by(PriceHistory.checked_at.asc())
            )
            records = result.scalars().all()
            return [
                {
                    "total": r.total,
                    "category": r.category,
                    "cruise_line": r.cruise_line,
                    "checked_at": r.checked_at.isoformat() if r.checked_at else None,
                }
                for r in records
            ]

    # ── DB Persistence ──────────────────────────────────────────

    async def _save_result_to_db(self, result: BookingResult) -> None:
        """Save a booking result to the database."""
        import json
        async with async_session() as session:
            record = BookingRecord(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                status=result.status.value,
                old_total=result.old_total,
                new_total=result.new_total,
                net_saving=result.net_saving,
                confidence=result.confidence,
                price_category=result.price_category,
                new_price_category=result.new_price_category,
                note=result.note,
                error=result.error,
                lost_pkg_names=json.dumps(result.lost_pkg_names),
                # ADDED 2026-08-27: these 14 fields were computed on every
                # single check and thrown away here, leaving the system
                # unable to audit its own money decisions. `obc_change` in
                # particular is what the OBC rule turns on, and
                # old_promos/new_promos exist specifically to make a
                # LATRIPLE TRAP verdict auditable. See BookingRecord.
                price_drop=result.price_drop,
                obc_change=result.obc_change,
                lost_pkg_value=result.lost_pkg_value,
                currency=result.currency,
                old_promos=result.old_promos,
                new_promos=result.new_promos,
                lost_fares=json.dumps(result.lost_fares),
                re_addable_fares=json.dumps(result.re_addable_fares),
                gained_fares=json.dumps(result.gained_fares),
                lost_travel_protection=json.dumps(result.lost_travel_protection),
                # COLLECTED DATA ONLY — see BookingRecord's commission note.
                # Stored, never read by any saving or status logic.
                commission_rate=result.commission_rate,
                commission_earned=result.commission_earned,
                commission_due=result.commission_due,
                old_cruise_fare=result.old_cruise_fare,
                new_cruise_fare=result.new_cruise_fare,
                fare_change_pct=result.fare_change_pct,
            )
            session.add(record)
            await session.commit()

    async def _previous_total(self, result: BookingResult) -> float | None:
        """This booking's total at the PREVIOUS scan, or None if first seen.

        One indexed lookup on (booking_id, cruise_line) ordered by
        checked_at. Returns None rather than 0.0 when there is no history -
        "never scanned" and "cost nothing" are different facts.
        """
        try:
            async with async_session() as session:
                rows = await session.execute(
                    select(PriceHistory.total)
                    .where(PriceHistory.booking_id == result.booking_id,
                           PriceHistory.cruise_line == result.cruise_line.value)
                    .order_by(PriceHistory.checked_at.desc())
                    .limit(1))
                value = rows.scalar_one_or_none()
                return float(value) if value is not None else None
        except Exception as exc:
            logger.warning("price_change.lookup_failed",
                           booking_id=result.booking_id, error=str(exc)[:200])
            return None

    async def _save_price_history(self, result: BookingResult,
                                  features=None) -> None:
        """Record a price snapshot, with the drivers that move the price.

        `features` is a core.booking_features.BookingFeatures captured from
        the same scan. It is OPTIONAL and every field inside it is nullable:
        a scan that could not read a sail date writes NULL, never a 0 that a
        model would read as "sails today".

        WIDENED 2026-09-21. Until now this stored price, category and a
        timestamp - and a drop-prediction model built on 5,067 such rows
        scored 0.549 AUC on a temporal split once scan-cadence features were
        removed, because the actual drivers (days to sailing above all) were
        nowhere in the table. Capturing them from now on is what makes the
        question answerable later; see core/booking_features.py.
        """
        if result.old_total <= 0:
            return
        row = dict(
            booking_id=result.booking_id,
            cruise_line=result.cruise_line.value,
            total=result.old_total,
            category=result.price_category,
        )
        if features is not None:
            try:
                row.update({k: v for k, v in features.as_row().items()
                            if v is not None})
            except Exception as exc:
                # Never let feature capture cost us the price row itself -
                # the price is the thing we cannot re-derive later.
                logger.warning("price_history.features_failed",
                               booking_id=result.booking_id, error=str(exc)[:200])
        async with async_session() as session:
            session.add(PriceHistory(**row))
            await session.commit()
        # One line per stored booking, for scan_watchdog's feature monitor.
        # ESPRESSO's driver fields come from a regex over its Angular
        # bootstrap, so a portal restructure would make them silently NULL
        # rather than raise - this is what makes that visible while the scan
        # is still running.
        logger.info("price_history.features",
                    booking_id=result.booking_id,
                    cruise_line=result.cruise_line.value,
                    sail_date=row.get("sail_date"),
                    days_to_sailing=row.get("days_to_sailing"))

    async def _save_market_data_to_db(self, result: BookingResult, market_data: dict) -> None:
        """Persist read-only market/category snapshot data."""
        import json

        capture_types = {
            CruiseLine.ESPRESSO: "espresso_category_table",
            CruiseLine.NCL: "ncl_category_table",
            CruiseLine.GOCCL: "goccl_offer_code_comparison",
        }

        # HONOUR THE SCRAPER'S OWN LABEL, fixed 2026-09-16. This function
        # assumed every capture looks like ESPRESSO's - a category table
        # under "rows" - and forced the cruise line's default label on top.
        # NCL's capture has NO "rows" key at all: it carries the payment
        # state. So all 135 NCL rows written in the 2026-09-16 run were
        # empty AND mislabelled "ncl_category_table" when the scraper had
        # already said "ncl_booking_details".
        capture_type = (market_data.get("capture_type")
                        or capture_types.get(result.cruise_line, "category_table"))

        # Keep the WHOLE payload. The fields being dropped here -
        # final_payment_date, amount_due, commission_rate, inside NCL's
        # "derived" - are exactly the ones needed to rank a booking by
        # urgency and to warn that a finding is about to expire. Their
        # absence is what let $3,945 of found savings lapse during an
        # 18-day scan gap: nothing could tell which findings were about to
        # become unoptimizable. Storing the raw capture means a future
        # question can be answered from history instead of another live run.
        async with async_session() as session:
            session.add(MarketDataRecord(
                booking_id=result.booking_id,
                cruise_line=result.cruise_line.value,
                capture_type=capture_type,
                current_category=market_data.get("currentCategory"),
                execution_token=market_data.get("executionToken"),
                selection_json=market_data.get("selectionJSON"),
                category_table_json=json.dumps(market_data.get("rows", []), ensure_ascii=False),
                payload_json=json.dumps(market_data, ensure_ascii=False, default=str),
            ))
            await session.commit()

    async def _save_job_to_db(self, job: ScanJob) -> None:
        """Save a new scan job."""
        import json
        async with async_session() as session:
            record = ScanJobRecord(
                job_id=job.job_id,
                booking_ids_json=json.dumps(job.booking_ids),
                cruise_line=job.cruise_line.value,
                status=job.status.value,
                progress_total=job.progress_total,
                started_at=job.started_at,
                signature=getattr(job, "signature", None),
            )
            session.add(record)
            await session.commit()

    #: Error text that has NEVER recovered on a re-scan, so retrying it is
    #: pure waste. Measured 2026-10-01 across all 514 ERROR rows: 88% of
    #: errors were followed by a successful scan of the same booking, but
    #: these were 0 of 9.
    _RETRY_NEVER = (
        "payment panel unreadable",
    )

    #: At most this many bookings are retried at the end of a run. A scan
    #: where hundreds failed has something systemically wrong - a dead
    #: session, a portal outage - and grinding through a second full pass
    #: would double the damage rather than fix it.
    _RETRY_MAX = 60

    def is_retryable_error(self, error: str | None) -> bool:
        """Whether a failed booking is worth a second attempt.

        MEASURED, not assumed. Across all 514 ERROR rows in the database,
        456 (88%) were followed by a SUCCESSFUL scan of the same booking -
        so the failure was transient and a retry would have worked:

            Page.wait_for_selector timeout 60000ms   117   100% recovered
            Cannot read categories: VX._form_12       58   100%
            Session logged out while searching        45   100%
            Page.wait_for_selector timeout 12000ms    28   100%
            NCL portal error: Reservation not found   20   100%
            payment panel unreadable                   9     0%  <- never

        "Payment panel unreadable" is the one that never recovers, and it
        is also the one where guessing is dangerous - it is the exact
        condition that produced the false $400 on booking 3001001. Left
        out deliberately.
        """
        if not error:
            return False
        text = str(error).lower()
        return not any(never in text for never in self._RETRY_NEVER)

    def _bookings_to_retry(self, job: ScanJob) -> list[str]:
        """Bookings that errored and are worth one more attempt.

        Only bookings whose LATEST result is an ERROR - one that already
        succeeded on a session-recovery retry must not be scanned a third
        time. Order follows the queue, and the list is capped (_RETRY_MAX).
        """
        latest: dict[str, object] = {}
        for result in job.results:
            latest[str(result.booking_id)] = result

        out = [
            booking_id for booking_id, result in latest.items()
            if getattr(result.status, "value", result.status) == "ERROR"
            and self.is_retryable_error(getattr(result, "error", None))
        ]
        if len(out) > self._RETRY_MAX:
            logger.warning("batch.retry_capped", job_id=job.job_id,
                           failed=len(out), retrying=self._RETRY_MAX)
            out = out[:self._RETRY_MAX]
        return out

    async def recent_identical_scan(self, cruise_line: str, booking_ids,
                                    *, bypass_cache: bool = False,
                                    within_hours: float | None = None) -> dict | None:
        """The last COMPLETED run of this exact request, if it is recent.

        Neon 2026-10-01: *"it is the same list it should not scan again it
        should gave me the same results ... at least 2 hours."*

        Returns None when a scan should go ahead, or a dict describing the
        previous run when it should be reused:

            {"job_id", "scanned_at", "age_hours", "next_allowed_at",
             "minutes_remaining", "bookings"}

        WHAT WILL NOT SUPPRESS A SCAN, deliberately:

          * a FAILED, STOPPED or still-RUNNING job. An interrupted run is
            not an answer, and reusing one would hide exactly the work
            that needs redoing (see resumable_jobs).
          * a DIFFERENT booking set. Order and duplicates do not count as
            different; membership does.
          * a different cruise line, or "Force live recheck" - that flag is
            part of the signature, so a forced re-check can never be
            suppressed by an ordinary scan.

        Never raises. A failure to answer means "go ahead and scan": a
        redundant scan costs time, a wrongly suppressed one costs a real
        client's price drop.
        """
        window = (settings.scan_suppression_hours
                  if within_hours is None else within_hours)
        if not window or window <= 0:
            return None

        signature = scan_signature(cruise_line, booking_ids,
                                   bypass_cache=bypass_cache)
        cutoff = datetime.utcnow() - timedelta(hours=window)
        try:
            async with async_session() as session:
                row = (await session.execute(
                    select(ScanJobRecord).where(
                        ScanJobRecord.signature == signature,
                        ScanJobRecord.status == ScanJobStatus.COMPLETED.value,
                        ScanJobRecord.completed_at.isnot(None),
                        ScanJobRecord.completed_at >= cutoff,
                    ).order_by(ScanJobRecord.completed_at.desc()).limit(1)
                )).scalars().first()
        except Exception as exc:  # noqa: BLE001 - fail OPEN, see docstring
            logger.warning("scan.suppression_lookup_failed",
                           error=str(exc)[:200])
            return None

        if row is None:
            return None

        finished = row.completed_at
        next_allowed = finished + timedelta(hours=window)
        remaining = (next_allowed - datetime.utcnow()).total_seconds()
        return {
            "job_id": row.job_id,
            "scanned_at": finished,
            "age_hours": round(
                (datetime.utcnow() - finished).total_seconds() / 3600, 2),
            "next_allowed_at": next_allowed,
            "minutes_remaining": max(0, int(round(remaining / 60))),
            "bookings": row.progress_total or 0,
        }

    async def scan_plan(self, cruise_line: str, booking_ids,
                        *, bypass_cache: bool = False) -> dict:
        """What pressing Start would actually do, before it does it.

        Lets the GUI tell the operator the truth up front rather than
        opening a browser and leaving them to guess - Neon's standing
        complaint that he could not tell whether a booking was really
        rescanned.

        Returns `{"action": "reuse"|"scan", ...}` and, when scanning, how
        the request differs from the most recent one on this line.
        """
        suppressed = await self.recent_identical_scan(
            cruise_line, booking_ids, bypass_cache=bypass_cache)
        if suppressed:
            return {"action": "reuse", "reason": "identical_scan_recently",
                    **suppressed}

        overlap = None
        try:
            async with async_session() as session:
                row = (await session.execute(
                    select(ScanJobRecord).where(
                        ScanJobRecord.cruise_line == cruise_line,
                        ScanJobRecord.status == ScanJobStatus.COMPLETED.value,
                    ).order_by(ScanJobRecord.completed_at.desc()).limit(1)
                )).scalars().first()
            if row is not None:
                import json as _json
                overlap = describe_overlap(
                    _json.loads(row.booking_ids_json or "[]"), booking_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scan.plan_overlap_failed", error=str(exc)[:200])

        return {"action": "scan", "overlap": overlap,
                "requested": len(set(str(b).strip() for b in booking_ids or []
                                     if str(b).strip()))}

    async def resumable_jobs(self, cruise_line: str | None = None,
                             max_age_hours: float = 72.0) -> list[dict]:
        """Interrupted jobs that still have bookings left to scan.

        A job qualifies when it did not reach COMPLETED and some of its
        booking ids have no recorded result. RUNNING counts: a job the
        process died inside is left RUNNING forever until
        reconcile_stale_jobs gets to it, and that is precisely the case
        worth resuming.

        Never raises - a failure to OFFER a resume must not stop a scan
        being started normally.
        """
        import json
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        try:
            async with async_session() as session:
                query = select(ScanJobRecord).where(
                    ScanJobRecord.status != ScanJobStatus.COMPLETED.value,
                    ScanJobRecord.started_at >= cutoff,
                ).order_by(ScanJobRecord.started_at.desc())
                if cruise_line:
                    query = query.where(ScanJobRecord.cruise_line == cruise_line)
                records = (await session.execute(query)).scalars().all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("batch.resumable_lookup_failed", error=str(exc)[:200])
            return []

        out: list[dict] = []
        for record in records:
            try:
                all_ids = json.loads(record.booking_ids_json or "[]")
            except ValueError:
                continue
            done = set(record.completed_ids)
            remaining = [b for b in all_ids if b not in done]
            if not remaining:
                continue
            out.append({
                "job_id": record.job_id,
                "cruise_line": record.cruise_line,
                "status": record.status,
                "started_at": record.started_at,
                "total": len(all_ids),
                "done": len(all_ids) - len(remaining),
                "remaining": remaining,
            })
        return out

    async def remaining_for(self, job_id: str) -> list[str]:
        """The bookings of one job that still have no result.

        Returns [] for an unknown job rather than raising - the caller is
        usually about to decide whether to offer a resume.
        """
        for job in await self.resumable_jobs(max_age_hours=24 * 365):
            if job["job_id"] == job_id:
                return job["remaining"]
        return []

    async def reconcile_stale_jobs(self, max_age_hours: float = 12.0) -> int:
        """Mark abandoned scan_jobs rows FAILED instead of RUNNING forever.

        CONFIRMED REAL CORRUPTION, quantified 2026-08-27: the live DB holds
        **24 rows stuck at status='RUNNING'** with progress_done=0 and
        completed_at=NULL, the oldest from 2026-07-19 and one with
        progress_total=623. `_update_job_in_db` itself is correct — the rows
        are stuck because the OWNING PROCESS DIED (window closed mid-scan,
        Ctrl+C, a `timeout` kill), so the `finally` that would have written
        the terminal status never ran. Nothing ever reconciled them
        afterwards.

        Why it matters beyond tidiness: a RUNNING row that no process owns
        is a lie about the system's state, `run_persistent_watchlist_scan.py`
        reasons about scan_jobs when deciding what to resume, and the GUI
        polls while a job reads PENDING/RUNNING.

        Deliberately AGE-BASED, not "anything RUNNING at startup". Multiple
        processes legitimately coexist here — right now there are two live
        `gui.main` processes — so blanket-failing every RUNNING row on
        startup would kill a healthy concurrent scan. 12h is far beyond any
        real run (today's 506-booking ESPRESSO scan took 2h49m) while still
        catching every one of the 24.
        """
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        async with async_session() as session:
            result = await session.execute(
                select(ScanJobRecord).where(
                    ScanJobRecord.status.in_(("RUNNING", "PENDING")),
                    ScanJobRecord.started_at < cutoff,
                )
            )
            stale = result.scalars().all()
            for record in stale:
                record.status = ScanJobStatus.FAILED.value
                record.completed_at = datetime.utcnow()
            if stale:
                await session.commit()
            return len(stale)

    async def _update_job_in_db(self, job: ScanJob) -> None:
        """Update a scan job's status, progress and completed-booking list.

        ALSO RECORDS WHICH BOOKINGS FINISHED, as of 2026-10-01, so an
        interrupted run can resume. The ids are derived from job.results
        rather than tracked separately - there are five places that append
        a result, and a sixth would otherwise silently stop being counted.
        """
        import json

        completed = []
        seen = set()
        for result in job.results:
            booking_id = str(getattr(result, "booking_id", "") or "")
            if booking_id and booking_id not in seen:
                seen.add(booking_id)
                completed.append(booking_id)

        async with async_session() as session:
            result = await session.execute(
                select(ScanJobRecord).where(ScanJobRecord.job_id == job.job_id)
            )
            record = result.scalar_one_or_none()
            if record:
                record.status = job.status.value
                record.progress_done = job.progress_done
                record.completed_at = job.completed_at
                record.completed_ids_json = json.dumps(completed)
                await session.commit()

    async def _checkpoint(self, job: ScanJob) -> None:
        """Write progress mid-run. Never raises.

        THE BUG THIS FIXES. `_update_job_in_db` was called exactly ONCE, in
        _run_batch's `finally`. A hard death - the process killed, a crash,
        the machine going down - wrote nothing at all, and
        reconcile_stale_jobs then marked the row FAILED with
        progress_done = 0.

        Measured 2026-10-01 against each job's own booking list: rows
        recorded as "0 of 723" had really scanned 530, "0 of 721" had
        scanned 559, and one NCL job that completed all 189 of its
        bookings was on record as a total failure. The 43% completion
        figure in the roadmap was measuring bookkeeping, not work.

        One small UPDATE per booking against a booking that already costs
        seconds of browser time is not worth batching, and batching is
        exactly what loses the last N on a crash.
        """
        try:
            await self._update_job_in_db(job)
        except Exception as exc:  # noqa: BLE001 - a checkpoint must never
            # take down the scan it is only trying to record.
            logger.warning("batch.checkpoint_failed", job_id=job.job_id,
                           error=str(exc)[:200])
