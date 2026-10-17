"""Fields the portals were already showing us, and we were throwing away.

MEASURED 2026-09-28 — price_history fill rate, today's rows:

    column                ESPRESSO       NCL
    final_payment_date          0%      100%
    net_balance_due             0%        0%
    region                      0%        0%

Three of those were not missing from the portals at all. ESPRESSO's payment
panel carries the final payment date and the outstanding balance - both were
parsed for the paid-in-full gate and discarded. NCL's bookingFacts carries
"Destination" on 20 of 20 captures, next to fields already being read.

Nothing extra is scraped for any of this. It is data already in hand being
written down.
"""

from datetime import date

from core.booking_features import from_espresso, from_ncl
from scraper.espresso import EspressoScraper


# ── ESPRESSO: the payment panel ──────────────────────────────────────────
#
# The layout is the one documented at _PAYMENT_FIELD_PATTERNS:
#
#     Final Payment Due (CAD):  Due: 10JUL2026
#     Final Payment:            0.02

PANEL = ("Total Price (CAD): 6,627.00\n"
         "Final Payment Due (CAD):  Due: 10JUL2026\n"
         "Final Payment:            0.02")


def test_the_final_payment_date_is_read_from_the_panel():
    m = EspressoScraper._FINAL_PAYMENT_DATE_RE.search(PANEL)
    assert m and m.group(1) == "10JUL2026"


def test_the_date_pattern_does_not_fire_on_the_amount_layout():
    """The same label carries an AMOUNT on the other layout. Reading
    "1234.00" as a date would be worse than leaving the column NULL."""
    amount_only = "Final Payment Due (USD): 1234.00"
    assert EspressoScraper._FINAL_PAYMENT_DATE_RE.search(amount_only) is None


def test_espresso_fills_final_payment_date_and_balance():
    f = from_espresso(
        page_fields={"sailDate": "21FEB2027", "shipCode": "AL",
                     "finalPaymentDate": "10JUL2026", "netBalanceDue": 1234.5},
        observed=date(2026, 9, 28))
    assert f.final_payment_date == date(2026, 7, 10)
    assert f.net_balance_due == 1234.5


def test_an_unreadable_balance_stays_none_not_zero():
    """A fabricated 0.00 reads as "paid in full" to everything downstream.
    Missing is not zero - the rule that produced the false $400 saving."""
    f = from_espresso(page_fields={"sailDate": "21FEB2027",
                                   "netBalanceDue": "n/a"})
    assert f.net_balance_due is None


def test_espresso_without_the_panel_still_works():
    """The panel is unreadable on some bookings. That must cost the two new
    columns, not the whole feature row."""
    f = from_espresso(page_fields={"sailDate": "21FEB2027", "shipCode": "AL"},
                      observed=date(2026, 9, 28))
    assert f.sail_date == date(2027, 2, 21)
    assert f.final_payment_date is None and f.net_balance_due is None


# ── NCL: bookingFacts ────────────────────────────────────────────────────


def _facts(**extra):
    base = {"Vacation Start Date": "05/22/2027", "Vacation End Date": "05/29/2027",
            "Ship": "Norwegian Bliss", "Guests": "2", "Destination": "ALASKA"}
    base.update(extra)
    return {"payment": {"bookingFacts": base}}


def test_ncl_fills_region_from_destination():
    """0% filled for every line, while NCL reported it on 20 of 20 captures."""
    f = from_ncl(_facts(), observed=date(2026, 9, 28))
    assert f.region == "Alaska"


def test_ncl_region_is_absent_when_destination_is():
    f = from_ncl({"payment": {"bookingFacts": {"Ship": "Norwegian Bliss"}}})
    assert f.region is None


def test_ncl_stateroom_number_is_not_written_as_a_type():
    """bookingFacts["Stateroom"] is a cabin NUMBER ("9232"). Writing it into
    stateroom_type would teach a model a fabricated fact - the same class of
    error as the deliberately-unmapped ESPRESSO "occupancy"."""
    f = from_ncl(_facts(Stateroom="9232"), observed=date(2026, 9, 28))
    assert f.stateroom_type is None


def test_ncl_keeps_what_it_already_had():
    f = from_ncl(_facts(), observed=date(2026, 9, 28))
    assert f.nights == 7
    assert f.guests_count == 2
    assert f.ship_name == "Norwegian Bliss"
