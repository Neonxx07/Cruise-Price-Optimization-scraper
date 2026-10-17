"""Princess refare decisions, on the figures from Neon's 2026-10-15 captures.

Every quote below is a real PRICING DETAIL screen and closes exactly:
  DEMO33 KKS DF4  Fare 3,405.00 pp, Base 2,997.00, fees 213.03, taxes 194.97,
                  Gross 6,810.00; deposit due 15OCT26, final 28NOV26.
                  On CATEGORY FARE COMPARISONS the same DF4 under NSC (also
                  "CYBER SUMMER LTO - STANDARD") was 3,555.00 pp.
  DEMO32 KKS IC   Fare 1,329.00 + Transfers/Packages 88.00 = Gross 1,417.00
                  pp (2,834.00), Base 979.00, fees 170.22, taxes 179.78,
                  Onboard Credit 75.00 pp. The booking's landing Fare: 3,038.00.
  MN5VMM landing  Fare = Gross 2,862.00, Base 2,062.00, fees 540.76 + taxes
                  259.24 = 800.00; 200.00 received, Gross Payment Due 2,662.00.
"""
from datetime import date

from core.calculator_princess import (
    PrincessBooking,
    PrincessQuote,
    parse_polar_date,
)
from core.calculator_princess import decide as _decide

TODAY = date(2026, 10, 15)   # the captures' day - never the wall clock


def decide(booking, quote):
    return _decide(booking, quote, today=TODAY)
from core.princess_packages import PLUS, PREMIER, STANDARD


def _kks_df4(**kw):
    base = dict(promo_code="KKS", promo_description="CYBER SUMMER LTO - STANDARD",
                category="DF4", currency="USD", tier=STANDARD,
                fare_pp=[3405.0, 3405.0], gross_total=6810.0,
                base_pp=[2997.0, 2997.0], fees_pp=[213.03, 213.03],
                taxes_pp=[194.97, 194.97],
                deposit_due=parse_polar_date("15OCT26"), final_due=parse_polar_date("28NOV26"))
    base.update(kw)
    return PrincessQuote(**base)


def _on_nsc(**kw):
    """The same DF4 cabin on NSC - 3,555 pp from the comparison matrix."""
    base = dict(booking_id="DEMO33", gross=7110.0, category="DF4", currency="USD",
                tier=STANDARD, gross_payment_due=6910.0,
                deposit_due=date(2026, 10, 15), final_due=date(2026, 11, 28))
    base.update(kw)
    return PrincessBooking(**base)


def test_polar_dates():
    assert parse_polar_date("15OCT26") == date(2026, 10, 15)
    assert parse_polar_date("01MAY27") == date(2027, 5, 1)
    assert parse_polar_date("2026-10-15") is None


def test_the_real_quotes_add_up():
    assert _kks_df4().reconciles() == (True, "")
    mn6lxv = PrincessQuote(
        promo_code="KKS", promo_description="CYBER SUMMER LTO - STANDARD", category="IC",
        currency="USD", tier=STANDARD, fare_pp=[1329.0, 1329.0], gross_total=2834.0,
        base_pp=[979.0, 979.0], fees_pp=[170.22, 170.22], taxes_pp=[179.78, 179.78],
        transfers_pp=[88.0, 88.0], obc_pp=[75.0, 75.0])
    assert mn6lxv.reconciles() == (True, "")


def test_same_package_same_cabin_cheaper_promo_is_a_saving():
    v = decide(_on_nsc(), _kks_df4())
    assert v.status == "OPTIMIZATION"
    assert v.saving == 300.0


def test_a_different_package_is_never_a_saving():
    """Standard against Plus is two products, not two prices."""
    v = decide(_on_nsc(tier=PLUS), _kks_df4())
    assert v.status == "NO_SAVING"
    assert "different packages" in v.note


def test_an_unknown_current_package_refuses():
    v = decide(_on_nsc(tier=None), _kks_df4())
    assert v.status == "NO_SAVING"
    assert "could not be read" in v.note


def test_another_category_is_not_the_bookings_cabin():
    v = decide(_on_nsc(category="DE4"), _kks_df4())
    assert v.status == "NO_SAVING" and "category" in v.note


def test_a_quote_that_does_not_add_up_is_an_error_not_a_saving():
    v = decide(_on_nsc(), _kks_df4(gross_total=6000.0))
    assert v.status == "ERROR"


def test_paid_in_full_is_never_repriced():
    assert decide(_on_nsc(gross_payment_due=0.0), _kks_df4()).status == "PAID_IN_FULL"


def test_onboard_credit_is_not_cash():
    """DEMO32's quote carries 150.00 onboard credit. A dearer quote with
    credit is still dearer."""
    v = decide(_on_nsc(gross=6700.0), _kks_df4(obc_pp=[75.0, 75.0]))
    assert v.status == "NO_SAVING"
    assert "not cash" in v.note


def test_giving_up_onboard_credit_needs_three_times_the_cash():
    v = decide(_on_nsc(gross=6900.0, obc=100.0), _kks_df4())  # 90 cash for 100 OBC
    assert v.status == "NO_SAVING" and "3x" in v.note
    v = decide(_on_nsc(gross=7200.0, obc=100.0), _kks_df4())  # 390 cash for 100 OBC
    assert v.status == "OPTIMIZATION"


def test_dropping_the_bookings_packages_is_a_loss():
    v = decide(_on_nsc(packages=176.0), _kks_df4())
    assert v.status == "NO_SAVING" and "nothing may be lost" in v.note


def test_added_transfers_stay_inside_the_gross_and_are_named():
    """DEMO32 shape: the quote adds 88.00 pp transfers - compared on gross,
    so the saving already pays for them."""
    q = _kks_df4(fare_pp=[3317.0, 3317.0], transfers_pp=[88.0, 88.0], gross_total=6810.0,
                 base_pp=[2909.0, 2909.0])
    v = decide(_on_nsc(), q)
    assert v.status == "OPTIMIZATION" and v.saving == 300.0
    assert "adds transfers/packages of 176.00" in v.note


def test_an_earlier_final_payment_is_flagged_not_refused():
    """Neon 2026-10-15: Princess payment dates are "a thing we keep an eye
    on but it is not as madnatory as carnival" - flagged, never blocking."""
    v = decide(_on_nsc(final_due=date(2026, 12, 15)), _kks_df4())
    assert v.status == "OPTIMIZATION" and v.saving == 300.0
    assert v.note.startswith("WATCH PAYMENT DATES") and "EARLIER" in v.note


def test_a_premier_booking_is_compared_only_with_premier():
    v = decide(_on_nsc(tier=PREMIER), _kks_df4(tier=PREMIER))
    assert v.status == "OPTIMIZATION"


# -- the category row (Neon 2026-10-15: "i press find and put b4 and choose
#    whatever begins with b4"; Princess's page: value = substring(0,2)) ---------

# The 35 rows on DEMO12's CATEGORY FARES page, in order, as displayRow() got them.
DEMO12_ROWS = ['S2+', 'S33', 'S44', 'S53', 'S64', 'S88', 'M14', 'MB4', 'MD4', 'ME4',
               'B14', 'B2+', 'B43', 'BA+', 'BB+', 'BC+', 'BD+', 'BE ', 'BF ', 'OB ',
               'OC4', 'OE ', 'OF4', 'OV4', 'OW4', 'OY4', 'OZ4', 'IA4', 'IB4', 'IC4',
               'ID4', 'IE4', 'IF4', 'Y  ', '   ']


def test_the_row_is_the_one_that_begins_with_the_category():
    from core.calculator_princess import find_category_row, polar_category
    assert polar_category("B43") == "B4" and polar_category("DF4") == "DF"
    assert find_category_row(DEMO12_ROWS, "B4") == ("B43", "")
    assert find_category_row(DEMO12_ROWS, "be") == ("BE ", "")
    # MB4 CONTAINS B4 but does not begin with it - never matched
    assert find_category_row(DEMO12_ROWS, "B4")[0] != "MB4"


def test_no_row_or_two_rows_refuse():
    from core.calculator_princess import find_category_row
    row, why = find_category_row(DEMO12_ROWS, "DF")
    assert row is None and "not offered" in why
    row, why = find_category_row(["B43", "B41"], "B4")
    assert row is None and "cannot be told" in why


def test_a_quote_row_code_matches_the_bookings_two_letter_category():
    v = decide(_on_nsc(category="DF"), _kks_df4())      # booking DF, quote row DF4
    assert v.status == "OPTIMIZATION"


# -- which package the booking holds (Neon: plus -> plus rate, premier ->
#    premier rate, standard -> standard rate) ----------------------------------

def test_demo12_is_a_plus_booking():
    """DEMO12: Special Requests "Princess Plus" x2, offer code PLUS2, promos
    RG1 (LAST MINUTE DEALS - STANDARD (NBO)) + M1E (UPGRADE STANDARD TO PLUS
    70). The Plus upgrade outranks the Standard promo underneath it."""
    from core.calculator_princess import booking_tier
    assert booking_tier(["PLUS2"], ["Tray of Assorted Canapes", "Princess Plus ", "Princess Plus "],
                        ["LAST MINUTE DEALS - STANDARD (NBO)", "UPGRADE STANDARD TO PLUS 70"]) == PLUS


def test_each_signal_alone_is_enough():
    from core.calculator_princess import booking_tier
    assert booking_tier(offer_codes=["PLUS2"]) == PLUS
    assert booking_tier(offer_codes=["PLS2PREM"]) == PREMIER
    assert booking_tier(special_requests=["Princess Premier"]) == PREMIER
    assert booking_tier(promo_descriptions=["CYBER SUMMER LTO - STANDARD"]) == STANDARD


def test_nothing_said_is_unknown_not_standard():
    from core.calculator_princess import booking_tier
    assert booking_tier([], ["Anniversary/Pax Celebrate Onboard"], []) is None


def test_a_quote_with_different_vacation_protection_is_not_comparable():
    """POLAR help: Standard coverage "is usually added automatically"."""
    v = decide(_on_nsc(), _kks_df4(vacation_protection_pp=[99.0, 99.0], gross_total=7008.0))
    assert v.status == "NO_SAVING" and "vacation protection" in v.note


# -- 2026-10-15 live read-only run: what a refare can and cannot touch --------

def test_air_mods_and_services_are_not_the_fare():
    """DEMO27: Gross 3,309.22 = Fare 1,876.00 + Air 1,433.22. A fare quote
    without air must be set against 1,876.00, never against the gross."""
    booking = PrincessBooking(booking_id="DEMO27", gross=3309.22, fare=1876.0,
                              category="IC", currency="USD", tier=STANDARD,
                              gross_payment_due=3111.22, final_due=date(2026, 11, 28))
    quote = _kks_df4(category="IC", fare_pp=[900.0, 900.0], gross_total=1800.0,
                     base_pp=[], fees_pp=[], taxes_pp=[], deposit_due=None, final_due=None)
    v = decide(booking, quote)
    # POLAR's quote shows no payment dates: the note says to look (Neon
    # 2026-10-15 - a watch item on Princess, not a block).
    assert v.status == "OPTIMIZATION" and v.saving == 76.0      # 1,876 - 1,800, not 1,509.22
    assert "check the payment dates" in v.note and "unconfirmed" not in v.note.lower()


def test_past_final_payment_is_never_repriced():
    """Princess's own notice on DEMO12 (final due 14OCT26): a change leaving a
    balance is cancelled automatically if unpaid."""
    v = decide(_on_nsc(final_due=date(2026, 10, 14)), _kks_df4())
    assert v.status == "PAST_FINAL_PAYMENT"


def test_mods_sit_inside_the_quotes_gross():
    """DEMO24, KLS live 2026-10-15: Fare 4,237 + Mods 48 = Gross Fare 4,285
    per guest (8,570 for two). The quote adds up; Mods are not the fare."""
    q = _kks_df4(category="BC", fare_pp=[4237.0, 4237.0], mods_pp=[48.0, 48.0],
                 gross_total=8570.0, base_pp=[], fees_pp=[], taxes_pp=[])
    assert q.reconciles() == (True, "")
    assert q.cruise_total == 8474.0
    booking = PrincessBooking(booking_id="DEMO24", gross=7610.0, fare=7514.0, category="BC",
                              currency="USD", tier=STANDARD, gross_payment_due=7410.0,
                              final_due=date(2027, 4, 12))
    v = decide(booking, q)
    assert v.status == "NO_SAVING"                         # 8,474 today vs 7,514 booked


def test_giving_up_onboard_credit_is_said_even_when_the_3x_rule_passes():
    v = decide(_on_nsc(gross=7200.0, obc=100.0), _kks_df4())    # 390 cash for 100 OBC
    assert v.status == "OPTIMIZATION" and "gives up 100.00 onboard credit" in v.note
