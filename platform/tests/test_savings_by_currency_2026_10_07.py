"""Canadian dollars were being added to American ones.

`total_optimization_savings` sums every `net_saving` together and every
caller prints a `$` in front of the answer. Its own docstring flagged this
on 2026-08-13 and deferred it, for a good reason: the obvious fix - filter
to USD - would SILENTLY DROP results from a total the operator already
relies on, which is a worse error than the one being fixed.

The live database says it is not hypothetical. OPTIMIZATION rows:

    USD      73    $6,914.00
    CAD      35     5,923.00 CAD     <- added to the dollar figure
    UNKNOWN  51     6,933.26
    (unset)  80    13,053.81

ESPRESSO alone carries 2,992 USD and 623 CAD rows, so even a single
cruise-line tab mixes them.

SPLITTING DROPS NOTHING. Every result still appears, under the currency it
was actually recorded in, and one whose currency was never established
appears as UNKNOWN instead of being quietly counted as dollars.

The display stays quiet when there is nothing to disambiguate - a
single-currency total reads exactly as it always has, because naming the
currency when only one is present is noise.
"""

from core.calculator import format_savings_by_currency, savings_by_currency
from core.models import BookingStatus, CruiseLine


class _R:
    """Minimal stand-in: savings_by_currency reads status, net_saving,
    currency and note."""

    def __init__(self, net_saving, currency, status=BookingStatus.OPTIMIZATION,
                 note=""):
        self.status = status
        self.net_saving = net_saving
        self.currency = currency
        self.note = note
        self.cruise_line = CruiseLine.ESPRESSO


# -- the split ------------------------------------------------------------


def test_two_currencies_are_kept_apart():
    totals = savings_by_currency([_R(100.0, "USD"), _R(50.0, "CAD")])
    assert totals == {"USD": 100.0, "CAD": 50.0}


def test_nothing_is_dropped():
    """The 2026-08-13 objection to filtering. Every dollar still appears
    somewhere."""
    rows = [_R(100.0, "USD"), _R(50.0, "CAD"), _R(7.0, None), _R(3.0, "UNKNOWN")]
    totals = savings_by_currency(rows)
    assert round(sum(totals.values()), 2) == 160.0


def test_an_unrecorded_currency_is_not_counted_as_dollars():
    assert savings_by_currency([_R(7.0, None)]) == {"UNKNOWN": 7.0}


def test_currency_codes_are_normalised():
    assert savings_by_currency([_R(1.0, " usd "), _R(2.0, "USD")]) == {"USD": 3.0}


def test_only_optimizations_count():
    """Unchanged from total_optimization_savings: a NO_SAVING row carries a
    signed net_saving and must never be summed in."""
    rows = [_R(100.0, "USD"), _R(-250.0, "USD", status=BookingStatus.NO_SAVING)]
    assert savings_by_currency(rows) == {"USD": 100.0}


def test_self_declared_unconfirmed_rows_stay_excluded():
    """$4,100 of an all-time total was once unverified GoCCL candidates."""
    rows = [_R(100.0, "USD"),
            _R(900.0, "USD", note="UNCONFIRMED, run preview_fare_code")]
    assert savings_by_currency(rows) == {"USD": 100.0}


def test_no_results_is_distinguishable_from_no_money():
    assert savings_by_currency([]) == {}


# -- the display ----------------------------------------------------------


def test_a_single_dollar_total_reads_exactly_as_before():
    assert format_savings_by_currency({"USD": 6914.0}) == "$6,914.00 confirmed savings"


def test_a_single_foreign_total_names_its_currency():
    out = format_savings_by_currency({"CAD": 5923.0})
    assert "CAD" in out and "$" not in out


def test_an_unknown_currency_is_never_given_a_dollar_sign():
    out = format_savings_by_currency({"UNKNOWN": 120.0})
    assert "$" not in out
    assert "not recorded" in out


def test_mixed_currencies_are_both_named():
    out = format_savings_by_currency({"USD": 6914.0, "CAD": 5923.0})
    assert "$6,914.00" in out
    assert "5,923.00 CAD" in out
    assert out.count("confirmed savings") == 1


def test_unknown_is_listed_last_so_the_real_money_leads():
    out = format_savings_by_currency({"UNKNOWN": 12.0, "USD": 100.0, "CAD": 50.0})
    assert out.index("UNKNOWN") > out.index("CAD")
    assert out.index("UNKNOWN") > out.index("$100.00")


def test_empty_is_a_zero_not_a_crash():
    assert format_savings_by_currency({}) == "$0.00 confirmed savings"


# -- the GUI uses it ------------------------------------------------------


def test_both_summaries_are_currency_aware():
    """The per-line summary and the across-all-lines footer. ESPRESSO alone
    mixes USD and CAD, so fixing only the footer would leave the tab the
    operator actually watches still wrong."""
    import pathlib

    src = pathlib.Path("gui/windows.py").read_text(encoding="utf-8")
    assert src.count("savings_by_currency(") >= 2, (
        "one of the two savings summaries still sums across currencies")
    assert "total_optimization_savings" not in src, (
        "a currency-blind total is back in the GUI")
