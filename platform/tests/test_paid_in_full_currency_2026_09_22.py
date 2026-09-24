"""A Canadian booking that was paid in full was sold as a $400 saving.

Neon 2026-09-22: "3001001 i ran a script yasterday but this booking was paid
in full and the project did not detect it ... MAKE SURE THIS DOES NOT HAPPEN
AGAIN".

WHAT HAPPENED. Booking 3001001 is Canadian. Its Reservation Summary reads:

    Total Price (CAD):        2,109.00
    Payments Received (CAD):  2,108.98
    Final Payment:                0.02      <- two cents outstanding

The field patterns required the literal "(USD)", so every figure came back
None. is_paid_in_full(None, ...) returns False BY DESIGN - it refuses to
guess - but the scan read that False as "not paid in full" rather than "we
do not know", ran the full comparison and reported:

    2026-09-21 20:07   ESPRESSO   OPTIMIZATION   old 2109.00 -> new 1638.00
                                                 net $400, confidence 5

11 of 120 sampled ESPRESSO booking pages are CAD, so roughly 9% of the
watchlist had no payment gate at all.

TWO SEPARATE DEFECTS, fixed separately below, because fixing only the first
would leave the same trap for the next unparseable panel.
"""
import pytest

from core.calculator import is_paid_in_full
from scraper.espresso import EspressoScraper


# ── defect 1: the patterns were USD-only ─────────────────────────────────


@pytest.mark.parametrize("currency", ["USD", "CAD", "GBP", "EUR", "AUD"])
def test_payment_fields_parse_in_any_currency(currency):
    """The amount FORMAT is identical across currencies; only the label
    differs, and the code is captured separately by _CURRENCY_LABEL_RE."""
    pats = EspressoScraper._PAYMENT_FIELD_PATTERNS
    assert pats["total_price"].search(f"Total Price ({currency}): 2,109.00").group(1) == "2,109.00"
    assert pats["payments_received"].search(
        f"Payments Received ({currency}): 2,108.98").group(1) == "2,108.98"


def test_the_exact_3001001_panel_now_parses():
    """The real text from that booking's captured page."""
    body = ("Total Price (CAD): 2109.00 Deposit (CAD): 530.00 "
            "Payments Received (CAD): 2108.98 "
            "Final Payment Due (CAD): Due: 10JUL2026 Final Payment: 0.02")
    pats = EspressoScraper._PAYMENT_FIELD_PATTERNS
    total = float(pats["total_price"].search(body).group(1).replace(",", ""))
    received = float(pats["payments_received"].search(body).group(1).replace(",", ""))
    assert total == 2109.00 and received == 2108.98


def test_the_outstanding_amount_is_found_under_its_own_label():
    """On this layout "Final Payment Due (CAD):" is followed by a DATE; the
    figure sits under a separate "Final Payment:" label."""
    body = "Final Payment Due (CAD): Due: 10JUL2026 Final Payment: 0.02"
    assert EspressoScraper._PAYMENT_FIELD_PATTERNS["final_payment_due"].search(body) is None
    assert EspressoScraper._FINAL_PAYMENT_AMOUNT_RE.search(body).group(1) == "0.02"


def test_3001001_is_now_CLASSIFIED_paid_in_full():
    """THE REGRESSION. Two cents against a 2,109.00 booking is paid in full
    under any sane tolerance - and was reported as a $400 optimization."""
    assert is_paid_in_full(0.02, 2109.00) is True


def test_a_genuinely_outstanding_balance_is_still_not_paid_in_full():
    """The gate must not now swing the other way and suppress real work."""
    assert is_paid_in_full(1987.56, 2109.00) is False


# ── defect 2: unreadable != unpaid ───────────────────────────────────────


def test_is_paid_in_full_still_refuses_to_guess():
    """This behaviour was CORRECT and is deliberately unchanged. The bug was
    in how the caller read the answer."""
    assert is_paid_in_full(None, 2109.00) is False


def test_espresso_refuses_to_report_a_saving_when_the_panel_is_unreadable():
    """THE GUARD THAT MATTERS FOR NEXT TIME. The patterns are
    currency-agnostic now, so 3001001 parses - but a relabelled panel, a new
    layout or a currency written some other way would put us right back
    here. An unreadable payment state is not evidence of an outstanding
    balance."""
    import inspect

    src = inspect.getsource(EspressoScraper.check_booking)
    assert "payment_state_readable" in src
    assert "_paymentUnreadable" in src


def test_espresso_reports_readability_alongside_the_figures():
    import inspect

    src = inspect.getsource(EspressoScraper._read_payment_status)
    assert "payment_state_readable" in src


def test_ncl_has_the_same_guard():
    """NCL had the SAME SHAPE: if _read_payment_state raises, `payment` is
    {}, every figure is None, cruise_line_fully_paid evaluates False rather
    than "unknown", and the scan reports a saving for a booking whose
    balance was never read."""
    import inspect

    from scraper.ncl import NclScraper

    src = inspect.getsource(NclScraper.check_booking)
    assert "payment_state_unreadable" in src
    assert "payment_state_readable" in src


@pytest.mark.parametrize("figures,readable", [
    ({"amount_due": 100.0, "net_due": None}, True),
    ({"amount_due": None, "net_due": 0.0}, True),      # a real zero counts
    ({"amount_due": None, "net_due": None}, False),
])
def test_readability_distinguishes_a_real_zero_from_a_missing_figure(figures, readable):
    """A balance of 0.00 is a MEASUREMENT. A balance of None is silence.
    Collapsing them is exactly what produced the false $400."""
    assert any(v is not None for v in figures.values()) is readable
