"""Tests for the 2026-08-27 concurrent multi-cruise-line scanner.

Covers the scheduling/isolation machinery WITHOUT a real browser or a
real portal (the browser-level cookie/localStorage isolation itself was
verified separately against a live Chromium — see the module docstrings
in scraper/browser_pool.py). These are the invariants that must never
silently regress: the concurrency bound, per-line accounting, failure
isolation, pause/stop, and the resource gate.
"""
import asyncio

import pytest

from core.models import BookingResult, BookingStatus, CruiseLine
from services.multi_line_coordinator import LineState, MultiLineCoordinator
from services.resource_governor import ResourceGovernor, SingleInstanceGuard


class _FakePool:
    """Stands in for SharedBrowserPool — scheduling tests must not launch
    a real browser."""

    is_alive = True
    _browser = None

    def __init__(self):
        self.recycled: list[str] = []

    @property
    def context_count(self):
        return 3

    async def start(self):
        pass

    async def close(self):
        pass

    async def recycle_context(self, cruise_line):
        self.recycled.append(cruise_line.value)
        return None

    def live_cruise_lines(self):
        return ["ESPRESSO", "NCL", "GOCCL"]


def _coordinator(max_concurrent=2):
    c = MultiLineCoordinator(max_concurrent=max_concurrent, use_single_instance_guard=False)
    c.pool = _FakePool()
    return c


def _ok_worker(cruise_line, tracker=None, delay=0.05):
    async def w(booking_id):
        if tracker is not None:
            tracker["inflight"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["inflight"])
        try:
            await asyncio.sleep(delay)
            return BookingResult(
                cruise_line=cruise_line, status=BookingStatus.NO_SAVING, booking_id=booking_id,
            )
        finally:
            if tracker is not None:
                tracker["inflight"] -= 1

    return w


@pytest.mark.asyncio
async def test_all_three_lines_run_concurrently_and_all_results_collected():
    """Requirement 1: all three cruise lines scan at the same time."""
    c = _coordinator(max_concurrent=3)
    c.register_line(CruiseLine.ESPRESSO, ["E1", "E2"], _ok_worker(CruiseLine.ESPRESSO))
    c.register_line(CruiseLine.NCL, ["N1", "N2"], _ok_worker(CruiseLine.NCL))
    c.register_line(CruiseLine.GOCCL, ["G1"], _ok_worker(CruiseLine.GOCCL))

    snap = await c.run()

    assert len(c.all_results()) == 5
    assert {l["cruise_line"] for l in snap["lines"]} == {"ESPRESSO", "NCL", "GOCCL"}
    assert all(l["state"] == LineState.DONE.value for l in snap["lines"])
    assert all(l["progress_pct"] == 100.0 for l in snap["lines"])


@pytest.mark.asyncio
async def test_concurrency_bound_is_never_exceeded():
    """Requirement 3: the PC stays responsive because the limit holds.
    Would fail if the global semaphore were missing or per-line only."""
    tracker = {"inflight": 0, "peak": 0}
    c = _coordinator(max_concurrent=2)
    for cl, ids in (
        (CruiseLine.ESPRESSO, ["E1", "E2", "E3"]),
        (CruiseLine.NCL, ["N1", "N2", "N3"]),
        (CruiseLine.GOCCL, ["G1", "G2", "G3"]),
    ):
        c.register_line(cl, ids, _ok_worker(cl, tracker))

    await c.run()
    assert tracker["peak"] <= 2, f"concurrency bound breached: peak={tracker['peak']}"
    assert len(c.all_results()) == 9


@pytest.mark.asyncio
async def test_single_line_only_also_works():
    """Requirement 1's second half: 'and sometimes by one cruise line at a
    time.' Registering one line must work exactly the same way."""
    c = _coordinator()
    c.register_line(CruiseLine.NCL, ["N1", "N2"], _ok_worker(CruiseLine.NCL))
    snap = await c.run()
    assert len(snap["lines"]) == 1
    assert snap["lines"][0]["cruise_line"] == "NCL"
    assert snap["lines"][0]["done"] == 2


@pytest.mark.asyncio
async def test_one_line_failing_does_not_stop_others_and_recycles_only_its_context():
    """Requirement 4: reliability. A failure on one line must be isolated
    to that line, and must recycle ONLY that line's context."""

    async def boom(booking_id):
        raise RuntimeError("simulated portal failure")

    c = _coordinator(max_concurrent=3)
    c.register_line(CruiseLine.ESPRESSO, ["E1", "E2"], _ok_worker(CruiseLine.ESPRESSO))
    c.register_line(CruiseLine.NCL, ["N1"], boom)
    c.register_line(CruiseLine.GOCCL, ["G1"], _ok_worker(CruiseLine.GOCCL))

    snap = await c.run()
    by_line = {l["cruise_line"]: l for l in snap["lines"]}

    assert by_line["ESPRESSO"]["done"] == 2 and by_line["ESPRESSO"]["errors"] == 0
    assert by_line["GOCCL"]["done"] == 1
    assert by_line["NCL"]["errors"] == 1
    assert by_line["NCL"]["context_recycles"] == 1
    # Only the failing line's context was recycled.
    assert c.pool.recycled == ["NCL"]


@pytest.mark.asyncio
async def test_error_results_counted_as_errors_not_successes():
    """An ERROR BookingResult (the normal way a portal failure surfaces)
    must land in the error count, not the done count."""

    async def err_worker(booking_id):
        return BookingResult(
            cruise_line=CruiseLine.NCL,
            status=BookingStatus.ERROR,
            booking_id=booking_id,
            error="portal timeout",
        )

    c = _coordinator()
    c.register_line(CruiseLine.NCL, ["N1"], err_worker)
    snap = await c.run()
    line = snap["lines"][0]
    assert line["errors"] == 1 and line["done"] == 0
    assert "portal timeout" in (line["last_error"] or "")


@pytest.mark.asyncio
async def test_stop_request_halts_before_the_next_booking():
    """Stop must be cooperative — checked BETWEEN bookings so an in-flight
    booking always finishes its own cleanup (e.g. releasing NCL's
    30-minute edit lock) instead of being abandoned."""
    c = _coordinator(max_concurrent=1)
    seen: list[str] = []

    async def w(booking_id):
        seen.append(booking_id)
        c.request_stop()  # ask to stop after the first booking
        return BookingResult(
            cruise_line=CruiseLine.NCL, status=BookingStatus.NO_SAVING, booking_id=booking_id,
        )

    c.register_line(CruiseLine.NCL, ["N1", "N2", "N3"], w)
    snap = await c.run()
    assert seen == ["N1"], f"stop was not honored between bookings: {seen}"
    assert snap["lines"][0]["state"] == LineState.STOPPED.value


@pytest.mark.asyncio
async def test_pause_and_resume_flags():
    c = _coordinator()
    assert not c.is_paused
    c.pause()
    assert c.is_paused
    c.resume()
    assert not c.is_paused


@pytest.mark.asyncio
async def test_duplicate_bookings_are_deduped_per_line():
    """A duplicate booking id means a duplicate real portal visit."""
    c = _coordinator()
    c.register_line(CruiseLine.NCL, ["N1", "N1", " N1 ", "N2"], _ok_worker(CruiseLine.NCL))
    snap = await c.run()
    assert snap["lines"][0]["total"] == 2
    assert len(c.all_results()) == 2


@pytest.mark.asyncio
async def test_status_snapshot_exposes_everything_the_gui_needs():
    c = _coordinator()
    c.register_line(CruiseLine.NCL, ["N1"], _ok_worker(CruiseLine.NCL))
    snap = await c.run()
    for key in (
        "lines", "resources", "max_concurrent", "paused", "stopping",
        "browser_alive", "live_contexts", "live_cruise_lines",
    ):
        assert key in snap, f"missing status key: {key}"
    for key in ("cpu_percent", "ram_percent", "browser_rss_mb", "throttled"):
        assert key in snap["resources"], f"missing resource key: {key}"


# ── resource governor ────────────────────────────────────────────


def test_governor_sums_child_process_memory():
    """The single most important line in the governor: Chromium's
    renderers are CHILD processes, so measuring only our own RSS would
    understate memory badly and the throttle would never fire."""
    g = ResourceGovernor()
    snap = g.sample_now()
    assert snap.browser_rss_mb > 0


def test_governor_trips_when_threshold_exceeded():
    g = ResourceGovernor(max_cpu_percent=-1.0)  # impossible to satisfy
    snap = g.sample_now()
    assert snap.throttled is True
    assert "CPU" in snap.reason


def test_governor_fails_open_when_sampling_breaks(monkeypatch):
    """A broken thermometer must not wedge every worker forever."""
    import psutil

    def _boom(**kwargs):
        raise RuntimeError("simulated psutil failure")

    g = ResourceGovernor()
    monkeypatch.setattr(psutil, "cpu_percent", _boom)
    snap = g.sample_now()
    assert snap.throttled is False


@pytest.mark.asyncio
async def test_governor_gate_opens_after_stop():
    g = ResourceGovernor(max_cpu_percent=-1.0, sample_interval_s=0.1)
    g.start()
    await asyncio.sleep(0.4)
    assert g.is_throttled
    await g.stop()
    assert not g.is_throttled, "stopping the governor must release blocked workers"


# ── single-instance guard ────────────────────────────────────────


def test_single_instance_guard_blocks_a_second_holder(tmp_path):
    a = SingleInstanceGuard("pytest_scope", lock_dir=str(tmp_path))
    b = SingleInstanceGuard("pytest_scope", lock_dir=str(tmp_path))
    try:
        assert a.acquire() is True
        assert b.acquire() is False, "a second controller was allowed to start"
    finally:
        a.release()


def test_single_instance_guard_reacquirable_after_release(tmp_path):
    a = SingleInstanceGuard("pytest_scope2", lock_dir=str(tmp_path))
    assert a.acquire() is True
    a.release()
    b = SingleInstanceGuard("pytest_scope2", lock_dir=str(tmp_path))
    assert b.acquire() is True
    b.release()
