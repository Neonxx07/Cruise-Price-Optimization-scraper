"""Re-adding a PERK puts its price back. Only a discount comes back as money.

THE BUG, found 2026-10-13 on booking 3000067 (Celebrity Constellation, Y
guarantee, CAD 7,656, ALL INC 2PK NRD). The scan's note said::

    optimized $2337 — re-add: ALL INC 2PK NRD (worth $2,100.00 — saving
    becomes $4,437.00 if re-added)

Neon's recording and two sanitized HAR files of Promotions -> Compare gave
the portal's own Y prices per person (`pricing.FIRST_SECOND.price`) and
the allocate response's `totalInvoiceAmount`:

    Best Rate                BESTRATE  1,609.50   (3,219)
    ALL INC 2PK NRD          DJ065340  2,659.50   total 5,319.00
    ALL INC 2PK (refundable) DJ028141  2,972.50   total 5,945.00

Re-adding the perk costs 1,050 pp = $2,100 - exactly its CRUISE_PROMO line
(+2,100, a CHARGE) which lost_pkg_value already subtracts. So net_saving
2,337 (= 7,656 - 5,319) is the saving with the perk kept, and the note's
$4,437 promised the perk back for free. 189 latest ESPRESSO rows carried a
"saving becomes" note when this was found.

The discount case (Email Bonus NRD -66, booking 3001021) stays as it was:
see test_readd_value_is_quoted_2026_10_01.
"""

import json
import pathlib

import pytest

from core.calculator import calculate_espresso
from core.models import BookingStatus

RAW = pathlib.Path("data/raw_responses.jsonl")


def _capture(booking_id: str, day: str) -> dict:
    """The capture taken on `day` - never "the newest". 3000067 was re-scanned
    on 2026-10-14 after Neon moved it to $5,319, and a newest-capture test
    broke the same day (BUGS_AND_FIXES §9.7, repeated)."""
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
            if (str(rec.get("booking_id")) != booking_id
                    or not str(rec.get("captured_at", "")).startswith(day)):
                continue
            payload = rec.get("raw")
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                found = payload
    if found is None:
        pytest.skip(f"no capture for {booking_id}")
    return found


def _raw(old_total, new_total, promos=(), packages=(), old_fares=(), new_fares=()):
    """Real portal shape: CRUISE_PROMO lines per passenger, packages as
    paxId=total lines (as in 3000067's capture)."""
    items = [{"paxId": "total", "type": "VACATION_TOTAL", "amount": old_total}]
    for name, amount in promos:
        items.append({"paxId": "1", "type": "CRUISE_PROMO", "name": name, "amount": amount})
    for name, amount in packages:
        # 3000067's real line carries no "type", only a componentKey.
        items.append({"paxId": "total", "componentKey": "49~CRUISE",
                      "name": name, "amount": amount})
    return {"result": {
        "oldInvoice": {"invoiceItems": items},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": new_total}]},
        "oldFares": [{"name": n} for n in old_fares],
        "newFares": [{"name": n} for n in new_fares],
    }}


# -- the real booking ------------------------------------------------------


def test_3000067_quotes_the_saving_with_the_perk_kept():
    result = calculate_espresso(_capture("3000067", "2026-10-13"), "3000067", None)

    assert result.status == BookingStatus.OPTIMIZATION
    assert result.net_saving == 2337.0, "= 7,656 - 5,319, the portal's own total"
    assert "$4,437.00" not in result.note, "the perk does not come back for free"
    assert "saving becomes" not in result.note
    assert "puts its $2,100.00 price back" in result.note
    assert "$2,337.00 is the saving with it kept" in result.note


# -- the rule --------------------------------------------------------------


def test_a_positive_promo_line_is_a_price_not_a_discount():
    """Without a package line the perk's price is NOT inside net, so keeping
    it has to be paid out of the drop: 400 - 300 = 100."""
    result = calculate_espresso(
        _raw(1000.0, 600.0, promos=[("ALL INC 2PK NRD", 300.0)],
             old_fares=["ALL INC 2PK NRD"]),
        "X", None)

    assert "saving becomes" not in result.note
    assert "puts its $300.00 price back — $100.00 is the saving with it kept" in result.note


def test_a_trap_says_what_keeping_the_perk_costs():
    """3001036's shape: drop 1,557, perk 2,002 -> keeping it costs 445 more.
    The old note said "saving becomes $1,557"."""
    result = calculate_espresso(
        _raw(4207.0, 2650.0, promos=[("ALL INC 2PK NRD", 2002.0)],
             packages=[("ALL INC 2PK NRD", 2002.0)],
             old_fares=["ALL INC 2PK NRD"]),
        "X", None)

    assert result.status != BookingStatus.OPTIMIZATION
    assert "$1,557.00" not in result.note
    assert "keeping it costs $445.00 more than now" in result.note


def test_a_discount_still_comes_back_as_money():
    """Unchanged from 2026-10-01 (3001021)."""
    result = calculate_espresso(
        _raw(1000.0, 900.0, promos=[("Email Bonus NRD", -66.0)],
             old_fares=["Email Bonus NRD"]),
        "X", None)

    assert "worth $66.00 — saving becomes $166.00 if re-added" in result.note


def test_perk_and_discount_together_are_kept_apart():
    result = calculate_espresso(
        _raw(1000.0, 500.0,
             promos=[("ALL INC 2PK NRD", 300.0), ("BONUS SAV NRD", -40.0)],
             packages=[("ALL INC 2PK NRD", 300.0)],
             old_fares=["ALL INC 2PK NRD", "BONUS SAV NRD"]),
        "X", None)

    assert result.net_saving == 200.0, "500 drop - 300 perk"
    assert "worth $40.00 — saving becomes $240.00 if re-added" in result.note
    assert "puts its $300.00 price back — $240.00 is the saving with it kept" in result.note


# -- the TRAP that charged a re-addable perk twice -------------------------


def test_3000067_on_2026_10_12_was_a_saving_with_the_perk_kept():
    """The scan said "trap - losing $2100 perk for only $1769 net". Net had
    the perk's $2,100 taken off already and the portal re-adds it for that
    price, so $1,769 was real with ALL INC 2PK NRD kept."""
    result = calculate_espresso(
        _raw(7656.0, 3787.0, promos=[("ALL INC 2PK NRD", 2100.0)],
             packages=[("ALL INC 2PK NRD", 2100.0)],
             old_fares=["ALL INC 2PK NRD"]),
        "X", None)

    assert result.net_saving == 1769.0
    assert result.status == BookingStatus.OPTIMIZATION
    assert "$1,769.00 is the saving with it kept" in result.note


def test_a_perk_that_cannot_be_re_added_is_still_a_trap():
    """The original case the rule was written for: $50 net for giving up a
    $594 drink package that does not come back."""
    result = calculate_espresso(
        _raw(1000.0, 356.0, packages=[("DRINK PACKAGE", 594.0)]),
        "X", None)

    assert result.net_saving == 50.0
    assert result.status == BookingStatus.TRAP


def test_3000067_once_moved_to_the_nrd_perk_has_nothing_left():
    """2026-10-14: Neon had moved it to ALL INC 2PK NRD at $5,319. Best Rate
    3,219 + the perk's 2,100 = 5,319 exactly - no saving with the perk kept,
    and nothing may call one."""
    result = calculate_espresso(_capture("3000067", "2026-10-14"), "3000067", None)
    assert result.old_total == 5319.0
    assert result.net_saving == 0.0
    assert result.status != BookingStatus.OPTIMIZATION
    assert "$0.00 is the saving with it kept" in result.note
