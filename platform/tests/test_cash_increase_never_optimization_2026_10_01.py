"""If the client pays more cash, it is never a green OPTIMIZATION.

THE INCIDENT, 2026-10-01. Booking 3001014 reached the GUI as a GREEN
OPTIMIZATION row::

    3001014  ESPRESSO  OPTIMIZATION  -$53.00 saved  9,236.94  9,483.94
             note: "optimized $53 — re-add: BONUS SAV NRD"

The client's bill goes **UP $247**. Neon, on seeing it:

    "THIS BOOKING IS SHOWING A HIGHER PRICE AND IT IS GREEN AS WELL AS IT
    IS SHOWING AN OPTIMIZATION CAN YOU FIX THIS BUG. SUCH MISTAKE CAN
    NEVER HAPPEN AGAIN IN OUR PROJECT!!!"

The arithmetic was right and the rule was wrong::

    price_drop  -247.00      (negative: the price went UP)
    obc_change  +300.00
    net          +53.00  ->  OPTIMIZATION

`net_saving` adds OBC to cash as if they were the same thing. **They are
not.** Onboard credit cannot pay the invoice, is restricted to onboard
spending, and is commonly use-it-or-lose-it and non-refundable. Handing
over $247 of real money for $300 of OBC is a judgement call, never an
automatic win.

A 2026-08-13 audit had examined this exact behaviour, recorded it as
"CONFIRMED INTENTIONAL DESIGN, not a bug", and written
`test_case_c_documented_edge_case_fare_increase_with_bigger_obc_gain` to
lock it in. The test did its job - it made the behaviour visible and
stable. It was still wrong, and only the person who sells the result could
say so. **A test defending a behaviour is not evidence the behaviour is
correct.**

The calculator already refused the MIRROR of this trade - a price drop paid
for by surrendering OBC is held to `OBC_LOSS_MIN_RATIO`. It simply had no
guard in this direction. The rule is now symmetric.
"""

import json
import pathlib

import pytest

from core.calculator import calculate_espresso
from core.models import BookingStatus

RAW = pathlib.Path("data/raw_responses.jsonl")


def _raw(old_total, new_total, old_obc=0.0, new_obc=0.0):
    """Minimal real-shaped reprice payload."""
    def items(total, obc):
        return [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": total},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": obc},
        ]
    return {"result": {
        "oldInvoice": {"invoiceItems": items(old_total, old_obc)},
        "newInvoice": {"invoiceItems": items(new_total, new_obc)},
    }}


# -- the real booking --------------------------------------------------


def test_3001014_is_not_an_optimization():
    """Against the real captured payload that produced the green row."""
    if not RAW.exists():
        pytest.skip("captured corpus not present")
    found = None
    with RAW.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "3001014" not in line or '"showRepriceModal"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("raw")
            if (isinstance(payload, dict)
                    and isinstance(payload.get("result"), dict)
                    and str(rec.get("captured_at", "")).startswith("2026-10-01")):
                found = payload
    if found is None:
        pytest.skip("no 2026-10-01 capture for 3001014")

    result = calculate_espresso(found, "3001014", None)

    assert result.new_total > result.old_total, "the premise: the price went UP"
    assert result.status != BookingStatus.OPTIMIZATION
    assert result.status == BookingStatus.NO_SAVING


# -- the rule ----------------------------------------------------------


def test_a_price_increase_is_never_an_optimization_however_large_the_obc():
    """Even an absurd OBC gain cannot make a higher bill a win."""
    result = calculate_espresso(
        _raw(1000.0, 1050.0, old_obc=0.0, new_obc=5000.0), "X", None)
    assert result.status != BookingStatus.OPTIMIZATION


def test_the_trade_is_reported_not_hidden():
    """A human may genuinely want the OBC, so the numbers must be on the
    row - the fix suppresses the green label, not the information."""
    result = calculate_espresso(
        _raw(9236.94, 9483.94, old_obc=300.0, new_obc=600.0), "X", None)
    assert "INCREASES" in result.note
    assert "247" in result.note
    assert "300" in result.note and "OBC" in result.note


def test_the_note_says_why_obc_is_not_cash():
    """The reason has to travel with the row; a bare "no saving" invites
    someone to override it."""
    result = calculate_espresso(
        _raw(1000.0, 1050.0, old_obc=0.0, new_obc=200.0), "X", None)
    assert "OBC is not cash" in result.note


def test_an_equal_price_is_not_caught_by_this_guard():
    """The guard fires on an INCREASE. A flat price with a real OBC gain
    is still a legitimate win - nothing more is being paid."""
    result = calculate_espresso(
        _raw(1000.0, 1000.0, old_obc=0.0, new_obc=200.0), "X", None)
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.net_saving == 200.0


def test_a_genuine_price_drop_is_still_an_optimization():
    """The guard must not suppress the thing the product exists to find."""
    result = calculate_espresso(_raw(2468.78, 2389.78), "3001011", None)
    assert result.status == BookingStatus.OPTIMIZATION
    assert result.net_saving == 79.0


def test_losing_obc_to_get_a_drop_is_still_held_to_the_ratio():
    """The mirror guard that already existed must survive untouched."""
    result = calculate_espresso(
        _raw(1000.0, 700.0, old_obc=250.0, new_obc=0.0), "X", None)
    assert result.status != BookingStatus.OPTIMIZATION


# -- the invariant, across every capture we hold -----------------------


def test_no_capture_in_the_corpus_yields_a_green_row_at_a_higher_price():
    """The whole corpus, as a standing guarantee. If this ever fails, a
    client is being shown a price rise labelled as a saving."""
    if not RAW.exists():
        pytest.skip("captured corpus not present")

    offenders = []
    with RAW.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"showRepriceModal"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("raw")
            if not (isinstance(payload, dict)
                    and isinstance(payload.get("result"), dict)):
                continue
            if not (payload["result"].get("oldInvoice") or {}).get("invoiceItems"):
                continue
            try:
                result = calculate_espresso(payload, str(rec.get("booking_id")), None)
            except Exception:  # noqa: BLE001 - malformed captures are not this test's subject
                continue
            if (result.status == BookingStatus.OPTIMIZATION
                    and result.old_total is not None
                    and result.new_total is not None
                    and result.new_total > result.old_total):
                offenders.append(
                    f"{rec.get('booking_id')} {result.old_total}->{result.new_total}")

    assert not offenders, (
        f"{len(offenders)} optimizations quote a HIGHER price: {offenders[:5]}")


# -- the money column must say "higher price", not a positive figure ----
#
# Neon 2026-10-01, after the status was corrected: "I WANT TO TREAT THIS AS
# NO SAVING AND A HIGHER PRICE." The status said NO_SAVING, but the Net
# Saving column still rendered "$53.00 (not recommended - see status)" - a
# positive dollar figure on a booking that costs $247 MORE. That is most of
# what made the row alarming, and the status fix alone did not touch it.


def _format(net_saving, status, price_drop=None):
    """The shipped GUI formatter, pulled from the AST.

    Imported this way rather than by importing gui.windows, which needs a
    Qt application. Taking it from the tree also means a comment quoting
    these strings can never satisfy the assertions.
    """
    import ast
    import pathlib

    source = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    namespace: dict = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "_format_net_saving":
            node.decorator_list = []
            exec(compile(ast.Module([node], []), "<f>", "exec"), namespace)  # noqa: S102
            return namespace["_format_net_saving"](net_saving, status, price_drop)
    pytest.fail("gui/windows.py no longer defines _format_net_saving")


def test_a_higher_price_is_labelled_more_expensive_not_a_dollar_figure():
    """3001014 exactly: net +53 on paper, cash price up 247."""
    assert _format(53.0, "NO_SAVING", -247.0) == "+$247.00 more expensive"


def test_the_cash_increase_beats_the_positive_net_in_the_column():
    """Without the price_drop the old text comes back - this is the
    assertion that the call site actually passes it."""
    assert "53.00" not in _format(53.0, "NO_SAVING", -247.0)


def test_a_genuine_saving_still_reads_as_saved():
    assert _format(79.0, "OPTIMIZATION", 79.0) == "$79.00 saved"


def test_a_flat_price_with_an_obc_gain_still_reads_as_saved():
    """price_drop == 0 is not an increase; the guard must not fire."""
    assert _format(200.0, "OPTIMIZATION", 0.0) == "$200.00 saved"


def test_a_missing_price_drop_falls_back_to_the_old_behaviour():
    """Older rows reloaded from the database may not carry price_drop."""
    assert _format(53.0, "NO_SAVING", None).startswith("$53.00")


def test_a_saving_carries_no_minus_sign():
    """Neon 2026-10-01: the sign used to follow the PRICE DELTA, so a
    saving read "-$79.00 saved" - a minus beside the word "saved". Gone."""
    rendered = _format(79.0, "OPTIMIZATION", 79.0)
    assert not rendered.startswith("-")
    assert rendered == "$79.00 saved"


def test_a_trap_states_the_cash_and_the_outcome():
    """3001020: the cash price FALLS $546 while a $1,260 perk is given up.
    Reporting only "more expensive" is true about the outcome and false
    about the cash - the same half-truth that made 3001014 alarming,
    pointing the other way."""
    rendered = _format(-714.0, "TRAP", 546.0)
    assert "546" in rendered and "714" in rendered
    assert "worse overall" in rendered
    assert "more expensive" not in rendered


def test_a_real_cost_increase_still_reads_more_expensive():
    """Cash and net agree here, so the simple wording stands."""
    assert _format(-250.0, "NO_SAVING", -250.0) == "+$250.00 more expensive"


def test_an_old_row_without_a_price_delta_still_renders():
    """Rows reloaded from the database may predate price_drop."""
    assert _format(-714.0, "TRAP", None) == "+$714.00 more expensive"
