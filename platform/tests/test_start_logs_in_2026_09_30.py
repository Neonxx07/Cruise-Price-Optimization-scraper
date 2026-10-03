"""Pressing Start should log you in, not send you to another button.

Neon 2026-09-30, on a freshly opened app:

    16:52:35  gui.start_entered          <- pressed Start
    16:52:37  gui.login_check_entered    <- told to press this instead
    16:52:51  login_check.success  via=auto_login
    16:52:52  gui.start_entered          <- pressed Start AGAIN

The credentials were in the OS keyring the whole time and `auto_login` took
nine seconds. Refusing to start, in order to ask for a click that runs the
very thing we could have run ourselves, is a dialog standing in for a
feature.

WHAT MUST NOT CHANGE. The guard itself is load-bearing (added 2026-08-26): a
live browser is NOT a login, `check_login` opens the browser before polling,
and a 15-minute login timeout leaves a perfectly alive scraper sitting on a
login page. Starting then would run the whole watchlist against a logged-out
portal. So the refusal stays - it just becomes the SECOND answer, after
logging in has actually been attempted.
"""

import inspect
import io
import tokenize

from gui.windows import CruiseLinePanel


def _code() -> str:
    """_on_start_guarded with comments stripped.

    The comments here quote the incident and name the calls they explain, so
    matching prose would prove nothing. Six tests in this codebase have made
    that mistake.
    """
    src = inspect.getsource(CruiseLinePanel._on_start_guarded)
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT)


def test_start_attempts_a_login_before_refusing():
    """THE fix. Start must try, not delegate."""
    code = _code()
    guard = code.index("_login_ok_for != cruise_line_check")
    assert code.index("check_login", guard) < code.index("QMessageBox", guard)


def test_the_logged_out_guard_is_still_there():
    """A browser being open is not a login. Removing this would run the
    whole watchlist against a login page."""
    code = _code()
    assert "_login_ok_for != cruise_line_check" in code
    assert "has_session" in code


def test_a_failed_login_still_stops_the_scan():
    """ESPRESSO can demand MFA, which no automation completes. When the
    login does not happen, Start must still refuse."""
    code = _code()
    guard = code.index("_login_ok_for != cruise_line_check")
    tail = code[guard:]
    assert "QMessageBox" in tail
    assert "return" in tail[tail.index("QMessageBox"):]


def test_a_successful_login_records_the_confirmation():
    """_login_ok_for is the ONLY thing the Start guard trusts (2026-08-26).
    Logging in without setting it would refuse on the next press."""
    code = _code()
    guard = code.index("_login_ok_for != cruise_line_check")
    assert "self._login_ok_for = cruise_line_check" in code[guard:]


def test_the_attempt_is_logged_both_ways():
    """So a Start that silently did nothing can be told from one that
    logged in - the exact ambiguity that made this hard to see."""
    code = _code()
    assert "gui.start_auto_login_attempt" in code
    assert "gui.start_auto_login_ok" in code


def test_an_exception_during_login_does_not_crash_start():
    """A login that throws must become a refusal, not a traceback on the
    UI thread.

    STRUCTURAL. The first version asserted "except Exception" appeared
    within 1800 characters of the guard; the handler sits 2181 away, so a
    correct implementation failed. Distance in a source file is not a
    property worth testing - being inside a try block is. This codebase
    has now made that mistake twice in one day.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(CruiseLinePanel._on_start_guarded)))
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Try) and node.handlers
        and "check_login" in ast.dump(ast.Module(body=node.body,
                                                 type_ignores=[]))
    ]
    assert guarded, "the login attempt is not inside a try/except"


def test_msc_still_uses_its_own_login_path():
    """MSC is driven by a separate subsystem - routing it through the
    standard scraper login would open ESPRESSO's portal."""
    code = _code()
    guard = code.index("_login_ok_for != cruise_line_check")
    assert "msc_service.check_login" in code[guard:]
