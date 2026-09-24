"""The price drivers, captured on every scan from now on.

Neon 2026-09-21: "okay then implementt so from now on on any scan specially
on the gui specially we can predic moveing forward for all the cruise lines".

WHY. A drop-prediction model trained on price_history's first 5,067 rows
scored AUC 0.686 on a TEMPORAL split - and 0.549, a coin flip, once
scan-cadence features were stripped out. The table stored price, category
and a timestamp and none of the actual drivers; days-to-sailing, the
dominant one in cruise pricing, was recorded nowhere at all.

Every value below came from a REAL capture of the line in question.
"""
from datetime import date

import pytest

from core.booking_features import (
    BookingFeatures,
    extract,
    from_espresso,
    from_goccl,
    from_ncl,
    parse_date,
)

OBS = date(2026, 9, 21)


# ── date shapes, one per portal ──────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("21FEB2027", date(2027, 2, 21)),        # ESPRESSO
    ("08/28/2026", date(2026, 8, 28)),       # NCL bookingFacts
    ("2026-11-21T15:30:00-06:00", date(2026, 11, 21)),   # GoCCL rawValue
    ("2026-11-21", date(2026, 11, 21)),
])
def test_every_real_date_shape_parses(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "32FEB2027", "13/45/2026"])
def test_an_unreadable_date_is_None_never_a_guess(raw):
    assert parse_date(raw) is None


# ── ESPRESSO ─────────────────────────────────────────────────────────────


def test_espresso_reads_sail_date_and_ship_from_the_page_json():
    """Real capture: "sailDate":"21FEB2027", "shipCode":"AL",
    "shipName":"Allure Of The Seas"."""
    page = ('junk{"sailDate":"21FEB2027","shipCode":"AL",'
            '"shipName":"Allure Of The Seas"}junk')
    f = from_espresso(page, observed=OBS)
    assert f.sail_date == date(2027, 2, 21)
    assert f.ship_code == "AL"
    assert f.ship_name == "Allure Of The Seas"
    assert f.days_to_sailing == 153
    assert f.sailing_month == 2


def test_espresso_with_no_page_yields_all_None():
    f = from_espresso(None, observed=OBS)
    assert f.sail_date is None and f.days_to_sailing is None


# ── NCL ──────────────────────────────────────────────────────────────────


NCL_PAYLOAD = {"payment": {"finalPaymentDate": "08/27/2026", "bookingFacts": {
    "Vacation Start Date": "08/28/2026", "Vacation End Date": "08/31/2026",
    "Ship": "Norwegian Getaway", "Guests": "2"}}}


def test_ncl_reads_booking_facts():
    f = from_ncl(NCL_PAYLOAD, observed=OBS)
    assert f.sail_date == date(2026, 8, 28)
    assert f.nights == 3
    assert f.ship_name == "Norwegian Getaway"
    assert f.guests_count == 2
    assert f.final_payment_date == date(2026, 8, 27)


def test_a_past_sailing_gives_a_NEGATIVE_days_to_sailing():
    """Real case: this booking had already sailed when observed. -24 is the
    truth; clamping it to 0 would tell a model the ship leaves today."""
    assert from_ncl(NCL_PAYLOAD, observed=OBS).days_to_sailing == -24


def test_ncl_missing_facts_do_not_raise():
    for payload in (None, {}, {"payment": None}, {"payment": {"bookingFacts": "nope"}}):
        f = from_ncl(payload, observed=OBS)
        assert f.sail_date is None and f.guests_count is None


# ── GoCCL ────────────────────────────────────────────────────────────────


GOCCL = {
    "ship": {"code": "LI", "name": "CARNIVAL LIBERTY"},
    "category": {"code": "8A", "stateroomType": {"name": "BALCONY"}},
    "rate": {"code": "BKGRTE", "virtualCode": "PXF", "gbrCode": "PXF"},
    "guests": [{"sequenceNumber": 1}, {"sequenceNumber": 2}],
    "currencyCode": "USD",
    "paymentSchedule": {"finalPaymentDueDate": {"rawValue": "2026-08-23T23:59:00-04:00"},
                        "netBalanceDue": {"amount": 0.0}},
    "itinerary": {"duration": 8, "itineraryCode": "EC7",
                  "destinationName": "Bahamas",
                  "embarkationPortName": "New Orleans",
                  "departure": {"rawValue": "2026-11-21T15:30:00-06:00"}},
}


def test_goccl_reads_everything_including_region():
    f = from_goccl(GOCCL, observed=OBS)
    assert f.sail_date == date(2026, 11, 21)
    assert f.days_to_sailing == 61
    assert f.nights == 8
    assert f.region == "Bahamas"
    assert f.itinerary_code == "EC7"
    assert f.embark_port == "New Orleans"
    assert f.stateroom_type == "BALCONY"
    assert f.guests_count == 2
    assert f.net_balance_due == 0.0


def test_goccl_sail_date_comes_from_itinerary_departure_rawValue():
    """A first version guessed sailDate/startDate/departureDate and got None
    on every booking. The real key is itinerary.departure.rawValue - found by
    reading a capture instead of guessing."""
    assert from_goccl({"itinerary": {"sailDate": "2026-01-01"}}).sail_date is not None
    assert from_goccl(GOCCL).sail_date == date(2026, 11, 21)


def test_goccl_fare_code_is_the_real_one_not_the_sentinel():
    """rate.code is the constant "BKGRTE" on every booking; the real code is
    virtualCode/gbrCode."""
    f = from_goccl(GOCCL, observed=OBS)
    assert f.fare_code == "PXF"
    assert f.fare_code != "BKGRTE"


# ── the rule that makes the data trustworthy ─────────────────────────────


def test_missing_stays_missing_never_zero():
    """THE POINT. A booking whose sail date could not be read must record
    NULL, not 0 - a model would read 0 as "sails today" and learn from a
    fabricated fact. This is the same missing-is-not-zero rule as
    core/goccl_review.py's POBC handling."""
    f = BookingFeatures()
    row = f.as_row()
    assert all(v is None for v in row.values())
    assert 0 not in row.values() and 0.0 not in row.values()


def test_an_unknown_cruise_line_yields_empty_not_an_error():
    """A new line must never break a scan."""
    f = extract("PRINCESS")
    assert isinstance(f, BookingFeatures)
    assert f.sail_date is None


def test_dispatch_routes_each_line_to_its_own_extractor():
    assert extract("NCL", market_data=NCL_PAYLOAD, observed=OBS).ship_name == "Norwegian Getaway"
    assert extract("GOCCL", initial_data=GOCCL, observed=OBS).region == "Bahamas"
    assert extract("ESPRESSO", page_text='{"shipCode":"AL"}', observed=OBS).ship_code == "AL"


def test_as_row_serialises_dates_for_sqlite():
    f = from_goccl(GOCCL, observed=OBS)
    row = f.as_row()
    assert row["sail_date"] == "2026-11-21"
    assert row["final_payment_date"] == "2026-08-23"
    assert isinstance(row["days_to_sailing"], int)


# ── ESPRESSO coverage, widened after measuring it ────────────────────────
#
# ESPRESSO is 4,500 of 5,091 price_history rows (88%) and was filling only
# 4 of 14 driver columns - the line with the most data had the least signal.


def test_espresso_also_captures_currency_and_stateroom_type():
    """Both confirmed present on 12 of 12 real booking pages."""
    page = ('{"sailDate":"21FEB2027","shipCode":"AL","shipName":"Allure Of The Seas",'
            '"currency":"USD","stateroomType":"Ocean View Balcony","occupancy":4}')
    f = from_espresso(page, observed=OBS)
    assert f.currency == "USD"
    assert f.stateroom_type == "Ocean View Balcony"


def test_espresso_occupancy_is_NOT_written_to_guests_count():
    """THE JUDGEMENT CALL. "occupancy" is present and varies (2 and 4 across
    12 real pages), but nothing observed says whether it is the booking's
    GUEST COUNT or the cabin's BERTH CAPACITY - and the neighbouring
    "totalGuests" reads the unrendered template "(Pending)".

    Writing a berth capacity into guests_count would teach a model a
    fabricated fact about occupancy. It stays unmapped until a booking with
    a known guest count settles it."""
    page = '{"sailDate":"21FEB2027","occupancy":4}'
    assert from_espresso(page, observed=OBS).guests_count is None
