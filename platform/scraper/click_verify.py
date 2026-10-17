"""Never assume a click worked because `click()` returned.

Neon 2026-10-01: *"The system should never assume a click succeeded merely
because `click()` returned successfully ... For important actions, verify
the resulting page state ... Never silently continue after an uncertain
click."*

WHAT THE EVIDENCE SAYS, because it shaped what this does and does not do.
All 542 error strings in `data/cruiseintel.log`, classified:

    navigation                                  477   (88%)
    session / logged out                         28
    element never appeared (timeout)             16
    other timeout                                 8
    element genuinely absent                      6
    click timed out (element FOUND, not actionable)  2
    WRONG ELEMENT CLICKED                         0

**Zero.** Playwright runs locators in strict mode, so an ambiguous selector
raises rather than guessing, and it never has. So this module is NOT about
finding the right element - the DOM already does that better than pixels
could. It is about the case the log cannot show at all: a click that
Playwright reports as successful while the page does nothing.

That case is invisible by construction. `click()` returning only means
Playwright dispatched an event to an element that passed its actionability
checks. A React handler that silently bailed, a form that failed
validation, an overlay that swallowed the event - all return cleanly, and
the scraper then carries on against a page that never changed. The release
dialog bug of 2026-09-18 was exactly this shape: a click was logged as
successful, the dialog stayed open, the booking stayed locked, and a human
had to press Exit by hand.

WHY NO OCR OR COMPUTER VISION. Playwright already has the DOM; OCR has
pixels, which is strictly less information about the same page. It would
cost CPU and a dependency to answer a question we can answer better, for a
failure mode measured at zero occurrences. Every "smart element" project
worth naming - Skyvern, browser-use, LaVague, Agent-E - is LLM-backed and
barred by this project's no-AI rule. Playwright's own tracing
(screenshots + DOM snapshots + sources) is already enabled in BaseScraper
and is the better diagnostic.

COST. One `is_visible()` before, one cheap check after. No screenshots in
the normal path - `dump_failure_snapshot` already fires on failure, which
is the moment the image is actually worth having.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ClickOutcome:
    """What actually happened, so a caller can branch on it.

    `verified` is the one that matters: False means the click was
    dispatched but the page did not reach the expected state, and the
    caller must recover rather than continue.
    """

    clicked: bool = False
    verified: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.clicked and self.verified

    def __bool__(self) -> bool:
        return self.ok


async def _expect_gone(page, selector: str) -> bool:
    """The clicked thing disappeared - a dialog closing, a button
    consumed. The release-dialog signal."""
    try:
        locator = page.locator(selector)
        if await locator.count() == 0:
            return True
        return not await locator.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def _expect_visible(page, selector: str) -> bool:
    """Something new appeared - the next step's form, a confirmation."""
    try:
        locator = page.locator(selector)
        return await locator.count() > 0 and await locator.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def _expect_text(page, text: str) -> bool:
    """The page says something it did not before.

    Read from `body` innerText rather than a selector, because the portals
    here put confirmations in whatever container the template felt like.
    """
    try:
        body = await page.inner_text("body")
        return text.lower() in (body or "").lower()
    except Exception:  # noqa: BLE001
        return False


async def _expect_url_changed(page, before: str) -> bool:
    try:
        return page.url != before
    except Exception:  # noqa: BLE001
        return False


async def click_verified(page, selector: str, *,
                         expect_gone: str | None = None,
                         expect_visible: str | None = None,
                         expect_text: str | None = None,
                         expect_url_change: bool = False,
                         timeout_ms: int = 5000,
                         settle_ms: int = 400,
                         attempts: int = 2,
                         label: str = "") -> ClickOutcome:
    """Click, then prove the page reacted. Never raises.

    Order of operations, which is the whole point:

      1. the element must EXIST and be VISIBLE - a click on something that
         is not there is a different failure from one that did nothing;
      2. click it;
      3. check a deterministic signal that the intended state arrived;
      4. on failure, retry once via the element's own handler - a real
         overlay can swallow a synthetic click while the handler still
         works - and then report `verified=False`.

    AT LEAST ONE EXPECTATION SHOULD BE GIVEN. With none, this degrades to
    an ordinary click and says so in `reason`, so an unverified call is
    visible in the log rather than looking like a verified one.

    NEVER RAISES, deliberately. A caller must be able to branch on the
    outcome; an exception here would turn "the page did not react" into a
    failed booking, and the scraper already has richer recovery than that.
    """
    name = label or selector
    outcome = ClickOutcome()

    # ── 1. is it actually there? ─────────────────────────────────────
    try:
        locator = page.locator(selector)
        if await locator.count() == 0:
            outcome.reason = "element_absent"
            logger.warning("click.element_absent", target=name)
            return outcome
        target = locator.first
        if not await target.is_visible():
            outcome.reason = "element_not_visible"
            logger.warning("click.element_not_visible", target=name)
            return outcome
    except Exception as exc:  # noqa: BLE001
        outcome.reason = "lookup_failed"
        outcome.detail = {"error": str(exc)[:200]}
        logger.warning("click.lookup_failed", target=name, error=str(exc)[:200])
        return outcome

    url_before = ""
    try:
        url_before = page.url
    except Exception:  # noqa: BLE001
        pass

    expectations = [expect_gone, expect_visible, expect_text,
                    expect_url_change or None]
    if not any(expectations):
        outcome.reason = "no_expectation_given"

    for attempt in range(1, attempts + 1):
        # ── 2. click ─────────────────────────────────────────────────
        try:
            if attempt == 1:
                await target.click(timeout=timeout_ms)
            else:
                # A real overlay can swallow a synthetic click while the
                # element's own handler still works when invoked directly.
                # Only ever attempted on an element already confirmed
                # present - never as a way to force a missing one.
                await target.evaluate("el => el.click()")
            outcome.clicked = True
        except Exception as exc:  # noqa: BLE001
            outcome.detail = {"error": str(exc)[:200]}
            logger.warning("click.failed", target=name, attempt=attempt,
                           error=str(exc)[:200])
            continue

        if not any(expectations):
            # Clicked, nothing to check against. Reported honestly rather
            # than counted as verified.
            outcome.verified = False
            return outcome

        try:
            await page.wait_for_timeout(settle_ms)
        except Exception:  # noqa: BLE001
            pass

        # ── 3. did the page react? ───────────────────────────────────
        checks: list[tuple[str, bool]] = []
        if expect_gone:
            checks.append(("gone", await _expect_gone(page, expect_gone)))
        if expect_visible:
            checks.append(("visible", await _expect_visible(page, expect_visible)))
        if expect_text:
            checks.append(("text", await _expect_text(page, expect_text)))
        if expect_url_change:
            checks.append(("url", await _expect_url_changed(page, url_before)))

        # ALL expectations must hold. Any one of them failing means the
        # page is not in the state the caller is about to assume.
        if all(passed for _why, passed in checks):
            outcome.verified = True
            outcome.reason = "verified"
            outcome.detail = {"checks": dict(checks), "attempt": attempt}
            logger.info("click.verified", target=name, attempt=attempt)
            return outcome

        outcome.detail = {"checks": dict(checks), "attempt": attempt}
        logger.warning("click.unverified", target=name, attempt=attempt,
                       checks=dict(checks))

    # ── 4. clicked, but the page never reached the expected state ────
    outcome.verified = False
    if outcome.clicked and outcome.reason != "no_expectation_given":
        outcome.reason = "state_not_reached"
    elif not outcome.clicked:
        outcome.reason = outcome.reason or "click_failed"
    logger.warning("click.give_up", target=name, reason=outcome.reason,
                   detail=outcome.detail)
    return outcome
