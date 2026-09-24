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
    fired: set = field(default_factory=set)


# ── the monitors ─────────────────────────────────────────────────────────
#
# Each returns a list of Alerts. A monitor must be cheap and must never
# raise: a watchdog that crashes the thing it watches is worse than none.


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


MONITORS = (m_cancellations, m_error_streak, m_session_health, m_status_mix,
            m_feature_capture, m_advisories, m_slowdown, m_structure_drift)


def consume(state: ScanState, entry: dict) -> None:
    """Fold one structured log line into the running state."""
    event = entry.get("event") or ""
    state.events[event] += 1

    if event.endswith(".result"):
        status = str(entry.get("status") or "").upper()
        if status:
            state.results[status] += 1
            state.recent_statuses.append(status)
            state.booking_count += 1
            state.last_progress = time.monotonic()
    elif event.endswith(".timings"):
        total = entry.get("total_ms")
        if isinstance(total, (int, float)):
            state.timings_ms.append(int(total))
    elif event == "goccl.advisory":
        state.advisories[str(entry.get("code") or "?")] += 1
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
    return (f"{total} bookings   {mix or '(none yet)'}"
            + (f"   avg {avg:.1f}s/booking" if avg else ""))


def follow(path: Path, once: bool = False, interval: float = 5.0) -> int:
    state = ScanState()
    if not path.exists():
        print(f"no log at {path}\n"
              f"Logging to file was enabled on 2026-09-21; start the GUI or a "
              f"scan and it will appear.", file=sys.stderr)
        return 2

    print(f"watching {path}\n")
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
            for alert in run_monitors(state):
                print(alert, flush=True)
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
    args = ap.parse_args()
    try:
        return follow(args.log, once=args.once, interval=args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
