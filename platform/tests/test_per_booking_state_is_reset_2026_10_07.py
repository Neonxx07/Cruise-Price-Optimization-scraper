"""A scraper must forget the previous booking before it starts the next.

CONFIRMED IN THE LIVE DATABASE. 21 distinct `ncl_booking_details` payloads
are stored against **49 different bookings** - byte-identical payment
panels, same final-payment date and same amount, filed under two or three
booking ids:

    bookings ['3000058', '3000060']  finalPaymentDate 08/27/2026  amount 1138
    bookings ['3000056', '3001050']  finalPaymentDate 11/14/2026  amount 1714
    bookings ['3001048', '3001049']  finalPaymentDate 10/20/2026  amount 3988

**Every single group pairs a real booking with a NOT_ON_THIS_ACCOUNT one**,
and it repeats in every scan since the capture was added.

THE MECHANISM. A scraper object outlives the booking it scrapes - one
instance checks hundreds in a row - so anything cached on `self` survives
into the next booking. BookingService then reads several of those
attributes AFTER check_booking returns and writes them keyed to THAT
booking:

    await self._save_market_data_to_db(result, scraper.last_market_data)
    extract_booking_features(..., market_data=..., initial_data=...)

NclScraper.check_booking sets `last_market_data` at the very END, after
several early returns. A booking that takes one of those returns keeps the
previous booking's panel and has it written under its own id.

WHY IT SURVIVED SO LONG. ESPRESSO had been fixed one attribute at a time -
three separate `= None` lines added on three separate days, each after its
own incident, the last (`last_payment_status`, 2026-10-05) because
BookingService decides a PERMANENT paid-in-full exclusion on it. Each fix
was correct and none was general, so NCL and GoCCL were never covered.

The fix is `BaseScraper.reset_per_booking_state()` plus the
`PER_BOOKING_STATE` tuple: one place, every scraper, and a new cached
attribute is protected by adding its name to the tuple.
"""

import ast
import inspect
import pathlib
import textwrap

from scraper.base import BaseScraper
from scraper.espresso import EspressoScraper
from scraper.goccl import GoCCLScraper
from scraper.ncl import NclScraper


# -- the mechanism -------------------------------------------------------


class _Scraper(BaseScraper):
    """BaseScraper is abstract; the reset logic is not."""

    async def check_booking(self, booking_id, capture_market_data=False):
        raise NotImplementedError


def test_every_cached_attribute_is_cleared():
    s = _Scraper()
    for name in BaseScraper.PER_BOOKING_STATE:
        setattr(s, name, {"booking": "the previous one"})
    s.reset_per_booking_state()
    for name in BaseScraper.PER_BOOKING_STATE:
        assert getattr(s, name) is None, f"{name} survived into the next booking"


def test_the_attributes_booking_service_actually_reads_are_covered():
    """The guard is only worth anything if it covers the attributes that
    get written to the database. These four are read by name in
    BookingService._run_batch after check_booking returns."""
    for name in ("last_market_data", "last_feature_fields",
                 "last_payment_status", "last_initial_data"):
        assert name in BaseScraper.PER_BOOKING_STATE


def test_the_covered_attributes_exist_on_a_fresh_scraper():
    """reset_per_booking_state uses setattr, which would happily invent a
    typo'd attribute and silently protect nothing."""
    s = _Scraper()
    for name in BaseScraper.PER_BOOKING_STATE:
        assert hasattr(s, name), f"{name} is not initialised in __init__"


def test_resetting_twice_is_harmless():
    """ESPRESSO calls it once per booking and again per retry attempt."""
    s = _Scraper()
    s.reset_per_booking_state()
    s.reset_per_booking_state()
    assert s.last_market_data is None


def test_goccl_advisories_are_not_dragged_into_this():
    """`last_advisories` is a LIST with its own reset-to-[] and an
    `append` call site. Setting it to None would raise there, so it is
    deliberately not in PER_BOOKING_STATE."""
    assert "last_advisories" not in BaseScraper.PER_BOOKING_STATE


# -- every scraper actually calls it -------------------------------------


def _reset_precedes_every_return(func) -> bool:
    """AST, not string search: a reset placed after an early return is the
    exact bug being fixed, and the words also appear in comments and
    docstrings that prove nothing."""
    src = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(src)

    resets = [n.lineno for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "reset_per_booking_state"]
    returns = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Return)]
    if not resets:
        return False
    return min(resets) < min(returns, default=10**9)


def test_ncl_resets_before_it_can_return():
    """NCL is where the 49 corrupted rows came from: its capture sits at
    the END of check_booking, after make_not_on_this_account_result and
    make_error_result can both return."""
    # The walk moved into _check_booking_market on 2026-10-16; check_booking
    # is now a thin wrapper that only stamps the account's currency.
    assert _reset_precedes_every_return(NclScraper._check_booking_market)


def test_goccl_resets_before_it_can_return():
    assert _reset_precedes_every_return(GoCCLScraper.check_booking)


def test_espresso_resets_per_booking_and_per_attempt():
    """ESPRESSO retries `_attempt`, so an attempt that died after reading
    the payment panel must not leave that read standing for one that never
    got there."""
    src = inspect.getsource(EspressoScraper._check_booking_inner)
    assert src.count("reset_per_booking_state") >= 2, (
        "ESPRESSO must reset once per booking AND once per retry attempt")
    outer = src.index("reset_per_booking_state")
    assert outer < src.index("async def _attempt")


def test_no_scraper_still_resets_cached_state_by_hand():
    """A stray `self.last_x = None` is how this drifted apart in the first
    place: three correct one-attribute fixes that never became general."""
    import re

    for path in ("scraper/espresso.py", "scraper/ncl.py", "scraper/goccl.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        for name in BaseScraper.PER_BOOKING_STATE:
            stray = re.search(rf"self\.{name}\s*=\s*None", src)
            assert stray is None, (
                f"{path} clears {name} by hand - add it to "
                "PER_BOOKING_STATE and call reset_per_booking_state() "
                "instead, so the next scraper gets it for free")
