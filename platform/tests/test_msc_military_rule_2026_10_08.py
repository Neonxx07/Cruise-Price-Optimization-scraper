"""RULE MSC-D9: no Voyagers discount on top of a military discount.

Neon 2026-10-08, on booking 3001062 (MIL-CIV-IL-DSCNT-05%): "yes it is
military ... you cannot make a status match because you cannot apply a
vouyagers discount on top of military".

Evidence in data/msc_control: 2 of 133 captured bookings carry a military
discount.
  * 3001062  MIL-CIV-IL-DSCNT-05%  no Voyagers members  (SRN 172.90 = 182 x 0.95)
  * 3001058  MIL-CIV-IL-DSCNT-10%  Voyagers members aboard - and
    live_check_results.jsonl reported it
      VOYAGERS_SELECTION: OPPORTUNITY  MSVG10W (10%) ... confirmed to stack
      with the base 5% Club discount
    which this rule makes impossible. The check counted only SPECIAL OFFER /
    VOYAGERS EXCLUSIV as "already applied".
"""

import json
from pathlib import Path

import pytest

from core.calculator_msc import (
    _check_discount_add,
    _check_discount_tier_upgrade,
    _check_voyagers_selection,
    evaluate_msc_booking,
    is_military_label,
    military_discounts,
)
from core.models import MscCheckStatus, MscOpportunityType
from core.msc_status_match import NOT_ELIGIBLE, assess_status_match

MIL_10 = [{"kind": "named", "label": "MIL-CIV-IL-DSCNT-10%", "rate_pct": 10.0}]   # 3001058
MIL_05 = [{"kind": "named", "label": "MIL-CIV-IL-DSCNT-05%", "rate_pct": 5.0}]    # 3001062
SELECTION_CATALOG = [{"disc_cd": "MSVG10W", "rate_pct": 10.0,
                      "program_name": "Voyagers Selection WELCOME"}]


@pytest.mark.parametrize("label, expected", [
    ("MIL-CIV-IL-DSCNT-05%", True),
    ("mil-civ-il-dscnt-10%", True),
    ("MILITARY DISCOUNT", True),
    ("SPECIAL OFFER 10%", False),
    ("MSCCLUB5", False),
    (None, False),
])
def test_military_labels_are_recognised(label, expected):
    assert is_military_label(label) is expected


def test_3001058_no_longer_gets_voyagers_selection_on_top_of_military():
    check = _check_voyagers_selection(MIL_10, SELECTION_CATALOG,
                                      has_voyagers=True, senior_count=0)
    assert check.status == MscCheckStatus.NO_OPPORTUNITY
    assert "MSC-D9" in check.note and "MIL-CIV-IL-DSCNT-10%" in check.note


def test_the_group_rate_path_does_not_offer_the_club_5_on_top_of_military():
    check = _check_discount_add(MIL_05, ["TODAY10"], is_group_rate=True,
                                club_discount_offered=True, has_voyagers=False)
    assert check.status == MscCheckStatus.NO_OPPORTUNITY
    assert "MSC-D9" in check.note


def test_discount_add_names_the_military_reason():
    check = _check_discount_add(MIL_05, ["TODAY10"], club_discount_offered=True)
    assert check.status == MscCheckStatus.NO_OPPORTUNITY
    assert "military" in check.note


def test_every_price_scan_result_says_military_first():
    result = evaluate_msc_booking("3001062", "BP", current_discounts=MIL_05,
                                  today_discount_options=["TODAY10"],
                                  today_discount_catalog=SELECTION_CATALOG,
                                  club_discount_offered=True)
    assert result.note.startswith("MILITARY DISCOUNT APPLIED")
    voyagers = {c.type: c for c in result.checks}
    for kind in (MscOpportunityType.DISCOUNT_ADD, MscOpportunityType.VOYAGERS_SELECTION):
        assert voyagers[kind].status != MscCheckStatus.OPPORTUNITY


# -- MSC-D10: swapping military OUT, Neon 2026-10-08: "only if the cx has
# a vouagers memebership if not the do not allow that at all" -------------

def test_3001062_no_member_may_not_swap_its_military_discount():
    check = _check_discount_tier_upgrade(MIL_05, ["TODAY10 10%"], has_voyagers=False)
    assert check.status == MscCheckStatus.NO_OPPORTUNITY
    assert "MSC-D10" in check.note and "MIL-CIV-IL-DSCNT-05%" in check.note


def test_3001058_members_aboard_may_swap_and_are_told_it_replaces_military():
    check = _check_discount_tier_upgrade(MIL_10, ["TODAY15 15%"], has_voyagers=True)
    assert check.status == MscCheckStatus.OPPORTUNITY
    assert "REPLACES the military discount" in check.note and "MSC-D10" in check.note
    assert check.estimated_value == 5.0


def test_the_price_scan_passes_membership_through_to_the_swap_rule():
    blocked = evaluate_msc_booking("3001062", "BP", current_discounts=MIL_05,
                                   today_discount_options=["TODAY10 10%"], has_voyagers=False)
    # Priced since 2026-10-15 (the reprice rule, MSC-D1/D2, is now applied
    # to every discount lead): 1,100 today less 15% = 935 < 1,000 held.
    allowed = evaluate_msc_booking("3001058", "BP", current_discounts=MIL_10,
                                   today_discount_options=["TODAY15 15%"], has_voyagers=True,
                                   current_total_price=1000.0, today_base_price=1100.0,
                                   today_price_tab_confirmed=True)
    tier = MscOpportunityType.DISCOUNT_TIER_UPGRADE
    assert {c.type: c for c in blocked.checks}[tier].status == MscCheckStatus.NO_OPPORTUNITY
    assert {c.type: c for c in allowed.checks}[tier].status == MscCheckStatus.OPPORTUNITY


def test_a_non_military_upgrade_needs_no_membership():
    check = _check_discount_tier_upgrade(
        [{"kind": "named", "label": "SPECIAL OFFER 5%", "rate_pct": 5.0}],
        ["TODAY10 10%"], has_voyagers=False)
    assert check.status == MscCheckStatus.OPPORTUNITY
    assert "military" not in check.note


def test_a_booking_without_military_is_unaffected():
    result = evaluate_msc_booking("X", "BP", current_discounts=[],
                                  today_discount_options=[], club_discount_offered=True)
    assert not result.note.startswith("MILITARY")
    assert military_discounts([]) == [] and military_discounts(None) == []


def test_a_military_booking_cannot_be_status_matched():
    record = {
        "booking_id": "3001062",
        "summary_text": "Duration: 8 Days , 7 Nights",
        "breakdown_text": "SRN\tNon commissionable fares\t$0.00\t$0.00\t-\t$172.90\t$172.90",
        "passengers": [{"name": "JORDAN DEMOGUEST", "dob": "01/17/1996", "voyagers_number": None},
                       {"name": "CASEY DEMOGUEST", "dob": "02/12/1998", "voyagers_number": None}],
        "current_discounts": MIL_05,
    }
    r = assess_status_match(record)
    assert r.verdict == NOT_ELIGIBLE
    assert r.reason.startswith("no status match: military discount applied")


def test_every_captured_military_booking_is_refused_a_status_match():
    corpus = Path("data/msc_control/booking_data.jsonl")
    if not corpus.exists():
        pytest.skip("no MSC capture corpus on this machine")
    latest = {}
    for line in corpus.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            latest[row["booking_id"]] = row
    military = [r for r in latest.values() if military_discounts(r.get("current_discounts"))]
    assert military, "the corpus holds military bookings (3001058, 3001062)"
    for record in military:
        if record.get("explicitly_cancelled") or not record.get("passengers"):
            continue
        has_member = any(p.get("voyagers_number") for p in record["passengers"])
        verdict = assess_status_match(record)
        assert verdict.verdict == NOT_ELIGIBLE, record["booking_id"]
        if not has_member:
            assert "military" in verdict.reason, record["booking_id"]
