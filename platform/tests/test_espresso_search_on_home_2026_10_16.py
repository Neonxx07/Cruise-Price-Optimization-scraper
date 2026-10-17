"""ESPRESSO: search from /home, never open reservations.do directly.

Neon 2026-10-16: "can we fix this for good!!! again log in issue with
esspresso" / "it keeps logging in and out !!" - and nobody else was signed
in. Traced live that morning (scratchpad espresso_login_diag*.py), on every
login:

  13.7 s  /home                 (login OK, iPlanetDirectoryPro set)
  16.1 s  GET reservations.do   -> 302 ../../SecureLogin.do
          GET SecureLogin.do    -> 301 /logout
  18.5 s  endSession            -> 302 /login        <- WE logged us out

ESPRESSO moved "Search by Reservation ID, Name or Date" onto /home; the old
reservations page now forwards to LOGOUT. Dropping the stale LtpaToken2 did
not help (run C); searching from /home did: 3001041 and 3001035 checked and
released, session up 2 minutes after, no re-login.

AST, never comments (CLAUDE.md testing conventions).
"""
import ast
import inspect
import textwrap

from scraper.espresso import EspressoScraper


def _navigations_to_base_url(func) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    hits = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "navigate"):
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "espresso_base_url":
                    hits += 1
    return hits


def test_the_scan_never_opens_reservations_do_directly():
    # _check_booking_inner holds the walk; check_booking is its retry/release wrapper.
    for name in ("check_booking", "_check_booking_inner", "_prove_session",
                 "_open_booking_for_viewing"):
        assert _navigations_to_base_url(getattr(EspressoScraper, name)) == 0, name


def test_the_only_way_to_reservations_do_is_the_logged_fallback():
    assert _navigations_to_base_url(EspressoScraper._go_to_search) == 1
    src = inspect.getsource(EspressoScraper._go_to_search)
    assert "espresso.search_box_not_on_home" in src


def test_the_proof_and_the_scan_both_go_through_go_to_search():
    for name in ("_check_booking_inner", "_prove_session", "_open_booking_for_viewing"):
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(EspressoScraper, name))))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "_go_to_search"]
        assert calls, name
