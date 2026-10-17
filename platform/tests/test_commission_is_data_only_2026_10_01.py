"""Commission is COLLECTED, and must never touch the saving logic.

Neon 2026-10-01, in two messages. First the boundary::

    "ignore any agency commission in the road map we will calculate it but
    please do not include it in the scans or python scripts our main focus
    is the prices difference."

then the clarification that sets what was actually built::

    "do not totally ignore the comission include it in the database
    infromations and collected data but seprate it totaly away from our
    optimization process or saving process or whatever u call it."

So: **record it, never act on it.**

WHAT WAS WRONG BEFORE. The arrangement was exactly inverted. NCL's scraper
read `Commiss.Earned`, `Com.Due` and the invoice total from the portal's own
summary, computed a rate - and then **threw all of it away**. It was never
stored. Its only output was a sentence appended to an NCL OPTIMIZATION note:

    "- COSTS $48 OF COMMISSION (at the booking's own 13% rate), so the net
     gain to the agency is $252."

carried by 150 of 2,021 NCL rows. That note never changed `status` or
`net_saving` - but a note IS the optimization's output, which is precisely
what Neon asked to separate.

NOW: `commission_rate`, `commission_earned` and `commission_due` are stored
on every scan (`BookingRecord`), and nothing in the status rules, the
saving maths or any recommendation reads them.

THE REAL GUARD is `test_the_saving_path_never_reads_commission`, which walks
the AST of `calculate_ncl` and `calculate_espresso`. Prose cannot satisfy it
and a future edit that quietly reintroduces the coupling fails it.
"""

import ast
import pathlib

import pytest

from core.calculator import calculate_ncl, ncl_commission_loss
from core.models import BookingResult, BookingStatus, CruiseLine

CALCULATOR = pathlib.Path("core/calculator.py")


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(CALCULATOR.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"core/calculator.py no longer defines {name}")


# -- the separation, structurally ----------------------------------------


#: The ONLY commission-shaped names allowed inside a saving calculation,
#: each for a stated reason. Anything else is a regression.
#:
#:   commission_rate            - passed straight out on the returned
#:                                BookingResult so it can be STORED. Never
#:                                read, never arithmetic.
#:   balance_is_all_commission  - NOT agency accounting. It answers "is this
#:                                saving collectable at all?": when Net Due
#:                                is zero because the whole outstanding
#:                                balance is commission, a reprice cannot
#:                                save the CLIENT anything. That is a
#:                                price-validity fact and it stays.
ALLOWED_IN_SAVING_PATH = {"commission_rate", "balance_is_all_commission"}


def test_the_saving_path_never_reads_commission():
    """The guard. Any commission-shaped name inside a calculation that
    decides a status or a saving must be on the allowlist above, by NAME.

    Deliberately not a count. The first version of this test allowed "at
    most 2 references" and its own comment claimed both were halves of one
    keyword - they were not, they were two unrelated things, so the budget
    was already spent and a NEW commission read would have passed
    unnoticed. A guard that can be satisfied by arithmetic is not a guard.
    """
    offenders = []
    for name in ("calculate_ncl", "calculate_espresso"):
        func = _function(name)
        for node in ast.walk(func):
            found = None
            if isinstance(node, ast.Name):
                found = node.id
            elif isinstance(node, ast.Attribute):
                found = node.attr
            if (found and "commission" in found.lower()
                    and found not in ALLOWED_IN_SAVING_PATH):
                offenders.append(f"{name}:{node.lineno}:{found}")

    assert not offenders, (
        "commission reached a saving path: " + ", ".join(offenders)
        + " — store it on BookingResult instead; see this module's docstring")


def test_the_allowlist_itself_stays_small():
    """If this needs to grow, the separation is eroding. Make the case in
    review rather than widening it quietly."""
    assert len(ALLOWED_IN_SAVING_PATH) == 2


def test_the_rate_is_only_ever_passed_through_never_computed_with():
    """commission_rate may appear exactly once in calculate_ncl, as the
    keyword carrying it out to be stored."""
    func = _function("calculate_ncl")
    uses = [n for n in ast.walk(func)
            if isinstance(n, ast.Name) and n.id == "commission_rate"]
    assert len(uses) == 1, (
        f"commission_rate is read {len(uses)} times; only the pass-through "
        "to BookingResult is allowed")


def test_the_optimization_note_never_mentions_commission():
    """The sentence that used to ride on 150 NCL rows."""
    source = CALCULATOR.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "OF COMMISSION" in node.value:
                pytest.fail(
                    f"line {node.lineno}: commission text is back in a result")


def test_ncl_no_longer_computes_a_commission_loss():
    """`ncl_commission_loss` survives as a utility, but the saving path
    must not call it."""
    func = _function("calculate_ncl")
    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "ncl_commission_loss"]
    assert not calls, "calculate_ncl is computing commission again"


# -- it is still collected -----------------------------------------------


def test_a_booking_result_can_carry_commission():
    result = BookingResult(cruise_line=CruiseLine.NCL, status=BookingStatus.NO_SAVING,
                           booking_id="X", commission_rate=0.1317,
                           commission_earned=521.28, commission_due=250.90)
    assert result.commission_rate == 0.1317
    assert result.commission_earned == 521.28
    assert result.commission_due == 250.90


def test_commission_defaults_to_unknown_not_zero():
    """An unread rate and a zero rate are different facts - the same
    distinction that matters for an unread payment panel."""
    result = BookingResult(cruise_line=CruiseLine.NCL,
                           status=BookingStatus.NO_SAVING, booking_id="X")
    assert result.commission_rate is None
    assert result.commission_earned is None
    assert result.commission_due is None


def test_the_database_has_somewhere_to_put_it():
    from models.database import BookingRecord

    for column in ("commission_rate", "commission_earned", "commission_due"):
        assert hasattr(BookingRecord, column), f"{column} is not stored"


def test_the_scan_writer_persists_all_three():
    """Columns nothing writes to are worse than no columns."""
    source = pathlib.Path("services/booking_service.py").read_text(encoding="utf-8")
    start = source.index("record = BookingRecord(")
    window = source[start:start + 3000]
    for column in ("commission_rate", "commission_earned", "commission_due"):
        assert f"{column}=result.{column}" in window, f"{column} is never written"


def test_ncl_still_reads_commission_from_the_portal():
    """Collection must survive the separation - that was the whole point
    of Neon's second message."""
    source = pathlib.Path("scraper/ncl.py").read_text(encoding="utf-8")
    assert "result.commission_earned = commiss_earned" in source
    assert "result.commission_due = com_due" in source
    assert "commission_rate = round(commiss_earned / invoice_total, 4)" in source


# -- the verdict is unchanged by commission ------------------------------


def test_the_same_booking_scores_identically_at_any_commission_rate():
    """The behavioural proof: vary only the commission rate and nothing
    about the recommendation may move."""
    def run(rate):
        return calculate_ncl(
            "3000054", "IB", 3000.0, 2700.0,
            amount_due=3000.0, commission_rate=rate)

    none_, low, high = run(None), run(0.05), run(0.40)

    assert none_.status == low.status == high.status
    assert none_.net_saving == low.net_saving == high.net_saving
    assert none_.note == low.note == high.note


def test_the_rate_still_reaches_the_result_for_storage():
    result = calculate_ncl("3000054", "IB", 3000.0, 2700.0,
                           amount_due=3000.0, commission_rate=0.1317)
    assert result.commission_rate == 0.1317


# -- the helper stays correct, just uncalled -----------------------------


def test_the_commission_helper_is_still_right():
    """Kept so the agency can use it against the stored figures."""
    assert ncl_commission_loss(300.0, 0.16) == 48.0
    assert ncl_commission_loss(300.0, None) == 0.0
    assert ncl_commission_loss(300.0, 0.0) == 0.0
