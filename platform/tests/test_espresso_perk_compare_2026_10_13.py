"""ESPRESSO Promotions -> Compare: the booking priced WITH its perk kept.

EVIDENCE: booking 3000067, 2026-10-13. Neon's recording plus two
sanitized HAR files (fixture: fixtures/espresso/perk_compare_3000067_
2026_10_13.json - price data only). The booking was on the REFUNDABLE
ALL INC 2PK at $5,945 when he recorded them; per person, Y:

    Best Rate                BESTRATE  1,609.50
    ALL INC 2PK NRD          DJ065340  2,659.50   allocate total 5,319.00
    ALL INC 2PK (refundable) DJ028141  2,972.50   allocate total 5,945.00

so the NRD version of the SAME perk was $626 cheaper, which no scan saw:
every one of the 446 allocate calls in data/network_traffic.jsonl priced
BESTRATE. Neon, on seeing it: "there is a lower price again of 600".

THE SAFETY LINE: Select sits right next to Compare on the Promotions page
("You may select one of the available offers, or compare two different
offers"). The step may click the offer checkboxes and Compare, nothing
else - asserted here against a fake page where Select is wired to a
counter.
"""

import json
from pathlib import Path

import pytest

from core.espresso_perk_compare import (
    compare_targets,
    is_perk,
    merge_perk_compare,
    parse_promotion_value,
    perk_columns,
    refundability_note,
    twin_name,
    worth_repricing,
)
from core.models import BookingResult, BookingStatus, CruiseLine

FX = json.loads((Path(__file__).parent / "fixtures" / "espresso"
                 / "perk_compare_3000067_2026_10_13.json").read_text(encoding="utf-8"))
REFUNDABLE_BOOKING = [r["name"] for r in
                      FX["categories_after_compare_nrd"]["allocations"]["reservationsSummary"][0]["fare"]["rates"]]
NRD_BOOKING = ["ALL INC 2PK NRD", "SAVEUPTO100 NRD", "BONUS SAV NRD", "BOGO75 NRD"]


# -- reading the Promotions page ------------------------------------------


def test_the_value_format_is_the_one_the_form_posts():
    p = parse_promotion_value("DJ065340^ALL INC 2PK NRD^FIT^D")
    assert (p["code"], p["name"], p["flag"]) == ("DJ065340", "ALL INC 2PK NRD", "D")
    assert parse_promotion_value("BESTRATE^Best Rate^FIT")["flag"] == ""
    assert parse_promotion_value("garbage") is None


def test_all_inc_2pk_and_its_nrd_twin_are_different_offers():
    """Neon: "please note that there is ALL INC 2PK NRD and ALL INC 2PK"."""
    assert twin_name("ALL INC 2PK NRD") == "ALL INC 2PK"
    assert twin_name("ALL INC 2PK") == "ALL INC 2PK NRD"
    codes = {parse_promotion_value(v)["name"]: parse_promotion_value(v)["code"]
             for v in FX["promotion_values"]}
    assert codes["ALL INC 2PK NRD"] == "DJ065340"
    assert codes["ALL INC 2PK"] == "DJ028141"


def test_the_refundable_booking_compares_its_own_perk_and_the_nrd_twin():
    targets = compare_targets(REFUNDABLE_BOOKING, FX["promotion_values"])
    assert [t["code"] for t in targets] == ["DJ028141", "DJ065340"]


def test_the_nrd_booking_compares_its_own_perk_first():
    targets = compare_targets(NRD_BOOKING, FX["promotion_values"])
    assert [t["name"] for t in targets] == ["ALL INC 2PK NRD", "ALL INC 2PK"]


def test_never_more_than_two_and_never_best_rate_or_noperk():
    targets = compare_targets(["ALL INC 2PK NRD", "RETREAT NRD"], FX["promotion_values"])
    assert len(targets) == 2
    assert all(is_perk(t["name"]) for t in targets)


def test_no_perk_on_the_booking_means_nothing_to_compare():
    assert compare_targets(["BOGO75 NRD", "NOPERK NRD"], FX["promotion_values"]) == []


def test_a_perk_this_sailing_does_not_offer_is_not_invented():
    assert compare_targets(["ALL INC 2PK"], ["BESTRATE^Best Rate^FIT"]) == []


# -- reading the compared columns ------------------------------------------


def _columns(page_key, codes):
    targets = [{"code": c, "name": n} for c, n in codes]
    return perk_columns(FX[page_key]["matrix_Y"], targets)


def test_the_portals_own_y_prices_are_read():
    nrd = _columns("categories_after_compare_nrd", [("DJ065340", "ALL INC 2PK NRD")])[0]
    ref = _columns("categories_after_compare_refundable", [("DJ028141", "ALL INC 2PK")])[0]
    assert (nrd["price_pp"], nrd["non_refundable"]) == (2659.5, True)
    assert (ref["price_pp"], ref["non_refundable"]) == (2972.5, False)
    # Per person x 2 guests = the allocate response's own total.
    assert nrd["price_pp"] * 2 == FX["allocate_total_nrd_Y"]
    assert ref["price_pp"] * 2 == FX["allocate_total_refundable_Y"]


def test_a_column_the_page_did_not_return_is_left_out():
    assert _columns("categories_after_compare_nrd", [("DJ028141", "ALL INC 2PK")]) == []


def test_only_a_column_that_can_win_is_repriced():
    nrd = _columns("categories_after_compare_nrd", [("DJ065340", "ALL INC 2PK NRD")])[0]
    ref = _columns("categories_after_compare_refundable", [("DJ028141", "ALL INC 2PK")])[0]
    assert worth_repricing(nrd, 5945.0, 2) is True       # 5,319 < 5,945
    assert worth_repricing(ref, 5945.0, 2) is False      # 5,945 is today's price
    assert worth_repricing(ref, 5945.0, 3) is True       # 3 guests: not pre-judged
    assert worth_repricing({**nrd, "status": "WLT"}, 9999.0, 2) is False


def test_moving_to_the_nrd_twin_says_the_deposit_becomes_non_refundable():
    nrd = _columns("categories_after_compare_nrd", [("DJ065340", "ALL INC 2PK NRD")])[0]
    assert "DEPOSIT BECOMES NON-REFUNDABLE" in refundability_note(REFUNDABLE_BOOKING, nrd)
    assert refundability_note(NRD_BOOKING, nrd) == ""


# -- choosing the verdict ---------------------------------------------------


def _result(status, net, new_total, note="", **kw):
    return BookingResult(cruise_line=CruiseLine.ESPRESSO, booking_id="3000067",
                         status=status, net_saving=net, new_total=new_total,
                         old_total=5945.0, price_drop=5945.0 - new_total, note=note, **kw)


def test_a_clean_perk_kept_column_replaces_the_estimate():
    main = _result(BookingStatus.OPTIMIZATION, 626.0, 3219.0, "optimized $626 (estimate)")
    column = {"name": "ALL INC 2PK NRD", "non_refundable": True, "price_pp": 2659.5}
    kept = _result(BookingStatus.OPTIMIZATION, 626.0, 5319.0, "optimized $626")
    out = merge_perk_compare(main, {"results": [(column, kept)], "columns": [column]},
                             REFUNDABLE_BOOKING)
    assert out is kept
    assert out.new_total == 5319.0
    assert out.note.startswith("PERK KEPT — ALL INC 2PK NRD")
    assert "DEPOSIT BECOMES NON-REFUNDABLE" in out.note


def test_a_column_that_loses_something_never_replaces_the_verdict():
    main = _result(BookingStatus.TRAP, -100.0, 3219.0, "trap")
    column = {"name": "ALL INC 2PK NRD", "non_refundable": True, "price_pp": 2659.5,
              "new_total": 5319.0, "verdict": "OPTIMIZATION"}
    lossy = _result(BookingStatus.OPTIMIZATION, 300.0, 5319.0, lost_pkg_value=326.0)
    out = merge_perk_compare(main, {"outcome": "DONE", "results": [(column, lossy)],
                                    "columns": [column]}, REFUNDABLE_BOOKING)
    assert out is main
    assert "[PERK COMPARE DONE: ALL INC 2PK NRD 2,659.50pp → total $5,319.00" in out.note


def test_the_compare_outcome_is_always_written_down():
    main = _result(BookingStatus.NO_SAVING, 0.0, 5945.0, "no saving")
    out = merge_perk_compare(main, {"outcome": "PERK_NOT_ON_PROMOTIONS_PAGE", "columns": []},
                             REFUNDABLE_BOOKING)
    assert "[PERK COMPARE PERK_NOT_ON_PROMOTIONS_PAGE: no column]" in out.note


# -- the clicks, on a fake ESPRESSO ---------------------------------------

HOST = "https://secure.cruisingpower.com"
FLOW = f"{HOST}/espresso/protected/reservations.do"

_ROW = """<tr><td class="c1"><div class="categoryIcon"><span>Y</span></div></td>
{radios}</tr>"""
_RADIO = ('<td><input type="radio" name="rbCategorySelection" value="1" '
          'data-columnindex="{i}" data-requestedcode="{code}" '
          'onclick="pick(this)"/></td>')

_CATEGORIES = """<html><body>
<a href="/espresso/protected/reservations.do?execution={ex}&amp;_eventId=linkToPromotionList">Promotions</a>
<input class="selectionJSON" value="[]"/>
<table id="catAvailCategoryList"><tbody>{row}</tbody></table>
<script>
var viewJSON = {view};
function pick(r) {{
  var code = r.getAttribute('data-requestedcode').replace('INDIVIDUAL-', '');
  document.querySelector('input.selectionJSON').value =
    JSON.stringify([{{columnPricePromo: {{requestedCode: code}}, category: {{code: 'Y'}}}}]);
}}
</script></body></html>"""

_PROMOTIONS = """<html><body>
<form method="post" id="promoFormId" action="/espresso/protected/reservations.do?execution=e7s6&amp;_eventId=saveChanges">
{boxes}
<a href="#" id="promoFare00" class="btnCompare disabled" style="{header_style}" onclick="compare(); return false;">Compare</a>
<a href="#" id="promoFare01" class="btnSelect disabled" onclick="location.href='{flow}?execution=e7s6&amp;_eventId=SELECT_CLICKED'; return false;">Select</a>
<a href="#" id="promoFare10" class="btnCompare disabled" onclick="compare(); return false;">Compare</a>
<a href="#" id="promoFare11" class="btnSelect disabled" onclick="location.href='{flow}?execution=e7s6&amp;_eventId=SELECT_CLICKED'; return false;">Select</a>
</form>
<script>
function refresh() {{
  var n = document.querySelectorAll('input[name="promotions"]:checked').length;
  ['promoFare00', 'promoFare10'].forEach(function (id) {{
    document.getElementById(id).className = 'btnCompare' + (n === 2 ? '' : ' disabled');
  }});
}}
// What the live page did on 2026-10-14 (9 bookings, TICK_MISMATCH): the two
// versions of one perk cannot be ticked together - ticking one unticks the other.
function twinOf(v) {{
  var name = v.split('^')[1] || '';
  if (name.indexOf('ALL INC 2PK') !== 0) return null;
  return /NRD$/.test(name) ? name.replace(/ NRD$/, '') : name + ' NRD';
}}
document.querySelectorAll('input[name="promotions"]').forEach(function (b) {{
  b.addEventListener('click', function () {{
    var twin = twinOf(b.value);
    if (b.checked && twin) {{
      document.querySelectorAll('input[name="promotions"]').forEach(function (o) {{
        if (o !== b && o.value.split('^')[1] === twin) o.checked = false;
      }});
    }}
    refresh();
  }});
}});
function compare() {{ document.getElementById('promoFormId').submit(); }}
refresh();
</script></body></html>"""


def _reprice(new_total, new_perk):
    old = REFUNDABLE_BOOKING
    return {"result": {
        "oldInvoice": {"invoiceItems": [{"paxId": "total", "type": "VACATION_TOTAL", "amount": 5945.0}]},
        "newInvoice": {"invoiceItems": [{"paxId": "total", "type": "VACATION_TOTAL", "amount": new_total}]},
        "oldFares": [{"name": n} for n in old],
        "newFares": [{"name": n} for n in [new_perk, "SAVEUPTO100 NRD", "BONUS SAV NRD", "BOGO75 NRD"]],
    }, "padding": "x" * 400}


async def _fake_espresso(header_style=""):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"chromium unavailable: {str(exc)[:80]}")
    context = await browser.new_context()
    seen = {"compare_posts": [], "allocate_codes": [], "select_clicks": 0, "reprices": 0}
    nrd_entry = FX["categories_after_compare_nrd"]["matrix_Y"]["INDIVIDUAL-DJ065340"]
    ref_entry = FX["categories_after_compare_refundable"]["matrix_Y"]["INDIVIDUAL-DJ028141"]

    def categories(ex, columns):
        view = {"matrix": {"matrix": {"Y": columns}},
                "sailingModel": {"occupancy": "2"},
                "allocations": {"totalInvoiceAmount": 5945.0}}
        radios = "".join(_RADIO.format(i=i, code=k) for i, k in enumerate(columns))
        return _CATEGORIES.format(ex=ex, row=_ROW.format(radios=radios), view=json.dumps(view))

    async def serve(route):
        req = route.request
        url = req.url
        if "SELECT_CLICKED" in url:
            seen["select_clicks"] += 1
            await route.fulfill(status=200, content_type="text/html", body="<html>selected</html>")
        elif "linkToPromotionList" in url:
            boxes = "".join(
                f'<input name="promotions" type="checkbox" value="{v}"'
                f'{" checked" if v.startswith("BESTRATE") else ""}/>'
                for v in FX["promotion_values"])
            await route.fulfill(status=200, content_type="text/html",
                                body=_PROMOTIONS.format(boxes=boxes, flow=FLOW,
                                                        header_style=header_style))
        elif "saveChanges" in url:
            seen["compare_posts"].append(req.post_data or "")
            await route.fulfill(status=200, content_type="text/html", body=categories(
                "e7s7", {"INDIVIDUAL-DJ028141": ref_entry, "INDIVIDUAL-DJ065340": nrd_entry}))
        elif "_eventId=allocate" in url:
            body = req.post_data or ""
            for code in ("DJ065340", "DJ028141", "BESTRATE"):
                if code in body:
                    seen["allocate_codes"].append(code)
            await route.fulfill(status=200, content_type="text/html", body="ok")
        elif "showRepriceModalCheck" in url:
            seen["reprices"] += 1
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(_reprice(5319.0, "ALL INC 2PK NRD")))
        else:
            await route.fulfill(status=200, content_type="text/html", body=categories(
                "e7s5", {"INDIVIDUAL-BESTRATE": [], "INDIVIDUAL-BVL": []}))

    await context.route(f"{HOST}/**", serve)
    page = await context.new_page()
    await page.goto(f"{FLOW}?execution=e7s5")
    from scraper.espresso import EspressoScraper
    scraper = EspressoScraper()
    scraper._page = page
    return pw, browser, scraper, seen


@pytest.mark.asyncio
async def test_the_compare_walk_prices_the_nrd_twin_and_never_clicks_select():
    pw, browser, s, seen = await _fake_espresso()
    try:
        record = await s._perk_compare("3000067", "Y", REFUNDABLE_BOOKING, 5945.0)
    finally:
        await browser.close()
        await pw.stop()

    assert record["outcome"] == "DONE"
    assert seen["select_clicks"] == 0
    # One Compare per perk, each paired with Best Rate - the pair the HARs
    # proved; the two versions are never ticked together.
    assert len(seen["compare_posts"]) == 2
    for post, code, other in zip(seen["compare_posts"], ("DJ028141", "DJ065340"),
                                 ("DJ065340", "DJ028141")):
        assert code in post and "BESTRATE" in post and other not in post
    # Only the NRD column can beat $5,945, so only it is repriced - on ITS radio.
    assert seen["allocate_codes"] == ["DJ065340"]
    assert seen["reprices"] == 1
    by_name = {c["name"]: c for c in record["columns"]}
    assert by_name["ALL INC 2PK NRD"]["new_total"] == 5319.0
    assert by_name["ALL INC 2PK"]["repriced"] is False

    out = merge_perk_compare(
        _result(BookingStatus.TRAP, -1474.0, 3219.0, "trap"), record, REFUNDABLE_BOOKING)
    assert out.status == BookingStatus.OPTIMIZATION
    assert out.net_saving == 626.0
    assert "DEPOSIT BECOMES NON-REFUNDABLE" in out.note


@pytest.mark.asyncio
async def test_a_page_without_the_perk_stops_before_any_click():
    pw, browser, s, seen = await _fake_espresso()
    try:
        record = await s._perk_compare("X", "Y", ["BOGO75 NRD"], 5945.0)
    finally:
        await browser.close()
        await pw.stop()
    assert record["outcome"] == "PERK_NOT_ON_PROMOTIONS_PAGE"
    assert seen["compare_posts"] == [] and seen["select_clicks"] == 0


@pytest.mark.asyncio
async def test_a_hidden_header_compare_uses_the_visible_footer_one():
    """Live 2026-10-14, 3001033 and 3001069: #promoFare00 was found but never
    became visible, and the click waited 30 s. The footer twin is clicked."""
    pw, browser, s, seen = await _fake_espresso(header_style="display:none")
    try:
        record = await s._perk_compare("3000067", "Y", REFUNDABLE_BOOKING, 5945.0)
    finally:
        await browser.close()
        await pw.stop()
    assert record["outcome"] == "DONE"
    assert len(seen["compare_posts"]) == 2
    assert seen["select_clicks"] == 0
