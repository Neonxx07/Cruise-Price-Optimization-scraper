"""Carnival guest (VIFP) record: reading it, normalising it, matching it.

Built 2026-10-08 for the MSC status-match branch. The evidence MSC asks for
is GoCCL's own "Carnival Guest Info" window for the customer, looked up by
their Carnival Guest Number. This module is the pure half of that: no
browser, so every rule here is testable offline.

THE RECORD IS READ, NEVER WRITTEN. Title, first name, last name and date of
birth are display text in GoCCL (<span class="vifp-guest-info__txt">), not
inputs. They are compared against the MSC booking; they are never supplied
to the page. A record that does not match a passenger on the MSC booking is
a skip for a human to look at - never something to adjust until it fits.

Fields are keyed by their LABEL, not their CSS class. Confirmed against the
saved page (Downloads/1b40c7e0-....htm, 2026-10-08): the class
`vifp-guest-info__title` marks three elements - the window heading
"Carnival Guest Info", "Title" and "Carnival Rewards Points Available" - so
a class selector for the title is ambiguous.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

#: GoCCL label text -> field name. The labels are the stable identity.
GUEST_INFO_LABELS = {
    "Title": "title",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Date of Birth": "date_of_birth",
    "State of Residency": "state_of_residency",
    "Carnival Guest Number": "guest_number",
    "Carnival Rewards Status": "tier",
    "Carnival Rewards Points Available": "points_available",
}

#: A record missing any of these is not usable as evidence.
REQUIRED_FIELDS = ("first_name", "last_name", "date_of_birth",
                   "state_of_residency", "guest_number", "tier")

# ISO (guest.json), GoCCL's display ("January 15, 1990"), MSC's passenger
# list ("01/15/1990").
_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%m/%d/%Y")

_TITLES = {"MR", "MRS", "MS", "MISS", "MSTR", "DR"}


@dataclass(frozen=True)
class CarnivalGuestInfo:
    title: str
    first_name: str
    last_name: str
    date_of_birth: date
    date_of_birth_text: str          # exactly as GoCCL displayed it
    state_of_residency: str          # GoCCL's text, e.g. "Georgia"
    guest_number: str
    tier: str
    points_available: str

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "date_of_birth": self.date_of_birth.isoformat(),
            "date_of_birth_text": self.date_of_birth_text,
            "state_of_residency": self.state_of_residency,
            "guest_number": self.guest_number,
            "tier": self.tier,
            "points_available": self.points_available,
        }


def parse_date(text: str | None) -> date | None:
    """Any of the three date formats in play, or None. Never guesses."""
    text = " ".join((text or "").split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso_to_long_date(iso: str) -> str:
    """'1990-01-15' -> 'January 15, 1990', the way GoCCL displays it."""
    return datetime.strptime(iso.strip(), "%Y-%m-%d").strftime("%B %d, %Y")


def normalise_title(text: str | None) -> str:
    """'Mr.' / 'mr' / 'MR' -> 'MR'. Unknown titles pass through upper-cased."""
    return re.sub(r"[^A-Z]", "", (text or "").upper())


def normalise_guest_number(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def name_tokens(text: str | None) -> list[str]:
    """Upper-case name parts with accents, apostrophes and periods removed;
    hyphens split. 'O'Brien-Smith' -> ['OBRIEN', 'SMITH']."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).upper()
    text = re.sub(r"['.`’]", "", text)
    return [t for t in re.split(r"[\s\-]+", text) if t]


def guest_info_from_fields(fields: dict[str, str]) -> CarnivalGuestInfo:
    """Build the record from GoCCL label -> text pairs. Raises ValueError
    naming every missing or unreadable field rather than returning a
    partial record."""
    values = {GUEST_INFO_LABELS[k]: " ".join((v or "").split())
              for k, v in fields.items() if k in GUEST_INFO_LABELS}
    missing = [f for f in REQUIRED_FIELDS if not values.get(f)]
    dob = parse_date(values.get("date_of_birth"))
    if values.get("date_of_birth") and dob is None:
        missing.append("date_of_birth (unreadable: "
                       f"{values['date_of_birth']!r})")
    if missing:
        raise ValueError("Carnival Guest Info is missing: " + ", ".join(missing))
    return CarnivalGuestInfo(
        title=normalise_title(values.get("title")),
        first_name=values["first_name"].upper(),
        last_name=values["last_name"].upper(),
        date_of_birth=dob,
        date_of_birth_text=values["date_of_birth"],
        state_of_residency=values["state_of_residency"],
        guest_number=normalise_guest_number(values["guest_number"]),
        tier=values["tier"].upper(),
        points_available=values.get("points_available", ""),
    )


def verify_guest(info: CarnivalGuestInfo, expected: dict) -> list[str]:
    """Compare the record with expected values (only the keys given).

    Returns one message per mismatch; an empty list means every given value
    matched. Dates may be ISO or display format; titles and guest numbers
    are normalised first.
    """
    problems = []
    checks = {
        "title": (normalise_title, info.title),
        "first_name": (lambda v: " ".join(name_tokens(v)), " ".join(name_tokens(info.first_name))),
        "last_name": (lambda v: " ".join(name_tokens(v)), " ".join(name_tokens(info.last_name))),
        "date_of_birth": (parse_date, info.date_of_birth),
        "guest_number": (normalise_guest_number, info.guest_number),
        "tier": (lambda v: (v or "").strip().upper(), info.tier),
    }
    for key, value in expected.items():
        if key not in checks:
            continue
        convert, actual = checks[key]
        if convert(str(value)) != actual:
            problems.append(f"{key}: GoCCL shows {actual!r}, expected {value!r}")
    if info.title and info.title not in _TITLES:
        problems.append(f"title: unrecognised title {info.title!r}")
    return problems


@dataclass(frozen=True)
class PassengerMatch:
    passenger: dict | None
    reason: str


def match_msc_passenger(info: CarnivalGuestInfo, passengers: list[dict]) -> PassengerMatch:
    """Which passenger on the MSC booking is this Carnival guest?

    Conservative on purpose - the result decides whose loyalty record goes
    to MSC as evidence. A passenger matches only when:
      * the date of birth is identical, and
      * every Carnival first- and last-name part appears in the MSC name
        (MSC may carry an extra middle name; Carnival may not).
    Exactly one passenger must match. None, or more than one, is reported
    with the reason and left for a human.
    """
    wanted = set(name_tokens(info.first_name)) | set(name_tokens(info.last_name))
    if not passengers:
        return PassengerMatch(None, "the MSC booking has no passenger list")
    hits = [p for p in passengers
            if parse_date(p.get("dob")) == info.date_of_birth
            and wanted <= set(name_tokens(p.get("name")))]
    if len(hits) == 1:
        return PassengerMatch(hits[0], "name and date of birth match")
    if not hits:
        same_dob = [p for p in passengers if parse_date(p.get("dob")) == info.date_of_birth]
        why = ("a passenger has the same date of birth but a different name"
               if same_dob else "no passenger has this date of birth")
        return PassengerMatch(None, f"no match on the MSC booking: {why}")
    return PassengerMatch(None, f"ambiguous: {len(hits)} passengers match")
