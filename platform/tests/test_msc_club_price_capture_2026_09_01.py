"""Today's quote must carry the customer's own club discount.

THE ROOT CAUSE OF "ZERO OPTIMIZATIONS ON MSC", found 2026-09-01.

Staging deliberately never entered the Voyagers Club membership, to keep
`today_base_price` "undiscounted". But nothing removed the discount from
the OTHER side: `current_value` is the customer's real total with their
discount already baked in. So every MSC booking was judged as today's LIST
price against the customer's DISCOUNTED price - a comparison that can
almost never show a saving, on any booking, ever.

Booking 3000081, against Neon's own screenshot of the IR2 card:
    captured without the membership   $3,610.66 > $3,517.34 -> "no opportunity"
    the card with it applied          $3,435.36 < $3,517.34 -> a real $81.98

Neon chose to capture MSC's own discounted figure rather than compute one:
"Capture the discounted price directly ... Most faithful, since it's MSC's
own number rather than ours", driven off whether the customer actually
holds a membership - "specially if the customer has a vouygers same like we
see if the passenger customer is senior or not".
"""
import pytest

from core.calculator_msc import _check_price_match, evaluate_msc_booking


# -- the real booking, both ways ------------------------------------


def test_the_undiscounted_capture_used_to_hide_a_real_81_dollar_saving():
    """What the old flow produced on 3000081. The figure is not merely
    unhelpful - it is a confident NO on a booking that had a real saving."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=3610.66,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        customer_has_club_membership=False,
        today_price_includes_club_discount=False,
    )
    assert chk.status.value != "OPPORTUNITY"


def test_the_discounted_capture_finds_neons_81_dollars():
    """MSC's own card price, once the membership is entered."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=3435.36,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        customer_has_club_membership=True,
        today_price_includes_club_discount=True,
    )
    assert chk.status.value == "OPPORTUNITY"
    assert chk.estimated_value == pytest.approx(81.98, abs=0.01)


# -- the refusal that stops the bug coming back quietly -------------


def test_a_member_whose_discount_could_not_be_entered_is_refused():
    """If the customer holds a membership and staging could not enter it,
    today's price is a LIST price. Reporting NO_OPPORTUNITY from it would
    silently reinstate the original bug on that booking, so it refuses."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=3610.66,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        customer_has_club_membership=True,
        today_price_includes_club_discount=False,
        club_entry_note="MSC rejected the membership details",
    )
    assert chk.status.value == "INSUFFICIENT_DATA"
    assert chk.estimated_value is None
    assert "without it" in chk.note
    assert "MSC rejected the membership details" in chk.note


def test_a_customer_with_no_membership_prices_normally():
    """The guard keys off the CUSTOMER holding a membership, not off the
    discount being absent - otherwise every non-member booking would be
    refused and MSC would stop producing results entirely."""
    chk = _check_price_match(
        current_base_price=None, today_base_price=1000.0,
        current_total_price=3517.34, today_price_tab_confirmed=True,
        customer_has_club_membership=False,
        today_price_includes_club_discount=False,
    )
    assert chk.status.value == "OPPORTUNITY"


def test_the_guard_is_reachable_from_the_public_entry_point():
    """A guard the scraper cannot actually trigger is no guard at all -
    the same way msc_occupancy_is_trustworthy sat inert for a whole day."""
    result = evaluate_msc_booking(
        booking_id="3000081", category="IR2",
        current_base_price=None, today_base_price=3610.66,
        current_total_price=3517.34, current_discounts=[],
        today_discount_options=[], today_price_tab_confirmed=True,
        customer_has_club_membership=True,
        today_price_includes_club_discount=False,
        club_entry_note="no Voyagers Club control on this screen",
    )
    pm = {c.type.value: c for c in result.checks}["PRICE_MATCH"]
    assert pm.status.value == "INSUFFICIENT_DATA"
    assert "no Voyagers Club control" in pm.note


# -- the browser-side helper ----------------------------------------


def test_no_membership_on_the_booking_is_a_clean_skip_not_an_error():
    import asyncio

    from msc_commands import _apply_voyagers_club

    out = asyncio.run(_apply_voyagers_club(
        None, [{"name": "A B", "dob": "01/01/1950", "voyagers_number": None}]))
    assert out["applied"] is False
    assert "no passenger holds" in out["reason"]


def test_an_unreadable_member_name_refuses_rather_than_guessing():
    """Filling the modal with a wrong name fails MSC's validation and
    leaves the price silently undiscounted - worse than not trying."""
    import asyncio

    from msc_commands import _apply_voyagers_club

    out = asyncio.run(_apply_voyagers_club(
        None, [{"name": "Cher", "dob": "01/01/1950", "voyagers_number": "123"}]))
    assert out["applied"] is False
    assert "unreadable" in out["reason"]


def test_staging_records_whether_the_discount_actually_landed():
    """`applied` must never be inferred from "we clicked it" - it is set
    from MSC echoing the membership number back."""
    import inspect

    import msc_commands

    src = inspect.getsource(msc_commands._apply_voyagers_club)
    assert "verified" in src and "in body" in src, (
        "the outcome must be verified against the page, not assumed"
    )


# -- defects found by the 2026-09-01 full audit ---------------------


def test_a_zero_current_total_is_treated_as_not_captured():
    """FOUND IN THE FULL AUDIT. Bookings 3000076 and 3000012 both parse a
    "Value of the cruise" of exactly $0.00. No real cruise costs nothing,
    so the figure was simply not on the page - but every price comparison
    would happily measure today's price against zero and produce nonsense.
    """
    chk = _check_price_match(
        current_base_price=None, today_base_price=896.00,
        current_total_price=0.0, today_price_tab_confirmed=True,
    )
    assert chk.status.value != "OPPORTUNITY"
    assert chk.estimated_value is None


def test_a_real_total_still_prices():
    chk = _check_price_match(
        current_base_price=None, today_base_price=896.00,
        current_total_price=1200.00, today_price_tab_confirmed=True,
    )
    assert chk.status.value == "OPPORTUNITY"


def test_a_glob_can_never_be_captured_as_a_booking_id():
    """FOUND IN THE FULL AUDIT. booking_data.jsonl holds a record stored
    under the booking_id "/*" - a shell glob that reached a lookup command
    as if it were a booking number and had a full invoice captured against
    it, then sat in the corpus as a phantom booking that every per-booking
    count silently included."""
    from msc_commands import is_valid_msc_booking_id

    for bad in ("/*", "*", "", "   ", "abc", "7121 7377", "12345", "1234567890"):
        assert not is_valid_msc_booking_id(bad), f"{bad!r} was accepted"
    for good in ("3000081", "3000071", "123456", "123456789"):
        assert is_valid_msc_booking_id(good), f"{good!r} was rejected"


def test_the_batch_commands_filter_invalid_ids():
    """Validation that exists but is not applied at the entry point is how
    "/*" got in. Pin the call sites, not just the helper."""
    import inspect

    import msc_commands

    src = inspect.getsource(msc_commands)
    for cmd in ("batch_lookup:", "batch_check_today_rate:", "check_booking_batch:"):
        seg = src[src.index(f'command[len("{cmd}")'):][:200]
        assert "is_valid_msc_booking_id" in seg, f"{cmd} does not validate ids"
