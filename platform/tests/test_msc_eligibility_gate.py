"""Regression tests for the 2026-08-27 MSC safety gates.

Two real defects, both of which allowed an AUTHORITATIVE-looking
`CONFIRMED_OPTIMIZATION` dollar figure on a booking where it was invalid:

1. **Eligibility bypass.** `generate_discount_candidates()` holds every
   hard eligibility rule (military/MIL-CIV never applied, senior discount
   needs 2+ seniors, Group Rate capped to the flat Voyagers 5%) but a
   repo-wide grep confirmed it was called ONLY from tests — never from
   production. The live `test_discount:<id>:<label>` command built a
   candidate straight from typed text, so the exact 2026-08-18 false
   positive (booking 3000030, a lone senior) ran unguarded here.

2. **Multi-cabin.** Every selector in the discount-test flow targets
   `data-cabin="1"`, so on a 2-cabin booking the baseline covers all
   cabins while the discounted price covers one — the "savings" is wrong
   by roughly the other cabins, reported as confirmed.

The gates live inside `test_discount_candidate` (not the command handler)
so EVERY caller is covered.
"""
import pytest

import msc_commands
from core.models import (
    MscDiscountApplicationMethod,
    MscDiscountCandidate,
    MscDiscountTestStatus,
)


def _staged(**overrides):
    """A realistic staged dict — the shape _stage_booking_for_confirm
    actually returns."""
    base = {
        "found": True,
        "status": "staged",
        "category": "BR1",
        "current_value": "2,588.72",
        "rate_name": "CRUISE ONLY OBC INCLUDED",
        "is_guaranteed": False,
        "occupancy_fix": {"stalled": False},
        "discount_options": ["SENIOR DISCOUNT", "TODAY10", "MIL-CIV-IL-DSCNT-10%"],
        "senior_count": 2,
        "is_group_rate": False,
        "cabin_count": 1,
    }
    base.update(overrides)
    return base


async def _run(monkeypatch, candidate_label, staged, method=None):
    """Drive test_discount_candidate far enough to hit the gates.

    `_apply_discount_candidate` is monkeypatched to explode: if a gate
    fails to fire, the test blows up loudly instead of silently passing
    on a later unrelated failure.
    """
    async def fake_stage(page, booking_id):
        return staged

    reached = {"apply": False}

    async def must_not_apply(page, candidate):
        # NOTE: test_discount_candidate never raises (it converts every
        # exception into a result), so a sentinel exception here would be
        # swallowed and invisible. Record a flag instead, and stop the
        # flow with a normal failure return.
        reached["apply"] = True
        return {"success": False, "reason": "halted by test after the gates"}

    monkeypatch.setattr(msc_commands, "_stage_booking_for_confirm", fake_stage)
    monkeypatch.setattr(msc_commands, "_apply_discount_candidate", must_not_apply)

    async def fake_lookup(page, booking_id, **kw):
        return {"found": True, "summary_text": "Booking Value\n$2,588.72", "breakdown_text": None}

    monkeypatch.setattr(msc_commands, "_lookup_one_booking", fake_lookup, raising=False)

    candidate = MscDiscountCandidate(
        label=candidate_label,
        method=method or MscDiscountApplicationMethod.DROPDOWN_OPTION,
    )

    class _Page:
        url = "https://www.mscbook.com/x?partNumber=VI1"

        async def wait_for_timeout(self, ms):
            pass

        async def inner_text(self, sel):
            return "Booking Value\n$2,588.72"

    state = {"page": _Page(), "pages": [_Page()], "context": None}
    result = await msc_commands.test_discount_candidate(state, "TESTBOOKING", candidate, page=_Page())
    return result, reached["apply"]


# ── Priority 2: eligibility gate ─────────────────────────────────


@pytest.mark.asyncio
async def test_senior_discount_refused_for_a_lone_senior(monkeypatch):
    """THE 2026-08-18 false positive, on the live path. Senior discount
    needs 2+ seniors; MSC's dropdown offers it regardless of party
    composition, so the dropdown being present is NOT eligibility."""
    result, reached_apply = await _run(
        monkeypatch, "SENIOR DISCOUNT", _staged(senior_count=1),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert "not an eligible discount" in result.reason.lower()
    assert "senior_count=1" in result.reason


@pytest.mark.asyncio
async def test_senior_discount_allowed_with_two_seniors(monkeypatch):
    """The gate must not over-block: 2+ seniors IS eligible, so this
    candidate must pass the gate and reach the apply step."""
    result, reached_apply = await _run(monkeypatch, "SENIOR DISCOUNT", _staged(senior_count=2))
    assert reached_apply is True, (
        f"eligible candidate was blocked before apply: {result.reason}"
    )


@pytest.mark.asyncio
async def test_military_discount_always_refused(monkeypatch):
    """CruiseIntel never applies military discounts from the agency side,
    regardless of what MSC's dropdown lists."""
    result, reached_apply = await _run(
        monkeypatch, "MIL-CIV-IL-DSCNT-10%", _staged(),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert "not an eligible discount" in result.reason.lower()


@pytest.mark.asyncio
async def test_group_rate_booking_refuses_dropdown_discounts(monkeypatch):
    """Group Rate bookings are capped at the flat Voyagers 5% — no
    dropdown tier is eligible, whatever the dropdown shows."""
    result, reached_apply = await _run(
        monkeypatch, "TODAY10", _staged(is_group_rate=True),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert "is_group_rate=True" in result.reason


@pytest.mark.asyncio
async def test_label_not_offered_at_all_is_refused(monkeypatch):
    """A typed label that simply isn't on this booking's dropdown."""
    result, reached_apply = await _run(
        monkeypatch, "TOTALLY MADE UP DISCOUNT", _staged(),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA


@pytest.mark.asyncio
async def test_uncaptured_dropdown_is_refused_with_a_distinct_reason(monkeypatch):
    """`discount_options is None` means the dropdown was never CAPTURED —
    a scrape problem, not a policy answer. Both refuse, but the operator
    needs to be able to tell them apart."""
    result, reached_apply = await _run(
        monkeypatch, "SENIOR DISCOUNT", _staged(discount_options=None),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert "never captured" in result.reason.lower()


# ── Priority 4: multi-cabin guard ────────────────────────────────


@pytest.mark.asyncio
async def test_multi_cabin_booking_is_refused(monkeypatch):
    """Every selector in this flow targets cabin 1, so a 2-cabin
    booking's 'savings' would be wrong by roughly the other cabin."""
    result, reached_apply = await _run(
        monkeypatch, "SENIOR DISCOUNT", _staged(cabin_count=2),
    )
    assert result.status == MscDiscountTestStatus.INSUFFICIENT_DATA
    assert "2 cabins" in result.reason
    assert "cabin 1 only" in result.reason


@pytest.mark.asyncio
async def test_single_cabin_booking_passes_the_cabin_guard(monkeypatch):
    result, reached_apply = await _run(monkeypatch, "SENIOR DISCOUNT", _staged(cabin_count=1))
    assert reached_apply is True, f"single-cabin booking was blocked: {result.reason}"


@pytest.mark.asyncio
async def test_unknown_cabin_count_does_not_block(monkeypatch):
    """cabin_count == 0 means UNKNOWN (pattern not found), not
    'multi-cabin'. Blocking on unknown would refuse ordinary bookings
    whose summary wording differs; the guard only fires on a POSITIVE
    multi-cabin finding."""
    result, reached_apply = await _run(monkeypatch, "SENIOR DISCOUNT", _staged(cabin_count=0))
    assert reached_apply is True, f"unknown cabin count wrongly blocked: {result.reason}"


# ── occupancy: the second failure mode ───────────────────────────


@pytest.mark.asyncio
async def test_empty_passenger_occupancy_is_refused(monkeypatch):
    """`skipped_empty_passengers` means passenger extraction FAILED so
    occupancy was left at MSC's adults-only default — the confirmed
    3000024 bug (3 kids dropped -> a fake $1,929.61 'opportunity'),
    which this path previously never checked for."""
    result, reached_apply = await _run(
        monkeypatch, "SENIOR DISCOUNT",
        _staged(occupancy_fix={"stalled": False, "skipped_empty_passengers": True}),
    )
    assert result.status == MscDiscountTestStatus.OCCUPANCY_MISMATCH
    assert "passenger extraction failed" in result.reason.lower()


# ── cabin counting helper ────────────────────────────────────────


def test_count_cabins_dedupes_repeated_headings():
    """The same cabin heading legitimately appears more than once (summary
    plus itemized breakdown); counting raw lines would overstate it and
    wrongly refuse a normal single-cabin booking."""
    assert msc_commands._count_cabins("Cabin 1 - BALCONY\nx\nCabin 1 - BALCONY") == 1
    assert msc_commands._count_cabins("Cabin 1 - A\nCabin 2 - B") == 2
    assert msc_commands._count_cabins("Cabin 1 - A\nCabin 2 - B\nCabin 3 - C\nCabin 1 - A") == 3


def test_count_cabins_returns_zero_when_unknown():
    assert msc_commands._count_cabins("no cabin lines") == 0
    assert msc_commands._count_cabins("") == 0
    assert msc_commands._count_cabins(None) == 0
