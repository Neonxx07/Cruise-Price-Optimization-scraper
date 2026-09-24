"""The things that actually move a cruise price - captured on every scan.

Neon 2026-09-21: "okay then implementt so from now on on any scan specially
on the gui specially we can predic moveing forward for all the cruise lines".

WHY THIS EXISTS. A drop-prediction model was built from the 5,067 rows of
price_history already collected and honestly measured:

    random cross-validation   AUC 0.780   lift 5.4x   <- optimistic
    TEMPORAL split            AUC 0.686   lift 2.6x   <- the real number
    ...minus scan-cadence features:  AUC 0.549        <- a coin flip

Almost all the apparent skill came from `age_days` and `n_obs` - features
describing HOW WE SCANNED, not how prices behave. The reason is simply that
price_history stores five columns:

    booking_id | cruise_line | total | category | checked_at

and none of the real drivers. DAYS TO SAILING is the dominant one in cruise
pricing and was not recorded anywhere; `market_data` did not rescue it
either (0 of 5,407 payloads carried sailing or itinerary data). No model can
find a pattern in a variable it cannot see.

Every field below was located in REAL captured data, per line - nothing here
is a guessed key:

    ESPRESSO   page JSON      "sailDate":"21FEB2027", "shipCode":"AL",
                              "shipName":"Allure Of The Seas"
    NCL        payload        payment.bookingFacts["Vacation Start Date"],
                              ["Vacation End Date"], ["Ship"], ["Guests"],
                              payment.finalPaymentDate
    GOCCL      initialData    ship.name/.code, category.code,
                              category.stateroomType.name, guests[],
                              rate.gbrCode, paymentSchedule.finalPaymentDueDate,
                              netBalanceDue

MISSING STAYS MISSING. Every field is Optional and defaults to None. A
booking whose sail date could not be read must NOT record a days_to_sailing
of 0 - that would be a confident lie, and the whole point of this module is
to stop the model learning from artifacts.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# "21FEB2027" - ESPRESSO
_DDMMMYYYY = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{4})$")
# "08/28/2026" - NCL bookingFacts
_MDY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_date(value) -> date | None:
    """A date from any of the shapes the three portals use, or None.

    Deliberately returns None rather than raising or guessing: a date we
    cannot read is not a date, and downstream every consumer treats None as
    "unknown" rather than as a zero.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().upper()
    if not text:
        return None
    m = _DDMMMYYYY.match(text)
    if m and m.group(2) in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    m = _MDY.match(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    # ISO, with or without a time part.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class BookingFeatures:
    """What a scan knows about a booking beyond its price."""
    sail_date: date | None = None
    days_to_sailing: int | None = None
    ship_code: str | None = None
    ship_name: str | None = None
    nights: int | None = None
    fare_code: str | None = None
    stateroom_type: str | None = None
    guests_count: int | None = None
    final_payment_date: date | None = None
    net_balance_due: float | None = None
    currency: str | None = None
    #: Where it sails - a real price driver (Caribbean in hurricane season
    #: does not behave like Alaska in July). GoCCL states it outright;
    #: the other lines do not expose it yet, so it stays None there rather
    #: than being inferred from a ship name.
    region: str | None = None
    itinerary_code: str | None = None
    embark_port: str | None = None

    def as_row(self) -> dict:
        """Flat dict for the price_history row; dates as ISO strings."""
        row = asdict(self)
        for key in ("sail_date", "final_payment_date"):
            row[key] = row[key].isoformat() if row[key] else None
        return row

    @property
    def sailing_month(self) -> int | None:
        """Season proxy - the month a cruise sails in is a real price driver."""
        return self.sail_date.month if self.sail_date else None


def _days_between(sail: date | None, observed: date | None) -> int | None:
    if sail is None:
        return None
    return (sail - (observed or date.today())).days


def _dig(node, *path):
    """Walk a nested dict safely; None the moment anything is missing."""
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def from_espresso(page_text: str | None = None, observed: date | None = None,
                  page_fields: dict | None = None) -> BookingFeatures:
    """ESPRESSO exposes these as JSON embedded in the booking page.

    Confirmed on a real capture: "sailDate":"21FEB2027", "shipCode":"AL",
    "shipName":"Allure Of The Seas". Read by regex because the surrounding
    blob is an Angular bootstrap, not a clean document-level JSON object.
    """
    f = BookingFeatures()
    if page_fields:
        # Pre-extracted in the browser by EspressoScraper.read_feature_fields
        # - the cheap path, ~200 bytes instead of a 422 KB page dump.
        def grab(key):
            v = page_fields.get(key)
            return str(v) if v not in (None, "") else None
    elif page_text:
        def grab(key):
            m = re.search(rf'"{key}"\s*:\s*"([^"]{{1,60}})"', page_text)
            return m.group(1) if m else None
    else:
        return f

    f.sail_date = parse_date(grab("sailDate") or grab("sailingDate"))
    f.ship_code = grab("shipCode")
    f.ship_name = grab("shipName")

    # WIDENED 2026-09-21 after measuring coverage: ESPRESSO is 4,500 of the
    # 5,091 price_history rows (88%) and was filling only 4 of 14 driver
    # columns - the line with the most data had the least signal. Both of
    # these were confirmed present on 12 of 12 real booking pages.
    f.currency = grab("currency")
    f.stateroom_type = grab("stateroomType")

    # DELIBERATELY NOT MAPPED: "occupancy". It is present and it varies
    # (2 and 4 across those same 12 pages), so it is tempting - but nothing
    # observed says whether it is the BOOKING'S GUEST COUNT or the CABIN'S
    # BERTH CAPACITY, and the neighbouring "totalGuests" reads the
    # unrendered template "(Pending)". Writing a berth capacity into
    # guests_count would teach a model a fabricated fact about occupancy,
    # which is precisely the class of error the whole features module exists
    # to avoid. It stays unmapped until a booking with a KNOWN guest count
    # settles which one it is.
    f.days_to_sailing = _days_between(f.sail_date, observed)
    return f


def from_ncl(market_data: dict | None, observed: date | None = None) -> BookingFeatures:
    """NCL keeps them in payment.bookingFacts, a flat label->value map."""
    f = BookingFeatures()
    facts = _dig(market_data or {}, "payment", "bookingFacts") or {}
    if not isinstance(facts, dict):
        facts = {}
    f.sail_date = parse_date(facts.get("Vacation Start Date"))
    end = parse_date(facts.get("Vacation End Date"))
    if f.sail_date and end:
        f.nights = (end - f.sail_date).days or None
    f.ship_name = (facts.get("Ship") or None)
    guests = facts.get("Guests")
    try:
        f.guests_count = int(str(guests).strip()) if guests not in (None, "") else None
    except (TypeError, ValueError):
        f.guests_count = None
    f.final_payment_date = parse_date(
        _dig(market_data or {}, "payment", "finalPaymentDate")
        or _dig(market_data or {}, "derived", "final_payment_date"))
    f.days_to_sailing = _days_between(f.sail_date, observed)
    return f


def from_goccl(initial_data: dict | None, observed: date | None = None) -> BookingFeatures:
    """GoCCL's window.initialData, mapped during the 2026-09-18 forensics."""
    f = BookingFeatures()
    d = initial_data or {}
    ship = d.get("ship") or {}
    cat = d.get("category") or {}
    rate = d.get("rate") or {}
    sched = d.get("paymentSchedule") or {}

    f.ship_code = ship.get("code") or None
    f.ship_name = ship.get("name") or None
    f.stateroom_type = _dig(cat, "stateroomType", "name")
    # The REAL offer code - rate.code is the constant sentinel "BKGRTE".
    f.fare_code = rate.get("virtualCode") or rate.get("gbrCode") or None
    guests = d.get("guests")
    f.guests_count = len(guests) if isinstance(guests, list) and guests else None
    f.currency = d.get("currencyCode") or None
    f.final_payment_date = parse_date(_dig(sched, "finalPaymentDueDate", "rawValue"))
    nb = _dig(sched, "netBalanceDue", "amount")
    f.net_balance_due = float(nb) if isinstance(nb, (int, float)) else None

    itin = d.get("itinerary") or {}
    if isinstance(itin, dict):
        # CONFIRMED SHAPE, 2026-09-21. The sail date is a nested money-style
        # object, not a bare string: itinerary.departure.rawValue =
        # "2026-11-21T15:30:00-06:00". A first version guessed
        # sailDate/startDate/departureDate and got None on every booking -
        # exactly the silent gap this module exists to close, so the key is
        # read from a real capture instead.
        f.sail_date = parse_date(
            _dig(itin, "departure", "rawValue")
            or _dig(itin, "departure", "shortDate")
            or itin.get("sailDate") or itin.get("startDate"))
        nights = itin.get("duration") or itin.get("nights")
        try:
            f.nights = int(nights) if nights is not None else None
        except (TypeError, ValueError):
            f.nights = None
        f.region = itin.get("destinationName") or None
        f.itinerary_code = itin.get("itineraryCode") or None
        f.embark_port = itin.get("embarkationPortName") or None
        f.ship_code = f.ship_code or itin.get("shipCode") or None
        f.ship_name = f.ship_name or itin.get("shipName") or None
    f.days_to_sailing = _days_between(f.sail_date, observed)
    return f


def extract(cruise_line: str, *, page_text: str | None = None,
            market_data: dict | None = None, initial_data: dict | None = None,
            page_fields: dict | None = None,
            observed: date | None = None) -> BookingFeatures:
    """Per-line dispatch. An unknown line yields an empty (all-None) record
    rather than an error - a new cruise line must not break a scan."""
    line = (cruise_line or "").upper()
    if line == "ESPRESSO":
        return from_espresso(page_text, observed, page_fields=page_fields)
    if line == "NCL":
        return from_ncl(market_data, observed)
    if line == "GOCCL":
        return from_goccl(initial_data or market_data, observed)
    return BookingFeatures()
