"""ESPRESSO must never launch headless, and the guard must stay.

RE-TESTED 2026-09-23 against Neon's question: the "never headless" rule was
set on 2026-08-14, before Chromium's new headless mode was on the table.
Playwright distinguishes the old headless SHELL from the new headless
CHROMIUM, and the newer mode is much closer to a real browser - so the old
conclusion deserved re-testing rather than trusting.

It was re-tested. The conclusion holds, and is now much better evidenced.

MEASURED, unauthenticated GET of the ESPRESSO home URL, same machine, same
minute, browser mode the only variable:

    A headed                     HTTP 200  -> redirected to /login, real page
    B headless (no channel)      HTTP 404  -> "Not found"
    C headless channel=chromium  HTTP 404  -> "Not found"
    D headless channel=chrome    HTTP 404  -> "Not found"

Re-run in REVERSE order to rule out rate limiting or a warming effect:
identical results, 8/8 trials, fully deterministic.

The block happens at the CDN edge, BEFORE any application code runs - a
bland 404 rather than a 403, which is how Akamai denies without telling a
scraper it was detected. No Playwright channel fixes it.

WHY THE NEW HEADLESS MODE DOES NOT HELP. The fingerprints were measured too,
and C/D are identical to headed on every axis that usually matters:

    surface              headed   B shell    C chromium   D chrome
    plugins                   5         0            5           5
    languages          en-US,en     en-US     en-US,en    en-US,en
    WebGL renderer     Intel HD  SwiftShader  Intel HD    Intel HD
    navigator.webdriver    True      True         True        True

Two things follow. First, the headless SHELL really is a poor impersonation
(0 plugins, software WebGL) - but fixing that is not sufficient, because C
and D match headed and are still blocked. Second, `navigator.webdriver` is
True in HEADED PRODUCTION TOO, so it is not the discriminator; whatever
Akamai keys on is not visible from page JavaScript.

The remaining measurable difference is the "HeadlessChrome" token in the
user-agent. Masking it would be anti-bot evasion, which is explicitly out of
scope - "The goal is reliable legitimate automation, not bypassing security
controls". So ESPRESSO stays headed, and this test pins the guard.
"""

import ast
import inspect
from pathlib import Path

from core.models import CruiseLine
from scraper.base import BaseScraper

BASE_PY = Path(__file__).resolve().parents[1] / "scraper" / "base.py"


def _start_code() -> str:
    """BaseScraper.start source with comments stripped.

    Comments describe the rule; they must never be what the test matches.
    This codebase has had three comments that confidently described
    behaviour the code did not have.
    """
    import io
    import tokenize

    src = inspect.getsource(BaseScraper.start)
    return tokenize.untokenize(
        tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type != tokenize.COMMENT
    )


def test_espresso_is_forced_visible_in_the_single_launch_path():
    """The guard lives in BaseScraper.start so no caller can bypass it -
    not the CLI --headless flag, not easy_menu's default answer, not a GUI
    scan. All three used to be able to launch ESPRESSO headless."""
    code = _start_code()
    assert "CruiseLine.ESPRESSO" in code
    assert "resolved_headless = False" in code


def test_the_guard_runs_before_the_browser_is_launched():
    """Overriding after launch would be useless."""
    code = _start_code()
    assert code.index("resolved_headless = False") < code.index("chromium.launch")


def test_the_override_is_logged_so_it_is_never_silent():
    code = _start_code()
    assert "espresso_headless_forced_visible" in code


def test_only_espresso_is_forced():
    """NCL was MEASURED headless-capable on 2026-09-16 - same three bookings,
    identical totals and identical category counts. The guard must not take
    that away."""
    code = _start_code()
    forced = code[code.index("CruiseLine.ESPRESSO"):]
    for other in ("CruiseLine.NCL", "CruiseLine.GOCCL", "CruiseLine.MSC"):
        assert other not in forced.split("resolved_headless = False")[0], (
            f"{other} must still honour the headless setting")


def test_the_settings_default_is_not_what_protects_espresso():
    """settings.browser_headless defaults True (NCL/GoCCL want it). ESPRESSO
    is protected by the launch-path guard, NOT by the default - so flipping
    the default must never be treated as the safety mechanism."""
    from config.settings import settings
    assert settings.browser_headless is True
    assert "CruiseLine.ESPRESSO" in _start_code()


def test_the_guard_covers_an_explicitly_requested_headless_run():
    """`start(headless=True)` is the dangerous call - a caller asking for it
    directly. The override must apply to the RESOLVED value, after the
    argument has been folded in."""
    code = _start_code()
    resolve = code.index("resolved_headless = settings.browser_headless")
    override = code.index("resolved_headless = False")
    assert resolve < override, (
        "the override must come after the argument is resolved, or an "
        "explicit headless=True would win")


def test_espresso_is_still_the_line_this_applies_to():
    """Cheap guard against the enum being renamed out from under the rule."""
    assert CruiseLine.ESPRESSO.value == "ESPRESSO"


def test_the_evidence_is_recorded_next_to_the_rule():
    """A rule this expensive to re-derive must carry its evidence. The
    2026-08-14 version said only 'Akamai bot detection' with no measurement,
    which is why it was reasonable to question in the first place."""
    src = BASE_PY.read_text(encoding="utf-8")
    start = next(n for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "start")
    seg = ast.get_source_segment(src, start)
    assert "2026-08-14" in seg or "2026-09-23" in seg
