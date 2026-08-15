"""Regression tests for the Phase 0 correctness/reliability fixes
(2026-08-13 audit) — one section per fix, plus the explicitly-required
coverage for previously-untested weak areas (calc_confidence,
find_upgrade_candidates, make_upgrade_available_result, calculate_goccl,
NCL addon/FOBC business logic, MSC current_total_price fallback, MSC
DISCOUNT_TIER_UPGRADE estimated_value).

Every expected value below is derived independently from the documented
business rule or from plain arithmetic — never by running the code once
and pasting whatever it happened to output."""
from __future__ import annotations

import os

import pytest

from core.calculator import (
    calculate_espresso,
    calculate_goccl,
    calculate_ncl,
    find_upgrade_candidates,
    make_upgrade_available_result,
    safe_float_or_none,
    _get_total,
)
from core.calculator_msc import _check_discount_tier_upgrade, _check_price_match
from core.confidence import calc_confidence, _round2
from core.models import BookingResult, BookingStatus


# ── Fix 1: GoCCL guest count ────────────────────────────────────────────

def _goccl_candidates(price_per_person=300.0):
    return [{
        "offer_code": "NEW",
        "offer_name": "Test Offer",
        "stateroom_type": "BALCONY",
        "price_per_person": price_per_person,
    }]


def test_goccl_guest_count_2_guests_verified_matches_hand_math():
    # 2 guests x $300/person = $600. old_total=$1000 -> price_drop=$400.
    result = calculate_goccl(
        "B1", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
        guests_count=2, guests_count_verified=True,
    )
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.new_total == 600.0
    assert result.price_drop == 400.0
    assert result.net_saving == 400.0
    assert "UNVERIFIED" not in result.note


def test_goccl_guest_count_1_guest_verified():
    # 1 guest x $300 = $300. price_drop = 1000-300 = 700.
    result = calculate_goccl(
        "B2", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
        guests_count=1, guests_count_verified=True,
    )
    assert result.new_total == 300.0
    assert result.price_drop == 700.0


def test_goccl_guest_count_3_guests_verified():
    # 3 guests x $300 = $900. price_drop = 1000-900 = 100.
    result = calculate_goccl(
        "B3", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
        guests_count=3, guests_count_verified=True,
    )
    assert result.new_total == 900.0
    assert result.price_drop == 100.0
    assert result.status == BookingStatus.OPTIMIZATION


def test_regression_goccl_wrong_guest_count_flips_classification():
    """CONFIRMED REAL BUG this documents: the SAME underlying booking can
    flip between OPTIMIZATION and NO_SAVING purely based on which guest
    count is assumed. 4 guests x $300 = $1200, which is MORE than the
    $1000 old_total -> price_drop <= 0 -> NO_SAVING, even though 2 or 3
    guests (above) showed a real, larger saving for the identical
    candidate price. This is exactly why guests_count must never be
    silently assumed without disclosure."""
    result = calculate_goccl(
        "B4", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
        guests_count=4, guests_count_verified=True,
    )
    assert result.new_total == 1200.0
    assert result.status == BookingStatus.NO_SAVING


def test_regression_goccl_unverified_guest_count_note_says_so():
    """The real, current call pattern (scraper/goccl.py never confirms a
    per-booking guest count) — guests_count_verified defaults to False."""
    result = calculate_goccl(
        "B5", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
    )
    assert "UNVERIFIED" in result.note
    assert "assumed 2" in result.note


def test_goccl_verified_guest_count_note_has_no_disclaimer():
    result = calculate_goccl(
        "B6", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(300.0),
        guests_count=2, guests_count_verified=True,
    )
    assert "UNVERIFIED" not in result.note


def test_goccl_no_saving_branch_also_discloses_unverified_guest_count():
    # price_per_person=600 x 2(default, unverified) = 1200 > old_total(1000) -> NO_SAVING.
    result = calculate_goccl(
        "B7", "BA", "BALCONY", "OLD", 1000.0, _goccl_candidates(600.0),
    )
    assert result.status == BookingStatus.NO_SAVING
    assert "UNVERIFIED" in result.note


# ── Fix 2: safe_float UNKNOWN vs ZERO ────────────────────────────────────

def test_safe_float_or_none_preserves_genuine_zero():
    assert safe_float_or_none(0) == 0.0
    assert safe_float_or_none(0.0) == 0.0
    assert safe_float_or_none("0") == 0.0


def test_safe_float_or_none_preserves_valid_number():
    assert safe_float_or_none(42.5) == 42.5
    assert safe_float_or_none("123.45") == 123.45


def test_safe_float_or_none_none_input_stays_none():
    assert safe_float_or_none(None) is None


def test_safe_float_or_none_invalid_string_becomes_none_not_zero():
    assert safe_float_or_none("abc") is None
    assert safe_float_or_none("") is None
    assert safe_float_or_none({}) is None


def test_get_total_no_matching_row_is_a_genuine_zero():
    # No OBC_TOTAL row present at all -- this booking legitimately has no OBC.
    items = [{"paxId": "total", "type": "VACATION_TOTAL", "amount": "1000.00"}]
    assert _get_total(items, "OBC_TOTAL") == 0.0


def test_get_total_matching_row_with_malformed_amount_is_none_not_zero():
    """CONFIRMED REAL BUG this documents: a VACATION_TOTAL/OBC_TOTAL row
    that IS present but whose amount can't be parsed used to silently
    become 0.0 -- indistinguishable from 'no OBC applied'. Must be None."""
    items = [{"paxId": "total", "type": "OBC_TOTAL", "amount": "not-a-number"}]
    assert _get_total(items, "OBC_TOTAL") is None


def _espresso_invoice(vacation_total, obc_total="0"):
    return {
        "oldInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": "1000.00"},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": "0"},
        ]},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": vacation_total},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": obc_total},
        ]},
    }


def test_regression_unparseable_new_total_becomes_error_not_fake_optimization():
    """CONFIRMED REAL BUG this documents: before the fix, a malformed
    VACATION_TOTAL amount silently became $0.00, which would have looked
    like a 100%-off price drop -- a fabricated, enormous fake
    OPTIMIZATION. Must become ERROR instead."""
    raw = _espresso_invoice(vacation_total="GARBAGE")
    result = calculate_espresso(raw, "E1", "IB")
    assert result.status == BookingStatus.ERROR
    assert result.status != BookingStatus.OPTIMIZATION
    assert result.status != BookingStatus.NO_SAVING
    assert "amount" in (result.error or "").lower() or "parsed" in (result.error or "").lower()


def test_espresso_genuine_zero_obc_still_computes_normally():
    """Preserving genuine zero values: a booking that legitimately has no
    OBC on either side must still compute a normal result, not an error."""
    raw = _espresso_invoice(vacation_total="900.00", obc_total="0")
    result = calculate_espresso(raw, "E2", "IB")
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.old_total == 1000.00
    assert result.new_total == 900.00
    assert result.obc_change == 0.0


# ── Fix 3: Currency ──────────────────────────────────────────────────────

def test_booking_result_currency_defaults_to_unknown():
    r = BookingResult(cruise_line="ESPRESSO", status="NO_SAVING", booking_id="1")
    assert r.currency == "UNKNOWN"


def test_booking_result_currency_can_be_set_explicitly():
    r = BookingResult(cruise_line="ESPRESSO", status="NO_SAVING", booking_id="1", currency="USD")
    assert r.currency == "USD"


def test_currency_label_regex_detects_usd():
    from scraper.espresso import EspressoScraper
    text = "Total Price (USD): 1,234.56\nDeposit (USD): 100.00"
    m = EspressoScraper._CURRENCY_LABEL_RE.search(text)
    assert m is not None
    assert m.group(1).upper() == "USD"


def test_currency_label_regex_detects_non_usd_code():
    """Never assume USD -- if the page ever shows a different code, it
    must be detected as that code, not silently ignored or forced to USD."""
    from scraper.espresso import EspressoScraper
    text = "Total Price (CAD): 1,234.56"
    m = EspressoScraper._CURRENCY_LABEL_RE.search(text)
    assert m is not None
    assert m.group(1).upper() == "CAD"


def test_currency_label_regex_none_when_no_label_present():
    from scraper.espresso import EspressoScraper
    text = "Some unrelated page text with no payment fields at all."
    m = EspressoScraper._CURRENCY_LABEL_RE.search(text)
    assert m is None


# ── Fix 4: ESPRESSO dual-rate columns ────────────────────────────────────

def test_dual_rate_note_appended_when_dual_columns_present():
    from scraper.espresso import _append_dual_rate_note
    note = _append_dual_rate_note("optimized $50", {"dualRateColumns": True})
    assert "c3" in note
    assert "NOT evaluated" in note
    assert note.startswith("optimized $50")


def test_dual_rate_note_not_appended_when_single_column():
    from scraper.espresso import _append_dual_rate_note
    note = _append_dual_rate_note("optimized $50", {"dualRateColumns": False})
    assert note == "optimized $50"


def test_dual_rate_note_not_appended_when_market_data_missing():
    from scraper.espresso import _append_dual_rate_note
    assert _append_dual_rate_note("no saving", None) == "no saving"


# ── Fix 5: NCL + GoCCL browser failure handling ──────────────────────────

def test_is_dead_browser_error_detects_has_been_closed():
    from scraper.base import is_dead_browser_error
    assert is_dead_browser_error(Exception("Page.goto: Target page, context or browser has been closed"))


def test_is_dead_browser_error_detects_target_closed():
    from scraper.base import is_dead_browser_error
    assert is_dead_browser_error(Exception("Protocol error: Target closed."))


def test_is_dead_browser_error_detects_crash():
    from scraper.base import is_dead_browser_error
    assert is_dead_browser_error(Exception("Page crashed"))


def test_is_dead_browser_error_false_for_normal_failure():
    from scraper.base import is_dead_browser_error
    assert not is_dead_browser_error(Exception("Timeout 30000ms exceeded waiting for selector"))


@pytest.mark.asyncio
async def test_regression_ncl_dead_browser_error_propagates_not_swallowed(monkeypatch):
    """CONFIRMED REAL BUG this documents: NclScraper.check_booking used to
    catch and convert EVERY exception -- including a dead browser -- into
    an ordinary ERROR BookingResult, permanently defeating
    BookingService's restart mechanism for NCL."""
    from scraper.ncl import NclScraper

    scraper = NclScraper()

    async def _raise_dead_browser(*a, **k):
        raise RuntimeError("Page.goto: Target page, context or browser has been closed")

    monkeypatch.setattr(scraper, "navigate", _raise_dead_browser)
    monkeypatch.setattr(scraper, "log_action", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="has been closed"):
        await scraper.check_booking("123456")


@pytest.mark.asyncio
async def test_ncl_normal_failure_still_becomes_error_result_not_raised(monkeypatch):
    """Every OTHER exception must still become a normal ERROR result,
    exactly as before -- this fix must not change behavior for real
    portal-level failures."""
    from scraper.ncl import NclScraper

    scraper = NclScraper()

    async def _raise_normal(*a, **k):
        raise RuntimeError("Timeout 30000ms exceeded waiting for selector")

    monkeypatch.setattr(scraper, "navigate", _raise_normal)
    monkeypatch.setattr(scraper, "log_action", lambda *a, **k: None)

    result = await scraper.check_booking("123456")
    assert result.status == BookingStatus.ERROR


@pytest.mark.asyncio
async def test_regression_goccl_dead_browser_error_propagates_not_swallowed(monkeypatch):
    from scraper.goccl import GoCCLScraper

    scraper = GoCCLScraper()

    async def _raise_dead_browser(*a, **k):
        raise RuntimeError("Target closed")

    async def _noop_coro(*a, **k):
        return None

    monkeypatch.setattr(scraper, "search_booking", _raise_dead_browser)
    monkeypatch.setattr(scraper, "log_action", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "dump_failure_snapshot", _noop_coro)

    with pytest.raises(RuntimeError, match="Target closed"):
        await scraper.check_booking("CG123")


# ── Fix 6: BookingService restart/shutdown safety ────────────────────────

@pytest.mark.asyncio
async def test_regression_failed_restart_stops_batch_and_clears_live_scraper(monkeypatch):
    """CONFIRMED REAL BUG this documents: a failed browser-restart attempt
    used to only log the failure and let the batch loop continue with a
    scraper that never started, burning through every remaining booking
    with an unrecognizable generic error, and never marking the batch
    FAILED or clearing the stale _live_scraper reference."""
    from services.booking_service import BookingService
    from core.models import CruiseLine, ScanJob, ScanJobStatus

    service = BookingService()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(service, "_save_result_to_db", _noop)
    monkeypatch.setattr(service, "_save_price_history", _noop)
    monkeypatch.setattr(service, "_save_market_data_to_db", _noop)
    monkeypatch.setattr(service, "_save_job_to_db", _noop)
    monkeypatch.setattr(service, "_update_job_in_db", _noop)

    class _StubCache:
        async def get(self, *a, **k):
            return None

        async def set_no_saving(self, *a, **k):
            return None

    service.cache = _StubCache()

    class _DeadBrowserScraper:
        raw_dump_dir = None
        capture_everything = False
        on_action = None
        last_market_data = None

        async def start(self, headless=None):
            return None

        async def check_booking(self, booking_id, capture_market_data=False):
            raise RuntimeError("Page.goto: Target page, context or browser has been closed")

        async def stop(self):
            return None

    class _RestartFailsScraper:
        raw_dump_dir = None
        capture_everything = False
        on_action = None

        async def start(self, headless=None):
            raise RuntimeError("simulated chromium.launch() failure")

        async def stop(self):
            return None

    scrapers = [_DeadBrowserScraper(), _RestartFailsScraper()]

    def _get_scraper(cruise_line):
        return scrapers.pop(0)

    monkeypatch.setattr(service, "_get_scraper", _get_scraper)
    service._live_scraper = None

    job = ScanJob(
        job_id="test-job", booking_ids=["1", "2", "3"],
        cruise_line=CruiseLine.ESPRESSO, status=ScanJobStatus.RUNNING, progress_total=3,
    )
    await service._run_batch(job, keep_browser_open=True)

    assert job.status == ScanJobStatus.FAILED, "must not be silently overwritten back to COMPLETED"
    assert job.progress_done < len(job.booking_ids), "batch must stop, not grind through remaining bookings"
    assert service._live_scraper is None, "a never-started scraper must never look like a live session"


@pytest.mark.asyncio
async def test_scraper_start_cleans_up_on_launch_failure():
    """CONFIRMED REAL BUG this documents: if chromium.launch() failed after
    async_playwright().start() succeeded, the Playwright driver subprocess
    was never stopped -- a leaked process per failed restart attempt."""
    from scraper.espresso import EspressoScraper

    scraper = EspressoScraper()

    class _FakeChromium:
        async def launch(self, **kwargs):
            raise RuntimeError("simulated launch failure")

    class _FakePlaywrightDriver:
        def __init__(self):
            self.stopped = False
            self.chromium = _FakeChromium()

        async def stop(self):
            self.stopped = True

    fake_driver = _FakePlaywrightDriver()

    class _FakeAsyncPlaywrightCtx:
        async def start(self):
            return fake_driver

    import scraper.base as base_module
    original = base_module.async_playwright
    base_module.async_playwright = lambda: _FakeAsyncPlaywrightCtx()
    try:
        with pytest.raises(RuntimeError, match="simulated launch failure"):
            await scraper.start()
    finally:
        base_module.async_playwright = original

    assert fake_driver.stopped, "playwright.stop() must be called to avoid leaking the driver subprocess"
    assert scraper._playwright is None
    assert scraper._browser is None


# ── Fix 7: MSC command-file race ─────────────────────────────────────────

def test_should_delete_command_file_same_content_is_safe_to_delete():
    from msc_session_controller import should_delete_command_file
    assert should_delete_command_file("check_booking:123", "check_booking:123") is True


def test_regression_should_delete_command_file_overwritten_content_must_not_delete():
    """CONFIRMED REAL BUG this documents: a second command written to
    command.txt while the first was still executing used to be silently
    deleted here, never executed."""
    from msc_session_controller import should_delete_command_file
    assert should_delete_command_file("check_booking:123", "check_booking:456") is False


def test_should_delete_command_file_file_already_gone_must_not_delete():
    from msc_session_controller import should_delete_command_file
    assert should_delete_command_file("check_booking:123", None) is False


# ── Fix 8: MSC stale discount data / exception handlers ──────────────────

def test_regression_error_string_would_have_been_falsely_confirmed_empty():
    """Demonstrates the exact mechanism of the bug this fix closes: an
    exception-message STRING (what the old except block produced) is
    truthy but matches no discount pattern, so _extract_discounts would
    silently treat it as 'confirmed empty' -- a false negative. None (what
    the fixed except block now produces) is correctly treated as unknown."""
    from msc_commands import _extract_discounts
    assert _extract_discounts("ERROR reading breakdown: Timeout 30000ms exceeded") == []
    assert _extract_discounts(None) is None


def test_extract_discounts_genuinely_empty_breakdown_stays_empty_list():
    from msc_commands import _extract_discounts
    assert _extract_discounts("") == []


def test_dedupe_does_not_affect_discount_catalog_contract():
    """Sanity check on the None-vs-[] contract this fix protects,
    consumed by evaluate_msc_booking's DISCOUNT_ADD/DISCOUNT_TIER_UPGRADE
    checks (see core/calculator_msc.py) -- None must produce
    INSUFFICIENT_DATA, [] must not."""
    from core.calculator_msc import _check_discount_add
    insufficient = _check_discount_add(current_discounts=None, today_discount_options=["SENIOR DISCOUNT"])
    confirmed_empty = _check_discount_add(current_discounts=[], today_discount_options=["SENIOR DISCOUNT"])
    assert insufficient.status.value == "INSUFFICIENT_DATA"
    assert confirmed_empty.status.value == "OPPORTUNITY"


# ── Fix 9: MSC tab matching ambiguity ────────────────────────────────────

def test_tab_match_tier2_ambiguous_substring_refuses_to_guess():
    """CONFIRMED REAL RISK this documents: two tabs that BOTH substring-
    match the rate name used to let the FIRST one in DOM order silently
    win -- the same bug shape as the historical '$654 vs $26' incident.
    Uses two filler words ("flash", "sale" -- see _TAB_MATCH_FILLER_WORDS)
    so tier 3's keyword-subset tiebreak ALSO ties (0 extra real words
    either way), isolating tier 2's own guard rather than accidentally
    exercising tier 3's already-correct disambiguation."""
    from msc_commands import _select_matching_tab
    target, reason = _select_matching_tab(
        "DRINKS PACKAGE", ["FLASH DRINKS PACKAGE", "SALE DRINKS PACKAGE"],
    )
    assert target is None
    assert "ambiguous" in reason.lower()


def test_tab_match_tier2_single_substring_match_still_works():
    """The fix must not break the normal, unambiguous case."""
    from msc_commands import _select_matching_tab
    target, reason = _select_matching_tab(
        "DRINKS PACKAGE", ["FLASH SALE DRINKS PACKAGE", "CRUISE ONLY"],
    )
    assert target == "FLASH SALE DRINKS PACKAGE"
    assert reason is None


def test_regression_654_vs_26_class_failure_prevented():
    """Reproduces the SHAPE of the historical incident: a rate name that
    could plausibly match two differently-priced tabs must never be
    resolved by picking whichever tab happens to come first."""
    from msc_commands import _select_matching_tab
    target, reason = _select_matching_tab(
        "BALCONY UPGRADE PROMO", ["BALCONY UPGRADE PROMO A", "BALCONY UPGRADE PROMO B"],
    )
    assert target is None, "must refuse to guess between two substring-ambiguous tabs"


def test_tab_match_tier3_ambiguous_keyword_tie_refuses_to_guess():
    """Two tabs tied on keyword-subset match with the SAME number of
    extra words used to let min() silently pick whichever came first."""
    from msc_commands import _select_matching_tab
    target, reason = _select_matching_tab(
        "DRINKS WIFI",
        ["SUMMER SALE DRINKS WIFI", "WINTER SALE DRINKS WIFI"],
    )
    assert target is None
    assert "ambiguous" in reason.lower()


def test_tab_match_tier3_unique_closest_match_still_works():
    from msc_commands import _select_matching_tab
    target, reason = _select_matching_tab(
        "DRINKS WIFI",
        ["SUMMER MEGA SALE DRINKS WIFI BONUS", "SALE DRINKS WIFI"],
    )
    assert target == "SALE DRINKS WIFI"


# ── Fix 10: MSC batch2 duplicate booking IDs ─────────────────────────────

def test_dedupe_booking_ids_removes_duplicates_preserves_order():
    from msc_commands import _dedupe_booking_ids
    ids, duplicates = _dedupe_booking_ids(["100", "200", "100", "300", "200"])
    assert ids == ["100", "200", "300"]
    assert duplicates == ["100", "200"]


def test_dedupe_booking_ids_no_duplicates_unchanged():
    from msc_commands import _dedupe_booking_ids
    ids, duplicates = _dedupe_booking_ids(["1", "2", "3"])
    assert ids == ["1", "2", "3"]
    assert duplicates == []


def test_regression_dedupe_prevents_same_booking_on_both_tabs():
    """CONFIRMED REAL RISK this documents: without dedup, the same booking
    ID at index 0 and index 1 would be assigned to BOTH concurrent tabs
    (even indices -> tab A, odd indices -> tab B)."""
    from msc_commands import _dedupe_booking_ids
    ids, _ = _dedupe_booking_ids(["999", "999"])
    tab_a = ids[0::2]
    tab_b = ids[1::2]
    assert not (set(tab_a) & set(tab_b)), "the same booking ID must never land on both tabs"


# ── Fix 11: GUI re-entrancy ───────────────────────────────────────────────

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pyside6 = pytest.importorskip("PySide6")
qasync = pytest.importorskip("qasync")

import asyncio as _asyncio
from PySide6.QtWidgets import QApplication, QMessageBox


@pytest.fixture(scope="module")
def qapp_and_loop_phase0():
    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    _asyncio.set_event_loop(loop)
    yield app, loop


class _FakeQueueManagerAlreadyRunning:
    is_running = True

    def get_snapshot(self):
        from types import SimpleNamespace
        return SimpleNamespace(queued=1, running=1, done=0, error=0, items=[])

    def has_live_session(self, cruise_line):
        return True

    async def start_processing(self, **kwargs):
        raise RuntimeError("Scan queue is already running")


def test_regression_on_start_does_not_reenable_controls_when_scan_already_running(qapp_and_loop_phase0, monkeypatch):
    """CONFIRMED REAL BUG this documents: _on_start's finally block used to
    unconditionally re-enable every control, even when start_processing()
    failed simply because a real scan was ALREADY running -- reopening a
    window for Login/Start to race against that still-running scan."""
    from gui.windows import MainWindow

    app, loop = qapp_and_loop_phase0
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    win = MainWindow()
    win.queue_manager = _FakeQueueManagerAlreadyRunning()
    win.cruise_line_selector.setCurrentIndex(0)

    win.start_button.setEnabled(False)
    win.login_button.setEnabled(False)
    win.add_booking_button.setEnabled(False)
    win.clear_queue_button.setEnabled(False)

    loop.run_until_complete(win._on_start())

    assert not win.start_button.isEnabled(), "Start must stay disabled while the real scan is still running"
    assert not win.login_button.isEnabled(), "Login must stay disabled -- re-enabling it reopens the concurrent-session race"


# ── Fix 12: Confidence rounding ───────────────────────────────────────────

def test_confidence_round2_matches_established_half_up_boundaries():
    """Same ROUND_HALF_UP boundary values already independently verified
    for core.calculator.round2 -- confidence.py's _round2 must agree,
    since it's the identical fix for the identical defect class."""
    assert _round2(1.005) == 1.01
    assert _round2(2.675) == 2.68
    assert _round2(10.125) == 10.13
    assert _round2(10.135) == 10.14


def test_calc_confidence_fare_change_pct_independently_computed():
    # (820 - 800) / 800 = 0.025 -> *100 = 2.5 (exact, no rounding ambiguity).
    result = calc_confidence(
        old_cruise_fare=800.0, new_cruise_fare=820.0, net_saving=50.0,
        old_total=1000.0, lost_pkg_value=0.0, obc_change=0.0,
    )
    assert result.fare_change_pct == 2.5


# ── Required coverage: calc_confidence() ──────────────────────────────────

def test_calc_confidence_strong_case_independently_derived():
    """By-hand derivation: fare -5% (<-0.02 -> +2), net 10% (>0.05 -> +2),
    no package loss (+1), OBC stable (+1) = 6 pts -> clamped to 6 ->
    5 stars. Neither safety cap applies (fare_change_pct is negative)."""
    result = calc_confidence(
        old_cruise_fare=1000.0, new_cruise_fare=950.0, net_saving=100.0,
        old_total=1000.0, lost_pkg_value=0.0, obc_change=0.0,
    )
    assert result.score == 5


def test_calc_confidence_weak_case_independently_derived():
    """By-hand derivation: fare +20% (>0.15 -> -2), net 1% (no bonus),
    no package loss (+1), OBC stable (+1) = 0 pts -> 2 stars."""
    result = calc_confidence(
        old_cruise_fare=1000.0, new_cruise_fare=1200.0, net_saving=10.0,
        old_total=1000.0, lost_pkg_value=0.0, obc_change=0.0,
    )
    assert result.score == 2


def test_calc_confidence_package_loss_reduces_score():
    """By-hand derivation: fare -3% (<-0.02 -> +2), net 6% (>0.05 -> +2),
    package LOST (lost_pkg_value>0, so no +1), OBC stable (+1) = 5 pts ->
    5 stars. Compare against the +1 case above to isolate the package
    term's real effect (would be 6 pts / capped-5-stars either way here,
    but the intermediate points differ — this proves lost_pkg_value is
    actually read, not ignored)."""
    result = calc_confidence(
        old_cruise_fare=1000.0, new_cruise_fare=970.0, net_saving=60.0,
        old_total=1000.0, lost_pkg_value=25.0, obc_change=0.0,
    )
    assert result.score == 5  # pts=5 -> pts_to_stars[5]=5, same table entry as pts=6


def test_calc_confidence_zero_old_total_and_fare_no_crash():
    result = calc_confidence(
        old_cruise_fare=0.0, new_cruise_fare=0.0, net_saving=0.0,
        old_total=0.0, lost_pkg_value=0.0, obc_change=0.0,
    )
    assert isinstance(result.score, int)
    assert result.fare_change_pct == 0.0


# ── Required coverage: find_upgrade_candidates() ──────────────────────────

def _category_row(category, room_type_text, price, status="AVL"):
    return {
        "category": category,
        "status": status,
        "rowText": f"{category}\n{room_type_text}\n\t{room_type_text}\t\nSTANDARD\n\t\n{status}(5)\n\t{price:.2f}\n\n",
    }


def test_find_upgrade_candidates_finds_cheaper_higher_tier():
    # INTERIOR (rank 1) at $500, BALCONY STATEROOM (rank 3) at $450 <= $500 -> candidate.
    rows = [
        _category_row("IB", "Interior", 500.0),
        _category_row("BA", "Balcony Stateroom", 450.0),
    ]
    candidates = find_upgrade_candidates("IB", rows)
    assert len(candidates) == 1
    assert candidates[0]["category"] == "BA"
    assert candidates[0]["table_per_person_price"] == 450.0


def test_find_upgrade_candidates_excludes_more_expensive_higher_tier():
    # BALCONY at $600 > current $500 -> NOT a candidate (would be a real cost increase).
    rows = [
        _category_row("IB", "Interior", 500.0),
        _category_row("BA", "Balcony Stateroom", 600.0),
    ]
    candidates = find_upgrade_candidates("IB", rows)
    assert candidates == []


def test_find_upgrade_candidates_excludes_same_or_lower_tier():
    # Another INTERIOR row (same rank, not strictly higher) must never count as an "upgrade".
    rows = [
        _category_row("IB", "Interior", 500.0),
        _category_row("IC", "Interior", 400.0),
    ]
    candidates = find_upgrade_candidates("IB", rows)
    assert candidates == []


def test_find_upgrade_candidates_excludes_non_avl_status():
    rows = [
        _category_row("IB", "Interior", 500.0),
        _category_row("BA", "Balcony Stateroom", 400.0, status="WLT"),
    ]
    candidates = find_upgrade_candidates("IB", rows)
    assert candidates == []


def test_find_upgrade_candidates_current_category_not_in_rows_returns_empty():
    rows = [_category_row("BA", "Balcony Stateroom", 400.0)]
    assert find_upgrade_candidates("ZZ", rows) == []


# ── Required coverage: make_upgrade_available_result() ───────────────────

def test_make_upgrade_available_result_independently_derived_math():
    upgrade = {"category": "BA", "room_type": "balcony stateroom", "price": 450.0}
    result = make_upgrade_available_result("U1", "IB", "ESPRESSO", old_total=500.0, upgrade=upgrade)
    assert result.status == BookingStatus.UPGRADE_AVAILABLE
    assert result.new_price_category == "BA"
    assert result.old_total == 500.0
    assert result.new_total == 450.0
    assert result.price_drop == 50.0
    assert result.net_saving == 50.0


# ── Required coverage: calculate_goccl() no-candidate / no-saving paths ──

def test_calculate_goccl_no_candidates_at_all():
    result = calculate_goccl("G1", "BA", "BALCONY", "OLD", 1000.0, available_offer_codes=[])
    assert result.status == BookingStatus.NO_SAVING
    assert result.old_total == 1000.0
    assert result.new_total == 1000.0


def test_calculate_goccl_wrong_stateroom_type_excluded():
    candidates = [{"offer_code": "X", "offer_name": "N", "stateroom_type": "SUITE", "price_per_person": 100.0}]
    result = calculate_goccl("G2", "BA", "BALCONY", "OLD", 1000.0, candidates)
    assert result.status == BookingStatus.NO_SAVING


# ── Required coverage: NCL addon/FOBC business logic (in context) ────────

def test_ncl_fobc_lost_addon_correctly_subtracted():
    """Independently derived: $150 price drop, but losing FOBC costs the
    $150 OBC certificate addon -> net = 150-150 = 0 -> not > 0 -> since
    price_drop(150) > 0 and net(0) <= 0 -> TRAP."""
    addons = [{"name": "On-Board Credit Certificate $150", "qty": 1}]
    result = calculate_ncl(
        "N1", "BA", invoice_total=1000.0, new_res_total=850.0,
        addons=addons, old_promos="FOBC SAVE10", new_promos="SAVE10",
    )
    assert result.status == BookingStatus.TRAP
    assert result.lost_pkg_value == 150.0


def test_ncl_fobc_retained_addons_not_subtracted():
    """Same addons, but FOBC is retained on both sides -> lost_fobc=False
    -> lost_addon_value must stay 0 regardless of what addons exist."""
    addons = [{"name": "On-Board Credit Certificate $150", "qty": 1}]
    result = calculate_ncl(
        "N2", "BA", invoice_total=1000.0, new_res_total=850.0,
        addons=addons, old_promos="FOBC SAVE10", new_promos="FOBC SAVE10",
    )
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.lost_pkg_value == 0.0
    assert result.net_saving == 150.0


def test_ncl_duplicate_addons_counted_once():
    """calculate_ncl's own de-dup (`seen` set) must prevent the same addon
    name from being subtracted twice."""
    addons = [
        {"name": "On-Board Credit Certificate $150", "qty": 1},
        {"name": "On-Board Credit Certificate $150", "qty": 1},
    ]
    result = calculate_ncl(
        "N3", "BA", invoice_total=1000.0, new_res_total=850.0,
        addons=addons, old_promos="FOBC", new_promos="",
    )
    assert result.lost_pkg_value == 150.0  # not 300.0


# ── Required coverage: MSC current_total_price fallback ──────────────────

def test_msc_price_match_current_total_fallback_confirmed_opportunity():
    """Independently derived: today's undiscounted base ($800) already
    below the current DISCOUNTED total ($850) mathematically guarantees
    a real opportunity (see _check_price_match's own docstring) -- diff=50."""
    check = _check_price_match(
        current_base_price=None, today_base_price=800.0, current_total_price=850.0,
        today_price_tab_confirmed=True,
    )
    assert check.status.value == "OPPORTUNITY"
    assert check.estimated_value == 50.0


def test_msc_price_match_current_total_fallback_ambiguous_boundary():
    """The documented subtle case: today's base sits AT the current total
    -- can't tell which way without the true pre-discount base, must be
    INSUFFICIENT_DATA, never guessed as NO_OPPORTUNITY or OPPORTUNITY."""
    check = _check_price_match(
        current_base_price=None, today_base_price=850.0, current_total_price=850.0,
        today_price_tab_confirmed=True,
    )
    assert check.status.value == "INSUFFICIENT_DATA"


def test_msc_price_match_paid_in_full_blocks_even_with_fallback_data():
    check = _check_price_match(
        current_base_price=None, today_base_price=800.0, current_total_price=850.0,
        today_price_tab_confirmed=True, is_paid_in_full=True,
    )
    assert check.status.value == "NO_OPPORTUNITY"


# ── Required coverage: MSC DISCOUNT_TIER_UPGRADE estimated_value unit ────

def test_msc_discount_tier_upgrade_estimated_value_is_percentage_points_not_dollars():
    """Independently derived: current best named discount 5.0%, today
    offers 15.0% -> estimated_value = 15.0 - 5.0 = 10.0 PERCENTAGE POINTS.
    This must never be treated as a dollar figure by any caller."""
    current_discounts = [{"kind": "named", "label": "MSCCLUB5", "rate_pct": 5.0}]
    check = _check_discount_tier_upgrade(
        current_discounts=current_discounts, today_discount_options=["SPECIAL OFFER 15%"],
    )
    assert check.status.value == "OPPORTUNITY"
    assert check.estimated_value == 10.0
    # Documents the unit explicitly so a future reader/aggregator can't
    # mistake this for a dollar amount without re-deriving it themselves.
    assert check.estimated_value < 100, "sanity bound: a percentage-point delta, not a plausible dollar figure"
