"""Flag a price that moved since the previous scan.

Neon 2026-09-29: *"compare the new prices against the previous successful
scan and flag changes (price dropped / increased / unchanged). A price drop
is the repricing opportunity, so it should stand out."*

`price_history` has kept a row per scan all along - 6,606 of them - and
nothing ever compared two consecutive rows.
"""

import pytest

from core.price_change import (
    DROPPED,
    INCREASED,
    UNCHANGED,
    UNKNOWN,
    compare,
)


def test_a_drop_is_flagged():
    c = compare(6627.0, 5924.0)
    assert c.direction == DROPPED and c.is_drop
    assert c.delta == -703.0
    assert "DROPPED" in c.summary


def test_an_increase_is_flagged_but_is_not_an_opportunity():
    c = compare(5924.0, 6627.0)
    assert c.direction == INCREASED
    assert c.is_drop is False


def test_an_identical_total_is_unchanged():
    """Of 55 real repeats measured on 2026-09-29, 55 were identical."""
    c = compare(2109.0, 2109.0)
    assert c.direction == UNCHANGED and not c.is_drop


@pytest.mark.parametrize("delta", [0.0, 0.5, -0.99])
def test_noise_is_not_a_price_change(delta):
    """Rounding between currencies and a cent of tax reconciliation are not
    news. Reporting them would train the operator to ignore the column."""
    assert compare(1000.0, 1000.0 + delta).direction == UNCHANGED


def test_a_movement_just_over_the_floor_counts():
    assert compare(1000.0, 998.5).direction == DROPPED


# ── missing is not zero ──────────────────────────────────────────────────


def test_a_booking_never_seen_before_is_unknown_not_unchanged():
    """"No previous scan" and "the price did not move" are completely
    different facts. Reporting the first as UNCHANGED invents a history
    that does not exist."""
    c = compare(None, 5924.0)
    assert c.direction == UNKNOWN and c.delta is None
    assert "no previous scan" in c.summary


def test_an_unreadable_current_total_is_unknown():
    assert compare(5924.0, None).direction == UNKNOWN


def test_both_missing_is_unknown():
    assert compare(None, None).direction == UNKNOWN


def test_a_zero_previous_total_is_not_treated_as_missing():
    """0.0 is a real reading, however odd. Only None means "not recorded"."""
    assert compare(0.0, 100.0).direction == INCREASED


# ── wired into the scan ──────────────────────────────────────────────────


def test_the_previous_total_is_read_before_the_new_row_is_written():
    """Otherwise "previous" would be the row this very scan just wrote."""
    import inspect
    import io
    import tokenize

    from services.booking_service import BookingService
    src = inspect.getsource(BookingService._run_batch)
    code = tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT)
    assert code.index("_previous_total") < code.index("_save_price_history")


def test_a_drop_is_surfaced_on_the_row_itself():
    """It must stand out without anyone reading the log."""
    import inspect

    from services.booking_service import BookingService
    src = inspect.getsource(BookingService._run_batch)
    assert "PRICE DROP" in src


def test_the_comparison_never_breaks_a_scan():
    """A failed comparison costs a flag, not a booking result."""
    import inspect

    from services.booking_service import BookingService
    src = inspect.getsource(BookingService._run_batch)
    idx = src.index("compare_price")
    assert "except Exception" in src[idx:idx + 900]
