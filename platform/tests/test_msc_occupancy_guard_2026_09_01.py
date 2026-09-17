"""The MSC occupancy blind spot, closed.

Neon gave three real MSC answers on 2026-09-01:
    3000081  less 81.98 + 25 extra OBC   (system said $267.01)
    3000071  less 63.24                  (right lever found, no dollar value)
    3000083  less 23.66                  (system said "no opportunity found")

Booking 3000081, the SAME booking on two dates:
  2026-08-12  extraction 2 -> screen 2 adults -> today $3,610.66, ABOVE the
              current $3,517.34 -> correctly NO opportunity
  2026-08-24  extraction 2 -> screen **1 adult** -> today $3,250.33 ->
              reported a "confirmed price-match opportunity" of $267.01

A one-guest quote was compared against a two-guest booking total. Passenger
extraction is also intermittently short: six captures of this booking
returned 2, 1, 2, 0, 2, 2 while the invoice consistently says
"Passengers : 2".

This is the 3000024 bug (3 kids dropped -> fake $1,929.61) recurring
somewhere new. Counting from ONE source can always fail this way, so the
count is now cross-checked against the invoice's own words AND against the
state the occupancy screen actually reached.
"""
from core.calculator_msc import _check_price_match
from msc_commands import (
    _compute_required_occupancy,
    msc_invoice_guest_count,
    msc_occupancy_is_trustworthy,
)


# Always present at module scope at runtime, but absent from
# `dir(builtins)` - so a scanner that checks builtins alone reports
# __file__ as undefined. Found 2026-09-15 when gui/windows.py started
# using __file__ to locate the project root.
_MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__debug__",
}

def _pax(*ages):
    return [{"name": f"P{i}", "age": a} for i, a in enumerate(ages)]


# -- the count itself ------------------------------------------------


def test_a_passenger_with_no_readable_age_is_counted_not_silently_dropped():
    """`if age is None: continue` used to skip silently, so a 2-guest
    booking could be priced as 1 guest with nothing recorded anywhere."""
    req = _compute_required_occupancy(
        [{"name": "A", "age": 60}, {"name": "B", "age": None}])
    assert req["counts"]["adult"] == 1
    assert req["dropped"] == 1
    assert req["passengers_seen"] == 2


def test_total_guests_is_reported_for_cross_checking():
    req = _compute_required_occupancy(_pax(60, 75))
    assert req["total_guests"] == 2
    assert req["dropped"] == 0


# -- the invoice's own count ----------------------------------------


def test_guest_count_read_from_the_summary_wording():
    assert msc_invoice_guest_count("Passengers :\n2\n", None) == 2


def test_guest_count_read_from_total_adult_lines():
    assert msc_invoice_guest_count(
        None, "Total Adult 1 ...\nTotal Adult 2 ...") == 2


def test_unreadable_invoice_count_is_none_not_a_guess():
    assert msc_invoice_guest_count("no such field", "") is None


# -- the trust rule -------------------------------------------------


def test_a_short_extraction_is_refused():
    """The 2026-08-11T12:29:01 capture: extracted 1, invoice said 2."""
    ok, why = msc_occupancy_is_trustworthy(
        _compute_required_occupancy(_pax(60)), 2)
    assert ok is False
    assert "MISMATCH" in why and "invoice says 2" in why


def test_zero_guests_is_refused():
    """The 2026-08-12T10:15:26 capture: extracted 0 while the invoice said 2.
    Zero guests can never be a basis for a price."""
    ok, why = msc_occupancy_is_trustworthy(_compute_required_occupancy([]), 2)
    assert ok is False
    assert "no guests" in why.lower()


def test_an_unreadable_invoice_count_is_refused():
    """Without a second source there is nothing to cross-check against, so
    the count cannot be trusted - the whole point of the rule."""
    ok, why = msc_occupancy_is_trustworthy(
        _compute_required_occupancy(_pax(60, 75)), None)
    assert ok is False
    assert "cannot be cross-checked" in why


def test_a_matching_count_is_trusted():
    """Must not over-block: a correct occupancy has to price normally."""
    ok, why = msc_occupancy_is_trustworthy(
        _compute_required_occupancy(_pax(60, 75)), 2)
    assert ok is True and why == ""


def test_dropped_passengers_are_refused_even_when_totals_coincide():
    """A dropped passenger can leave the total accidentally matching. The
    drop itself is disqualifying."""
    req = _compute_required_occupancy([{"age": 60}, {"age": None}])
    ok, why = msc_occupancy_is_trustworthy(req, 1)
    assert ok is False
    assert "no readable age" in why


# -- the APPLIED state, which is what actually got priced -----------


def test_the_screen_state_is_checked_not_just_the_intention():
    """THE 2026-08-24 case. Extraction was 2 and the invoice said 2 - every
    count agreed - and the screen still ended at 1 adult. Checking intent
    alone can never catch this."""
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"after": {"adult": 1, "child": 0, "jrchild": 0, "infant": 0},
           "applied_guests": 1, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, 2, fix)
    assert ok is False
    assert "ended at 1 guest" in why


def test_a_stalled_occupancy_fix_is_refused():
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"after": {"adult": 2}, "applied_guests": 2, "stalled": True}
    ok, why = msc_occupancy_is_trustworthy(req, 2, fix)
    assert ok is False
    assert "stalled" in why


def test_a_correctly_applied_occupancy_passes():
    """The 2026-08-12 run: screen reached 2 adults, invoice said 2."""
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"after": {"adult": 2, "child": 0, "jrchild": 0, "infant": 0},
           "applied_guests": 2, "stalled": False}
    ok, _ = msc_occupancy_is_trustworthy(req, 2, fix)
    assert ok is True


# -- the calculator must refuse rather than produce a number --------


def test_the_fake_267_dollar_opportunity_is_refused():
    """Booking 3000081's exact figures. Real answer: $81.98."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=3250.33,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        occupancy_verified=False,
        occupancy_note="the occupancy screen ended at 1 guest(s) but the invoice says 2",
    )
    assert chk.status.value == "INSUFFICIENT_DATA"
    assert chk.estimated_value is None, "a fabricated figure must not survive"
    assert "not verified against the invoice" in chk.note


def test_a_verified_occupancy_still_prices():
    """The guard must not disable price matching altogether."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=3250.33,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        occupancy_verified=True,
    )
    assert chk.status.value == "OPPORTUNITY"
    assert chk.estimated_value == 267.01


def test_the_guard_runs_before_any_arithmetic():
    """No price is worth computing from mismatched guest counts, so the
    refusal must not depend on the numbers being sane."""
    chk = _check_price_match(
        current_base_price=999999.0, today_base_price=1.0,
        current_total_price=999999.0, today_price_tab_confirmed=True,
        occupancy_verified=False, occupancy_note="mismatch",
    )
    assert chk.status.value == "INSUFFICIENT_DATA"
    assert chk.estimated_value is None


# -- the blind spot the FIX itself created, then closed --------------


def test_price_match_params_are_keyword_only():
    """MY OWN BUG, 2026-09-01. `occupancy_verified` / `occupancy_note` were
    inserted mid-signature while the only production call site passed
    everything POSITIONALLY. That silently re-bound `is_group_rate` ->
    `occupancy_verified` and `is_paid_in_full` -> `occupancy_note`, so the
    paid-in-full rule and the final-payment gate - two HARD business rules -
    stopped being applied at all. Nothing raised.

    Keyword-only makes the same mistake a TypeError instead of a wrong
    answer, and lets parameters be added later without auditing callers.
    """
    import inspect

    params = inspect.signature(_check_price_match).parameters
    positional = [n for n, p in params.items()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert positional == ["current_base_price", "today_base_price"], (
        f"only the two prices may be positional, got {positional}"
    )
    for name in ("is_paid_in_full", "final_payment_date_passed",
                 "is_group_rate", "occupancy_verified"):
        assert params[name].kind == params[name].KEYWORD_ONLY, f"{name} is positional"


def test_paid_in_full_still_reaches_the_rule_through_the_public_entry_point():
    """The regression the shifted arguments caused, pinned at the level that
    actually matters - the public function, not the private helper."""
    from core.calculator_msc import evaluate_msc_booking

    result = evaluate_msc_booking(
        booking_id="PIF", category="BP", current_base_price=1000.0,
        today_base_price=500.0, current_discounts=[],
        today_discount_options=["SENIOR DISCOUNT"], club_discount_offered=True,
        is_paid_in_full=True,
    )
    by_type = {c.type.value: c for c in result.checks}
    pm = by_type["PRICE_MATCH"]
    assert pm.status.value == "NO_OPPORTUNITY", (
        "a paid-in-full booking must never report a price-match opportunity"
    )
    assert "paid in full" in pm.note.lower()


def test_the_occupancy_guard_is_reachable_from_the_public_entry_point():
    """The guard is useless if the public function cannot pass it through."""
    from core.calculator_msc import evaluate_msc_booking

    result = evaluate_msc_booking(
        booking_id="OCC", category="IR2", current_base_price=None,
        today_base_price=3250.33, current_total_price=3517.34,
        current_discounts=[], today_discount_options=[],
        today_price_tab_confirmed=True,
        occupancy_verified=False,
        occupancy_note="the occupancy screen ended at 1 guest(s) but the invoice says 2",
    )
    pm = {c.type.value: c for c in result.checks}["PRICE_MATCH"]
    assert pm.status.value == "INSUFFICIENT_DATA"
    assert pm.estimated_value is None


# -- MSC's own pre-fill: the second source that actually exists ------


def test_reducing_below_mscs_own_prefill_is_refused():
    """THE REAL 2026-08-24 FAILURE, replayed from the stored capture.

    occupancy_fix showed before=2 adults (MSC arrived already populated
    FROM THE BOOKING), required=1, after=1. The code did not fail to read
    the guest count - it overrode a CORRECT pre-fill of 2 with its own bad
    extraction of 1, then priced 1 adult ($3,250.33) against a 2-guest
    total ($3,517.34) and reported a fake $267.01. Real answer: $81.98.

    Across every dated capture of this booking the pre-fill read 2 and was
    right, including on the run that produced the fake price - so it is a
    genuine independent source, and a downward correction against it is
    never trustworthy.
    """
    req = _compute_required_occupancy(_pax(60))          # extraction said 1
    fix = {"before": {"adult": 2, "child": 0, "jrchild": 0, "infant": 0},
           "required": {"adult": 1}, "after": {"adult": 1},
           "applied_guests": 1, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, None, fix)
    assert ok is False
    assert "REDUCED below MSC's own pre-fill" in why
    assert "fake $267.01" in why


def test_the_prefill_substitutes_when_the_invoice_count_is_unreadable():
    """CONFIRMED GAP, 2026-09-01. The live run reported
    invoice_guest_count=None on all three of Neon's bookings - `staged`
    carries no summary_text/breakdown_text, so the invoice reading could
    never fire and the guard was inert in production. The pre-fill IS
    present in every capture, so it is what the rule relies on."""
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"before": {"adult": 2}, "after": {"adult": 2},
           "applied_guests": 2, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, None, fix)
    assert ok is True, why


def test_no_second_source_at_all_is_still_refused():
    """Neither the invoice nor a pre-fill means nothing to cross-check
    against - the original refusal must survive."""
    req = _compute_required_occupancy(_pax(60, 75))
    ok, why = msc_occupancy_is_trustworthy(req, None, None)
    assert ok is False
    assert "cannot be cross-checked" in why


def test_a_prefill_lower_than_the_booking_does_not_block():
    """Upward correction is the safe direction: MSC pre-filling fewer
    guests than the booking has cannot manufacture a saving. Blocking it
    would refuse legitimate work."""
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"before": {"adult": 1}, "after": {"adult": 2},
           "applied_guests": 2, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, 2, fix)
    assert ok is True, why


def test_the_2026_09_01_captures_all_pass_the_guard():
    """The three bookings Neon gave real answers for, with the occupancy
    figures their fresh captures actually recorded. All three were applied
    correctly, so the guard must let them price - it exists to catch the
    bad run, not to disable MSC."""
    captures = {
        "3000081": (2, {"before": {"adult": 2}, "after": {"adult": 2},
                         "applied_guests": 2, "stalled": False}),
        "3000071": (1, {"before": {"adult": 1}, "after": {"adult": 1},
                         "applied_guests": 1, "stalled": False}),
        "3000083": (2, {"before": {"adult": 2}, "after": {"adult": 2},
                         "applied_guests": 2, "stalled": False}),
    }
    for booking, (guests, fix) in captures.items():
        req = _compute_required_occupancy(_pax(*([60] * guests)))
        ok, why = msc_occupancy_is_trustworthy(req, None, fix)
        assert ok is True, f"{booking} was wrongly refused: {why}"


def test_no_function_references_an_undefined_staged_variable():
    """MY OWN BUG, 2026-09-01, and the reason this test exists at module
    scope rather than as a comment.

    A `.replace(old, new, 1)` patch anchored on "listing_confirmed" hit the
    FIRST occurrence in the file - inside `_check_today_rate()`, ~2500 lines
    before the intended rate record - leaving `staged.get("summary_text")`
    in a function that has no `staged` variable at all. That is a guaranteed
    NameError on every console check-rates run. No test caught it because
    the live path uses a different function, and the file still compiled.

    Scans EVERY function, so the next misplaced patch fails here.
    """
    import ast
    import builtins
    import inspect

    import msc_commands

    tree = ast.parse(inspect.getsource(msc_commands))

    def bound_names(node):
        """Every name this scope binds, INCLUDING names bound by nested
        functions - a closure legitimately reads its parent's locals, so
        checking a nested def in isolation produces false positives (the
        first version of this test flagged 40 of them)."""
        names = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs:
                names.add(arg.arg)
            for extra in (a.vararg, a.kwarg):
                if extra:
                    names.add(extra.arg)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                names.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
                names |= bound_names(n) if n is not node else set()
            elif isinstance(n, ast.Lambda) and n is not node:
                names |= bound_names(n)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                names |= set(n.names)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    names.add(alias.asname or alias.name.split(".")[0])
        return names

    module_scope = bound_names(tree) | set(dir(builtins)) | _MODULE_DUNDERS

    problems = []
    for func in tree.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        visible = module_scope | bound_names(func)
        for n in ast.walk(func):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id not in visible):
                problems.append(f"{func.name}() line {n.lineno}: '{n.id}'")

    assert not problems, (
        "undefined name(s) referenced - NameError at runtime:\n  "
        + "\n  ".join(sorted(set(problems)))
    )


# -- multi-cabin: the 3000071 finding ------------------------------


def test_a_multi_cabin_booking_cannot_be_price_matched():
    """CONFIRMED 2026-09-01 on booking 3000071, one of the three Neon gave
    a real answer for ($63.24).

    Its stored invoice carries TWO cabin rows (Cabin 1 and Cabin 2, both
    N°14071) and only ONE "Total Adult" line. Every selector in the pricing
    flow targets cabin 1, while `current_value` ($2,357.72) is the total
    across both cabins - so a one-cabin quote was being compared against a
    two-cabin total on all four of its captures. That comparison is wrong in
    both directions regardless of the guest count, so it is refused before
    any guest arithmetic.
    """
    req = _compute_required_occupancy(_pax(60))
    fix = {"before": {"adult": 2}, "after": {"adult": 1},
           "applied_guests": 1, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, 1, fix, cabin_count=2)
    assert ok is False
    assert "2 cabins" in why
    assert "cabin 1 only" in why


def test_a_single_cabin_booking_is_unaffected_by_the_cabin_rule():
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"before": {"adult": 2}, "after": {"adult": 2},
           "applied_guests": 2, "stalled": False}
    ok, why = msc_occupancy_is_trustworthy(req, 2, fix, cabin_count=1)
    assert ok is True, why


def test_the_cabin_rule_is_checked_before_the_prefill_rule():
    """On 3000071 the pre-fill read 2 because it tracks the two CABIN rows,
    not two guests. If the pre-fill rule ran first the refusal would name
    the wrong cause, and a reader would go looking for a passenger-parsing
    bug that is not there."""
    req = _compute_required_occupancy(_pax(60))
    fix = {"before": {"adult": 2}, "after": {"adult": 1},
           "applied_guests": 1, "stalled": False}
    _, why = msc_occupancy_is_trustworthy(req, None, fix, cabin_count=2)
    assert "cabins" in why
    assert "pre-fill" not in why


def test_an_unknown_cabin_count_does_not_block():
    """cabin_count is absent from older captured records replayed through
    msc_run_calculator.py - that must not refuse every historical booking."""
    req = _compute_required_occupancy(_pax(60, 75))
    fix = {"before": {"adult": 2}, "after": {"adult": 2},
           "applied_guests": 2, "stalled": False}
    ok, _ = msc_occupancy_is_trustworthy(req, 2, fix, cabin_count=None)
    assert ok is True
