"""Two Princess fares are usually NOT comparable.

Neon 2026-09-16: "search online to understand standard princess plus princess
premuim undertsnad what are tthese and start to map".

POLAR's CATEGORY FARE COMPARISONS screen puts fares side by side, and the
promo descriptions on booking DEMO04 show why that is dangerous - every promo
names a PACKAGE:

    KFK/NFK  SANCTUARY     KKP/NCC  PPLUS
    KKS/NSC  STANDARD      KNK/NRE  PREMIER

Those columns are not four prices for one thing. They are prices for four
different things, and subtracting across them is the same mistake that
produced MSC's fabricated $267.01, the fabricated $1,929.61 on 3000024, and
a whole run of false "no opportunity" verdicts - four instances now of
comparing two numbers that do not cover the same thing.

Here it would be worse, because the package is bigger than the fare. DEMO04 is
FLL1-BCN1 on 28MAR27, a 14-night transatlantic on Sun Princess: Premier at
2026 Sphere-class rates is ~$1,470 per guest against a $1,253 Standard DE4
Balcony fare. "Downgrade and save" would throw away more than it recovers.
"""
import pytest

from core.princess_packages import (
    PLUS,
    PREMIER,
    SANCTUARY,
    STANDARD,
    TIERS,
    fares_are_comparable,
    is_sphere_class,
    package_tier,
    package_value,
)


# -- the eight real promos from booking DEMO04 -----------------------


@pytest.mark.parametrize("code,description,expected", [
    ("KFK", "CYBER SUMMER LTO - SANCTUARY", SANCTUARY),
    ("KKP", "CYBER SUMMER LTO - PPLUS", PLUS),
    ("KKS", "CYBER SUMMER LTO - STANDARD", STANDARD),
    ("KNK", "CYBER SUMMER LTO - PREMIER", PREMIER),
    ("NCC", "CYBER SUMMER LTO - PPLUS", PLUS),
    ("NFK", "CYBER SUMMER LTO - SANCTUARY", SANCTUARY),
    ("NRE", "CYBER SUMMER LTO - PREMIER", PREMIER),
    ("NSC", "CYBER SUMMER LTO - STANDARD", STANDARD),
])
def test_every_real_promo_maps_to_its_package(code, description, expected):
    assert package_tier(description) == expected, code


def test_the_tier_comes_from_the_DESCRIPTION_not_the_code():
    """Promo codes are per-campaign - KKS and NSC are both Standard, KKP and
    NCC both Plus, on the SAME booking. Keying on the code would be keying on
    noise."""
    assert package_tier("CYBER SUMMER LTO - STANDARD") == package_tier(
        "WINTER SALE - STANDARD")


def test_an_unrecognised_description_is_unknown_not_standard():
    """Assuming the cheapest tier would understate what a switch gives up."""
    assert package_tier("SOME NEW 2027 PROMO") is None
    assert package_tier("") is None
    assert package_tier(None) is None


# -- the comparison rule ---------------------------------------------


def test_the_real_opportunity_on_screen_is_allowed():
    """KKS vs NSC were BOTH Standard, and DE4 Balcony priced 1,253 vs 1,553 -
    a genuine $300/guest difference for identical inclusions. This is exactly
    what the scanner should find, so the guard must not block it."""
    ok, why = fares_are_comparable(STANDARD, STANDARD)
    assert ok is True and why == ""


@pytest.mark.parametrize("a,b", [
    (STANDARD, PREMIER), (STANDARD, PLUS), (PLUS, PREMIER),
    (PREMIER, SANCTUARY), (STANDARD, SANCTUARY),
])
def test_across_packages_is_refused(a, b):
    ok, why = fares_are_comparable(a, b)
    assert ok is False
    assert "not a saving" in why


def test_an_unknown_tier_refuses_rather_than_assuming():
    ok, why = fares_are_comparable(None, STANDARD)
    assert ok is False
    assert "could not be read" in why


def test_the_refusal_explains_itself_in_business_terms():
    """A message a human can act on, not just a rejection."""
    _, why = fares_are_comparable(STANDARD, PREMIER)
    assert "STANDARD" in why and "PREMIER" in why
    assert "inclusions" in why


# -- describing what a switch gives up -------------------------------


def test_sphere_class_ships_carry_the_higher_rate():
    """Sun and Star Princess price the packages above the rest of the fleet -
    $70/$105 rather than $65/$100."""
    assert is_sphere_class("SUN") and is_sphere_class("STAR")
    assert not is_sphere_class("CROWN")
    assert not is_sphere_class(None)
    assert (package_value(PREMIER, 14, "SUN").per_day
            > package_value(PREMIER, 14, "CROWN").per_day)


def test_the_package_can_exceed_the_fare_itself():
    """THE REASON THIS MODULE EXISTS. DEMO04's 14-night Sun Princess Premier
    is worth about $1,470 per guest against a $1,253 Standard DE4 Balcony
    fare - so a 'saving' from downgrading would destroy value."""
    premier = package_value(PREMIER, 14, "SUN")
    assert premier.total > 1253.00


def test_standard_has_nothing_to_lose():
    v = package_value(STANDARD, 14, "SUN")
    assert v.total is None
    assert "no package inclusions to lose" in v.note


def test_sanctuary_is_described_as_a_different_product():
    """Sanctuary is not a package on a fare - it is Sun/Star only, has Premier
    built in, and has tiny inventory (80 Signature Suites, 123 Mini Suites, 12
    Premium Deluxe Balconies), so it can never be a like-for-like swap."""
    v = package_value(SANCTUARY, 14, "SUN")
    assert v.total is None
    assert "built into the fare" in v.note
    assert "like-for-like" in v.note


def test_an_unknown_voyage_length_produces_no_figure():
    """Better to say nothing than to invent a number - the habit behind every
    fabricated saving this project has had to retract."""
    v = package_value(PREMIER, None, "SUN")
    assert v.total is None
    assert "length unknown" in v.note


def test_the_value_is_never_presented_as_money_to_net_off():
    """It describes what is given up; it must not be added to or subtracted
    from a fare. Package list rates changed for 2026 and will change again,
    and actual usage per guest is unknowable."""
    import inspect

    import core.princess_packages as mod

    doc = inspect.getdoc(mod.package_value)
    assert "DESCRIPTIVE ONLY" in doc
    assert "never" in doc


def test_the_tiers_are_ordered_least_to_most_inclusive():
    """Order is meaningful: moving down the list strips inclusions, however
    attractive the fare looks."""
    assert TIERS == (STANDARD, PLUS, PREMIER, SANCTUARY)


# -- the fare model, verified against the real panels -----------------


REAL = dict(gross=2256.00, base_fare=1996.00, ncf=812.00,
            required_fees=173.42, govt_taxes=86.58, commission=177.60)


def _fare(**over):
    from core.princess_packages import PrincessFare

    return PrincessFare(**{**REAL, "tier": STANDARD, **over})


def test_the_real_booking_reconciles_exactly():
    """Neon's CRUISE FARE INFORMATION panel: base 1,996.00 + fees 173.42 +
    taxes 86.58 = gross 2,256.00."""
    ok, why = _fare().reconciles()
    assert ok, why


def test_commission_is_charged_on_base_fare_MINUS_ncf():
    """Both stated rates resolve to the same base: 118.40/0.10 and 59.20/0.05
    are each 1,184.00, and 1,996.00 - 812.00 = 1,184.00 exactly. NCF is carved
    out of the commissionable amount the same way MSC's SRN is."""
    assert _fare().commissionable == 1184.00
    assert round(1184.00 * 0.15, 2) == 177.60


def test_net_due_is_gross_less_commission():
    """The panel states 2,078.40."""
    assert _fare().net_due == 2078.40


def test_a_capture_that_does_not_add_up_is_caught():
    """A misparse must surface here, not inside a wrong saving - the same
    reasoning as msc_invoice_components."""
    ok, why = _fare(base_fare=1500.00).reconciles()
    assert ok is False
    assert "gross reads" in why


def test_a_partial_capture_is_not_treated_as_broken():
    """Not every screen shows every line; absence is not contradiction."""
    ok, _ = _fare(required_fees=None, govt_taxes=None).reconciles()
    assert ok is True


# -- the saving, gross to gross, within one package -------------------


def test_a_same_package_saving_is_gross_to_gross():
    from core.princess_packages import princess_saving

    saving, why = princess_saving(_fare(), _fare(gross=1956.00, base_fare=1696.00))
    assert saving == 300.00 and why == ""


def test_a_cross_package_comparison_returns_no_number():
    """THE GUARD. On DEMO04 a 14-night Premier package is worth ~$1,470/guest
    against a $1,253 Standard fare, so netting across tiers would call
    destroying value a saving."""
    from core.princess_packages import PrincessFare, princess_saving

    saving, why = princess_saving(
        _fare(), PrincessFare(gross=1956.00, tier=PREMIER))
    assert saving is None
    assert "different packages" in why


def test_a_fare_that_does_not_reconcile_produces_no_saving():
    from core.princess_packages import princess_saving

    saving, why = princess_saving(_fare(), _fare(gross=1956.00, base_fare=99.00))
    assert saving is None
    assert "does not add up" in why
