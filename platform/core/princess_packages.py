"""Princess fare packages — and why two Princess fares are usually NOT comparable.

Neon 2026-09-16: "search online to understand standard princess plus princess
premuim undertsnad what are tthese and start to map".

WHAT THE PROMO LIST IS ACTUALLY SHOWING. POLAR's FARE COMPARISONS screen lists
promos by description, and every one on booking DEMO04 ended in a package name:

    KFK  CYBER SUMMER LTO - SANCTUARY      NFK  CYBER SUMMER LTO - SANCTUARY
    KKP  CYBER SUMMER LTO - PPLUS          NCC  CYBER SUMMER LTO - PPLUS
    KKS  CYBER SUMMER LTO - STANDARD       NSC  CYBER SUMMER LTO - STANDARD
    KNK  CYBER SUMMER LTO - PREMIER        NRE  CYBER SUMMER LTO - PREMIER

So the CATEGORY FARE COMPARISONS matrix puts DIFFERENT PACKAGES side by side.
Those columns are not four prices for one thing - they are prices for four
different things.

THE TRAP THIS MODULE EXISTS TO PREVENT. Reading a Standard fare against a
Premier fare and calling the difference a "saving" is the same mistake that
produced MSC's fabricated $267.01 (a 1-guest quote against a 2-guest total),
the fabricated $1,929.61 on 3000024 (3 dropped children), and a whole run of
false "no opportunity" verdicts (an undiscounted quote against a discounted
total). Four times now, always the same shape: two numbers that do not cover
the same thing.

Here it would be worse than usual, because the package is not a rounding
error. Booking DEMO04 is FLL1-BCN1 departing 28MAR27 - a transatlantic. At
2026 rates a 14-night Premier package is $1,400 per guest, which is MORE than
the $1,253 Standard fare for its DE4 Balcony. "Downgrade to Standard and save
$1,400" would be advice to throw away more value than it recovers.

WHAT IS AND IS NOT A REAL OPPORTUNITY:
  * SAME package, different promo  -> genuinely comparable, a real saving.
    Real example from that screen: KKS and NSC are BOTH Standard, and DE4
    Balcony priced 1,253 vs 1,553 - a $300/guest difference for identical
    inclusions. That is the opportunity this scanner should find.
  * DIFFERENT package              -> not comparable on fare alone. Report it,
    never net it, and never call it a saving.

Rates below are 2026, from Princess's own package page and the 2026 update
announcements. They are DATED and WILL move - Princess raised them for 2026 -
so they are used only to describe the size of what is being given up, never to
manufacture a saving figure.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Standard fare: the cruise itself - stateroom, main dining, entertainment,
#: and the Medallion wearable. No drinks, no wifi, no crew appreciation.
STANDARD = "STANDARD"

#: Princess Plus: drinks to a $15/drink cap, ONE device of wifi, four casual
#: meals, crew appreciation, and unlimited specialty coffee/tea (which does
#: NOT count against the 15-drink daily limit).
PLUS = "PLUS"

#: Princess Premier: unlimited premium drinks, FOUR devices of wifi, unlimited
#: casual AND specialty dining, unlimited digital photos, reserved theatre
#: seating, waived OceanNow/room-service fees, crew appreciation. New for
#: 2026 it also carries a shore-excursion credit that scales with length.
PREMIER = "PREMIER"

#: Sanctuary Collection: not a package bolted onto a fare but a separate
#: product on Sun Princess and Star Princess only - Premier is BUILT INTO the
#: fare, plus an adults-only top-deck Sanctuary Club, the exclusive Sanctuary
#: Restaurant and priority dining reservations. Inventory is tiny (80
#: Signature Suites, 123 Mini Suites, 12 Premium Deluxe Balconies), so a
#: Sanctuary fare is never a like-for-like swap for an ordinary cabin.
SANCTUARY = "SANCTUARY"

#: Ordered least to most inclusive. Position is meaningful: a move DOWN this
#: list strips inclusions, however attractive the fare looks.
TIERS = (STANDARD, PLUS, PREMIER, SANCTUARY)

#: How POLAR spells each tier in a promo description. Matched on the DESCRIPTION,
#: never on the promo code - codes are per-campaign (KKS, NSC, KKP, NCC ... all
#: seen on one booking) while the description names the product.
_DESCRIPTION_MARKERS = {
    "SANCTUARY": SANCTUARY,
    "PREMIER": PREMIER,
    "PPLUS": PLUS,
    "PLUS": PLUS,
    "STANDARD": STANDARD,
}


@dataclass(frozen=True)
class PackageValue:
    """What a package is worth on one voyage, per guest."""

    tier: str
    per_day: float | None
    nights: int | None
    total: float | None
    note: str


#: 2026 per-guest-per-day rates. Sphere-class ships (Sun Princess, Star
#: Princess) are priced higher than the rest of the fleet.
_PER_DAY_2026 = {
    PLUS: {"standard": 65.0, "sphere": 70.0},
    PREMIER: {"standard": 100.0, "sphere": 105.0},
}

#: Ships on which the higher Sphere-class rate applies.
SPHERE_CLASS_SHIPS = frozenset({"SUN", "STAR"})


def package_tier(promo_description: str | None) -> str | None:
    """The package a POLAR promo description refers to, or None.

    None means "not recognised", which must be treated as unknown rather than
    assumed Standard - guessing the cheapest tier would understate what a
    switch gives up.
    """
    if not promo_description:
        return None
    text = promo_description.upper()
    # SANCTUARY and PREMIER first: a Sanctuary description also implies
    # Premier inclusions, and the more specific product must win.
    for marker, tier in _DESCRIPTION_MARKERS.items():
        if marker in text:
            return tier
    return None


def is_sphere_class(ship_code: str | None) -> bool:
    """Sun and Star Princess carry the higher package rate."""
    return bool(ship_code) and ship_code.strip().upper()[:4] in SPHERE_CLASS_SHIPS


def package_value(tier: str | None, nights: int | None,
                  ship_code: str | None = None) -> PackageValue:
    """Roughly what `tier` is worth per guest on a voyage of `nights`.

    DESCRIPTIVE ONLY. This exists to say "switching down strips about $910 of
    inclusions per guest", never to be added to or subtracted from a fare. The
    rates are published 2026 list prices, they changed for 2026 and will change
    again, and what a given guest actually uses is unknowable. Treating this as
    money would be inventing a number - the habit that produced every
    fabricated saving this project has had to retract.
    """
    if tier in (None, STANDARD):
        return PackageValue(tier or "UNKNOWN", None, nights, None,
                            "Standard fare - no package inclusions to lose")
    if tier == SANCTUARY:
        return PackageValue(
            SANCTUARY, None, nights, None,
            "Sanctuary Collection - Premier is built into the fare, plus the "
            "Sanctuary Club, Sanctuary Restaurant and priority dining. Sun and "
            "Star Princess only, and very limited inventory, so it is never a "
            "like-for-like swap for an ordinary cabin")
    rates = _PER_DAY_2026.get(tier)
    if not rates or not nights:
        return PackageValue(tier, None, nights, None,
                            f"{tier} package - voyage length unknown, so its "
                            f"value cannot be described")
    per_day = rates["sphere" if is_sphere_class(ship_code) else "standard"]
    return PackageValue(
        tier, per_day, nights, round(per_day * nights, 2),
        f"{tier} package at 2026 list rates is about ${per_day:.0f} per guest "
        f"per day, roughly ${per_day * nights:,.0f} over {nights} nights")


def fares_are_comparable(tier_a: str | None,
                         tier_b: str | None) -> tuple[bool, str]:
    """Whether two POLAR fares may be subtracted from one another.

    THE WHOLE POINT OF THIS MODULE. Same tier is a real comparison; different
    tiers are two different products and the difference is not a saving.
    Unknown on either side also refuses - an unrecognised description is not
    evidence of a match.
    """
    if tier_a is None or tier_b is None:
        return False, (
            "package tier could not be read on at least one fare, so they "
            "cannot be compared - an unrecognised promo is not a Standard fare")
    if tier_a == tier_b:
        return True, ""
    return False, (
        f"different packages: {tier_a} vs {tier_b}. The fare difference is not "
        f"a saving - it is the price of different inclusions. Compare fares "
        f"within one package, or report the switch without netting it")


# ---------------------------------------------------------------------------
# The Princess fare model — every figure verified against a real booking
# ---------------------------------------------------------------------------
# Neon supplied the CRUISE FARE INFORMATION and COMMISSION INFORMATION panels
# on 2026-09-16 (and rightly pointed out I had claimed the current fare was not
# in the screenshots after opening only 4 of 45). Every number closes exactly,
# and the two panels cross-validate against the PRICING DETAIL screen:
#
#   CRUISE FARE INFORMATION            COMMISSION INFORMATION
#     Fare                 2,256.00      Standard 10.0%      118.40
#     Base Fare            1,996.00      Override  5.0%       59.20
#     Gross                2,256.00      Total Commission    177.60
#     Req'd Cruise Fees      173.42      Net Due           2,078.40
#     Incld Gov't Taxes       86.58
#
#   Fare - Base Fare = 260.00 = 173.42 + 86.58            (fees are inside Fare)
#   118.40/0.10 = 59.20/0.05 = 1,184.00                   (one commissionable base)
#   Base Fare 1,996.00 - NCF 812.00 = 1,184.00            (NCF from PRICING DETAIL)
#   Gross 2,256.00 - Commission 177.60 = 2,078.40         (Net Due)
#
# So: GROSS is what the client pays and is the number to compare between fares.
# NCF is carved out of the commissionable base exactly as MSC's SRN is - the
# same structure under a different name.


@dataclass(frozen=True)
class PrincessFare:
    """One Princess fare, itemised as POLAR reports it."""

    gross: float                    #: what the client pays - the comparable number
    base_fare: float | None = None  #: Fare net of required fees and taxes
    ncf: float | None = None        #: non-commissionable fares, inside base_fare
    required_fees: float | None = None
    govt_taxes: float | None = None
    commission: float | None = None
    tier: str | None = None         #: package, from the promo description

    @property
    def commissionable(self) -> float | None:
        """Base fare less NCF - what commission is actually charged on."""
        if self.base_fare is None or self.ncf is None:
            return None
        return round(self.base_fare - self.ncf, 2)

    @property
    def net_due(self) -> float | None:
        """What the agency remits: gross less commission."""
        if self.commission is None:
            return None
        return round(self.gross - self.commission, 2)

    def reconciles(self, tolerance: float = 0.01) -> tuple[bool, str]:
        """Do the parts add up the way the real booking's did?

        Same idea as msc_invoice_components: a capture that does not add up is
        a misparse, and catching it here is far cheaper than discovering it in
        a wrong saving. Only checks what is present - a partial capture is not
        a failure, it is a partial capture.
        """
        if (self.base_fare is not None and self.required_fees is not None
                and self.govt_taxes is not None):
            expected = round(self.base_fare + self.required_fees + self.govt_taxes, 2)
            if abs(expected - self.gross) > tolerance:
                return False, (
                    f"base fare {self.base_fare:,.2f} + fees "
                    f"{self.required_fees:,.2f} + taxes {self.govt_taxes:,.2f} "
                    f"= {expected:,.2f}, but gross reads {self.gross:,.2f}")
        return True, ""


def princess_saving(current: PrincessFare, quoted: PrincessFare) -> tuple[float | None, str]:
    """What switching from `current` to `quoted` is worth, or why it cannot be said.

    GROSS TO GROSS, and only within one package. The package guard is the whole
    reason this function exists rather than a bare subtraction: on booking
    DEMO04 a 14-night Premier package is worth about $1,470 per guest against a
    $1,253 Standard fare, so netting across tiers would call destroying value a
    saving. That is the fifth appearance of the like-for-like mistake in this
    project and the first one caught before it shipped.
    """
    comparable, why = fares_are_comparable(current.tier, quoted.tier)
    if not comparable:
        return None, why
    for fare, label in ((current, "current"), (quoted, "quoted")):
        ok, detail = fare.reconciles()
        if not ok:
            return None, f"the {label} fare does not add up: {detail}"
    return round(current.gross - quoted.gross, 2), ""
