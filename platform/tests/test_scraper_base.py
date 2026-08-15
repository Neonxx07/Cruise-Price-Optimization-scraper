"""Regression tests for BaseScraper.stop()'s shutdown sequencing fix
(2026-08-13 audit)."""
import pytest

from scraper.espresso import EspressoScraper


class _FakeContextThatDies:
    async def storage_state(self, path=None):
        return {}

    async def close(self):
        raise RuntimeError("context already dead (simulated crash)")


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_regression_stop_does_not_skip_browser_close_when_context_close_fails():
    """CONFIRMED REAL BUG, fixed 2026-08-13: context.close()/browser.close()/
    playwright.stop() used to share one try/except -- if context.close()
    raised (most likely exactly when the browser is already dead, which
    is the dead-browser recovery path's whole reason for calling stop()),
    browser.close()/playwright.stop() were skipped entirely, leaking the
    Chromium process and the Playwright driver subprocess."""
    s = EspressoScraper()
    s._context = _FakeContextThatDies()
    fake_browser = _FakeBrowser()
    fake_playwright = _FakePlaywright()
    s._browser = fake_browser
    s._playwright = fake_playwright

    await s.stop()

    assert fake_browser.closed, "browser.close() was skipped after context.close() raised"
    assert fake_playwright.stopped, "playwright.stop() was skipped after context.close() raised"


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    s = EspressoScraper()
    s._context = _FakeContextThatDies()
    s._browser = _FakeBrowser()
    s._playwright = _FakePlaywright()

    await s.stop()
    await s.stop()  # must not raise
