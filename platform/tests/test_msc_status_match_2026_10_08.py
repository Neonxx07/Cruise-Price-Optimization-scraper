"""MSC status-match eligibility: no Voyagers membership, no discount.

Neon 2026-10-08: "detect that this customer doesnt have a msc voyagers
membership and no discount applied".

Evidence, data/msc_control/booking_data.jsonl, 133 bookings captured by
2026-10-08, latest record each:
    member aboard 93 · not found 21 · no passenger list 9 · cancelled 4
    ELIGIBLE 3 · no discount printed but unprovable 2 · discount applied 1

The unprovable case is the one that matters. 3000031 (9 nights) has no
Voyagers member and an EMPTY discount list - and on 2026-08-24 exactly that
kind of empty list produced a false "add senior discount" because the SRN
check never ran for its length. It must be CHECK_MANUALLY, never ELIGIBLE.
"""

import json
from pathlib import Path

import pytest

from core.msc_status_match import (
    CHECK_MANUALLY,
    ELIGIBLE,
    NOT_ELIGIBLE,
    assess_status_match,
)

CORPUS = Path("data/msc_control/booking_data.jsonl")

# A 7-night booking whose SRN equals the standard $182 NCF: no discount, proven.
_PROVEN_CLEAN = {
    "booking_id": "T1",
    "summary_text": "Duration: Jul 17, 2027 , 8 Days , 7 Nights",
    "breakdown_text": "SRN\tNon commissionable fares\t$0.00\t$0.00\t-\t$182.00\t$182.00",
    "passengers": [{"name": "AVERY DEMOGUEST", "dob": "01/01/1980", "voyagers_number": None}],
    "current_discounts": [],
}


def _with(**changes):
    return {**_PROVEN_CLEAN, **changes}


def test_no_member_and_proven_no_discount_is_eligible():
    assert assess_status_match(_PROVEN_CLEAN).verdict == ELIGIBLE


def test_a_voyagers_member_aboard_is_not_eligible_and_named():
    r = assess_status_match(_with(passengers=[
        {"name": "AVERY DEMOGUEST", "dob": "01/01/1980", "voyagers_number": "123", "voyagers_tier": "SILVER"}]))
    assert r.verdict == NOT_ELIGIBLE and "AVERY DEMOGUEST (SILVER)" in r.reason


def test_an_applied_discount_is_not_eligible_and_named():
    r = assess_status_match(_with(current_discounts=[
        {"kind": "named", "label": "SPECIAL OFFER 15%", "rate_pct": 15.0}]))
    assert r.verdict == NOT_ELIGIBLE and "SPECIAL OFFER" in r.reason


def test_an_uncaptured_breakdown_is_unknown_not_clean():
    assert assess_status_match(_with(current_discounts=None)).verdict == CHECK_MANUALLY


def test_an_empty_list_on_an_uncovered_length_is_unprovable():
    r = assess_status_match(_with(summary_text="Duration: 10 Days , 9 Nights"))
    assert r.verdict == CHECK_MANUALLY


def test_no_passenger_list_is_unknown():
    assert assess_status_match(_with(passengers=[])).verdict == CHECK_MANUALLY


def test_missing_record_is_unknown():
    assert assess_status_match(None).verdict == CHECK_MANUALLY


def test_expired_session_is_unknown_even_if_old_fields_look_clean():
    result = assess_status_match(_with(session_expired=True))
    assert result.verdict == CHECK_MANUALLY
    assert "session expired" in result.reason


def test_malformed_record_or_passenger_data_is_never_eligible():
    assert assess_status_match([]).verdict == CHECK_MANUALLY
    assert assess_status_match(_with(passengers=[None])).verdict == CHECK_MANUALLY


def test_missing_membership_capture_is_unknown():
    result = assess_status_match(_with(
        has_voyagers=None,
        passengers=[{"name": "AVERY DEMOGUEST", "dob": "01/01/1980"}],
    ))
    assert result.verdict == CHECK_MANUALLY
    assert "membership was not captured" in result.reason


def test_explicit_voyagers_flag_blocks_status_match_without_a_number():
    result = assess_status_match(_with(
        has_voyagers=True,
        passengers=[{"name": "AVERY DEMOGUEST", "dob": "01/01/1980",
                     "voyagers_number": None}],
    ))
    assert result.verdict == NOT_ELIGIBLE
    assert "Voyagers member" in result.reason


def test_malformed_discount_capture_is_unknown_not_clean():
    assert assess_status_match(_with(current_discounts="none")).verdict == CHECK_MANUALLY
    assert assess_status_match(_with(current_discounts=[None])).verdict == CHECK_MANUALLY


@pytest.mark.parametrize("change", [
    {"summary_text": "No bookings found"},
    {"explicitly_cancelled": True},
    {"cancelled_or_postponed_placeholder": True},
])
def test_not_found_or_cancelled_is_not_eligible(change):
    assert assess_status_match(_with(**change)).verdict == NOT_ELIGIBLE


# ── the whole captured corpus ────────────────────────────────────────────

def _latest_records():
    if not CORPUS.exists():
        pytest.skip("no MSC capture corpus on this machine")
    latest = {}
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            latest[row["booking_id"]] = row
    return latest


def test_no_captured_booking_is_eligible_with_a_member_or_a_discount():
    for booking_id, record in _latest_records().items():
        if assess_status_match(record).verdict != ELIGIBLE:
            continue
        assert not any(p.get("voyagers_number") for p in record["passengers"]), booking_id
        assert record["current_discounts"] == [], booking_id


def test_the_2026_08_24_false_positive_lengths_are_never_eligible():
    records = _latest_records()
    for booking_id in ("3000031", "3000018", "3000017"):
        if booking_id in records:
            assert assess_status_match(records[booking_id]).verdict != ELIGIBLE, booking_id


# ── a stale record is not used ───────────────────────────────────────────

def test_a_record_from_before_this_run_is_ignored(monkeypatch):
    import services.msc_live_service as live

    monkeypatch.setattr(live, "latest_msc_booking",
                        lambda _id: {**_PROVEN_CLEAN, "captured_at": "2026-01-01T00:00:00"})
    stale = live._status_match_for("T1", read_since="2026-10-08T12:00:00")
    assert stale["verdict"] == CHECK_MANUALLY

    monkeypatch.setattr(live, "latest_msc_booking",
                        lambda _id: {**_PROVEN_CLEAN, "captured_at": "2026-10-08T12:00:05"})
    fresh = live._status_match_for("T1", read_since="2026-10-08T12:00:00")
    assert fresh["verdict"] == ELIGIBLE
