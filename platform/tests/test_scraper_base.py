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


# ── check_structure_drift (ARIA-snapshot early-warning check) ───────────


class _FakeLocator:
    """Models a real Playwright locator closely enough to catch the bug the
    old fake hid.

    UPDATED 2026-08-27. The old fake had no `.first`, and no `match_count`
    concept — so it could not represent the situation that actually broke
    production: a comma-OR selector matching MORE THAN ONE element.
    `page.click(sel)` is non-strict and clicks the first match (so scrapes
    worked), but `locator(sel).aria_snapshot()` is STRICT and raises, which
    is why `espresso_search_button.yaml` was never created while
    `espresso_search_input.yaml` was. `check_structure_drift` now calls
    `.first`; this fake makes a bare `aria_snapshot()` on a multi-match
    locator raise, exactly as Playwright does, so the fix is genuinely
    exercised instead of assumed."""

    def __init__(self, snapshot: str | Exception, match_count: int = 1):
        self._snapshot = snapshot
        self._match_count = match_count

    @property
    def first(self):
        return _FakeLocator(self._snapshot, match_count=1)

    async def aria_snapshot(self):
        if self._match_count > 1:
            raise RuntimeError(
                f"strict mode violation: locator resolved to "
                f"{self._match_count} elements"
            )
        if isinstance(self._snapshot, Exception):
            raise self._snapshot
        return self._snapshot


class _FakePage:
    def __init__(self, snapshot: str | Exception, match_count: int = 1):
        self._snapshot = snapshot
        self._match_count = match_count

    def locator(self, selector):
        return _FakeLocator(self._snapshot, match_count=self._match_count)


@pytest.mark.asyncio
async def test_structure_drift_first_call_creates_baseline(tmp_path):
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage("- textbox \"Reservation ID\"")

    result = await s.check_structure_drift("test_widget", "#whatever")

    assert result["status"] == "baseline_created"
    assert (tmp_path / "test_widget.yaml").read_text(encoding="utf-8") == "- textbox \"Reservation ID\""


@pytest.mark.asyncio
async def test_structure_drift_unchanged_when_snapshot_matches_baseline(tmp_path):
    (tmp_path / "test_widget.yaml").write_text("- textbox \"Reservation ID\"", encoding="utf-8")
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage("- textbox \"Reservation ID\"")

    result = await s.check_structure_drift("test_widget", "#whatever")

    assert result["status"] == "unchanged"


@pytest.mark.asyncio
async def test_structure_drift_flags_a_real_change():
    """The exact real-world case this exists for: the page's accessible
    structure changed (e.g. a portal redesign) since the baseline was
    saved."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        with open(f"{tmp}/test_widget.yaml", "w", encoding="utf-8") as f:
            f.write("- textbox \"Reservation ID\"")
        s = EspressoScraper()
        s.STRUCTURE_BASELINE_DIR = tmp
        s._page = _FakePage("- searchbox \"Search by Reservation ID, Name or Date\"")

        result = await s.check_structure_drift("test_widget", "#whatever")

        assert result["status"] == "changed"
        assert "textbox" in result["baseline"]
        assert "searchbox" in result["current"]


@pytest.mark.asyncio
async def test_structure_drift_capture_failure_never_raises(tmp_path):
    """If the selector can't even be found (e.g. it's the one that just
    broke), this must degrade to a clear status, never raise and never
    block the actual scrape that called it."""
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage(TimeoutError("locator not found"))

    result = await s.check_structure_drift("test_widget", "#whatever")

    assert result["status"] == "capture_failed"


@pytest.mark.asyncio
async def test_structure_drift_only_checks_once_per_session(tmp_path):
    """Real cost concern: this must not re-snapshot/re-read-the-baseline-
    file on every single booking in a batch -- only once per browser
    session, since the page layout can't change between bookings in the
    same run."""
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage("- textbox \"Reservation ID\"")

    first = await s.check_structure_drift("test_widget", "#whatever")
    s._page = _FakePage("- searchbox \"a totally different structure\"")  # simulate a real change mid-session
    second = await s.check_structure_drift("test_widget", "#whatever")

    assert first["status"] == "baseline_created"
    assert second["status"] == "skipped_already_checked_this_session"


@pytest.mark.asyncio
async def test_regression_multi_match_selector_still_creates_a_baseline(tmp_path):
    """CONFIRMED REAL BUG, fixed 2026-08-27.

    This project deliberately uses comma-OR selectors so a portal redesign
    can't break a scrape — EspressoScraper._SEARCH_BUTTON_SELECTOR is
    '#searchReservationBtn, [aria-label="Search by Reservation ID, Name or
    Date"]'. When both halves match, Playwright's STRICT locator API raises
    on aria_snapshot() even though page.click() is happy. That exception was
    caught and logged as a warning, so the drift monitor silently watched
    only ONE of the two selectors it was wired to watch: after the real
    2026-08-27 ESPRESSO run, data/structure_baselines/ held
    espresso_search_input.yaml and no espresso_search_button.yaml at all.
    """
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage("- button \"Search\"", match_count=3)

    result = await s.check_structure_drift("espresso_search_button", "#a, #b, #c")

    assert result["status"] == "baseline_created", (
        f"a multi-match selector must still be snapshotted via .first, got {result}"
    )
    assert (tmp_path / "espresso_search_button.yaml").exists()


@pytest.mark.asyncio
async def test_both_espresso_selectors_get_a_baseline(tmp_path):
    """The end-to-end shape of the bug: espresso.py asks for baselines on
    BOTH the search input and the search button. Before the fix only one
    file appeared. Both selectors are multi-match by construction."""
    s = EspressoScraper()
    s.STRUCTURE_BASELINE_DIR = str(tmp_path)
    s._page = _FakePage("- textbox \"Search by Reservation ID, Name or Date\"", match_count=2)

    await s.check_structure_drift("espresso_search_input", s._SEARCH_INPUT_SELECTOR)
    await s.check_structure_drift("espresso_search_button", s._SEARCH_BUTTON_SELECTOR)

    created = sorted(f.name for f in tmp_path.glob("*.yaml"))
    assert created == ["espresso_search_button.yaml", "espresso_search_input.yaml"], created
