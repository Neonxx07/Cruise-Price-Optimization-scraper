"""Saying "re-add this" without saying what it is worth under-quotes. (P1.4)

THE DISCREPANCY, open since 2026-09-30 and solved 2026-10-01.

Neon's agency ledger for booking 3001021: we quoted $2,231 -> $2,183
(save $48); he actually achieved **$2,117 (save $114)** - $66 better, and
nobody knew why. It was logged as roadmap P1.4, "are we under-quoting?",
with one data point.

The next day he questioned bookings 3001023 and 3001022, which the scan
called optimizations while the portal's Rate Comparison appeared to show a
HIGHER price. The first screenshots turned out to be a different rate
selection - his own correction - but chasing it produced the second data
point: we quoted $2,547, the portal's own comparison showed **$2,481**.

**$66 again.** Both gaps are exactly the value of `Email Bonus NRD` - the
promo this very note was already telling him to re-add::

    optimized $48 — re-add: Email Bonus NRD

The calculator identified the promo correctly and then left its money out
of the figure, so every quote carrying a re-addable fare reads low.

MEASURED across the corpus: **401 bookings carry a re-addable promo with a
real dollar value, totalling $97,565** absent from the quoted savings. Most
common: Email Bonus NRD (234), BONUS SAV NRD (91), WEEKENDSAV NRD (42).

WHY `net_saving` IS NOT INFLATED BY IT. Re-adding is a manual step on the
Promotions screen and it can fail. Promising money that is not yet secured
is exactly the mistake the OBC rule exists to prevent, pointed the other
way - see test_cash_increase_never_optimization_2026_10_01. The verdict
stays conservative; the note states the ceiling.
"""

import json
import pathlib

import pytest

from core.calculator import calculate_espresso
from core.models import BookingStatus

RAW = pathlib.Path("data/raw_responses.jsonl")


def _capture(booking_id: str) -> dict:
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
            if str(rec.get("booking_id")) != booking_id:
                continue
            payload = rec.get("raw")
            if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                found = payload
    if found is None:
        pytest.skip(f"no capture for {booking_id}")
    return found


def _raw(old_total, new_total, promos=(), old_fares=(), new_fares=()):
    """Real portal shape: CRUISE_PROMO lines are per-passenger."""
    items = [{"paxId": "total", "type": "VACATION_TOTAL", "amount": old_total}]
    for name, amount in promos:
        items.append({"paxId": "1", "type": "CRUISE_PROMO",
                      "name": name, "amount": amount})
    return {"result": {
        "oldInvoice": {"invoiceItems": items},
        "newInvoice": {"invoiceItems": [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": new_total}]},
        "oldFares": [{"name": n} for n in old_fares],
        "newFares": [{"name": n} for n in new_fares],
    }}


# -- the two real bookings, against Neon's own figures ------------------


def test_3001021_quotes_the_114_he_actually_achieved():
    """His ledger row: $2,231 -> $2,117, saving $114."""
    result = calculate_espresso(_capture("3001021"), "3001021", None)

    assert result.net_saving == 48.0, "the conservative figure is unchanged"
    assert "Email Bonus NRD" in result.note
    assert "$66.00" in result.note
    assert "$114.00" in result.note, "the saving he really got is not stated"


def test_3001022_quotes_the_116_the_portal_showed():
    """His corrected Rate Comparison screenshot: new total $2,481, i.e. a
    $116 saving against $2,597."""
    result = calculate_espresso(_capture("3001022"), "3001022", None)

    assert result.net_saving == 50.0
    assert "$116.00" in result.note


def test_3001023_is_the_same_shape():
    result = calculate_espresso(_capture("3001023"), "3001023", None)
    assert "$117.00" in result.note


# -- the rule ------------------------------------------------------------


def test_the_note_states_both_the_value_and_the_resulting_saving():
    """"Re-add X" alone is not actionable - it does not say whether X is
    worth the click."""
    result = calculate_espresso(
        _raw(1000.0, 900.0, promos=[("Email Bonus NRD", -66.0)],
             old_fares=["Email Bonus NRD"], new_fares=[]),
        "X", None)

    assert "worth $66.00" in result.note
    assert "$166.00" in result.note


def test_the_verdict_is_not_inflated_by_a_re_addable_promo():
    """The whole safety point. net_saving is what you get WITHOUT the extra
    step; promising unsecured money is the OBC mistake reversed."""
    result = calculate_espresso(
        _raw(1000.0, 900.0, promos=[("Email Bonus NRD", -66.0)],
             old_fares=["Email Bonus NRD"], new_fares=[]),
        "X", None)

    assert result.net_saving == 100.0, "net_saving must stay conservative"


def test_a_re_addable_promo_cannot_flip_a_verdict():
    """A booking with no real saving must not become an OPTIMIZATION just
    because a re-addable promo would cover the gap."""
    result = calculate_espresso(
        _raw(1000.0, 1000.0, promos=[("Email Bonus NRD", -66.0)],
             old_fares=["Email Bonus NRD"], new_fares=[]),
        "X", None)

    assert result.status != BookingStatus.OPTIMIZATION


def test_a_re_addable_fare_with_no_priced_line_says_only_what_it_knows():
    """No CRUISE_PROMO line means no dollar value. Inventing one would be a
    guess, so the note names the fare and stops."""
    result = calculate_espresso(
        _raw(1000.0, 900.0, old_fares=["Email Bonus NRD"], new_fares=[]),
        "X", None)

    assert "re-add: Email Bonus NRD" in result.note
    assert "worth $" not in result.note


def test_several_re_addable_promos_are_summed():
    result = calculate_espresso(
        _raw(1000.0, 900.0,
             promos=[("Email Bonus NRD", -66.0), ("BONUS SAV NRD", -34.0)],
             old_fares=["Email Bonus NRD", "BONUS SAV NRD"], new_fares=[]),
        "X", None)

    assert "worth $100.00" in result.note
    assert "$200.00" in result.note


def test_a_booking_with_nothing_to_re_add_says_nothing():
    result = calculate_espresso(_raw(1000.0, 900.0), "X", None)
    assert "re-add" not in result.note


# -- the scale this was worth fixing for --------------------------------


def test_the_corpus_still_carries_unquoted_re_addable_value():
    """A standing measurement, not a threshold to pass: it records that
    this is a real, recurring sum rather than two anecdotes. If it ever
    drops to zero, either the portal stopped offering these promos or the
    detection broke - both worth knowing."""
    if not RAW.exists():
        pytest.skip("captured corpus not present")

    from core.calculator import _get_promo_value_by_name, norm_str

    seen: dict[str, dict] = {}
    with RAW.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"showRepriceModal"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = (rec.get("raw") or {}).get("result")
            if isinstance(data, dict) and (data.get("oldInvoice") or {}).get("invoiceItems"):
                seen[str(rec.get("booking_id"))] = rec["raw"]

    carrying = 0
    for booking_id, payload in seen.items():
        try:
            result = calculate_espresso(payload, booking_id, None)
        except Exception:  # noqa: BLE001
            continue
        if not result.re_addable_fares:
            continue
        promos = _get_promo_value_by_name(
            (payload["result"].get("oldInvoice") or {}).get("invoiceItems", []))
        if any(promos.get(norm_str(name)) for name in result.re_addable_fares):
            carrying += 1

    assert carrying > 100, (
        f"only {carrying} bookings carry a priced re-addable promo; it was "
        "401 on 2026-10-01 - check whether the detection broke")
