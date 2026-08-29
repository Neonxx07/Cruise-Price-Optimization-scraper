"""Regression tests for _ncl_addon_value's dollar-parsing fix
(2026-08-13 audit): the old regex `\\$(\\d+)` truncated decimals,
silently understating lost-addon value and overstating net_saving in
the wrong (non-conservative) direction."""
import pytest

from core.calculator import _ncl_addon_value


@pytest.mark.parametrize("name,expected", [
    ("$149", 149.0),
    ("$149.99", 149.99),
    ("$0.99", 0.99),
    ("$1,249.99", 1249.99),
    ("USD 149.99", 149.99),
])
def test_ncl_addon_value_parses_real_formats(name, expected):
    assert _ncl_addon_value(name) == expected


@pytest.mark.parametrize("name", [
    "", None, "$", "$abc", "Random addon with no dollar figure or known keyword",
])
def test_ncl_addon_value_malformed_input_never_crashes(name):
    assert _ncl_addon_value(name) == 0.0


def test_ncl_addon_value_falls_back_to_keyword_table_when_no_dollar_figure():
    assert _ncl_addon_value("Wi-Fi Package") == 150.0
    assert _ncl_addon_value("Specialty Dining Package") == 80.0


def test_regression_decimal_addon_value_no_longer_truncated():
    """CONFIRMED REAL BUG, fixed 2026-08-13: '$149.99 Beverage Package'
    used to parse as 149 (int), silently discarding the 99 cents."""
    assert _ncl_addon_value("$149.99 Beverage Package") == 149.99
    assert _ncl_addon_value("$149.99 Beverage Package") != 149
