"""NCL OBC rule — two CONFIRMED false positives reported by Neon.

Both came back GREEN as OPTIMIZATION at confidence 5/5 in the live
2026-08-27 run:

    3000055   $1,817.60 -> $1,760.60   "$57 saving"   LOST a $100 OBC cert
    3000054   $2,257.00 -> $2,197.00   "$60 saving"   LOST a $50  OBC cert

3000055 is really a $43 net LOSS. 3000054 is a 1.2x margin against the
OBC given up — below the project's OBC_LOSS_MIN_RATIO of 3x.

ROOT CAUSE: OBC loss was inferred from a PROMO SUBSTRING —
    lost_fobc = "FOBC" in old_promos and "FOBC" not in new_promos
— and the certificate was priced only if that fired. Neither booking's
promo string contained "FOBC", so lost_addon_value stayed 0.00 and the
whole price drop counted as clean net. The scraper's own before/after addon
diff had already found the lost certificate and put it in the note, where
it influenced nothing.

Two bookings Neon confirmed as CORRECT optimizations are pinned too, so
the fix cannot be "reject everything":

    3000004   $3,388.00 -> $3,348.00   $40   nothing lost
    3000050   $8,085.98 -> $7,974.08   $112  nothing lost
"""
import pytest

from core.calculator import (
    OBC_LOSS_MIN_RATIO,
    calculate_ncl,
    ncl_lost_addons,
    ncl_price_lost_addons,
)
from core.models import BookingStatus


def _addons(guest: str, *names: str) -> list[dict]:
    return [{"guest": guest, "name": n, "qty": 1} for n in names]


# ── the four real bookings ───────────────────────────────────────


def test_3000055_is_a_trap_not_an_optimization():
    """$57 drop, $100 OBC forfeited = a $43 LOSS. Was GREEN at 5/5."""
    r = calculate_ncl(
        "3000055", "BF", 1817.60, 1760.60,
        _addons("MS RHONDA JEAN RICH",
                "Excursion Credit",
                "Free $100 On-Board Credit Certificate Non -Refundable",
                "Specialty Dining: 3 Meals",
                "Unlimited Open Bar Package"),
        "", "", new_addons=[],
    )
    assert r.status == BookingStatus.TRAP
    assert r.status != BookingStatus.OPTIMIZATION
    assert r.net_saving == -43.0
    assert r.obc_change == -100.0
    assert "100" in r.note


def test_3000054_fails_the_obc_ratio():
    """$60 drop that forfeits $50 OBC is 1.2x — under the 3x rule."""
    r = calculate_ncl(
        "3000054", "BF", 2257.00, 2197.00,
        _addons("MR EDWIN LYNN SCOTT",
                "Free $50 On-Board Credit Certificate Non -Refundable",
                "Unlimited Open Bar Package",
                "Wi-Fi Package: 150 mins"),
        "", "", new_addons=[],
    )
    assert r.status == BookingStatus.NO_SAVING
    assert r.status != BookingStatus.OPTIMIZATION
    assert r.obc_change == -50.0
    assert f"{OBC_LOSS_MIN_RATIO:.0f}x" in r.note


def test_3000004_stays_a_real_optimization():
    """Neon confirmed this one is correct — the fix must not over-reject."""
    r = calculate_ncl("3000004", "IA", 3388.00, 3348.00, [], "", "", new_addons=[])
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 40.0
    assert r.confidence == 5


def test_3000050_stays_a_real_optimization():
    r = calculate_ncl("3000050", "BF", 8085.98, 7974.08, [], "", "", new_addons=[])
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == pytest.approx(111.90)
    assert r.confidence == 5


# ── confidence must not rank a trap like a win ───────────────────


@pytest.mark.parametrize("old_t,new_t,cert", [
    (1817.60, 1760.60, "Free $100 On-Board Credit Certificate Non -Refundable"),
    (2257.00, 2197.00, "Free $50 On-Board Credit Certificate Non -Refundable"),
])
def test_obc_driven_rejections_score_low_confidence(old_t, new_t, cert):
    """Both bookings originally scored 5/5 while being TRAP/NO_SAVING,
    because OBC loss lives in obc_change and the confidence arms only
    looked at lost_addon_value. A 5/5 "don't do this" sorts next to genuine
    opportunities in any confidence-ranked report."""
    r = calculate_ncl("X", "BF", old_t, new_t, _addons("A", cert), "", "", new_addons=[])
    assert r.status in (BookingStatus.TRAP, BookingStatus.NO_SAVING)
    assert r.confidence <= 2, f"a rejection scored {r.confidence}"


def test_a_big_drop_clearing_the_ratio_is_still_an_optimization():
    """The rule is a RATIO, not a ban on losing OBC: a $400 drop that costs
    $100 of OBC is 4x and should pass."""
    r = calculate_ncl(
        "Y", "BF", 2000.00, 1600.00,
        _addons("A", "Free $100 On-Board Credit Certificate Non -Refundable"),
        "", "", new_addons=[],
    )
    assert r.status == BookingStatus.OPTIMIZATION
    assert r.net_saving == 300.0


def test_exactly_at_the_ratio_boundary_passes():
    """price_drop == 3 x OBC must NOT be rejected — the rule is `<`."""
    r = calculate_ncl(
        "Z", "BF", 1300.00, 1000.00,
        _addons("A", "Free $100 On-Board Credit Certificate Non -Refundable"),
        "", "", new_addons=[],
    )
    assert r.status == BookingStatus.OPTIMIZATION


# ── the diff itself ──────────────────────────────────────────────


def test_only_addons_actually_gone_count_as_lost():
    before = _addons("A", "Free $100 On-Board Credit Certificate", "Wi-Fi Package: 150 mins")
    after = _addons("A", "Wi-Fi Package: 150 mins")
    lost = ncl_lost_addons(before, after)
    assert len(lost) == 1
    assert "100" in lost[0]["name"]


def test_nothing_lost_when_addons_are_unchanged():
    same = _addons("A", "Free $100 On-Board Credit Certificate")
    assert ncl_lost_addons(same, list(same)) == []


def test_per_guest_losses_are_counted_separately():
    """The same perk legitimately appears once per guest; a name-only key
    would collapse a two-guest loss into one."""
    before = (_addons("GUEST ONE", "Free $100 On-Board Credit Certificate")
              + _addons("GUEST TWO", "Free $100 On-Board Credit Certificate"))
    priced = ncl_price_lost_addons(ncl_lost_addons(before, []))
    assert priced["obc_value"] == 200.0


def test_estimated_values_are_never_priced_into_the_verdict():
    """"Unlimited Open Bar Package" has no dollar figure in its name, so
    _ncl_addon_value falls back to a heuristic ESTIMATE table. Estimates
    must not drive a money decision — that is the direct lesson of the
    free-upgrade false-positive incident. They are named instead."""
    priced = ncl_price_lost_addons(
        ncl_lost_addons(_addons("A", "Unlimited Open Bar Package"), [])
    )
    assert priced["obc_value"] == 0.0
    assert priced["priced_value"] == 0.0
    assert any("Unlimited Open Bar" in n for n in priced["unpriced_names"])


def test_an_optimization_that_loses_an_unpriced_perk_says_so():
    """It stays an OPTIMIZATION (we cannot prove a dollar loss) but must not
    present an unqualified win."""
    r = calculate_ncl(
        "W", "BF", 2000.00, 1600.00,
        _addons("A", "Unlimited Open Bar Package"), "", "", new_addons=[],
    )
    assert r.status == BookingStatus.OPTIMIZATION
    assert "value not readable" in r.note
    assert "Unlimited Open Bar" in r.note


def test_real_dollar_value_in_a_non_obc_perk_is_priced():
    """A perk naming its own price IS trustworthy, OBC or not."""
    priced = ncl_price_lost_addons(
        ncl_lost_addons(_addons("A", "$149.99 Beverage Package"), [])
    )
    assert priced["priced_value"] == 149.99


# ── the old promo-substring path must be gone ────────────────────


def test_obc_loss_is_detected_without_any_fobc_promo_string():
    """THE root cause. Neither real booking's promo string contained
    "FOBC", which is exactly why the loss went unpriced. Detection must now
    come from the addon diff alone."""
    r = calculate_ncl(
        "NOFOBC", "BF", 1817.60, 1760.60,
        _addons("A", "Free $100 On-Board Credit Certificate Non -Refundable"),
        old_promos="SOMEPROMO,OTHER", new_promos="SOMEPROMO,OTHER",
        new_addons=[],
    )
    assert r.obc_change == -100.0
    assert r.status == BookingStatus.TRAP


def test_no_after_list_means_no_loss_is_invented():
    """The no-price-change and price-increase early returns pass no
    `new_addons` — the booking was never touched, so nothing CAN have been
    lost. Must not price the before-list as if it were all forfeited."""
    r = calculate_ncl(
        "NOAFTER", "BF", 1817.60, 1817.60,
        _addons("A", "Free $100 On-Board Credit Certificate Non -Refundable"),
        "", "",
    )
    assert r.obc_change == 0.0
    assert r.lost_pkg_value == 0.0
    assert r.status == BookingStatus.NO_SAVING


def test_protected_promo_gate_still_wins_over_the_obc_rule():
    """LATRIPPLE/FREESRVC is a hard "never", checked before any status —
    it must not be displaced by the new OBC branches."""
    r = calculate_ncl(
        "PROT", "BF", 2000.00, 1600.00, [],
        old_promos="LATRIPLE,FREESRVC", new_promos="FREESRVC", new_addons=[],
    )
    assert r.status == BookingStatus.TRAP
    assert "LATRIPLE" in r.note
    assert r.confidence == 1
