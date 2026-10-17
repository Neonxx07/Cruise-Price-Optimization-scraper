"""Princess (POLAR Online) — is a refare a real saving? Pure decision logic.

Built 2026-10-15, before the scraper, from figures that close exactly in
Neon's recordings and screenshots. The scraper (blocked on a DevTools HAR -
no positional selectors next to CANCEL BOOKING) will only have to fill in
`PrincessBooking` and `PrincessQuote`; every money rule lives here and is
tested now.

WHERE THE NUMBERS COME FROM
  * PrincessBooking - the booking's own landing page (Booking Confirmation,
    RCBD): CRUISE FARE INFORMATION and PAYMENT HISTORY. Read-only, no wizard.
    MN5VMM: Fare 2,862.00 = Gross 2,862.00, Base 2,062.00, fees 540.76 +
    taxes 259.24 = 800.00 = Fare - Base; Gross Payment Due 2,662.00 after
    200.00 received.
  * PrincessQuote - PRICING DETAIL (RDLE), the itemised quote for ONE promo
    and ONE category, reached after CATEGORY FARE COMPARISONS. Per passenger:
      DEMO33 KKS DF4: Fare 3,405.00, Base 2,997.00, fees 213.03, taxes
        194.97 (3,405 - 2,997 = 408 = 213.03 + 194.97); Gross 6,810.00.
      DEMO32 KKS IC: Fare 1,329.00 + Transfers/Packages 88.00 = Gross
        1,417.00 (2,834.00 for two); 1,329 - 979 = 350 = 170.22 + 179.78;
        Onboard Credit 75.00 pp.
  * NEVER the "Category Fares - Best Buy" prices: that page marks them
    "* = Price net of On Board Credit" - onboard credit taken off, so they
    are not cash.

THE RULES (each one is a reason a refare can be refused)
  1. Same currency.
  2. Same category - the booking's own (Neon 2026-10-15 on Carnival: "you
     must find the same category").
  3. Same package tier - Standard / Plus / Premier / Sanctuary
     (core/princess_packages.fares_are_comparable). Unknown on either side
     refuses: an unrecognised promo is not a Standard fare.
  4. Cash decides: GROSS to GROSS. Onboard credit is never cash (rule 12);
     a quote with LESS onboard credit than the booking must clear the 3x
     rule like every other line.
  5. Nothing may be lost: a quote without the Transfers/Packages the booking
     already has is a loss, not a saving. A quote that ADDS them is compared
     on gross anyway (conservative: the added cost is inside the new gross).
  6. The payment schedule may not move against the client: a final payment
     date earlier than today's, or a deposit date earlier than the booking's,
     refuses (the GoCCL rule; Neon to confirm for Princess).
  7. A quote that does not add up is a misparse, never a saving.
  8. Commission is never read (rule 9).
  9. Paid in full: Gross Payment Due 0 - never repriced, always reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from core.calculator import OBC_LOSS_MIN_RATIO
from core.payment_schedule_rule import check_payment_schedule
from core.princess_packages import fares_are_comparable

_TOL = 0.01


def parse_polar_date(text: str | None) -> date | None:
    """POLAR writes dates as 15OCT26 / 01MAY27. None if not that shape."""
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d{2})([A-Za-z]{3})(\d{2})\s*", text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}{m.group(2).title()}{m.group(3)}", "%d%b%y").date()
    except ValueError:
        return None


def polar_category(row_code: str | None) -> str:
    """The category a CATEGORY FARES row stands for: its first two characters.

    Princess's own page script, read from Neon's DEMO12 HAR 2026-10-15:
        productVal = trim(product.substring(0,2));
        <input type="radio" name="CATEGORY_SELECTION" value="'+productVal+'">
    so the row shown "B43 Balcony" submits B4, "DF4" submits DF. Neon's own
    method is the same: Ctrl+F the booking's category and take the row that
    BEGINS with it."""
    return (row_code or "").strip()[:2].strip().upper()


def find_category_row(row_codes: list[str], booking_category: str) -> tuple[str | None, str]:
    """The ONE row for the booking's category, or None and why.

    Never a guess: no row, or two rows sharing the same first two characters,
    refuses. (DEMO12's 35 rows have unique prefixes; that is an observation
    about one sailing, not a rule.)"""
    want = (booking_category or "").strip().upper()
    if not want:
        return None, "the booking's category could not be read"
    hits = [r for r in row_codes if polar_category(r) == want]
    if not hits:
        return None, f"category {want} is not offered on this sailing's list"
    if len(hits) > 1:
        return None, (f"{len(hits)} rows begin with {want} ({', '.join(h.strip() for h in hits)}) "
                      "- which one is the booking's cabin cannot be told")
    return hits[0], ""


#: POLAR's own offer-code help page (onesourcecruises.com/html/tapres/
#: coupon_code_content.html, read 2026-10-15): codes entered "to upgrade the
#: fare in your booking". "Uppers" are the 3rd/4th-guest versions.
_PREMIER_OFFER_CODES = frozenset({"PREMIER", "PREMUP", "PLS2PREM", "OLDPLS2PRM"})
_PLUS_OFFER_CODES = frozenset({"PLUS2", "PLUSUP"})


def booking_tier(offer_codes: list[str] | None = None,
                 special_requests: list[str] | None = None,
                 promo_descriptions: list[str] | None = None) -> str | None:
    """Which package the booking holds - Neon 2026-10-15: "if it is a plus we
    choose the plus rate, if it is premier choose the premier rate, standard i
    choose the standard rate". He finds it in the special notes / the offer.

    Three places say so, strongest first; the highest package found wins:
      * Special Requests / Gifts lines "Princess Premier" / "Princess Plus"
        (DEMO12: "Princess Plus" 1,120.00 x 2, inside the fare - Princess:
        packages "are included in your total cruise fare when selected").
      * The Booking Information offer code: PLUS2/PLUSUP (Standard to Plus),
        PREMIER/PREMUP/PLS2PREM/OLDPLS2PRM (to Premier). DEMO12: PLUS2.
      * Promo descriptions: "... - PPLUS", "... - PREMIER", "UPGRADE STANDARD
        TO PLUS 70" (DEMO12's M1E), "... - STANDARD".
    None when nothing says - unknown is never assumed Standard."""
    from core.princess_packages import PLUS, PREMIER, SANCTUARY, STANDARD, package_tier

    found: set[str] = set()
    for line in special_requests or []:
        text = (line or "").upper()
        if "PRINCESS PREMIER" in text:
            found.add(PREMIER)
        elif "PRINCESS PLUS" in text:
            found.add(PLUS)
    for code in offer_codes or []:
        code = (code or "").strip().upper()
        if code in _PREMIER_OFFER_CODES:
            found.add(PREMIER)
        elif code in _PLUS_OFFER_CODES:
            found.add(PLUS)
    for desc in promo_descriptions or []:
        tier = package_tier(desc)
        if tier:
            found.add(tier)
    for tier in (SANCTUARY, PREMIER, PLUS, STANDARD):
        if tier in found:
            return tier
    return None


@dataclass(frozen=True)
class PrincessBooking:
    """The booking as it stands - its landing page."""

    booking_id: str
    gross: float                       #: CRUISE FARE INFORMATION "Gross" (all guests)
    category: str
    currency: str
    tier: str | None = None            #: package tier of the CURRENT promo
    packages: float = 0.0              #: "Packages" line (all guests)
    #: "Non-Cruise Gov't Taxes & Fees" (all guests) - taxes on packages /
    #: transfers, a line of its own on the landing page (2026-10-16).
    non_cruise_taxes: float = 0.0
    obc: float = 0.0                   #: "Onboard Credit" (all guests)
    gross_payment_due: float | None = None
    final_due: date | None = None
    deposit_due: date | None = None
    vacation_protection: float = 0.0   #: "Vacation Protection" line (all guests)
    #: CRUISE FARE INFORMATION "Fare" (all guests, fees and taxes inside).
    #: Gross is NOT the fare - live 2026-10-15 it also carried Air (DEMO27
    #: 1,433.22; DEMO31 6,685.28), Mods (96.00 on the Coral/Island bookings)
    #: and Services (DEMO25 1,031.76), none of which a fare change touches.
    fare: float | None = None

    @property
    def cruise_total(self) -> float:
        """What a refare can change: Fare + Packages (transfers) + the
        non-cruise taxes on them. Gross only when the Fare line was not read."""
        if self.fare is None:
            return self.gross
        return round(self.fare + self.packages + self.non_cruise_taxes, 2)


@dataclass(frozen=True)
class PrincessQuote:
    """One PRICING DETAIL quote. Per-passenger lines are lists, one per guest."""

    promo_code: str
    promo_description: str
    category: str
    currency: str
    tier: str | None
    fare_pp: list[float]
    gross_total: float                 #: "Gross Fare" Total column
    base_pp: list[float] = field(default_factory=list)
    fees_pp: list[float] = field(default_factory=list)
    taxes_pp: list[float] = field(default_factory=list)
    transfers_pp: list[float] = field(default_factory=list)
    obc_pp: list[float] = field(default_factory=list)
    final_due: date | None = None
    deposit_due: date | None = None
    vacation_protection_pp: list[float] = field(default_factory=list)
    #: "Mods" per guest - inside Gross Fare, outside the fare (DEMO24 KLS
    #: 2026-10-15: Fare 4,237 + Mods 48 = Gross Fare 4,285 per guest).
    mods_pp: list[float] = field(default_factory=list)
    #: "Non-Cruise Gov't Taxes & Fees" per guest - taxes on transfers /
    #: packages, INSIDE Gross Fare (DEMO28 2026-10-16: 5.00 per guest on 93.00
    #: of transfers; 5 quotes failed to add up by exactly this).
    non_cruise_taxes_pp: list[float] = field(default_factory=list)

    @property
    def vacation_protection(self) -> float:
        return round(sum(self.vacation_protection_pp), 2)

    @property
    def obc(self) -> float:
        return round(sum(self.obc_pp), 2)

    @property
    def transfers(self) -> float:
        return round(sum(self.transfers_pp), 2)

    @property
    def cruise_total(self) -> float:
        """Fare + Transfers/Packages + their non-cruise taxes - the same scope
        as PrincessBooking's."""
        return round(sum(self.fare_pp) + self.transfers + sum(self.non_cruise_taxes_pp), 2)

    def reconciles(self) -> tuple[bool, str]:
        """The quote's own arithmetic, as it closed on DEMO33 and DEMO32."""
        n = len(self.fare_pp)
        if not n:
            return False, "no per-passenger fare on the quote"
        gross = (sum(self.fare_pp) + sum(self.transfers_pp) + sum(self.mods_pp)
                 + sum(self.vacation_protection_pp) + sum(self.non_cruise_taxes_pp))
        if abs(round(gross, 2) - self.gross_total) > _TOL:
            return False, (f"fare + transfers + mods + vacation protection + non-cruise taxes "
                           f"sum to {gross:,.2f} "
                           f"but Gross Fare total reads {self.gross_total:,.2f}")
        if self.base_pp and self.fees_pp and self.taxes_pp:
            for i, (f, b, fe, t) in enumerate(zip(self.fare_pp, self.base_pp,
                                                  self.fees_pp, self.taxes_pp), 1):
                if abs(round(f - b, 2) - round(fe + t, 2)) > _TOL:
                    return False, (f"guest {i}: fare {f:,.2f} - base {b:,.2f} = "
                                   f"{f - b:,.2f}, but fees + taxes = {fe + t:,.2f}")
        return True, ""


@dataclass(frozen=True)
class PrincessVerdict:
    status: str                        #: OPTIMIZATION / NO_SAVING / PAID_IN_FULL / PAST_FINAL_PAYMENT / ERROR
    saving: float
    note: str


def decide(booking: PrincessBooking, quote: PrincessQuote,
           today: date | None = None) -> PrincessVerdict:
    """Is moving `booking` to `quote` a real saving? Every refusal says why."""
    def no(reason: str, status: str = "NO_SAVING") -> PrincessVerdict:
        return PrincessVerdict(status, 0.0, f"no saving - {reason}")

    if booking.gross_payment_due is not None and booking.gross_payment_due <= _TOL:
        return PrincessVerdict("PAID_IN_FULL", 0.0, "paid in full - never repriced")
    # Princess's own notice, live 2026-10-15, on every booking past final
    # payment: "Any changes to your booking that result in a balance due must
    # be paid in full at the time you made the change. Failure to do so will
    # result in your booking being automatically cancelled".
    today = today or date.today()
    if booking.final_due and booking.final_due < today:
        return PrincessVerdict("PAST_FINAL_PAYMENT", 0.0,
                               f"past final payment ({booking.final_due}) - never repriced: a "
                               "change leaving a balance is cancelled automatically if unpaid")
    if booking.currency != quote.currency:
        return no(f"currency {quote.currency} vs the booking's {booking.currency}")
    if polar_category(quote.category) != polar_category(booking.category):
        return no(f"category {quote.category} is not the booking's own "
                  f"{booking.category}")
    ok, why = fares_are_comparable(booking.tier, quote.tier)
    if not ok:
        return no(why)
    ok, why = quote.reconciles()
    if not ok:
        return PrincessVerdict("ERROR", 0.0, f"quote does not add up - {why}")
    # POLAR's Pricing Detail help: "Standard coverage is usually added
    # automatically". A quote carrying different cover is a different product,
    # so its gross cannot be set against the booking's.
    if abs(quote.vacation_protection - booking.vacation_protection) > _TOL:
        return no(f"vacation protection differs - {quote.vacation_protection:,.2f} on "
                  f"the quote vs {booking.vacation_protection:,.2f} on the booking")
    if booking.packages > _TOL and quote.transfers + _TOL < booking.packages:
        return no(f"the quote drops packages/transfers worth "
                  f"{booking.packages - quote.transfers:,.2f} the booking has - "
                  "nothing may be lost")
    # PAYMENT DATES ON PRINCESS ARE A WATCH ITEM, NOT A BLOCK. Neon
    # 2026-10-15: "there is payment dates before at the original booking and
    # before you save but it changes sometimes however it is a thing we keep
    # an eye on but it is not as madnatory as carnival". The same rule
    # function as GoCCL (core/payment_schedule_rule.py) is run, but a change
    # is WRITTEN INTO THE NOTE, never turned into NO_SAVING. POLAR's Pricing
    # Detail shows no dates, so usually the note is the reminder to look.
    # A deposit date already behind us is paid, not pending.
    def pending(d: date | None) -> date | None:
        return d if d and d >= today else None
    sched = check_payment_schedule(
        booking.final_due, quote.final_due,
        pending(booking.deposit_due), pending(quote.deposit_due),
        deposit_on_new=bool(pending(quote.deposit_due)) or None,
        deposit_on_orig=bool(pending(booking.deposit_due)))

    cash = round(booking.cruise_total - quote.cruise_total, 2)
    obc_change = round(quote.obc - booking.obc, 2)
    figures = (f"{quote.promo_code} ({quote.promo_description}) fare+packages "
               f"{quote.cruise_total:,.2f} vs {booking.cruise_total:,.2f} now")
    if cash <= _TOL:
        extra = (f"; it carries {obc_change:,.2f} more onboard credit, which is "
                 "not cash" if obc_change > _TOL else "")
        return no(f"{figures}{extra}")
    if obc_change < -_TOL and cash < abs(obc_change) * OBC_LOSS_MIN_RATIO:
        return no(f"{figures}: a {cash:,.2f} cash drop gives up "
                  f"{abs(obc_change):,.2f} onboard credit (need "
                  f"{OBC_LOSS_MIN_RATIO:.0f}x)")
    notes = [f"saving {cash:,.2f} - {figures}"]
    if obc_change < -_TOL:
        notes.append(f"gives up {abs(obc_change):,.2f} onboard credit (cash saving clears "
                     f"the {OBC_LOSS_MIN_RATIO:.0f}x rule)")
    if obc_change > _TOL:
        notes.append(f"plus {obc_change:,.2f} onboard credit (not counted - not cash)")
    if quote.transfers > booking.packages + _TOL:
        notes.append(f"the quote adds transfers/packages of "
                     f"{quote.transfers - booking.packages:,.2f}, already inside "
                     "its gross - check they are wanted")
    if sched.allowed is False:
        notes.insert(0, f"WATCH PAYMENT DATES - {sched.reason}")
    elif sched.final_moved_later:
        notes.append(sched.reason)
    elif sched.allowed is None:
        notes.append("check the payment dates on the screen before Save "
                     "(POLAR's quote shows none)")
    return PrincessVerdict("OPTIMIZATION", cash, "; ".join(notes))
