"""Tests for scraper/ncl.py's two real bugs found and fixed 2026-08-26
while bringing NCL support online (found by a code-review audit before
any live run — no test data exists yet since NCL has never actually been
run against the real portal, see msc_project_knowledge.md/the upgrade
backlog memory for the full context).

Both bugs are unit-tested at the Python-logic level here (mocking
whatever a real browser's page.evaluate()/query_selector() would return)
rather than the JS snippets themselves, which can only be verified live —
same split this project always uses: the surrounding Python control flow
is testable now, the actual portal markup is not.
"""
import pytest

from scraper.ncl import NclScraper, _resolve_new_total, _summarize_addon_change


# ── _resolve_new_total: None-vs-zero fallback (the dead-fallback bug) ──


def test_resolve_new_total_uses_real_value_when_present():
    assert _resolve_new_total({"resTotal": 1234.56}, fallback=999.0) == 1234.56


def test_resolve_new_total_trusts_a_genuine_zero():
    """A real $0 category total must be trusted, not treated as a failed
    read — this is exactly the None-vs-zero distinction the fix exists
    to preserve."""
    assert _resolve_new_total({"resTotal": 0.0}, fallback=999.0) == 0.0


def test_resolve_new_total_falls_back_when_read_failed():
    """CONFIRMED REAL BUG, fixed 2026-08-26: before this fix,
    _read_new_total always returned resTotal=0 (never None) on a failed
    lookup, so this fallback could never trigger — a failed re-read
    silently produced new_total=0 instead of the pre-selection grid
    value, which would make calculate_ncl compute a huge fake
    price_drop against the real old_total."""
    assert _resolve_new_total({"resTotal": None}, fallback=1500.0) == 1500.0


# ── _switch_to_edit_mode: already-in-edit-mode detection ───────────────


class _FakeElement:
    async def click(self):
        pass


class _FakePage:
    """Returns a canned element (or None) per selector, and records
    which selectors were queried — enough to drive _switch_to_edit_mode's
    branches without a real browser."""

    def __init__(self, present_selectors: set[str]):
        self._present = present_selectors
        self.queried: list[str] = []

    async def query_selector(self, selector: str):
        """A real browser's querySelector treats a comma-separated
        selector as an OR across each part — mirror that here so a test
        can express "#a, #b" the same way the real code does."""
        self.queried.append(selector)
        parts = {p.strip() for p in selector.split(",")}
        return _FakeElement() if parts & self._present else None

    async def click(self, selector: str, **kwargs):
        pass

    async def wait_for_selector(self, selector: str, **kwargs):
        pass


def _ncl_scraper_with_fake_page(present_selectors: set[str]) -> tuple[NclScraper, _FakePage]:
    s = NclScraper()
    page = _FakePage(present_selectors)
    s._page = page
    return s, page


@pytest.mark.asyncio
async def test_switch_to_edit_mode_normal_case_clicks_and_confirms():
    s, page = _ncl_scraper_with_fake_page({"#res-switch-edit", "#res-edit-save"})
    result = await s._switch_to_edit_mode()
    assert result is True


@pytest.mark.asyncio
async def test_switch_to_edit_mode_detects_already_editing_when_switch_button_absent():
    """CONFIRMED REAL GAP, fixed 2026-08-26: the switch-to-edit button can
    be absent because the booking is ALREADY mid-edit (a prior run
    crashed after acquiring the lock but before releasing it), not only
    because editing isn't offered. Before this fix, an absent switch
    button always returned False, meaning the caller's `in_edit_mode`
    flag never got set True and the mandatory `finally: _cancel_edit()`
    unlock never ran — leaving a real booking locked."""
    s, page = _ncl_scraper_with_fake_page({"#res-edit-cancel"})  # switch button absent
    result = await s._switch_to_edit_mode()
    assert result is True, "must report locked so the caller's finally-block unlock still fires"


@pytest.mark.asyncio
async def test_switch_to_edit_mode_returns_false_when_editing_genuinely_unavailable():
    """The other real reason the switch button can be absent: editing
    just isn't offered for this booking at all. Must NOT be
    misreported as locked (that would make the caller attempt a
    _cancel_edit() that has nothing to cancel — harmless, but a false
    signal in the logs worth keeping accurate)."""
    s, page = _ncl_scraper_with_fake_page(set())  # nothing present at all
    result = await s._switch_to_edit_mode()
    assert result is False


# ── _summarize_addon_change: real example from booking 3000007 ────────


def test_summarize_addon_change_detects_the_real_confirmed_swap():
    """Real example, confirmed 2026-08-26 (project owner's own external
    report + a recorded live session): re-selecting the same category
    converted "FREE PREPAID SERVICE CHARGES" into a "Free $50 On-Board
    Credit Certificate Non-Refundable" addon, per guest."""
    before = [
        {"guest": "MRS CHERI A MINIX", "name": "Wi-Fi Package: 150 mins", "qty": 1},
        {"guest": "MRS MARIA GBUR", "name": "Wi-Fi Package: 150 mins", "qty": 1},
        {"guest": "MRS CHERI A MINIX", "name": "Excursion Credit", "qty": 1},
        {"guest": "MRS CHERI A MINIX", "name": "FREE PREPAID SERVICE CHARGES", "qty": 1},
        {"guest": "MRS MARIA GBUR", "name": "FREE PREPAID SERVICE CHARGES", "qty": 1},
    ]
    after = [
        {"guest": "MRS CHERI A MINIX", "name": "Wi-Fi Package: 150 mins", "qty": 1},
        {"guest": "MRS MARIA GBUR", "name": "Wi-Fi Package: 150 mins", "qty": 1},
        {"guest": "MRS CHERI A MINIX", "name": "Excursion Credit", "qty": 1},
        {"guest": "MRS CHERI A MINIX", "name": "Free $50 On-Board Credit Certificate Non -Refundable", "qty": 1},
        {"guest": "MRS MARIA GBUR", "name": "Free $50 On-Board Credit Certificate Non -Refundable", "qty": 1},
    ]
    summary = _summarize_addon_change(before, after)
    assert "lost" in summary
    assert "gained" in summary
    assert "FREE PREPAID SERVICE CHARGES" in summary
    assert "On-Board Credit Certificate" in summary


def test_summarize_addon_change_empty_when_nothing_changed():
    same = [{"guest": "A GUEST", "name": "Excursion Credit", "qty": 1}]
    assert _summarize_addon_change(same, same) == ""


def test_summarize_addon_change_handles_none_and_empty_lists():
    assert _summarize_addon_change(None, None) == ""
    assert _summarize_addon_change([], []) == ""
    only_after = [{"guest": "A", "name": "Wi-Fi", "qty": 1}]
    assert "gained" in _summarize_addon_change(None, only_after)
    assert "lost" in _summarize_addon_change(only_after, None)


# ── auto_login: never-raises contract (mirrors MSC's auto_login) ───────


@pytest.mark.asyncio
async def test_auto_login_returns_status_when_no_credentials_saved(monkeypatch):
    """Must short-circuit on the credential check BEFORE touching the
    network/page at all — this runs on a scraper with no browser
    started, so any attempt to navigate would raise instead of
    returning a status. Mirrors msc_commands.auto_login's contract:
    NEVER raises, always returns a status string the caller can branch
    on to fall back to a manual login."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, key: None)
    s = NclScraper()
    assert await s.auto_login() == "NO_CREDENTIALS_SAVED"


@pytest.mark.asyncio
async def test_auto_login_never_raises_on_page_failure(monkeypatch):
    """With credentials present but no started browser, navigate() fails —
    that must come back as an "ERROR: ..." status, not an exception, so
    a failed auto-login can never crash a run."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, key: "placeholder")
    s = NclScraper()
    status = await s.auto_login()
    assert status.startswith("ERROR:"), status


# ── LATRIPLE hard gate (project owner's rule, 2026-08-26) ─────────────
#
# "if LATRIPLE is before only we do not optimize the booking, and we
#  optimize if it is after, or if a booking does not have LATRIPLE before
#  but does have it after then we optimize also"


def test_latriple_lost_blocks_optimization_real_example():
    """REAL confirmed case, booking 3000003 (2026-08-26 live run): promos
    went from "...EASYFARE, LATRIPLE, MAP10OFF..." to
    "...EASYFARE | LATREW | MAP10OFF..." while the fare dropped $72.
    LATRIPLE present before, gone after -> must NOT be an optimization."""
    from core.calculator import calculate_ncl
    from core.models import BookingStatus

    r = calculate_ncl(
        "3000003", "IT", 2153.10, 2081.10, [],
        "DISC50, DISXIN, EASYFARE, LATRIPLE, MAP10OFF, MILITARY, NCLHBENE, SHX50",
        "DISC50 | DISXIN | EASYFARE | LATREW | MAP10OFF | MILITARY | NCLHBENE | SHX50",
    )
    assert r.status != BookingStatus.OPTIMIZATION
    assert r.status == BookingStatus.TRAP
    assert "LATRIPLE" in r.note


def test_latriple_kept_still_allows_optimization():
    """before=Y after=Y -> nothing lost, normal optimization applies."""
    from core.calculator import calculate_ncl
    from core.models import BookingStatus

    r = calculate_ncl("X", "IT", 2153.10, 2081.10, [], "EASYFARE, LATRIPLE", "EASYFARE | LATRIPLE")
    assert r.status == BookingStatus.OPTIMIZATION


def test_latriple_gained_allows_optimization():
    """before=N after=Y -> explicitly called out as OK by the rule."""
    from core.calculator import calculate_ncl
    from core.models import BookingStatus

    r = calculate_ncl("X", "IT", 2153.10, 2081.10, [], "EASYFARE, LATREW", "EASYFARE | LATRIPLE")
    assert r.status == BookingStatus.OPTIMIZATION


def test_no_latriple_either_side_is_unaffected():
    """before=N after=N -> rule never involved, ordinary logic applies."""
    from core.calculator import calculate_ncl
    from core.models import BookingStatus

    r = calculate_ncl("X", "BA", 2878.00, 2858.00, [], "DISC50, EASYFARE", "DISC50 | EASYFARE")
    assert r.status == BookingStatus.OPTIMIZATION


def test_promo_parsing_handles_both_real_separators():
    """Confirmed real formats: the booking header is comma-separated, the
    category-grid row is pipe-separated. Both must parse."""
    from core.calculator import _split_promo_codes

    assert _split_promo_codes("DISC50, EASYFARE, LATRIPLE") == {"DISC50", "EASYFARE", "LATRIPLE"}
    assert _split_promo_codes("DISC50 | EASYFARE | LATREW") == {"DISC50", "EASYFARE", "LATREW"}
    assert _split_promo_codes("  latriple ,, ") == {"LATRIPLE"}   # case + stray separators
    assert _split_promo_codes(None) == set()
    assert _split_promo_codes("") == set()
