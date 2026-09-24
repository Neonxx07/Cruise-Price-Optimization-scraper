"""ESPRESSO scraper — Royal Caribbean & Celebrity Cruises.

Ported from adapter_espresso.js. Uses Playwright to automate the
ESPRESSO portal flow: login check → search → read category →
load categories table → click radio → execute API calls → parse results.

SAFETY BOUNDARY — DO NOT CHANGE:
This scraper must never interact with #repriceModalAcceptBtn1 /
#repriceModalAcceptBtn2 ("Continue with New Rate") or any other control
that commits a new rate to a live booking. That is confirmed (directly
from the portal's own markup) to be the actual save action. Everything
this file does — including the direct showRepriceModalCheck fetch call
in _execute_api_calls — stops at reading the Rate Comparison data
(old/new invoice, OBC, offers). The equivalent of clicking "Continue"
on the categories page (#submitToContinue, _eventId=saveCategories) is
simulated read-only via the allocate fetch call; the equivalent of
clicking "Continue with New Rate" must never be added here. That step
is reserved for a human, in the real portal, permanently.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from config.settings import settings
from core.calculator import (
    calculate_espresso,
    find_upgrade_candidates,
    is_paid_in_full,
    make_cancelled_result,
    make_error_result,
    make_no_price_change_result,
    make_paid_in_full_result,
    make_skip_reprice_result,
    make_upgrade_available_result,
    make_wlt_result,
)
from core.models import BookingResult, BookingStatus, CruiseLine
from utils.logging import get_logger
from utils.retry import retry_async

from .base import BaseScraper, _Stopwatch

logger = get_logger(__name__)

# Matches an unrendered Mustache/Angular-style template placeholder,
# e.g. "{{sb.reservation.category.priceCategory}}", so we can tell it
# apart from a real category code like "ZI".
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"^\{\{.*\}\}$")


def _is_template_placeholder(value: str) -> bool:
    return bool(_TEMPLATE_PLACEHOLDER_RE.match(value.strip()))


_RATE_CELL_PRICE_RE = re.compile(r"([\d,]+\.\d{2})")


def _cell_price(cells: list | None) -> float | None:
    """Last money-looking figure in a row's cells for one rate column.

    Read from the RIGHT: a cell can carry a trailing extra like
    "4,268.50175 OBC", where the fare is the first figure and the OBC rides
    behind it in the same cell, so the regex takes whole ``nnn.nn`` groups
    and the price is the last complete one before any suffix.
    """
    for cell in reversed(cells or []):
        matches = _RATE_CELL_PRICE_RE.findall(str(cell))
        if matches:
            return float(matches[0].replace(",", ""))
    return None


def summarize_rate_columns(market_data: dict | None) -> dict:
    """Compare the two rate-program columns row by row.

    REWRITTEN 2026-09-18, replacing a note that said c3 was "captured but
    NOT evaluated". That caveat was written when no live evidence existed.
    It does now: 60 captures in market_data carry both columns, and they
    say two different things depending on the booking type.

      INDIVIDUAL  (41 bookings, 1,602 rows priced in both columns)
        "Best Rate" (c2) and "Best Value" (c3) were IDENTICAL in every
        single row - zero divergence. Reading only c2 lost nothing.

      GROUP       (19 bookings, 730 rows)
        "Group Allocation" (c2) was EMPTY on 677 of 730 rows - 93%. Every
        price sat in "Group Prevailing" (c3). Of the 53 rows priced in
        both, 48 differed and c3 was the cheaper side 16 times, by up to
        $776.00 (booking 3001006, category I2).

    So c3 is not a curiosity - on group bookings it is where the prices
    live, and a scraper reading only c2 was reading a blank column.

    Returns counts and the best c3 undercut. It does NOT pick a column:
    moving between rate programs is a scope change (see core.price_scope),
    and whether a booking may actually move from Allocation to Prevailing
    is a commercial question this scraper has no evidence for. It reports.
    """
    summary = {
        "dual": False, "c2_label": None, "c3_label": None,
        "c2_code": None, "c3_code": None,
        "rows": 0, "c2_priced": 0, "c3_priced": 0,
        "only_c3": 0, "c3_cheaper": 0, "c2_cheaper": 0,
        "best_c3_gain": None,
    }
    if not market_data or not market_data.get("dualRateColumns"):
        return summary
    summary["dual"] = True
    summary["c2_label"] = market_data.get("c2Label")
    summary["c3_label"] = market_data.get("c3Label")

    for row in market_data.get("rows") or []:
        cols = {c.get("column"): c for c in (row.get("columns") or []) if c}
        c2, c3 = cols.get("c2"), cols.get("c3")
        # Fall back to the raw cell lists for captures taken before
        # `columns` existed, so historical rows still summarise.
        p2 = _cell_price(c2["cells"] if c2 else row.get("c2Cells"))
        p3 = _cell_price(c3["cells"] if c3 else row.get("c3Cells"))
        if c2 and c2.get("requestedCode"):
            summary["c2_code"] = c2["requestedCode"]
        if c3 and c3.get("requestedCode"):
            summary["c3_code"] = c3["requestedCode"]

        summary["rows"] += 1
        summary["c2_priced"] += p2 is not None
        summary["c3_priced"] += p3 is not None
        if p2 is None and p3 is not None:
            summary["only_c3"] += 1
        elif p2 is not None and p3 is not None and abs(p2 - p3) > 0.005:
            if p3 < p2:
                summary["c3_cheaper"] += 1
                gain = p2 - p3
                best = summary["best_c3_gain"]
                if best is None or gain > best["gain"]:
                    summary["best_c3_gain"] = {
                        "category": row.get("category"),
                        "c2_price": p2, "c3_price": p3, "gain": round(gain, 2),
                    }
            else:
                summary["c2_cheaper"] += 1
    return summary


def _append_dual_rate_note(note: str, market_data: dict | None) -> str:
    """Extracted as a pure function (2026-08-13, Phase 0 correctness audit)
    so the dual-rate-column visibility fix is directly unit-testable.

    Still a note rather than an automatic column switch - see
    summarize_rate_columns for why - but the note now states what THIS
    booking's second column actually contains instead of a blanket
    "not evaluated" on every dual-column booking alike.
    """
    s = summarize_rate_columns(market_data)
    if not s["dual"]:
        return note

    c3_name = s["c3_label"] or s["c3_code"] or "second rate program"
    c2_name = s["c2_label"] or s["c2_code"] or "the column used here"

    if s["only_c3"] and s["c2_priced"] == 0:
        return note + (
            f" [RATE COLUMNS: '{c2_name}' is EMPTY on this booking — all "
            f"{s['only_c3']} priced categories are quoted under '{c3_name}' "
            f"only. The price used here comes from a column with no "
            f"quotes; check '{c3_name}' by hand]"
        )
    if s["only_c3"]:
        detail = (
            f" [RATE COLUMNS: {s['only_c3']} of {s['rows']} categories are "
            f"priced ONLY under '{c3_name}', not under '{c2_name}'"
        )
    elif s["c3_cheaper"] or s["c2_cheaper"]:
        detail = (
            f" [RATE COLUMNS: '{c3_name}' differs from '{c2_name}' on this "
            f"booking"
        )
    else:
        return note + (
            f" [RATE COLUMNS: '{c3_name}' quotes the same price as "
            f"'{c2_name}' on all {s['c2_priced']} priced categories — no "
            f"second-column opportunity]"
        )

    if s["best_c3_gain"]:
        g = s["best_c3_gain"]
        detail += (
            f"; cheapest undercut is category {g['category']} at "
            f"{g['c3_price']:,.2f} vs {g['c2_price']:,.2f} "
            f"(-{g['gain']:,.2f})"
        )
    return note + detail + ". Verify by hand — switching rate program is not automatic]"


class EspressoScraper(BaseScraper):
    """Scraper for ESPRESSO (Royal Caribbean / Celebrity) portal."""

    cruise_line = CruiseLine.ESPRESSO

    # Auth HOSTS (matched on the hostname) and auth PATH segments (matched
    # on the path). Split deliberately, and never as a bare substring of the
    # whole URL.
    #
    # CONFIRMED FALSE POSITIVE, caught by this file's own test before it
    # ever ran live: a first version listed "sso" as a whole-URL substring,
    # and "sso" appears inside **"espresso"** - so
    # `/espresso/protected/reservations.do` was classed as a login page and
    # EVERY booking would have failed with "Not logged in". Short substrings
    # against a full URL are not safe; host and path are checked separately.
    _AUTH_HOST_PREFIXES = ("auth.", "idp.", "sso.", "login.", "signin.")
    _AUTH_PATH_SEGMENTS = (
        "/login", "/signin", "/sign-in", "/oauth", "/oauth2",
        "/sso/", "/saml", "/federate", "/as/authorization",
    )

    @property
    def credential_service(self) -> str:
        """Where save_login.py stores this account's credential."""
        return settings.espresso_credential_service

    async def auto_login(self) -> str:
        """Fill the ESPRESSO login form from the saved credential.

        NEVER RAISES - returns a status string, matching NclScraper.
        auto_login and msc_commands.auto_login, so a caller can fall back
        to a manual prompt instead of crashing a run. Returns "OK",
        "ALREADY_LOGGED_IN", "NO_CREDENTIALS_SAVED", "NO_LOGIN_FORM",
        "FILLED_AWAITING_MFA", or "ERROR: ...".

        "OK" means fully authenticated. "FILLED_AWAITING_MFA" means the
        credential went in and the human must finish - ESPRESSO requires
        MFA, so a fully unattended login is not possible and this does not
        pretend otherwise.

        Never logs, prints or returns the credential values themselves.
        """
        try:
            import keyring
        except Exception as exc:  # never crash a login on the keyring
            return f"ERROR: keyring unavailable ({exc})"

        username = keyring.get_password(self.credential_service, "username")
        password = keyring.get_password(self.credential_service, "password")
        if not username or not password:
            return "NO_CREDENTIALS_SAVED"

        try:
            # A restored session may already be valid. Re-submitting a
            # login over a live one is pointless and, on this portal,
            # risky - ESPRESSO allows a single active session per account.
            if await self._check_login():
                logger.info("espresso.auto_login", result="ALREADY_LOGGED_IN")
                return "ALREADY_LOGGED_IN"

            # Evidence for the future: no ESPRESSO login page has ever been
            # captured, which is why the selectors below are discovered
            # rather than exact. Dumping it once means the next change can
            # be made against real markup.
            try:
                await self.dump_page_snapshot("_login", "espresso_login_form")
            except Exception:
                pass

            found = await self.page.evaluate("""
                (() => {
                  const vis = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0
                        && s.visibility !== 'hidden' && s.display !== 'none';
                  };
                  const pw = Array.from(
                      document.querySelectorAll('input[type="password"]')).filter(vis)[0];
                  if (!pw) return {ok: false, why: 'no visible password input'};
                  // The username box is the nearest preceding visible
                  // text/email input - that IS the shape of a login form.
                  const scope = pw.form || document;
                  const texts = Array.from(scope.querySelectorAll(
                      'input[type="text"], input[type="email"], input:not([type])'))
                      .filter(vis);
                  const before = texts.filter(el =>
                      pw.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING);
                  const user = before.length ? before[before.length - 1] : texts[0];
                  if (!user) return {ok: false, why: 'no visible username input'};
                  const mark = (el, name) => { el.setAttribute('data-ch-login', name); };
                  mark(user, 'user'); mark(pw, 'pass');
                  const submit = scope.querySelector(
                      'input[type="submit"], button[type="submit"], button:not([type])');
                  if (submit && vis(submit)) submit.setAttribute('data-ch-login', 'submit');
                  return {ok: true, hasSubmit: !!(submit && vis(submit)),
                          userId: user.id || user.name || '(unnamed)',
                          passId: pw.id || pw.name || '(unnamed)'};
                })()
            """)

            if not found or not found.get("ok"):
                logger.warning("espresso.auto_login", result="NO_LOGIN_FORM",
                               why=(found or {}).get("why"), url=self.page.url)
                return "NO_LOGIN_FORM"

            logger.info("espresso.auto_login_form_found",
                        user_field=found.get("userId"),
                        pass_field=found.get("passId"),
                        has_submit=found.get("hasSubmit"))

            # FILL VIA LOCATORS, NOT VIA THE MARKS ABOVE.
            #
            # CONFIRMED BUG, fixed 2026-09-22 from the first log file this
            # project has ever had. Neon: "check login and then when i press
            # start the page refreshes and i need to log in again", still
            # happening after the auto-logout fix. The log showed exactly
            # why, twice in the same second:
            #
            #   14:36:24 espresso.auto_login_form_found
            #            user_field=mantine-d8ke1v2g9 pass_field=mantine-ytoafz0pi
            #   14:36:54 espresso.auto_login_failed
            #            Page.fill: Timeout 30000ms exceeded
            #            waiting for locator("[data-ch-login=\\"user\\"]")
            #
            # The discovery JS above FOUND the fields and stamped them with
            # data-ch-login - and then a SEPARATE page.fill call could not
            # find the stamp. Those ids are the tell: "mantine-d8ke1v2g9" is
            # generated fresh on every render, so this is a React/Mantine
            # form that re-renders between the two calls and throws away the
            # attribute along with the node it was on.
            #
            # So auto-login has never worked: it burned 30 seconds per
            # attempt and always ended in a manual login. Playwright
            # locators re-resolve at action time, which is exactly what a
            # re-rendering form needs - the marks are kept only as the
            # diagnostic they always really were.
            pass_box = self.page.locator("input[type='password']:visible").first
            user_box = self.page.locator(
                "input[type='text']:visible, input[type='email']:visible, "
                "input:not([type]):visible").first

            await user_box.fill(username, timeout=10000)
            await pass_box.fill(password, timeout=10000)
            if found.get("hasSubmit"):
                submit = self.page.locator(
                    "input[type='submit']:visible, button[type='submit']:visible").first
                await submit.click(timeout=10000)
            else:
                await pass_box.press("Enter")

            # Short settle only. Anything longer would be waiting on MFA,
            # which is the human's job - blocking here would look like a
            # hang with no explanation.
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            if await self._check_login():
                logger.info("espresso.auto_login", result="OK")
                return "OK"

            logger.info("espresso.auto_login", result="FILLED_AWAITING_MFA",
                        url=self.page.url)
            return "FILLED_AWAITING_MFA"
        except Exception as exc:
            logger.warning("espresso.auto_login_failed", error=str(exc)[:200])
            return f"ERROR: {exc}"[:200]

    # Keys core.booking_features wants from the booking page. Read IN THE
    # PAGE and returned as a ~200-byte dict.
    _FEATURE_KEYS = ("sailDate", "sailingDate", "shipCode", "shipName",
                     "currency", "stateroomType")

    async def read_feature_fields(self) -> dict:
        """The price-driver fields, without serialising the whole DOM.

        PERFORMANCE FIX, 2026-09-21. Feature capture was added earlier the
        same day by calling `page.content()` and regexing the result - which
        pulls the ENTIRE page over CDP. Real ESPRESSO booking pages average
        422 KB, and the scan already does that twice per booking for the two
        page snapshots, so this made it three times: ~1.27 MB serialised per
        booking, ~630 MB across a 500-booking watchlist, for six short
        strings.

        This runs a regex over the page's own inline scripts INSIDE the
        browser and returns only the matches. Same source, same values,
        about 200 bytes on the wire.

        Never raises: a feature is a nice-to-have, a booking result is not.
        """
        try:
            return await self.page.evaluate(
                r"""(keys) => {
                    // SCRIPTS ONLY, not the whole DOM. These values live in
                    // the page's inline Angular bootstrap, and
                    // document.documentElement.innerHTML SERIALISES THE
                    // ENTIRE DOCUMENT to find them - ~108 KB on a real
                    // booking page, rebuilt once per booking. The script
                    // nodes' textContent is the same source without the
                    // serialisation, and the loop stops as soon as every
                    // key is found.
                    const out = {};
                    const want = keys.slice();
                    const scripts = document.querySelectorAll('script');
                    for (const s of scripts) {
                        const text = s.textContent;
                        if (!text || text.length < 32) continue;
                        for (let i = want.length - 1; i >= 0; i--) {
                            const k = want[i];
                            const m = text.match(
                                new RegExp('"' + k + '"\s*:\s*"([^"]{1,60})"'));
                            if (m) { out[k] = m[1]; want.splice(i, 1); }
                        }
                        if (!want.length) break;
                    }
                    return out;
                }""",
                list(self._FEATURE_KEYS),
            )
        except Exception as exc:
            logger.debug("espresso.feature_fields_failed", error=str(exc)[:200])
            return {}

    # ESPRESSO'S OWN AUTO-LOGOUT, found 2026-09-21 in a live failure capture
    # from Neon's running scan and then confirmed in 50 occurrences across
    # 25 captured pages - it is armed on EVERY page load:
    #
    #     setTimeout(function(){
    #         window.location.href = window.Base.flowExecutionURL
    #                              + "&_eventId=logout"
    #     }, 1830000);
    #
    # 1,830,000 ms = 30.5 minutes. The BROWSER signs itself out. Every
    # navigation re-arms it, so an active scan keeps resetting it - but a
    # session that sits idle does not, which is exactly the reported
    # sequence: "Check login" succeeds, the operator does something else for
    # half an hour, presses Start, and is sent back to a login screen they
    # had already completed. Nothing in this project reset it, because
    # nothing knew it existed.
    ESPRESSO_AUTO_LOGOUT_MS = 1_830_000
    #: Re-touch the portal well inside that window. 20 minutes leaves 10
    #: minutes of headroom for a slow page or a paused scan.
    SESSION_KEEPALIVE_SECONDS = 20 * 60

    async def keep_session_alive(self, force: bool = False) -> bool:
        """Re-arm ESPRESSO's auto-logout timer if the session has gone idle.

        Returns True if a refresh was performed. Cheap: one navigation, and
        only when the page has been sitting longer than
        SESSION_KEEPALIVE_SECONDS. Never raises - a failed keepalive must
        not take down a scan that might still be perfectly fine.
        """
        last = getattr(self, "_last_navigation_at", None)
        idle = time.monotonic() - last if last is not None else None
        if not force and (idle is None or idle < self.SESSION_KEEPALIVE_SECONDS):
            return False
        try:
            logger.info("espresso.session_keepalive",
                        idle_seconds=int(idle) if idle is not None else None,
                        auto_logout_seconds=self.ESPRESSO_AUTO_LOGOUT_MS // 1000)
            await self.navigate(settings.espresso_home_url)
            self._last_navigation_at = time.monotonic()
            return True
        except Exception as exc:
            logger.warning("espresso.session_keepalive_failed",
                           error=str(exc)[:200])
            return False

    async def _adopt_authenticated_tab(self) -> bool:
        """If the live session ended up in ANOTHER TAB, move to it.

        HYPOTHESIS UNDER TEST, added 2026-09-21. Neon: "i press in logg in
        yes it does logg in however when i press on start the pages
        refreshes and then i need to log in again", still happening after
        the SSO-race fix.

        Nothing in this project has ever tracked more than one tab:
        BaseScraper.start does a single `context.new_page()` and there is no
        `context.on("page")` handler anywhere. GoCCL turned out to open its
        booking engine in a second window (target="_blank"), so the pattern
        is real on these portals - and if ESPRESSO's OAuth round-trip ever
        lands the authenticated session in a new tab, `self.page` keeps
        pointing at the ORIGINAL one, which still shows the login form.
        _check_login then reports "not logged in" about a session the human
        can plainly see working, and Start sends them back to log in again.

        NOT YET CONFIRMED against a live ESPRESSO login - it explains every
        symptom, including why it strikes only sometimes, but the proof
        needs a watched session. It is implemented anyway because it can
        only ever help: it is reached solely on the paths that were about to
        return False, and it adopts a tab ONLY if that tab is genuinely on
        ESPRESSO with no login form. Nothing is adopted speculatively.
        """
        try:
            context = self.page.context
        except Exception:
            return False

        for candidate in list(getattr(context, "pages", []) or []):
            if candidate is self.page or candidate.is_closed():
                continue
            try:
                url = (candidate.url or "").lower()
                if "cruisingpower.com" not in url:
                    continue
                from urllib.parse import urlsplit

                parts = urlsplit(url)
                if parts.netloc.startswith(self._AUTH_HOST_PREFIXES) or any(
                    seg in parts.path for seg in self._AUTH_PATH_SEGMENTS
                ):
                    continue
                if await candidate.locator("input[type='password']").count() > 0:
                    continue
            except Exception:
                continue

            logger.warning(
                "login.adopted_other_tab",
                was=(self.page.url or "")[:120], now=(candidate.url or "")[:120],
                msg="the authenticated session was in a different tab",
            )
            self._page = candidate
            return True
        return False

    async def _check_login(self) -> bool:
        """Whether we are really authenticated on the page we are on now.

        CONFIRMED REAL BUG, fixed 2026-08-28. Neon: "i log in twice in one
        time i press on check log in and start i log in again". This used to
        be PURELY a URL test:

            if "login" in url or "signin" in url: return False
            return "cruisingpower.com" in url

        ESPRESSO authenticates through an OAuth SSO hop on
        `auth.cruisingpower.com` (the redirect chain is documented in
        _search_booking: login -> auth.cruisingpower.com -> oauth callback
        -> reservations.do). That host contains NEITHER "login" NOR
        "signin", and it IS on cruisingpower.com - so this returned **True
        while the browser was still showing the login form**.

        The consequence is exactly the double-login: BookingService.
        check_login polls this, saw True within ~10s, reported
        "Login status: OK" before the human had typed anything, and Start
        then drove into the real login wall - so the operator logged in a
        SECOND time. It also meant the 2026-08-04 "same URL twice" guard
        could not help: both polls agreed, on the login page.

        Now three independent checks, cheapest first:
          1. the URL is not an auth/SSO host,
          2. we are on cruisingpower.com at all,
          3. no password field is present - the decisive one, because it is
             true of a login form whatever the URL says. Same signal
             NclScraper.auto_login already uses to detect a rejected login.
        """
        from urllib.parse import urlsplit

        url = (self.page.url or "").lower()
        parts = urlsplit(url)
        host, path = parts.netloc, parts.path
        if host.startswith(self._AUTH_HOST_PREFIXES) or any(
            seg in path for seg in self._AUTH_PATH_SEGMENTS
        ):
            # AN SSO HOP IS NOT A LOGOUT. Fixed 2026-09-18. Neon: "i press
            # in logg in yes it does logg in however when i press on start
            # the pages refreshes and then i need to log in again".
            #
            # ESPRESSO authenticates through an OAuth round-trip -
            # `auth.cruisingpower.com` - and `auth.` is the first entry in
            # _AUTH_HOST_PREFIXES. check_booking navigates to /home and calls
            # this IMMEDIATELY afterwards, so when the sample lands mid-hop
            # the caller is told "Not logged in" and the operator is sent
            # back to a login screen they had just completed.
            #
            # That is a RACE, which is why it struck only sometimes and why
            # logging in again always "fixed" it - the second attempt simply
            # happened to sample after the redirect settled.
            #
            # So give the chain a moment to land before judging. A genuine
            # logout STAYS on the auth page and still returns False; all this
            # costs in that case is a few seconds, against a login loop.
            settled_url = url
            for _ in range(8):
                try:
                    await self.page.wait_for_load_state(
                        "domcontentloaded", timeout=1500)
                except Exception:
                    pass
                settled_url = (self.page.url or "").lower()
                settled = urlsplit(settled_url)
                still_auth = settled.netloc.startswith(self._AUTH_HOST_PREFIXES) or any(
                    seg in settled.path for seg in self._AUTH_PATH_SEGMENTS
                )
                if not still_auth:
                    logger.info(
                        "login.sso_hop_settled", was=url, now=settled_url,
                        msg="transient SSO redirect, not a logout",
                    )
                    url, parts = settled_url, settled
                    host, path = parts.netloc, parts.path
                    break
            else:
                if await self._adopt_authenticated_tab():
                    return True
                logger.warning(
                    "login.required", url=settled_url,
                    msg="still on an auth/SSO page after waiting - please log into ESPRESSO",
                )
                return False
        if "cruisingpower.com" not in url:
            if await self._adopt_authenticated_tab():
                return True
            logger.warning("login.required", url=url, msg="not on ESPRESSO at all")
            return False
        try:
            # A password box on screen means the form is still up even
            # though the URL looks like the app. Bounded and non-fatal: a
            # probe failure must not be read as "logged out" (that would
            # refuse a perfectly good session).
            if await self.page.locator("input[type='password']").count() > 0:
                # A LOGIN FORM MID-HYDRATION IS NOT A LOGOUT. Fixed
                # 2026-09-22 from the live log. Neon: "i still get this
                # page however it is checking bookings normally which is
                # weird i feel it is login in and out".
                #
                # Exactly that, captured at 17:39:31 on booking 299 of 721:
                #
                #   17:39:30 espresso.navigate_home  booking 3001004
                #   17:39:31 login.required "password field present"
                #   17:39:31 Attempt 1/3 failed: Not logged in - retry in 3s
                #   17:39:34 espresso.navigate_home  (retry)
                #   17:39:38 browser.navigate_recovered
                #
                # /home renders a login form for a moment while it
                # bootstraps, this sampled it at that instant, and the retry
                # three seconds later sailed through - so the operator sees
                # the login page flash while the scan carries on.
                #
                # The SSO-host branch above already waits for the page to
                # settle; this branch returned False immediately. Same race,
                # same treatment. A GENUINE logout still keeps its form for
                # the whole window and still returns False - the only cost
                # is a few seconds on a booking that was going to fail
                # anyway, against a needless retry plus two navigations
                # every time it struck.
                for _ in range(6):                      # ~6s
                    await asyncio.sleep(1.0)
                    try:
                        if await self.page.locator(
                                "input[type='password']").count() == 0:
                            logger.info(
                                "login.password_form_settled", url=url,
                                msg="transient login form during page "
                                    "bootstrap, not a logout")
                            return True
                    except Exception:
                        pass
                logger.warning(
                    "login.required", url=url,
                    msg="password field present - login form still showing",
                )
                return False
        except Exception as exc:
            logger.debug("login.password_probe_failed", error=str(exc))
        return True

    # ESPRESSO's reservation search box was rebuilt on Mantine at some
    # point — the old plain `#reservationid` input/`#searchReservationBtn`
    # button no longer exist on the redesigned page (confirmed against a
    # live capture of the current portal). Mantine assigns a fresh
    # autogenerated id per render, so the stable hook is the `data-qa`
    # attribute, not an id. Both selectors are tried together (old first)
    # so this keeps working if either version of the page is ever served.
    _SEARCH_INPUT_SELECTOR = (
        '#reservationid, [data-qa="secure.espresso.input.reservation.search"]'
    )
    _SEARCH_BUTTON_SELECTOR = (
        '#searchReservationBtn, [aria-label="Search by Reservation ID, Name or Date"]'
    )

    async def _search_booking(self, booking_id: str) -> None:
        """Submit a booking ID in the search form."""
        # Login can still be mid-way through ESPRESSO's OAuth SSO redirect
        # chain (login -> auth.cruisingpower.com -> oauth callback ->
        # reservations.do) when we get here — that can take longer than
        # the generic 30s action timeout, so wait for the search box
        # itself with a dedicated, longer timeout before touching it.
        try:
            await self.wait_for(self._SEARCH_INPUT_SELECTOR, timeout=settings.scraper_login_timeout_ms)
        except Exception:
            # CONFIRMED REAL PATTERN, 2026-08-17 run: 9 consecutive bookings
            # (15:39-16:12) all failed here with a generic Playwright
            # timeout, one of them (3000032) showing in its own error log
            # that the page navigated through an OAuth endSession call and
            # landed on /login WHILE this wait was in progress — the
            # session died in the gap between _check_login() passing (just
            # before this call) and the search box actually appearing, so
            # the existing "Not logged in" checks around this call never
            # caught it. Re-checking login here doesn't fix the retry — it
            # already retries via retry_async — but it turns a cryptic raw
            # timeout into a clear, correctly-labeled error so it's obvious
            # from the note/error field alone that this was a session
            # logout, not a slow page or a real portal error.
            if not await self._check_login():
                raise RuntimeError(
                    "Session logged out while searching — please log into ESPRESSO again"
                )
            raise
        await self.page.fill(self._SEARCH_INPUT_SELECTOR, "")
        await self.page.fill(self._SEARCH_INPUT_SELECTOR, booking_id)
        await self.page.click(self._SEARCH_BUTTON_SELECTOR)
        # Some bookings' search-result pages render the sidebar noticeably
        # slower (observed recurring timeouts on real bookings at 15s while
        # every other booking clears well under it) — 25s gives those the
        # room to load without meaningfully slowing down the normal case.
        await self.wait_for("#sideBar, [id*='sideBar']", timeout=25000)

    async def _read_category(self) -> str | None:
        """Read the current price category from the booking page.

        Right after the search results load, this value can briefly be
        the literal unrendered template string (e.g.
        "{{sb.reservation.category.priceCategory}}") before the page's
        client-side templating finishes. Poll until we get a real value
        or the timeout elapses, rather than returning the placeholder.
        """
        read_js = """
            (() => {
                const h = document.getElementById('currentPriceCat');
                if (h?.value?.trim()) return h.value.trim();
                const s = document.querySelector('[class*="priceCategory"] [class*="value"]')
                       || document.querySelector('.priceCategory .value');
                return s?.textContent?.trim() || null;
            })()
        """
        deadline = time.monotonic() + settings.scraper_category_poll_timeout_ms / 1000
        cat: str | None = None
        while True:
            cat = await self.page.evaluate(read_js)
            if cat and not _is_template_placeholder(cat):
                return cat
            if time.monotonic() >= deadline:
                if cat:
                    logger.warning("espresso.category_still_placeholder", value=cat)
                return None
            await asyncio.sleep(0.2)

    async def _check_wlt(self, category: str) -> bool:
        """Check if the current category is waitlisted in the categories table."""
        result = await self.page.evaluate(f"""
            (() => {{
                const tbody = document.querySelector('#catAvailCategoryList tbody')
                           || document.querySelector('[id*="catAvail"] tbody');
                if (!tbody) return false;
                for (const row of tbody.querySelectorAll('tr')) {{
                    const icon = row.querySelector('td.c1 div.categoryIcon span, .categoryIcon span');
                    if (icon && icon.textContent.trim() === '{category}') {{
                        const st = row.querySelector('td.c2.rooms .svCabin .status, .svCabin .status')?.textContent?.trim();
                        return st === 'WLT';
                    }}
                }}
                return false;
            }})()
        """)
        return bool(result)

    async def _check_paid_status(self) -> dict | None:
        """Fallback paid-status check, kept for defense in depth inside the
        short-response branch. Its own selectors were guessed and never
        verified — checked against 590 real captured bookings, the "paid in
        full" text fallback below matched zero of them. _read_payment_status
        (below), run as an early gate right after search, is the reliable
        path now; this only matters if that early gate is ever skipped."""
        result = await self.page.evaluate("""
            (() => {
                const totalEl = document.querySelector('[class*="totalPrice"] .amount, .total-price .amount, #totalPrice');
                const paidEl = document.querySelector('[class*="paymentsReceived"] .amount, .payments-received .amount, #paymentsReceived');
                if (totalEl && paidEl) {
                    const total = parseFloat(totalEl.textContent.replace(/[^0-9.]/g, '')) || 0;
                    const paid = parseFloat(paidEl.textContent.replace(/[^0-9.]/g, '')) || 0;
                    if (total > 0 && paid >= total) return { isPaid: true, totalPrice: total };
                }
                const bodyText = document.body?.innerText || '';
                if (/paid\\s+in\\s+full/i.test(bodyText)) return { isPaid: true, totalPrice: 0 };
                return { isPaid: false };
            })()
        """)
        return result

    # CURRENCY-AGNOSTIC, fixed 2026-09-22. Neon: booking 3001001 "was paid
    # in full and the project did not detect it".
    #
    # That booking is CANADIAN. These patterns required the literal "(USD)",
    # so on a CAD reservation every field came back None - and the earlier
    # note here argued that was CORRECT, because refusing to parse beats
    # parsing a foreign amount as a USD one. The refusal was right; what
    # happened next was not. is_paid_in_full(None, ...) returns False, the
    # scan read that as "not paid in full" rather than "unknown", and
    # reported a $400 optimization on a reservation with 2 cents outstanding
    # (Total Price (CAD) 2,109.00, Payments Received (CAD) 2,108.98).
    #
    # 11 of 120 sampled ESPRESSO booking pages are CAD - roughly 9% of the
    # watchlist had NO payment gate at all. The amount FORMAT is identical
    # across currencies; only the label differs, and the code is captured
    # separately by _CURRENCY_LABEL_RE, so accepting any ISO code loses
    # nothing and closes the hole.
    _PAYMENT_FIELD_PATTERNS = {
        "total_price": re.compile(r"Total Price \([A-Z]{3}\):\s*(-?[\d,]+\.?\d*)", re.IGNORECASE),
        "deposit": re.compile(r"Deposit \([A-Z]{3}\):\s*(-?[\d,]+\.?\d*)", re.IGNORECASE),
        "payments_received": re.compile(r"Payments Received \([A-Z]{3}\):\s*(-?[\d,]+\.?\d*)", re.IGNORECASE),
        "final_payment_due": re.compile(r"Final Payment Due \([A-Z]{3}\):\s*(-?[\d,]+\.?\d*)", re.IGNORECASE),
    }

    # The outstanding amount also appears under its OWN label, separate from
    # the "Final Payment Due (XXX):" line, which on the current layout is
    # followed by a DATE rather than a figure:
    #
    #   Final Payment Due (CAD):  Due: 10JUL2026
    #   Final Payment:            0.02
    #
    # Used as a fallback so the gate survives either layout.
    _FINAL_PAYMENT_AMOUNT_RE = re.compile(
        r"Final Payment:\s*(-?[\d,]+\.?\d*)", re.IGNORECASE)

    # ADDED 2026-08-13 (Phase 0 correctness audit): the amount patterns
    # above are deliberately UNCHANGED (still require the literal "(USD)"
    # label) — this only adds detection of whatever currency code the page
    # actually shows, without touching how any amount is parsed. Confirmed
    # real risk this closes: if ESPRESSO/Celebrity ever renders "(CAD)" (or
    # any non-USD code) instead, the amount patterns above already
    # correctly fail to match (returning None, not a wrong USD-shaped
    # number) — but nothing previously recorded WHY, or distinguished that
    # from "this field just wasn't on the page." This makes that distinction
    # visible via the `currency` key below, generalized to any 3-letter
    # code rather than assuming USD.
    _CURRENCY_LABEL_RE = re.compile(
        r"(?:Total Price|Deposit|Payments Received|Final Payment Due)\s*\(([A-Z]{3})\)",
        re.IGNORECASE,
    )

    async def is_cancelled(self) -> bool:
        """Is this reservation CANCELLED?

        VIP RULE, added 2026-09-22 at Neon's explicit instruction: "IF THE
        SCANNER OR SCRIPT CATCHES THIS IT MEANS THAT THE BOOKING IS CANCELED
        AND IT IS VERY MADNATORY TO REPORT IT AS IT SOMETHING VERY
        CRITICAL".

        ESPRESSO marks it with sb.reservation.status == 'CX' and renders
        "N/A" wherever a price would go:

            <span ng-show="'CX' == sb.reservation.status">{{labels.NA}}</span>

        WHY THIS CANNOT BE A TEXT OR MARKUP MATCH. That span is in EVERY
        booking page - it is an Angular template, present whether or not the
        booking is cancelled, and Angular simply hides it when the status is
        something else. The page also carries the opposite guard
        ("'CX' != sb.reservation.status") on each price link. So the only
        honest signal is the RUNTIME one: is such a span actually VISIBLE.

        WHY IT MATTERS SO MUCH. A cancelled booking's payment panel still
        reads Total Price 0.00 and Final Payment Due 0.00 - and
        is_paid_in_full(0.00, 0.00) is True. Booking 3001005 was therefore
        reported as "Fully paid - repricing unavailable" on 15, 16, 21 and
        22 September. 87 stored results across 27 distinct bookings carry
        that same zero-total signature.

        Returns False when it cannot tell. A false CANCELLED would hide a
        live booking from the watchlist, so this errs toward saying nothing
        - the payment-readability guard downstream still refuses to invent a
        saving from an unreadable panel.
        """
        try:
            return bool(await self.page.evaluate(
                r"""() => {
                    const nodes = document.querySelectorAll('[ng-show]');
                    for (const el of nodes) {
                        const cond = el.getAttribute('ng-show') || '';
                        // the POSITIVE guard only: "'CX' == ...status".
                        // The negative one ("'CX' != ...") is on every
                        // price link of a perfectly healthy booking.
                        if (!/'CX'\s*==\s*sb\.reservation\.status/.test(cond)) continue;
                        // Angular hides via ng-hide; offsetParent covers
                        // display:none on the node or any ancestor.
                        if (el.offsetParent !== null &&
                            !el.classList.contains('ng-hide')) return true;
                    }
                    return false;
                }"""))
        except Exception as exc:
            logger.debug("espresso.cancel_probe_failed", error=str(exc)[:200])
            return False

    async def _read_payment_status(self) -> dict:
        """Reads Total Price / Deposit / Payments Received / Final Payment
        Due directly from the Reservation Summary page's plain body text.
        Confirmed against 590 real bookings: whenever Total Price is
        present, Final Payment Due is too — and it's already reconciled
        for taxes/credits/adjustments, so it's used directly by
        is_paid_in_full() rather than re-derived from Total minus Received.

        Also returns "currency": the 3-letter code actually shown next to
        these labels (e.g. "USD"), or None if no such label was found at
        all. Never assume "USD" when this is None — see BookingResult.currency."""
        body_text = await self.page.inner_text("body")
        values: dict[str, float | None] = {}
        for key, pattern in self._PAYMENT_FIELD_PATTERNS.items():
            m = pattern.search(body_text)
            values[key] = float(m.group(1).replace(",", "")) if m else None
        # Fallback for the layout where "Final Payment Due (XXX):" carries a
        # date and the figure sits under its own "Final Payment:" label.
        if values.get("final_payment_due") is None:
            m = self._FINAL_PAYMENT_AMOUNT_RE.search(body_text)
            if m:
                values["final_payment_due"] = float(m.group(1).replace(",", ""))
                values["final_payment_due_source"] = "final_payment_label"

        currency_match = self._CURRENCY_LABEL_RE.search(body_text)
        values["currency"] = currency_match.group(1).upper() if currency_match else None
        # Whether the payment panel was readable AT ALL. The caller must be
        # able to tell "this booking owes nothing" from "we could not see
        # what it owes" - conflating them is what produced the false $400.
        values["payment_state_readable"] = any(
            values.get(k) is not None
            for k in ("total_price", "payments_received", "final_payment_due"))
        return values

    async def _click_categories(self) -> None:
        """Click the Categories link to load the category table."""
        await self.page.evaluate("""
            (() => {
                const a = Array.from(document.querySelectorAll('a')).find(
                    el => el.textContent.trim() === 'Categories'
                ) || document.querySelector('#sideBar a[href*="catAvail"]')
                  || document.querySelector('a[href*="categor"]');
                if (a) a.click();
            })()
        """)
        await self.wait_for("#catAvailCategoryList, [id*='catAvail']", timeout=12000)

    async def _capture_category_table(self, current_category: str | None = None) -> dict:
        """Capture the loaded ESPRESSO category table state without mutating the page.

        Some bookings (confirmed both on group bookings and at least one
        individual booking, 2026-08-09) render TWO side-by-side rate-program
        columns per row via a `columnSelection` radio pair — e.g. "Group
        Allocation - Best Rate" vs "Group Prevailing - Best Rate", or
        "Individual - Best Rate" vs "Individual - Best Value". Only the
        left (`c2`, always pre-checked) column has ever been read by this
        scraper or fed into any decision logic — the right (`c3`) column's
        price has never been captured at all. Neon's observation, from
        working the real portal: the right-hand column sometimes shows a
        genuinely lower price than the left. Both columns' cells already
        exist in the same page load (no extra click/request needed), so
        this now records both, purely as additional captured data — it
        does NOT feed either column into any pricing decision yet. That is
        deliberately deferred until there's a real evidence base of when/how
        often the two columns actually diverge (same lesson as the
        free-upgrade-detection incident: verify before deciding, never
        guess which column is "right")."""
        result = await self.page.evaluate(f"""
            (() => {{
                const tbody = document.querySelector('#catAvailCategoryList tbody')
                           || document.querySelector('[id*="catAvail"] tbody');
                const rows = [];
                if (tbody) {{
                    for (const row of tbody.querySelectorAll('tr')) {{
                        const category = row.querySelector('td.c1 div.categoryIcon span, .categoryIcon span')?.textContent?.trim() || null;
                        const status = row.querySelector('td.c2.rooms .svCabin .status, .svCabin .status')?.textContent?.trim() || '';
                        const radio = row.querySelector('input[name="rbCategorySelection"][data-columnindex="0"]')
                                   || row.querySelector('input[type="radio"]');
                        const c2Cells = Array.from(row.querySelectorAll('td.c2:not(.clearCell)')).map(td => td.textContent.trim());
                        const c3Cells = Array.from(row.querySelectorAll('td.c3:not(.clearCell)')).map(td => td.textContent.trim());
                        // BOTH rate-program radios, not just the left one.
                        // Verified 2026-09-18 against the saved categories
                        // pages: every row carries TWO rbCategorySelection
                        // radios, data-columnindex="0" (c2) and "1" (c3),
                        // each with its own data-id/value and a
                        // data-requestedcode naming the rate program -
                        // INDIVIDUAL-BESTRATE / INDIVIDUAL-BVL /
                        // GROUP_ALLOCATION-BESTRATE / GROUP_PREVAILING-BESTRATE.
                        // The program therefore never has to be inferred from
                        // the header label; the row states it.
                        const allRadios = Array.from(row.querySelectorAll('input[name="rbCategorySelection"]'));
                        const describe = (idx, cells) => {{
                            const el = allRadios.find(
                                r => r.getAttribute('data-columnindex') === String(idx)) || null;
                            if (!el && !(cells || []).some(t => t)) return null;
                            return {{
                                columnIndex: idx,
                                column: idx === 0 ? 'c2' : 'c3',
                                dataId: el?.getAttribute('data-id') || null,
                                radioValue: el?.value || null,
                                radioChecked: Boolean(el?.checked),
                                requestedCode: el?.getAttribute('data-requestedcode') || null,
                                selectable: Boolean(el) && !el.disabled,
                                cells: cells || [],
                            }};
                        }};
                        const columns = [describe(0, c2Cells), describe(1, c3Cells)].filter(Boolean);
                        rows.push({{
                            category,
                            status,
                            radioValue: radio?.value || null,
                            radioChecked: Boolean(radio?.checked),
                            promo: row.querySelector('.promo, .currentPromo')?.textContent?.trim() || '',
                            rowText: row.innerText.trim(),
                            c2Cells,
                            c3Cells,
                            columns,
                        }});
                    }}
                }}
                const token = location.href.match(/execution=(e\\d+s\\d+)/)?.[1] || null;
                const selectionJSON = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]')?.value || '';
                const c2Header = document.querySelector('th.columnSelection.c2 label');
                const c3Header = document.querySelector('th.columnSelection.c3 label');
                const c2Radio = document.querySelector('input[name="columnSelection"].c2');
                return {{
                    ok: true,
                    currentCategory: {json.dumps(current_category)},
                    executionToken: token,
                    selectionJSON,
                    rows,
                    dualRateColumns: Boolean(c2Header || c3Header),
                    c2Label: c2Header?.textContent?.trim() || null,
                    c3Label: c3Header?.textContent?.trim() || null,
                    activeColumn: (c2Radio ? (c2Radio.checked ? 'c2' : 'c3') : null),
                }};
            }})()
        """)
        return result

    async def _read_page_data(self, category: str | None) -> dict:
        """Read execution token, selection JSON, and radio value from page."""
        cat_js = f"'{category}'" if category else "null"
        result = await self.page.evaluate(f"""
            (async () => {{
                const m = location.href.match(/execution=(e\\d+s\\d+)/);
                const token = m ? m[1] : null;
                let radio = '1';
                const sel0 = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]');
                const beforeJson = sel0?.value || '';
                const cat = {cat_js};
                const tbody = document.querySelector('#catAvailCategoryList tbody')
                           || document.querySelector('[id*="catAvail"] tbody');
                if (tbody && cat) {{
                    for (const row of tbody.querySelectorAll('tr')) {{
                        const icon = row.querySelector('td.c1 div.categoryIcon span, .categoryIcon span');
                        if (icon && icon.textContent.trim() === cat) {{
                            const r = row.querySelector('input[name="rbCategorySelection"][data-columnindex="0"]')
                                   || row.querySelector('input[type="radio"]');
                            if (r) {{
                                radio = r.value;
                                r.checked = true;
                                r.click();
                                r.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                break;
                            }}
                        }}
                    }}
                }}
                const deadline = Date.now() + 2000;
                while (Date.now() < deadline) {{
                    await new Promise(res => setTimeout(res, 100));
                    const cur = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]')?.value || '';
                    if (cur && cur !== beforeJson && cur !== '[]') break;
                }}
                await new Promise(res => setTimeout(res, 150));
                const selFinal = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]');
                return {{ executionToken: token, selectionJSON: selFinal?.value || '[]', radioValue: radio }};
            }})()
        """)
        return result

    async def _read_top_prices(self) -> dict:
        """Read the two 'viewPriceQuoteLink' price figures shown at the top
        of the categories page — the reservation's current price
        (sb.summary.price.price) and the price for whichever category
        radio is currently selected (sb.summary.price.allocationPrice).
        These are rendered directly from the page's own Angular state, so
        they're a reliable "did the price actually change" check — cheaper
        and more trustworthy than the showRepriceModalCheck fetch, which
        returns a short, non-JSON body specifically when there's no real
        change (previously misdiagnosed downstream as an expired token).
        """
        result = await self.page.evaluate("""
            (() => {
                const parseAmt = (el) => {
                    if (!el) return null;
                    const n = parseFloat((el.textContent || '').replace(/[^0-9.\\-]/g, ''));
                    return isNaN(n) ? null : n;
                };
                const links = Array.from(document.querySelectorAll('a.viewPriceQuoteLink.fit'));
                const currentEl = links.find(el => (el.getAttribute('ng-show') || '').includes('sb.summary.price.price'));
                const allocEl = links.find(el => (el.getAttribute('ng-show') || '').includes('sb.summary.price.allocationPrice'));
                return { currentPrice: parseAmt(currentEl), allocationPrice: parseAmt(allocEl) };
            })()
        """)
        return result

    async def _confirm_candidate_total(self, category: str) -> float | None:
        """Get ESPRESSO's own REAL confirmed total for `category` — never
        an estimate. Runs the exact same allocate()+repriceModalCheck()
        sequence already trusted for OPTIMIZATION/TRAP (same safety
        boundary: never touches the actual "Continue with New Rate" commit
        button), then reads the confirmed total back via _read_top_prices()
        rather than parsing repriceModalCheck's JSON body.

        Confirmed live 2026-08-01 (booking 3000002, candidate J3):
        repriceModalCheck can return {"key": "skipRepriceModal"} — meaning
        this booking can't COMMIT a reprice into this category — while
        sb.summary.price.allocationPrice still updates to the real
        confirmed total regardless. Those are different questions ("can I
        confirm this" vs "what would it cost"), and only the second one is
        needed to know whether a category is a genuine upgrade. Verified
        against all 6 bookings from the original false-positive incident:
        every one now correctly shows a real cost INCREASE ($401-$5,459),
        matching manual verification that none of them were real upgrades.

        Returns None if no price was ever rendered (never treat missing
        data as a signal of savings).
        """
        page_data = await self._read_page_data(category)
        if not page_data.get("executionToken"):
            return None
        await self._execute_api_calls(
            page_data["executionToken"], page_data["selectionJSON"], page_data["radioValue"],
        )
        # Let Angular finish recomputing/rendering the allocation price
        # after the real allocate() response comes back.
        await asyncio.sleep(1.0)
        top_prices = await self._read_top_prices()
        return top_prices.get("allocationPrice")

    async def _execute_api_calls(self, token: str, selection_json: str, radio: str) -> dict:
        """Execute the allocate + reprice API calls inside the page context."""
        result = await self.page.evaluate(f"""
            (async () => {{
                try {{
                    const b1 = new URLSearchParams({{
                        'columnSelection': 'on',
                        'rbCategorySelection': '{radio}',
                        '_eventId': 'saveCategories',
                        'categorySingleViewFormModel.selectionJSON': {json.dumps(selection_json)}
                    }}).toString();
                    const r1 = await fetch(
                        '/espresso/protected/reservations.do?execution={token}&_eventId=allocate&ajaxSource=true',
                        {{ method:'POST', headers:{{ 'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With':'XMLHttpRequest' }}, body:b1, credentials:'include' }}
                    );
                    if (!r1.ok) return {{ ok:false, error:'Allocate HTTP ' + r1.status }};
                    await new Promise(res => setTimeout(res, 300));
                    const r2 = await fetch(
                        '/espresso/protected/repriceModalController.do/showRepriceModalCheck?execution={token}',
                        {{ method:'POST', headers:{{ 'Content-Type':'application/x-www-form-urlencoded', 'X-Requested-With':'XMLHttpRequest', 'Accept':'application/json' }}, body:'execution={token}', credentials:'include' }}
                    );
                    if (!r2.ok) return {{ ok:false, error:'Reprice HTTP ' + r2.status }};
                    const text = await r2.text();
                    try {{ return {{ ok:true, data: JSON.parse(text), dataLength: text.length }}; }}
                    catch(e) {{ return {{ ok:false, error:'Not JSON: ' + text.substring(0, 200) }}; }}
                }} catch(e) {{ return {{ ok:false, error:e.message }}; }}
            }})()
        """)
        return result

    async def release_booking(self, booking_id: str = "") -> bool:
        """Exit a retrieved reservation so ESPRESSO frees its 15-minute lock.

        Neon 2026-09-16: "esspresso has a lock mechanisem we need to exit
        every booking after checking or else the booking stay locked for 15
        mins". Nothing released it before - the scan just moved on - so
        every booking checked was locked for a quarter of an hour, blocking
        both a human and a re-scan.

        Returns True if the release was issued, False otherwise. NEVER
        raises: a failed release must not turn a good result into an error.
        Worst case the lock expires on its own, which is today's behaviour.

        NOT a cancellation. The portal's own wording for this control is
        "any changes you made since you retrieved this reservation will not
        be saved" - it discards and unlocks. "Cancel Reservation" is a
        different, destructive control on the same page and is never
        touched here; this scan never intends to save anything anyway.
        """
        try:
            # 1) The real control, as captured: the Exit link, then the
            # confirm button inside the dialog it opens.
            link = self.page.locator("#ignoreReservationLink")
            if await link.count() > 0:
                await link.first.click()

                # THE CONFIRM STEP IS NOT OPTIONAL. Neon 2026-09-18: "the
                # script is not pressong on exit i have to press it
                # manually", and supplied the real element:
                #
                #   <input type="button" id="acceptIgnoreReservation"
                #          class="submit" value="Exit">
                #
                # The previous version waited 4s for it and, on timeout,
                # silently continued past it on the theory that "some flows
                # exit without the confirm step" - then logged
                # booking_released and returned True regardless. So a dialog
                # that took longer than 4s to draw was left sitting open,
                # the reservation stayed locked, and the log claimed success.
                # A human then had to press Exit by hand. That "pass" was
                # doing the opposite of what it promised.
                #
                # Now: wait properly, click, and VERIFY the dialog actually
                # went away - and if it is still there, say so instead of
                # reporting a release that did not happen.
                # THE PAGE SHIPS TWO OF THESE. Confirmed 2026-09-18 across
                # every captured categories page: the whole Exit dialog is
                # duplicated, so there are TWO elements with
                # id="acceptIgnoreReservation", each inside its own
                #   <div id="ignoreReservationPopup" style="display: none;">
                # Duplicate ids are invalid HTML, but the portal does it
                # anyway, and clicking the Exit link reveals only one of them.
                #
                # `.first` therefore had a 50/50 chance of targeting a node
                # that stays hidden forever - the wait times out, the click
                # lands on nothing, and the dialog is left open for a human
                # to dismiss. Selecting on VISIBILITY rather than document
                # order is what actually makes this deterministic.
                confirm = self.page.locator("#acceptIgnoreReservation").locator(
                    "visible=true")
                clicked = False
                try:
                    await confirm.first.wait_for(state="visible", timeout=15000)
                    await confirm.first.click(timeout=5000)
                    clicked = True
                except Exception:
                    # A real overlay can swallow a synthetic click; the
                    # element's own handler still works when invoked
                    # directly. Only attempted when the control is actually
                    # present - never as a way to force a missing one.
                    try:
                        if await confirm.count() > 0:
                            await confirm.first.evaluate("el => el.click()")
                            clicked = True
                    except Exception:
                        pass

                await self.page.wait_for_load_state("domcontentloaded", timeout=10000)

                # Still showing the confirm dialog? Then nothing was released.
                try:
                    still_open = (await confirm.count() > 0
                                  and await confirm.first.is_visible())
                except Exception:
                    still_open = False
                if still_open:
                    logger.warning(
                        "espresso.booking_release_unconfirmed",
                        booking_id=booking_id,
                        reason="Exit dialog still open after clicking "
                               "#acceptIgnoreReservation",
                    )
                    self.log_action("release_booking_unconfirmed",
                                    booking_id=booking_id)
                    return False

                logger.info("espresso.booking_released", booking_id=booking_id,
                            via="ignoreReservationLink", confirmed=clicked)
                self.log_action("release_booking", booking_id=booking_id,
                                via="ignoreReservationLink", confirmed=clicked)
                return True

            # 2) Fallback: the navigation the page's own handler performs.
            # Group bookings use a DIFFERENT event - taken from that same
            # handler, not assumed.
            released = await self.page.evaluate("""
                (() => {
                  if (!window.Base || !window.Base.flowExecutionURL) return null;
                  const group = (typeof isGroupBooking !== 'undefined') && isGroupBooking;
                  return window.Base.flowExecutionURL + '&_eventId='
                       + (group ? 'linkToCompleteIgnoreReservationGb'
                                : 'linkToIgnoreReservation');
                })()
            """)
            if released:
                await self.navigate(released)
                logger.info("espresso.booking_released", booking_id=booking_id,
                            via="flowExecutionURL")
                self.log_action("release_booking", booking_id=booking_id,
                                via="flowExecutionURL")
                return True

            logger.info("espresso.booking_release_skipped", booking_id=booking_id,
                        reason="no exit control on this page")
            return False
        except Exception as exc:
            # Deliberately swallowed - see the docstring.
            logger.warning("espresso.booking_release_failed",
                           booking_id=booking_id, error=str(exc)[:200])
            return False

    async def check_booking(self, booking_id: str, capture_market_data: bool = False) -> BookingResult:
        """
        Full ESPRESSO booking check flow.

        Steps: navigate → login check → search → read category →
        load categories → WLT check → execute API → calculate result.
        """
        price_category: str | None = None
        # ADDED 2026-08-13 (Phase 0 correctness audit): the real currency
        # code detected from the Reservation Summary page text (see
        # _read_payment_status/_CURRENCY_LABEL_RE), or None if no
        # recognizable label was found. Applied to whatever BookingResult
        # is ultimately returned, further down, regardless of which branch
        # produced it — never assumed "USD" when this stays None.
        detected_currency: str | None = None
        self.last_market_data = None

        async def _attempt():
            nonlocal price_category, detected_currency

            # Go through the portal home page first, the same path a human
            # takes right after login — deep-linking straight to
            # reservations.do skips whatever session/flow initialization
            # /home does, and appears to be what was causing the forced
            # logouts and desynced execution tokens seen during testing.
            # Per-stage timings for this booking (see _Stopwatch). ESPRESSO
            # performs TWO full navigations and TWO login checks here, which
            # is the most likely reason a long scan feels heavy - but that
            # was a hypothesis until this measured it. The /home hop is NOT
            # removed on a guess: its own comment records that skipping it
            # previously caused forced logouts and desynced execution
            # tokens, so it stays until the numbers say otherwise.
            watch = _Stopwatch()
            self._last_stage_timings = watch
            # Cleared per booking: a stale value would silently attribute
            # the PREVIOUS booking's ship and sail date to this one.
            self.last_feature_fields = None

            logger.info("espresso.navigate_home", booking_id=booking_id)
            self.log_action("navigate", booking_id=booking_id, url=settings.espresso_home_url)
            await self.navigate(settings.espresso_home_url)
            watch.mark("navigate_home")
            if not await self._check_login():
                raise RuntimeError("Not logged in — please log into ESPRESSO first")
            watch.mark("check_login_1")

            logger.info("espresso.navigate", booking_id=booking_id)
            self.log_action("navigate", booking_id=booking_id, url=settings.espresso_base_url)
            await self.navigate(settings.espresso_base_url)
            watch.mark("navigate_reservations")
            if not await self._check_login():
                raise RuntimeError("Not logged in — please log into ESPRESSO first")
            watch.mark("check_login_2")

            # Early-warning structure check (once per session, not once
            # per booking — see check_structure_drift's docstring). Never
            # blocks the actual search below even if this itself fails to
            # find the element — that failure is informative on its own.
            await self.check_structure_drift("espresso_search_input", self._SEARCH_INPUT_SELECTOR)
            await self.check_structure_drift("espresso_search_button", self._SEARCH_BUTTON_SELECTOR)

            logger.info("espresso.search", booking_id=booking_id)
            self.log_action("search_booking", booking_id=booking_id)
            try:
                await self._search_booking(booking_id)
            except Exception as e:
                await self.dump_failure_snapshot(booking_id, "search_booking_failed", str(e))
                raise
            await self.dump_page_snapshot(booking_id, "after_search")
            watch.mark("search")

            price_category = await self._read_category()
            logger.info("espresso.category", booking_id=booking_id, category=price_category)
            self.log_action("read_category", booking_id=booking_id, category=price_category)

            # Paid-in-full early gate — runs before Categories is even
            # clicked, using the Reservation Summary page's own "Final
            # Payment Due (USD)" figure (see is_paid_in_full/
            # _read_payment_status). Catches this regardless of what the
            # reprice-modal API would have returned — a real booking
            # (3000040) was previously slipping through as a false
            # "$77 OPTIMIZATION" because its API response was a normal
            # length, so the old reactive-only paid-status check never ran.
            watch.mark("read_category")
            # CANCELLED FIRST - before every pricing gate. A cancelled
            # booking's payment panel reads Total Price 0.00 / Final Payment
            # Due 0.00, which is_paid_in_full() accepts, so checking it
            # later would keep filing cancellations as "fully paid".
            if await self.is_cancelled():
                logger.warning("espresso.booking_cancelled", booking_id=booking_id,
                               msg="reservation status CX - reported as CANCELLED")
                self.log_action("booking_cancelled", booking_id=booking_id)
                return {"_cancelled": True}

            payment_status = await self._read_payment_status()
            detected_currency = payment_status.get("currency")

            # PRICE-DRIVER FIELDS, CAPTURED HERE - ON THE BOOKING PAGE.
            #
            # CONFIRMED BUG, fixed 2026-09-22. These were read from the
            # batch loop AFTER check_booking returned - but check_booking
            # ends with release_booking(), which clicks Exit and navigates
            # away. So the read happened on whatever page came next, and
            # sail_date came back None even though the booking page plainly
            # carried it: bookings 3001000 (02JAN2028), 3000073 (27MAR2027)
            # and 3001002 (05JUN2028) all logged sail_date=None while their
            # captured pages contained those exact values.
            #
            # 11% of today's ESPRESSO rows lost their sail date that way -
            # silently, because a missing feature is a NULL column, not an
            # error. Reading it here, while the booking is still on screen,
            # is the whole fix.
            try:
                watch.mark("payment_status")
                self.last_feature_fields = await self.read_feature_fields()
            except Exception:
                self.last_feature_fields = {}
            watch.mark("feature_fields")
            self.log_action("payment_status", booking_id=booking_id, **payment_status)
            if is_paid_in_full(
                payment_status.get("final_payment_due"),
                payment_status.get("total_price") or 0.0,
            ):
                return {"_paidInFull": True, "oldTotal": payment_status.get("total_price") or 0.0}

            # FAIL SAFE WHEN THE PAYMENT PANEL CANNOT BE READ AT ALL.
            #
            # THE BUG THIS CLOSES, 2026-09-22. Booking 3001001 is Canadian.
            # The field patterns required a literal "(USD)", so every figure
            # came back None - and is_paid_in_full(None, ...) returns False
            # by design, because it refuses to guess. The scan then read that
            # False as "not paid in full" rather than "we do not know", ran
            # the whole comparison, and reported a $400 OPTIMIZATION on a
            # reservation with TWO CENTS outstanding (Total Price (CAD)
            # 2,109.00 against Payments Received (CAD) 2,108.98).
            #
            # The patterns are currency-agnostic now, so this specific
            # booking parses. This guard is for the NEXT cause - a relabelled
            # panel, a new layout, a currency written some other way. An
            # unreadable payment state is not evidence of an outstanding
            # balance, and a saving on a settled booking is worse than no
            # saving at all.
            if not payment_status.get("payment_state_readable"):
                logger.warning(
                    "espresso.payment_state_unreadable",
                    booking_id=booking_id,
                    currency=detected_currency,
                    msg="no payment figures could be read - refusing to "
                        "report a saving for a booking whose balance is unknown",
                )
                self.log_action("payment_state_unreadable", booking_id=booking_id,
                                currency=detected_currency)
                return {"_paymentUnreadable": True}

            # Click categories and load the table
            self.log_action("click_categories", booking_id=booking_id)
            await self._click_categories()
            await self.dump_page_snapshot(booking_id, "categories_table")

            # Always captured now (not just when capture_market_data is on)
            # — find_free_upgrade() needs these rows for every booking, not
            # just ones being logged for analysis. capture_market_data still
            # controls whether this gets persisted to the market_data table.
            self.last_market_data = await self._capture_category_table(price_category)
            if capture_market_data:
                logger.info(
                    "espresso.market_data_captured",
                    booking_id=booking_id,
                    current_category=price_category,
                    rows_count=len(self.last_market_data.get("rows", [])),
                )

            # WLT check (AFTER categories table is loaded — fix from v6.3)
            if price_category and await self._check_wlt(price_category):
                return {"_wlt": True}

            page_data = await self._read_page_data(price_category)
            if not page_data.get("executionToken"):
                raise RuntimeError("No execution token in URL")

            logger.info("espresso.api_calls", booking_id=booking_id, token=page_data["executionToken"])
            self.log_action("execute_api_calls", booking_id=booking_id, token=page_data["executionToken"])
            api_result = await self._execute_api_calls(
                page_data["executionToken"],
                page_data["selectionJSON"],
                page_data["radioValue"],
            )

            # ESPRESSO's own API deliberately returns this shape — it is
            # not an error or an expired token, it's a clean "this booking
            # has a restriction that blocks repricing" (confirmed against
            # the portal's own "Booking Restriction: Changing price pgm
            # is not allowed" message). No point retrying — it won't change.
            if api_result.get("ok") and (api_result.get("data") or {}).get("key") == "skipRepriceModal":
                logger.info("espresso.skip_reprice", booking_id=booking_id)
                return {"_skipRepriceModal": True}

            short_response = (api_result.get("dataLength") or 0) < 300
            if not api_result.get("ok") or short_response:
                if short_response:
                    paid = await self._check_paid_status()
                    if paid and paid.get("isPaid"):
                        return {"_paidInFull": True, "oldTotal": paid.get("totalPrice", 0)}

                    # A short/non-JSON response from showRepriceModalCheck is
                    # what the portal returns when there's genuinely nothing
                    # to compare. Confirm that against the page's own
                    # displayed price — read only now, after the real
                    # allocate/reprice calls have actually run, never as a
                    # pre-emptive skip (an earlier version of this check ran
                    # before the API call and produced false "no change"
                    # verdicts, because the page hadn't updated yet at that
                    # point — masking real optimizations).
                    top_prices = await self._read_top_prices()
                    current_price = top_prices.get("currentPrice")
                    allocation_price = top_prices.get("allocationPrice")
                    if (
                        current_price is not None
                        and allocation_price is not None
                        and abs(current_price - allocation_price) < 0.01
                    ):
                        logger.info("espresso.no_price_change", booking_id=booking_id, price=current_price)
                        self.log_action("price_check", booking_id=booking_id, result="no_change", price=current_price)
                        return {"_noPriceChange": True, "price": current_price}

                if not api_result.get("ok"):
                    logger.warning("espresso.api_call_failed", booking_id=booking_id, error=api_result.get("error"))
                    raise RuntimeError(api_result.get("error", "API failed"))

                # Log + persist the actual short body instead of just its
                # length — "token expired" was a guess; this shows what
                # the portal is really saying so we can classify it properly.
                body = api_result.get("data")
                logger.warning(
                    "espresso.short_response",
                    booking_id=booking_id,
                    data_length=api_result.get("dataLength"),
                    body=body,
                )
                self.dump_raw(booking_id, {"short_response": True, "dataLength": api_result.get("dataLength"), "body": body})
                raise RuntimeError(f"API returned only {api_result.get('dataLength')} chars — body: {body}")

            self.dump_raw(booking_id, api_result.get("data"))
            return api_result

        api_result = await retry_async(
            _attempt,
            attempts=settings.scraper_retry_attempts,
            delay_s=settings.scraper_retry_delay_ms / 1000,
            label=f"ESPRESSO {booking_id}",
        )

        # Handle sentinel results
        if api_result.get("_wlt"):
            return make_wlt_result(booking_id, price_category, CruiseLine.ESPRESSO)
        if api_result.get("_paidInFull"):
            return make_paid_in_full_result(
                booking_id, price_category, CruiseLine.ESPRESSO, api_result.get("oldTotal", 0),
            )
        if api_result.get("_cancelled"):
            return make_cancelled_result(booking_id, price_category, CruiseLine.ESPRESSO)
        if api_result.get("_paymentUnreadable"):
            # See the guard in the flow above. An unreadable payment panel
            # means the balance is UNKNOWN, and an unknown balance must not
            # become a reported saving - booking 3001001 was settled to
            # within two cents and was reported as a $400 optimization
            # because a CAD label did not match a USD-only pattern.
            return make_error_result(
                booking_id, price_category, CruiseLine.ESPRESSO,
                "payment panel unreadable — cannot confirm whether this "
                "booking is paid in full, so no saving is reported. Check "
                "the reservation by hand.",
            )
        if api_result.get("_skipRepriceModal"):
            return make_skip_reprice_result(booking_id, price_category, CruiseLine.ESPRESSO)
        if api_result.get("_noPriceChange"):
            return make_no_price_change_result(
                booking_id, price_category, CruiseLine.ESPRESSO, api_result.get("price", 0),
            )

        result = calculate_espresso(api_result["data"], booking_id, price_category)

        # RE-ENABLED 2026-08-01 with a real fix — see core/calculator.py's
        # "ESPRESSO Free-Upgrade Detection" module docstring for the full
        # incident history (design #3 was confirmed wrong against real
        # data: 6 false positives from comparing a per-person table price
        # to a whole-booking total). find_upgrade_candidates() is a free,
        # unit-safe pre-filter only — it decides nothing. Every candidate
        # it returns still gets a real allocate()+repriceModalCheck() round
        # trip via _confirm_candidate_total(), and only a REAL confirmed
        # total that's actually <= old_total is ever surfaced. Verified
        # live against all 6 original false positives: every one now
        # correctly comes back as costing more, not less.
        if result.status == BookingStatus.NO_SAVING and self.last_market_data:
            candidates = find_upgrade_candidates(price_category, self.last_market_data.get("rows", []))
            best: dict | None = None
            for candidate in candidates:
                confirmed_total = await self._confirm_candidate_total(candidate["category"])
                logger.info(
                    "espresso.upgrade_candidate_confirmed",
                    booking_id=booking_id, category=candidate["category"], confirmed_total=confirmed_total,
                )
                if confirmed_total is None or confirmed_total > result.old_total:
                    continue
                if best is None or confirmed_total < best["price"]:
                    best = {"category": candidate["category"], "room_type": candidate["room_type"], "price": confirmed_total}
            if best is not None:
                result = make_upgrade_available_result(
                    booking_id, price_category, CruiseLine.ESPRESSO, result.old_total, best,
                )

        # Applied uniformly to every branch above (sentinel WLT/paid-in-full/
        # skip-reprice/no-price-change results, the main calculated result,
        # and the upgrade-available override) — "UNKNOWN" unless the page
        # text actually showed a recognizable currency label this run.
        result.currency = detected_currency or "UNKNOWN"

        # ADDED 2026-08-13 (Phase 0 correctness audit): _capture_category_table
        # already captures the c3 (alternate rate-program) column's raw cell
        # text for bookings that render one, but — per that function's own
        # docstring — never feeds it into any pricing decision, since no live
        # capture exists confirming what c3's cell format actually means or
        # how it compares to c2's. Deliberately NOT adding speculative price
        # comparison/selection logic here — there's no evidence to build it
        # on, and guessing wrong here is exactly the failure class the
        # free-upgrade-detection incident history in core/calculator.py
        # warns against. Instead: make the KNOWN ambiguity visible rather
        # than silently absent — a human reviewing this result now knows a
        # second, unevaluated rate-program column exists on this booking.
        result.note = _append_dual_rate_note(result.note, self.last_market_data)

        # RELEASE THE LOCK before moving on. Placed here so it runs for
        # every branch above - WLT, paid-in-full, skip-reprice,
        # no-price-change, the calculated result and the upgrade override
        # alike. A booking left retrieved stays locked for 15 minutes.
        _release_watch = getattr(self, "_last_stage_timings", None)
        await self.release_booking(booking_id)
        if _release_watch is not None:
            _release_watch.mark("release_booking")

        watch = getattr(self, "_last_stage_timings", None)
        if watch is not None:
            watch.mark("finish")
            logger.info("espresso.timings", booking_id=booking_id,
                        total_ms=watch.total_ms, **watch.stages)
        logger.info("espresso.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
        self.log_action("result", booking_id=booking_id, status=result.status.value, net_saving=result.net_saving)
        return result
