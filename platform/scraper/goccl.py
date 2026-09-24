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
from core.calculator import calculate_goccl, make_error_result
from core.models import BookingResult, BookingStatus, CruiseLine
from utils.logging import get_logger

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
    # sweep. Four bookings (DEMO09, CQ7X35, CQ7W42, CH7M42) reached /guest
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

                asyncio.ensure_future(read())
            except Exception:
                pass

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

    async def search_booking(self, booking_number: str) -> None:
        # Reach the booking engine through the real flow (popup and all)
        # rather than deep-linking into it.
        await self.open_booking_tool()
        # Advisories are per booking - clear them, then listen. Attached
        # here rather than in start() so the listener is bound to the page
        # the wizard actually runs on (the booking engine, not the portal).
        self.last_advisories = []
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

        if submitted_by is None:
            raise RuntimeError(
                f"GoCCL search for {booking_number} never reached the booking "
                f"page (#booked-root) after trying the Search link, the "
                f"Search button and the Enter key"
            )
        self.log_action("search_submitted", booking_id=booking_number, via=submitted_by)
        await self.dump_page_snapshot(booking_number, "after_search")

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
            # date (MW24H6 8/23/2026, TM66H0 8/11/2026). Every one had a
            # working rate screen, so the scanner would have reported a
            # confident saving on a booking that is fully paid.
            #
            # netBalanceDue is the field to trust, NOT gross - paid. On
            # PR40T9 gross 1,987.31 - paid 1,766.81 = 220.50 while
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
                    await self.page.wait_for_load_state("networkidle", timeout=10000)
                    return True
            except Exception:
                continue
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
