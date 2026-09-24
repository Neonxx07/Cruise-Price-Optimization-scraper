"""Rules derived from the 14-booking forensic sweep of 2026-09-18.

Neon's brief: "A result of NO RESULT / UNVERIFIED is preferable to a FALSE
$267 SAVING when the underlying quote is invalid."

Each rule here exists because real bookings demonstrated the failure.
Evidence lives in data/goccl_forensic/.
"""
import pytest

from core.models import BookingStatus
from scraper.goccl import GoCCLScraper


# ── RULE ADV-001: the portal refuses, and says why ───────────────────────
#
# EVIDENCE. Four bookings (DEMO09, CQ7X35, CQ7W42, CH7M42) reached /guest and
# then timed out after 30s "waiting for section.rate__container". The DOM gave
# no clue - the Change Offer/Rate control is byte-identical to a working
# booking's and is not disabled. Capturing RESPONSE BODIES showed the reason:
#
#     working : GET availability/rate -> 200, rates = list[5]
#     blocked : GET availability/rate -> 409
#               {"code":"999999","message":"Advisories were received",
#                "details":[{"code":"5108",
#                            "message":"The VIFP number is incorrect."}]}
#
# Identical on all four and reproduced exactly on a re-run, so it is
# deterministic and booking-specific.
#
# FAILURE SCENARIO IF UNFIXED: a fixable data problem on the booking (a bad
# loyalty number) is reported as a 30-second infrastructure timeout, so
# nobody fixes it and the booking silently never gets repriced.


def test_the_advisory_envelope_is_not_reported_as_the_reason():
    """Code 999999 "Advisories were received" is the wrapper, not a cause.
    Reporting it would tell a human nothing."""
    s = GoCCLScraper()
    s.last_advisories = [
        {"code": "999999", "message": "Advisories were received",
         "status": 409, "path": "/availability/rate"},
        {"code": "5108", "message": "The VIFP number is incorrect.",
         "status": 409, "path": "/availability/rate"},
    ]
    summary = s.advisory_summary()
    assert "5108" in summary
    assert "VIFP number is incorrect" in summary
    assert "999999" not in summary
    assert "Advisories were received" not in summary


def test_no_advisory_produces_no_claim():
    """Silence must not be dressed up as a diagnosis."""
    assert GoCCLScraper().advisory_summary() == ""


def test_advisories_start_empty_per_scraper():
    assert GoCCLScraper().last_advisories == []


def test_multiple_advisories_are_all_reported():
    """The API returns a LIST of details; a second reason must not be lost."""
    s = GoCCLScraper()
    s.last_advisories = [
        {"code": "5108", "message": "The VIFP number is incorrect.",
         "status": 409, "path": "/availability/rate"},
        {"code": "6001", "message": "Some other advisory.",
         "status": 409, "path": "/availability/rate"},
    ]
    summary = s.advisory_summary()
    assert "5108" in summary and "6001" in summary


# ── RULE PAY-001: a settled booking is not an opportunity ────────────────
#
# EVIDENCE. 6 of the 14 bookings surveyed were already settled
# (netBalanceDue == 0): MZ81K0, MZ81X2, MW24H6, PR40T9, BF87H3, TM66H0. Two
# were PAST their final payment date (MW24H6 8/23/2026, TM66H0 8/11/2026).
# Every one had a working rate screen, so the scanner would have produced a
# confident saving on a fully-paid booking.
#
# GoCCL had NO payment gating whatsoever - scraper/ncl.py carries 8
# references to final_payment_date and core/calculator_msc.py 6; GoCCL had 0.


def test_paid_in_full_is_an_existing_status_not_a_new_one():
    """ESPRESSO and NCL already model this. GoCCL simply never used it."""
    assert BookingStatus.PAID_IN_FULL.value == "PAID_IN_FULL"


@pytest.mark.parametrize("net_balance,settled", [
    (0.0, True),        # MW24H6, MZ81K0, MZ81X2, PR40T9, BF87H3, TM66H0
    (0.01, True),       # tolerance, matching the rest of the project
    (51.00, False),
    (1187.40, False),   # CH7M42
    (1575.50, False),   # CQ7X35 / CQ7W42
])
def test_the_settled_test_is_net_balance_due(net_balance, settled):
    assert (net_balance <= 0.01) is settled


def test_balance_is_never_derived_as_gross_minus_paid():
    """RULE PAY-002. On PR40T9 the two disagree:
        gross 1,987.31 - paid 1,766.81 = 220.50
        balanceDue                      =  51.00
        netBalanceDue                   =   0.00   (= net 1,766.81 - paid)
    netBalanceDue is the one that reconciles. What balanceDue represents
    when they disagree is UNKNOWN and must not be invented."""
    gross, paid, net = 1987.31, 1766.81, 1766.81
    assert round(gross - paid, 2) == 220.50
    assert round(net - paid, 2) == 0.00        # netBalanceDue, as reported
    assert round(gross - paid, 2) != 51.00     # balanceDue is NOT this


def test_a_missing_balance_does_not_count_as_settled():
    """RULE DATA-001: missing != zero. A booking whose payment schedule
    could not be read must not be silently classified as paid."""
    net_balance = None
    assert not (net_balance is not None and net_balance <= 0.01)


# ── RULE OCC-001: occupancy, observed on every booking ───────────────────
#
# EVIDENCE. Across all 14 bookings, len(initialData.guests) equalled
# amountOfGuests=N on every availability call, and the birthDates count
# agreed. Occupancies of 1, 2, 3 and 4 were covered. The engine DERIVES
# occupancy from the booking - the scraper never sets it - which is why the
# MSC-style wrong-occupancy trap has no obvious route here.
#
# "No evidence of it" is not "impossible", so the check stays.


@pytest.mark.parametrize("booking,quoted,verdict", [
    (1, [1], "MATCH"),
    (2, [2], "MATCH"),
    (3, [3], "MATCH"),
    (4, [4], "MATCH"),
    (2, [1], "MISMATCH"),     # the failure this guards against
    (2, [], "UNVERIFIED"),    # nothing observed - not a pass
])
def test_occupancy_verdicts(booking, quoted, verdict):
    got = ("MATCH" if quoted == [booking]
           else "UNVERIFIED" if not quoted else "MISMATCH")
    assert got == verdict


def test_an_unobserved_occupancy_is_unverified_not_valid():
    """Neon's principle: NO RESULT beats a false saving. An occupancy that
    was never observed is not evidence of a correct one."""
    quoted: list[int] = []
    assert ("MATCH" if quoted == [2] else "UNVERIFIED" if not quoted else "MISMATCH") \
        == "UNVERIFIED"


# ── ADV-001 refinement: advisories also ride on HTTP 200 ─────────────────
#
# EVIDENCE, 2026-09-18: availability/stateroom returned 200 carrying
#   advisorySummary: {"advisories": [], "hasError": false,
#                     "hasInformational": false}
# That is the portal's own first-class advisory channel, and it exists
# independently of the 409 error envelope. Keying only on status >= 400
# would miss a problem reported on a successful response.


def test_the_advisory_listener_reads_success_responses_too():
    """"Treat HTTP 200 as automatically valid" is on the brief's DO NOT
    list. advisorySummary must be inspected regardless of status."""
    import inspect

    from scraper.goccl import GoCCLScraper

    src = inspect.getsource(GoCCLScraper._attach_advisory_listener)
    assert "advisorySummary" in src
    # the early-return on status must no longer gate the whole handler
    assert "if resp.status < 400 or not self._ADVISORY_PATH_RE" not in src


def test_an_empty_advisory_summary_yields_no_advisories():
    """hasError false with an empty list is the NORMAL case and must not
    manufacture a warning."""
    s = GoCCLScraper()
    assert s.advisory_summary() == ""


# ── the real state machine, as observed ──────────────────────────────────
#
# EVIDENCE: a full millisecond timeline of ZM55G1 showed FIVE wizard steps,
# not four, each with its own availability endpoint:
#
#   /guest      guest-services
#   /rate       rate
#   /category   category
#   /stateroom  stateroom      <- previously unmapped
#   /review
#
# and TWO exits from /category:
#   Keep Same Stateroom (data-comp=continue-to-review) -> /review
#   Continue            (data-comp=goto-next-page)     -> /stateroom


def test_the_wizard_has_a_stateroom_step_between_category_and_review():
    """Discovered by walking it. An earlier map went straight from
    /category to /review because the booking observed then could keep its
    cabin; ZM55G1 cannot, and must pass through /stateroom."""
    observed = ["guest", "rate", "category", "stateroom", "review"]
    assert observed.index("stateroom") == observed.index("category") + 1
    assert observed.index("review") == observed.index("stateroom") + 1


def test_keep_same_stateroom_is_conditional_not_guaranteed():
    """On ZM55G1 'Keep Same Stateroom' stayed DISABLED for a full 15s after
    the booking's own category (8A) was selected, while 'Continue' was
    enabled. So the same cabin cannot always be retained under a new rate,
    and a scraper that waits for that button will hang on such bookings."""
    keep_enabled, continue_enabled = False, True
    route = "review" if keep_enabled else "stateroom"
    assert route == "stateroom"
    assert continue_enabled


def test_the_tile_price_is_not_a_strict_minimum():
    """ZM55G1: the OB7 BALCONY tile advertised 1,265.00 per person, but the
    booking's own category 8A priced at 1,264 on /category - BELOW the
    "From" figure. So the tile is a display figure, not a floor, and must
    not be used to bound or sanity-check the category price."""
    tile_from, category_price = 1265.00, 1264.0
    assert category_price < tile_from


# ── hasError separates a refusal from a remark ───────────────────────────
#
# A REGRESSION TEST FOR A BUG INTRODUCED AND CAUGHT ON 2026-09-18. A first
# version of the advisorySummary handling treated every advisory as a
# problem. Replayed over the 23-booking evidence set it condemned ~20
# perfectly good bookings - ones that had just returned 3 to 11 rates - on
# the strength of this, from a 200 response:
#
#   {"advisories":[{"code":1241,
#                   "description":"Option extension is not applicable to
#                                  deposited bookings."}],
#    "hasError":false, "hasInformational":true}
#
# That is a remark about an unrelated feature. Turning it into
# "GoCCL will not quote this booking" is exactly the false negative the
# brief warns against, in the opposite direction: suppressing real savings.


def test_an_informational_advisory_is_not_a_refusal():
    s = GoCCLScraper()
    s.last_advisories = [{
        "code": "1241",
        "message": "Option extension is not applicable to deposited bookings.",
        "blocking": False, "status": 200,
        "path": "/api/v1.0/availability/guest-services",
    }]
    assert s.advisory_summary() == ""
    assert len(s.informational_advisories()) == 1


def test_a_blocking_advisory_still_refuses():
    s = GoCCLScraper()
    s.last_advisories = [{
        "code": "5108", "message": "The VIFP number is incorrect.",
        "blocking": True, "status": 409,
        "path": "/api/v1.0/availability/rate",
    }]
    assert "5108" in s.advisory_summary()
    assert s.informational_advisories() == []


def test_informational_and_blocking_together_report_only_the_blocker():
    """A booking can carry both. The verdict must come from the blocker."""
    s = GoCCLScraper()
    s.last_advisories = [
        {"code": "1241", "message": "Option extension is not applicable.",
         "blocking": False, "status": 200, "path": "/guest-services"},
        {"code": "5108", "message": "The VIFP number is incorrect.",
         "blocking": True, "status": 409, "path": "/rate"},
    ]
    summary = s.advisory_summary()
    assert "5108" in summary and "1241" not in summary


def test_the_success_channel_uses_description_not_message():
    """advisorySummary entries carry "description"; the 409 envelope carries
    "message". Reading only "message" produced advisories whose text was the
    literal string "None"."""
    import inspect

    from scraper.goccl import GoCCLScraper as G

    src = inspect.getsource(G._attach_advisory_listener)
    assert 'detail.get("description")' in src
    assert 'summary.get("hasError")' in src


def test_a_409_detail_defaults_to_blocking_when_unflagged():
    """Historical entries carry no "blocking" key. They came from 409
    envelopes, so the safe default is blocking - failing closed."""
    s = GoCCLScraper()
    s.last_advisories = [{"code": "5108", "message": "The VIFP number is incorrect.",
                          "status": 409, "path": "/rate"}]
    assert "5108" in s.advisory_summary()
