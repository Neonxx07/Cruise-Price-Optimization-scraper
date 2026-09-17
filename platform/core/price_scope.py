"""What a price actually covers — so two prices can never be compared blind.

Neon, 2026-09-02: "MAKE EVERYTHING AS ACCURATE AS HUMAN EYES".

THE OBSERVATION THIS MODULE EXISTS FOR. Almost every serious MSC defect has
been the same mistake in different clothing — comparing two numbers that do
not cover the same thing:

    club discount   today's price with NO discount  vs  a DISCOUNTED total
    occupancy       a quote for 1 guest             vs  a total for 2 guests
    multi-cabin     a quote for cabin 1             vs  a total for 2 cabins
    added services  a cruise-only quote             vs  a total with excursions

Those produced a fabricated $267.01 (booking 3000081), a fabricated
$1,929.61 (3000024), a $96.00 overstatement on 3000013's headline
$1,292.51, and — via the club-discount case — a whole run of false "no
opportunity" verdicts across 67 of 86 rate-checked bookings.

Each was fixed individually, by a separate guard, after it had already
produced a wrong number. The point of this module is to stop the NEXT one:
a bare float carries no record of what it measured, so nothing can check
compatibility. A `PriceScope` carries that record, and `scopes_comparable()`
is the single place where "may these two be subtracted?" is decided.

A human doing this by hand never makes this mistake, because they can see
that one screen says 1 guest and the other says 2. This gives the code the
same thing to look at.

DELIBERATELY NOT a replacement for the existing guards. Those stay: they
carry the specific, hard-won detail (MSC's occupancy pre-fill, the invoice
guest count, the cabin rule). This is the backstop that catches a dimension
nobody thought to write a guard for.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceScope:
    """The things that must match before two prices can be compared.

    Every field is optional and defaults to None meaning "unknown". Unknown
    never blocks a comparison on its own — that would refuse every legacy
    caller and every replayed historical record. It is recorded so the
    reason a comparison was allowed is visible, rather than assumed.
    """

    #: How many guests the price covers.
    guests: int | None = None
    #: How many cabins the price covers.
    cabins: int | None = None
    #: Whether the customer's Voyagers Club discount is inside this figure.
    includes_club_discount: bool | None = None
    #: Non-cruise charges (excursions/transfers/flights) inside this figure.
    non_cruise_charges: float | None = None
    #: Human-readable origin, used only in messages ("booking total",
    #: "today's listing card"). Never compared.
    label: str = ""

    def describe(self) -> str:
        bits = []
        if self.guests is not None:
            bits.append(f"{self.guests} guest(s)")
        if self.cabins is not None:
            bits.append(f"{self.cabins} cabin(s)")
        if self.includes_club_discount is not None:
            bits.append("club discount included" if self.includes_club_discount
                        else "no club discount")
        if self.non_cruise_charges:
            bits.append(f"${self.non_cruise_charges:,.2f} non-cruise charges")
        return f"{self.label or 'price'} ({', '.join(bits) or 'scope unknown'})"


def scopes_comparable(left: PriceScope | None,
                      right: PriceScope | None) -> tuple[bool, str]:
    """Whether two prices measure the same thing.

    Returns (True, "") when they may be compared, or (False, reason) naming
    the dimension that differs — the dimension is the useful part, because
    every past instance of this bug was diagnosed slowly from a wrong
    dollar figure rather than quickly from a named mismatch.

    Unknown (None) on either side is not a mismatch. This is a backstop, not
    a gate: refusing on absent metadata would break every existing caller
    and every replay of historical captures, and the specific guards already
    handle the cases where absence itself is disqualifying.
    """
    if left is None or right is None:
        return True, ""

    def differs(attr):
        a, b = getattr(left, attr), getattr(right, attr)
        if a is None or b is None:
            return None
        return (a, b) if a != b else None

    for attr, noun in (("guests", "guest count"), ("cabins", "cabin count")):
        pair = differs(attr)
        if pair is not None:
            return False, (
                f"{noun} MISMATCH: {left.describe()} vs {right.describe()} — "
                f"a per-{noun.split()[0]} figure compared against a whole-booking "
                f"total is how bookings 3000081 and 3000024 produced "
                f"fabricated savings"
            )

    pair = differs("includes_club_discount")
    if pair is not None:
        return False, (
            f"discount MISMATCH: {left.describe()} vs {right.describe()} — "
            f"comparing a list price against a discounted total is what made "
            f"67 of 86 rate-checked bookings look like 'no opportunity'"
        )

    # Non-cruise charges are a quantity, not a flag: any difference makes the
    # comparison wrong by exactly that amount. The caller normally backs them
    # out instead, so reaching here means it did not.
    a = left.non_cruise_charges or 0.0
    b = right.non_cruise_charges or 0.0
    if abs(a - b) > 0.01:
        return False, (
            f"non-cruise charges MISMATCH: {left.describe()} vs "
            f"{right.describe()} — the comparison is wrong by ${abs(a - b):,.2f} "
            f"(excursions/transfers/flights cannot appear in a category quote)"
        )
    return True, ""
