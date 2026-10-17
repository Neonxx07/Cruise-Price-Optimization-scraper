"""Carnival guest (VIFP) lookup for the MSC status-match branch, 2026-10-08.

Evidence: a real signed-in GoCCL page saved with SingleFile from
https://www.goccl.com/app/bookingengine?openLookup=yes on 2026-10-08
(fixtures/goccl/vifp_guest_info_saved_2026_10_08.htm), and Neon's
annotated copy of it used by his standalone Playwright test
(fixtures/goccl/vifp_guest_form_fixture_2026_10_08.htm, from
Documents/guest_form_test/fill_guest_form.py). That script's verified run:

    #title: verified text 'MR'
    #first_name: verified text 'ALEX'
    #last_name: verified text 'SAMPLE'
    #date_of_birth: verified text 'January 15, 1990'
    #residency: selected GA
    #guest_number: filled 9000000001

The same outcome is asserted here through the project code, on BOTH
pages: the annotated fixture renamed GoCCL's ids (vifpNumber_0 ->
guest_number, StateOfResidency -> residency), so passing on both proves
the selectors do not depend on the annotations.

Pinned facts from the saved page:
  * name, title and DOB are read-only <span class=vifp-guest-info__txt>
  * the class vifp-guest-info__title marks THREE elements - the window
    heading "Carnival Guest Info", "Title" and "Carnival Rewards Points
    Available" - so fields must be read by label
  * State of Residency is <select id=StateOfResidency>, value GA = Georgia

The guest values are the fixture's own content, used only as expectations
for that page - not production data.
"""

from pathlib import Path

import pytest

from core.vifp_guest import (
    CarnivalGuestInfo,
    guest_info_from_fields,
    iso_to_long_date,
    match_msc_passenger,
    name_tokens,
    normalise_title,
    parse_date,
    verify_guest,
)
from scraper.goccl import GoCCLScraper
from services.status_match_service import latest_msc_booking, prepare_status_match

FIXTURES = Path(__file__).parent / "fixtures" / "goccl"
SAVED_PAGE = FIXTURES / "vifp_guest_info_saved_2026_10_08.htm"
ANNOTATED_PAGE = FIXTURES / "vifp_guest_form_fixture_2026_10_08.htm"
PAGES = [pytest.param(SAVED_PAGE, id="saved_page"),
         pytest.param(ANNOTATED_PAGE, id="annotated_fixture")]

# What the fixture page displays (Neon's guest.json, ISO date).
FIXTURE_GUEST = {
    "title": "MR",
    "first_name": "ALEX",
    "last_name": "SAMPLE",
    "date_of_birth": "1990-01-15",
    "guest_number": "9000000001",
}


# ── pure helpers ──────────────────────────────────────────────────────────

def test_iso_dates_render_the_way_goccl_shows_them():
    assert iso_to_long_date("1990-01-15") == "January 15, 1990"


@pytest.mark.parametrize("text", ["1990-01-15", "January 15, 1990",
                                  "01/15/1990", "  November  06, 1981 "])
def test_all_three_date_formats_parse_to_the_same_day(text):
    assert parse_date(text).isoformat() == "1990-01-15"


def test_an_unreadable_date_is_none_not_a_guess():
    assert parse_date("06/11/81") is None
    assert parse_date("") is None


@pytest.mark.parametrize("raw", ["MR", "Mr.", "mr", " Mr "])
def test_titles_normalise(raw):
    assert normalise_title(raw) == "MR"


def test_name_tokens_strip_accents_apostrophes_and_split_hyphens():
    assert name_tokens("José O'Brien-Smith") == ["JOSE", "OBRIEN", "SMITH"]


def _info(first="ALEX", last="SAMPLE", dob="January 15, 1990"):
    return guest_info_from_fields({
        "Title": "MR", "First Name": first, "Last Name": last,
        "Date of Birth": dob, "State of Residency": "Georgia",
        "Carnival Guest Number": "9000000001",
        "Carnival Rewards Status": "GOLD",
        "Carnival Rewards Points Available": "No",
    })


def test_a_record_with_a_missing_field_is_refused_whole():
    with pytest.raises(ValueError, match="tier"):
        guest_info_from_fields({"First Name": "A", "Last Name": "B",
                                "Date of Birth": "January 15, 1990",
                                "State of Residency": "Georgia",
                                "Carnival Guest Number": "1"})


def test_verify_reports_each_mismatch():
    problems = verify_guest(_info(), {**FIXTURE_GUEST, "last_name": "HOLLAWAY"})
    assert len(problems) == 1 and problems[0].startswith("last_name")


# ── matching the Carnival record to an MSC passenger ─────────────────────

def test_the_passenger_with_same_name_and_dob_matches():
    m = match_msc_passenger(_info(), [
        {"name": "JANE SAMPLE", "dob": "02/14/1995"},
        {"name": "ALEX SAMPLE", "dob": "01/15/1990"},
    ])
    assert m.passenger["name"] == "ALEX SAMPLE"


def test_an_extra_middle_name_on_msc_still_matches():
    m = match_msc_passenger(_info(), [{"name": "ALEX QUINN SAMPLE", "dob": "01/15/1990"}])
    assert m.passenger is not None


def test_same_dob_different_name_is_not_a_match():
    m = match_msc_passenger(_info(), [{"name": "ALEX SAMPLE", "dob": "01/15/1990"}])
    assert m.passenger is None
    assert "different name" in m.reason


def test_same_name_different_dob_is_not_a_match():
    m = match_msc_passenger(_info(), [{"name": "ALEX SAMPLE", "dob": "11/07/1999"}])
    assert m.passenger is None


def test_two_matching_passengers_is_ambiguous_not_a_pick():
    p = {"name": "ALEX SAMPLE", "dob": "01/15/1990"}
    m = match_msc_passenger(_info(), [p, dict(p)])
    assert m.passenger is None and "ambiguous" in m.reason


def test_no_passenger_list_is_not_a_match():
    assert match_msc_passenger(_info(), []).passenger is None


# ── the real page, through the scraper ───────────────────────────────────

async def _open(path):
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
    page = await browser.new_page()
    await page.goto(path.resolve().as_uri())
    scraper = GoCCLScraper()
    scraper._switch_to_page(page)
    return pw, browser, scraper


@pytest.mark.asyncio
async def test_the_title_class_really_is_shared_so_labels_are_required():
    pw, browser, scraper = await _open(SAVED_PAGE)
    try:
        # The window heading, "Title" and "Carnival Rewards Points Available".
        assert await scraper.page.locator(".vifp-guest-info__title").count() == 3
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_lookup_reads_the_record_and_screenshots_it(path, tmp_path):
    pw, browser, scraper = await _open(path)
    try:
        await scraper.open_vifp_lookup()            # already on screen: no navigation
        shot = tmp_path / "evidence.png"
        info = await scraper.lookup_vifp_guest("9000000001", screenshot_path=str(shot))
    finally:
        await browser.close()
        await pw.stop()
    assert verify_guest(info, FIXTURE_GUEST) == []
    assert info.date_of_birth_text == "January 15, 1990"
    assert info.state_of_residency == "Georgia"
    assert info.tier == "GOLD"
    assert shot.stat().st_size > 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_add_number_then_residency_is_selected_and_verified(path):
    pw, browser, scraper = await _open(path)
    try:
        info = await scraper.lookup_vifp_guest("9000000001")
        code = await scraper.add_vifp_guest(info)
        shown = await scraper.page.locator(scraper.STATE_OF_RESIDENCY).input_value()
    finally:
        await browser.close()
        await pw.stop()
    assert code == "GA" and shown == "GA"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_a_window_showing_another_guest_is_refused(path):
    pw, browser, scraper = await _open(path)
    try:
        with pytest.raises(RuntimeError, match="different guest number"):
            await scraper.lookup_vifp_guest("1234567890")
    finally:
        await browser.close()
        await pw.stop()


# ── the service, end to end on the fixture ───────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path", PAGES)
async def test_a_matching_customer_is_prepared_and_recorded(path, tmp_path):
    msc = {"booking_id": "TEST0001", "current_discounts": [],
           "passengers": [{"name": "ALEX SAMPLE", "dob": "01/15/1990"},
                          {"name": "JANE SAMPLE", "dob": "02/14/1995"}]}
    pw, browser, scraper = await _open(path)
    try:
        result = await prepare_status_match(scraper, "TEST0001", "9000000001",
                                            msc_record=msc, evidence_dir=tmp_path)
    finally:
        await browser.close()
        await pw.stop()
    assert result.ready, result.reason
    assert result.residency_code == "GA"
    assert result.passenger["name"] == "ALEX SAMPLE"
    assert Path(result.screenshot_path).stat().st_size > 1000
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1


@pytest.mark.asyncio
async def test_a_non_matching_customer_stops_before_add_number(tmp_path):
    msc = {"booking_id": "TEST0002",
           "passengers": [{"name": "SOMEONE ELSE", "dob": "01/01/1970"}]}
    pw, browser, scraper = await _open(SAVED_PAGE)

    async def must_not_run(_info):
        raise AssertionError("Add Number clicked for a record that matched nobody")
    scraper.add_vifp_guest = must_not_run
    try:
        result = await prepare_status_match(scraper, "TEST0002", "9000000001",
                                            msc_record=msc, evidence_dir=tmp_path)
    finally:
        await browser.close()
        await pw.stop()
    assert not result.ready
    assert "no passenger has this date of birth" in result.reason
    # the evidence of the refusal is kept, like every capture
    assert Path(result.screenshot_path).exists()
    assert (tmp_path / "results.jsonl").exists()


def test_an_unscanned_msc_booking_is_reported_not_guessed(tmp_path):
    assert latest_msc_booking("99999999", path=tmp_path / "none.jsonl") is None


def test_the_latest_scan_of_a_booking_wins(tmp_path):
    f = tmp_path / "booking_data.jsonl"
    f.write_text('{"booking_id": "1", "v": 1}\n{"booking_id": "2", "v": 9}\n'
                 '{"booking_id": "1", "v": 2}\n', encoding="utf-8")
    assert latest_msc_booking("1", path=f)["v"] == 2


def test_guest_info_is_a_plain_record():
    assert isinstance(_info(), CarnivalGuestInfo)
