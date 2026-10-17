"""POLAR page readers, on the real DEMO12 pages (price markup only).

Fixture: fixtures/princess/demo12_pages_2026_10_15.json, cut from Neon's
HAR with names, phone, ages and agent contact removed. Every figure below is
the one on his screenshots of DEMO12 (RUBY R702/11JAN27, B4, group THN):
Fare 6,358 + Packages 176 = Gross 6,534; OBC 325; received 6,493 + due 41;
final due 14OCT26; Special Requests "Princess Plus" 1,120 x 2; offer code
PLUS2; promos THN TND M1E RG1; FIT B4 quote 3,179 pp = the booking exactly;
group B4 quote 4,079 pp = 2,959 + 1,120 (Plus inside).

The same readers were also run over the 32 bookings of the live read-only
run (data/princess_readonly_20261015T*): Fare - Base = fees + taxes held on
every one, and received + due = gross wherever a balance was open.
"""
import json
from pathlib import Path

import pytest

from core import princess_pages as P

FX = json.loads((Path(__file__).parent / "fixtures" / "princess"
                 / "demo12_pages_2026_10_15.json").read_text(encoding="utf-8"))


def test_money():
    assert P.money("6,534.00") == 6534.0
    assert P.money("665.50-") == -665.5
    assert P.money("325.00 USD") == 325.0
    assert P.money("") is None and P.money(None) is None


def test_the_landing_page():
    c = P.booking_confirmation(FX["landing"])
    assert c["status"]["code"] == "B" and c["status"]["days_out"] == 88
    assert c["group"] == "THN"
    assert c["currency"] == "USD"
    f = c["fare_lines"]
    assert (f["Fare"], f["Packages"], f["Gross"], f["Onboard Credit"]) == (6358.0, 176.0, 6534.0, 325.0)
    assert f["Fare"] - f["Base Fare (Incld in Cruise Fare)"] == pytest.approx(
        f["Req'd Cruise Fees & Expenses"] + f["Incld Gov't Taxes & Fees"])
    assert (c["total_received"], c["gross_payment_due"], c["final_due"]) == (6493.0, 41.0, "14OCT26")


def test_special_requests_and_offer_code():
    sr = P.special_requests(FX["special_requests"])
    assert [(s["description"], s["pax"], s["price"]) for s in sr] == [
        ("Tray of Assorted Canapes", "01", 28.5), ("Princess Plus", "01", 1120.0),
        ("Princess Plus", "02", 1120.0)]
    # the page's script also contains '-COUPON" value="' - never read as a code
    assert P.offer_codes(FX["booking_information_offer"]) == ["PLUS2"]


def test_category_rows_and_the_two_character_category():
    rows = P.category_rows(FX["category_fares"])
    b4 = next(r for r in rows if r["row"] == "B43")
    assert b4["category"] == "B4"
    assert (b4["price_fit_net_of_obc"], b4["price_group"], b4["promo_fit"]) == (3179.0, 2959.0, "RG1")
    assert P.preselected_category(FX["category_fares"]) == "B4"
    assert len({r["category"] for r in rows}) == len(rows)       # no two rows share a prefix here


def test_promo_list():
    promos = {p["code"]: p for p in P.promo_list(FX["fare_comparisons"])}
    assert promos["M1E"]["description"] == "UPGRADE STANDARD TO PLUS 70"
    assert promos["M1E"]["advisory"] == "Automatic"
    assert promos["RG1"]["description"] == "LAST MINUTE DEALS - STANDARD (NBO)"
    assert set(promos) == {"THN", "TND", "M1E", "RG1"}


def test_the_fit_quote_reproduces_the_booking():
    q = P.pricing_detail(FX["pricing_detail_fit"])
    assert q["lines"]["Fare"] == [3179.0, 3179.0]
    assert q["lines"]["Transfers/Packages"] == [88.0, 88.0]
    assert q["totals"]["Gross Fare"] == 6534.0                    # = the booking's Gross
    assert q["promo_codes"] == [["RG1", "M1E", "GCH", "GS4", "KR1"]]
    assert q["currency"] == "USD"


def test_the_group_quote_carries_plus_inside_the_fare():
    q = P.pricing_detail(FX["pricing_detail_group"])
    assert q["lines"]["Fare"] == [4079.0, 4079.0]                  # 2,959 + 1,120
    assert q["totals"]["Gross Fare"] == 8334.0
    assert q["promo_codes"][0][0] == "M1E"


def test_only_counted_or_guaranteed_space_is_available():
    """Live 2026-10-15: DEMO29 and DEMO27 read 'W/' on the cheaper promo -
    waitlist, never a saving. Unknown statuses are not available either."""
    for ok in ("9", "3", "1", "G", "G/G", " 9 "):
        assert P.category_available(ok), ok
    for no in ("W/", "W", "C", "", None, "B/2", "Call RES"):
        assert not P.category_available(no), no


def test_vacation_protection_with_its_asterisk_is_read():
    """DEMO25 2026-10-15: the quote row reads 'Vacation Protection *'."""
    html = ("<table><tr><td>&nbsp;</td><td align='left'><strong>Vacation Protection</strong> *</td>"
            "<td> 103.35 </td><td> 103.35 </td><td> </td><td> 206.70</td></tr></table>")
    q = P.pricing_detail(html)
    assert q["lines"]["Vacation Protection"] == [103.35, 103.35]
    assert q["totals"]["Vacation Protection"] == 206.70
