"""Start is a 2-in-1: log in, then scan. (Roadmap P0.1)

Neon 2026-10-01: *"keep the check log in button however now switch since i
just press start make it loggs in and starts by this button as well meaning
start starts loggin in and starts scanning a 2 in 1 button."*

Start already attempted a login when it had never had one (fixed
2026-09-30). The hole was the OTHER branch.

`_login_ok_for` is STICKY - set once on a successful login and never
re-checked. So after any earlier login, Start skipped the login step
entirely and went straight to scanning. ESPRESSO drops a session roughly
hourly (see espresso.py's auto-logout note), so a Start pressed an hour
later ran the whole batch against a logged-out portal. That is where the
log's **339 `login.required`** events come from.

`has_live_session()` could not catch it either: it answers "is a browser
alive", and a browser sitting on a login wall is perfectly alive. That is
exactly what an expired ESPRESSO session looks like.

Now Start verifies the session for real - one cheap page check against the
already-open browser - and logs in again when it has gone. The "Check
login" button stays, at Neon's request, for logging in without scanning.
"""

import ast
import pathlib

import pytest

WINDOWS = pathlib.Path("gui/windows.py")
SERVICE = pathlib.Path("services/booking_service.py")


def _function(path: pathlib.Path, name: str):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    pytest.fail(f"{path} no longer defines {name}")


# -- the check exists and is real --------------------------------------


def test_the_service_can_say_whether_a_session_is_logged_in():
    from services.booking_service import BookingService
    assert hasattr(BookingService, "session_is_logged_in")


def test_the_gui_can_reach_it():
    from gui.queue_manager import BookingQueueManager
    assert hasattr(BookingQueueManager, "session_is_logged_in")


def test_being_logged_in_is_not_the_same_question_as_being_alive():
    """has_live_session asks about the browser; session_is_logged_in asks
    about the portal. Conflating them is the bug."""
    func = _function(SERVICE, "session_is_logged_in")
    source = ast.unparse(func)
    assert "has_live_session" in source, "it must still require a live browser"
    assert "_verify_login" in source, "it must actually check the login"


@pytest.mark.asyncio
async def test_no_live_session_is_reported_as_not_logged_in():
    from core.models import CruiseLine
    from services.booking_service import BookingService

    service = BookingService()
    service._live_scraper = None
    assert await service.session_is_logged_in(CruiseLine.ESPRESSO) is False


@pytest.mark.asyncio
async def test_an_unanswerable_check_reports_not_logged_in():
    """Never raises, and never guesses "yes" - the caller would scan blind."""
    from core.models import CruiseLine
    from services.booking_service import BookingService

    class Scraper:
        cruise_line = CruiseLine.ESPRESSO
        is_alive = True

    async def boom(_scraper, _line):
        raise RuntimeError("page gone")

    service = BookingService()
    service._live_scraper = Scraper()
    service._verify_login = boom

    assert await service.session_is_logged_in(CruiseLine.ESPRESSO) is False


# -- Start uses it -----------------------------------------------------


def test_start_verifies_the_session_instead_of_trusting_the_sticky_flag():
    """Structural, from the AST: _on_start_guarded must call the real
    check. Taken from the tree so a comment mentioning it cannot satisfy
    the test."""
    func = _function(WINDOWS, "_on_start_guarded")
    calls = [n for n in ast.walk(func)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "session_is_logged_in"]
    assert calls, (
        "Start no longer verifies the session; a stale _login_ok_for will "
        "let it scan against a logged-out portal again")


def test_start_still_has_a_login_step_at_all():
    func = _function(WINDOWS, "_on_start_guarded")
    source = ast.unparse(func)
    assert "check_login" in source, "Start must still be able to log in"


def test_a_failed_verification_clears_the_sticky_flag():
    """Otherwise the next Start trusts it again."""
    func = _function(WINDOWS, "_on_start_guarded")
    source = ast.unparse(func)
    assert "_login_ok_for = None" in source


def test_the_check_login_button_is_kept():
    """Neon asked for it explicitly: *"keep the check log in button"*."""
    source = WINDOWS.read_text(encoding="utf-8")
    assert 'QPushButton("Check login")' in source
    assert "_on_login_check" in source
