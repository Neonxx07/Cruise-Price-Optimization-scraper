"""The drift check must not cost more than the scrape it protects.

MEASURED 2026-09-30 from Neon's live run:

    16:44:03  espresso.navigate
    16:44:34  structure_watch.capture_failed  espresso_search_input   (30s)
    16:45:04  structure_watch.capture_failed  espresso_search_button  (30s)
    16:45:04  espresso.search

**61 seconds** between navigate and search on booking 1, against a usual
~2. `aria_snapshot()` inherited `page.set_default_timeout` (30 s), and BOTH
watched selectors timed out.

The same two failures appear on 09-23, 09-28 and 09-30 - every session since
this was wired up, roughly a minute each time - and those two baselines have
never once been captured. An early-warning diagnostic that costs a minute a
session and has never worked is worse than not having it.
"""

import ast
import inspect
import textwrap

from scraper.base import BaseScraper


def _code() -> str:
    """check_structure_drift with the DOCSTRING removed.

    Its docstring discusses aria_snapshot() at length, several lines before
    the call itself - so a plain substring search finds the prose and tests
    nothing. This codebase has made that mistake six times now.
    """
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(BaseScraper.check_structure_drift)))
    fn = tree.body[0]
    body = fn.body[1:] if (isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def test_the_snapshot_has_its_own_small_budget():
    """It must not inherit the 30-second action timeout."""
    assert BaseScraper.STRUCTURE_SNAPSHOT_TIMEOUT_MS <= 10_000
    assert BaseScraper.STRUCTURE_SNAPSHOT_TIMEOUT_MS >= 1_000


def test_the_budget_is_actually_passed_to_the_snapshot():
    """A constant nothing uses is decoration."""
    code = _code()
    assert "aria_snapshot(" in code
    call = code[code.index("aria_snapshot("):]
    assert "timeout=" in call[:120], "aria_snapshot still inherits the default"
    assert "STRUCTURE_SNAPSHOT_TIMEOUT_MS" in call[:160]


def test_a_timeout_is_not_logged_as_an_error():
    """It fired twice a session, every session, while nothing was actually
    broken - the scrape succeeded immediately afterwards. An error that
    always fires and never means anything trains you to ignore the log."""
    code = _code()
    assert "timed_out" in code
    assert "logger.info if timed_out else logger.error" in code


def test_a_real_failure_is_still_an_error():
    """A non-timeout failure means this baseline is unwatched - the silent
    coverage gap the error level exists for."""
    src = inspect.getsource(BaseScraper.check_structure_drift)
    assert "logger.error" in src


def test_the_check_still_never_blocks_a_scan():
    """Whatever happens, it returns a dict rather than raising."""
    src = inspect.getsource(BaseScraper.check_structure_drift)
    assert "except Exception" in src
    assert 'return {"status": "capture_failed"' in src


def test_it_still_runs_once_per_session():
    """Per booking, even 4 seconds twice would be 96 minutes on a
    723-booking watchlist."""
    src = inspect.getsource(BaseScraper.check_structure_drift)
    assert "_structure_checked" in src
    assert "skipped_already_checked_this_session" in src
