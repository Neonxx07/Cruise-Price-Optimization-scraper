"""Accuracy invariants — Neon: "MAKE EVERYTHING AS ACCURATE AS HUMAN EYES".

Almost every serious MSC defect this project has hit is ONE mistake wearing
different clothes: comparing two numbers that do not cover the same thing.

    club discount   today's price with NO discount  vs  a DISCOUNTED total
    occupancy       a quote for 1 guest             vs  a total for 2 guests
    multi-cabin     a quote for cabin 1             vs  a total for 2 cabins
    added services  a cruise-only quote            vs  a total with excursions

The first three produced fabricated opportunities of $267.01, $1,929.61 and
a whole run of false "no opportunity" verdicts. The fourth was found by the
reconciliation check below and overstated booking 3000013's headline
$1,292.51 by $96.00.

Two families of invariant here:
  * IDEA 2 - the invoice must add up. Cheap, and it found the added-services
    bug on its first run (96/100 reconciled; all 4 gaps were real).
  * IDEA 6 - every guard must actually be CALLED. msc_occupancy_is_trustworthy
    sat inert for a full day; the overpayment flag reached only one check.
"""
import glob
import json
import os
import re

import pytest

from core.calculator_msc import _check_price_match
from msc_commands import msc_invoice_components

# Resolved relative to this file (tests/ -> platform/), not an absolute path
# on one machine: the captures live in the git-ignored platform/data/ dir, so
# a hardcoded path silently globs to nothing anywhere else and every test
# below then skips without ever saying why.
_PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA = glob.glob(os.path.join(_PLATFORM_DIR, "data", "msc_control", "*.jsonl"))


def _corpus():
    out = {}
    for path in _DATA:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                bid = str(d.get("booking_id") or "")
                if d.get("breakdown_text") and re.fullmatch(r"\d{6,9}", bid):
                    out[bid] = d
    return out


# -- IDEA 2: the invoice must add up -------------------------------


def test_the_line_codes_and_column_order_are_understood():
    """Column order is CODE, description, commission$, discount$, comm%,
    NET, GROSS - verified because gross minus net equals the commission,
    and commission over gross equals the printed rate."""
    got = msc_invoice_components(
        "CAB\tDELUXE BALCONY\t$57.49\t$0.00\t17%\t$280.71\t$338.20\n"
        "Total Adult 1\t$57.49\t$0.00\t-\t$280.71\t$338.20\n")
    assert got["by_code"] == {"CAB": 338.20}
    assert got["reconciles"] is True
    assert got["gap"] == 0.0


def test_excursions_and_transfers_are_flagged_as_non_cruise():
    """ACT (shore excursions), TRF (transfers) and AIR (flights) are real
    codes found on captured invoices. A category quote can never include
    them."""
    got = msc_invoice_components(
        "CAB\tBALCONY\t$0.00\t$0.00\t17%\t$100.00\t$2,052.28\n"
        "ACT\tPISA: THE TOWN OF THE LEANING TOWER\t$5.60\t$0.00\t5%\t$106.40\t$112.00\n"
        "Total Adult 1\t$0.00\t$0.00\t-\t$0.00\t$2,164.28\n")
    assert got["non_cruise_total"] == 112.00
    assert got["non_cruise_codes"] == ["ACT"]
    assert got["cruise_total"] == 2052.28
    assert got["reconciles"] is True


def test_a_truncated_invoice_fails_to_reconcile():
    """The failure mode this exists for: a truncated capture silently
    losing a line used to produce an empty current_discounts on a booking
    that really had a discount, and a false DISCOUNT_ADD with it."""
    got = msc_invoice_components(
        "CAB\tBALCONY\t$0.00\t$0.00\t17%\t$100.00\t$1,000.00\n"
        "Total Adult 1\t$0.00\t$0.00\t-\t$0.00\t$1,250.00\n")
    assert got["reconciles"] is False
    assert got["gap"] == 250.00


def test_an_unknown_line_code_is_reported_not_swallowed():
    got = msc_invoice_components(
        "CAB\tBALCONY\t$0.00\t$0.00\t17%\t$1.00\t$100.00\n"
        "ZZZ\tsomething new\t$0.00\t$0.00\t-\t$1.00\t$50.00\n"
        "Total Adult 1\t$0.00\t$0.00\t-\t$0.00\t$150.00\n")
    assert got["unknown_codes"] == ["ZZZ"]


def test_missing_input_returns_empty_rather_than_raising():
    for bad in (None, ""):
        got = msc_invoice_components(bad)
        assert got["reconciles"] is False
        assert got["by_code"] == {}


@pytest.mark.skipif(not _DATA, reason="captured MSC data not present")
def test_every_captured_invoice_reconciles_to_the_cent():
    """THE REAL CHECK, run against all 100 captured invoices. It failed 4
    times on its first run and every failure was a genuine finding - an
    unparsed non-cruise service line. If this ever fails again it means
    either a new line code exists or the parser has regressed; both are
    things a human reading the invoice would notice immediately.
    """
    corpus = _corpus()
    if not corpus:
        pytest.skip("no captured invoices")
    broken = []
    for bid, rec in corpus.items():
        got = msc_invoice_components(rec["breakdown_text"])
        if got["stated_total"] is None:
            continue
        if not got["reconciles"]:
            broken.append((bid, got["gap"], got["unknown_codes"]))
    assert not broken, (
        "invoices that do not add up (gap, unknown codes):\n  "
        + "\n  ".join(f"{b}: {g:+.2f} {u}" for b, g, u in broken)
    )


# -- the added-services correction ---------------------------------


def test_non_cruise_charges_are_backed_out_before_comparing():
    """Booking 3000013's shape: $96.00 of excursions inside the total.
    Subtracted rather than refused, because the amount is known exactly -
    refusing would discard four real bookings for no reason."""
    with_services = _check_price_match(
        current_base_price=None, today_base_price=1000.00,
        current_total_price=1096.00, today_price_tab_confirmed=True,
        non_cruise_charges=96.00,
    )
    assert with_services.estimated_value == pytest.approx(0.0, abs=0.02) or (
        with_services.status.value != "OPPORTUNITY")


def test_the_excursion_no_longer_masquerades_as_a_saving():
    """Without the correction, $96 of excursions reads as $96 of saving."""
    uncorrected = _check_price_match(
        current_base_price=None, today_base_price=1000.00,
        current_total_price=1096.00, today_price_tab_confirmed=True)
    corrected = _check_price_match(
        current_base_price=None, today_base_price=1000.00,
        current_total_price=1096.00, today_price_tab_confirmed=True,
        non_cruise_charges=96.00)
    assert uncorrected.estimated_value == pytest.approx(96.00, abs=0.02)
    assert (corrected.estimated_value or 0.0) < 96.00


def test_a_booking_with_no_added_services_is_unaffected():
    plain = _check_price_match(
        current_base_price=None, today_base_price=900.00,
        current_total_price=1000.00, today_price_tab_confirmed=True,
        non_cruise_charges=0.0)
    assert plain.status.value == "OPPORTUNITY"
    assert plain.estimated_value == pytest.approx(100.00, abs=0.02)


# -- IDEA 6: a guard that is never called is not a guard -----------


GUARDS = {
    # guard -> the production module that must reach it
    "msc_occupancy_is_trustworthy": "msc_commands",
    "msc_invoice_guest_count": "msc_commands",
    "msc_invoice_components": "msc_commands",
    "is_valid_msc_booking_id": "msc_commands",
}


@pytest.mark.parametrize("guard,module_name", sorted(GUARDS.items()))
def test_every_guard_is_actually_called_in_production(guard, module_name):
    """CONFIRMED FAILURE THIS SESSION. `msc_occupancy_is_trustworthy` was
    written, tested and documented - and never called. `evaluate_msc_booking`
    accepted `occupancy_verified` and nothing computed it, so the guard
    protecting against the fake $267.01 was completely inert for a day. The
    unit tests all passed the whole time, because they called the guard
    directly.

    A guard is only real if the production path reaches it, so that is what
    this asserts. Definition alone is not enough.
    """
    import importlib
    import inspect

    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    # strip the def block itself so its own name does not count as a call
    calls = re.findall(rf"(?<!def ){re.escape(guard)}\s*\(", src)
    assert len(calls) >= 1, (
        f"{guard} is defined but never called in {module_name} - it is "
        f"inert, exactly like msc_occupancy_is_trustworthy was"
    )


def test_the_calculator_gates_are_all_reachable_from_the_public_function():
    """Each hard gate must be triggerable through `evaluate_msc_booking`,
    not just through the private helper. Both the overpayment rule and the
    occupancy guard were briefly unreachable that way."""
    from core.calculator_msc import evaluate_msc_booking

    base = dict(booking_id="G", category="BP", current_discounts=[],
                today_discount_options=[], today_base_price=100.0,
                current_total_price=1000.0, today_price_tab_confirmed=True)

    overpaid = evaluate_msc_booking(**base, is_overpayment=True)
    assert overpaid.checks == [] and "OVERPAID" in overpaid.note

    occ = evaluate_msc_booking(**base, occupancy_verified=False,
                               occupancy_note="counts disagree")
    assert {c.type.value: c for c in occ.checks}["PRICE_MATCH"].status.value == (
        "INSUFFICIENT_DATA")

    club = evaluate_msc_booking(**base, customer_has_club_membership=True,
                                today_price_includes_club_discount=False)
    assert {c.type.value: c for c in club.checks}["PRICE_MATCH"].status.value == (
        "INSUFFICIENT_DATA")


def test_price_match_keeps_every_new_parameter_keyword_only():
    """Params have been inserted mid-signature twice now, and the first
    time it silently re-bound `is_group_rate` to `occupancy_verified` and
    disabled two hard business rules with nothing raising."""
    import inspect

    params = inspect.signature(_check_price_match).parameters
    positional = [n for n, p in params.items()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert positional == ["current_base_price", "today_base_price"], positional
    for name in ("non_cruise_charges", "customer_has_club_membership",
                 "today_price_includes_club_discount"):
        assert params[name].kind == params[name].KEYWORD_ONLY


# -- IDEA 1: the like-for-like backstop ----------------------------


def test_the_four_historical_bugs_are_all_caught_by_one_rule():
    """THE POINT OF PriceScope. Four separate incidents, four separate
    guards written after each had already produced a wrong number — and
    all four are the same mistake. One rule now catches every one, which
    is what should catch the fifth before it costs anything.
    """
    from core.price_scope import PriceScope, scopes_comparable

    cases = {
        "occupancy (booking 3000081, fake $267.01)": (
            PriceScope(guests=2, label="the booking's own total"),
            PriceScope(guests=1, label="today's listing card"),
            "guest count",
        ),
        "dropped children (booking 3000024, fake $1,929.61)": (
            PriceScope(guests=5), PriceScope(guests=2), "guest count",
        ),
        "multi-cabin (booking 3000071)": (
            PriceScope(cabins=2), PriceScope(cabins=1), "cabin count",
        ),
        "club discount (67 of 86 bookings)": (
            PriceScope(includes_club_discount=True),
            PriceScope(includes_club_discount=False),
            "discount",
        ),
        "added services (booking 3000013, $96.00 overstated)": (
            PriceScope(non_cruise_charges=96.00),
            PriceScope(non_cruise_charges=0.0),
            "non-cruise charges",
        ),
    }
    for name, (left, right, expected_noun) in cases.items():
        ok, why = scopes_comparable(left, right)
        assert ok is False, f"{name} was NOT caught"
        assert expected_noun in why, f"{name}: reason did not name the dimension: {why}"


def test_matching_scopes_compare_normally():
    """It must not refuse everything — that would just be a different way
    of finding nothing."""
    from core.price_scope import PriceScope, scopes_comparable

    ok, why = scopes_comparable(
        PriceScope(guests=2, cabins=1, includes_club_discount=True),
        PriceScope(guests=2, cabins=1, includes_club_discount=True))
    assert ok is True and why == ""


def test_unknown_scope_information_does_not_block():
    """A backstop, not a gate. Refusing on absent metadata would break
    every existing caller and every replay of a historical capture — and
    the specific guards already handle the cases where absence itself is
    disqualifying."""
    from core.price_scope import PriceScope, scopes_comparable

    assert scopes_comparable(None, PriceScope(guests=2))[0] is True
    assert scopes_comparable(PriceScope(), PriceScope())[0] is True
    assert scopes_comparable(PriceScope(guests=2), PriceScope(guests=None))[0] is True


def test_the_reason_names_the_dimension_not_just_the_failure():
    """Every past instance of this bug was diagnosed slowly, from a wrong
    dollar figure. Naming the dimension is most of the value."""
    from core.price_scope import PriceScope, scopes_comparable

    _, why = scopes_comparable(
        PriceScope(guests=2, label="the booking's own total"),
        PriceScope(guests=1, label="today's listing card"))
    assert "guest count MISMATCH" in why
    assert "the booking's own total" in why and "today's listing card" in why


def test_the_backstop_is_reachable_through_price_match():
    """A guard that production cannot trigger is not a guard — the exact
    failure msc_occupancy_is_trustworthy had."""
    from core.price_scope import PriceScope

    chk = _check_price_match(
        current_base_price=None, today_base_price=1.0,
        current_total_price=9999.0, today_price_tab_confirmed=True,
        current_scope=PriceScope(guests=2, label="booking total"),
        today_scope=PriceScope(guests=1, label="today's card"),
    )
    assert chk.status.value == "INSUFFICIENT_DATA"
    assert chk.estimated_value is None
    assert "cannot compare these prices" in chk.note


def test_the_scraper_builds_both_scopes_from_real_data():
    """Scopes that are never populated are decoration. Pin that the
    production call site actually constructs them."""
    import inspect

    import msc_commands

    src = inspect.getsource(msc_commands)
    seg = src[src.index("result = evaluate_msc_booking("):][:2500]
    assert "current_scope=PriceScope(" in seg
    assert "today_scope=PriceScope(" in seg
    assert "msc_invoice_guest_count(" in seg, "guest count not read from the invoice"
