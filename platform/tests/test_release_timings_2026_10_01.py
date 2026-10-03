"""Measure release_booking before optimising it. (Roadmap P4.1)

MEASURED over 1,369 real bookings in `data/cruiseintel.log`::

    total_ms               median  16.39s   max  64.68s
    release_booking        median  10.20s   max  25.45s   <- 62%
    search                 median   3.57s   max  52.14s
    navigate_reservations  median   1.11s
    navigate_home          median   1.07s
    check_login_1          median   0.10s
    read_category          median   0.06s
    payment_status         median   0.01s

`release_booking` is **62% of ESPRESSO scan time** - higher than the 46.7%
recorded in the September handoff, because it now runs on EVERY booking
rather than the 38% that used to reach it. Halving it roughly halves a run.

BUT THE 10s IS SPREAD ACROSS FOUR STEPS and nothing said which one owns it:
open the dialog, wait for it, click Exit, wait for the page to settle.
Guessing here is how a release gets broken, and a missed release means a
booking locked for 15 minutes - the V-VIP bug this project spent a day on.

So this change MEASURES and optimises nothing. The breakdown goes to its own
log line, deliberately not folded into the existing `release_booking` stage,
so the 1,369 historical records stay comparable.

The constraint the instrumentation itself must respect: `release_booking`
NEVER RAISES - a failed release must not turn a good result into an error -
so a timing call inside it must not be able to either.
"""

import ast
import pathlib

import pytest

ESPRESSO = pathlib.Path("scraper/espresso.py")


def _release_booking() -> ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(ESPRESSO.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "release_booking"):
            return node
    pytest.fail("scraper/espresso.py no longer defines release_booking")


def _marks() -> list[str]:
    """Every stage name marked inside release_booking, from the AST."""
    out = []
    for node in ast.walk(_release_booking()):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mark"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            out.append(node.args[0].value)
    return out


# -- the four steps are each timed ---------------------------------------


def test_every_step_of_the_release_is_timed():
    """One of these owns the 10 seconds. Without all four, the next run
    still cannot say which."""
    marks = set(_marks())
    for stage in ("find_link", "open_dialog", "await_dialog",
                  "click_exit", "page_settle", "verify"):
        assert stage in marks, f"{stage} is not timed; the breakdown has a hole"


def test_the_breakdown_is_logged():
    for node in ast.walk(_release_booking()):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "info"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "espresso.release_timings"):
            return
    pytest.fail("the release breakdown is measured but never logged")


def test_the_total_is_logged_alongside_the_stages():
    """Stages that do not add up to the total mean a step is unmeasured."""
    source = ast.unparse(_release_booking())
    assert "total_ms=release_watch.total_ms" in source


# -- it must not disturb what already works ------------------------------


def test_the_existing_release_booking_stage_is_untouched():
    """1,369 historical timing records use that key. Folding sub-stages
    into it would silently change what the number means."""
    source = ESPRESSO.read_text(encoding="utf-8")
    assert '_release_watch.mark("release_booking")' in source, (
        "the outer release_booking stage mark is gone - historical timings "
        "are no longer comparable")


def test_the_release_still_never_raises():
    """The V-VIP constraint. A failed release must not turn a good result
    into an error, so the whole body stays inside a try/except."""
    func = _release_booking()
    tries = [n for n in func.body if isinstance(n, ast.Try)]
    assert tries, "release_booking's body is no longer wrapped in a try"
    assert any(t.handlers for t in tries), "the try has no except"


def test_timing_never_introduces_a_bare_sleep():
    """Instrumentation must not slow down the thing it measures."""
    func = _release_booking()
    sleeps = [n for n in ast.walk(func)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "sleep"]
    assert not sleeps, "a sleep was added inside release_booking"


# -- the stopwatch is cheap ----------------------------------------------


def test_the_stopwatch_costs_a_clock_read():
    """Per booking this runs six times; it must not be expensive."""
    from scraper.base import _Stopwatch

    watch = _Stopwatch()
    watch.mark("a")
    watch.mark("b")
    assert set(watch.stages) == {"a", "b"}
    assert all(isinstance(v, int) for v in watch.stages.values())
    assert watch.total_ms >= 0


def test_marks_measure_intervals_not_cumulative_time():
    """If mark() were cumulative the breakdown would read as nonsense -
    every later stage inflated by the ones before it."""
    import time

    from scraper.base import _Stopwatch

    watch = _Stopwatch()
    time.sleep(0.02)
    watch.mark("first")
    watch.mark("second")

    assert watch.stages["first"] >= 15
    assert watch.stages["second"] < 15
