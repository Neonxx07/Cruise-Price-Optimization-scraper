"""auto_login must not declare ALREADY_LOGGED_IN off a half-loaded page.

Neon 2026-09-28: "i have entered the username and password in the cmd and
now i have opened the esspreesso but the project did not log in
automatically".

The log showed exactly why::

    18:05:33  browser.started      restored_session=True
    18:05:35  espresso.auto_login  result=ALREADY_LOGGED_IN
    18:14:12  login.required       url=.../login

`_check_login` ran TWO SECONDS after the browser started, while a
restored-but-dead session was still redirecting itself to /login. It
sampled a half-loaded page, reported authenticated, and auto_login returned
without ever filling the credential - so the GUI sat polling for a human who
should not have been needed.

This was identified on 2026-09-23 and left open. It is the same race as the
aborted navigations (268 ERR_ABORTED) and the failed session recovery:
asking a page a question before it has stopped moving. A false
ALREADY_LOGGED_IN is the worst version, because it makes auto_login do
nothing at all.
"""

import inspect
import io
import tokenize

from scraper.espresso import EspressoScraper


def _auto_login_code() -> str:
    """auto_login source, comments stripped - the comments discuss the bug."""
    src = inspect.getsource(EspressoScraper.auto_login)
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT
    )


def test_the_page_is_settled_before_the_already_logged_in_check():
    """THE regression guard. Checking first is what made auto_login a no-op
    on a dead restored session."""
    code = _auto_login_code()
    assert "_settle_navigation" in code, "auto_login never settles the page"
    assert code.index("_settle_navigation") < code.index("_check_login"), (
        "the settle must run BEFORE the login check, or a page still "
        "redirecting to /login reads as authenticated")


def test_already_logged_in_is_still_possible():
    """The early return is a real optimisation - ESPRESSO allows one session
    per account, so re-submitting a login over a live one is risky. The fix
    must not remove it, only stop it firing too early."""
    code = _auto_login_code()
    assert "ALREADY_LOGGED_IN" in code


def test_credentials_are_still_read_before_anything_else():
    """A missing credential must be reported as NO_CREDENTIALS_SAVED, not
    hidden behind a page check."""
    code = _auto_login_code()
    assert code.index("NO_CREDENTIALS_SAVED") < code.index("_settle_navigation")


def test_the_settle_is_bounded():
    """It runs on every login check. An unbounded wait would hang the GUI."""
    code = _auto_login_code()
    settle = code.index("_settle_navigation")
    assert "timeout_ms" in code[settle:settle + 160]
