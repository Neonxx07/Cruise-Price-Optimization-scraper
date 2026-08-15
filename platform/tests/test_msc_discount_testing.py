"""Tests for the live discount price-testing pipeline (2026-08-13,
forensic investigation of bookings 2000017/2000020).

These validate the LOGIC of test_discount_candidate/_apply_discount_candidate/
_wait_for_post_discount_price/generate_discount_candidates using fake
Playwright objects -- never a live browser. The ground-truth regression
case below uses the REAL numbers observed live on booking 2000017
($2,588.72 -> $2,565.26 -> $23.46), fed into the pipeline as a MOCKED
scenario -- never hardcoded into production logic. The point of every
test here is that a dollar figure, or the absence of one, is only ever
trustworthy because each contributing step was independently verified;
these tests prove that property holds under success, partial failure,
and safety-alarm conditions.
"""
from __future__ import annotations

import pytest

import msc_commands
from core.models import (
    MscDiscountApplicationMethod,
    MscDiscountCandidate,
    MscDiscountTestStatus,
)


# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeOptionsLocator:
    def __init__(self, texts):
        self._texts = texts

    async def all_text_contents(self):
        return self._texts


class _FakeSelectLocator:
    def __init__(self, option_texts, select_calls, index):
        self._option_texts = option_texts
        self._calls = select_calls
        self._index = index

    def locator(self, sel):
        assert sel == "option"
        return _FakeOptionsLocator(self._option_texts)

    async def select_option(self, label=None, force=None, timeout=None):
        self._calls.append({"index": self._index, "label": label})


class _FakeSelectsLocator:
    def __init__(self, selects_option_texts, select_calls):
        self._selects = selects_option_texts
        self._calls = select_calls

    async def count(self):
        return len(self._selects)

    def nth(self, i):
        return _FakeSelectLocator(self._selects[i], self._calls, i)


class FakePage:
    """Minimal fake of the Playwright Page surface this pipeline touches.
    `inner_text_sequence` is consumed one value per page.inner_text() call
    (falls back to repeating the last value once exhausted) -- lets a test
    simulate the page's text changing over the course of a poll loop."""

    def __init__(self, selects_option_texts=None, evaluate_results=None,
                 inner_text_sequence=None, url="https://www.mscbook.com/x?partNumber=VI20260905SOUSOU"):
        self._selects_option_texts = selects_option_texts or []
        self.select_calls = []
        self.fill_calls = []
        self.evaluate_calls = []
        self._evaluate_results = list(evaluate_results or [])
        self.wait_calls = 0
        self._inner_text_sequence = list(inner_text_sequence or [""])
        self.url = url

    def locator(self, sel):
        assert sel == "select"
        return _FakeSelectsLocator(self._selects_option_texts, self.select_calls)

    async def wait_for_timeout(self, ms):
        self.wait_calls += 1

    async def evaluate(self, js):
        self.evaluate_calls.append(js)
        if self._evaluate_results:
            return self._evaluate_results.pop(0)
        return None

    async def fill(self, sel, value, timeout=None):
        self.fill_calls.append((sel, value))

    async def inner_text(self, sel):
        if len(self._inner_text_sequence) > 1:
            return self._inner_text_sequence.pop(0)
        return self._inner_text_sequence[0]


# ── _apply_discount_candidate: DROPDOWN_OPTION ──────────────────────────


@pytest.mark.asyncio
async def test_apply_discount_dropdown_success():
    page = FakePage(selects_option_texts=[["Select Special Discounts", "SENIOR DISCOUNT", "MIL-CIV-IL-DSCNT-10%"]])
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands._apply_discount_candidate(page, candidate)

    assert result["success"] is True
    assert page.select_calls == [{"index": 0, "label": "SENIOR DISCOUNT"}]


@pytest.mark.asyncio
async def test_apply_discount_dropdown_option_not_present():
    page = FakePage(selects_option_texts=[["Select Special Discounts", "MIL-CIV-IL-DSCNT-10%"]])
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands._apply_discount_candidate(page, candidate)

    assert result["success"] is False
    assert not page.select_calls
    assert "SENIOR DISCOUNT" in result["reason"]


# ── _apply_discount_candidate: VOYAGERS_CLUB_INSERT ─────────────────────


@pytest.mark.asyncio
async def test_apply_discount_voyagers_missing_fields_never_touches_page():
    page = FakePage()
    candidate = MscDiscountCandidate(
        label="Voyagers Club", method=MscDiscountApplicationMethod.VOYAGERS_CLUB_INSERT,
        voyagers_first_name="DANY", voyagers_last_name="AZZI", voyagers_dob=None, voyagers_card_number="4813462",
    )

    result = await msc_commands._apply_discount_candidate(page, candidate)

    assert result["success"] is False
    assert not page.evaluate_calls
    assert not page.fill_calls


@pytest.mark.asyncio
async def test_apply_discount_voyagers_success_fills_real_fields():
    page = FakePage(evaluate_results=[True, None])
    candidate = MscDiscountCandidate(
        label="Voyagers Club", method=MscDiscountApplicationMethod.VOYAGERS_CLUB_INSERT,
        voyagers_first_name="DANY", voyagers_last_name="AZZI",
        voyagers_dob="06/15/1965", voyagers_card_number="4813462",
    )

    result = await msc_commands._apply_discount_candidate(page, candidate)

    assert result["success"] is True
    assert ("#club-firstname", "DANY") in page.fill_calls
    assert ("#club-card", "4813462") in page.fill_calls


# ── _wait_for_post_discount_price ───────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_post_discount_price_finds_category_listing_price():
    text = "IB\nInterior\n\tInterior\t\nSTANDARD\n\t\nAVL(5)\n\t(BL2)\n\t$ 2,565.26\n\n"
    page = FakePage(inner_text_sequence=[text])

    evidence = await msc_commands._wait_for_post_discount_price(page, "BL2", False, timeout_s=1, poll_s=0.01)

    assert evidence["price_str"] is not None
    assert evidence["source"] == "category_listing"


@pytest.mark.asyncio
async def test_wait_for_post_discount_price_falls_back_to_price_breakdown_line():
    """CONFIRMED REAL FIX this documents: no rate tabs / category listing
    at all, but a real Price Breakdown line IS on the page -- must still
    find a usable price rather than giving up."""
    text = "Price Breakdown\nTotal Stateroom Price: $2,565.26\nCommissions: $300.00\n"
    page = FakePage(inner_text_sequence=[text])

    evidence = await msc_commands._wait_for_post_discount_price(page, "BL2", False, timeout_s=1, poll_s=0.01)

    assert evidence["price_str"] == "2,565.26"
    assert evidence["source"] == "price_breakdown"


@pytest.mark.asyncio
async def test_wait_for_post_discount_price_polls_until_evidence_appears():
    """Condition-based wait: the first read has nothing, the second does
    -- must not give up after a single check (this is exactly the timing
    fragility class this project's own history has hit before)."""
    page = FakePage(inner_text_sequence=["still loading...", "(BL2)\n$ 2,565.26"])

    evidence = await msc_commands._wait_for_post_discount_price(page, "BL2", False, timeout_s=2, poll_s=0.01)

    assert evidence["price_str"] is not None


@pytest.mark.asyncio
async def test_wait_for_post_discount_price_genuine_timeout_returns_none():
    page = FakePage(inner_text_sequence=["nothing useful here, ever"])

    evidence = await msc_commands._wait_for_post_discount_price(page, "BL2", False, timeout_s=0.05, poll_s=0.01)

    assert evidence["price_str"] is None


# ── generate_discount_candidates ────────────────────────────────────────


def test_generate_discount_candidates_excludes_military():
    staged = {"discount_options": ["SENIOR DISCOUNT", "MIL-CIV-IL-DSCNT-10%", "MIL-CIV-IL-DSCNT-05%"], "is_group_rate": False}

    candidates = msc_commands.generate_discount_candidates(staged)

    labels = [c.label for c in candidates]
    assert "SENIOR DISCOUNT" in labels
    assert not any("MIL" in l for l in labels)


def test_generate_discount_candidates_group_rate_yields_none():
    staged = {"discount_options": ["SENIOR DISCOUNT"], "is_group_rate": True}

    candidates = msc_commands.generate_discount_candidates(staged)

    assert candidates == []


def test_generate_discount_candidates_no_options_yields_empty_list_not_none():
    staged = {"discount_options": None, "is_group_rate": False}

    candidates = msc_commands.generate_discount_candidates(staged)

    assert candidates == []


# ── test_discount_candidate: full pipeline ──────────────────────────────


def _patch_pipeline(monkeypatch, *, baseline_value="1,000.00", verification_value="1,000.00",
                     category="BR1", staged_status=None, occupancy_stalled=False,
                     apply_success=True, apply_reason="ok",
                     confirm_success=True, tab_matched=True,
                     post_price_evidence=None, part_number="VI20260905SOUSOU",
                     verification_part_number="VI20260905SOUSOU",
                     session_expired_baseline=False, rate_name="CRUISE ONLY OBC INCLUDED"):
    if post_price_evidence is None:
        post_price_evidence = {"price_str": "900.00", "source": "category_listing", "text_excerpt": ""}

    async def fake_lookup(page, booking_id):
        if session_expired_baseline:
            return {"session_expired": True}
        value = baseline_value if fake_lookup.calls == 0 else verification_value
        pn = part_number if fake_lookup.calls == 0 else verification_part_number
        fake_lookup.calls += 1
        page.url = f"https://www.mscbook.com/x?partNumber={pn}"
        return {"summary_text": f"Booking\n{booking_id}\nCONFIRMED\nBooking Value\n${value}\n"
                                 f"Cabin  1 - N°1 SOMETHING   ({category})\n"}
    fake_lookup.calls = 0

    async def fake_stage(page, booking_id):
        return {
            "found": True, "status": staged_status, "category": category,
            "current_value": baseline_value, "rate_name": rate_name,
            "is_guaranteed": False,
            "occupancy_fix": {"stalled": occupancy_stalled, "before": {}, "required": {}, "after": {}},
        }

    async def fake_apply(page, candidate):
        return {"success": apply_success, "reason": apply_reason}

    async def fake_confirm(page):
        return confirm_success

    async def fake_match_tab(page, rate_name):
        return {"matched": tab_matched, "reason": None if tab_matched else "ambiguous", "active_tab": rate_name}

    async def fake_wait_price(page, cat, is_guaranteed, timeout_s=10.0, poll_s=0.5):
        return post_price_evidence

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup)
    monkeypatch.setattr(msc_commands, "_stage_booking_for_confirm", fake_stage)
    monkeypatch.setattr(msc_commands, "_apply_discount_candidate", fake_apply)
    monkeypatch.setattr(msc_commands, "_confirm_and_proceed_click", fake_confirm)
    monkeypatch.setattr(msc_commands, "_match_rate_tab", fake_match_tab)
    monkeypatch.setattr(msc_commands, "_wait_for_post_discount_price", fake_wait_price)


@pytest.mark.asyncio
async def test_regression_2000017_ground_truth_confirmed_optimization(monkeypatch):
    """Real observed numbers from the live session: $2,588.72 -> $2,565.26.
    Fed in as a mocked scenario -- NOT hardcoded into test_discount_candidate
    itself, which only ever sees whatever its (mocked) dependencies return."""
    _patch_pipeline(
        monkeypatch, baseline_value="2,588.72", verification_value="2,588.72", category="BL2",
        tab_matched=False,  # exactly what the real live attempt hit
        post_price_evidence={"price_str": "2,565.26", "source": "price_breakdown", "text_excerpt": "..."},
    )
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.CONFIRMED_OPTIMIZATION
    assert result.price_before == 2588.72
    assert result.price_after == 2565.26
    assert result.actual_savings == 23.46
    assert result.rate_tab_confirmed is False  # honestly recorded, not hidden
    assert result.restoration_verified is True
    assert result.price_source == "price_breakdown"


@pytest.mark.asyncio
async def test_regression_price_source_recorded_for_diagnosability(monkeypatch):
    """CONFIRMED REAL GAP this documents: a live retest of 2000017
    produced a DIFFERENT number ($2,559.00) than a human had separately
    observed live ($2,565.26), and there was no way to tell which of the
    two fallback strategies actually produced it. price_source must
    always be populated whenever price_after is."""
    _patch_pipeline(monkeypatch, post_price_evidence={"price_str": "900.00", "source": "category_listing", "text_excerpt": ""})
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.price_source == "category_listing"


@pytest.mark.asyncio
async def test_regression_no_rate_tabs_no_longer_hard_fails_when_price_available(monkeypatch):
    """CONFIRMED REAL BUG this documents (the exact failure from the first
    live test): tab_matched=False used to be an immediate INSUFFICIENT_DATA
    with everything else discarded. Must now still reach a real result."""
    _patch_pipeline(monkeypatch, tab_matched=False, post_price_evidence={"price_str": "900.00", "source": "price_breakdown", "text_excerpt": ""})
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status in (MscDiscountTestStatus.CONFIRMED_OPTIMIZATION, MscDiscountTestStatus.CONFIRMED_NO_SAVINGS)
    assert result.price_after == 900.00


@pytest.mark.asyncio
async def test_regression_evidence_never_discarded_on_post_price_not_found(monkeypatch):
    """CONFIRMED REAL BUG this documents: price_before/application_success/
    confirm_success used to reset to defaults on ANY later failure. All
    three must survive into the final result even when the price genuinely
    can't be found anywhere on the page."""
    _patch_pipeline(monkeypatch, baseline_value="2,588.72", tab_matched=False,
                     post_price_evidence={"price_str": None, "source": None, "text_excerpt": "nothing"})
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.POST_PRICE_NOT_FOUND
    assert result.price_before == 2588.72  # NOT None
    assert result.application_success is True  # NOT False
    assert result.confirm_success is True  # NOT False
    assert result.actual_savings is None


@pytest.mark.asyncio
async def test_discount_candidate_application_failed_preserves_baseline(monkeypatch):
    _patch_pipeline(monkeypatch, baseline_value="1,000.00", apply_success=False, apply_reason="no matching option")
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.DISCOUNT_APPLICATION_FAILED
    assert result.price_before == 1000.00
    assert result.application_success is False
    assert result.actual_savings is None


@pytest.mark.asyncio
async def test_discount_candidate_confirm_failed(monkeypatch):
    _patch_pipeline(monkeypatch, confirm_success=False)
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.CONFIRM_FAILED
    assert result.application_success is True  # discount selection DID succeed
    assert result.confirm_success is False
    assert result.actual_savings is None


@pytest.mark.asyncio
async def test_discount_candidate_occupancy_stalled_stops_before_applying(monkeypatch):
    """Occupancy is part of price identity -- must not even attempt the
    discount if occupancy couldn't be safely established."""
    _patch_pipeline(monkeypatch, occupancy_stalled=True)
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.OCCUPANCY_MISMATCH
    assert result.application_attempted is False


@pytest.mark.asyncio
async def test_discount_candidate_identity_validation_failed_on_partnumber_change(monkeypatch):
    """Simulates the sailing identity changing between baseline capture and
    the post-discount price read -- e.g. a stray navigation mid-experiment.
    The confirm step is the mock that flips page.url, since that's the
    real interaction point between baseline capture and price reading."""
    page = FakePage(url="https://www.mscbook.com/x?partNumber=VI20260905SOUSOU")

    async def fake_lookup(page, booking_id):
        return {"summary_text": f"Booking\n{booking_id}\nCONFIRMED\nBooking Value\n$1,000.00\n"
                                 f"Cabin  1 - N°1 SOMETHING   (BR1)\n"}

    async def fake_stage(page, booking_id):
        return {"found": True, "status": None, "category": "BR1", "current_value": "1,000.00",
                "rate_name": "CRUISE ONLY OBC INCLUDED", "is_guaranteed": False,
                "occupancy_fix": {"stalled": False}}

    async def fake_apply(page, candidate):
        return {"success": True, "reason": "ok"}

    async def fake_confirm(page):
        page.url = "https://www.mscbook.com/x?partNumber=DIFFERENT123"
        return True

    async def fake_match_tab(page, rate_name):
        return {"matched": True, "reason": None, "active_tab": rate_name}

    async def fake_wait_price(page, cat, is_guaranteed, timeout_s=10.0, poll_s=0.5):
        return {"price_str": "900.00", "source": "category_listing", "text_excerpt": ""}

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup)
    monkeypatch.setattr(msc_commands, "_stage_booking_for_confirm", fake_stage)
    monkeypatch.setattr(msc_commands, "_apply_discount_candidate", fake_apply)
    monkeypatch.setattr(msc_commands, "_confirm_and_proceed_click", fake_confirm)
    monkeypatch.setattr(msc_commands, "_match_rate_tab", fake_match_tab)
    monkeypatch.setattr(msc_commands, "_wait_for_post_discount_price", fake_wait_price)

    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)
    result = await msc_commands.test_discount_candidate({"page": page}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.IDENTITY_VALIDATION_FAILED
    assert result.actual_savings is None
    assert result.price_before == 1000.00  # preserved despite the failure


@pytest.mark.asyncio
async def test_regression_restoration_failed_never_reports_savings(monkeypatch):
    _patch_pipeline(monkeypatch, baseline_value="1,000.00", verification_value="1,050.00")
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.RESTORATION_FAILED
    assert result.actual_savings is None
    assert result.price_before == 1000.00
    assert result.price_after == 900.00


@pytest.mark.asyncio
async def test_discount_candidate_price_increase_is_confirmed_no_savings_not_hidden(monkeypatch):
    _patch_pipeline(monkeypatch, baseline_value="1,000.00",
                     post_price_evidence={"price_str": "1,050.00", "source": "category_listing", "text_excerpt": ""})
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.CONFIRMED_NO_SAVINGS
    assert result.actual_savings == -50.00


@pytest.mark.asyncio
async def test_discount_candidate_unchanged_price_is_confirmed_no_savings(monkeypatch):
    _patch_pipeline(monkeypatch, baseline_value="1,000.00",
                     post_price_evidence={"price_str": "1,000.00", "source": "category_listing", "text_excerpt": ""})
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.CONFIRMED_NO_SAVINGS
    assert result.actual_savings == 0.0


@pytest.mark.asyncio
async def test_discount_candidate_session_expired_baseline(monkeypatch):
    """Both lookup attempts (initial + post-relogin) report session_expired
    -- must give up honestly, not loop forever or guess."""
    async def fake_lookup(page, booking_id):
        return {"session_expired": True}

    async def fake_auto_login(page):
        return "still expired"

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup)
    monkeypatch.setattr(msc_commands, "auto_login", fake_auto_login)
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert result.actual_savings is None
    assert "relogin" in result.reason


@pytest.mark.asyncio
async def test_regression_session_expired_recovers_via_one_relogin_retry(monkeypatch):
    """CONFIRMED REAL GAP this documents: a live retest hit a genuinely
    expired session with no way to recover, unlike _check_booking_msc's
    existing one-retry-via-relogin pattern. auto_login() must be called
    exactly once, using the SAME already-open page/session -- no new
    browser, no restart."""
    calls = {"lookup": 0, "login": 0}

    async def fake_lookup(page, booking_id):
        calls["lookup"] += 1
        if calls["lookup"] == 1:
            return {"session_expired": True}
        return {"summary_text": f"Booking\n{booking_id}\nCONFIRMED\nBooking Value\n$1,000.00\n"
                                 f"Cabin  1 - N°1 SOMETHING   (BR1)\n"}

    async def fake_auto_login(page):
        calls["login"] += 1
        return "OK"

    _patch_pipeline(monkeypatch, baseline_value="1,000.00")
    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup)
    monkeypatch.setattr(msc_commands, "auto_login", fake_auto_login)
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert calls["login"] == 1
    assert result.price_before == 1000.00
    assert result.status != MscDiscountTestStatus.INSUFFICIENT_DATA


@pytest.mark.asyncio
async def test_discount_candidate_unexpected_exception_is_error_not_crash(monkeypatch):
    async def fake_lookup_raises(page, booking_id):
        raise RuntimeError("simulated Playwright crash")

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup_raises)
    candidate = MscDiscountCandidate(label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION)

    result = await msc_commands.test_discount_candidate({"page": FakePage()}, "2000017", candidate)

    assert result.status == MscDiscountTestStatus.ERROR
    assert result.actual_savings is None


# ── Formatting ───────────────────────────────────────────────────────────


def test_format_discount_test_result_distinguishes_statuses():
    from core.models import MscDiscountTestResult

    confirmed = MscDiscountTestResult(
        booking_id="1", candidate_label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION,
        status=MscDiscountTestStatus.CONFIRMED_OPTIMIZATION, price_before=100.0, price_after=90.0, actual_savings=10.0,
        rate_tab_confirmed=False,
    )
    failed = MscDiscountTestResult(
        booking_id="1", candidate_label="SENIOR DISCOUNT", method=MscDiscountApplicationMethod.DROPDOWN_OPTION,
        status=MscDiscountTestStatus.POST_PRICE_NOT_FOUND, price_before=100.0, application_attempted=True,
        application_success=True, confirm_attempted=True, confirm_success=True, reason="no evidence found",
    )

    confirmed_msg = msc_commands._format_discount_test_result(confirmed)
    failed_msg = msc_commands._format_discount_test_result(failed)

    assert "CONFIRMED OPTIMIZATION" in confirmed_msg
    assert "$10.00" in confirmed_msg
    assert "POST_PRICE_NOT_FOUND" in failed_msg
    assert "baseline=$100.00" in failed_msg
    assert "discount_applied=True" in failed_msg
