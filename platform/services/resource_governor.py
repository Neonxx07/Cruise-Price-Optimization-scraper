"""CPU/RAM-aware throttle gate, plus a single-instance process guard.

WHY (added 2026-08-27)
----------------------
Scanning three cruise lines at once on a 4-core / 17 GB machine can make
the PC unusable if nothing watches load. The stated priority for this
whole feature is "smoothness and reliability, not maximum concurrency",
so this module is the part that enforces that: workers `await
gate.wait_until_ok()` before starting a booking, and a background sampler
closes the gate whenever CPU or RAM is over threshold.

PATTERN CREDIT / WHY IT'S SHAPED THIS WAY
-----------------------------------------
Modeled on apify/crawlee-python's `_utils/system.py` +
`_autoscaling/snapshotter.py`, which is the one production-grade example
of resource-aware browser throttling in Python. Two details copied
deliberately because they are easy to get wrong:

1. **Child-process RSS is summed, not just our own.** Chromium runs each
   renderer/GPU/utility as a SEPARATE OS process, all children of the
   Playwright driver we spawned. Measuring only `Process().memory_info()`
   would report a few tens of MB while the browser actually holds
   gigabytes — i.e. the throttle would never fire. This is the single
   most important line in the file.
2. **`cpu_percent` needs a real interval.** `psutil.cpu_percent()` with
   no interval returns the value since the *previous* call, which is
   meaningless on the first sample and jumpy afterwards. A short blocking
   interval in a background task is the honest way to read it.

Deliberately NOT importing crawlee itself — it's a whole crawler
framework (its own scheduler, storages, HTTP clients) and this project
needs one gate. Copying the pattern is right; taking the dependency is not.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import psutil

from utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ResourceSnapshot:
    """One sample. `browser_rss_mb` includes child processes — see the
    module docstring for why that matters."""
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    browser_rss_mb: float = 0.0
    throttled: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "cpu_percent": round(self.cpu_percent, 1),
            "ram_percent": round(self.ram_percent, 1),
            "browser_rss_mb": round(self.browser_rss_mb, 1),
            "throttled": self.throttled,
            "reason": self.reason,
        }


class ResourceGovernor:
    """Samples system load in the background and gates worker starts.

    Fail-OPEN by design: if sampling itself errors (a psutil permission
    quirk, a process vanishing mid-read), the gate OPENS rather than
    wedging every worker forever. A broken thermometer must not stop the
    scan.
    """

    def __init__(
        self,
        max_cpu_percent: float = 85.0,
        max_ram_percent: float = 85.0,
        sample_interval_s: float = 5.0,
    ) -> None:
        self.max_cpu_percent = max_cpu_percent
        self.max_ram_percent = max_ram_percent
        self.sample_interval_s = sample_interval_s
        # Set == "clear to proceed". Starts open so nothing blocks before
        # the first sample lands.
        self._gate = asyncio.Event()
        self._gate.set()
        self._snapshot = ResourceSnapshot()
        self._task: asyncio.Task | None = None
        self._stop = False

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._sample_loop())
        logger.info(
            "governor.started",
            max_cpu_percent=self.max_cpu_percent,
            max_ram_percent=self.max_ram_percent,
            interval_s=self.sample_interval_s,
        )

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._gate.set()  # never leave workers blocked on a stopped governor
        logger.info("governor.stopped")

    # ── the gate workers await ───────────────────────────────────

    async def wait_until_ok(self, timeout_s: float = 300.0) -> bool:
        """Block until load is acceptable. Returns False if it never
        cleared within `timeout_s` (caller decides whether to proceed
        anyway or defer the booking)."""
        if self._gate.is_set():
            return True
        logger.info("governor.worker_waiting", **self._snapshot.as_dict())
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            logger.warning("governor.wait_timeout", timeout_s=timeout_s, **self._snapshot.as_dict())
            return False

    @property
    def snapshot(self) -> ResourceSnapshot:
        return self._snapshot

    @property
    def is_throttled(self) -> bool:
        return not self._gate.is_set()

    # ── sampling ─────────────────────────────────────────────────

    def sample_now(self) -> ResourceSnapshot:
        """Take one synchronous sample. Separated out so it's directly
        unit-testable without running the loop."""
        try:
            cpu = psutil.cpu_percent(interval=0.1)
            ram = psutil.virtual_memory().percent
            rss_mb = self._browser_family_rss_mb()
            reasons = []
            if cpu > self.max_cpu_percent:
                reasons.append(f"CPU {cpu:.0f}% > {self.max_cpu_percent:.0f}%")
            if ram > self.max_ram_percent:
                reasons.append(f"RAM {ram:.0f}% > {self.max_ram_percent:.0f}%")
            return ResourceSnapshot(
                cpu_percent=cpu, ram_percent=ram, browser_rss_mb=rss_mb,
                throttled=bool(reasons), reason="; ".join(reasons),
            )
        except Exception as e:
            # Fail OPEN — see the class docstring.
            logger.warning("governor.sample_failed", error=str(e))
            return ResourceSnapshot(throttled=False, reason=f"sample failed: {e}")

    @staticmethod
    def _browser_family_rss_mb() -> float:
        """RSS of this process AND all its descendants, in MB.

        THE important measurement: Chromium's renderer/GPU/utility
        processes are separate OS processes spawned under the Playwright
        driver we started, so our own RSS alone wildly understates real
        memory use and the throttle would never trigger.
        """
        total = 0
        try:
            me = psutil.Process(os.getpid())
            procs = [me] + me.children(recursive=True)
            for p in procs:
                try:
                    total += p.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # Processes come and go constantly while a browser
                    # works; a vanished child is normal, not an error.
                    continue
        except Exception:
            return 0.0
        return total / (1024 * 1024)

    async def _sample_loop(self) -> None:
        while not self._stop:
            snap = await asyncio.to_thread(self.sample_now)
            was_throttled = not self._gate.is_set()
            self._snapshot = snap
            if snap.throttled:
                if not was_throttled:
                    logger.warning("governor.throttling_on", **snap.as_dict())
                self._gate.clear()
            else:
                if was_throttled:
                    logger.info("governor.throttling_off", **snap.as_dict())
                self._gate.set()
            await asyncio.sleep(self.sample_interval_s)


class SingleInstanceGuard:
    """Refuse to start a second controller for the same scope.

    WHY: this project has no process-level guard at all today, and the
    audit found the concrete consequences — two drivers evict each other's
    portal session (these portals allow one active session per account),
    clobber each other's `storage_state_*.json` (last writer wins, so a
    dead session can overwrite a good one), and contend on SQLite.

    Uses `filelock` (>=3.29.0) rather than a hand-rolled
    `O_CREAT|O_EXCL` + PID check: on Windows, raw PID liveness checks are
    unreliable because PIDs get reused, and filelock 3.29.0 is the release
    that added stale-lock detection ON WINDOWS specifically — i.e. it
    correctly reclaims a lock whose holder died without cleaning up,
    which is exactly the crash case that would otherwise wedge the app
    permanently.
    """

    def __init__(self, scope: str, lock_dir: str = "data/locks") -> None:
        self.scope = scope
        self.lock_dir = lock_dir
        self.lock_path = os.path.join(lock_dir, f"{scope}.lock")
        self._lock = None

    def acquire(self) -> bool:
        """True if we now hold the lock; False if someone else does."""
        from filelock import FileLock, Timeout

        os.makedirs(self.lock_dir, exist_ok=True)
        self._lock = FileLock(self.lock_path, timeout=0)
        try:
            self._lock.acquire()
        except Timeout:
            logger.error(
                "single_instance.already_running",
                scope=self.scope, lock_path=self.lock_path,
                note="another controller holds this lock — refusing to start a second one",
            )
            self._lock = None
            return False
        # Record who holds it, for a useful error message next time.
        try:
            with open(self.lock_path + ".owner", "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()}\n")
        except Exception:
            pass
        logger.info("single_instance.acquired", scope=self.scope, pid=os.getpid())
        return True

    def release(self) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            except Exception as e:
                logger.warning("single_instance.release_failed", scope=self.scope, error=str(e))
            self._lock = None
            try:
                os.unlink(self.lock_path + ".owner")
            except Exception:
                pass
            logger.info("single_instance.released", scope=self.scope)

    def holder_pid(self) -> str | None:
        """Best-effort read of who currently holds the lock."""
        try:
            with open(self.lock_path + ".owner", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                f"Another CruiseIntel controller is already running for scope "
                f"'{self.scope}' ({self.holder_pid() or 'unknown holder'}). "
                f"Stop it before starting another."
            )
        return self

    def __exit__(self, *exc) -> None:
        self.release()
