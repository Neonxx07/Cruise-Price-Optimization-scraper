"""GoCCL Navigator scraper — Carnival Cruise Line booking repricing check.

Mirrors the pattern used in scraper/espresso.py and scraper/ncl.py:
- read-only discovery of current price + all available category/offer prices
- NEVER clicks the final purchase/confirm button
- stops at the review screen and returns the comparison for a human to decide

Workflow (confirmed against live GoCCL Navigator DOM, July 2026):
  1. Search booking -> 2. Read current price/category -> 3. Modify Booking ->
  4. Change Offer/Rate -> 5. Read all offer codes x stateroom-type prices ->
  6. For the matching stateroom type, read every category row's price ->
  7. (optional) select cheapest category, click "Keep Same Stateroom" ->
  8. Read review screen GROSS AMOUNT -> STOP. Return comparison. No confirm click.

check_booking() only performs steps 1-5 (safe, read-only discovery) and
returns a BookingResult with an UNCONFIRMED candidate — see
core/calculator.py:calculate_goccl for why GoCCL can't produce a confirmed
net saving the way ESPRESSO/NCL do. preview_fare_code() (steps 6-8) is the
separate, human-triggered path that confirms one candidate at a time.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from glom import Coalesce, glom
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config.settings import settings
from core.calculator import (
    apply_goccl_review,
    calculate_goccl,
    make_cancelled_result,
    make_error_result,
)
from core.goccl_review import ReviewInvoice, parse_review_changes
from core.models import BookingResult, BookingStatus, CruiseLine
from core.vifp_guest import CarnivalGuestInfo, guest_info_from_fields, normalise_guest_number
from utils.logging import get_logger, track_background_task

from .base import BaseScraper, is_dead_browser_error

logger = get_logger(__name__)

# CONFIRMED REAL GAP, 2026-08-25: no live GoCCL capture has ever confirmed a
# real per-booking guest-count/occupancy field anywhere in window.initialData
# (see read_current_price_and_selection's own docstring below and
# core/calculator.py's calculate_goccl guests_count_verified handling) --
# every real call site today falls back to a global default. These are
# PLAUSIBLE, UNCONFIRMED candidate paths based on common booking-JSON naming
# conventions -- NOT a claim that any of them is real. Coalesce tries each in
# order and returns whichever resolves first, or its default (None) if none
# do -- this NEVER fabricates a guest count; it only surfaces one IF a real
# field happens to exist under one of these names, for a human to confirm
# against the dump this now captures (see check_booking's dump_raw call).
# If a real path is ever confirmed against live data, promote it to
# read_current_price_and_selection's actual return value and set
# guests_count_verified=True at the real call site -- do not do that here.
_GUEST_COUNT_CANDIDATE_PATHS = [
    Coalesce("guestCount", default=None),
    Coalesce("numGuests", default=None),
    Coalesce("occupancy.total", default=None),
    Coalesce("occupancy.guestCount", default=None),
    Coalesce("stateroom.occupancy.total", default=None),
    Coalesce(("passengers", len), default=None),
    Coalesce(("guests", len), default=None),
    Coalesce("booking.guestCount", default=None),
]
_GUEST_COUNT_CANDIDATE_NAMES = [
    "guestCount", "numGuests", "occupancy.total", "occupancy.guestCount",
    "stateroom.occupancy.total", "passengers (count)", "guests (count)",
    "booking.guestCount",
]


def _probe_guest_count_candidates(data: dict) -> dict:
    """Safely try every candidate path above against a real
    window.initialData capture, returning only the ones that actually
    resolved to something — never raises, never guesses which (if any)
    is the real field. Purely diagnostic: NOT wired into guests_count or
    guests_count_verified anywhere. Look at this dict in a real capture
    (data/raw_responses.jsonl or a raw dump) to find out whether any of
    these paths is real before ever trusting one."""
    found = {}
    for name, spec in zip(_GUEST_COUNT_CANDIDATE_NAMES, _GUEST_COUNT_CANDIDATE_PATHS):
        try:
            value = glom(data, spec)
        except Exception:
            # Broad on purpose: this is a best-effort diagnostic probe over
            # an unconfirmed JSON shape, not a decision input -- a
            # malformed candidate path (e.g. `len()` on a None where a
            # list was hoped for) must never crash the scrape, and
            # Coalesce's own default only covers its own "not found" case,
            # not every possible shape mismatch further down a path.
            continue
        if value is not None:
            found[name] = value
    return found

# Guests count matters: the category table shows "Average Per Person," but
# the review screen's GROSS AMOUNT is the full per-cabin total (guests x
# per-person + taxes/fees). Always compare like-for-like — either multiply
# per-person by guest count, or read the review screen's gross amount
# directly after a tentative selection.


@dataclass
class OfferCodeOption:
    offer_name: str
    offer_code: str
    stateroom_type: str  # e.g. "BALCONY"
    price_per_person: float


@dataclass
class FareCodeCandidateResult:
    """Result of previewing ONE fare/offer code at the booking's existing,
    unchanged category. This is the real comparison axis for GoCCL: category
    stays fixed, fare code varies."""
    offer_code: str
    category_code: str  # same as the original booking's category, unchanged
    new_price_gross: float
    new_obc_total: float
    new_obc_lines: dict = field(default_factory=dict)  # e.g. {"POBC": 25.0, "NOBC": 25.0}


class BookingCancelled(Exception):
    """GoCCL says the booking is cancelled (span#ctl00_DefaultContent_
    lblErrorMessage "This booking is cancelled" - DEMO46, every run since
    2026-10-13). A fact about the booking, reported as CANCELLED."""


class GoCCLScraper(BaseScraper):
    """
    Playwright-based scraper for GoCCL Navigator (Carnival travel-agent portal).
    Read-only: discovers pricing, never submits a booking change.
    """

    cruise_line = CruiseLine.GOCCL

    def __init__(self, guests_count: int | None = None):
        super().__init__()
        # CONFIRMED REAL BUG, fixed 2026-08-13: guests_count used to always
        # silently be settings.goccl_default_guests_count for every booking
        # (every real caller — main.py, services/booking_service.py —
        # constructs this class with no argument), with no distinction
        # between "this booking really has 2 guests" and "we never checked."
        # No live GoCCL capture has ever confirmed a real per-booking guest-
        # count field in window.initialData (see read_current_price_and_selection
        # below — it reads gross/rate/category/stateroom_type only), so this
        # does NOT guess a new selector for it. guests_count_verified tracks
        # whether the caller actually supplied a real, confirmed count
        # (only ever True if a FUTURE caller passes one) — it stays False
        # for every current call site, and core.calculator.calculate_goccl
        # uses it to make that assumption visible in the result's note
        # instead of presenting an unverified guest count as settled fact.
        self.guests_count = guests_count if guests_count is not None else settings.goccl_default_guests_count
        self.guests_count_verified = guests_count is not None
        # The signed-in portal tab, once the booking engine has been opened
        # in its popup and self.page has moved there. Kept because closing
        # it would tear the popup down with it.
        self.portal_page = None
        # Advisories the booking engine's own API returned for the booking
        # currently being checked. See _attach_advisory_listener.
        self.last_advisories: list[dict] = []
        #: Raw window.initialData from the most recent booking read.
        self.last_initial_data: dict | None = None
        self._advisory_listener_attached = False

    # ── login and the booking-engine popup ──────────────────────────────
    #
    # THE STRUCTURAL BUG, found 2026-09-18 from a Playwright CRX recording
    # of a real session. GoCCL does not serve the booking engine in the tab
    # you sign into. The real flow is:
    #
    #     goccl.com/accounts/login  ->  fill username + password, Sign In
    #       ->  click "Individual/Groups Staterooms"
    #       ->  the booking engine opens in a POPUP WINDOW
    #       ->  every later step happens in that popup
    #
    # This scraper deep-linked straight to goccl_search_url and had no
    # popup handling anywhere - no expect_popup, no expect_page, nothing.
    # That works only while a session is already authenticated in the
    # current tab; otherwise the deep link lands on a login page and every
    # wait_for_selector("#booked-root") sits there until it times out. It
    # is the most likely cause of the 8 Locator.wait_for / Page.inner_text
    # timeouts in the recorded Carnival run.

    SEARCH_INPUT = "#ctl00_DefaultContent_txtBookingNumber"

    # ── THE POINT OF NO RETURN ──────────────────────────────────────────
    #
    # Enumerated 2026-09-18 from every button on all five wizard pages of a
    # real session. The wizard is read-only right up until ONE control:
    #
    #   /rate      Continue              data-comp="goto-next-page"
    #              Discard Changes       data-comp="cancel"
    #   /category  Select $1,391         (picks a category, still reversible)
    #              Keep Same Stateroom   data-comp="continue-to-review"
    #              Continue              data-comp="goto-next-page"
    #   /review    Discard All Changes   (reversible)
    #              Confirm Changes       <-- COMMITS THE REFARE
    #
    # "Confirm Changes" carries NO data-comp attribute, so it can only be
    # matched on its text - which is exactly why it is listed explicitly
    # here rather than left to a convention. This mirrors the standing rule
    # in scraper/espresso.py about #repriceModalAcceptBtn1/2.
    #
    # This scraper discovers prices. It must never commit one.
    FORBIDDEN_CLICK_TEXT = (
        "confirm changes",
        "confirm my changes",
        "complete booking",
        "submit payment",
        "make a payment",
        "pay with funship pay",
        # A cancelled booking's page offers REACTIVATE BOOKING next to the
        # "This booking is cancelled" message (DEMO46, 2026-10-16).
        "reactivate",
    )

    def _assert_safe_click(self, label: str | None) -> None:
        """Raise rather than click anything that commits a change.

        A guard, not a convention: a future edit that wires up a new click
        path gets stopped here instead of silently repricing a live
        booking. Deliberately matches on substring and case-insensitively,
        because the failure this prevents is irreversible and a near-miss
        on capitalisation is not a reason to let it through.
        """
        text = (label or "").strip().lower()
        if not text:
            return
        for banned in self.FORBIDDEN_CLICK_TEXT:
            if banned in text:
                raise RuntimeError(
                    f"REFUSING to click {label!r}: this control commits a "
                    f"change to a live booking. This scraper is read-only - "
                    f"it discovers prices and never applies them."
                )

    def _switch_to_page(self, page) -> None:
        """Make `page` the one this scraper operates on.

        Writes to _page, NOT to `page`: BaseScraper exposes `page` as a
        read-only property that raises if the scraper has not started, so
        `self.page = popup` fails with
            AttributeError: property 'page' of 'GoCCLScraper' object has no setter
        Caught 2026-09-18 by a test before this ever ran against the portal -
        all three popup-following paths would have crashed on the first
        window GoCCL opened.
        """
        self._page = page

    async def auto_login(self) -> str:
        """Sign in with the credential saved by save_login.py (option 5).

        NEVER RAISES - returns a status string, matching NclScraper.auto_login
        and msc_commands.auto_login so a caller can fall back to a manual
        login rather than crashing a whole run. Returns "OK",
        "ALREADY_LOGGED_IN", "NO_CREDENTIALS_SAVED", "INVALID_CREDENTIALS",
        "TIMEOUT_WAITING_FOR_LOGIN" or "ERROR: ...".

        Selectors are matched by ROLE and visible label, exactly as the CRX
        recording captured them ("Username:" textbox, "Sign In" button),
        because no saved copy of the login page exists to read ids off. The
        password field is located relative to the form rather than guessed
        at by id - see _fill_password below.
        """
        import keyring

        try:
            service = settings.goccl_credential_service
            username = keyring.get_password(service, "username")
            password = keyring.get_password(service, "password")
            if not username or not password:
                return "NO_CREDENTIALS_SAVED"

            await self.navigate(settings.goccl_login_url, wait_until="domcontentloaded")

            # WAIT FOR THE SPA BEFORE DECIDING ANYTHING. The login page is a
            # client-rendered app: the served HTML is a 13KB shell with an
            # empty <title>, an empty <div id="accounts-root"> and a single
            # hidden input, and the form is drawn later by
            # /accounts/gocclr-accounts/assets/index-*.js. Confirmed
            # 2026-09-18 by capturing the real page.
            #
            # Deciding before that lands is the ESPRESSO SSO race again: an
            # unhydrated page has no form AND no booking-tool link, so a
            # count() on either returns 0 and the code concludes something
            # false about a page that simply had not drawn yet.
            state = await self._wait_for_accounts_spa()
            if state == "LOGGED_IN":
                return "ALREADY_LOGGED_IN"
            if state == "NOTHING_RENDERED":
                return "TIMEOUT_WAITING_FOR_LOGIN"

            # Clear the consent overlay BEFORE touching the form, or it
            # intercepts the submit click.
            await self._dismiss_cookie_banner()

            user_box = self._username_box()
            await user_box.wait_for(state="visible", timeout=15000)
            await user_box.fill(username)

            if not await self._fill_password(password):
                return "ERROR: no password field found on the GoCCL login page"

            submit = self.page.locator(self.LOGIN_SUBMIT)
            if await submit.count():
                await submit.first.click()
            else:
                await self.page.get_by_role(
                    "button", name=re.compile(r"Sign\s*In", re.I)).click()

            # Success is the booking-tool link appearing; failure is an error
            # message or simply still being on the login form.
            # Login lands on /accounts/post-login and then redirects to the
            # dashboard at goccl.com/, where the booking-tool link lives.
            # Waited on as ATTACHED rather than VISIBLE: it sits in a
            # dashboard panel that need not be scrolled into view for the
            # session to be good.
            try:
                await self._booking_tool_link().first.wait_for(state="attached", timeout=25000)
            except Exception:
                body = ""
                try:
                    body = (await self.page.inner_text("body"))[:4000]
                except Exception:
                    pass
                if re.search(r"invalid|incorrect|not recogni|try again|locked", body, re.I):
                    return "INVALID_CREDENTIALS"
                return "TIMEOUT_WAITING_FOR_LOGIN"

            self.log_action("auto_login", status="OK")
            return "OK"
        except Exception as exc:  # never raise out of a login helper
            return f"ERROR: {str(exc)[:200]}"

    # Real ids, CONFIRMED 2026-09-18 by capturing the rendered login page in
    # a live watched session. The element CLASSES are hashed
    # styled-components ("sc-FRoXv hUGVKw") and must never be selected on,
    # but the ids are clean and semantic, and each is tied to its own
    # <label for=...> ("Username:" / "Password:") exactly as the CRX
    # recording showed.
    LOGIN_USERNAME = "#username"
    LOGIN_PASSWORD = "#password"
    LOGIN_SUBMIT = "#loginButton"
    # OneTrust consent banner. It renders over the page and will happily
    # swallow the Sign In click - a silent, intermittent login failure that
    # would look exactly like bad credentials.
    COOKIE_ACCEPT = "#onetrust-accept-btn-handler"

    async def _dismiss_cookie_banner(self) -> None:
        try:
            btn = self.page.locator(self.COOKIE_ACCEPT)
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                self.log_action("cookie_banner_dismissed")
                await asyncio.sleep(0.4)
        except Exception:
            pass  # never let consent handling break a login

    def _username_box(self):
        """The username field: confirmed id first, accessible name as backup."""
        box = self.page.locator(self.LOGIN_USERNAME)
        return box if box else self.page.get_by_role(
            "textbox", name=re.compile("Username", re.I))

    async def _wait_for_accounts_spa(self, timeout: float = 30.0) -> str:
        """Wait until the accounts SPA has actually drawn something.

        Returns "LOGGED_IN" (the booking-tool link is on screen),
        "LOGIN_FORM" (a password field is on screen) or "NOTHING_RENDERED".
        Polls rather than racing a single wait_for, because either outcome
        is legitimate and whichever arrives first is the answer.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self._booking_tool_link().count():
                    return "LOGGED_IN"
                if await self.page.locator("input[type='password']:visible").count():
                    return "LOGIN_FORM"
            except Exception:
                pass  # mid-navigation; try again
            await asyncio.sleep(0.5)
        return "NOTHING_RENDERED"

    async def _fill_password(self, password: str) -> bool:
        """Fill the password box without guessing at an id.

        The recording never captured the password field (CRX deliberately
        omits it), so this finds it structurally: the one visible
        input[type=password] on the page. If there isn't exactly one, it
        reports failure rather than typing a password into whatever it
        found first.
        """
        box = self.page.locator(self.LOGIN_PASSWORD)
        if await box.count() == 1:
            await box.first.fill(password)
            return True
        boxes = self.page.locator("input[type='password']:visible")
        if await boxes.count() != 1:
            return False
        await boxes.first.fill(password)
        return True

    def _booking_tool_link(self):
        """The dashboard link that opens the booking engine.

        Selected by HREF. The dashboard carries three different links to
        the same booking-engine URL, and their text disagrees:

            "Individual/Group Staterooms"    data-gtm-event=individual_group_staterooms
            "Individual/Groups Staterooms"   data-gtm-event=group_staterooms
            "The Fun Shops"                  data-gtm-event=the_fun_shops

        Both spellings of Group(s) are genuine - they are simply different
        links - so a text match picks whichever happens to be found first
        and silently depends on which one that is. The href is the same on
        all three and is the stable identity.

        Also why this is not a get_by_role(name=regex) lookup: Playwright
        serialises a compiled regex into its own /.../flags selector
        syntax, and the "/" inside "Individual/Group..." closes the literal
        early -
            InvalidSelectorError: unexpected symbol "G" at position 22
        """
        return self.page.locator(settings.goccl_booking_tool_selector)

    async def open_booking_tool(self) -> None:
        """Click through to the booking engine, following it into its popup.

        Sets self.page to the popup so every later step in this class
        operates on the window that actually holds the booking tool. The
        original portal tab is kept on self.portal_page - it is still a
        live, signed-in session and closing it would end the popup's too.
        """
        if await self.page.locator(self.SEARCH_INPUT).count():
            return  # already in the booking engine

        # The dashboard link is a PLAIN href opened with target="_blank":
        #   <a href="/BookingEngine/BookingSearch/SearchForReservations.aspx"
        #      target="_blank" data-gtm-event="individual_group_staterooms">
        # So the new window is the browser honouring target="_blank", not a
        # scripted popup, and the same URL can simply be navigated to in
        # place. That is strictly more robust: no window to lose track of,
        # no popup timeout to tune.
        #
        # This also explains the original failure precisely. The old code's
        # deep link to this URL was the RIGHT url - it just had no login
        # step, so an expired session served a login page instead, and
        # wait_for_selector("#booked-root") sat there until it timed out.
        # Being signed in is the part that was missing.
        self.log_action("open_booking_tool", url=settings.goccl_search_url)
        await self.navigate(settings.goccl_search_url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_selector(self.SEARCH_INPUT, timeout=20000)
            return
        except PlaywrightTimeoutError:
            pass

        # Fallback: drive it the way a human does, and follow the window
        # that opens. Kept because a portal that starts requiring the click
        # (or bounces the deep link) must not take the scraper down with it.
        self.log_action("open_booking_tool_fallback_click")
        await self.navigate(settings.goccl_dashboard_url, wait_until="domcontentloaded")
        link = self._booking_tool_link().first
        await link.wait_for(state="attached", timeout=20000)
        popup = await self._click_following_popup(link)
        if popup is not None:
            self.portal_page = self.page
            self._switch_to_page(popup)
        await self.page.wait_for_load_state("domcontentloaded")
        await self.page.wait_for_selector(self.SEARCH_INPUT, timeout=30000)

    async def _click_following_popup(self, locator, timeout: int = 15000):
        """Click something that may open a popup, and return the popup.

        Returns None when the click navigated in place instead. GoCCL opens
        the booking engine in a popup, and some of its later controls open
        further windows, so this is used wherever a click could go either
        way - the alternative is what this scraper did before: keep waiting
        on the old page for content that moved to a window it never knew
        about.
        """
        try:
            async with self.page.expect_popup(timeout=timeout) as popup_info:
                await locator.click()
            popup = await popup_info.value
            await popup.wait_for_load_state("domcontentloaded")
            self.log_action("followed_popup", url=popup.url)
            return popup
        except PlaywrightTimeoutError:
            return None

    # ── RULE ADV-001: the portal refuses, and says why ──────────────────
    #
    # Found 2026-09-18 by capturing API RESPONSE BODIES during the forensic
    # sweep. Four bookings (DEMO09, DEMO20, DEMO19, DEMO16) reached /guest
    # and then sat there until open_change_offer_rate timed out after 30
    # seconds with "waiting for section.rate__container". Nothing in the DOM
    # explained it - the Change Offer/Rate control is byte-identical to a
    # working booking's, and is not disabled.
    #
    # The answer was never in the page. availability/rate returned:
    #
    #     HTTP 409  {"code":"999999","message":"Advisories were received",
    #                "details":[{"code":"5108",
    #                            "message":"The VIFP number is incorrect."}]}
    #
    # while a working booking returns HTTP 200 with rates=list[5]. The same
    # advisory appeared on all four, and reproduced exactly on a re-run, so
    # it is deterministic and booking-specific: Carnival will not quote a
    # booking whose loyalty (VIFP) number is invalid.
    #
    # That is a DATA-QUALITY problem on the booking, fixable by a human -
    # but only if it is reported. Reporting it as a 30-second timeout hid a
    # fixable defect behind an infrastructure-shaped error.
    _ADVISORY_PATH_RE = re.compile(r"/app/bookingengine/api/", re.I)

    def _attach_advisory_listener(self) -> None:
        """Record advisories the booking engine's API returns.

        Attached once per page. Never raises and never blocks the response:
        the body is read on a background task, because a response handler
        that awaits inside Playwright's event loop can deadlock the page.
        """
        if self._advisory_listener_attached:
            return

        def on_response(resp):
            try:
                if not self._ADVISORY_PATH_RE.search(resp.url):
                    return

                async def read():
                    try:
                        body = await resp.json()
                    except Exception:
                        return
                    if not isinstance(body, dict):
                        return

                    # TWO CHANNELS, both real.
                    #
                    # 1) The 409 error envelope:
                    #      {"code":"999999","message":"Advisories were
                    #       received","details":[{"code":"5108", ...}]}
                    # 2) advisorySummary on a SUCCESS response, found
                    #    2026-09-18 on a 200 from availability/stateroom:
                    #      {"advisories": [], "hasError": false,
                    #       "hasInformational": false}
                    #
                    # The second is the portal's own first-class advisory
                    # field and can carry a problem while the HTTP status
                    # is 200 - so keying only on status >= 400 would miss
                    # it. "200 is not automatically valid" (brief s.30).
                    candidates: list[tuple[dict, bool]] = []
                    if resp.status >= 400:
                        # The 409 envelope is always a refusal.
                        candidates += [(d, True)
                                       for d in ((body.get("details") or []) or [body])]

                    summary = body.get("advisorySummary")
                    if isinstance(summary, dict):
                        # hasError SEPARATES A REFUSAL FROM A REMARK, and
                        # getting this wrong is not academic. A first cut
                        # treated every advisorySummary entry as a problem;
                        # replayed over the evidence it condemned ~20
                        # perfectly good bookings on the strength of
                        #   {"code": 1241,
                        #    "description": "Option extension is not
                        #                    applicable to deposited bookings.",
                        #    hasError: false, hasInformational: true}
                        # which is a note about an unrelated feature, on a
                        # 200, on bookings that quoted 3-11 rates fine.
                        #
                        # Note also the field name: this channel says
                        # DESCRIPTION where the 409 envelope says MESSAGE.
                        # Reading only "message" produced advisories whose
                        # text was the literal string "None".
                        blocking = bool(summary.get("hasError"))
                        candidates += [(a, blocking)
                                       for a in (summary.get("advisories") or [])]

                    for detail, blocking in candidates:
                        if not isinstance(detail, dict):
                            continue
                        code = str(detail.get("code") or "").strip()
                        msg = str(detail.get("message")
                                  or detail.get("description") or "").strip()
                        if not msg:
                            continue
                        entry = {"code": code, "message": msg,
                                 "blocking": blocking,
                                 "status": resp.status,
                                 "path": resp.url.split("?")[0]}
                        if entry not in self.last_advisories:
                            self.last_advisories.append(entry)
                            log = logger.warning if blocking else logger.info
                            log("goccl.advisory", code=code, message=msg,
                                blocking=blocking, status=resp.status)

                # RETAINED, 2026-09-30. This was the last unreferenced
                # capture listener in the project. track_background_task's
                # own docstring already named "MSC/ESPRESSO network-response
                # capture listeners" as the sites that needed this; GoCCL's
                # was missed. A task with no strong reference is a real
                # candidate for "Task was destroyed but it is pending!", and
                # what it drops here is CAPTURED DATA - the advisory codes
                # behind goccl.advisory - which this project never discards.
                # self._background_tasks comes from BaseScraper, which also
                # already drains it on close, so this gets that for free.
                track_background_task(self._background_tasks,
                                      asyncio.ensure_future(read()))
            except Exception as exc:
                # Still never raises - this is a Playwright event callback,
                # and throwing here would take the response handler down
                # with it. But it no longer vanishes: a silent `pass` in a
                # CAPTURE path meant advisory data could stop arriving and
                # nothing would ever say so, which is the same failure the
                # crash-reporting work existed to end (an error that is
                # never recorded cannot be alerted on). debug, not warning -
                # it fires per response and must not flood the log.
                logger.debug("goccl.advisory_listener_error",
                             error=str(exc)[:200])

        self.page.on("response", on_response)
        self._advisory_listener_attached = True

    def advisory_summary(self) -> str:
        """BLOCKING advisories only, as one human-readable string, or "".

        Informational advisories are recorded (they are useful evidence and
        may matter later) but deliberately excluded here: this string is
        what turns into "GoCCL will not quote this booking", and a remark
        like 1241 "Option extension is not applicable to deposited
        bookings." must never produce that verdict.
        """
        real = [a for a in self.last_advisories
                if a.get("blocking", True)            # 409 details default to blocking
                and a["code"] not in ("999999",)]     # the envelope, not a reason
        return "; ".join(f"{a['code']}: {a['message']}" for a in real)

    def informational_advisories(self) -> list[dict]:
        """Non-blocking advisories, kept for evidence and schema-drift watch."""
        return [a for a in self.last_advisories if not a.get("blocking", True)]

    async def _open_booking_directly(self, booking_number: str) -> bool:
        """Open the booking at /app/bookingengine/<number>. True only when
        the booking page arrived AND it is this booking.

        Neon 2026-10-12: "the link does not change ... i just keep changing
        after /" - his DEMO18 recording starts at exactly that address, and
        the captured DEMO08 page there carries the same #booked-root the
        search path waits for. The booking number is then checked against
        the page's own initialData, so a redirect can never hand us another
        booking's data. False (e.g. a signed-out session landing on a login
        page) falls back to the search page.
        """
        ref = booking_number.strip().upper()
        url = f"{settings.goccl_booking_engine_url}/{ref}"
        self.log_action("open_booking_direct", booking_id=ref, url=url)
        try:
            await self.navigate(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - the search path still works
            if is_dead_browser_error(exc):
                raise
            return False
        if not await self._search_landed(timeout=20000):
            return False
        shown = await self.page.evaluate(
            "() => (window.initialData && window.initialData.bookingNumber) || null")
        if str(shown or "").strip().upper() != ref:
            logger.warning("goccl.direct_link_other_booking", requested=ref, shown=shown)
            return False
        return True

    async def search_booking(self, booking_number: str) -> None:
        # Advisories are per booking - clear them, then listen. Attached
        # to the page the wizard actually runs on (the booking engine, not
        # the portal).
        self.last_advisories = []

        # FASTEST, AND PROVEN: the direct booking address (2026-10-12).
        if await self._open_booking_directly(booking_number):
            self._attach_advisory_listener()
            self.log_action("search_submitted", booking_id=booking_number, via="direct_link")
            await self.dump_page_snapshot(booking_number, "after_search")
            return

        # Otherwise the real flow through the search page (popup and all).
        await self.open_booking_tool()
        self._attach_advisory_listener()

        booking_input = self.page.locator(self.SEARCH_INPUT)
        await booking_input.click()
        await booking_input.fill(booking_number)
        self.log_action("search_booking", booking_id=booking_number)
        # "Search" is a LINK, not a button - confirmed by the CRX recording
        # (getByRole('link', {name: 'Search'}).first()). The old code clicked
        # a #btnSearchBookingNumber button id that the recording never shows,
        # then silently fell back to pressing Enter, so a wrong selector here
        # never surfaced as an error.
        #
        # EACH ATTEMPT IS CHECKED BEFORE THE NEXT ONE RUNS. The previous
        # version chained the three blindly on exception, which produced a
        # real failure on booking DEMO09 (2026-09-18 batch, 1 of 4 bookings):
        #
        #   Locator.press: Timeout 30000ms exceeded.
        #   waiting for locator("#ctl00_DefaultContent_txtBookingNumber")
        #
        # The click had ALREADY submitted the search - the booking page
        # loaded fine, its initialData was readable afterwards - but the
        # click call still raised, because the navigation it triggered
        # detached the element mid-click. The chain then pressed Enter on an
        # input that no longer existed and spent 30s timing out on it.
        #
        # So a raised exception is not evidence of failure here; the only
        # evidence that counts is whether the booking page arrived.
        submitted_by = None
        attempts = (
            ("search_link", lambda: self.page.get_by_role(
                "link", name=re.compile(r"^\s*Search\s*$", re.I)).first.click(timeout=5000)),
            ("search_button", lambda: self.page.click(
                "#ctl00_DefaultContent_btnSearchBookingNumber", timeout=3000)),
            ("enter_key", lambda: booking_input.press("Enter", timeout=5000)),
        )
        for name, action in attempts:
            if await self._search_landed(timeout=1000):
                submitted_by = submitted_by or "already_landed"
                break
            try:
                await action()
            except Exception:
                pass          # the check below decides, not the exception
            if await self._search_landed(timeout=15000):
                submitted_by = name
                break
            refusal = await self._search_refusal()
            if refusal:
                break

        if submitted_by is None:
            refusal = await self._search_refusal()
            if refusal and refusal[0] == "cancelled":
                raise BookingCancelled(refusal[1])
            if refusal and refusal[0] == "maintenance":
                raise RuntimeError(
                    f"GoCCL showed its maintenance page for {booking_number} "
                    f"('{refusal[1][:90]}') - a portal problem with this booking, "
                    f"not a scraper fault; check it by hand")
            raise RuntimeError(
                f"GoCCL search for {booking_number} never reached the booking "
                f"page (#booked-root) after trying the Search link, the "
                f"Search button and the Enter key"
            )
        self.log_action("search_submitted", booking_id=booking_number, via=submitted_by)
        await self.dump_page_snapshot(booking_number, "after_search")

    async def _open_booking_for_viewing(self, booking_id: str) -> None:
        """Open in portal (GUI button): the scan's own search_booking.

        search_booking also resets advisories and may follow the booking
        engine into a popup (setting portal_page); none of that may leak
        into the scan's state, so it is saved and put back. The page that
        ends up holding the booking stays in self._page for the caller.
        """
        saved = (self.portal_page, self.last_advisories, self._advisory_listener_attached)
        # Never attach the advisory listener to the person's tab: whatever
        # they browse there during a later scan would land in that scan's
        # booking advisories.
        self._advisory_listener_attached = True
        try:
            await self.search_booking(booking_id)
        finally:
            (self.portal_page, self.last_advisories,
             self._advisory_listener_attached) = saved

    # GoCCL's search page states its refusal in this label (read live on
    # DEMO46 2026-10-16: "This booking is cancelled").
    _SEARCH_MESSAGE = "#ctl00_DefaultContent_lblErrorMessage"

    async def _search_refusal(self) -> tuple[str, str] | None:
        """("cancelled" | "maintenance", text) when GoCCL refused to open the
        booking, else None. Never raises."""
        try:
            label = self.page.locator(self._SEARCH_MESSAGE)
            if await label.count():
                text = (await label.first.inner_text()).strip()
                if "cancel" in text.lower():
                    return "cancelled", text
            body = (await self.page.inner_text("body"))[:2000]
            # DEMO47 2026-10-15/16: "Oops...the site is not currently
            # available, but we're working on it. ... routine site maintenance"
            if "not currently available" in body.lower():
                return "maintenance", " ".join(body.split())[:160]
        except Exception:
            return None
        return None

    async def _search_landed(self, timeout: int = 15000) -> bool:
        """Has the booking page arrived? The only trustworthy success signal."""
        try:
            await self.page.wait_for_selector("#booked-root", timeout=timeout)
            return True
        except Exception:
            return False

    async def read_current_price_and_selection(self) -> dict:
        """Reads the current booking's price, offer/rate code, category, and
        stateroom type from window['initialData'] — a JSON blob the booking
        page embeds on load with the full invoice/rate/category detail (same
        pattern as NCL's window.__preloaded_data).

        The CSS selectors this replaced (recorded via DevTools) didn't match
        anything on a real live booking: confirmed against booking DEMO02
        that "[data-component='category-rate-header-rate-name']" and
        "booking-details-bar__category*" don't exist anywhere on the page —
        the rate/offer code in particular is never rendered as visible text
        at all, only present in this JSON (data.rate.code)."""
        data = await self.page.evaluate("() => window.initialData")
        if not data:
            raise RuntimeError("window.initialData not found on page — booking summary may not have loaded")

        invoice = data.get("invoiceSummary") or {}
        payment = data.get("paymentSchedule") or {}
        # Kept for core.booking_features, which reads sail date, region,
        # nights and ship from itinerary/ship - fields this method does not
        # itself return. No extra page load: this is the blob already read.
        self.last_initial_data = data
        gross = ((data.get("invoiceSummary") or {}).get("grossAmount") or {}).get("amount")
        rate = data.get("rate") or {}
        category = data.get("category") or {}
        stateroom_type = category.get("stateroomType") or {}

        if gross is None:
            # CONFIRMED REAL RISK, fixed 2026-08-13: this used to silently
            # default to 0.0 — old_total=0.0 flowing straight into
            # calculate_goccl would make every candidate look like a
            # negative-infinity "price_drop", either fabricating a huge
            # fake OPTIMIZATION or masking a real one. Refuse to guess.
            raise RuntimeError(
                "GoCCL window.initialData has no readable invoiceSummary.grossAmount.amount "
                "— refusing to treat this booking's total as $0"
            )

        # THE OFFER CODE IS NOT rate.code. Confirmed 2026-09-18 on a live
        # signed-in session (booking DEMO08): rate.code is the literal
        # constant "BKGRTE" - it appeared on all five wizard pages, and is
        # plainly a sentinel ("booking rate"), not an offer code. The real
        # code lives in rate.virtualCode / rate.gbrCode ("O7O" here), and
        # gbrCode is exactly what the comparison tiles publish as
        # data-rate-gbrcode.
        #
        # This mattered: read_offer_code_comparison returns codes like GO2 /
        # OB7 / PB4 / PNS / PSV, so a current code of "BKGRTE" could never
        # match any of them, and "is the booking already on this offer?"
        # was unanswerable - every offer looked like a different one.
        offer_code = rate.get("virtualCode") or rate.get("gbrCode") or ""
        if not offer_code and rate.get("code") not in (None, "", "BKGRTE"):
            offer_code = rate["code"]

        return {
            "current_price_gross": float(gross),
            "current_offer_code": offer_code,
            # Kept separately so the sentinel is visible rather than silently
            # swapped out, and so a future capture can prove whether
            # "BKGRTE" really is constant across different bookings (only
            # one booking has been observed so far).
            "current_rate_code_raw": rate.get("code") or "",
            "current_offer_name": rate.get("name") or "",
            "current_rate_is_group": bool(rate.get("isGroupRate")),
            # The current fare's own terms, as the booking states them.
            # rate.rules is a list of sentences, and it is where the CURRENT
            # onboard credit is declared - DEMO08's read "Offer includes
            # non-refundable and non-transferable onboard credit of USD
            # $50.00 per cabin." The comparison tiles never include the
            # booking's existing fare, so this is the only place on the
            # /rate screen that says whether OBC is being given up at all.
            "current_rate_disclaimer": " ".join(
                str(r) for r in (rate.get("rules") or []) if r),
            "current_rate_rules": rate.get("rules") or [],
            # THE REAL GUEST COUNT. Confirmed 2026-09-18 on booking DEMO08
            # against the live portal, closing the gap that
            # _probe_guest_count_candidates was written to detect - it fired
            # on exactly this path ("guests (count)": 1).
            #
            # Two independent confirmations on the same booking:
            #   * data.guests is the real guest list - one full record with
            #     name, date of birth, age, gratuities flag.
            #   * the engine's own request carried amountOfGuests=1.
            #
            # This matters because the /rate and /category prices are quoted
            # per guest: calculate_goccl multiplies by the guest count, and
            # with the old assumed default of 2 it turned a 1,392.00 quote
            # into a 2,784.00 "new total" on a single-guest booking. A wrong
            # occupancy does not produce a slightly-off number, it doubles
            # or halves every figure.
            "guests_count_actual": len(data.get("guests") or []) or None,
            # RULE PAY-001 / PAY-002. Payment state, read from the booking's
            # own paymentSchedule. GoCCL did no payment gating at all -
            # scraper/ncl.py has 8 references to final_payment_date and
            # core/calculator_msc.py 6, GoCCL had zero - and 6 of the 14
            # bookings surveyed on 2026-09-18 were already SETTLED
            # (netBalanceDue == 0), two of them past their final payment
            # date (DEMO35 8/23/2026, DEMO38 8/11/2026). Every one had a
            # working rate screen, so the scanner would have reported a
            # confident saving on a booking that is fully paid.
            #
            # netBalanceDue is the field to trust, NOT gross - paid. On
            # DEMO37 gross 1,987.31 - paid 1,766.81 = 220.50 while
            # balanceDue read 51.00; netBalanceDue was 0.00 and reconciled
            # exactly against net - paid. What balanceDue represents when
            # the two disagree is UNKNOWN and is deliberately not guessed at
            # here - it is captured so the question stays answerable.
            "net_balance_due": ((payment.get("netBalanceDue") or {}) or {}).get("amount"),
            "balance_due": ((payment.get("balanceDue") or {}) or {}).get("amount"),
            "has_debt": payment.get("hasDebt"),
            "final_payment_due_date": (
                (payment.get("finalPaymentDueDate") or {}) or {}).get("rawValue"),
            "payment_received": ((invoice.get("paymentReceivedAmount") or {}) or {}).get("amount"),
            "current_category": category.get("code") or "",
            "current_stateroom_type": stateroom_type.get("name") or "",
            # Diagnostic only, see _probe_guest_count_candidates above -- not
            # used for guests_count/guests_count_verified anywhere.
            "guest_count_candidates": _probe_guest_count_candidates(data),
            # Full raw blob, captured (2026-08-25) so a future real
            # occupancy field can actually be found and confirmed against
            # this booking's own real data instead of guessed — previously
            # only the four narrow fields above were ever dumped.
            "_raw_initial_data": data,
        }

    async def open_modify_booking(self) -> None:
        # Confirmed against a real booking: this is an <a> with no href
        # attribute (data-component="blue-bar-link-label"), so it never
        # gets the implicit ARIA "link" role get_by_role("link", ...)
        # requires — it just times out finding nothing. Text match doesn't
        # depend on role/href, so it works regardless of how the element
        # is implemented under the hood.
        modify_btn = self.page.get_by_text("Modify Booking", exact=True)
        await modify_btn.wait_for(state="visible")
        # Popup-aware (2026-09-18): GoCCL already moves the booking engine
        # into a popup once, so a click here that opens another window must
        # be followed rather than waited out on the page being left behind.
        popup = await self._click_following_popup(modify_btn, timeout=5000)
        if popup is not None:
            self._switch_to_page(popup)
        await self.page.wait_for_load_state("networkidle")

    async def open_change_offer_rate(self) -> None:
        # Same accessible-role caveat as open_modify_booking above — try
        # role-based first (works if this one really is a <button>), fall
        # back to a plain text match if not.
        try:
            change_rate_btn = self.page.get_by_role("button", name=re.compile("Change Offer/Rate", re.IGNORECASE))
            await change_rate_btn.wait_for(state="visible", timeout=5000)
        except Exception:
            change_rate_btn = self.page.get_by_text(re.compile("Change Offer/Rate", re.IGNORECASE))
            await change_rate_btn.wait_for(state="visible")
        popup = await self._click_following_popup(change_rate_btn, timeout=5000)
        if popup is not None:
            self._switch_to_page(popup)
        try:
            await self.page.wait_for_selector(
                "section.rate__container, div[class*='rate']", timeout=30000)
        except PlaywrightTimeoutError:
            # RULE ADV-001. Before calling this a timeout, ask whether the
            # portal actually refused. Give any in-flight response body a
            # moment to be read, then report the portal's own words.
            await asyncio.sleep(1.5)
            advisory = self.advisory_summary()
            if advisory:
                raise RuntimeError(
                    f"GoCCL will not quote this booking — {advisory}. "
                    f"The rate screen never loads because "
                    f"availability/rate returned an advisory, not rates. "
                    f"This is a fixable problem on the booking itself, not a "
                    f"scraper fault."
                ) from None
            raise

    async def read_offer_code_comparison(self) -> list[OfferCodeOption]:
        """Reads the offer-code comparison screen.

        Confirmed against a real booking (DEMO02): each offer is a
        div.rate-code-tile carrying data-rate-code/data-rate-name directly
        as attributes, and each stateroom-type price is a
        button.rate-code-tile__price-button carrying data-rate-meta-name/
        data-rate-meta-price/data-rate-meta-soldout. Reading these
        attributes directly is exact and order-independent.

        This replaced an earlier button-index-position guess (a fixed
        ["UPPER_LOWER","INTERIOR","OCEAN_VIEW","BALCONY","SUITE"] list
        assumed to line up with button order) that silently misaligned
        columns whenever a cell was sold out/N-A and shifted the index —
        confirmed against real data: it reported a "BALCONY" candidate
        that was actually an OCEAN VIEW price, a stateroom downgrade
        masquerading as a same-category fare-code swap.
        """
        tiles = await self.page.query_selector_all("div.rate-code-tile")
        results = []
        for tile in tiles:
            offer_code = (await tile.get_attribute("data-rate-code")) or ""
            offer_name = (await tile.get_attribute("data-rate-name")) or ""

            price_buttons = await tile.query_selector_all("button.rate-code-tile__price-button")
            for button in price_buttons:
                sold_out = (await button.get_attribute("data-rate-meta-soldout")) == "true"
                if sold_out:
                    continue
                stateroom_name = (await button.get_attribute("data-rate-meta-name")) or ""
                price_attr = await button.get_attribute("data-rate-meta-price")
                if not price_attr:
                    continue
                results.append(OfferCodeOption(
                    offer_name=offer_name,
                    offer_code=offer_code,
                    stateroom_type=stateroom_name,
                    price_per_person=self._parse_price(price_attr),
                ))
        return results

    async def read_category_prices(self) -> list[dict]:
        """Read the per-category price table on the /category wizard step.

        CONFIRMED 2026-09-18 against a live session (booking DEMO08). The
        booking engine is a SPA with a four-step wizard:

            /app/bookingengine/<REF>           booking summary
              -> /guest  -> /rate  -> /category  -> /review

        and the two screens carry DIFFERENT numbers:

          /rate      div.rate-code-tile[data-rate-code] with a price per
                     stateroom TYPE in data-rate-meta-price. These are the
                     "From $X" teasers.
          /category  one <tr data-cat="4A" data-cat-price="1391"> per
                     CATEGORY - the actual bookable price.

        The teaser and the real price are not the same: the OB7 tile
        advertised "From $1,392" while category 4A actually priced at
        1,391. So the tiles are for shortlisting only; a saving must never
        be computed from them.

        TRUNCATION - data-cat-price is a WHOLE NUMBER. The review step for
        that same 4A selection totalled $1,391.39, so this attribute is the
        gross truncated to dollars and carries up to $1 of error. Fine for
        ranking candidates, never for a reported saving: confirm the exact
        figure on /review.

        WHAT THE PRICE INCLUDES - taxes and fees, yes: the page states "All
        prices are in USD. Taxes & fees are included." and the review
        breakdown bears that out:
            Cruise Rate            410.00
            Non-Comm Cruise Amount 458.00   -> Guest Subtotal 868.00
            Required Cruise Fees   376.78
            Government Taxes & Fees 146.61  -> TOTAL 1,391.39

        BUT IT IS PER PERSON, NOT PER BOOKING. Corrected 2026-09-18. An
        earlier version of this docstring called these "gross figures,
        directly comparable to invoiceSummary.grossAmount" - true only by
        accident, because the single booking it was written from (DEMO08)
        had ONE guest, where per-person and total are the same number.

        The 3-guest booking DEMO10 separates them cleanly:
            PHY BALCONY tile = 514.00, category 8A = 514,
            and the booking total = 1,542.00 = 514 x 3.
        The page says so too: "Cruise rates are in US Dollars, average per
        person and based on single occupancy."

        So a comparison is:
            current invoiceSummary.grossAmount   (whole booking)
          vs  this price x guests_count          (per person x occupancy)
        which is what calculate_goccl does. Generalising from a one-guest
        booking is precisely the like-for-like mistake core/price_scope.py
        exists to catch - it just happened to be made in a comment rather
        than in the arithmetic.
        """
        return await self.page.evaluate("""
            () => Array.from(document.querySelectorAll('tr[data-cat]')).map(tr => ({
                category: tr.getAttribute('data-cat'),
                price_whole: tr.getAttribute('data-cat-price'),
                currency: tr.getAttribute('data-cat-price-currency'),
                is_guarantee: tr.getAttribute('data-cat-gtee') === 'true',
                selected: tr.getAttribute('data-cat-selected') === 'true',
                index: tr.getAttribute('data-index'),
            }))
        """)

    async def discard_changes(self) -> bool:
        """Back out of the refare wizard, leaving the booking untouched.

        Every wizard step offers a reversible exit - "Discard Changes"
        (data-comp="cancel") on /rate and /category, "Discard All Changes"
        on /review. Walking away without clicking one leaves the booking
        parked mid-edit, the same hazard ESPRESSO has with a reservation
        left retrieved (release_booking, 15-minute lock).

        Returns True if an exit control was found and clicked. Never
        raises: this runs on the error path, where the original failure is
        the thing worth reporting.
        """
        for selector in ('button[data-comp="cancel"]',
                         'button:has-text("Discard All Changes")',
                         'button:has-text("Discard Changes")'):
            try:
                btn = self.page.locator(selector).first
                if await btn.count() and await btn.is_visible():
                    # Cheap insurance: prove what is about to be clicked is
                    # a discard, not a commit that happens to sit nearby.
                    self._assert_safe_click(await btn.inner_text())
                    await btn.click(timeout=5000)
                    self.log_action("discard_changes", selector=selector)
                    await self._answer_discard_confirmation()
                    await self.page.wait_for_load_state("networkidle", timeout=10000)
                    return True
            except Exception:
                continue
        return False

    #: The "Are you sure? Navigating away from this page will discard any
    #: selections..." box. Recorded by Neon on DEMO18, 2026-10-12: Discard
    #: All Changes on /review opens it, and nothing is discarded until
    #: "Yes, Discard Changes" is pressed.
    _DISCARD_CONFIRM = 'div[role="dialog"] button:has-text("Yes, Discard Changes")'

    async def _answer_discard_confirmation(self) -> bool:
        """Press "Yes, Discard Changes" if the confirmation box appears.

        Without this the booking was left parked mid-edit behind an
        unanswered dialog - the same hazard discard_changes exists to end.
        Not every step asks (Discard Changes on /rate and /category did not
        in any capture), so its absence is fine. Never raises."""
        yes = self.page.locator(self._DISCARD_CONFIRM).first
        try:
            await yes.wait_for(state="visible", timeout=3000)
        except Exception:
            return False
        try:
            self._assert_safe_click(await yes.inner_text())
            await yes.click(timeout=5000)
            await yes.wait_for(state="hidden", timeout=5000)
            self.log_action("discard_confirmed")
            return True
        except Exception as exc:  # noqa: BLE001 - discard runs on error paths
            logger.warning("goccl.discard_confirm_failed", error=str(exc)[:200])
            return False

    async def read_obc_breakdown(self) -> dict:
        """Reads the PERKS section of the price breakdown on the review screen —
        confirmed structure from real Inspect element data. Returns a dict of
        {price_line_code: dollar_value}, e.g. {"POBC": 25.0, "NOBC": 25.0}.
        Call this on both the ORIGINAL booking and after each fare-code preview —
        OBC can silently change or disappear even when category stays the same."""
        lines = await self.page.query_selector_all(
            "article[data-component='price-breakdown-children'] li[data-price-line-code]"
        )
        result = {}
        for line in lines:
            code = await line.get_attribute("data-price-line-code")
            value_attr = await line.get_attribute("data-price-line-value")
            result[code] = self._parse_price(value_attr) if value_attr else 0.0
        return result

    # ── confirming a candidate on /review (ROADMAP P2.2, 2026-10-12) ─────
    #
    # The click sequence is the one walked live on 2026-09-18 by
    # goccl_safe_review.py (data/goccl_safe_review/): DEMO11 and DEMO39
    # reached /review, DEMO42 stopped at REVIEW_PATH_BLOCKED. Every
    # selector below was read off those captured pages and
    # data/goccl_watch/20260918T113403/:
    #   /rate      div.rate-code-tile[data-rate-code] button[data-rate-meta-name]
    #              button[data-comp="goto-next-page"]   "Continue"
    #   /category  tr[data-cat="<own>"] button          "Select $1,391"
    #              button[data-comp="continue-to-review"] "Keep Same Stateroom"
    #   /review    GET .../manage/<ref>/review-changes  the coded invoice
    #
    # PATH A ONLY (Neon, 2026-09-18): the booking keeps its own category
    # and stateroom. If "Keep Same Stateroom" stays disabled the walk STOPS
    # (REVIEW_PATH_BLOCKED) - another cabin is never picked to force the
    # review. "Confirm Changes" stays blocked by _assert_safe_click, and
    # check_booking discards on every path.

    _REVIEW_CHANGES_RE = re.compile(
        r"/app/bookingengine/api/v1\.0/manage/[^/?]+/review-changes", re.I)
    _KEEP_SAME = 'button[data-comp="continue-to-review"]'
    #: How long Keep Same Stateroom is given to enable, PER CATEGORY ROW tried
    #: (the live walker of 2026-09-18 used 16s for its one row).
    _KEEP_SAME_WAIT_S = 10.0
    #: At most this many category rows are tried per candidate, cheapest first.
    _MAX_CATEGORY_TRIES = 6
    #: The cabin label on /review - "Stateroom #1114" on DEMO08
    #: (data/goccl_watch/20260918T113403/009_win2_review.html).
    _STATEROOM_LABEL = '[data-component="offer-details-bar-stateroom-label"]'
    #: Which category row the last confirmation used (see confirm_candidate_on_review).
    last_review_choice: dict | None = None

    async def _keep_same_enabled(self, keep, wait_s: float) -> bool:
        """Is Keep Same Stateroom present and enabled? Polled, because it
        enables a moment after a row is selected. `disabled` alone is not
        trusted - aria-disabled is checked too, as the live walker did."""
        deadline = time.monotonic() + wait_s
        while True:
            try:
                if (await keep.count() and await keep.is_enabled()
                        and (await keep.get_attribute("aria-disabled")) != "true"):
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(1.0)

    def original_stateroom(self) -> str | None:
        """The booking's cabin number, from the page it loaded with."""
        room = (self.last_initial_data or {}).get("stateroom")
        code = room.get("code") if isinstance(room, dict) else None
        return str(code).strip().upper() if code else None

    #: The "Current Selections" bar on /rate and /category. Neon 2026-10-12:
    #: "check the category from here". Real markup (captured on DEMO40's and
    #: DEMO43's rate and category pages the same day):
    #:   <article data-component="booking-details-bar" data-component-name=
    #:            "category" data-detail="8D" data-metaname="BALCONY"
    #:            data-upgrade-to="Upgraded to: " ...>
    #: data-detail is the RATE category; when Carnival has upgraded the cabin
    #: the bar also reads "Upgraded to: 6B" (DEMO18: "6A Upgraded to: 6B").
    _CATEGORY_BAR = ('article[data-component="booking-details-bar"]'
                     '[data-component-name="category"]')

    async def read_category_bar(self) -> dict | None:
        """{"category", "stateroom_type", "upgraded_to"} from the bar, or None."""
        bar = self.page.locator(self._CATEGORY_BAR).first
        try:
            if not await bar.count():
                return None
            detail = ((await bar.get_attribute("data-detail")) or "").strip().upper()
            meta = ((await bar.get_attribute("data-metaname")) or "").strip().upper()
            text = " ".join((await bar.inner_text()).split())
        except Exception:
            return None
        up = re.search(r"Upgraded to:\s*([A-Z0-9]+)", text, re.I)
        return {"category": detail or None, "stateroom_type": meta or None,
                "upgraded_to": up.group(1).upper() if up else None}

    async def _deposit_on_review(self) -> bool | None:
        """Does /review's Payment Schedule list a Deposit? None if the box
        was not found. Neon's DEMO18 screenshot (2026-10-12) shows it as
        its own line - "Deposit $300.00 / Due Date Aug07, 2026" - above
        "Final Payment"; the captured DEMO08 review lists only Final Payment
        and Due Date under the "Payment Schedule" title."""
        try:
            lines = await self.page.evaluate("""() => {
                const out = [];
                for (const line of document.querySelectorAll('[data-component="price-breakdown-line"]')) {
                    const sec = line.closest('[data-component="price-breakdown-children"], [data-component="price-breakdown"]');
                    const title = sec && sec.querySelector('[data-component="price-breakdown-children__title"], [data-component="price-breakdown__header"]');
                    const d = line.querySelector('[data-component="price-breakdown-line__description"]');
                    out.push([(title && title.innerText || '').trim(), (d && d.innerText || '').trim()]);
                }
                return out;
            }""")
        except Exception:
            return None
        schedule = [desc for title, desc in lines if "payment schedule" in title.lower()]
        if not schedule:
            return None
        return any(desc.lower().startswith("deposit") for desc in schedule)

    async def _stateroom_on_review(self) -> str | None:
        """The cabin number /review shows, or None if the label is absent."""
        label = self.page.locator(self._STATEROOM_LABEL).first
        try:
            await label.wait_for(state="attached", timeout=5000)
            text = (await label.inner_text()).strip()
        except Exception:
            return None
        match = re.search(r"#\s*([A-Z0-9]+)", text, re.I)
        return match.group(1).upper() if match else None

    def original_invoice(self) -> ReviewInvoice:
        """The booking's own invoice, from the page it loaded with.

        initialData.invoiceSummary.agentInvoice has the SAME schema as the
        review-changes response, so before and after are read by the same
        parser and compared like for like (DEMO08: gross 1,365.81, POBC 50,
        reconciles to the cent)."""
        data = self.last_initial_data or {}
        agent = (data.get("invoiceSummary") or {}).get("agentInvoice")
        # The booking page names it paymentSchedule; /review paymentsSchedule.
        return parse_review_changes({"agentInvoice": agent,
                                     "paymentsSchedule": data.get("paymentSchedule")})

    async def confirm_candidate_on_review(
        self, offer_code: str, stateroom_type: str, own_category: str | None,
    ) -> tuple[str, ReviewInvoice | None]:
        """From the /rate screen, take ONE candidate to /review and return
        (path, invoice). path is REVIEW_REACHED or the reason it stopped.
        Leaves the wizard where it stopped - the caller discards."""
        # Never let one CANDIDATE's choice reach the next candidate's note.
        # Across bookings, reset_per_booking_state() already clears it
        # (PER_BOOKING_STATE); this is the per-candidate reset inside one
        # booking. {} reads exactly like None to every consumer
        # (`choice or {}`).
        self.last_review_choice = {}
        page = self.page
        tile_button = page.locator(
            f'div.rate-code-tile[data-rate-code="{offer_code}"] '
            f'button[data-rate-meta-name="{stateroom_type}"]')
        if await tile_button.count() != 1:
            return "CANDIDATE_NOT_ON_SCREEN", None
        await tile_button.first.click()
        await page.wait_for_load_state("networkidle", timeout=20000)

        cont = page.locator('button[data-comp="goto-next-page"]').first
        self._assert_safe_click((await cont.inner_text()).strip())
        await cont.click()
        # The category SCREEN, not a row: Keep Same Stateroom is always on
        # /category (captured 2026-09-18), so an empty table reports
        # OWN_CATEGORY_NOT_OFFERED at once instead of a 25s row timeout.
        await page.wait_for_selector(f"{self._KEEP_SAME}, tr[data-cat]", timeout=25000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        try:
            # The button may draw before the rows; give the rows a moment
            # before concluding the table is genuinely empty.
            await page.wait_for_selector("tr[data-cat]", timeout=8000)
        except PlaywrightTimeoutError:
            pass

        # CHEAPEST CATEGORY THAT KEEPS THE SAME STATEROOM. Neon 2026-10-12:
        # "i always in category choose the cheapest but you must always make
        # sure that keep same state room button is eenabled ... not lose
        # anything on the booking". On DEMO18 he took 6A at $574 for cabin
        # R162 (category 6B, $579): 6A's "Upgrade up to: 6C" covers 6B, so
        # Carnival kept the cabin at the cheaper rate - $46 saved against
        # $36 at the booking's own row. So: rows cheapest first, never above
        # the booking's own category price, and a row is used only once
        # Carnival enables Keep Same Stateroom for it. Replaces the
        # 2026-09-18 own-row-only rule; the cabin rule itself is unchanged
        # and is now also VERIFIED on /review (_stateroom_on_review).
        rows = await page.evaluate(
            "() => [...document.querySelectorAll('tr[data-cat]')]"
            ".map(r => [r.dataset.cat, r.dataset.catPrice])")
        priced = []
        for cat, price in rows:
            try:
                priced.append((float(str(price).replace(",", "")), cat))
            except (TypeError, ValueError):
                continue
        priced.sort()
        own_price = next((p for p, c in priced if c == own_category), None)
        tries = [(p, c) for p, c in priced if own_price is None or p <= own_price]
        if not tries:
            return "OWN_CATEGORY_NOT_OFFERED", None
        # THE OWN ROW IS ALWAYS TRIED. On DEMO15 (2026-10-12) the table had
        # 16+ rows; Keep Same Stateroom enabled only on the booking's own 8D
        # ($667), after the cheaper $617/$652 rows did not. A plain cap could
        # run out of tries before reaching it. Cheapest first, own row last.
        own_row = [(p, c) for p, c in tries if c == own_category]
        cheaper = [(p, c) for p, c in tries if c != own_category]
        tries = cheaper[:self._MAX_CATEGORY_TRIES - len(own_row)] + own_row

        keep = page.locator(self._KEEP_SAME).first
        chosen = None
        for price, cat in tries:
            row = page.locator(f'tr[data-cat="{cat}"] button').first
            if not await row.count():
                continue
            self._assert_safe_click((await row.inner_text()).strip())
            await row.click()
            await page.wait_for_load_state("networkidle", timeout=20000)
            if await self._keep_same_enabled(keep, self._KEEP_SAME_WAIT_S):
                chosen = (price, cat)
                break
            self.log_action("keep_same_disabled", offer_code=offer_code, category=cat)
        if chosen is None:
            self.log_action("review_path_blocked", offer_code=offer_code,
                            tried=[c for _, c in tries])
            return "REVIEW_PATH_BLOCKED", None
        self.last_review_choice = {"category": chosen[1], "row_price": chosen[0],
                                   "own_category": own_category}
        self.log_action("category_chosen", offer_code=offer_code,
                        category=chosen[1], row_price=chosen[0],
                        own_category=own_category)

        # READ THE BODY THE MOMENT IT ARRIVES. Caught 2026-10-12 by the fake
        # wizard test, which hung: Keep Same Stateroom fires the request and
        # then routes to /review, and awaiting response.json() AFTER that
        # navigation can stall indefinitely in Chromium. The live walker of
        # 2026-09-18 read bodies from a response listener for the same
        # reason. Bounded at both ends so a scan can never hang here.
        captured: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_response(resp):
            if captured.done() or not self._REVIEW_CHANGES_RE.search(resp.url):
                return

            async def read():
                body = None
                if resp.status < 400:
                    try:
                        body = await asyncio.wait_for(resp.json(), timeout=20)
                    except Exception:
                        body = None
                if not captured.done():
                    captured.set_result((resp.status, body))
            track_background_task(self._background_tasks, asyncio.ensure_future(read()))

        self._assert_safe_click((await keep.inner_text()).strip())
        page.on("response", on_response)
        try:
            await keep.click()
            status, body = await asyncio.wait_for(captured, timeout=30)
        except asyncio.TimeoutError:
            return "REVIEW_NOT_CAPTURED", None
        finally:
            page.remove_listener("response", on_response)
        if status >= 400:
            self.log_action("review_refused", offer_code=offer_code, status=status)
            return "REVIEW_REFUSED", None
        if body is None:
            return "REVIEW_NOT_CAPTURED", None
        review = parse_review_changes(body)

        # THE CABIN, VERIFIED. Keep Same Stateroom being enabled is
        # Carnival's promise; the label on /review is the proof. A different
        # cabin is never a saving - stop. An absent label is recorded, not
        # guessed past silently.
        original_room = self.original_stateroom()
        review_room = await self._stateroom_on_review()
        self.last_review_choice = {**(self.last_review_choice or {}),
                                   "stateroom_before": original_room,
                                   "stateroom_on_review": review_room}
        if original_room and review_room and review_room != original_room:
            self.log_action("stateroom_changed", offer_code=offer_code,
                            before=original_room, on_review=review_room)
            return "STATEROOM_CHANGED", None
        if not review_room:
            review.warnings.append("stateroom label not shown on /review - "
                                   "cabin kept per Keep Same Stateroom, not re-read")
        review.deposit_on_review = await self._deposit_on_review()

        self.log_action("review_reached", offer_code=offer_code,
                        gross=str(review.gross), obc=str(review.obc),
                        stateroom=review_room)
        return "REVIEW_REACHED", review

    async def check_booking(self, booking_id: str, capture_market_data: bool = False) -> BookingResult:
        """
        Full read-only check: current price, current OBC, current category, and
        all available fare/offer codes for the current stateroom type. Does NOT
        select/click/confirm anything beyond navigating to the comparison screen.

        Returns an UNCONFIRMED candidate (see core/calculator.py:calculate_goccl)
        — GoCCL doesn't expose per-fare-code OBC without clicking through, so
        this automatic scan can only flag a cheaper offer code to check by hand.
        To actually confirm one candidate (which requires clicking through),
        use preview_fare_code() afterward for one human-selected candidate at a time.
        """
        # See BaseScraper.reset_per_booking_state. GoCCL caches
        # last_initial_data and last_market_data on self, and BookingService
        # reads both AFTER this returns.
        self.reset_per_booking_state()

        current_category: str | None = None
        try:
            await self.search_booking(booking_id)
            current = await self.read_current_price_and_selection()
            current_category = current["current_category"]
            current_obc = await self.read_obc_breakdown()

            # RULE PAY-001: a settled booking is not an opportunity.
            # Checked BEFORE entering the wizard, so a fully-paid booking is
            # never parked mid-refare just to be rejected afterwards.
            #
            # netBalanceDue is the agency's outstanding amount. 0 means the
            # booking is paid; repricing it is a different, manual
            # conversation (refund/credit) rather than the automatic
            # price-drop this scan looks for - the same reasoning as
            # ESPRESSO's and NCL's paid-in-full handling, which GoCCL simply
            # never had.
            net_balance = current.get("net_balance_due")
            if net_balance is not None and net_balance <= 0.01:
                paid = current.get("payment_received")
                logger.info("goccl.paid_in_full", booking_id=booking_id,
                            net_balance_due=net_balance, paid=paid)
                self.log_action("paid_in_full", booking_id=booking_id,
                                net_balance_due=net_balance)
                await self.discard_changes()
                return BookingResult(
                    cruise_line=CruiseLine.GOCCL,
                    status=BookingStatus.PAID_IN_FULL,
                    booking_id=booking_id,
                    price_category=current_category,
                    old_total=round(float(current["current_price_gross"]), 2),
                    new_total=round(float(current["current_price_gross"]), 2),
                    currency=current.get("currency") or "UNKNOWN",
                    # The booking's own offer, kept even when it is not
                    # priced (Neon 2026-10-15: save before for everything).
                    old_promos=" ".join(x for x in (current.get("current_offer_code"),
                                                    current.get("current_offer_name")) if x),
                    note=(
                        f"paid in full — net balance due "
                        f"{net_balance:,.2f}"
                        + (f", {paid:,.2f} received" if paid is not None else "")
                        + ". Repricing a settled booking is a refund/credit "
                          "conversation, not an automatic price drop."
                    ),
                )

            await self.open_modify_booking()
            await self.open_change_offer_rate()
            await self.dump_page_snapshot(booking_id, "offer_code_comparison")
            # The booking's own category, read from the Current Selections
            # bar (Neon 2026-10-12: "check the category from here"). It
            # decides which category rows are cheaper than the booking's own.
            # When it disagrees with the page data the bar wins, logged.
            bar = await self.read_category_bar()
            own_category = (bar or {}).get("category") or current_category
            if bar and bar.get("category") and bar["category"] != (current_category or "").upper():
                logger.warning("goccl.category_mismatch", booking_id=booking_id,
                               bar=bar["category"], page_data=current_category,
                               upgraded_to=bar.get("upgraded_to"))
            self.log_action("category_bar", booking_id=booking_id,
                            category=(bar or {}).get("category"),
                            upgraded_to=(bar or {}).get("upgraded_to"))

            offer_codes = await self.read_offer_code_comparison()
            logger.info(
                "goccl.offer_codes", booking_id=booking_id,
                current_stateroom_type=current["current_stateroom_type"], count=len(offer_codes),
            )
            self.log_action(
                "read_offer_code_comparison", booking_id=booking_id, count=len(offer_codes),
            )

            # See _probe_guest_count_candidates -- surfaced loudly (not just
            # buried in the raw dump) the first time any candidate path ever
            # resolves to something real, so it actually gets noticed and
            # checked rather than sitting unread in a JSONL file.
            guest_candidates = current.get("guest_count_candidates") or {}
            if guest_candidates:
                logger.warning(
                    "goccl.guest_count_candidate_found", booking_id=booking_id,
                    candidates=guest_candidates,
                )
                self.log_action(
                    "guest_count_candidate_found", booking_id=booking_id,
                    candidates=guest_candidates,
                )

            if capture_market_data:
                self.last_market_data = {
                    "currentCategory": current_category,
                    "currentStateroomType": current["current_stateroom_type"],
                    "executionToken": None,
                    "selectionJSON": None,
                    "rows": [
                        {
                            "offer_name": o.offer_name,
                            "offer_code": o.offer_code,
                            "stateroom_type": o.stateroom_type,
                            "price_per_person": o.price_per_person,
                        }
                        for o in offer_codes
                    ],
                }

            self.dump_raw(booking_id, {
                "current": current,
                "current_obc": current_obc,
                "offer_codes": [o.__dict__ for o in offer_codes],
            })

            result = calculate_goccl(
                booking_id=booking_id,
                price_category=current_category,
                current_stateroom_type=current["current_stateroom_type"],
                current_offer_code=current["current_offer_code"],
                current_price_gross=current["current_price_gross"],
                available_offer_codes=[o.__dict__ for o in offer_codes],
                # Prefer the booking's OWN guest list over the global default.
                # An explicit count passed to the constructor still wins - a
                # caller who states the occupancy knows something this does
                # not - but otherwise the portal's own data beats a guess.
                guests_count=(
                    self.guests_count if self.guests_count_verified
                    else (current.get("guests_count_actual") or self.guests_count)
                ),
                guests_count_verified=(
                    self.guests_count_verified
                    or current.get("guests_count_actual") is not None
                ),
                # The booking's CURRENT fare, so a candidate can be judged on
                # what it does to the customer's terms and not only on price
                # (core/goccl_fare_types.py). The NAME carries the fare type
                # in plain words; the 3-letter code does not.
                current_offer_name=current.get("current_offer_name"),
                current_disclaimer=current.get("current_rate_disclaimer"),
            )
            # CONFIRM THE CANDIDATE ON /review (2026-10-12). Only an
            # OPTIMIZATION with an offer code is a candidate; everything else
            # is already final. We are still on /rate here.
            if (settings.goccl_confirm_on_review
                    and result.status == BookingStatus.OPTIMIZATION
                    and result.new_price_category):
                try:
                    path, review = await self.confirm_candidate_on_review(
                        result.new_price_category,
                        current["current_stateroom_type"], own_category)
                except Exception as exc:  # noqa: BLE001 - stays a candidate
                    if is_dead_browser_error(exc):
                        raise
                    logger.warning("goccl.review_failed", booking_id=booking_id,
                                   error=str(exc)[:200])
                    path, review = "REVIEW_NOT_CAPTURED", None
                logger.info("goccl.review_path", booking_id=booking_id, path=path,
                            offer_code=result.new_price_category)
                if review is not None:
                    self.dump_raw(booking_id, {"review_invoice": {
                        "offer_code": result.new_price_category,
                        "gross": str(review.gross), "obc": str(review.obc),
                        "items": {k: str(v) for k, v in review.items.items()},
                        "warnings": review.warnings}})
                result = apply_goccl_review(result, self.original_invoice(),
                                            review, path,
                                            choice=self.last_review_choice)
            logger.info("goccl.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
            self.log_action("result", booking_id=booking_id, status=result.status.value, net_saving=result.net_saving)
            # BACK OUT OF THE WIZARD. Added 2026-09-18 after driving the real
            # flow: open_change_offer_rate leaves the booking sitting inside
            # the refare wizard at /rate. Walking away from there parks a
            # pending change on a live booking - the same hazard as an
            # ESPRESSO reservation left retrieved under a 15-minute lock
            # (see espresso.release_booking). Verified against booking
            # DEMO08: discard_changes() returned True and the booking was
            # left untouched.
            await self.discard_changes()
            return result

        except BookingCancelled as e:
            # CLAUDE.md rule 7: recorded, reported every run, never re-scanned.
            logger.info("goccl.cancelled", booking_id=booking_id, message=str(e))
            self.log_action("cancelled", booking_id=booking_id)
            return make_cancelled_result(booking_id, current_category, CruiseLine.GOCCL,
                                         detail=f"GoCCL: {e}")
        except Exception as e:
            logger.error("goccl.error", booking_id=booking_id, error=str(e))
            self.log_action("error", booking_id=booking_id, error=str(e))
            await self.dump_failure_snapshot(booking_id, "check_booking_failed", str(e))
            # Back out on the failure path too, BEFORE deciding what to
            # return: a booking abandoned mid-wizard by an error is exactly
            # the case that would otherwise stay parked.
            try:
                await self.discard_changes()
            except Exception:
                pass
            # CONFIRMED REAL RISK, fixed 2026-08-13: same defect as NCL's
            # check_booking (see scraper/ncl.py) — swallowing every
            # exception here, including a dead browser/page/crash,
            # permanently defeated BookingService's restart mechanism for
            # GoCCL. Re-raise dead-browser-shaped exceptions so the caller
            # can actually recover; every other real portal-level failure
            # still becomes an ordinary ERROR result exactly as before.
            if is_dead_browser_error(e):
                raise
            return make_error_result(booking_id, current_category, CruiseLine.GOCCL, str(e))

    @staticmethod
    def _parse_price(text: str) -> float:
        cleaned = re.sub(r"[^\d.]", "", text)
        return float(cleaned) if cleaned else 0.0

    # -------------------------------------------------------------------
    # STOP HERE for normal scanning. Everything below this line is the
    # "preview a specific option" path, which involves clicking further
    # into the flow (still never to the final confirm). Only call this
    # for a single candidate the human has already chosen to look at
    # more closely — not as part of routine bulk scanning.
    # -------------------------------------------------------------------

    async def preview_fare_code(self, offer_code: str, original_category_code: str) -> FareCodeCandidateResult:
        """
        Previews ONE fare/offer code while keeping the booking's existing category
        unchanged — this is the real comparison GoCCL supports: category stays
        fixed, fare code varies. Confirmed two-step flow via DevTools Recorder:
          1. Pick the offer code's price for the current stateroom type, CONTINUE.
          2. On the category table, select the SAME category code as the original
             booking (not the cheapest — the original), then KEEP SAME STATEROOM.
        Reads GROSS AMOUNT and the full OBC/PERKS breakdown on the resulting
        review screen. Does NOT click any final purchase/confirm button.

        Call this once per fare code candidate, for a human-reviewed comparison —
        not as an unattended loop that assumes the cheapest gross price wins,
        since a lower price with reduced/dropped OBC may not be a real saving.
        """
        self.log_action("preview_fare_code", offer_code=offer_code, category_code=original_category_code)

        offer_button = self.page.get_by_role("button", name=re.compile(offer_code, re.IGNORECASE))
        await offer_button.first.scroll_into_view_if_needed()
        await offer_button.first.click()

        continue_btn = self.page.get_by_role("button", name="CONTINUE")
        await continue_btn.click()
        await self.page.wait_for_load_state("networkidle")

        # Select the SAME category as the original booking — not the cheapest.
        category_row = self.page.locator(f"tr[data-cat='{original_category_code}']")
        await category_row.locator("span.price__number").click()

        keep_stateroom_btn = self.page.get_by_role("button", name="KEEP SAME STATEROOM")
        await keep_stateroom_btn.click()
        await self.page.wait_for_load_state("networkidle")

        gross_text = await self.page.inner_text(
            "[data-component='price-breakdown-line__value']"
        )
        new_price_gross = self._parse_price(gross_text)
        obc_lines = await self.read_obc_breakdown()
        obc_total = obc_lines.get("POBC", 0.0)

        result = FareCodeCandidateResult(
            offer_code=offer_code,
            category_code=original_category_code,
            new_price_gross=new_price_gross,
            new_obc_total=obc_total,
            new_obc_lines=obc_lines,
        )
        self.log_action(
            "preview_fare_code_result", offer_code=offer_code,
            new_price_gross=new_price_gross, new_obc_total=obc_total,
        )
        return result

    # ── Carnival guest (VIFP) lookup - MSC status-match branch ───────────
    #
    # Added 2026-10-08. Every selector below was read off a real signed-in
    # page saved with SingleFile (Downloads/1b40c7e0-....htm, url
    # /app/bookingengine?openLookup=yes) and is pinned by
    # tests/test_vifp_guest_lookup_2026_10_08.py:
    #
    #   <input id=vifpNumber_0 class="... vifp-widget__field-input"
    #          placeholder="Carnival Guest #">
    #   <button class="vifp-widget__field-button">  (magnifying glass)
    #   <div role=dialog class="ReactModal__Content ... cmodal vifp__modal">
    #     <div class=vifp-guest-info>  label/text pairs, READ-ONLY spans
    #     <div class=vifp-guest-info__add><button>Add Number</button>
    #   <select id=StateOfResidency class=state-of-residency__select>
    #     options value=GA text=Georgia ...
    #
    # Each selector is a union of GoCCL's id and its own class, so it also
    # matches the hand-annotated regression fixture, which renamed the ids.
    #
    # The guest's name, title and date of birth are never typed anywhere:
    # GoCCL displays them, this reads them. See core/vifp_guest.py.

    VIFP_INPUT = "input#vifpNumber_0, input.vifp-widget__field-input"
    VIFP_SEARCH_BUTTON = "button.vifp-widget__field-button"
    VIFP_DIALOG = '[role="dialog"].vifp__modal'
    VIFP_GUEST_INFO = ".vifp-guest-info"
    VIFP_ADD_BUTTON = ".vifp-guest-info__add button"
    STATE_OF_RESIDENCY = "select#StateOfResidency, select.state-of-residency__select"

    _READ_GUEST_INFO_JS = """(root) => {
        const out = {}, dupes = [];
        for (const label of root.querySelectorAll('.vifp-guest-info__label')) {
            const key = label.innerText.trim();
            const txt = label.parentElement.querySelector('.vifp-guest-info__txt');
            if (key in out) dupes.push(key);
            out[key] = txt ? txt.innerText : '';
        }
        return {fields: out, dupes};
    }"""

    async def _exactly_one(self, selector: str, what: str):
        loc = self.page.locator(selector)
        count = await loc.count()
        if count != 1:
            raise RuntimeError(f"GoCCL {what}: expected 1 element for "
                               f"{selector!r}, found {count}")
        return loc

    async def open_vifp_lookup(self) -> None:
        """Reach the booking engine with the guest-number lookup showing.

        Needs a signed-in session, like open_booking_tool. Does nothing if
        the lookup is already on screen.
        """
        if await self.page.locator(self.VIFP_INPUT).count():
            return
        self.log_action("open_vifp_lookup", url=settings.goccl_vifp_lookup_url)
        await self.navigate(settings.goccl_vifp_lookup_url, wait_until="domcontentloaded")
        try:
            await self.page.wait_for_selector(self.VIFP_INPUT, timeout=20000)
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "GoCCL guest lookup did not load (no Carnival Guest # box) - "
                f"is the session signed in? Landed on {self.page.url}"
            ) from None

    async def _read_vifp_dialog(self) -> CarnivalGuestInfo:
        info_root = await self._exactly_one(
            f"{self.VIFP_DIALOG} {self.VIFP_GUEST_INFO}", "guest info window")
        raw = await info_root.evaluate(self._READ_GUEST_INFO_JS)
        if raw["dupes"]:
            raise RuntimeError(f"GoCCL guest info repeats labels {raw['dupes']} "
                               f"- refusing to guess which is right")
        return guest_info_from_fields(raw["fields"])

    async def lookup_vifp_guest(self, guest_number: str,
                                screenshot_path: str | None = None) -> CarnivalGuestInfo:
        """Look one guest up by Carnival Guest Number and read the record.

        detect -> act -> verify:
          1. type the number and confirm the box holds exactly it
          2. open the guest window (magnifying glass)
          3. read every field by its label
          4. confirm the window shows THIS guest number - a window left
             open for someone else must never be taken as this guest's
          5. screenshot the window, unedited, as the evidence

        Does not click Add Number - see add_vifp_guest.
        """
        wanted = normalise_guest_number(guest_number)
        if not wanted:
            raise ValueError(f"not a Carnival Guest Number: {guest_number!r}")

        box = (await self._exactly_one(self.VIFP_INPUT, "guest number box")).first
        await box.fill(wanted)
        typed = normalise_guest_number(await box.input_value())
        if typed != wanted:
            raise RuntimeError(f"GoCCL guest number box holds {typed!r} after "
                               f"typing {wanted!r}")
        self.log_action("vifp_number_entered")

        dialog = self.page.locator(self.VIFP_DIALOG).filter(
            has=self.page.locator(self.VIFP_GUEST_INFO))
        if await dialog.count() and await dialog.first.is_visible():
            # A window is already open. Only acceptable if it is this guest.
            already = await self._read_vifp_dialog()
            if already.guest_number != wanted:
                raise RuntimeError(
                    "a GoCCL guest window for a different guest number is "
                    "already open - close it and look the guest up again")
            self.log_action("vifp_dialog_already_open")
        else:
            button = (await self._exactly_one(self.VIFP_SEARCH_BUTTON, "guest lookup button")).first
            await button.click()
            try:
                await dialog.first.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                await self.dump_failure_snapshot(f"vifp_{wanted}", "vifp_lookup",
                                                 "guest window never opened")
                raise RuntimeError(
                    "GoCCL guest window never opened for this guest number - "
                    "the number may be wrong (GoCCL advisory 5108 reads "
                    "'The VIFP number is incorrect.')") from None

        info = await self._read_vifp_dialog()
        if info.guest_number != wanted:
            raise RuntimeError(f"GoCCL returned guest number {info.guest_number!r} "
                               f"for a lookup of {wanted!r}")

        if screenshot_path:
            await dialog.first.screenshot(path=screenshot_path)
        self.log_action("vifp_guest_read", tier=info.tier,
                        screenshot=bool(screenshot_path))
        return info

    async def add_vifp_guest(self, info: CarnivalGuestInfo) -> str:
        """Click Add Number, then set State of Residency from the record.

        Add Number puts the guest number on a NEW booking search; it does
        not touch any existing booking. The state is chosen by matching
        GoCCL's own option text to the record's "State of Residency", so no
        state table lives here. Returns the selected state code, verified.
        """
        add = (await self._exactly_one(self.VIFP_ADD_BUTTON, "Add Number button")).first
        label = (await add.inner_text()).strip()
        self._assert_safe_click(label)
        await add.click()
        # Verified: the number must still be in the box afterwards. Whether
        # GoCCL also closes the window has not been seen live yet, so it is
        # recorded rather than asserted - the first live run settles it.
        box = self.page.locator(self.VIFP_INPUT).first
        if normalise_guest_number(await box.input_value()) != info.guest_number:
            raise RuntimeError("the Carnival Guest # box no longer holds this "
                               "guest's number after Add Number")
        dialog_still_open = await self.page.locator(self.VIFP_DIALOG).first.is_visible()
        self.log_action("vifp_add_number_clicked", dialog_still_open=dialog_still_open)

        select = (await self._exactly_one(self.STATE_OF_RESIDENCY, "State of Residency")).first
        options = await select.evaluate(
            "s => [...s.options].map(o => [o.value, o.text.trim()])")
        wanted = info.state_of_residency.strip().lower()
        codes = [v for v, text in options if v and text.lower() == wanted]
        if len(codes) != 1:
            raise RuntimeError(f"State of Residency has {len(codes)} options "
                               f"named {info.state_of_residency!r}")
        await select.select_option(value=codes[0])
        chosen = await select.input_value()
        if chosen != codes[0]:
            raise RuntimeError(f"State of Residency shows {chosen!r} after "
                               f"selecting {codes[0]!r}")
        self.log_action("vifp_residency_selected", state=chosen)
        return chosen
