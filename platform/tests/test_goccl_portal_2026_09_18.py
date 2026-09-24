"""GoCCL (Carnival) portal facts, pinned to a real captured session.

Neon 2026-09-18: "deepdive in carnival goccl portal and start building a
perfect script" / "investigate more and moniotr the wepabge to build a good
strict scraper".

Everything asserted here was read off a live signed-in session (booking
DEMO08), captured to data/goccl_watch/20260918T113403/. Before that session
the scraper's downstream selectors had never been seen on a real page - they
were recorded from DevTools against a page that no longer matched, which is
how it ended up deep-linking into a wizard it could not reach.

The point of these tests is to make the portal's real shape expensive to
break silently.
"""
import pytest

from scraper.goccl import GoCCLScraper


# ── the commit guard ─────────────────────────────────────────────────────
#
# THE SAFETY PROPERTY. Enumerating every button on all five wizard pages
# showed the flow is read-only until exactly one control: "Confirm Changes"
# on /review. It carries no data-comp attribute, so only its text
# identifies it.


@pytest.mark.parametrize("label", [
    "Confirm Changes",
    "confirm changes",
    "  CONFIRM CHANGES  ",
    "Confirm Changes ",
    "Make a Payment",
    "Pay With Funship Pay",
])
def test_a_committing_control_is_refused(label):
    with pytest.raises(RuntimeError, match="REFUSING to click"):
        GoCCLScraper()._assert_safe_click(label)


def test_the_refusal_says_why_in_business_terms():
    with pytest.raises(RuntimeError) as exc:
        GoCCLScraper()._assert_safe_click("Confirm Changes")
    assert "read-only" in str(exc.value)
    assert "live booking" in str(exc.value)


@pytest.mark.parametrize("label", [
    "Discard Changes",
    "Discard All Changes",
    "Continue",
    "Keep Same Stateroom",
    "Select    $  1,391",
    "New Search",
    "",
    None,
])
def test_a_reversible_control_is_allowed(label):
    """The guard must not be so broad it blocks the flow itself - a scraper
    that cannot click Continue discovers nothing."""
    GoCCLScraper()._assert_safe_click(label)


def test_the_guard_matches_case_insensitively_and_on_substrings():
    """Irreversible. A near-miss on capitalisation or surrounding text is
    not a reason to let a commit through."""
    with pytest.raises(RuntimeError):
        GoCCLScraper()._assert_safe_click("Yes, Confirm Changes Now")


# ── the offer code is not rate.code ──────────────────────────────────────


def _initial_data(**over):
    """window.initialData as the real booking page served it."""
    base = {
        "invoiceSummary": {"grossAmount": {"amount": 1365.81}},
        "rate": {"code": "BKGRTE", "virtualCode": "O7O", "gbrCode": "O7O",
                 "name": "SAVE & SAIL: MORE TIME MORE PERKS SALE",
                 "isGroupRate": False},
        "category": {"code": "4A", "stateroomType": {"code": "IS", "name": "INTERIOR"}},
    }
    base.update(over)
    return base


class _FakePage:
    def __init__(self, data):
        self._data = data

    async def evaluate(self, _expr):
        return self._data


async def _read(data):
    s = GoCCLScraper()
    s._page = _FakePage(data)
    return await s.read_current_price_and_selection()


@pytest.mark.asyncio
async def test_the_offer_code_comes_from_gbr_code_not_the_sentinel():
    """CONFIRMED on booking DEMO08: rate.code is the literal constant
    "BKGRTE" on all five wizard pages - a sentinel, not an offer code. The
    real code is virtualCode/gbrCode ("O7O"), which is exactly what the
    comparison tiles publish as data-rate-gbrcode.

    This mattered: read_offer_code_comparison returns codes like GO2/OB7/
    PB4, so a current code of "BKGRTE" could never match any of them and
    "is this booking already on that offer?" was unanswerable.
    """
    out = await _read(_initial_data())
    assert out["current_offer_code"] == "O7O"


@pytest.mark.asyncio
async def test_the_raw_sentinel_is_kept_visible_not_silently_swapped():
    """Only ONE booking has been observed. Keeping the raw value lets the
    next capture prove whether "BKGRTE" really is constant, instead of the
    substitution hiding the evidence."""
    out = await _read(_initial_data())
    assert out["current_rate_code_raw"] == "BKGRTE"


@pytest.mark.asyncio
async def test_a_real_code_in_rate_code_is_still_honoured():
    """If a booking ever does carry a genuine code there, don't discard it."""
    out = await _read(_initial_data(
        rate={"code": "PB4", "virtualCode": None, "gbrCode": None}))
    assert out["current_offer_code"] == "PB4"


@pytest.mark.asyncio
async def test_the_sentinel_never_leaks_out_as_an_offer_code():
    out = await _read(_initial_data(
        rate={"code": "BKGRTE", "virtualCode": None, "gbrCode": None}))
    assert out["current_offer_code"] == ""


@pytest.mark.asyncio
async def test_a_missing_gross_still_refuses_rather_than_reporting_zero():
    """Pre-existing guard, re-pinned: old_total=0 would make every
    candidate look like an infinite saving."""
    with pytest.raises(RuntimeError, match="refusing to treat"):
        await _read(_initial_data(invoiceSummary={}))


# ── the fare model, from the review breakdown ────────────────────────────


def test_the_real_review_breakdown_reconciles():
    """DEMO08's /review step, read off the page:

        Cruise Rate              410.00
        Non-Comm Cruise Amount   458.00   -> Guest Subtotal   868.00
        Required Cruise Fees     376.78
        Government Taxes & Fees  146.61   -> TOTAL          1,391.39

    Proving this is what makes a Carnival comparison like-for-like: the
    wizard's prices are GROSS, directly comparable to
    invoiceSummary.grossAmount. Assuming it would have repeated the
    fabricated-saving mistakes in core/calculator.py's history.
    """
    cruise_rate, non_comm = 410.00, 458.00
    fees, taxes = 376.78, 146.61
    assert cruise_rate + non_comm == 868.00
    assert round(868.00 + fees + taxes, 2) == 1391.39


def test_commission_is_a_percentage_of_cruise_rate_only():
    """16% on DEMO08 (410 -> 65.60) but 15% on DEMO07 (742 -> 111.30), so
    the rate varies by booking and must never be hardcoded. Note it is
    charged on the Cruise Rate alone - the Non-Comm amount is carved out,
    the same shape as MSC's SRN and Princess's NCF."""
    assert round(410.00 * 0.16, 2) == 65.60
    assert round(742.00 * 0.15, 2) == 111.30
    assert round(1391.39 - 65.60, 2) == 1325.79      # net, as shown


def test_the_wizard_prices_are_PER_PERSON_not_per_booking():
    """CORRECTION, 2026-09-18. From DEMO08 alone I concluded the /rate and
    /category prices were gross totals comparable to
    invoiceSummary.grossAmount. That held only because DEMO08 has ONE guest,
    where per-person and total coincide.

    The 3-guest booking DEMO10 separates them:
        PHY BALCONY tile = 514.00,  category 8A = 514,
        booking total   = 1,542.00 = 514 x 3
    and the page states "average per person and based on single occupancy".

    Same sailing, three occupancies, from the live runs:
        1 guest -> 1,245    2 guests -> 1,354    3 guests -> 1,542
    Not a multiple of each other, because the PER-PERSON rate falls as
    occupancy rises - which is why the availability call carries
    amountOfGuests=N and the quote must never be scaled by hand.
    """
    per_person, guests = 514.00, 3
    assert per_person * guests == 1542.00

    # And the occupancy-dependent per-person rates behind the three totals.
    assert round(1245.00 / 1, 2) == 1245.00
    assert round(1354.00 / 2, 2) == 677.00
    assert round(1542.00 / 3, 2) == 514.00
    assert 1245.00 > 677.00 > 514.00        # per-person falls with occupancy


def test_a_single_guest_booking_cannot_distinguish_the_two():
    """WHY THE WRONG CONCLUSION WAS REACHABLE. On a one-guest booking the
    per-person price and the booking total are the same number, so no
    amount of care on DEMO08 alone could have told them apart. The fix was
    a second booking, not more reasoning about the first."""
    per_person, guests = 1392.00, 1
    assert per_person * guests == per_person


def test_the_category_price_attribute_is_truncated_not_rounded():
    """tr[data-cat="4A"] carried data-cat-price="1391" while that exact
    selection totalled 1,391.39 on /review. Up to $1 of error: fine for
    ranking candidates, never for a reported saving."""
    displayed, actual = 1391, 1391.39
    assert displayed == int(actual)
    assert actual - displayed > 0


def test_the_tile_price_is_a_teaser_not_the_bookable_price():
    """The OB7 tile advertised "From $1,392" where category 4A actually
    priced at 1,391 - the tiles are for shortlisting only."""
    tile_from, real_category_price = 1392.00, 1391.00
    assert tile_from != real_category_price


def test_the_captured_booking_is_a_price_increase_not_an_opportunity():
    """DEMO08 end to end: current gross 1,365.81 against the best
    available 1,391.39. The right answer is "no opportunity" - a scraper
    that reported a saving here would be inventing one."""
    current, best_available = 1365.81, 1391.39
    assert best_available > current
    assert round(best_available - current, 2) == 25.58


# ── the booking-engine link is identified by href, not text ──────────────


def test_the_booking_tool_is_matched_on_href_not_link_text():
    """Neon 2026-09-18, correcting me: the link he actually clicks is

        <a href="/BookingEngine/BookingSearch/SearchForReservations.aspx"
           target="_blank" class="secondary-link"
           data-gtm-event="group_staterooms">Individual/Groups Staterooms</a>

    The captured dashboard carries THREE links to that same href:
        individual_group_staterooms  "Individual/Group Staterooms"
        group_staterooms             "Individual/Groups Staterooms"
        the_fun_shops                "The Fun Shops"

    So both spellings of Group(s) are real - they are different links - and
    one of them is not about staterooms at all. Keying on text picks
    whichever is found first. The href is the same on all three.
    """
    from config.settings import settings

    sel = settings.goccl_booking_tool_selector
    assert "SearchForReservations.aspx" in sel
    assert "data-gtm-event" not in sel
    assert "Staterooms" not in sel


def test_the_selector_has_no_regex_slash_hazard():
    """get_by_role(name=re.compile("Individual/Group...")) fails outright:
    Playwright serialises the regex into /.../flags and the "/" in the link
    text closes the literal early -
        InvalidSelectorError: unexpected symbol "G" at position 22
    """
    from config.settings import settings

    assert not settings.goccl_booking_tool_selector.startswith("/")


# ── the guest count, finally confirmed ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_guest_count_comes_from_the_bookings_own_guest_list():
    """CLOSES A LONG-STANDING GAP. _probe_guest_count_candidates was written
    to detect the first time a candidate path resolved against real data. On
    2026-09-18 it fired live on booking DEMO08 with {"guests (count)": 1},
    and the portal's own request independently carried amountOfGuests=1.
    """
    out = await _read(_initial_data(guests=[{"sequenceNumber": 1, "age": 67}]))
    assert out["guests_count_actual"] == 1


@pytest.mark.asyncio
async def test_no_guest_list_yields_no_count_rather_than_zero():
    """A count of 0 would multiply every price to nothing."""
    out = await _read(_initial_data())
    assert out["guests_count_actual"] is None


def test_a_wrong_guest_count_doubles_the_price_not_nudges_it():
    """WHY THIS MATTERS. The /rate and /category quotes are per guest, so
    calculate_goccl multiplies by occupancy. Live on DEMO08 the assumed
    default of 2 turned a 1,392.00 quote into a 2,784.00 "new total" on a
    single-guest booking - not a rounding error, a doubling. With the real
    count it reads 1,392.00 against a 1,365.81 booking: NO saving, which is
    the correct answer."""
    quote, current = 1392.00, 1365.81
    assert quote * 2 == 2784.00          # what the assumption produced
    assert quote * 1 == 1392.00          # what the booking actually is
    assert quote > current               # still no saving, for the right reason


# ── ESPRESSO: the Exit dialog must actually be confirmed ─────────────────


def test_espresso_release_does_not_silently_skip_the_confirm_step():
    """Neon 2026-09-18: "the script is not pressong on exit i have to press
    it manually". The real control is

        <input type="button" id="acceptIgnoreReservation" class="submit" value="Exit">

    and release_booking waited 4s for it, then swallowed the timeout with
    `pass` ("some flows exit without the confirm step") and returned True
    anyway - logging a release that never happened while the dialog sat
    open and the reservation stayed locked for its full 15 minutes.

    Pinning the shape of the fix: the confirm must be waited for properly,
    and a dialog still on screen afterwards must NOT be reported as
    released.
    """
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper.release_booking)
    # the silent give-up is gone
    assert "Some flows exit without the confirm step" not in src
    # it verifies rather than assuming
    assert "still_open" in src
    assert "booking_release_unconfirmed" in src
    # and it no longer settles for 4 seconds
    assert "timeout=4000" not in src


def test_espresso_release_targets_the_VISIBLE_exit_button():
    """THE ACTUAL ROOT CAUSE. Confirmed 2026-09-18 across every captured
    categories page: ESPRESSO ships the Exit dialog TWICE, so the document
    contains two elements with id="acceptIgnoreReservation", each inside its
    own <div id="ignoreReservationPopup" style="display: none;">.

    Duplicate ids are invalid HTML, but the portal does it, and clicking the
    Exit link reveals only one of the two. Taking `.first` was therefore a
    coin flip: land on the wrong one and the wait times out against a node
    that never becomes visible, the click hits nothing, and the dialog stays
    open for a human to dismiss - exactly the reported symptom.

    Selecting on visibility instead of document order is what makes it
    deterministic.
    """
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper.release_booking)
    assert 'locator("visible=true")' in src or "visible=true" in src


def test_espresso_release_still_never_raises():
    """A failed release must not turn a good result into an error - the
    worst case is the lock expiring on its own, which was the old
    behaviour."""
    import inspect

    from scraper.espresso import EspressoScraper

    src = inspect.getsource(EspressoScraper.release_booking)
    assert "except Exception as exc:" in src
    assert "return False" in src
