"""A click is not done because `click()` returned. (Brief item #1)

Neon 2026-10-01: *"The system should never assume a click succeeded merely
because click() returned successfully ... For important actions, verify the
resulting page state ... Never silently continue after an uncertain click."*

THE FAILURE THIS CATCHES IS INVISIBLE IN THE LOG, by construction.
`click()` returning only means Playwright dispatched an event to an element
that passed its actionability checks. A React handler that silently bailed,
a form that failed validation, an overlay that swallowed the event - every
one of those returns cleanly, and the scraper carries on against a page that
never changed.

It has already happened here. The 2026-09-18 release bug: the Exit click was
logged as successful, the dialog stayed open, the booking stayed locked for
fifteen minutes, and Neon had to press Exit by hand - *"the script is not
pressong on exit i have to press it manually"*.

WHAT THE MEASUREMENT RULED OUT. All 542 error strings in the log:

    navigation                      477  (88%)
    session / logged out             28
    element never appeared           16
    click timed out (found)           2
    WRONG ELEMENT CLICKED             0

Zero. Playwright locators run in strict mode, so an ambiguous selector
raises rather than guessing. There is no wrong-element problem to solve,
which is why this module contains no OCR and no computer vision: they would
read pixels to answer a question the DOM already answers better, at a CPU
cost, for a failure mode measured at zero.
"""

import pytest

from scraper.click_verify import ClickOutcome, click_verified


class FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        return self._page.elements.get(self._selector, {}).get("count", 0)

    async def is_visible(self):
        return bool(self._page.elements.get(self._selector, {}).get("visible"))

    async def click(self, timeout=None):
        self._page.clicks.append(self._selector)
        effect = self._page.on_click.get(self._selector)
        if effect == "raise":
            raise RuntimeError("Locator.click: Timeout 5000ms exceeded")
        if callable(effect):
            effect(self._page)

    async def evaluate(self, _script):
        self._page.js_clicks.append(self._selector)
        effect = self._page.on_js_click.get(self._selector)
        if callable(effect):
            effect(self._page)


class FakePage:
    """Enough of a Playwright page to drive the real code path."""

    def __init__(self, elements=None, body="", url="https://portal/a"):
        self.elements = elements or {}
        self.body = body
        self.url = url
        self.clicks: list[str] = []
        self.js_clicks: list[str] = []
        self.on_click: dict = {}
        self.on_js_click: dict = {}

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def inner_text(self, _selector):
        return self.body

    async def wait_for_timeout(self, _ms):
        return None


def _present(**overrides):
    base = {"#btn": {"count": 1, "visible": True}}
    base.update(overrides)
    return base


# -- before the click: is it really there? -------------------------------


@pytest.mark.asyncio
async def test_a_missing_element_is_never_clicked():
    page = FakePage(elements={"#btn": {"count": 0, "visible": False}})

    outcome = await click_verified(page, "#btn", expect_gone="#dialog")

    assert outcome.clicked is False
    assert outcome.reason == "element_absent"
    assert page.clicks == []


@pytest.mark.asyncio
async def test_an_invisible_element_is_never_clicked():
    """Present in the DOM but not on screen - the duplicate-id case that
    made the Exit dialog a coin flip until `visible=true` was added."""
    page = FakePage(elements={"#btn": {"count": 1, "visible": False}})

    outcome = await click_verified(page, "#btn", expect_gone="#dialog")

    assert outcome.reason == "element_not_visible"
    assert page.clicks == []


@pytest.mark.asyncio
async def test_a_failed_lookup_is_reported_not_raised():
    class Broken(FakePage):
        def locator(self, selector):
            raise RuntimeError("page closed")

    outcome = await click_verified(Broken(), "#btn", expect_gone="#d")

    assert outcome.ok is False
    assert outcome.reason == "lookup_failed"


# -- after the click: did the page react? --------------------------------


@pytest.mark.asyncio
async def test_a_dialog_that_closes_verifies():
    """The release-dialog signal: the thing we clicked went away."""
    page = FakePage(elements=_present(**{"#dialog": {"count": 1, "visible": True}}))
    page.on_click["#btn"] = lambda p: p.elements.__setitem__(
        "#dialog", {"count": 0, "visible": False})

    outcome = await click_verified(page, "#btn", expect_gone="#dialog")

    assert outcome.ok is True
    assert outcome.reason == "verified"


@pytest.mark.asyncio
async def test_a_dialog_that_stays_open_is_not_verified():
    """EXACTLY the 2026-09-18 bug: click returns fine, dialog still there,
    booking stays locked."""
    page = FakePage(elements=_present(**{"#dialog": {"count": 1, "visible": True}}))

    outcome = await click_verified(page, "#btn", expect_gone="#dialog")

    assert outcome.clicked is True
    assert outcome.verified is False
    assert outcome.ok is False
    assert outcome.reason == "state_not_reached"


@pytest.mark.asyncio
async def test_an_expected_element_appearing_verifies():
    page = FakePage(elements=_present(**{"#next": {"count": 0, "visible": False}}))
    page.on_click["#btn"] = lambda p: p.elements.__setitem__(
        "#next", {"count": 1, "visible": True})

    assert (await click_verified(page, "#btn", expect_visible="#next")).ok


@pytest.mark.asyncio
async def test_expected_text_appearing_verifies():
    page = FakePage(elements=_present())
    page.on_click["#btn"] = lambda p: setattr(p, "body", "Reservation released")

    assert (await click_verified(page, "#btn", expect_text="released")).ok


@pytest.mark.asyncio
async def test_expected_text_is_case_insensitive():
    page = FakePage(elements=_present())
    page.on_click["#btn"] = lambda p: setattr(p, "body", "RESERVATION RELEASED")
    assert (await click_verified(page, "#btn", expect_text="released")).ok


@pytest.mark.asyncio
async def test_a_url_change_verifies():
    page = FakePage(elements=_present())
    page.on_click["#btn"] = lambda p: setattr(p, "url", "https://portal/b")

    assert (await click_verified(page, "#btn", expect_url_change=True)).ok


@pytest.mark.asyncio
async def test_a_url_that_does_not_change_is_not_verified():
    page = FakePage(elements=_present())
    assert not (await click_verified(page, "#btn", expect_url_change=True)).ok


@pytest.mark.asyncio
async def test_every_expectation_must_hold():
    """Any one failing means the page is not in the state the caller is
    about to assume."""
    page = FakePage(elements=_present(**{"#dialog": {"count": 1, "visible": True}}))
    page.on_click["#btn"] = lambda p: p.elements.__setitem__(
        "#dialog", {"count": 0, "visible": False})      # gone: yes

    outcome = await click_verified(page, "#btn",
                                   expect_gone="#dialog",
                                   expect_text="never appears")  # text: no
    assert outcome.ok is False


# -- recovery ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_swallowed_click_is_retried_through_the_handler():
    """A real overlay can swallow a synthetic click while the element's own
    handler still works - the fix already proven on the Exit dialog."""
    page = FakePage(elements=_present(**{"#dialog": {"count": 1, "visible": True}}))
    page.on_click["#btn"] = "raise"
    page.on_js_click["#btn"] = lambda p: p.elements.__setitem__(
        "#dialog", {"count": 0, "visible": False})

    outcome = await click_verified(page, "#btn", expect_gone="#dialog")

    assert outcome.ok is True
    assert page.js_clicks == ["#btn"], "the handler fallback never ran"


@pytest.mark.asyncio
async def test_the_fallback_only_runs_on_an_element_already_confirmed_present():
    """It must never become a way to force a missing element."""
    page = FakePage(elements={"#btn": {"count": 0, "visible": False}})

    await click_verified(page, "#btn", expect_gone="#d")

    assert page.js_clicks == []


@pytest.mark.asyncio
async def test_attempts_are_bounded():
    page = FakePage(elements=_present(**{"#dialog": {"count": 1, "visible": True}}))

    await click_verified(page, "#btn", expect_gone="#dialog", attempts=2)

    assert len(page.clicks) + len(page.js_clicks) == 2


# -- honesty about an unverified click -----------------------------------


@pytest.mark.asyncio
async def test_a_click_with_no_expectation_is_not_reported_as_verified():
    """Otherwise an unchecked call would look identical to a checked one,
    and the guarantee would quietly mean nothing."""
    page = FakePage(elements=_present())

    outcome = await click_verified(page, "#btn")

    assert outcome.clicked is True
    assert outcome.verified is False
    assert outcome.reason == "no_expectation_given"


@pytest.mark.asyncio
async def test_the_outcome_is_falsy_unless_clicked_and_verified():
    assert not ClickOutcome(clicked=True, verified=False)
    assert not ClickOutcome(clicked=False, verified=True)
    assert ClickOutcome(clicked=True, verified=True)


@pytest.mark.asyncio
async def test_nothing_raises_whatever_the_page_does():
    """A caller must be able to branch on the outcome; an exception would
    turn "the page did not react" into a failed booking."""
    class Hostile(FakePage):
        async def inner_text(self, _selector):
            raise RuntimeError("detached")

    page = Hostile(elements=_present())
    outcome = await click_verified(page, "#btn", expect_text="anything")
    assert outcome.ok is False


# -- it is reachable from a scraper --------------------------------------


def test_base_scraper_exposes_it():
    from scraper.base import BaseScraper
    assert hasattr(BaseScraper, "click_verified")


def test_no_ocr_or_vision_dependency_was_added():
    """The measurement said the wrong-element rate is zero, so pixels
    would cost CPU to answer a question the DOM answers better. This fails
    if someone later reaches for one."""
    import pathlib

    source = pathlib.Path("scraper/click_verify.py").read_text(encoding="utf-8")
    import ast

    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("cv2", "pytesseract", "easyocr", "PIL", "numpy",
                   "paddleocr", "skimage", "torch"):
        assert banned not in imported, f"{banned} crept into the click path"


# -- the audit, kept as a test ------------------------------------------
#
# AUDITED 2026-10-01 across the three live scrapers: of 35 `.click(` sites,
# 20 had no check of any kind in the 8 lines that follow.
#
#     scraper/espresso.py    7 clicks   4 checked   3 not
#     scraper/ncl.py        16 clicks   5 checked  11 not
#     scraper/goccl.py      12 clicks   6 checked   6 not
#
# Those were NOT converted wholesale. The most critical paths - the Exit
# dialog, the categories link - already hand-roll exactly this
# verification, which is why the measured wrong-click rate is zero and only
# two clicks ever timed out. Rewriting working code on the release path
# would risk the V-VIP "always release the booking" rule for no measured
# gain.
#
# The ones that genuinely lack a signal (NCL's search submit chain, for
# instance) need a REAL post-click signal chosen against a live page.
# Guessing a selector is forbidden in this project, so they are recorded
# here rather than papered over.
#
# This test pins the count so it cannot quietly grow.

_CLICK_AUDIT_BASELINE = 20


def test_the_number_of_unverified_clicks_does_not_grow():
    """New code must verify its clicks. This fails if someone adds an
    unchecked one, and should be LOWERED as sites are converted."""
    import pathlib
    import re

    verify = re.compile(
        r"wait_for|is_visible|count\(\)|inner_text|expect|_check_|"
        r"wait_until|settle|load_state|click_verified", re.I)

    unverified = 0
    for name in ("scraper/espresso.py", "scraper/ncl.py", "scraper/goccl.py"):
        lines = pathlib.Path(name).read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if ".click(" not in line or line.strip().startswith("#"):
                continue
            if not verify.search("\n".join(lines[i + 1:i + 9])):
                unverified += 1

    assert unverified <= _CLICK_AUDIT_BASELINE, (
        f"{unverified} clicks now have no post-click check, up from "
        f"{_CLICK_AUDIT_BASELINE}. Use BaseScraper.click_verified for new "
        "critical clicks."
    )
