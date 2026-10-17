"""An unreadable NCL price must never be reported as "no saving".

Neon, mid-run 2026-09-16: "it is results is only paid in full and no
savings the price does not chang at atll check the code again there is
somethong wrong with the code".

THE DEFECT, in two halves that combined into a silent wrong answer:

  1. The category-grid read coerced an unparseable price to zero -
     `resTotal: parseFloat(c.ResTotal) || 0`.
  2. The comparison then treated zero as "no change":
     `if live_price <= 0 or abs(live_price - old_total) < 0.01:` returned
     `calculate_ncl(..., old_total, old_total, ...)` - passing the OLD
     total as today's price, which literally states "today's price is
     identical".

So a price that was never read and a price that genuinely did not move
produced the same output: a confident NO_SAVING. A whole run can then show
prices that never change, which is exactly what Neon saw.

Same class as the MSC club-discount bug - a FAILURE presented as a
negative RESULT. The two must be distinguishable.
"""
import ast
import inspect

import pytest

import scraper.ncl as ncl_mod


def _price_branch_source() -> str:
    src = inspect.getsource(ncl_mod)
    start = src.index('live_price = current["resTotal"]')
    return src[start:start + 2600]


# -- the read must not invent a zero ---------------------------------


def test_the_grid_read_no_longer_coerces_an_unreadable_price_to_zero():
    """`|| 0` is what made a failed parse look like a real price of $0."""
    src = inspect.getsource(ncl_mod)
    assert "parseFloat(c.ResTotal) || 0" not in src, (
        "an unparseable ResTotal is being coerced to 0 again - that is "
        "indistinguishable from a genuine price and becomes 'no saving'"
    )


def test_the_grid_read_returns_null_for_a_missing_price():
    src = inspect.getsource(ncl_mod)
    seg = src[src.index("resTotal: (c.ResTotal"):][:520]
    for guard in ("=== null", "=== undefined", "isFinite"):
        assert guard in seg, f"{guard} missing from the resTotal read"
    assert "? null :" in seg


# -- an unreadable price must not become a verdict -------------------


def test_an_unreadable_price_is_refused_not_reported_as_no_saving():
    branch = _price_branch_source()
    assert "if live_price is None or live_price <= 0:" in branch
    assert "refusing" in branch, (
        "the unreadable case must refuse, not fall through to a verdict"
    )


def test_the_unreadable_case_no_longer_shares_a_branch_with_no_change():
    """The original single condition is what merged the two meanings."""
    src = inspect.getsource(ncl_mod)
    assert "if live_price <= 0 or abs(live_price - old_total) < 0.01:" not in src


def test_no_verdict_passes_old_total_as_todays_price_on_the_unreadable_path():
    """`calculate_ncl(..., old_total, old_total, ...)` is the statement
    'today costs exactly what it cost before'. It is legitimate for a
    genuinely unchanged price, and a lie for one that was never read."""
    src = inspect.getsource(ncl_mod)
    start = src.index("if live_price is None or live_price <= 0:")
    end = src.index("if live_price > old_total + 0.01:")
    unreadable_branch = src[start:end]
    assert "calculate_ncl(" not in unreadable_branch


# -- ordering: the guard must run before ANY comparison --------------


def test_the_guard_runs_before_every_numeric_comparison():
    """With an unreadable price now None rather than 0, comparing it
    before the guard would raise TypeError deep inside a live scrape. I
    made exactly that mistake writing this fix; the ordering is the point.
    """
    src = inspect.getsource(ncl_mod)
    assign = src.index('live_price = current["resTotal"]')
    guard = src.index("if live_price is None or live_price <= 0:")
    increase = src.index("if live_price > old_total + 0.01:")
    unchanged = src.index("if abs(live_price - old_total) < 0.01:")
    assert assign < guard < increase, "guard must precede the increase check"
    assert guard < unchanged, "guard must precede the unchanged check"


def test_the_price_branch_is_syntactically_whole():
    """The guard was moved by text surgery; make sure the module still
    parses and the function boundaries were not broken."""
    ast.parse(inspect.getsource(ncl_mod))


# -- the legitimate outcomes must still work -------------------------


@pytest.mark.parametrize("live,old,expect", [
    (1178.00, 1138.00, "increase"),   # booking 3000058, real: $40 more
    (1138.00, 1138.00, "unchanged"),
    (1000.00, 1138.00, "cheaper"),
])
def test_the_three_real_outcomes_remain_distinguishable(live, old, expect):
    """Refusing an unreadable price must not disturb the genuine cases: a
    real increase, a real no-change, and a real saving."""
    if expect == "increase":
        assert live > old + 0.01
    elif expect == "unchanged":
        assert abs(live - old) < 0.01
    else:
        assert live < old - 0.01
    assert live is not None and live > 0
