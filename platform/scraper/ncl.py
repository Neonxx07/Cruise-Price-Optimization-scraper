"""NCL scraper — Norwegian Cruise Line via SeaWeb portal.

CONFIRMED 2026-08-26 (directly by the project owner, and independently
by a real recorded login+search+edit session for booking 3000007):
NCL's real comparison is a SAME-CATEGORY reprice, matching ESPRESSO's
shape — NOT a switch to a cheaper, different category. This file was
originally ported from adapter_ncl.js assuming the latter; that
assumption was wrong and has been corrected (see check_booking's own
comments for the full redesign). Flow: search → read booking data →
scrape addons → enter edit mode → Category tab → read the CURRENT
category's own live grid price (no click needed for this part) → if it
differs from the booking's locked-in total, re-select the SAME category
(handling a real native confirm() dialog this triggers) → read the
recalculated promo/addon state from Reservation Summary → calculate →
ALWAYS cancel edit.

⚠️ THE 30-MINUTE LOCK: Entering edit mode locks the booking for 30 minutes.
The finally block ALWAYS cancels edit to release the lock, even on error.
CRITICAL, confirmed 2026-08-26: clicking Cancel Edit is NOT sufficient by
itself — a follow-up confirmation dialog (`#dialog_confirm_ok`) must also
be clicked, or the lock likely never actually releases. See _cancel_edit.
"""

from __future__ import annotations

import asyncio

from config.settings import settings
from core.calculator import (
    calculate_ncl,
    make_error_result,
    make_paid_in_full_result,
)
from core.models import BookingResult, CruiseLine
from utils.logging import get_logger

from .base import BaseScraper, is_dead_browser_error

logger = get_logger(__name__)


def _resolve_new_total(new_data: dict, fallback: float) -> float:
    """Pull the real re-read total out of `_read_new_total`'s result, or
    fall back to the pre-selection grid value on a failed re-read.

    Extracted as a pure function (2026-08-26, alongside the fix for the
    dead-fallback bug this closes) so the None-vs-zero distinction is
    directly unit-testable without a live page. `new_data["resTotal"]` is
    `null`/None specifically when the re-read couldn't find/parse a real
    value — `.get()`'s own default can never fire here since the key is
    always present in `_read_new_total`'s return shape; a genuine
    `resTotal: 0` must be trusted as a real (if odd) value, not treated
    as a failure."""
    new_total = new_data.get("resTotal")
    return fallback if new_total is None else new_total


def _summarize_addon_change(before: list[dict] | None, after: list[dict] | None) -> str:
    """Compare before/after per-guest addon lists (each a
    `{"name", "qty", "guest"}` dict, see `_scrape_addons`) and return a
    compact "lost X; gained Y" summary, or "" if nothing changed.

    Added 2026-08-26, confirmed against a real example (booking
    3000007): re-selecting the same category converted "FREE PREPAID
    SERVICE CHARGES" into a "Free $50 On-Board Credit Certificate
    Non-Refundable" addon for each guest — a real swap, not a hypothetical.
    Deliberately informational only (surfaced in the result's note, see
    check_booking) — NOT folded into net_saving; see check_booking's own
    comment on why this project owner's real reference report doesn't
    adjust its dollar Verdict for addon changes either."""
    before = before or []
    after = after or []
    before_keys = {(a.get("guest", ""), a.get("name", "")) for a in before}
    after_keys = {(a.get("guest", ""), a.get("name", "")) for a in after}
    lost = before_keys - after_keys
    gained = after_keys - before_keys
    if not lost and not gained:
        return ""

    def _fmt(pairs):
        return ", ".join(f"{g}: {n}" if g else n for g, n in sorted(pairs))

    parts = []
    if lost:
        parts.append("lost " + _fmt(lost))
    if gained:
        parts.append("gained " + _fmt(gained))
    return "; ".join(parts)


class NclScraper(BaseScraper):
    """Scraper for NCL SeaWeb portal."""

    cruise_line = CruiseLine.NCL

    def __init__(self):
        super().__init__()
        self._dialog_handler_installed = False
        # Every dialog seen this session: {"type", "message", "action"}.
        # Surfaced for post-run review (and by run_ncl_live_check.py) since
        # an UNEXPECTED dialog on this portal is exactly the kind of thing
        # that silently breaks a flow — see _install_dialog_handler.
        self.dialogs_seen: list[dict] = []

    def _install_dialog_handler(self) -> None:
        """Install ONE persistent dialog handler for the whole session.

        REPLACES the per-action `page.once("dialog", ...)` approach
        (2026-08-26): a one-off handler only covers the single action it
        wraps, so ANY dialog firing at an unanticipated moment falls
        through to Playwright's default — and Playwright's default is to
        DISMISS. This portal is unusually dialog-heavy (the real recorded
        session for booking 3000007 captured 24 separate "Page tried to
        navigate away" events plus two distinct confirms), so a
        session-wide handler that logs everything and decides per dialog
        TYPE is the robust shape here. Established Playwright practice —
        see Playwright's own dialogs docs, which branch on
        `dialog.type()` rather than blind-accepting.

        THIRD REAL DIALOG BUG this closes (found 2026-08-26 while
        implementing the persistent handler): `beforeunload` dialogs.
        Playwright auto-DISMISSES an unhandled dialog, and dismissing a
        `beforeunload` means "stay on this page" — i.e. it CANCELS the
        navigation. The recorded session shows this portal raising
        beforeunload constantly (every tab change during an active edit),
        and the human confirmed/allowed each one. Left unhandled, those
        navigations could silently fail to happen, leaving the scraper
        reading a stale page and drawing conclusions from it. Accepted
        explicitly here.

        Anything genuinely unrecognized is DISMISSED (the conservative
        choice — never blind-accept an unknown prompt on a real client
        booking) and logged loudly for review."""
        if self._dialog_handler_installed:
            return

        # Never let installing this be the thing that breaks a run.
        # CONFIRMED REGRESSION, caught 2026-08-26 by
        # test_regression_ncl_dead_browser_error_propagates_not_swallowed:
        # this became the FIRST code in check_booking to touch
        # `self.page`, whose property raises a plain RuntimeError when
        # the page is absent/dead. That error is not dead-browser-SHAPED,
        # so it got swallowed into an ordinary ERROR result and defeated
        # BookingService's restart path — the exact bug the 2026-08-13 fix
        # in check_booking's own except block exists to prevent. The
        # handler is defense-in-depth, not a critical step: if it can't be
        # installed, log it and continue (Playwright's default
        # dialog handling applies, which is the pre-2026-08-26 behavior)
        # and let the real flow below surface the real error.
        try:
            page = self.page
        except Exception as e:
            logger.warning("ncl.dialog_handler_install_skipped", error=str(e))
            return

        async def _on_dialog(dialog):
            dtype = dialog.type
            message = (dialog.message or "").strip()
            action = "dismissed"
            try:
                if dtype == "beforeunload":
                    # Allow the navigation — see docstring above.
                    await dialog.accept()
                    action = "accepted"
                elif dtype == "confirm":
                    # Both real confirms this portal is known to raise are
                    # expected parts of the read-only flow: the
                    # category-change fare warning, and Cancel Edit's
                    # "will be deleted from temporary storage" (which is
                    # about discarding the TEMP edit, i.e. exactly the
                    # release we want — not deleting a real booking).
                    await dialog.accept()
                    action = "accepted"
                else:
                    # alert/prompt — nothing in the known flow raises
                    # these. Dismiss rather than guess, and make it loud.
                    await dialog.dismiss()
                    logger.warning("ncl.unexpected_dialog", dialog_type=dtype, message=message)
            except Exception as e:
                logger.warning("ncl.dialog_handler_error", dialog_type=dtype, error=str(e))
            self.dialogs_seen.append({"type": dtype, "message": message, "action": action})
            logger.info("ncl.dialog", dialog_type=dtype, handled_as=action, message=message[:200])
            # NOTE: `log_action`'s own first parameter is named `action`,
            # so the disposition must be passed under a different kwarg
            # name here (caught by a real self-test 2026-08-26 — passing
            # `action=` raised TypeError: got multiple values for
            # argument 'action', inside an event handler where it
            # surfaced only as a stray asyncio callback traceback).
            self.log_action("dialog", dialog_type=dtype, handled_as=action, message=message[:200])

        page.on("dialog", _on_dialog)
        self._dialog_handler_installed = True
        logger.info("ncl.dialog_handler_installed")

    async def _check_login(self) -> bool:
        """Whether the current session is actually authenticated.

        Navigates to the search page and checks whether NCL let us stay
        there. A stale/expired session gets bounced to
        `/Security/login/?BackURL=...` with "That page is secured" — the
        real behavior confirmed live 2026-08-26."""
        try:
            await self.navigate(settings.ncl_search_url)
        except Exception as e:
            logger.warning("ncl.check_login_navigate_failed", error=str(e))
            return False
        url = self.page.url.lower()
        if "login" in url or "signin" in url:
            return False
        return bool(await self.page.query_selector("#SWXMLForm_SearchReservation_ResID"))

    async def auto_login(self) -> str:
        """Log into SeaWeb Agents using credentials saved via
        save_login.py (OS-native encrypted store — Windows Credential
        Manager / macOS Keychain; see that script's docstring).

        NEVER RAISES — returns a status string instead, exactly like
        MSC's `auto_login` in msc_commands.py, so a caller can fall back
        to a manual login prompt rather than crashing the whole run.
        Return values: "OK", "NO_CREDENTIALS_SAVED",
        "INVALID_CREDENTIALS", "TIMEOUT_WAITING_FOR_LOGIN", or
        "ERROR: ...".

        Real form structure CONFIRMED 2026-08-26 from a real recorded
        session (Chrome DevTools Recorder export of an actual login):
        `#LoginForm_LoginForm_Email` (labeled "Username" on screen —
        the id says Email but the real recorded value was a plain
        username, NOT an email address), `#LoginForm_LoginForm_Password`,
        and submit `#LoginForm_LoginForm_action_doLogin`. No MFA, no SSO
        redirect, and no cookie banner appeared on that session — unlike
        ESPRESSO, this login looks genuinely automatable. Still treated
        as best-effort: if anything about that changes (NCL has a newer
        "Norwegian Central" SSO layer that some accounts may be migrated
        to), this returns a status and the human logs in manually.

        Never logs, prints, or returns the credential values themselves.
        """
        try:
            import keyring
        except Exception as e:  # keyring is a hard dependency, but never crash login on it
            return f"ERROR: keyring unavailable ({e})"

        username = keyring.get_password(settings.ncl_credential_service, "username")
        password = keyring.get_password(settings.ncl_credential_service, "password")
        if not username or not password:
            return "NO_CREDENTIALS_SAVED"

        try:
            # 1) If the restored session is STILL valid, don't touch the
            # login form at all. Confirmed necessary 2026-08-26: BaseScraper
            # restores storage_state on every start(), and re-submitting a
            # login on top of a live session is both pointless and risky
            # (NCL, like ESPRESSO, appears to allow only one active session
            # per account — see the upgrade-backlog memory's section L).
            if await self._check_login():
                logger.info("ncl.auto_login", result="OK", note="restored session already valid")
                return "OK"

            # 2) Not authenticated — so any restored cookies are STALE.
            # CONFIRMED REAL FAILURE, 2026-08-26: submitting the login form
            # while stale cookies were present was silently rejected (page
            # re-rendered the bare form, no error text). SeaWeb is
            # SilverStripe, which embeds a per-session CSRF token
            # ("SecurityID") in the form — a token minted against a fresh
            # page load will not match a stale restored session cookie, so
            # the POST is discarded. Clearing cookies first makes the form
            # token and the session cookie come from the SAME fresh
            # session, which is what a real human login does.
            try:
                if self._context is not None:
                    await self._context.clear_cookies()
                    logger.info("ncl.auto_login_cleared_stale_cookies")
            except Exception as e:
                logger.warning("ncl.auto_login_cookie_clear_failed", error=str(e))

            await self.navigate(settings.ncl_login_url)
            await self.wait_for("#LoginForm_LoginForm_Password", timeout=15000)
            await self.page.fill("#LoginForm_LoginForm_Email", username)
            await self.page.fill("#LoginForm_LoginForm_Password", password)
            self.log_action("auto_login_submit")  # deliberately logs no values
            await self.page.click("#LoginForm_LoginForm_action_doLogin")

            # Poll for a real outcome rather than trusting the click.
            for _ in range(40):  # ~20s
                url = self.page.url.lower()
                if "login" not in url and "signin" not in url:
                    logger.info("ncl.auto_login", result="OK")
                    return "OK"
                try:
                    body = (await self.page.inner_text("body")).lower()
                except Exception:
                    body = ""
                if any(
                    marker in body
                    for marker in ("invalid", "incorrect", "not recognized", "try again")
                ):
                    logger.warning("ncl.auto_login", result="INVALID_CREDENTIALS")
                    return "INVALID_CREDENTIALS"
                await self.page.wait_for_timeout(500)

            # CONFIRMED REAL BEHAVIOR, 2026-08-26 (live diagnostic run):
            # NCL shows NO error text at all on a rejected login — it just
            # re-renders the bare login form and appends a
            # `#LoginForm_LoginForm` fragment to the URL (SilverStripe
            # posting a form back to itself). So the text-marker check
            # above can never fire, and a simply-wrong credential would
            # otherwise always report the misleading
            # TIMEOUT_WAITING_FOR_LOGIN. Distinguish the two: if the
            # password field is still on the page after submitting, the
            # login was rejected, not slow.
            try:
                still_on_form = await self.page.query_selector("#LoginForm_LoginForm_Password")
            except Exception:
                still_on_form = None
            if still_on_form:
                logger.warning(
                    "ncl.auto_login",
                    result="INVALID_CREDENTIALS",
                    note="login form still present after submit and no redirect — "
                         "NCL shows no error text on rejection, so this is the reliable signal",
                )
                return "INVALID_CREDENTIALS"

            logger.warning("ncl.auto_login", result="TIMEOUT_WAITING_FOR_LOGIN")
            return "TIMEOUT_WAITING_FOR_LOGIN"
        except Exception as e:
            logger.warning("ncl.auto_login", result="ERROR", error=str(e))
            return f"ERROR: {e}"

    async def _search_booking(self, booking_id: str) -> None:
        """Submit booking ID on the SeaWeb search page.

        CONFIRMED REAL FIX, 2026-08-26: a real recorded login+search
        session (Chrome DevTools Recorder export, booking 3000007)
        confirms the actual search-submit button ID is
        `#SWXMLForm_SearchReservation_action_DoReservationSearch` —
        `#lookup-button` (the original guessed primary selector) never
        matched anything real. Kept as a fallback in case a different
        page state renders it, but the confirmed ID now goes first. The
        rest of the fallback chain (spec §5.2 step 2, still unconfirmed)
        is kept as breadth, not a single guessed selector."""
        await self.page.fill("#SWXMLForm_SearchReservation_ResID", booking_id)
        if await self.page.query_selector("#SWXMLForm_SearchReservation_action_DoReservationSearch"):
            await self.page.click("#SWXMLForm_SearchReservation_action_DoReservationSearch")
            return
        if await self.page.query_selector("#lookup-button"):
            await self.page.click("#lookup-button")
            return
        submitted = await self.page.evaluate("""
            (() => {
                const input = document.querySelector('#SWXMLForm_SearchReservation_ResID');
                const form = input?.closest('form');
                const btn = form?.querySelector('[type="submit"]');
                if (btn) { btn.click(); return true; }
                if (form && typeof form.submit === 'function') { form.submit(); return true; }
                const candidates = Array.from(document.querySelectorAll('button, input[type="submit"]'));
                const fallback = candidates.find(el => {
                    const text = (el.textContent || el.value || '').trim().toLowerCase();
                    return text.includes('go') || text.includes('search');
                });
                if (fallback) { fallback.click(); return true; }
                return false;
            })()
        """)
        if not submitted:
            # Last resort, matches the original single-selector behavior.
            await self.page.click('[type="submit"]')

    async def _read_preloaded_data(self) -> dict:
        """Read booking state from window.__preloaded_data."""
        return await self.page.evaluate("""
            (() => {
                try {
                    const d = window.__preloaded_data;
                    if (!d) return { ok: false, error: '__preloaded_data not found' };
                    return {
                        ok: true,
                        resId: d.ResID || d.bi?.ResID,
                        isPaid: d.bi?.IsPaid || false,
                        isLocked: d.bi?.IsLocked || false,
                        category: d.bi?.Category || d.category || null,
                        // CONFIRMED REAL RISK, fixed 2026-08-13: `||` treats a
                        // genuine $0 InvoiceTotal the same as missing/undefined,
                        // silently falling through to a fallback field or a
                        // hardcoded 0 either way — indistinguishable from a real
                        // parse failure. `??` (nullish coalescing) only falls
                        // through on null/undefined, so a real zero survives,
                        // while a genuinely absent value now becomes `null`
                        // (read on the Python side, below) instead of a fake 0
                        // that would silently corrupt price_drop/net_saving.
                        invoiceTotal: (d.bi?.InvoiceTotal ?? d.baseInvoice?.INVOICE_TOTAL) ?? null,
                        promos: d.bi?.guests
                            ? Object.values(d.bi.guests || {}).map(g => g.Promos || '').join(',')
                            : '',
                        currentPromos: (() => {
                            const item = document.querySelector('.item.current');
                            if (!item) return '';
                            const row = Array.from(item.querySelectorAll('.row'))
                                .find(r => r.textContent.includes('Curr. Promos'));
                            return row?.querySelector('.value')?.textContent?.trim() || '';
                        })(),
                    };
                } catch(e) { return { ok: false, error: e.message }; }
            })()
        """)

    async def _scrape_addons(self) -> list[dict]:
        """Scrape the Addons table from the Reservation Summary page.

        WIDENED 2026-08-26 (real recorded session, booking 3000007):
        confirmed the real table has exactly 3 columns — Guest Name,
        Addon Name, Quantity — one row PER (guest, addon) pair, e.g. two
        guests each with "Wi-Fi Package: 150 mins" produce two separate
        rows. The original scraper read this positionally (cells[1] as
        name, cells[2] as qty), which — against the CONFIRMED real
        3-column shape — would have read the GUEST NAME as the addon
        name and the ADDON NAME as the quantity, silently wrong on every
        row. Now locates columns by their real header text ("Guest
        Name"/"Addon Name"/"Quantity") instead of trusting position, and
        returns the guest name alongside each addon — needed to build
        the same per-guest "Add-Ons Before/After Drop" comparison the
        project owner's own reference report already produces. The
        original positional `#transformation > ...` CSS path is kept as
        a first-try table locator (unconfirmed either way, harmless if
        it doesn't match) before falling back to the header-text search."""
        result = await self.page.evaluate("""
            (() => {
                try {
                    let table = document.querySelector(
                        '#transformation > div > div > div:nth-child(3) > div.content.clearfix > table'
                    );
                    if (!table || !Array.from(table.querySelectorAll('th')).some(h => h.textContent.includes('Addon'))) {
                        table = null;
                        for (const t of document.querySelectorAll('table')) {
                            const headers = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim());
                            if (headers.some(h => h.includes('Addon Name') || h.includes('Addon'))) {
                                table = t; break;
                            }
                        }
                    }
                    if (!table) return [];
                    const headers = Array.from(table.querySelectorAll('th')).map(h => h.textContent.trim());
                    const guestIdx = headers.findIndex(h => h.includes('Guest'));
                    const nameIdx = headers.findIndex(h => h.includes('Addon'));
                    const qtyIdx = headers.findIndex(h => h.includes('Quantity') || h.includes('Qty'));
                    const addons = [];
                    for (const row of table.querySelectorAll('tbody tr')) {
                        const cells = Array.from(row.querySelectorAll('td, th'));
                        // Fallback to the original fixed positions (guest=0,
                        // name=1, qty=2) only if header-based lookup found
                        // nothing -- keeps working even if headers are
                        // missing/unparsed, without silently misreading a
                        // confirmed 3-column table the wrong way.
                        const guest = cells[guestIdx >= 0 ? guestIdx : 0]?.textContent?.trim() || '';
                        const name = cells[nameIdx >= 0 ? nameIdx : 1]?.textContent?.trim();
                        const qtyText = cells[qtyIdx >= 0 ? qtyIdx : 2]?.textContent?.trim();
                        const qty = parseInt(qtyText) || 1;
                        if (name && name.length > 2) addons.push({ name, qty, guest });
                    }
                    return addons;
                } catch(e) { return []; }
            })()
        """)
        return result or []

    async def _switch_to_edit_mode(self) -> bool:
        """Click Switch to Edit Mode. Returns True if edit mode activated.

        The click itself acquires NCL's server-side 30-minute edit lock —
        confirmed real bug 2026-08-12: if the confirmation wait below timed
        out on a slow render, the original code let that exception
        propagate BEFORE this function ever returned, so the caller's
        `in_edit_mode` flag never got set and the `finally: _cancel_edit()`
        release never fired — leaving a real booking locked for 30 minutes
        with the code believing it had never entered edit mode. The lock is
        already acquired the moment the click succeeds; the wait below only
        confirms the UI caught up, so a slow render must never cost us the
        "we're locked, remember to release it" signal."""
        has_btn = await self.page.query_selector("#res-switch-edit")
        if has_btn:
            await self.page.click("#res-switch-edit")
            try:
                await self.wait_for("#res-edit-save, a[href*='storeBooking']", timeout=12000)
            except Exception as e:
                logger.warning("ncl.edit_mode_confirm_timeout", error=str(e))
            return True

        # CONFIRMED REAL GAP, fixed 2026-08-26 (found by code-review audit,
        # never yet run live): the switch-to-edit button can be absent for
        # two different reasons — (1) this booking is already mid-edit
        # (e.g. a prior run acquired the lock and crashed before releasing
        # it, or two automated runs raced), in which case the booking IS
        # locked and the caller's `finally: _cancel_edit()` must still run;
        # or (2) editing genuinely isn't offered for this booking. This
        # used to always assume (2) and return False, meaning `in_edit_mode`
        # never got set True and the lock (if actually held) was never
        # released — a real instance of exactly the 30-minute-lock risk
        # this file otherwise guards carefully against. Check for the
        # edit-mode-only controls directly instead of assuming.
        already_editing = await self.page.query_selector("#res-edit-save, #res-edit-cancel")
        if already_editing:
            logger.warning(
                "ncl.already_in_edit_mode",
                msg="switch-to-edit button absent but edit-mode controls are present — "
                    "booking is already locked, will still attempt release",
            )
            return True
        return False

    async def _cancel_edit(self) -> None:
        """ALWAYS call this to release the 30-minute booking lock.

        WIDENED 2026-08-26 to the full 5-tier cascade in RECREATE_PROMPT.md
        §5.3 (never yet run live, so kept as fallback breadth rather than
        trusting only the first two tiers): added the `viewMode` link tier
        and the direct viewUrl-navigation tier, both BEFORE finally giving
        up — this is the single most consequential function in the file
        (a failure here means a real client booking stays locked for 30
        minutes), so it gets every fallback the spec calls for, not just
        the two most likely to work.

        CRITICAL FIX, 2026-08-26, found from a real recorded session
        (booking 3000007): clicking `#res-edit-cancel` does NOT complete
        the unlock by itself — the real portal then shows a custom
        confirmation modal ("Current Reservation will be deleted from
        temporary storage!") with an OK button, confirmed real ID
        `#dialog_confirm_ok`. Before this fix, `_cancel_edit` clicked
        Cancel Edit and returned, NEVER confirming this dialog — meaning
        the booking likely stayed locked for the full 30 minutes on
        every real run, the exact failure mode this whole file exists to
        prevent. Now waits briefly for the dialog and clicks it if it
        appears; if it never appears (e.g. a future portal build removes
        it), proceeds without erroring, since we can't be sure it's
        actually required in every case."""
        try:
            cancel_btn = await self.page.query_selector("#res-edit-cancel")
            if cancel_btn:
                await cancel_btn.click()
                logger.info("ncl.unlock", method="#res-edit-cancel")
                try:
                    await self.wait_for("#dialog_confirm_ok", timeout=5000)
                    await self.page.click("#dialog_confirm_ok")
                    logger.info("ncl.unlock_confirm_dialog", method="#dialog_confirm_ok")
                except Exception as confirm_err:
                    logger.warning(
                        "ncl.unlock_confirm_dialog_not_found",
                        error=str(confirm_err),
                        msg="proceeding without it -- the unlock may not have fully completed",
                    )
                await asyncio.sleep(0.5)
                return

            # Tier 2: text match
            by_text = await self.page.evaluate("""
                (() => {
                    const el = Array.from(document.querySelectorAll('a, button'))
                        .find(el => el.textContent.trim().toUpperCase() === 'CANCEL EDIT');
                    if (el) { el.click(); return true; }
                    return false;
                })()
            """)
            if by_text:
                logger.info("ncl.unlock", method="text-match")
                await asyncio.sleep(0.5)
                return

            # Tier 3: a direct "view mode" link, if the portal offers one
            # as an alternate way out of edit mode.
            by_view_link = await self.page.evaluate("""
                (() => {
                    const el = document.querySelector('a[href*="viewMode"]');
                    if (el) { el.click(); return true; }
                    return false;
                })()
            """)
            if by_view_link:
                logger.info("ncl.unlock", method="viewMode-link")
                await asyncio.sleep(0.5)
                return

            # Tier 4: no clickable escape hatch found at all — reconstruct
            # the view-mode URL directly from the current edit URL and
            # navigate there. Only valid when the current path is actually
            # an edit URL (`/edit/`); if it isn't, there's nothing safe to
            # construct, so fall through to the tier-5 warning instead.
            view_url = await self.page.evaluate("""
                (() => {
                    const href = location.href;
                    if (!href.includes('/edit/')) return null;
                    return href.replace('/edit/', '/view/').split('?')[0] + 'doform/viewMode?';
                })()
            """)
            if view_url:
                await self.navigate(view_url)
                logger.info("ncl.unlock", method="direct-viewUrl-navigation")
                await asyncio.sleep(0.5)
                return

            # Tier 5: nothing worked — this is the most severe failure mode
            # in the whole system (a real booking stays locked for 30
            # minutes). Logged, never raised — the outer caller already
            # treats this path as best-effort cleanup, not a retryable step.
            logger.warning("ncl.unlock_failed", msg="No cancel edit mechanism found")
        except Exception as e:
            logger.error("ncl.unlock_critical", error=str(e))

    async def _click_category_tab(self) -> None:
        """Navigate to the Category tab."""
        await self.page.evaluate("""
            (() => {
                const link = Array.from(document.querySelectorAll('a'))
                    .find(a => a.href?.includes('/agent-edit-category/') && a.textContent.trim() === 'Category');
                if (link) { link.click(); return; }
                const fb = document.querySelector('a[href*="agent-edit-category"]');
                if (fb) fb.click();
            })()
        """)
        await self.wait_for("#SWXMLForm_SelectCategory_category, .slick-viewport", timeout=12000)
        await asyncio.sleep(0.6)  # Let SlickGrid render

    async def _read_category_data(self) -> dict:
        """Read all categories from VX._form_12 (SlickGrid data model)."""
        return await self.page.evaluate("""
            (() => {
                try {
                    const categories = window.VX?.get('_form_12');
                    if (!categories || !Array.isArray(categories))
                        return { ok: false, error: 'VX._form_12 not available' };
                    const currentVal = window.VX?.get('_form_10')?.value?.[0] || null;
                    return {
                        ok: true,
                        currentCategory: currentVal,
                        categories: categories.map(c => ({
                            category: c.Category,
                            resTotal: parseFloat(c.ResTotal) || 0,
                            status: c.Status,
                            hasAvailability: c.HasAvailability,
                            currentPromo: c.CurrentPromo || '',
                        }))
                    };
                } catch(e) { return { ok: false, error: e.message }; }
            })()
        """)

    async def _select_category(self, target_cat: str) -> bool:
        """Select a category via SlickGrid. Returns True on success.

        CRITICAL FIX, 2026-08-26, found from a real recorded session:
        clicking a category's "Select" link triggers a NATIVE browser
        confirm() dialog ("Selecting new category may result in change
        of fares. Do you want to proceed?") — confirmed via a Chrome
        DevTools Recorder export that explicitly handles it with
        `page.once('dialog', dialog => dialog.accept())`. Playwright's
        DEFAULT behavior for an unhandled `dialog` event is to auto-
        DISMISS it (not accept) — meaning without this fix, every real
        category selection would silently fail: the click "succeeds"
        from Playwright's point of view, but the dialog gets dismissed
        (equivalent to clicking Cancel) and the selection is rejected
        server-side.

        The handling itself now lives in the session-wide
        `_install_dialog_handler` (see its docstring) rather than a
        per-action `page.once(...)` here — a one-off handler only covers
        this single action, and this portal raises dialogs at many other
        points too."""
        self._install_dialog_handler()
        result = await self.page.evaluate(f"""
            (async () => {{
                try {{
                    const categories = window.VX?.get('_form_12');
                    if (!categories) return false;
                    const idx = categories.findIndex(c => c.Category === '{target_cat}');
                    if (idx < 0) return {{ ok: false, reason: 'category_not_in_grid_data' }};
                    // NOTE: HasAvailability is deliberately NOT required here.
                    // It was a leftover from the original "switch to a
                    // DIFFERENT cheaper category" design, where you genuinely
                    // need inventory to move INTO a category. For a
                    // SAME-category reprice (the real NCL model, confirmed
                    // 2026-08-26) the guest already occupies this category, so
                    // "is it open to new bookings" is a different question and
                    // must not block re-pricing what they already hold. Logged
                    // rather than enforced, so a real case where this matters
                    // is visible instead of silently refused.
                    const avail = categories[idx].HasAvailability;
                    const viewport = document.querySelector('.slick-viewport');
                    if (!viewport) return {{ ok: false, reason: 'slick_viewport_not_found', hasAvailability: avail }};

                    // Finds the target row among the CURRENTLY RENDERED rows.
                    const findRow = () => Array.from(viewport.querySelectorAll('.slick-row'))
                        .find(row => {{
                            const a = row.querySelector('.slick-cell.l0 a.infolink, .slick-cell:first-child a');
                            return a && a.textContent.trim() === '{target_cat}';
                        }});

                    // CONFIRMED REAL BUG, fixed 2026-08-26 (live run, booking
                    // 3000003 / category IT): SlickGrid is VIRTUALIZED — it
                    // only renders the rows in view. That booking's grid had
                    // 40+ categories but only 23 in the DOM, and IT was not
                    // among them, so the row search below found nothing and
                    // the whole check failed with "Category re-selection
                    // failed" even though IT's real price was sitting in the
                    // data model the entire time.
                    //
                    // The original scroll attempt relied on reaching the grid
                    // instance via jQuery (`$(el).data('SlickGrid')`) — live
                    // diagnostics proved that handle is NOT exposed on this
                    // portal (gridInstanceReachable: false), so the scroll
                    // silently never happened. Scroll the viewport element
                    // DIRECTLY instead, which needs no grid instance: estimate
                    // the row offset from a rendered row's height, then walk
                    // the viewport in steps as a fallback until the row
                    // renders. Re-query after every scroll, since virtualized
                    // rows are destroyed/recreated as they move in and out.
                    let row = findRow();
                    if (!row) {{
                        const sample = viewport.querySelector('.slick-row');
                        const rowH = (sample && sample.offsetHeight) || 25;
                        // Jump straight to the estimated position first.
                        viewport.scrollTop = Math.max(0, (idx * rowH) - (viewport.clientHeight / 2));
                        await new Promise(r => setTimeout(r, 250));
                        row = findRow();
                    }}
                    if (!row) {{
                        // Fallback: sweep the whole viewport top-to-bottom.
                        const step = Math.max(100, Math.floor(viewport.clientHeight * 0.8));
                        for (let pos = 0; pos <= viewport.scrollHeight; pos += step) {{
                            viewport.scrollTop = pos;
                            await new Promise(r => setTimeout(r, 150));
                            row = findRow();
                            if (row) break;
                        }}
                    }}
                    if (!row) {{
                        return {{
                            ok: false,
                            reason: 'row_never_rendered_after_scrolling',
                            hasAvailability: avail,
                            gridIndex: idx,
                            totalCategories: categories.length,
                            renderedRows: viewport.querySelectorAll('.slick-row').length,
                        }};
                    }}

                    // CONFIRMED real link text 2026-08-26 (recorded session):
                    // the row's select control is a plain <a> whose visible
                    // text is exactly "Select" -- kept alongside the original
                    // attribute-based guesses, which were never confirmed.
                    const selectBtn = row.querySelector('a[data-link-action="select"], a.navlink')
                        || Array.from(row.querySelectorAll('a')).find(a => a.textContent.trim() === 'Select');
                    if (!selectBtn) {{
                        return {{ ok: false, reason: 'select_link_not_found_in_row', hasAvailability: avail }};
                    }}
                    selectBtn.click();
                    await new Promise(r => setTimeout(r, 600));
                    return {{ ok: true, hasAvailability: avail, gridIndex: idx }};
                }} catch(e) {{
                    return {{ ok: false, reason: 'exception: ' + (e && e.message) }};
                }}
            }})()
        """)
        # Structured result (2026-08-26): the old bare `false` gave a caller
        # no way to tell "category missing from the data" from "row never
        # rendered" from "select link missing" — the IT/3000003 failure took
        # a dedicated live diagnostic run to identify for exactly that reason.
        if isinstance(result, dict):
            if not result.get("ok"):
                logger.warning("ncl.select_category_failed", target=target_cat, **{
                    k: v for k, v in result.items() if k != "ok"
                })
                return False
            logger.info(
                "ncl.select_category_ok", target=target_cat,
                grid_index=result.get("gridIndex"), has_availability=result.get("hasAvailability"),
            )
            return True
        return bool(result)

    async def _read_new_total(self, category: str) -> dict:
        """Read updated ResTotal from VX grid.

        CONFIRMED REAL BUG, fixed 2026-08-26 (found by code-review audit,
        never yet run live): this used to return `resTotal: 0` whenever
        the grid or the category lookup came back empty, indistinguishable
        from a genuine $0 total. The caller's fallback,
        `new_data.get("resTotal", target["resTotal"])`, can never trigger
        when the key is always present — so a failed re-read silently
        produced `new_total = 0`, which would make `calculate_ncl` compute
        a huge fake price_drop against the real old_total (a $0 "new
        price" reads as a massive fabricated OPTIMIZATION). Same `??`-vs-
        `||`/None-vs-zero discipline as `_read_preloaded_data` above:
        `resTotal` is now `null` (not `0`) when the value genuinely
        couldn't be read, so the Python side can tell "read a real $0"
        apart from "couldn't read it" and fall back to the pre-selection
        grid value instead of trusting a fabricated zero."""
        return await self.page.evaluate(f"""
            (() => {{
                const cats = window.VX?.get('_form_12');
                if (!cats) return {{ resTotal: null, currentPromo: '' }};
                const cat = cats.find(c => c.Category === '{category}');
                if (!cat) return {{ resTotal: null, currentPromo: '' }};
                const parsed = parseFloat(cat.ResTotal);
                return {{
                    resTotal: Number.isFinite(parsed) ? parsed : null,
                    currentPromo: cat.CurrentPromo || '',
                }};
            }})()
        """)

    async def _reapply_same_stateroom_if_prompted(self) -> None:
        """After re-selecting a category, NCL may land on a "Stateroom"
        step before the recalculated totals/promos/addons are available.

        BEST-EFFORT, added 2026-08-26 from a real recorded session
        (booking 3000007): the screenshots show the booking's own
        stateroom already pre-selected (a checked radio) on this page,
        with an "Apply" button — the recorded session did not capture an
        explicit click on it before moving to the Reservation Summary
        tab, so it's UNCONFIRMED whether an explicit Apply click is
        required or the page just carries the pre-selected stateroom
        forward on its own. This never selects a DIFFERENT stateroom —
        only ever tries to keep/confirm whatever is already selected —
        and is a no-op (returns immediately) if no stateroom-shaped page
        is present, so it's safe to call unconditionally. VERIFY ON THE
        FIRST LIVE RUN whether this is actually needed."""
        try:
            is_stateroom_step = await self.page.evaluate("""
                (() => {
                    const heading = Array.from(document.querySelectorAll('h1, h2, h3, .heading, legend'))
                        .some(el => el.textContent.trim() === 'Stateroom');
                    return heading || !!document.querySelector('[id*="Stateroom"]');
                })()
            """)
        except Exception:
            is_stateroom_step = False
        if not is_stateroom_step:
            return
        logger.info("ncl.stateroom_step_detected")
        clicked = await self.page.evaluate("""
            (() => {
                const btn = Array.from(document.querySelectorAll('button, input[type="button"], a'))
                    .find(el => (el.textContent || el.value || '').trim().toUpperCase() === 'APPLY');
                if (btn) { btn.click(); return true; }
                return false;
            })()
        """)
        if clicked:
            self.log_action("stateroom_reapplied")
            await asyncio.sleep(0.6)
        else:
            logger.warning("ncl.stateroom_apply_button_not_found")

    async def _goto_reservation_summary_and_read_warning(self) -> str | None:
        """Navigate to the Reservation Summary tab and capture NCL's own
        OBC/amenity-drop warning banner text, if present.

        ADDED 2026-08-26 from a real recorded session (booking 3000007):
        confirmed the portal itself surfaces a real, authoritative
        warning ("An OBC or AMENITY has been dropped due to a change in
        promotion/effective date; please verify that the guest is ok
        with the new promotion...") when re-selecting a category would
        change the addon/promo mix — a far more reliable signal than
        inferring the same fact by diffing two addon lists after the
        fact. Returns the banner text if found, else None. The nav-link
        selector is text-based ("Reservation Summary"), matching
        `_click_category_tab`'s existing style, since the recorded
        session's own selector for this link was a fragile
        position-based `li:nth-of-type(18)`, not something worth
        trusting as-is."""
        await self.page.evaluate("""
            (() => {
                const link = Array.from(document.querySelectorAll('a'))
                    .find(a => a.textContent.trim() === 'Reservation Summary');
                if (link) link.click();
            })()
        """)
        try:
            await self.wait_for('[class*="ReservationSummary"], .item.current', timeout=10000)
        except Exception as e:
            logger.warning("ncl.reservation_summary_after_select_timeout", error=str(e))
        await asyncio.sleep(0.4)
        return await self.page.evaluate("""
            (() => {
                const text = document.body.innerText || '';
                const m = text.match(/An OBC or AMENITY has been dropped[^\\n]*/i);
                return m ? m[0].trim() : null;
            })()
        """)

    async def check_booking(self, booking_id: str, capture_market_data: bool = False) -> BookingResult:
        """
        Full NCL booking check flow. ALWAYS unlocks in finally.

        Steps: navigate → search → read data → scrape addons →
        enter edit mode → load categories → find cheapest → calculate.
        """
        in_edit_mode = False
        addons: list[dict] = []
        old_total = 0.0
        current_category: str | None = None
        current_promos = ""

        try:
            # Step 0: install the session-wide dialog handler BEFORE any
            # navigation — this portal can raise a beforeunload dialog on
            # essentially any navigation during an active edit, and an
            # unhandled one gets auto-DISMISSED by Playwright (= navigation
            # silently cancelled). See _install_dialog_handler.
            self._install_dialog_handler()

            # Step 1: Navigate
            logger.info("ncl.navigate", booking_id=booking_id)
            self.log_action("navigate", booking_id=booking_id, url=settings.ncl_search_url)
            await self.navigate(settings.ncl_search_url)
            await self.wait_for("#SWXMLForm_SearchReservation_ResID", timeout=15000)

            # Step 2: Search
            logger.info("ncl.search", booking_id=booking_id)
            self.log_action("search_booking", booking_id=booking_id)
            await self._search_booking(booking_id)
            await self.dump_page_snapshot(booking_id, "after_search")

            try:
                await self.wait_for(
                    '.item.current, #res-switch-edit, #res-edit-save, [class*="ReservationSummary"]',
                    timeout=20000,
                )
            except Exception:
                error_text = await self.page.evaluate("""
                    (() => {
                        const sels = [
                            '.error', '.alert', '#pageMessages', '.swmessage',
                            '[class*="error"]', '[class*="alert"]', '.field-error',
                        ];
                        for (const sel of sels) {
                            const el = document.querySelector(sel);
                            if (el) { const t = el.innerText?.trim(); if (t?.length > 3) return t; }
                        }
                        return null;
                    })()
                """)
                if error_text:
                    raise RuntimeError(f"NCL portal error: {error_text}")
                raise RuntimeError("Timeout waiting for booking summary — check login and booking ID")

            # Step 3: Read booking state
            preload = await self._read_preloaded_data()
            if not preload.get("ok"):
                raise RuntimeError(f"Cannot read __preloaded_data: {preload.get('error')}")

            # CONFIRMED REAL RISK, fixed 2026-08-13: invoiceTotal is now `null`
            # (not a silent 0) when __preloaded_data genuinely had no readable
            # total (see _read_preloaded_data's `??` fix above) — refuse to
            # proceed with a fabricated $0 booking value, which would have
            # silently corrupted price_drop for a real repricing decision.
            invoice_total = preload.get("invoiceTotal")
            if invoice_total is None:
                raise RuntimeError("NCL booking has no readable InvoiceTotal — refusing to guess $0")

            if preload.get("isPaid"):
                return make_paid_in_full_result(
                    booking_id, preload.get("category"), CruiseLine.NCL, invoice_total,
                )

            current_category = preload.get("category")
            old_total = invoice_total
            current_promos = preload.get("currentPromos", "")
            logger.info("ncl.booking_info", booking_id=booking_id, category=current_category, total=old_total)
            self.log_action(
                "booking_info", booking_id=booking_id, category=current_category, old_total=old_total,
            )
            await self.dump_page_snapshot(booking_id, "booking_summary")

            # Step 4: Scrape addons
            addons = await self._scrape_addons()
            logger.info("ncl.addons", booking_id=booking_id, count=len(addons))
            self.log_action("scrape_addons", booking_id=booking_id, count=len(addons))

            # Step 5: Enter edit mode (LOCKS booking for 30 min)
            in_edit_mode = await self._switch_to_edit_mode()
            logger.info("ncl.edit_mode", booking_id=booking_id, locked=in_edit_mode)
            self.log_action("edit_mode", booking_id=booking_id, locked=in_edit_mode)

            # Step 6: Category tab
            await self._click_category_tab()
            await self.dump_page_snapshot(booking_id, "categories_table")

            # Step 7: Read categories
            cat_data = await self._read_category_data()
            if not cat_data.get("ok"):
                raise RuntimeError(f"Cannot read categories: {cat_data.get('error')}")

            categories = cat_data["categories"]
            current = next((c for c in categories if c["category"] == current_category), None)
            if not current:
                raise RuntimeError(f"Category '{current_category}' not found in grid data")

            # Step 8 (REDESIGNED 2026-08-26): NCL's real comparison is a
            # SAME-CATEGORY reprice, confirmed directly by the project
            # owner and by a real recorded session (booking 3000007) —
            # NOT a switch to a cheaper, different category, which is
            # what this step used to search for. The category grid's
            # own listed price for the booking's CURRENT category is
            # already today's live price: the recorded session's own
            # screenshot shows the correct live total ($2,858.00 for
            # 3000007) sitting in this row BEFORE anything was ever
            # clicked. Reading it costs nothing — no click, no risk to
            # the booking, no dialog to handle.
            live_price = current["resTotal"]

            if live_price <= 0 or abs(live_price - old_total) < 0.01:
                # No live price change at all -- nothing to gain from the
                # invasive re-select flow below (which mutates a live
                # in-progress edit, even though it's ultimately
                # cancelled). Still fully inside the try block, so
                # `finally` still cancels edit mode.
                logger.info("ncl.no_price_change", booking_id=booking_id, category=current_category)
                return calculate_ncl(
                    booking_id, current_category, old_total, old_total,
                    addons, current_promos, current_promos,
                )

            logger.info(
                "ncl.price_changed", booking_id=booking_id, category=current_category,
                old_total=old_total, live_price=live_price,
            )

            # Step 9: re-select the SAME category. Confirmed 2026-08-26
            # (real recorded session): the live grid price alone doesn't
            # reveal what the promo/addon mix would become — that only
            # shows up after NCL's backend actually recalculates it,
            # which requires going through the real re-selection flow
            # (triggers the confirm() dialog handled inside
            # _select_category, then possibly a Stateroom re-apply step).
            selected = await self._select_category(current_category)
            if not selected:
                raise RuntimeError(f"Category re-selection failed for '{current_category}'")
            await asyncio.sleep(0.8)

            await self._reapply_same_stateroom_if_prompted()

            # Step 10: navigate to Reservation Summary and read the
            # recalculated "after" state — new total/promo off the same
            # grid read used for the "before" state, the per-guest addon
            # table (now reflecting the tentative reselection), and
            # NCL's own OBC/amenity-drop warning banner if it fired.
            warning_banner = await self._goto_reservation_summary_and_read_warning()
            new_addons = await self._scrape_addons()

            new_data = await self._read_new_total(current_category)
            new_total = _resolve_new_total(new_data, live_price)
            new_promos = new_data.get("currentPromo") or current.get("currentPromo", "")

            # Step 11: Calculate. `addons` (the BEFORE-edit list from step
            # 4) is passed here deliberately, matching calculate_ncl's
            # existing design — its lost-FOBC check compares the
            # BEFORE addon list against the promo change, not the after
            # list. `new_addons` is surfaced separately below, in the
            # note, for a human to review — NOT folded into the dollar
            # math. This matches the project owner's own reference
            # report (confirmed 2026-08-26): its "Verdict" column is
            # always the raw price difference, unadjusted for promo/addon
            # changes even when the addon composition visibly changed —
            # those columns are context for human review, not inputs to
            # the dollar figure. Whether an addon SWAP (e.g. losing "FREE
            # PREPAID SERVICE CHARGES" for a gained OBC certificate)
            # should ever offset net_saving is a real judgment call, not
            # yet resolved with the project owner — see
            # calculate_ncl's/lost_fobc's own docstring caveats.
            result = calculate_ncl(
                booking_id, current_category, old_total, new_total,
                addons, current_promos, new_promos,
            )
            result.new_price_category = current_category  # same category, not a switch

            addon_change_note = _summarize_addon_change(addons, new_addons)
            if addon_change_note:
                result.note = f"{result.note} — addons changed: {addon_change_note}"
            if warning_banner:
                result.note = f"{result.note} — NCL flagged: {warning_banner}"
            logger.info("ncl.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
            self.log_action("result", booking_id=booking_id, status=result.status.value, net_saving=result.net_saving)
            return result

        except Exception as e:
            logger.error("ncl.error", booking_id=booking_id, error=str(e))
            self.log_action("error", booking_id=booking_id, error=str(e))
            # CONFIRMED REAL RISK, fixed 2026-08-13: this used to catch and
            # convert EVERY exception into an ordinary ERROR BookingResult,
            # including a genuinely dead browser/page ("has been closed",
            # "target closed", a renderer crash). BookingService's restart
            # path (_is_dead_browser_error) can only ever trigger on an
            # exception that actually propagates out of check_booking() —
            # swallowing it here made a dead browser permanently
            # unrecoverable for NCL: every remaining booking in the batch
            # would fail identically, one after another, forever, with no
            # self-healing. Re-raise dead-browser-shaped exceptions (the
            # `finally` below still runs first, releasing the edit lock if
            # one was held) so the caller can actually recover; every other
            # exception still becomes a normal ERROR result exactly as
            # before — this does not change behavior for any real portal-
            # level failure this project has ever seen.
            if is_dead_browser_error(e):
                raise
            return make_error_result(booking_id, current_category, CruiseLine.NCL, str(e))

        finally:
            # ALWAYS UNLOCK — even on success, error, and exception
            if in_edit_mode:
                await self._cancel_edit()
                self.log_action("cancel_edit", booking_id=booking_id)
