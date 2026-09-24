"""The bottom of an MSC booking page: perks, shipboard credit, applied discounts.

Neon, 2026-09-22: *"OBC or perks are usually at the down of the page the
original booking page usually written investigate this with the bookings u
have."*

He was right on both counts. Across the 133 bookings in
``data/msc_control/booking_data.jsonl`` the perk lines sit a median 0.70 of
the way down the page text, and they carry a line code of their own::

    Item  Description                               ...  Net      Gross
    CAB   DELUXE BALCONY DECK 9-10                       $280.71  $338.20
    OBS   Shipboard Credit 25 USD - Non Refundable       $25.00   $25.00
    OBS   FANTASTICA EXPERIENCE BENEFITS                 $0.00    $0.00
    PCH   Taxes, Fees & Port Expenses                    $75.00   $75.00
    SRN   Non commissionable fares                       $62.70   $62.70
    Total Adult 1                                        $443.41  $500.90

``OBS`` is the perk code, and the evidence splits it cleanly in two:

**Priced OBS** (12 bookings, $4.50-$245.00) is an extra the customer *bought*,
and it is inside the stateroom total - 338.20 + 25 + 75 + 62.70 = 500.90 on
booking 3000077, exactly. This matters for repricing: a booking total that
contains $245 of purchased credit is not comparable to a cabin-only quote for
today. That is the same shape as MSC's fabricated $267.01 and GoCCL's
+$184-that-was-really-minus-$6 - two numbers that do not cover the same thing.

**Free OBS** (93 bookings) is a perk bundled with the fare and priced at
$0.00: FANTASTICA/BELLA/AUREA/SUITE EXPERIENCE BENEFITS, drinks packages,
"Shipboard credit 50 Eur/Usd - 40 Gbp". It contributes nothing to the total,
so it never distorts a comparison - but it is real value that a reprice onto a
different fare code can silently drop.

The same block ends with the discounts already ON the booking, named and
rated::

    MSC Club Discount: MSCCLUB5 - Discount Type: Percentage - Discount Rate: 5.0%
    Discount Description: VOYAGERS EXCLUSIVES - ... - Discount Rate: 9.75%

That is an authoritative "already applied" signal - better than inferring it -
and it is where ``senior_discount_applied`` comes from for the exclusivity
rule in :mod:`core.calculator_msc`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

OBS_CODE = "OBS"

# Every combination seen in the corpus is one loyalty discount plus at most one
# promotional discount. MSCCLUB5 is the loyalty slot; SPECIAL OFFER, SENIOR
# DISCOUNT and VOYAGERS EXCLUSIVES compete for the promotional slot and were
# never once observed together (0 of 133 bookings).
_LOYALTY_PATTERNS = (re.compile(r"^MSCCLUB", re.I),)
_SENIOR_PATTERN = re.compile(r"SENIOR", re.I)

_MONEY = re.compile(r"\$\s*(-?[\d,]+\.\d{2})")

# "MSC Club Discount: X - Discount Type: Percentage - Discount Rate: 5.0%" and
# "Discount Description: X - ... - Discount Rate: 9.75%". Both forms appear.
_APPLIED_DISCOUNT = re.compile(
    r"(?:MSC Club Discount|Discount Description)\s*:\s*(?P<name>[^\-\n]+?)"
    r"\s*-\s*Discount Type\s*:[^\-\n]*-\s*Discount Rate\s*:\s*(?P<rate>[\d.]+)\s*%",
    re.I,
)


def _money(text: str) -> Decimal | None:
    """Last money figure on a line - the Gross column - or None.

    None is not zero. A line we cannot read must not be scored as free.
    """
    found = _MONEY.findall(text)
    if not found:
        return None
    try:
        return Decimal(found[-1].replace(",", ""))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class ObsLine:
    """One OBS row from the per-passenger breakdown."""

    description: str
    gross: Decimal | None

    @property
    def is_priced(self) -> bool:
        """True when the customer paid for this perk, so it sits in the total."""
        return self.gross is not None and self.gross > 0

    @property
    def is_included_perk(self) -> bool:
        """True when the fare bundles it at no charge."""
        return self.gross == 0

    @property
    def is_unreadable(self) -> bool:
        return self.gross is None


@dataclass(frozen=True)
class AppliedDiscount:
    """A discount already on the booking, as MSC itself prints it."""

    name: str
    rate: float

    @property
    def is_loyalty(self) -> bool:
        """MSC Club membership - observed only ever as a 5% stacking slot."""
        return any(p.search(self.name) for p in _LOYALTY_PATTERNS)

    @property
    def is_senior(self) -> bool:
        return bool(_SENIOR_PATTERN.search(self.name))

    @property
    def is_promotional(self) -> bool:
        return not self.is_loyalty


def parse_obs_lines(breakdown_text: str | None) -> list[ObsLine]:
    """Every OBS row in the page's price breakdown, in page order.

    Rows repeat per passenger; that is MSC's own structure and is preserved
    rather than deduplicated, because a two-passenger booking really does
    carry the credit twice.
    """
    lines: list[ObsLine] = []
    for raw in (breakdown_text or "").splitlines():
        row = raw.strip()
        if not row.startswith(OBS_CODE):
            continue
        parts = row.split("\t")
        # Guard against a description that merely begins with "OBS".
        if len(parts) < 2 or parts[0].strip() != OBS_CODE:
            continue
        lines.append(ObsLine(description=parts[1].strip(), gross=_money(row)))
    return lines


def purchased_extras_total(breakdown_text: str | None) -> Decimal:
    """What the customer PAID for perks, and so what the booking total carries.

    Subtract this before comparing a booking total against a cabin-only quote
    for today. Free perks are excluded - they cost nothing and distort nothing.
    """
    return sum(
        (line.gross for line in parse_obs_lines(breakdown_text) if line.is_priced),
        Decimal("0"),
    )


def included_perks(breakdown_text: str | None) -> list[str]:
    """Perks bundled into the fare at no charge - value a reprice can drop."""
    return [line.description for line in parse_obs_lines(breakdown_text)
            if line.is_included_perk]


def parse_applied_discounts(breakdown_text: str | None) -> list[AppliedDiscount]:
    """Discounts already on the booking, named and rated by MSC."""
    return [
        AppliedDiscount(name=m.group("name").strip(), rate=float(m.group("rate")))
        for m in _APPLIED_DISCOUNT.finditer(breakdown_text or "")
    ]


def senior_discount_applied(breakdown_text: str | None) -> bool:
    """Feeds MSC-D5. Neon: SPECIAL OFFER is blocked "only when the senior
    discount is actually applied" - which this reads off the page rather than
    inferring from passenger ages.
    """
    return any(d.is_senior for d in parse_applied_discounts(breakdown_text))
