"""The GoCCL review invoice: the only confirmed price Carnival gives us.

FOUND 2026-09-18 by walking booking DEMO11 to /review on the SAFE path
("Keep Same Stateroom", never selecting a different cabin). The review state
is loaded by

    GET /app/bookingengine/api/v1.0/manage/<ref>/review-changes

whose `agentInvoice.summary` carries a fully coded invoice - not text to be
scraped, but named fields:

    lineItems[]      CRUS  Cruise Rate
                     PTCH  Non-Comm Cruise Amount
                     RCFE  Required Cruise Fees & Expenses
                     GTFE  Government Taxes & Fees
                     INSU  Carnival Vacation Protection
                     PKGS  Pre/Post Packages
                     TADD  Transportation Add-On
                     CNRF  Cruise Charges          (a SUBTOTAL, isTotal=true)
    commissions[]    COMM  Commission
    perks[]          POBC  Total Onboard Credit
    summaryTotal     GRSS  Gross Amount

WHY THIS MATTERS. Until now every GoCCL candidate was an ESTIMATE:
per-person price x guest count. On DEMO11 that estimate said the new gross
was 1,542.00 when the portal's own review said 1,552.00 - a $10 error that
turned a real $22 saving into a claimed $32, overstating it by 31%. Taxes
and fees do not scale uniformly with occupancy, so the multiplication can
never be exact. This module reads the number instead of computing it.

CNRF IS NOT AN ADDEND. It is Cruise Charges, a subtotal of CRUS+PTCH+RCFE,
and carries isTotal=true. Summing every lineItem including it double-counts.
The real identities, verified to the cent on DEMO11:

    CNRF = CRUS + PTCH + RCFE          720.00 + 238.00 + 266.46 = 1,224.46
    GRSS = CNRF + GTFE + INSU + PKGS + TADD      1,224.46 + 327.54 = 1,552.00

Decimal throughout. Amounts are parsed from the API's own numeric fields and
never from rendered text, and nothing is rounded on the way in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Codes, as the portal publishes them.
CRUISE_RATE = "CRUS"
NON_COMM = "PTCH"
REQUIRED_FEES = "RCFE"
GOVT_TAXES = "GTFE"
INSURANCE = "INSU"
PACKAGES = "PKGS"
TRANSPORT = "TADD"
CRUISE_CHARGES = "CNRF"      # SUBTOTAL - never add this to the others
COMMISSION = "COMM"
ONBOARD_CREDIT = "POBC"
GROSS = "GRSS"

ADMIN_FEE = "ADMN"
NET_OBC = "NOBC"

#: Codes seen in real responses so far. NOT used to decide arithmetic - the
#: isIndented flag does that - only to report drift when an unfamiliar code
#: turns up in a sum that fails to balance.
_KNOWN_LINE_CODES = (CRUISE_CHARGES, CRUISE_RATE, NON_COMM, REQUIRED_FEES,
                     GOVT_TAXES, INSURANCE, PACKAGES, TRANSPORT, ADMIN_FEE)


def money(node: dict | None) -> Decimal | None:
    """Amount from Carnival's price object, as Decimal.

    Returns None when the field is absent - MISSING IS NOT ZERO. A real
    0.00 (POBC on a booking with no onboard credit) is a Decimal("0"), and
    the two must stay distinguishable: one means "no OBC", the other means
    "we could not read it".
    """
    if not isinstance(node, dict):
        return None
    price = node.get("price") if "price" in node else node
    if not isinstance(price, dict):
        return None
    raw = price.get("amount")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class ReviewInvoice:
    """What the portal states the booking would cost under the new rate."""
    gross: Decimal | None = None
    commission: Decimal | None = None
    obc: Decimal | None = None
    currency: str | None = None
    items: dict[str, Decimal] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    #: Codes the portal marked isIndented - components of the subtotal above
    #: them, and therefore NOT addends of the gross.
    indented: set[str] = field(default_factory=set)
    net_balance_due: Decimal | None = None
    balance_due: Decimal | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def line_codes(self) -> list[str]:
        """Invoice line codes only - commission and perks are not addends."""
        return [c for c in self.items
                if c not in (COMMISSION, ONBOARD_CREDIT, NET_OBC)]

    @property
    def cruise_rate(self) -> Decimal | None:
        return self.items.get(CRUISE_RATE)

    @property
    def cruise_charges_subtotal(self) -> Decimal | None:
        return self.items.get(CRUISE_CHARGES)

    def reconciles(self) -> tuple[bool, str]:
        """Do the parts add up to the stated total?

        A misparse must surface here rather than inside a wrong saving -
        the same reasoning as msc_invoice_components and
        princess_packages.PrincessFare.reconciles.
        """
        if self.gross is None:
            return False, "no GRSS gross amount in the review response"
        present = [c for c in self.line_codes if c not in self.indented]
        if not present:
            return False, "no priced line items to check the gross against"
        total = sum((self.items[c] for c in present), Decimal("0"))
        if total != self.gross:
            unknown = sorted(set(present) - set(_KNOWN_LINE_CODES))
            hint = f"; unrecognised codes {unknown}" if unknown else ""
            return False, (
                f"line items sum to {total} but GRSS reads {self.gross} "
                f"(difference {self.gross - total}){hint}"
            )
        # The subtotal, when present, must match its own parts.
        sub = self.items.get(CRUISE_CHARGES)
        parts = [self.items[c] for c in self.indented if c in self.items]
        if sub is not None and parts:
            got = sum(parts, Decimal("0"))
            if got != sub:
                return False, (
                    f"CNRF cruise charges reads {sub} but its indented "
                    f"components total {got}"
                )
        return True, ""


def parse_review_changes(body: dict | None) -> ReviewInvoice:
    """Parse a `manage/<ref>/review-changes` response.

    Never raises and never invents a figure: anything absent stays None and
    is recorded in `warnings`, so a caller can report UNVERIFIED rather than
    a confident wrong number.
    """
    inv = ReviewInvoice()
    if not isinstance(body, dict):
        inv.warnings.append("review response was not an object")
        return inv

    agent = body.get("agentInvoice")
    if not isinstance(agent, dict):
        inv.warnings.append("no agentInvoice in the review response")
        return inv
    summary = agent.get("summary")
    if not isinstance(summary, dict):
        inv.warnings.append("no agentInvoice.summary in the review response")
        return inv

    def collect(entries):
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code") or "").strip().upper()
            if not code:
                continue
            amount = money(entry)
            if amount is not None:
                inv.items[code] = amount
                inv.names[code] = str(entry.get("name") or "")
                # THE STRUCTURAL RULE. isIndented marks a COMPONENT of the
                # subtotal above it, so the figures that actually add up to
                # GRSS are the NON-indented ones. Verified on two bookings:
                #   DEMO11  CNRF 1224.46 + GTFE 327.54            = 1552.00
                #   TS45C7  CNRF 2736.42 + GTFE 239.58 + ADMN 100 = 3076.00
                # while the indented CRUS/PTCH/RCFE are what CNRF is made
                # of. Reading the flag instead of hardcoding a code list is
                # what let ADMN "Administrative Fee" - a code never seen
                # before - be handled without a change here.
                if entry.get("isIndented"):
                    inv.indented.add(code)
            price = entry.get("price") or {}
            if isinstance(price, dict) and price.get("currencyCode"):
                inv.currency = inv.currency or price["currencyCode"]

    collect(summary.get("lineItems"))
    collect(summary.get("commissions"))
    collect(summary.get("perks"))

    total_node = summary.get("summaryTotal")
    inv.gross = money(total_node)
    if isinstance(total_node, dict):
        code = str(total_node.get("code") or "").upper()
        if code and code != GROSS:
            inv.warnings.append(
                f"summaryTotal code is {code!r}, expected {GROSS!r} - "
                f"the review schema may have changed")
        price = total_node.get("price") or {}
        if isinstance(price, dict):
            inv.currency = inv.currency or price.get("currencyCode")
    if inv.gross is None:
        inv.warnings.append("no GRSS gross amount - price UNVERIFIED")

    inv.commission = inv.items.get(COMMISSION)
    if inv.commission is None:
        inv.warnings.append("no COMM commission line - COMMISSION_UNVERIFIED")

    inv.obc = inv.items.get(ONBOARD_CREDIT)
    if inv.obc is None:
        inv.warnings.append("no POBC onboard-credit line - OBC_UNVERIFIED")

    schedule = body.get("paymentsSchedule") or body.get("paymentSchedule") or {}
    if isinstance(schedule, dict):
        inv.net_balance_due = money({"price": schedule.get("netBalanceDue")})
        inv.balance_due = money({"price": schedule.get("balanceDue")})

    return inv


def confirmed_saving(
    original_gross: Decimal | None,
    review: ReviewInvoice,
    original_obc: Decimal | None = None,
) -> tuple[Decimal | None, str]:
    """The saving, from confirmed figures only.

    Returns (saving, reason). A None saving with a reason is the correct
    answer whenever the evidence is incomplete - "UNKNOWN must remain
    UNKNOWN". OBC is applied as a CHANGE, never inferred from the totals.
    """
    if original_gross is None:
        return None, "the booking's own gross was not read - PRICE_UNVERIFIED"
    if review.gross is None:
        return None, "the review returned no gross amount - PRICE_UNVERIFIED"

    ok, why = review.reconciles()
    if not ok:
        return None, f"the review invoice does not add up ({why}) - PRICE_CONFLICT"

    saving = original_gross - review.gross

    if original_obc is not None and review.obc is not None:
        obc_change = review.obc - original_obc
        if obc_change != 0:
            saving += obc_change
            return saving, (
                f"includes an onboard-credit change of {obc_change:+}"
            )
    elif review.obc is None:
        return saving, "OBC_UNVERIFIED - the review stated no POBC line"
    return saving, ""
