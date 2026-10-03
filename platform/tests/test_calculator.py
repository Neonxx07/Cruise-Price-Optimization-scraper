"""Core ESPRESSO/NCL calculation-engine tests.

Covers the Case A-L matrix from the 2026-08-13 production-readiness
audit, plus regression tests for every bug that audit found and fixed
in core/calculator.py. Deliberately NOT exhaustive of every code path
in calculator.py -- this targets the highest-risk area (silent wrong
financial results), per that audit's own instruction: "the goal is not
test quantity, the goal is preventing today's bugs from returning
tomorrow."
"""
from core.calculator import calculate_espresso, calculate_ncl, round2, total_optimization_savings
from core.models import BookingResult, BookingStatus, CruiseLine


def _espresso_raw(old_total, new_total, old_obc=0.0, new_obc=0.0, old_pkgs=None, new_pkgs=None):
    """Build a minimal, realistic ESPRESSO reprice-modal raw_data dict.

    SHAPE CORRECTED 2026-09-30, and the correction is the whole point of
    the bug these tests missed. The old builder emitted a perk as::

        {"paxId": "total", "type": "CRUISE_PROMO", "name": ..., "amount": +594}

    Measured across all 3,074 captured reprice responses, that row cannot
    occur:

      - every one of the 46,573 `paxId == "total"` rows has NO type field;
      - all 22,839 CRUISE_PROMO rows are per-passenger, never "total"
        (already stated in _get_promo_value_by_name's docstring: "0 of
        1,616 real instances");
      - a real perk is a COMPONENT (BEVCLINGRA9, BASICWIFCH9), all 7,626
        of which are likewise per-passenger.

    So the fixture asserted a perk is a CRUISE_PROMO, while in reality a
    positive CRUISE_PROMO is a SURCHARGE (`ALL INC 2PK NRD` at +$1,260 is
    the price of the package). The tests passed on data the portal never
    sends, and the 167 real cases went unnoticed.

    Now each perk emits what the portal actually sends: an untyped
    summary row plus a typed per-passenger COMPONENT row. Pass
    `type="CRUISE_PROMO"` in a pkg dict to model a fare/promo line
    instead of a perk.
    """
    def items(total, obc, pkgs):
        rows = [
            {"paxId": "total", "type": "VACATION_TOTAL", "amount": total},
            {"paxId": "total", "type": "OBC_TOTAL", "amount": obc},
        ]
        for pkg in (pkgs or []):
            # The summary row the calculator reads for the dollar value -
            # untyped, exactly as the portal sends it.
            rows.append({"paxId": "total", "name": pkg["name"], "amount": pkg["amount"]})
            # The per-passenger row that carries the TYPE, which is the
            # only place the category can be recovered from.
            rows.append({
                "paxId": "1",
                "type": pkg.get("type", "COMPONENT"),
                "name": pkg["name"],
                "amount": pkg["amount"],
            })
        return rows

    return {
        "result": {
            "oldInvoice": {"invoiceItems": items(old_total, old_obc, old_pkgs)},
            "newInvoice": {"invoiceItems": items(new_total, new_obc, new_pkgs)},
        }
    }


# ── Case A: normal price reduction ──────────────────────────────


def test_case_a_price_reduction_is_optimization():
    raw = _espresso_raw(old_total=1332.46, new_total=1282.46)
    r = calculate_espresso(raw, "BOOK1")
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.price_drop == 50.0
    assert r.net_saving == 50.0


# ── Case B: no price change ──────────────────────────────────────


def test_case_b_no_change_is_not_a_false_gain():
    raw = _espresso_raw(old_total=1332.46, new_total=1332.46)
    r = calculate_espresso(raw, "BOOK2")
    assert r.status == BookingStatus.NO_SAVING
    assert r.net_saving == 0.0


def test_case_b_no_change_but_obc_gain_is_a_real_optimization():
    # Flat fare + a genuine new OBC perk is a real, legitimate win --
    # confirmed intentional, not a bug (net_saving deliberately combines
    # fare and OBC dollars).
    raw = _espresso_raw(old_total=1000.0, new_total=1000.0, old_obc=0.0, new_obc=37.50)
    r = calculate_espresso(raw, "BOOK2B")
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 37.50


# ── Case C: price increase ───────────────────────────────────────


def test_case_c_plain_price_increase_is_not_misclassified():
    raw = _espresso_raw(old_total=1282.46, new_total=1332.46)
    r = calculate_espresso(raw, "BOOK3")
    assert r.status != BookingStatus.OPTIMIZATION
    assert r.status == BookingStatus.NO_SAVING


def test_case_c_a_fare_increase_is_never_an_optimization_even_with_more_obc():
    """REVERSED 2026-10-01. This test previously asserted the OPPOSITE and
    called it "CONFIRMED INTENTIONAL DESIGN, not a bug (2026-08-13 audit)":
    a fare increase could be reported OPTIMIZATION when a larger OBC gain
    offset it, because net_saving = price_drop + obc_change with no sign
    check on price_drop.

    It shipped, and on 2026-10-01 booking 3001014 reached the GUI as a
    GREEN OPTIMIZATION row reading `old 9,236.94 -> new 9,483.94` and
    "optimized $53", on a booking whose bill goes UP $247. Neon: "THIS
    BOOKING IS SHOWING A HIGHER PRICE AND IT IS GREEN ... SUCH MISTAKE CAN
    NEVER HAPPEN AGAIN IN OUR PROJECT".

    The arithmetic was never wrong. The rule was: OBC is not cash. Onboard
    credit cannot pay the invoice, is onboard-only, and is commonly
    use-it-or-lose-it and non-refundable. Paying real money for it is a
    judgement call and must never be auto-labelled a win.

    Being an "explicit, visible contract" did not make it correct - it only
    meant the wrong behaviour had a test defending it.
    """
    raw = _espresso_raw(old_total=1282.46, new_total=1332.46, old_obc=0.0, new_obc=200.0)
    r = calculate_espresso(raw, "BOOK3B")
    assert r.status != BookingStatus.OPTIMIZATION
    assert r.status == BookingStatus.NO_SAVING
    assert r.new_total > r.old_total        # the client genuinely pays more
    # The trade is still spelled out, not hidden - a human may want the OBC.
    assert "INCREASES" in r.note and "OBC is not cash" in r.note


# ── Cases F/G: independent levers (fare vs. OBC/package) ─────────


def test_case_g_package_loss_only_no_price_change_is_no_saving_not_trap():
    raw = _espresso_raw(
        old_total=1000.0, new_total=1000.0,
        old_pkgs=[{"name": "All-Inclusive Drink Package", "amount": 594.0}],
        new_pkgs=[],
    )
    r = calculate_espresso(raw, "BOOK4")
    assert r.status == BookingStatus.NO_SAVING


# ── Case J: zero values ──────────────────────────────────────────


def test_case_j_all_zero_values_no_crash():
    raw = _espresso_raw(old_total=0.0, new_total=0.0, old_obc=0.0, new_obc=0.0)
    r = calculate_espresso(raw, "BOOK5")
    assert r.status == BookingStatus.NO_SAVING
    assert r.net_saving == 0.0


# ── Case L: negative values (a real price increase + OBC loss) ──


def test_case_l_price_increase_with_obc_loss_is_safe():
    raw = _espresso_raw(old_total=1000.0, new_total=1050.0, old_obc=300.0, new_obc=0.0)
    r = calculate_espresso(raw, "BOOK6")
    assert r.status == BookingStatus.NO_SAVING
    assert r.net_saving < 0


# ── Real trap examples cited in this project's own code comments ──


def test_package_trap_detection_real_example():
    """Real case from calculator.py's own comment: a $50 net saving from
    losing a $594 all-inclusive drink package is a TRAP, not a win."""
    raw = _espresso_raw(
        old_total=1644.0, new_total=1000.0,  # $644 fare drop
        old_pkgs=[{"name": "All-Inclusive Drink Package", "amount": 594.0}],
        new_pkgs=[],
    )
    r = calculate_espresso(raw, "BOOK7")
    assert r.status == BookingStatus.TRAP
    assert r.net_saving == 50.0  # confirmed positive even though it's a TRAP -- see test below


def test_obc_loss_ratio_trap_real_example():
    """Real case from calculator.py's own comment: a $300 price drop that
    costs $250 of OBC (net $50, only ~1.2x) is NOT a safe trade -- must
    clear OBC_LOSS_MIN_RATIO (3x)."""
    raw = _espresso_raw(old_total=1300.0, new_total=1000.0, old_obc=250.0, new_obc=0.0)
    r = calculate_espresso(raw, "BOOK8")
    assert r.status == BookingStatus.NO_SAVING
    assert r.net_saving == 50.0  # confirmed positive even though it's NOT a recommended saving


# ── REGRESSION: net_saving semantics + safe aggregation ──────────


def test_regression_net_saving_can_be_positive_on_trap_and_no_saving():
    """Documents the CONFIRMED INTENDED semantics (2026-08-13 audit):
    net_saving is a raw signed figure, not gated to 'only when
    recommended' -- see core/models.py's field-level docstring."""
    trap = _espresso_raw(
        old_total=1644.0, new_total=1000.0,
        old_pkgs=[{"name": "Drink Package", "amount": 594.0}], new_pkgs=[],
    )
    r = calculate_espresso(trap, "T1")
    assert r.status == BookingStatus.TRAP
    assert r.net_saving > 0  # by design -- see docstring


def test_regression_total_optimization_savings_excludes_trap_and_no_saving():
    """CONFIRMED REAL BUG, fixed 2026-08-13: several summaries used to
    display TRAP/NO_SAVING's positive net_saving with "saved" language.
    total_optimization_savings() is the one safe aggregation path -- it
    must NEVER count a TRAP or NO_SAVING row's net_saving, even when
    that value is positive."""
    results = [
        BookingResult(cruise_line=CruiseLine.ESPRESSO, status=BookingStatus.OPTIMIZATION, booking_id="a", net_saving=50.0),
        BookingResult(cruise_line=CruiseLine.ESPRESSO, status=BookingStatus.TRAP, booking_id="b", net_saving=50.0),
        BookingResult(cruise_line=CruiseLine.ESPRESSO, status=BookingStatus.NO_SAVING, booking_id="c", net_saving=20.0),
        BookingResult(cruise_line=CruiseLine.ESPRESSO, status=BookingStatus.NO_SAVING, booking_id="d", net_saving=-300.0),
    ]
    assert total_optimization_savings(results) == 50.0


# ── Travel protection classification (added 2026-08-25) ──────────
#
# Backed by real data: mining all 532 captured ESPRESSO invoice responses
# (data/raw_responses.jsonl) found exactly one real travel-protection-
# shaped line item, "GRP TVL PRTC" (64 occurrences) — previously
# indistinguishable from an ordinary lost package. See
# core/calculator.py's _is_travel_protection and core/models.py's
# lost_travel_protection field docstrings.


def test_real_example_grp_tvl_prtc_is_classified_as_travel_protection_not_a_package():
    raw = _espresso_raw(
        old_total=1000.0, new_total=1000.0,
        old_pkgs=[{"name": "GRP TVL PRTC", "amount": 64.0}],
        new_pkgs=[],
    )
    r = calculate_espresso(raw, "TVLPRTC1")
    assert r.lost_travel_protection == ["GRP TVL PRTC ($64.00)"]
    assert r.lost_pkg_names == []  # not double-counted as an ordinary package
    assert "TRAVEL PROTECTION" in r.note
    assert r.lost_pkg_value == 64.0  # financial math unchanged


def test_ordinary_package_loss_is_not_misclassified_as_travel_protection():
    raw = _espresso_raw(
        old_total=1000.0, new_total=1000.0,
        old_pkgs=[{"name": "All-Inclusive Drink Package", "amount": 594.0}],
        new_pkgs=[],
    )
    r = calculate_espresso(raw, "TVLPRTC2")
    assert r.lost_travel_protection == []
    assert r.lost_pkg_names == ["All-Inclusive Drink Package"]
    assert "TRAVEL PROTECTION" not in r.note


def test_mixed_lost_package_and_travel_protection_both_reported_separately():
    raw = _espresso_raw(
        old_total=1000.0, new_total=1000.0,
        old_pkgs=[
            {"name": "All-Inclusive Drink Package", "amount": 594.0},
            {"name": "GRP TVL PRTC", "amount": 64.0},
        ],
        new_pkgs=[],
    )
    r = calculate_espresso(raw, "TVLPRTC3")
    assert r.lost_pkg_names == ["All-Inclusive Drink Package"]
    assert r.lost_travel_protection == ["GRP TVL PRTC ($64.00)"]
    assert r.lost_pkg_value == 658.0  # both still contribute to the same total


# ── REGRESSION: round2() precision, ROUND_HALF_UP ─────────────────


def test_regression_round2_normal_values_unchanged():
    """These must round exactly the same as the OLD round(x*100)/100
    implementation -- real scraped dollar amounts must never change."""
    for value in (1332.46, 1282.46, 50.00, 37.50, 0.01, 0.0, -50.0):
        assert round2(value) == round(float(value) * 100) / 100


def test_regression_round2_fixes_float_representation_tie_bug():
    """CONFIRMED REAL BUG, fixed 2026-08-13: the old implementation
    returned round2(1.005) == 1.0 (wrong) because 1.005*100 == 100.49999999999999
    in binary float. Decimal(str(x)) avoids this."""
    assert round2(1.005) == 1.01
    assert round2(0.005) == 0.01
    assert round2(594.005) == 594.01


def test_regression_round2_half_up_not_banker_rounding():
    """CONFIRMED INTENDED CONVENTION (matches the original calculator.js
    Math.round, and Excel/Google Sheets ROUND): ties round away from
    zero, not to the nearest even number."""
    assert round2(2.675) == 2.68
    assert round2(10.125) == 10.13
    assert round2(10.135) == 10.14


# ── NCL: Cases A-C, J, L via calculate_ncl ─────────────────────────


def test_ncl_price_reduction_is_optimization():
    r = calculate_ncl("N1", "BR", invoice_total=1000.0, new_res_total=900.0)
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 100.0


def test_ncl_price_increase_is_not_optimization():
    r = calculate_ncl("N2", "BR", invoice_total=900.0, new_res_total=1000.0)
    assert r.status != BookingStatus.OPTIMIZATION


def test_ncl_zero_values_no_crash():
    r = calculate_ncl("N3", "BR", invoice_total=0.0, new_res_total=0.0)
    assert r.status == BookingStatus.NO_SAVING
