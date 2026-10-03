"""ALL INC 2PK NRD: charged once, and the perk tier is reported.

THE INCIDENT. Neon checked booking 3001020 by hand and said it was not
more expensive - "it is the same price because of allinc2pk nrd". The tool
had reported::

    old 3804.66 -> new 3258.66      raw drop        546.00
    lost_pkg_names: ["ALL INC 2PK NRD", "ALL INC 2PK NRD ($1260.00)"]
    net_saving: -1974.00            status: TRAP

THE BUG: a DOUBLE COUNT. "ALL INC 2PK NRD" was charged as a lost package
AND as a lost fare - 546 - 1260 - 1260 = -1974 exactly. The duplicate was
visible in lost_pkg_names the whole time, the same name from two sources,
one bare and one priced. The honest figure is 546 - 1260 = **-714**, still
a TRAP, but the magnitude Neon judges by was wrong by $1,260.

THE DEAD END, kept because it nearly shipped. A second "fix" argued that
+1260 is a SURCHARGE (the price of the package), so dropping it saves
money rather than costing it, and excluded positive CRUISE_PROMO lines
from the perk detector. That turned 3001020 into a $546 OPTIMIZATION -
advice to trade drinks and Wi-Fi for $546. It is wrong because:

  - price_drop ALREADY carries the saving from not paying the charge;
    removing the perk's value too counts the same event twice, the other
    way round.
  - The charge IS what the perk is worth to this client. ESPRESSO's
    Promotions screen prices ALL INC 2PK NRD ABOVE Best Rate (A2 Aqua
    Class $6,210.00 vs $5,328.00). You pay for all-inclusive.
  - "ALL INC 2PK NRD" ("All Incl Bev and Wifi NRD") and "NOPERK NRD"
    ("No Perk Rate NRD") are mutually exclusive tiers - neither appears in
    the other's "Combined With" column - so the move gives the perk up.

And the newInvoice cannot be used to argue the perk survives: across all
148 perk-tier changes in the corpus the COMPONENT lines are IDENTICAL in
old and new, including 14 NOPERK -> STANDARD moves where the perk plainly
cannot survive. The modal echoes the booking's existing components into
the preview.

Neon also confirmed the offer is RE-ADDABLE: sidebar -> Promotions
(_eventId=linkToPromotionList), then the offer row (fareCodes=DI980793)
with a Compare button and a checkbox. Re-adding it costs the $1,260 again,
which is why this is "about the same price", not a win - exactly what he
said.
"""

import json
import pathlib

import pytest

from core.calculator import (
    _get_packages,
    _is_re_addable,
    calculate_espresso,
    perk_tier_change,
)
from core.models import BookingStatus

RAW = pathlib.Path("data/raw_responses.jsonl")


def _real_capture(booking_id: str, on: str | None = None) -> dict:
    """A real reprice payload for one booking - newest, or from one day.

    PIN THE DAY WHEN PINNING FIGURES. This test broke on 2026-10-01 the
    moment a new scan landed: the booking was re-quoted at 3,558.66 instead
    of 3,258.66 (a smaller fare drop, offset by $300 more OBC - net still
    -714.00, the calculator was right). An exact-figures assertion against
    "the newest capture" is really an assertion that nobody scans again.

    So: tests that pin EXACT NUMBERS pass `on=` and read that day's
    capture, which never changes. Tests that assert an INVARIANT - it is a
    trap, the perk is charged once - use the newest, because those must
    hold against whatever the portal says today.
    """
    if not RAW.exists():
        pytest.skip("captured corpus not present")
    found = None
    with RAW.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if booking_id not in line or '"showRepriceModal"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("raw")
            if not (isinstance(payload, dict)
                    and isinstance(payload.get("result"), dict)):
                continue
            if on and not str(rec.get("captured_at", "")).startswith(on):
                continue
            found = payload
    if found is None:
        pytest.skip(f"no capture for {booking_id}" + (f" on {on}" if on else ""))
    return found


# -- the real booking, pinned exactly -------------------------------------


def test_3001020_is_a_714_dollar_trap_not_a_1974_dollar_one():
    """The figures Neon disputed, against the capture they were read from.

    Pinned to 2026-09-30 deliberately - see _real_capture. The 2026-10-01
    scan re-quoted this booking at 3,558.66 with $300 more OBC, and net
    stayed -714.00.
    """
    result = calculate_espresso(
        _real_capture("3001020", on="2026-09-30"), "3001020", None)

    assert result.old_total == 3804.66
    assert result.new_total == 3258.66
    assert result.net_saving == -714.0, "was -1974.00 - charged twice"
    assert result.status == BookingStatus.TRAP


def test_3001020_charges_the_all_inclusive_package_exactly_once():
    result = calculate_espresso(_real_capture("3001020"), "3001020", None)
    assert result.lost_pkg_value == 1260.0, "1260 once, not 2520"
    assert result.lost_pkg_names == ["ALL INC 2PK NRD"]


def test_3001020_is_never_reported_as_an_optimization():
    """The dead end above would have made this a $546 OPTIMIZATION and told
    Neon to give up drinks and Wi-Fi to get it."""
    result = calculate_espresso(_real_capture("3001020"), "3001020", None)
    assert result.status != BookingStatus.OPTIMIZATION
    assert result.net_saving < 0


def test_3001020_names_the_perk_tier_it_would_give_up():
    result = calculate_espresso(_real_capture("3001020"), "3001020", None)
    assert "ALL INC 2PK" in result.note and "NOPERK" in result.note
    assert "Promotions" in result.note


def test_3001020_tells_the_agent_the_offer_can_go_back_on():
    result = calculate_espresso(_real_capture("3001020"), "3001020", None)
    assert "re-add" in result.note
    assert "ALL INC 2PK NRD" in result.note


# -- never charge the same loss twice -------------------------------------


def test_the_same_name_is_never_charged_twice():
    """One loss, one charge. A perk present as both a package row and a
    lost fare must not be subtracted twice - the whole bug."""
    raw = {"result": {
        "oldInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 2000.0},
            {"paxId": "total", "name": "DRINKS PKG", "amount": 600.0},
            {"paxId": "1", "type": "CRUISE_PROMO", "name": "DRINKS PKG",
             "amount": 600.0},
        ]},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 1000.0},
        ]},
        "oldFares": [{"name": "DRINKS PKG"}],
        "newFares": [],
    }}
    result = calculate_espresso(raw, "DUPE", None)
    assert result.lost_pkg_value == 600.0, "600 counted once, not 1200"
    assert result.net_saving == 400.0


def test_a_lost_fare_with_no_package_row_is_still_charged():
    """The de-duplication must not silence the lost-fare path itself."""
    raw = {"result": {
        "oldInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 2000.0},
            {"paxId": "1", "type": "CRUISE_PROMO", "name": "BOGO75 NRD",
             "amount": -1454.0},
        ]},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": 1800.0},
        ]},
        "oldFares": [{"name": "BOGO75 NRD"}],
        "newFares": [],
    }}
    result = calculate_espresso(raw, "LOSTDISC", None)
    assert result.net_saving == 200.0 - 1454.0
    assert result.status == BookingStatus.TRAP


def test_a_paid_for_perk_is_still_subtracted():
    """Guards the dead end. A positive CRUISE_PROMO is the PRICE of a perk
    the client loses, so it must keep counting against the saving."""
    packages = _get_packages([
        {"paxId": "total", "type": "VACATION_TOTAL", "amount": 1000.0},
        {"paxId": "total", "name": "ALL INC 2PK NRD", "amount": 1260.0},
        {"paxId": "1", "type": "CRUISE_PROMO", "name": "ALL INC 2PK NRD",
         "amount": 1260.0},
    ])
    assert [p["name"] for p in packages] == ["ALL INC 2PK NRD"]


# -- perk tiers -----------------------------------------------------------


def test_the_four_perk_tiers_are_mutually_exclusive():
    assert perk_tier_change(["ALL INC 2PK NRD"], ["NOPERK NRD"]) == (
        "ALL INC 2PK", "NOPERK")
    assert perk_tier_change(["NOPERK NRD"], ["STANDARD"]) == (
        "NOPERK", "STANDARD")


def test_savings_offers_are_not_perk_tiers():
    """BONUS SAV, BOGO75, MilitarySav and SAVEUPTO100 combine with every
    tier, so swapping them is not a perk change."""
    assert perk_tier_change(
        ["ALL INC 2PK NRD", "BONUS SAV"],
        ["ALL INC 2PK NRD", "BOGO75 NRD"],
    ) is None


def test_no_tier_on_either_side_is_not_a_change():
    assert perk_tier_change(["BOGO75 NRD"], ["BONUS SAV NRD"]) is None
    assert perk_tier_change([], []) is None


def test_all_inclusive_is_re_addable():
    """Confirmed by Neon against the live portal: sidebar -> Promotions
    lists the offer with a Compare button and a checkbox."""
    assert _is_re_addable("ALL INC 2PK NRD")


# -- the corpus-wide claims ----------------------------------------------


def _captures():
    if not RAW.exists():
        pytest.skip("captured corpus not present")
    with RAW.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"showRepriceModal"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = (rec.get("raw") or {}).get("result")
            if isinstance(data, dict) and (data.get("oldInvoice") or {}).get(
                    "invoiceItems"):
                yield rec.get("booking_id"), data


def test_no_booking_is_ever_charged_for_the_same_name_twice():
    """The measurement that found this, kept as a test."""
    offenders = []
    for booking_id, data in _captures():
        result = calculate_espresso({"result": data}, str(booking_id), None)
        names = [n.split(" ($")[0] for n in (result.lost_pkg_names or [])]
        if len(names) != len(set(names)):
            offenders.append(f"{booking_id}:{names}")
    assert not offenders, f"{len(offenders)} double charges: {offenders[:5]}"


def test_new_invoice_components_are_not_evidence_a_perk_survives():
    """Why the tier, not the invoice, decides. If this ever fails, the
    modal has stopped echoing components and the reasoning above needs
    revisiting rather than the test being adjusted."""
    echoed = identical = 0
    for _booking_id, data in _captures():
        move = perk_tier_change(
            [f.get("name") for f in data.get("oldFares") or []],
            [f.get("name") for f in data.get("newFares") or []],
        )
        if not move:
            continue
        echoed += 1

        def components(capture: dict, invoice: str) -> dict:
            # `capture` passed in rather than closed over: a closure on the
            # loop variable is the classic B023 trap.
            return {i.get("name"): i.get("amount")
                    for i in (capture.get(invoice) or {}).get("invoiceItems", [])
                    if i.get("type") == "COMPONENT" and i.get("name")}

        if components(data, "oldInvoice") == components(data, "newInvoice"):
            identical += 1

    assert echoed, "no perk-tier changes in the corpus to check"
    assert identical == echoed, (
        "the modal no longer echoes components - the perk-tier reasoning "
        "in this module's docstring must be re-derived")


def test_every_recorded_tier_change_is_a_downgrade():
    """134 ALL INC 2PK -> NOPERK and 14 NOPERK -> STANDARD, no upgrades.
    Repricing never hands the client a better perk."""
    rank = {"ALL INC 2PK": 3, "RETREAT": 2, "NOPERK": 1, "STANDARD": 0}
    upgrades = []
    for booking_id, data in _captures():
        move = perk_tier_change(
            [f.get("name") for f in data.get("oldFares") or []],
            [f.get("name") for f in data.get("newFares") or []],
        )
        if move and rank[move[1]] > rank[move[0]]:
            upgrades.append(f"{booking_id}:{move}")
    assert not upgrades, f"a reprice gained a perk tier: {upgrades[:5]}"
