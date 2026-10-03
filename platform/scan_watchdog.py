"""A third eye on a running scan.

Neon 2026-09-21: "i want an eye on the script code project that checks that
everything is working as expected a third eye after me you to moniotor the
scraping crawling".

WHY THIS AND NOT SPIDERMON. Spidermon is the obvious reference - it is the
Scrapy team's own monitoring extension and its patterns are the right ones
(stats thresholds, run-over-run comparison, periodic in-run monitors,
alerting). But it hooks Scrapy's signals and stats collector, and this
project is Playwright + PySide6 with no Scrapy anywhere, so it cannot be
installed. Its PATTERNS are reimplemented here instead.

WHAT WAS ALREADY COVERED, and is deliberately not duplicated:
    run_health.py            canaries, status mix, dead weight - POST-HOC,
                             reads the DB after a run
    BaseScraper.check_structure_drift
                             ARIA-tree baseline comparison, per session

THE GAP THIS FILLS: nothing watched a scan WHILE IT RAN. A 500-booking
ESPRESSO run takes hours, and the recorded failures all had a signature
visible long before the end - bookings #400-403 dying in sequence to a
logout, NCL's paid-in-full share jumping from 14% to 79%, a portal
restructure turning every booking into a selector timeout.

Every monitor below exists because of a REAL incident in this project's
history, not because it sounded prudent. It reads the structured JSON log
(utils.logging, data/cruiseintel.log), so it watches whatever is running right
now without being wired into it - no code change to the scan, no shared
state, and nothing it can break.

    python scan_watchdog.py              # follow the live log
    python scan_watchdog.py --once       # one pass over what is there
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOG = Path(__file__).with_name("data") / "cruiseintel.log"

# Mirrors utils.logging.CRASH_EVENT. Deliberately duplicated rather than
# imported: this script stays runnable with nothing but the stdlib, so it
# can watch a run that is failing to start. Kept in step by a test.
CRASH_EVENT = "crash.unhandled"

# What the GUI's command line looks like, for finding the process to watch
# when no --pid was given.
_SCAN_CMDLINE_MARKER = "gui.main"


@dataclass
class Alert:
    level: str          # WARN | ALARM
    monitor: str
    message: str

    def __str__(self) -> str:
        mark = "!!" if self.level == "ALARM" else " ~"
        return f"{mark} [{self.monitor}] {self.message}"


@dataclass
class ScanState:
    """What the watchdog has seen so far in this run."""
    results: Counter = field(default_factory=Counter)
    events: Counter = field(default_factory=Counter)
    recent_statuses: deque = field(default_factory=lambda: deque(maxlen=40))
    advisories: Counter = field(default_factory=Counter)
    timings_ms: list = field(default_factory=list)
    feature_rows: int = 0
    feature_nulls: int = 0
    last_progress: float = field(default_factory=time.monotonic)
    booking_count: int = 0
    # Bookings STARTED, from batch.checking. Counted separately from
    # booking_count (bookings that reported a result) because the two
    # diverge badly - see consume(). 2026-09-23: a live ESPRESSO run had
    # 458 started against 72 results.
    started_count: int = 0
    # Unhandled exceptions, newest last. Kept with their detail rather than
    # counted, because the whole point is being able to work out what broke
    # without going back to the raw log.
    crashes: list = field(default_factory=list)
    # Navigations, and how many needed a retry. Retries RECOVER, so they
    # never reach a result or an error - they only cost time.
    nav_attempts: int = 0
    nav_retries: int = 0
    # The process being watched, if one was given or found. None means the
    # watchdog is watching the log only and knows nothing about liveness.
    watched_pid: int | None = None
    process_gone: bool = False
    fired: set = field(default_factory=set)
    # Runs seen so far, and the all-time totals that survive a reset. The
    # per-run figures are what the monitors judge; these are kept so a
    # long-lived watchdog can still say what it has seen overall.
    window_hung: bool = False
    hang_stack: str = ""
    runs_seen: int = 0
    lifetime_started: int = 0
    lifetime_results: Counter = field(default_factory=Counter)

    def reset_for_new_run(self, count_run: bool = True) -> None:
        """Start the per-run counters over when a new batch begins.

        Everything a monitor THRESHOLDS on is reset; everything that is a
        standing fact about the process is not. Crashes and process death
        deliberately survive - a crash earlier in the session is still worth
        knowing about, and the process either died or it did not.
        """
        if count_run:
            self.runs_seen += 1
        self.lifetime_started += self.started_count
        self.lifetime_results.update(self.results)
        self.results = Counter()
        self.recent_statuses.clear()
        self.advisories = Counter()
        self.timings_ms = []
        self.feature_rows = 0
        self.feature_nulls = 0
        self.booking_count = 0
        self.started_count = 0
        self.nav_attempts = 0
        self.nav_retries = 0
        # m_session_health and m_structure_drift threshold on these. Leaving
        # them was what kept a "re-login FAILED - the scan stopped" ALARM on
        # screen five days after the scan in question had finished.
        self.events = Counter()
        # Alerts must be able to fire again for the new run - otherwise the
        # first run's cancellation alarm would suppress the second run's.
        self.fired = set()
        self.last_progress = time.monotonic()


# ── the monitors ─────────────────────────────────────────────────────────
#
# Each returns a list of Alerts. A monitor must be cheap and must never
# raise: a watchdog that crashes the thing it watches is worse than none.


def m_crashes(s: ScanState) -> list[Alert]:
    """An unhandled exception. ALARM on the first one, with enough to act on.

    THE INCIDENT this exists for: on 2026-09-22 a RuntimeError destroyed a
    running 721-booking scan overnight. It was printed to stdout and never
    logged, so nothing saw it and the dead scan sat until 14:14 the next
    day. utils.logging now records crashes as `crash.unhandled`; this is
    the half that reacts to them.

    Like m_cancellations, this does not wait for a population. One crash is
    one thing gone wrong, not a statistical signal - and unlike a booking
    outcome it will not show up anywhere else.
    """
    if not s.crashes:
        return []
    newest = s.crashes[-1]
    where = newest.get("source") or "?"
    kind = newest.get("error_type") or "error"
    detail = (newest.get("error") or "").strip()
    extra = f" in {newest['task']}" if newest.get("task") else ""
    more = f"  (+{len(s.crashes) - 1} earlier)" if len(s.crashes) > 1 else ""
    return [Alert("ALARM", "crash",
                  f"{kind} from {where}{extra}: {detail[:160]}{more}\n"
                  f"     full traceback in the log - "
                  f"grep '\"event\": \"crash.unhandled\"' data/cruiseintel.log")]


def m_process(s: ScanState) -> list[Alert]:
    """Is the thing we are watching actually still alive?

    A log going quiet is ambiguous - finished, paused, hung, or dead all
    look identical from the outside. Until 2026-09-23 the watchdog had no
    way to tell, so an overnight crash read exactly like a healthy idle
    run. psutil (already a dependency) resolves it.
    """
    if s.watched_pid is None or not s.process_gone:
        return []
    # A DELIBERATE SHUTDOWN IS NOT A FAILURE.
    #
    # Neon 2026-09-29: "notification keeps firing althouhgh the gui is
    # closed". It fired BECAUSE the GUI was closed - the process vanished
    # and this called that an ALARM. Closing an application is the most
    # ordinary thing an operator does, and a monitor that shouts about it
    # teaches you to ignore it.
    #
    # The GUI announces its own exit (`gui.shutdown_complete`, and
    # `single_instance.released` when it hands back its lock). If either is
    # in the log, the process ending is expected and says nothing.
    if s.events.get("gui.shutdown_complete") or s.events.get(
            "single_instance.released"):
        return []
    return [Alert("ALARM", "process",
                  f"the scan process (PID {s.watched_pid}) is GONE and did "
                  f"not shut down cleanly - it crashed or was killed. "
                  f"Anything still queued did not run.")]


def claim_single_instance() -> bool:
    """Refuse to start if another watchdog is already running.

    THE PROBLEM, 2026-09-29. Stopping a backgrounded watchdog kills the task
    wrapper but can leave the Python process alive, so restarts quietly
    stacked up - at one point four processes matched, and working out which
    were real cost more time than the fix. The same trap took four logins
    to spot during the 2026-09-23 headless investigation (recorded in
    docs/ESPRESSO_SESSION_BUGS_2026_09.md).

    Duplicates are not merely untidy now that ALARMs reach the screen: two
    watchdogs mean two toasts for every alert, and an alert that arrives
    twice is one the operator starts dismissing without reading.
    """
    import atexit
    import os
    lock = Path(__file__).with_name("data") / "_scan_watchdog.lock"
    lock.parent.mkdir(exist_ok=True)
    if lock.exists():
        try:
            other = int(lock.read_text(encoding="utf-8").strip())
            import psutil
            if psutil.pid_exists(other):
                proc = psutil.Process(other)
                if any("scan_watchdog" in str(a) for a in proc.cmdline()[1:]):
                    print(f"  a watchdog is already running (pid {other}).")
                    print("  Two would send every ALARM twice. Stop that one "
                          f"first, or delete {lock}")
                    return False
        except Exception:
            pass                      # stale lock, take it
    lock.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return True


def notify(alerts: list[Alert], target: str = "windows://") -> int:
    """Put ALARMs on the operator's screen. Returns how many were sent.

    WHY. Every alert this tool produced landed in a terminal nobody was
    watching. On 2026-09-22 a crash killed an overnight scan and it sat dead
    from 01:19 until 14:14 - the watchdog had the evidence and no way to say
    so.

    `windows://` is a LOCAL desktop toast: no API key, no account, no
    internet, nothing leaves the machine (it needs pywin32, which is now a
    dependency). A different target can be passed for an overnight run where
    a toast on a sleeping machine helps nobody - but anything off-machine
    sends data somewhere, so it is never the default.

    WARN is deliberately not sent. A notifier that fires on every minor
    thing gets muted, and a muted notifier is worse than none.

    NEVER RAISES. A notification failing must not take down the watchdog
    that is watching the scan.
    """
    alarms = [a for a in alerts if a.level == "ALARM"]
    if not alarms:
        return 0
    try:
        import apprise
    except Exception:
        return 0
    try:
        client = apprise.Apprise()
        if not client.add(target):
            return 0
        body = chr(10).join(f"[{a.monitor}] {a.message.splitlines()[0]}"
                            for a in alarms)
        # 250 characters is the windows:// limit; the full text is on screen
        # in the watchdog and in the log either way.
        client.notify(title="CruiseIntel scan", body=body[:240])
        return len(alarms)
    except Exception as exc:
        logger_line = f"(notify failed: {exc})"
        print(f"   {logger_line}", flush=True)
        return 0


def window_is_hung(pid: int) -> bool:
    """Is the process's window refusing to pump messages? Windows only.

    `IsHungAppWindow` is what Task Manager uses to decide something is "Not
    Responding". Cheap, and it answers the question a log cannot: a frozen
    UI writes nothing, so silence in the log looks identical to idleness.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    u32 = ctypes.windll.user32
    hung = []

    def _cb(hwnd, _):
        owner = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u32.IsWindowVisible(hwnd):
            if u32.GetWindowTextLengthW(hwnd) > 0:
                hung.append(bool(u32.IsHungAppWindow(hwnd)))
        return True

    try:
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows(proto(_cb), 0)
    except Exception:
        return False
    return any(hung)


def dump_stack(pid: int) -> str:
    """py-spy dump of a frozen process, or "" if it is unavailable.

    WHY THIS IS WORTH AUTOMATING. On 2026-09-28 the GUI froze and the log
    said nothing at all - a hung UI cannot write. `py-spy dump` turned an
    unfalsifiable "the GUI is not responding" into a line number in about
    ten seconds:

        Thread 5628 (idle)
            _on_login_check (gui\\windows.py:657)   <- a blocking print()

    Doing it automatically means the evidence exists at the moment of the
    hang, rather than whenever someone thinks to ask.
    """
    import shutil
    import subprocess
    exe = shutil.which("py-spy") or shutil.which(
        "py-spy", path=str(Path(sys.executable).parent))
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "dump", "--pid", str(pid)],
                             capture_output=True, text=True, timeout=60)
        return out.stdout or out.stderr or ""
    except Exception as exc:
        return f"(py-spy failed: {exc})"


def m_hung_window(s: ScanState) -> list[Alert]:
    """The GUI window has stopped responding.

    A hang is the one failure a log-reader cannot see on its own: the frozen
    process writes nothing, and silence is indistinguishable from an idle
    scan. Asking Windows directly closes that blind spot, and the stack dump
    turns the alert into a diagnosis.
    """
    if s.watched_pid is None or s.process_gone or not s.window_hung:
        return []
    detail = s.hang_stack.strip()
    where = ""
    for line in detail.splitlines():
        if ".py:" in line and "Thread" not in line:
            where = f"  blocked in: {line.strip()}"
            break
    return [Alert("ALARM", "hung",
                  f"the GUI window (PID {s.watched_pid}) has stopped "
                  f"responding - Windows reports it as hung.\n"
                  f"{where}\n"
                  f"     full stack in data/hang_dump.txt")]


def m_navigation_retries(s: ScanState) -> list[Alert]:
    """Navigations that fail first time and succeed on a retry.

    THE BLIND SPOT this closes. A retry that recovers produces no error, no
    failed booking and no bad result - it produces DELAY, and nothing was
    watching for it. Measured on the live log, by hour:

        2026-09-22T19  navigations=103  retries=  0    0.0%
        2026-09-23T15  navigations=179  retries= 91   50.8%
        2026-09-23T17  navigations=125  retries= 71   56.8%
        2026-09-23T18  navigations= 16  retries= 14   87.5%

    Every one recovered, so the run looked perfectly healthy while more
    than half of its navigations were being paid for twice. A rising rate
    is also the earliest sign of a portal or network going bad, ahead of
    the timeouts that eventually follow.
    """
    if s.nav_attempts < 40:
        return []
    rate = s.nav_retries / s.nav_attempts
    if rate >= 0.40:
        return [Alert("ALARM", "nav-retries",
                      f"{rate:.0%} of navigations ({s.nav_retries} of "
                      f"{s.nav_attempts}) needed a retry. They recover, so "
                      f"nothing fails - but the run is paying for those "
                      f"navigations twice. Check the network or the portal.")]
    if rate >= 0.15:
        return [Alert("WARN", "nav-retries",
                      f"{rate:.0%} of navigations needed a retry "
                      f"({s.nav_retries} of {s.nav_attempts})")]
    return []


def m_cancellations(s: ScanState) -> list[Alert]:
    """Cancelled bookings. ALWAYS reported, from the very first one.

    Neon 2026-09-22: reporting a cancellation is "VERY MADNATORY ...
    something very critical". Every other monitor here waits for a
    population before it will speak, because a single odd booking proves
    nothing about a run. This one does not: one cancelled booking is one
    piece of news the agency needs, not a statistical signal.
    """
    n = s.results.get("CANCELLED", 0)
    if not n:
        return []
    return [Alert("ALARM", "cancelled",
                  f"{n} booking(s) came back CANCELLED — the portal reports "
                  f"the reservation as cancelled (status CX). Not a pricing "
                  f"outcome: these need acting on.")]


def m_error_streak(s: ScanState) -> list[Alert]:
    """Consecutive ERRORs - the shape of every cascading failure here.

    REAL INCIDENT: ESPRESSO bookings #400-403 died in sequence on
    2026-08-27 when the portal signed the session out; nothing noticed and
    the batch kept going. A streak is the earliest honest signal that the
    next 100 bookings will fail the same way.
    """
    streak = 0
    for st in reversed(s.recent_statuses):
        if st == "ERROR":
            streak += 1
        else:
            break
    if streak >= 5:
        return [Alert("ALARM", "error-streak",
                      f"{streak} consecutive ERROR results - the run is very "
                      f"likely failing identically from here. Check the "
                      f"session and stop the scan rather than burning the "
                      f"watchlist.")]
    if streak >= 3:
        return [Alert("WARN", "error-streak", f"{streak} consecutive ERRORs")]
    return []


def m_session_health(s: ScanState) -> list[Alert]:
    """Logouts mid-scan, and whether re-login worked."""
    out = []
    if s.events.get("batch.session_expired_recovering"):
        out.append(Alert("WARN", "session",
                         "the portal signed us out mid-scan; automatic "
                         "re-login was attempted"))
    if s.events.get("batch.session_recovery_gave_up"):
        out.append(Alert("ALARM", "session",
                         "re-login FAILED - the scan stopped. On ESPRESSO "
                         "this usually means MFA was demanded; log in by "
                         "hand and Start again."))
    if s.events.get("batch.session_expired_again"):
        out.append(Alert("ALARM", "session",
                         "signed out a second time after a successful "
                         "re-login - the account may be being kicked by "
                         "another session."))
    if s.events.get("login.adopted_other_tab"):
        out.append(Alert("WARN", "session",
                         "the authenticated session was found in a DIFFERENT "
                         "TAB and adopted - worth confirming this is the "
                         "ESPRESSO login cause."))
    return out


def m_status_mix(s: ScanState) -> list[Alert]:
    """A sudden shift in the mix of outcomes.

    REAL INCIDENT: NCL returned PAID_IN_FULL for 113 of 143 bookings (79%)
    against a historical 14%. The run "succeeded" - it just answered the
    wrong question about almost every booking.
    """
    total = sum(s.results.values())
    if total < 25:
        return []
    out = []
    # NO_SAVING IS DELIBERATELY NOT MONITORED. It was, at a 97% threshold,
    # and a replay of a perfectly healthy run tripped it immediately. The
    # measured base rate says why: across 808 bookings observed on 2+ days,
    # 81% NEVER changed price at all. A run that is almost entirely
    # NO_SAVING is the NORMAL outcome, not an anomaly, and alerting on it
    # would train the reader to ignore the watchdog - which is worse than
    # not having one.
    for status, share_limit in (("PAID_IN_FULL", 0.60), ("ERROR", 0.25)):
        share = s.results.get(status, 0) / total
        if share >= share_limit:
            out.append(Alert("WARN", "status-mix",
                             f"{share*100:.0f}% of {total} bookings came back "
                             f"{status} - unusually high; confirm this is real "
                             f"and not a portal-state problem."))
    return out


def m_feature_capture(s: ScanState) -> list[Alert]:
    """Are the price-driver columns actually being filled?

    Added the same day the feature capture was: ESPRESSO's fields come from
    a regex over its Angular bootstrap, so a portal restructure would make
    them silently NULL rather than raise - the columns would fill with
    nothing and nobody would know until a model was trained on air.
    """
    if s.feature_rows < 20:
        return []
    null_share = s.feature_nulls / s.feature_rows
    if null_share >= 0.80:
        return [Alert("ALARM", "features",
                      f"{null_share*100:.0f}% of {s.feature_rows} bookings "
                      f"recorded NO sail date - the page shape has probably "
                      f"changed and the driver columns are filling with NULL.")]
    if null_share >= 0.40:
        return [Alert("WARN", "features",
                      f"{null_share*100:.0f}% of bookings recorded no sail date")]
    return []


def m_advisories(s: ScanState) -> list[Alert]:
    """Portal refusals, e.g. GoCCL 5108 "The VIFP number is incorrect."."""
    blocking = {c: n for c, n in s.advisories.items() if c not in ("1241",)}
    if not blocking:
        return []
    total = sum(blocking.values())
    if total >= 5:
        worst = ", ".join(f"{c} x{n}" for c, n in
                          sorted(blocking.items(), key=lambda kv: -kv[1])[:3])
        return [Alert("WARN", "advisories",
                      f"{total} bookings refused by the portal ({worst}) - "
                      f"these are fixable data problems on the bookings.")]
    return []


def m_slowdown(s: ScanState) -> list[Alert]:
    """Per-booking time drifting upward, or the run stalling outright."""
    out = []
    idle = time.monotonic() - s.last_progress
    if idle > 600:
        out.append(Alert("ALARM", "stall",
                         f"no booking completed for {idle/60:.0f} minutes - "
                         f"the scan looks stuck."))
    elif idle > 240:
        out.append(Alert("WARN", "stall", f"no progress for {idle/60:.0f} minutes"))

    if len(s.timings_ms) >= 20:
        first = s.timings_ms[:10]
        last = s.timings_ms[-10:]
        a, b = sum(first) / len(first), sum(last) / len(last)
        if a > 0 and b > a * 2.0:
            out.append(Alert("WARN", "slowdown",
                             f"bookings are taking {b/1000:.1f}s now vs "
                             f"{a/1000:.1f}s at the start of the run"))
    return out


def m_structure_drift(s: ScanState) -> list[Alert]:
    if s.events.get("structure.drift"):
        return [Alert("ALARM", "drift",
                      "the portal's page structure changed against the saved "
                      "baseline - selectors may be silently wrong.")]
    return []


MONITORS = (m_crashes, m_process, m_hung_window, m_cancellations, m_error_streak,
            m_session_health, m_status_mix, m_feature_capture,
            m_advisories, m_slowdown, m_structure_drift,
            m_navigation_retries)


def consume(state: ScanState, entry: dict) -> None:
    """Fold one structured log line into the running state."""
    event = entry.get("event") or ""
    state.events[event] += 1

    # PROGRESS IS A BOOKING STARTING, NOT A BOOKING REPORTING A RESULT.
    #
    # Measured on the live ESPRESSO run of 2026-09-23, which never stalled:
    #   gaps between *.result    max 26.8 min - 5 over the 10-min ALARM
    #   gaps between batch.checking  max  1.0 min - none over 4 min
    #
    # m_slowdown used to key its idle timer on *.result alone, so that
    # healthy run would have raised five "the scan looks stuck" ALARMs
    # while it was starting a booking every sixty seconds. The cause is
    # below: most ESPRESSO bookings never emit a result line at all.
    # A navigation that retries still succeeds, so it shows up nowhere
    # else. navigate_home is the per-booking navigation; the retry event
    # fires once per failed attempt.
    if event.endswith(".navigate_home"):
        state.nav_attempts += 1
    elif event == "browser.navigate_retry":
        state.nav_retries += 1

    if event == "batch.checking":
        # A NEW BATCH RESETS THE COUNTERS.
        #
        # THE PROBLEM, found 2026-09-28: the watchdog had no concept of a
        # "run". A process started on the 23rd was still going five days
        # later, accumulating forever, and reporting:
        #
        #     watchdog:  27% of navigations needed a retry (433 of 1609)
        #     reality:    0% (0 of 14) on the run actually in progress
        #
        # It was averaging a fixed bug's 35% into a healthy run's 0%. Its
        # session ALARM was likewise five days stale - "re-login FAILED,
        # the scan stopped" about a scan that had long since finished.
        #
        # A monitor whose numbers describe all of history describes nothing
        # in particular, and one that cries wolf about problems fixed days
        # ago is how a watchdog gets ignored. Rates must describe the run in
        # front of you.
        #
        # Index 1 is the first booking of a batch - that is the boundary.
        # Cumulative totals are kept separately so nothing is lost.
        # ALWAYS reset on index 1; only COUNT it as a completed run if
        # something had actually been seen. An earlier version reset only
        # when started_count was already non-zero, which missed the common
        # case: a watchdog reading a log from the beginning has counted
        # hundreds of navigations before the first batch.checking ever
        # appears, and those belong to an older run.
        if entry.get("index") == 1:
            had_activity = bool(state.started_count or state.nav_attempts
                                or state.results)
            state.reset_for_new_run(count_run=had_activity)
        state.started_count += 1
        state.last_progress = time.monotonic()

    if event.endswith(".result"):
        status = str(entry.get("status") or "").upper()
        if status:
            state.results[status] += 1
            state.recent_statuses.append(status)
            state.booking_count += 1
            state.last_progress = time.monotonic()
    # TERMINAL STATUSES THAT NEVER EMIT A RESULT LINE.
    #
    # ESPRESSO exits early for these and logs its own event instead, so
    # they never reached state.results and every monitor reading it was
    # blind to them. On the live run of 2026-09-23 the database held
    # PAID_IN_FULL=162, WLT=93, CANCELLED=8 - 263 of 458 bookings - while
    # the watchdog's own count sat at 72.
    #
    # m_cancellations was the worst of it: Neon called reporting a
    # cancellation "VERY MADNATORY", and it reads results["CANCELLED"],
    # which could never rise above zero. Eight cancelled bookings went
    # unreported in that run.
    #
    # Only the cancellation is recoverable from the log as it stands -
    # PAID_IN_FULL and WLT have no distinguishing event, which is why the
    # real fix is source-side: emit a result line on every terminal path.
    elif event.endswith(".booking_cancelled"):
        state.results["CANCELLED"] += 1
        state.recent_statuses.append("CANCELLED")
        state.booking_count += 1
        state.last_progress = time.monotonic()
    elif event.endswith(".timings"):
        total = entry.get("total_ms")
        if isinstance(total, (int, float)):
            state.timings_ms.append(int(total))
    elif event == "goccl.advisory":
        state.advisories[str(entry.get("code") or "?")] += 1
    elif event == CRASH_EVENT:
        # Keep the detail, not just a count - working out WHAT broke is the
        # reason this event exists at all. Bounded so a crash loop cannot
        # grow the watchdog's memory without limit.
        if len(state.crashes) < 200:
            state.crashes.append({
                "source": entry.get("source"),
                "error_type": entry.get("error_type"),
                "error": entry.get("error"),
                "task": entry.get("task"),
                "timestamp": entry.get("timestamp"),
            })
    elif event == "price_history.features":
        state.feature_rows += 1
        if not entry.get("sail_date"):
            state.feature_nulls += 1


def run_monitors(state: ScanState, *, repeat: bool = False) -> list[Alert]:
    """Every monitor, deduplicated so one condition alerts once."""
    alerts: list[Alert] = []
    for monitor in MONITORS:
        try:
            alerts.extend(monitor(state))
        except Exception as exc:          # a watchdog must never crash
            alerts.append(Alert("WARN", monitor.__name__,
                                f"monitor failed: {str(exc)[:120]}"))
    if repeat:
        return alerts
    fresh = []
    for a in alerts:
        key = (a.monitor, a.message[:60])
        if key not in state.fired:
            state.fired.add(key)
            fresh.append(a)
    return fresh


def summary(state: ScanState) -> str:
    total = sum(state.results.values())
    mix = "  ".join(f"{k}={v}" for k, v in state.results.most_common(6))
    avg = (sum(state.timings_ms) / len(state.timings_ms) / 1000
           if state.timings_ms else 0.0)
    # Show STARTED alongside REPORTED. The old line printed only the
    # reported count, so a run 458 bookings deep read as "144 bookings"
    # and looked like it had barely begun. Two numbers that differ are
    # the honest picture; one number that hides the difference is not.
    head = f"{state.started_count} started"
    if total != state.started_count:
        head += f" / {total} reported"
    return (f"{head}   {mix or '(none yet)'}"
            + (f"   avg {avg:.1f}s/booking" if avg else ""))


def find_scan_process() -> int | None:
    """PID of the running GUI, or None.

    psutil is already a project dependency, but it is imported lazily and
    every failure is swallowed: the watchdog must still work as a pure log
    reader on a machine where process inspection is blocked.
    """
    try:
        import psutil
    except Exception:
        return None
    # PICK THE BIGGEST, NOT THE FIRST. Launching the GUI leaves more than
    # one process with `-m gui.main` on its command line: on 2026-09-23 a
    # live run showed PID 12944 at 3 MB / 1 thread next to PID 26948 at
    # 419 MB / 20 threads. The small one is a launcher stub, and watching
    # it would have reported nothing useful about the process actually
    # doing the scanning. Resident memory separates them unambiguously.
    best_pid, best_rss = None, -1
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if _SCAN_CMDLINE_MARKER not in cmdline:
                continue
            try:
                rss = psutil.Process(proc.info["pid"]).memory_info().rss
            except Exception:
                rss = 0
            if rss > best_rss:
                best_pid, best_rss = int(proc.info["pid"]), rss
    except Exception:
        return None
    return best_pid


def process_is_alive(pid: int) -> bool:
    """False only when we are SURE the process is gone.

    An unknown answer must never read as death. A false "the scan is GONE"
    while a scan is happily running is the fastest way to make this tool
    ignorable, and an ignored watchdog is worse than none.
    """
    try:
        import psutil
    except Exception:
        return True
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception as exc:
        try:
            import psutil as _ps
            if isinstance(exc, _ps.NoSuchProcess):
                return False
        except Exception:
            pass
        return True


def follow(path: Path, once: bool = False, interval: float = 5.0,
           pid: int | None = None, notify_target: str = "") -> int:
    if not once and not claim_single_instance():
        return 3
    state = ScanState()
    # WATCH THE PROCESS, NOT ONLY ITS OUTPUT. A log going quiet is
    # ambiguous - finished, paused, hung and dead all look identical from
    # the outside. On 2026-09-22 a crash killed a scan overnight and the
    # silence that followed read exactly like a healthy idle run.
    state.watched_pid = pid if pid is not None else find_scan_process()
    if not path.exists():
        print(f"no log at {path}\n"
              f"Logging to file was enabled on 2026-09-21; start the GUI or a "
              f"scan and it will appear.", file=sys.stderr)
        return 2

    print(f"watching {path}")
    print(f"watching process PID {state.watched_pid}\n" if state.watched_pid
          else "no scan process found - watching the log only\n")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        while True:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    consume(state, json.loads(line))
                except Exception:
                    continue
            if state.watched_pid is not None and not state.process_gone:
                state.process_gone = not process_is_alive(state.watched_pid)
                # A frozen UI writes nothing, so the log alone cannot tell a
                # hang from an idle scan. Ask Windows, and grab the stack
                # the FIRST time it says hung - the evidence is worthless
                # after a restart.
                if not state.process_gone:
                    was = state.window_hung
                    state.window_hung = window_is_hung(state.watched_pid)
                    if state.window_hung and not was:
                        state.hang_stack = dump_stack(state.watched_pid)
                        try:
                            out = Path(__file__).with_name("data") / "hang_dump.txt"
                            out.parent.mkdir(exist_ok=True)
                            out.write_text(state.hang_stack, encoding="utf-8")
                        except Exception:
                            pass
                    elif was and not state.window_hung:
                        state.fired.discard("hung")   # it recovered
            tick_alerts = run_monitors(state)
            for alert in tick_alerts:
                print(alert, flush=True)
            if notify_target:
                notify(tick_alerts, notify_target)
            if once:
                print(f"\n{summary(state)}")
                return 1 if any(a.level == "ALARM"
                                for a in run_monitors(state, repeat=True)) else 0
            print(f"   ... {summary(state)}", flush=True)
            time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--once", action="store_true",
                    help="one pass over the existing log, then exit")
    ap.add_argument("--interval", type=float, default=5.0)
    # NOTIFICATIONS ARE OFF BY DEFAULT, 2026-09-29.
    #
    # Neon: "python notification keeps firing althouhgh the gui is closed
    # ... infact close the notifications at all we do not need it we just
    # need a watcher watching the ciode script project monotiring as a
    # third eye that everything is running and functioning properly."
    #
    # They were firing precisely BECAUSE he closed the GUI: m_process
    # alarmed on the watched process disappearing, which is exactly what a
    # deliberate shutdown looks like from the outside. A monitor that
    # shouts when you close an application is worse than silent - it trains
    # you to dismiss it, and then it is no use when something real breaks.
    #
    # Both halves are fixed: the toast is opt-in (--notify windows://), and
    # m_process no longer treats a CLEAN shutdown as a failure at all.
    ap.add_argument("--notify", default="",
                    help="Apprise target for ALARMs (e.g. windows:// for a "
                         "local desktop toast). EMPTY by default - the "
                         "watchdog just prints.")
    ap.add_argument("--pid", type=int, default=None,
                    help="process to watch for liveness "
                         "(default: auto-detect the running GUI)")
    args = ap.parse_args()
    try:
        return follow(args.log, once=args.once, interval=args.interval,
                      pid=args.pid, notify_target=args.notify)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
