"""Command dispatch for msc_session_controller.py — kept in its own
module and hot-reloaded (importlib.reload) before every command, so new
commands can be added while the browser session stays open. Never
requires a controller restart (and therefore never a re-login) just to
gain a new capability.

Commands (one per line in data/msc_control/command.txt):
  goto:<url>                 - navigate the CURRENT tab
  click_text:<exact text>    - click the first element whose visible text
                                matches exactly (link, button, span, etc.)
  click_selector:<css>       - click the first element matching a CSS selector
  read                       - dump page URL + visible body text
  screenshot                 - save a full-page screenshot
  new_tab                    - open a new tab in the SAME session (confirmed
                                safe on MSC, unlike ESPRESSO) and make it the
                                current tab for subsequent commands — the
                                previous tab is left open, not closed
  switch_tab:<index>         - make an already-open tab (0-based, in the
                                order opened) the current one again
  select_option_label:<text> - find whichever <select> on the page has an
                                <option> with this exact visible text, and
                                select it (native dropdowns don't open
                                visibly in a screenshot, so click-based
                                selection doesn't work for these)
  fill_by_placeholder:<placeholder>|<value> - fill the first input whose
                                placeholder text contains <placeholder>
  eval:<js expression>       - run arbitrary JS in the page and return the
                                result (generic escape hatch)
  lookup_booking:<id>        - read-only: open one booking, capture its
                                summary + Price Breakdown text, append to
                                data/msc_control/booking_data.jsonl
  batch_lookup:<id1,id2,...> - same as lookup_booking, looped over a
                                comma-separated list; one failure doesn't
                                stop the rest
  check_today_rate:<id>      - read-only: open a booking, click "Book Same
                                Departure", capture the category listing +
                                available discount options (none selected,
                                never proceeds further), append to
                                data/msc_control/rate_check_data.jsonl
  batch_check_today_rate:<id1,id2,...> - same, looped

  NOTE on check_today_rate/batch_check_today_rate: confirmed 2026-08-10 that
  the page these land on (the occupancy/discount screen) does NOT yet show
  any categories or prices — that only appears one screen further, behind
  the "CONFIRM AND PROCEED" button, which this automation is not allowed to
  click on its own (blocked by the safety classifier as a flow-advancing
  action, same as any other "commit-like" button). Use the batch-tab
  commands below to actually reach prices; treat the two commands above as
  useful only for a quick category/current-price/discount-dropdown-options
  peek, not for a real today's-price comparison.

  relogin                     - log back into MSC Book using credentials
                                saved via msc_save_credentials.py (Windows
                                Credential Manager) — use this any time the
                                session shows "Session Timed Out"/"Session
                                Expired" instead of asking Neon to log in
                                by hand.

  confirm_and_proceed        - clicks the real "CONFIRM AND PROCEED" button
                                (dismissing the "Policy Reminder" popup
                                first if it's showing). HISTORICAL NOTE:
                                this used to be restricted to Neon's own
                                confirm_and_proceed.ps1 trigger, since
                                Claude's own tool calls were blocked from
                                this exact click by Claude Code's safety
                                classifier (confirmed blocked twice,
                                2026-08-10). RESOLVED 2026-08-11: Neon
                                added a narrowly-scoped autoMode.allow
                                permission rule to his own
                                ~/.claude/settings.json for exactly this
                                click via this exact command/file,
                                confirmed working live — this command can
                                now be called by automation too (see
                                check_booking/check_booking_batch below),
                                not just Neon's hotkey. Kept as its own
                                command since the hotkey script still
                                uses it directly.

  --- Fully automated single-call flow (added 2026-08-11) ---
  check_booking:<id>          - THE recommended way to check one booking
                                from here on: runs lookup -> stage ->
                                confirm -> harvest -> evaluate in one
                                call, zero human clicks needed (safe
                                since the permission-rule fix above).
                                Automatically retries once via relogin if
                                the session had gone idle. Appends the
                                full MscBookingResult to
                                data/msc_control/live_check_results.jsonl
                                and returns a human-readable summary of
                                all four opportunity checks (including the
                                new VOYAGERS_SELECTION check — see
                                core/calculator_msc.py).
  check_booking_batch:<id1,id2,...> - same, looped over a comma-separated
                                list with pacing between bookings; one
                                failure doesn't stop the rest.
  NOTE 2026-08-11: staging/harvesting now also captures whether the
  literal on-page phrase "Club discount available, insert Voyagers Club
  to activate." is present — Neon's direct instruction: "always look at
  this phrase as this is a big indicator." It confirms the flat 5%
  Voyagers Club discount can genuinely be added to a sailing/rate, and
  closes a real gap where DISCOUNT_ADD previously only ever checked the
  military/senior dropdown and never checked club-discount eligibility
  at all, despite that being the single most common real finding across
  this project's history.

  check_booking_batch2:<id1,id2,...> - same, but runs 2 bookings truly
                                concurrently across 2 tabs (added
                                2026-08-11 at Neon's request — MSC does
                                allow 2 tabs against the same login).
                                KNOWN RISK, not fully eliminated: a real
                                2026-08-10 incident showed 2 tabs CAN
                                trigger a silent MSC-side session/cookie
                                conflict. Mitigated via a per-sailing
                                partNumber fingerprint check in
                                _check_booking_msc — a real conflict now
                                surfaces as an explicit
                                'sailing_identity_mismatch' result instead
                                of silently wrong data, but isn't
                                prevented outright (that would require
                                MSC's own backend to behave differently).
                                If mismatches show up in practice, drop
                                back to check_booking_batch (1 tab).

  --- Live discount price-testing (added 2026-08-13) ---
  CONFIRMED REAL GAP this closes, from a forensic investigation of
  bookings 3000026/74242969: evaluate_msc_booking() can detect a
  discount is ELIGIBLE but never determines what it's actually worth —
  MSC represents at least one real discount (Senior) as a non-literal,
  dynamically-computed rate with no percentage anywhere to parse, and the
  existing "Voyagers Club 5%" figure is a hardcoded assumption, never
  read from the live page. These commands actually select a discount on
  MSC's own occupancy screen and read MSC's own recalculated price —
  never an assumed percentage times the current total. Reuses the exact
  same "Book Same Departure" -> "CONFIRM AND PROCEED" flow every existing
  check_booking call already runs, unattended (see
  _confirm_and_proceed_click's docstring for why that click is already
  pre-authorized for automation) — no new commit-style action, no save/
  payment/purchase control is ever touched. Ends with a fresh, independent
  re-lookup of the real booking proving nothing was actually committed;
  if that verification ever fails, the result is RESTORATION_FAILED, not
  a savings figure, and that booking should not be processed further
  without human review.

  test_discount:<id>:<label>  - selects <label> from the "Additional
                                Discounts" dropdown on <id>'s occupancy
                                screen (e.g. "SENIOR DISCOUNT",
                                "MIL-CIV-IL-DSCNT-10%") and reports MSC's
                                own recalculated price for the booking's
                                own category. <label> must match the real
                                on-page option text exactly (see
                                evaluate_msc_booking's own
                                today_discount_options for what a given
                                booking actually offers today).
  test_voyagers_discount:<id> - inserts the FIRST passenger on <id> who
                                already has a real MSC Voyagers Club
                                membership (name/DOB/card number all read
                                from this booking's own scraped passenger
                                data — never typed manually, never
                                invented) and reports MSC's own
                                recalculated price. Returns
                                INSUFFICIENT_DATA if no passenger on this
                                booking has a Voyagers membership.

  REMOVED 2026-08-14: the manual single-tab (stage_booking/
  harvest_staged_booking) and legacy multi-tab (open_batch_tabs/
  harvest_batch_tabs) flows are gone. Both required Neon to manually click
  "CONFIRM AND PROCEED" between staging and harvesting each booking — a
  human-in-the-loop "watch, then review" step that check_booking/
  check_booking_batch below (the fully automated lookup -> stage -> confirm
  -> harvest -> evaluate flow, zero clicks needed) has fully superseded
  since 2026-08-11. Confirmed by real, repeated incidents: opening MORE
  THAN ONE tab against this WCS backend — even just two — can trigger a
  genuine server-side session/cookie conflict (_ERR_INVALID_COOKIE) that
  silently corrupts results (one tab showed a completely unrelated sailing
  with a different passenger's data, no visible error) — check_booking_batch2
  is the only supported way to run 2 tabs now, with the sailing-identity
  fingerprint check described there as its mitigation.

  close_tabs:<idx1,idx2,...>  - closes the given tab indices (opened via
                                new_tab), resyncing state["page"] to a
                                remaining tab if the active one was among
                                them.
"""

import asyncio
import importlib
import json
import os
import re
import time
from datetime import datetime

import dateparser
import keyring
from price_parser import Price
from rapidfuzz import fuzz

from config.settings import settings
from utils.logging import get_logger, track_background_task

logger = get_logger(__name__)

# Alias kept so existing references to this name elsewhere in the file
# don't need to change — value now comes from config/settings.py (single
# source of truth, consolidated 2026-08-11) instead of being hardcoded
# here AND separately in msc_save_credentials.py/msc_clear_credentials.py.
MSC_CREDENTIAL_SERVICE = settings.msc_credential_service


async def auto_login(page) -> str:
    """Log into MSC Book using credentials saved via
    msc_save_credentials.py (Windows Credential Manager, DPAPI-encrypted
    — see that script's docstring). Never raises: a failed auto-login
    returns a status string instead, so the caller can fall back to
    asking Neon to log in manually rather than the whole controller
    crashing on startup.

    Real login form structure, confirmed 2026-08-10 by inspecting a
    fresh unauthenticated session: mscbook.com/us/home shows a header
    'LOG IN' button that opens a modal containing a <form> with
    input[name="username"], input[name="password"], and a
    button[type="submit"] whose text is ALSO 'LOG IN' — scoped lookup
    via the form containing the password field is required since two
    'LOG IN' buttons exist simultaneously once the modal is open (the
    original header trigger stays in the DOM)."""
    username = keyring.get_password(MSC_CREDENTIAL_SERVICE, "username")
    password = keyring.get_password(MSC_CREDENTIAL_SERVICE, "password")
    if not username or not password:
        return "NO_CREDENTIALS_SAVED"

    try:
        await page.goto(settings.msc_home_url, wait_until="domcontentloaded")
        try:
            await page.click("#didomi-notice-agree-button", timeout=3000)
        except Exception:
            pass  # cookie banner not always present

        await page.get_by_role("button", name="LOG IN", exact=True).first.click(timeout=10000)
        await page.wait_for_selector('input[name="password"]', timeout=10000)
        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)

        clicked = await page.evaluate(
            "(() => { const pw = document.querySelector('input[name=\"password\"]'); "
            "const form = pw && pw.closest('form'); "
            "const btn = form && Array.from(form.querySelectorAll('button'))"
            ".find(b => b.textContent.trim().toUpperCase() === 'LOG IN'); "
            "if (btn) { btn.click(); return true; } return false; })()"
        )
        if not clicked:
            return "SUBMIT_BUTTON_NOT_FOUND"

        for _ in range(20):
            body = await page.inner_text("body")
            if "SIGN OUT" in body:
                return "OK"
            if "invalid" in body.lower() and ("password" in body.lower() or "username" in body.lower() or "credential" in body.lower()):
                return "INVALID_CREDENTIALS"
            await page.wait_for_timeout(500)
        return "TIMEOUT_WAITING_FOR_LOGIN"
    except Exception as e:
        return f"ERROR: {e}"

SCREENSHOT_DIR = "data/msc_control/screenshots"
BOOKING_DATA_PATH = "data/msc_control/booking_data.jsonl"


_SESSION_ERROR_MARKERS = (
    "Your logon ID may have been used in another location",
    "Generic Error",
    "Session Timed Out",
)


async def _read_booking_status_badge(page):
    """ADDED 2026-08-12, per Neon's direct instruction after confirming
    the real markup on a cancelled booking:
        <div class="BookingStatus StatusConfirmed">
            <span class="text-uppercase">Canceled</span>
        </div>
    IMPORTANT: the CSS class is literally 'StatusConfirmed' even on a
    CANCELED booking — MSC's own class name is actively misleading and
    must never be trusted. Only the visible TEXT inside the div is
    reliable. This queries that div directly (not the flattened
    page-text regex `_is_explicitly_cancelled` already uses) as a second,
    structurally independent signal — defense in depth, since the text-
    position regex depends on an assumption about line ordering that
    could break if MSC ever reflows the page. Returns the trimmed,
    uppercased status text ('CANCELED', 'CONFIRMED', etc.) or None if
    the element isn't found."""
    try:
        text = await page.evaluate(
            "(() => { const el = document.querySelector('.BookingStatus'); "
            "return el ? el.textContent.trim() : null; })()"
        )
        return text.strip().upper() if text else None
    except Exception:
        return None


async def _lookup_one_booking(page, booking_id: str) -> dict:
    """Read-only: open a booking, capture its summary text and (if
    reachable) its itemized Price Breakdown. Never touches Categories/
    cabin selection/discounts — purely a lookup, safe to loop over many
    bookings unattended.

    CONFIRMED REAL BUG, 2026-08-11: this function had NO session-expiry
    detection at all — a real session kick mid-batch ("Generic Error...
    Your logon ID may have been used in another location") landed on the
    SAME URL (no redirect to /welcome or /login, so the existing cold-
    navigation-redirect check never fired) and neither "Booking Value"
    nor "No bookings found" ever appears on that error page, so
    _wait_for_content's poll just timed out and returned the error page
    text as if it were a normal capture. That got written straight into
    booking_data.jsonl as this booking's "latest" record — corrupting a
    real booking's data with garbage (confirmed on 3000013, whose
    correct $3,790.51/$0.00-due data got overwritten this way) and
    silently regressing every later calculator run for it until noticed.
    Returns {"session_expired": True, ...} instead of a normal-looking
    record when one of _SESSION_ERROR_MARKERS is detected, so callers
    can retry after relogin rather than trusting/persisting it."""
    # Single source of truth for this URL is config/settings.py (consolidated
    # 2026-08-11 — was hardcoded identically in three places here before).
    url = settings.msc_booking_search_url.format(booking_id=booking_id)
    async def _wait_for_content():
        # Fixed sleeps were occasionally not enough (one lookup came back
        # with just the nav header, no booking content, on a slow page
        # load) — poll for the real content marker instead, with a fixed
        # sleep only as a final fallback.
        #
        # CONFIRMED REAL BUG, 2026-08-11: requiring "Booking Value" alone
        # was NOT sufficient — the Ship/Area/Itinerary/Departure-Arrival/
        # Duration block renders on its own separate timing and can still
        # be missing even once "Booking Value" is already present
        # (confirmed real capture, booking 3000024: jumped straight from
        # "Add Cruise" to "Cabin 1" details, skipping the whole itinerary
        # section entirely). This silently broke duration-dependent logic
        # (_extract_duration_nights, needed for the implied-discount SRN
        # check) without ever looking like an obvious failure — the
        # capture still had "Booking Value" and looked superficially
        # complete. Require "Duration" too now.
        for _ in range(10):
            body = await page.inner_text("body")
            if (
                ("Booking Value" in body and "Duration" in body)
                or "No bookings found" in body
                or any(m in body for m in _SESSION_ERROR_MARKERS)
            ):
                return body
            await page.wait_for_timeout(500)
        return await page.inner_text("body")

    resp = await page.goto(url, wait_until="domcontentloaded")
    summary_text = await _wait_for_content()
    if "welcome" in page.url or "login" in page.url.lower():
        # cold-navigation redirect seen before — one retry resolves it
        resp = await page.goto(url, wait_until="domcontentloaded")
        summary_text = await _wait_for_content()

    # CONFIRMED REAL BUG, 2026-08-12: a genuinely dead/logged-out session
    # lands here too (silently redirected to /us/welcome, showing the
    # "Log in to your account" modal) — that page contains NONE of
    # _SESSION_ERROR_MARKERS' text, so this was falling through
    # undetected and being treated as a normal (empty/garbage) capture
    # instead of triggering the auto-relogin retry in _check_booking_msc.
    # Real symptom: a batch of 6 bookings all came back
    # "confirm_button_not_found" because the session had already died
    # before the batch started and nothing ever caught it. Checking the
    # URL after the retry (not just the page text) closes this gap.
    if (
        any(m in summary_text for m in _SESSION_ERROR_MARKERS)
        or "welcome" in page.url
        or "login" in page.url.lower()
    ):
        return {"booking_id": booking_id, "captured_at": datetime.now().isoformat(), "session_expired": True}

    breakdown_text = None
    try:
        clicked = await page.evaluate(
            "(() => { const el = Array.from(document.querySelectorAll('a'))"
            ".find(e => e.textContent.includes('Price Breakdown')); "
            "if (el) { el.click(); return true; } return false; })()"
        )
        if clicked:
            # CONFIRMED REAL BUG, first automated batch run 2026-08-11: a
            # fixed 1500ms wait was fine for years of manual, human-paced
            # driving but was NOT always enough once check_booking_batch
            # started firing these back-to-back with no natural pauses —
            # one real capture (booking 3000016) grabbed the PLAIN
            # booking detail page instead of the modal, which
            # _extract_discounts then silently read as "zero discounts",
            # producing a dangerous false positive (recommending a
            # discount that was ALREADY applied). Poll for the modal's
            # own real content marker instead of trusting a fixed delay.
            candidate = None
            marker_seen = False
            # 10x400ms (4s) missed ~40% of real captures in live testing
            # 2026-08-11 (safely reported as INSUFFICIENT_DATA rather than
            # wrong, but losing real coverage) — widened for better hit
            # rate; still bounded, never blocks indefinitely.
            for _ in range(18):
                candidate = await page.inner_text("body")
                if "Total Stateroom Price" in candidate:
                    marker_seen = True
                    break
                await page.wait_for_timeout(500)
            if marker_seen:
                # CONFIRMED REAL BUG, same session, TWICE: "Total
                # Stateroom Price" (the modal's header total) renders
                # BEFORE the per-passenger itemized lines further down —
                # including the "Discount Description:"/"MSC Club
                # Discount:" lines _extract_discounts() actually needs.
                # A single fixed settle delay after the header (tried:
                # 1000ms) was STILL flaky — 1 of 3 repeated checks on the
                # same real booking (3000016, confirmed to genuinely
                # have SPECIAL OFFER 15% + MSCCLUB5 both applied) still
                # came back with zero discounts parsed, because render
                # time for the rest of the modal isn't constant. Poll
                # until the body text stops changing between reads
                # (content has genuinely settled) instead of guessing a
                # fixed number — the general-purpose fix for "an element
                # rendered but its content is still filling in".
                previous = candidate
                for _ in range(10):
                    await page.wait_for_timeout(400)
                    candidate = await page.inner_text("body")
                    if candidate == previous:
                        break
                    previous = candidate
            # Only trust the capture if the real marker actually showed
            # up — otherwise None (NOT the wrong page's text) so callers
            # can tell "didn't confirm this" apart from "confirmed empty".
            breakdown_text = candidate if marker_seen else None
    except Exception as e:
        # CONFIRMED REAL BUG, fixed 2026-08-13 (Phase 0 correctness audit):
        # this used to set breakdown_text to a non-empty error STRING —
        # _extract_discounts (see its own docstring) treats None as
        # "unknown, couldn't confirm" but treats any other falsy-empty
        # value as "confirmed empty, genuinely no discount", and this
        # error string is neither empty nor None, so it would get
        # regex-scanned, match nothing, and be silently treated as
        # "confirmed empty" — exactly the false "no discount, add one"
        # bug class this project already had one real incident from
        # (booking 3000016). Must stay None on any failure here.
        logger.warning("msc.breakdown_read_failed", error=str(e))
        breakdown_text = None

    passenger_info = _extract_passengers(summary_text)
    # Structural, DOM-based confirmation of booking status — see
    # _read_booking_status_badge's docstring for why the CSS class alone
    # ('StatusConfirmed', even when canceled) can't be trusted and only
    # the visible text is reliable. Combined with the text-position
    # regex below as two independent signals, not a replacement for it.
    status_badge = await _read_booking_status_badge(page)
    return {
        "booking_id": booking_id,
        "captured_at": datetime.now().isoformat(),
        "final_url": page.url,
        "status_code": resp.status if resp else None,
        "summary_text": summary_text[:4000],
        "breakdown_text": breakdown_text[:4000] if breakdown_text else None,
        "status_badge": status_badge,
        # A departure date in a placeholder far-future year (e.g. 2049,
        # 2050) means this sailing is 100% cancelled/postponed, regardless
        # of what the booking status field says — confirmed 2026-08-10.
        "cancelled_or_postponed_placeholder": _is_placeholder_departure(summary_text),
        # A DIFFERENT real cancellation shape, confirmed 2026-08-12
        # (booking 3000012): a plain outright cancellation with a
        # perfectly normal departure date, marked by the literal
        # "CANCELED" status word and a "REINSTATE BOOKING" button
        # instead of the far-future placeholder trick above. Checks BOTH
        # the flattened-text regex AND the structural status_badge query
        # — either one alone catching it is enough.
        "explicitly_cancelled": _is_explicitly_cancelled(summary_text) or bool(status_badge and "CANCEL" in status_badge),
        # Auto-detected from Passenger Details instead of relying on Neon
        # stating ages in chat every time — senior discount requires AT
        # LEAST TWO passengers 65+ in the cabin (see senior_count; corrected
        # 2026-08-18, all_seniors alone is not the real eligibility rule —
        # see _extract_passengers's docstring).
        "passengers": passenger_info["passengers"],
        "all_seniors": passenger_info["all_seniors"],
        "senior_count": passenger_info["senior_count"],
        "has_voyagers": passenger_info["has_voyagers"],
        # Structured discount(s) already applied to this booking — both
        # explicitly disclosed (Price Breakdown text) AND inferred via
        # SRN math for the silent discounts (senior, Voyagers Exclusive)
        # that never print a disclosure line at all. See
        # _extract_discounts_with_implied's docstring.
        "current_discounts": _extract_discounts_with_implied(summary_text, breakdown_text),
    }


RATE_CHECK_DATA_PATH = "data/msc_control/rate_check_data.jsonl"


async def _check_today_rate(page, booking_id: str) -> dict:
    """Read-only: open a booking, click 'Book Same Departure' to reach
    the dummy/practice booking's category listing for the SAME sailing,
    and capture (a) the full listing text (so the matching category's
    today-price can be found by comparing against the booking's own
    current category) and (b) the list of discount options currently
    offered, WITHOUT selecting any of them. Never proceeds past this
    listing page — never clicks Confirm/Continue, never enters guest
    info, never applies a discount. Nothing here can create a real
    booking or lock a real cabin."""
    # Single source of truth for this URL is config/settings.py (consolidated
    # 2026-08-11 — was hardcoded identically in three places here before).
    url = settings.msc_booking_search_url.format(booking_id=booking_id)

    async def _wait_for(marker_a, marker_b=None):
        for _ in range(10):
            body = await page.inner_text("body")
            if marker_a in body or (marker_b and marker_b in body):
                return body
            await page.wait_for_timeout(500)
        return await page.inner_text("body")

    await page.goto(url, wait_until="domcontentloaded")
    booking_text = await _wait_for("Booking Value", "No bookings found")
    if "welcome" in page.url or "login" in page.url.lower():
        await page.goto(url, wait_until="domcontentloaded")
        booking_text = await _wait_for("Booking Value", "No bookings found")

    if "No bookings found" in booking_text:
        return {
            "booking_id": booking_id,
            "captured_at": datetime.now().isoformat(),
            "found": False,
            "booking_text": None,
            "listing_text": None,
            "discount_options": None,
        }

    clicked = await page.evaluate(
        "(() => { const el = Array.from(document.querySelectorAll('button,a'))"
        ".find(e => e.textContent.trim() === 'Book Same Departure'); "
        "if (el) { el.click(); return true; } return false; })()"
    )
    listing_text = None
    discount_options = None
    if clicked:
        # Poll for the real listing content instead of a fixed sleep —
        # same lesson as _lookup_one_booking: fixed timeouts weren't
        # always enough, and a too-early capture silently returns the
        # PREVIOUS page's content (the original booking), not an error,
        # so this failure mode is easy to miss without checking for it.
        listing_text = await _wait_for("Select Special Discounts", "Additional Discounts")
        try:
            discount_options = await page.evaluate(
                "(() => { const sel = Array.from(document.querySelectorAll('select'))"
                ".find(s => Array.from(s.options).some(o => /DISCOUNT|TODAY|MIL-CIV/.test(o.textContent))); "
                "return sel ? Array.from(sel.options).map(o => o.textContent.trim()).filter(Boolean) : null; })()"
            )
        except Exception as e:
            # CONFIRMED REAL BUG, fixed 2026-08-13 (Phase 0 correctness
            # audit): same defect shape as _lookup_one_booking's
            # breakdown_text fix above — discount_options is expected to
            # be a list of strings or None (see evaluate_msc_booking's
            # docstring: "pass None (not []) when this wasn't captured, so
            # it's distinguishable from 'captured and genuinely empty'").
            # An error STRING here is neither — it doesn't crash, but it
            # is truthy and non-list, which downstream code was never
            # designed to receive. Must stay None on any failure here,
            # matching the equivalent, already-correct except block in
            # _stage_booking_for_confirm.
            logger.warning("msc.discount_options_read_failed", error=str(e))
            discount_options = None

    listing_confirmed = bool(listing_text) and (
        "Select Special Discounts" in listing_text or "Additional Discounts" in listing_text
    )

    return {
        "booking_id": booking_id,
        "captured_at": datetime.now().isoformat(),
        "found": True,
        "booking_text": booking_text[:3000],
        "clicked_book_same_departure": clicked,
        "listing_text": listing_text[:4000] if listing_text else None,
        # False here means the listing page never actually loaded in time —
        # listing_text is stale (the previous page), don't trust it.
        "listing_confirmed": listing_confirmed,
        # The occupancy cross-check result (see msc_occupancy_is_trustworthy).
        # Recorded so the run itself shows whether today's quote covered the
        # same guests as the booking - the exact thing that was invisible
        # when booking 3000081 was priced as 1 guest instead of 2.
        # The invoice's OWN passenger count, read from the booking page this
        # function already opened. `staged` does not exist in this scope -
        # referencing it here was a NameError on every console check-rates
        # run (my bug, 2026-09-01).
        "invoice_guest_count": msc_invoice_guest_count(booking_text, None),
        "discount_options": discount_options,
    }


NETWORK_CAPTURE_PATH = "data/msc_control/network_capture.jsonl"
# Written live by _check_booking_msc (the fully-automated single-call
# flow) as each booking is checked, distinct from
# data/msc_control/calculator_results.jsonl which msc_run_calculator.py
# owns exclusively (it fully rebuilds that file from scratch every run
# by re-reading booking_data.jsonl + rate_check_data.jsonl) — kept
# separate so the two writers never race or overwrite each other.
LIVE_CHECK_RESULTS_PATH = "data/msc_control/live_check_results.jsonl"


def _extract_booking_essentials(text: str) -> dict:
    """Pull the current Booking Value and cabin category code out of a
    booking's summary text. Category regex is anchored to the actual
    'Cabin N - N... (CODE)' line — an earlier, looser regex matched
    unrelated parenthesized codes elsewhere on the page (e.g. a travel
    notice) and silently gave wrong categories.

    "Guaranteed Cabin" bookings use a completely different line format —
    no cabin number, no parenthetical code, e.g. 'Cabin  1 - Guaranteed
    Cabin INT' — confirmed on bookings 3000020/74173329, where the first
    regex returned None for both. Falls back to a second pattern for
    that format.

    ADDED 2026-08-11, real gap found reviewing booking 3000013: this
    function never extracted "Due Amount" at all, so the automated
    pipeline's is_paid_in_full was always hardcoded False even on a
    booking confirmed genuinely paid in full ($0.00 due) — which meant a
    real, large PRICE_MATCH finding ($1292.51) on that exact booking
    never got flagged as "this booking is already paid in full, so
    price-matching this would very likely be a real refund, not just a
    future-payment reduction" (the exact refund-framing rule already
    established for DISCOUNT_ADD, which applies equally here).

    UPGRADED 2026-08-24: the dollar amounts below used to require an
    EXACT "\\d+\\.\\d{2}" shape and silently returned no match at all on
    any other real, valid format — the same bug CLASS already confirmed
    once on GoCCL (a whole-dollar "$1,418" with no cents broke a regex
    demanding exactly 2 decimal digits). Verified against this project's
    own real captured strings before adopting: price-parser's
    Price.fromstring() correctly handles whole-dollar amounts, a stray
    space after "$", and a "USD $X" prefix, none of which the old regex
    would have matched. It does NOT preserve a negative/parenthetical
    sign on its own (Price(amount=Decimal('50.00')) for both "-$50.00"
    and "($50.00)") — that detection stays a separate check on the raw
    captured line, same as before, now also recognizing a parenthetical
    negative (a common real-world credit convention) alongside the
    existing leading-minus check."""
    m_val_line = re.search(r"Booking Value\n(.+)", text or "")
    value_price = Price.fromstring(m_val_line.group(1)) if m_val_line else None
    value_str = f"{value_price.amount:.2f}" if value_price and value_price.amount is not None else None

    m_due_line = re.search(r"Due Amount\n(.+)", text or "")
    due_raw = m_due_line.group(1) if m_due_line else None
    due_price = Price.fromstring(due_raw) if due_raw else None
    due_str = f"{due_price.amount:.2f}" if due_price and due_price.amount is not None else None
    # Sign detection has to happen on the RAW captured text — price-parser
    # gives magnitude only, by design (confirmed via direct test: both
    # "-$50.00" and "($50.00)" parse to amount=Decimal('50.00')).
    is_negative_due = bool(due_raw and ("-" in due_raw or "(" in due_raw))
    # ADDED 2026-08-12, direct instruction from Neon: MSC can show
    # "Overpayment" instead of "Due Amount" when a client has paid more
    # than the booking's current total (e.g. after a price drop already
    # applied) — that's paid-in-full and then some, not just "due amount
    # not found." Also treats a negative "Due Amount" figure (e.g.
    # "-$50.00" or "($50.00)", either sign/dollar order) as the same
    # signal, in case MSC ever renders it that way instead of swapping
    # the label. Neither of these has been seen live yet — built
    # defensively per Neon's instruction, to be confirmed against a real
    # example the next time one turns up.
    is_overpayment = bool(re.search(r"\bOverpayment\b", text or "", re.IGNORECASE)) or is_negative_due
    # Real captured shape (booking 3000021) has a newline between the
    # label and the amount, same as "Due Amount"/"Booking Value" — [^\d\-]
    # (not [^\n]) is deliberate so the skip crosses that newline instead
    # of stopping at it. Capture widened to accept a whole-dollar amount
    # too (was "\.\d{2}" exactly), same fragility class fixed everywhere
    # else in this function.
    m_overpay = re.search(r"Overpayment[^\d\-]{0,20}(-?\$?[\d,]+(?:\.\d+)?)", text or "", re.IGNORECASE)
    overpay_price = Price.fromstring(m_overpay.group(1)) if m_overpay else None
    overpayment_amount = f"{overpay_price.amount:.2f}" if overpay_price and overpay_price.amount is not None else None
    # The booking's own rate/promo program, e.g. "Price :\nFLASH SALE
    # DRINKS AND WIFI" — needed to click the MATCHING tab on a fresh
    # listing (see _match_rate_tab). Confirmed real gap 2026-08-10: the
    # listing's DEFAULT-active tab is often a DIFFERENT rate than the
    # booking's own, and comparing across tabs silently produces a wildly
    # wrong price (one real case looked like a $654 gap that was actually
    # $26 once matched to the right tab).
    m_rate = re.search(r"Price\s*:\n(.+)", text or "")
    rate_name = m_rate.group(1).strip() if m_rate else None
    final_payment_date_passed = _final_payment_date_passed(text)
    m_cat = re.search(r"Cabin\s+\d+\s*-\s*N.\d+[^\n(]+\(([A-Z0-9]+)\)", text or "")
    if m_cat:
        return {
            "value": value_str,
            "due_amount": due_str,
            "is_overpayment": is_overpayment,
            "overpayment_amount": overpayment_amount,
            "category": m_cat.group(1),
            "is_guaranteed": False,
            "rate_name": rate_name,
            "final_payment_date_passed": final_payment_date_passed,
        }
    # "Guaranteed Cabin" bookings have no cabin number or parenthetical
    # code at all — e.g. 'Cabin  1 - Guaranteed Cabin INTERIOR' — so this
    # captures the category TYPE NAME instead. That name doesn't appear
    # literally in a fresh listing (the listing shows 'INTERIOR BELLA
    # (IB)' / 'Guaranteed stateroom', not the bare word), so downstream
    # matching needs a different strategy — see is_guaranteed flag.
    m_cat = re.search(r"Cabin\s+\d+\s*-\s*Guaranteed Cabin\s+([A-Z]+)", text or "")
    return {
        "value": value_str,
        "due_amount": due_str,
        "is_overpayment": is_overpayment,
        "overpayment_amount": overpayment_amount,
        "category": m_cat.group(1) if m_cat else None,
        "is_guaranteed": bool(m_cat),
        "rate_name": rate_name,
        "final_payment_date_passed": final_payment_date_passed,
    }


_FINAL_PAYMENT_DATE_LINE_RE = re.compile(r"Final Payment Date\n(.+)")


def _final_payment_date_passed(text: str) -> bool | None:
    """Whether this booking's own Final Payment Date has already passed,
    parsed directly from its Reservation Summary text. Returns None (not
    False) when the field isn't present/parseable — e.g. a booking with no
    "Final Payment Date" line at all — never assume "not passed" from
    missing data; callers should only apply this as a gate when it comes
    back True, same discipline as every other MSC signal in this file.

    CONFIRMED REAL RULE, Neon 2026-08-24 (bookings 3000031/3000018/
    3000017, all with a final payment date days-to-months in the past):
    MSC — like most cruise lines — will informally price-match/reprice a
    fare drop before final payment is due, but once that date passes the
    fare is locked in. A booking's PRICE_MATCH check must never report an
    opportunity once this date is behind us, the same way it already never
    does for a paid-in-full booking (is_paid_in_full and a passed final
    payment date are related but distinct signals — a booking can be past
    its final payment date without being fully paid off yet).

    UPGRADED 2026-08-24, same day this was written: the original regex
    required an exact "\\d{2}/\\d{2}/\\d{4}" shape and would have silently
    returned None (not even a wrong date — no match at all) on a real,
    valid single-digit month/day like "8/9/2026" — the exact fragility
    class already confirmed once on GoCCL's price regex. dateparser
    handles that plus month-name formats without needing to keep
    expanding a hand-rolled pattern by hand every time a new variant
    turns up. DATE_ORDER is pinned to MDY explicitly (never left to
    system-locale defaults) since MSC's own format is confirmed US-style
    month/day/year — verified this resolves a genuinely ambiguous date
    like "03/04/2026" to March 4th, not April 3rd."""
    m = _FINAL_PAYMENT_DATE_LINE_RE.search(text or "")
    if not m:
        return None
    final_payment_date = dateparser.parse(m.group(1).strip(), settings={"DATE_ORDER": "MDY"})
    if final_payment_date is None:
        return None
    return datetime.now() >= final_payment_date


# Confirmed 2026-08-10, directly stated by Neon: MSC rebooks cancelled/
# postponed sailings onto an absurd far-future placeholder departure date
# (seen: 01/2049, and Neon separately flagged 09/05/2050 as another real
# example) rather than marking the booking cancelled outright. ANY booking
# with a departure date this far out is 100% certain to be a cancelled or
# postponed sailing, not a real bookable cruise — check this FIRST, before
# ever clicking "Book Same Departure", so these get flagged instantly
# instead of discovered only after wasting a click/tab on them.
PLACEHOLDER_YEAR_THRESHOLD = 2045


def _extract_departure_year(text: str):
    m = re.search(r"Departure-\s*Arrival:\s*\n?\s*\d{2}/\d{2}/(\d{4})", text or "")
    return int(m.group(1)) if m else None


def _is_placeholder_departure(text: str) -> bool:
    year = _extract_departure_year(text)
    return bool(year) and year >= PLACEHOLDER_YEAR_THRESHOLD


# ADDED 2026-08-12, real miss caught by Neon: booking 3000012 has a
# perfectly normal departure date (09/21/2026 — nowhere near the far-
# future placeholder threshold above), so _is_placeholder_departure
# never flags it, but the booking IS genuinely cancelled — MSC just
# shows this a completely different way for a plain outright
# cancellation than for the "rebooked to a 2049 placeholder" FCC case.
# Real, confirmed markers on this exact booking's page: the literal
# status word "CANCELED" printed right under the booking number (before
# "Booking Value"), Booking Value/Due Amount/Amount Paid all $0.00, and
# a "REINSTATE BOOKING" action button in place of the normal "CANCEL
# BOOKING" one. The whole check_booking pipeline ran anyway on this
# booking and produced a nonsense "opportunity" from the garbage $0.00
# data — a real, actionable false positive, not just a cosmetic miss.
# Checking the literal status word is the most direct signal; the
# REINSTATE button is a second, independent confirmation in case MSC
# ever renders the status word differently.
def _is_explicitly_cancelled(text: str) -> bool:
    text = text or ""
    m = re.search(r"\n(\d{7,9})\n([A-Z][A-Z ]+)\n", text)
    if m and "CANCEL" in m.group(2):
        return True
    return "REINSTATE BOOKING" in text


_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_CLUB_RE = re.compile(r"^MSC Voyagers Club:\s*(\d+)\s*-\s*(.+)$")


def _extract_passengers(text: str) -> dict:
    """Parse the 'Passenger Details' section into structured per-passenger
    data (name, DOB, Voyagers Club number/tier if present), so senior-
    discount eligibility (ALL passengers 65+, confirmed rule) and Voyagers
    presence can be read automatically instead of Neon stating it in
    chat every time. Confirmed real format across 15+ bookings:
    'Passenger Details\\nNAME\\nMM/DD/YYYY\\n[MSC Voyagers Club: NUM -
    TIER\\n]...Additional Items' — the Voyagers line is optional and only
    appears for passengers who have a membership."""
    section_match = re.search(r"Passenger Details\n(.*?)(?:\nAdditional Items|$)", text or "", re.DOTALL)
    if not section_match:
        return {"passengers": [], "all_seniors": False, "senior_count": 0, "has_voyagers": False}

    lines = [l.strip() for l in section_match.group(1).split("\n") if l.strip()]
    passengers = []
    i = 0
    while i < len(lines):
        name = lines[i]
        if i + 1 < len(lines) and _DATE_RE.match(lines[i + 1]):
            dob = lines[i + 1]
            i += 2
            voyagers_number, voyagers_tier = None, None
            if i < len(lines):
                m = _CLUB_RE.match(lines[i])
                if m:
                    voyagers_number, voyagers_tier = m.group(1), m.group(2)
                    i += 1
            month, day, year = (int(x) for x in dob.split("/"))
            today = datetime.now()
            age = today.year - year - ((today.month, today.day) < (month, day))
            passengers.append({
                "name": name,
                "dob": dob,
                "age": age,
                "voyagers_number": voyagers_number,
                "voyagers_tier": voyagers_tier,
            })
        else:
            i += 1  # unrecognized line — skip rather than misparse

    return {
        "passengers": passengers,
        # CONFIRMED WRONG RULE, corrected 2026-08-18 by Neon (booking
        # 3000030 false positive): "all_seniors" (every passenger 65+)
        # was never the real senior-discount eligibility test and is kept
        # only as a raw descriptive fact now — do not use it to decide
        # senior-discount eligibility. The real rule, stated directly:
        # senior discount requires AT LEAST TWO passengers 65+ in the
        # cabin. A single senior traveling alone does NOT qualify, even
        # though "all passengers are seniors" is trivially true for them.
        # Non-senior passengers alongside 2+ seniors (e.g. grandchildren)
        # do NOT disqualify it — only the senior COUNT matters. See
        # senior_count below and core/calculator_msc.py's
        # _filter_out_ineligible_senior_discount, the actual eligibility
        # gate.
        "all_seniors": bool(passengers) and all(p["age"] >= 65 for p in passengers),
        "senior_count": sum(1 for p in passengers if p["age"] >= 65),
        "has_voyagers": any(p["voyagers_number"] for p in passengers),
    }


def _compute_required_occupancy(passengers: list) -> dict:
    """MSC's occupancy screen prices four independent age tiers (Adult
    18+, Child 12-17, Kids 2-11, Infant 0-1). 'Book Same Departure'
    auto-fills only the ADULT count from the real booking — confirmed
    real bug 2026-08-12, booking 3000024 (2 adults + 3 kids ages
    6/8/10): the dummy landed on Adult=2/Child=0/Kids=0/Infant=0,
    silently dropping all 3 kids, so the resulting today-price was a
    2-guest quote compared against the real booking's 5-guest total —
    looked like a genuine $1,929.61 price-match opportunity that was
    actually just 3 missing passengers. Neon's rule: always compute and
    enforce the real per-tier counts before trusting any price.

    Returns both the per-tier counts AND the sorted list of real ages
    in the child/jrchild/infant tiers — MSC requires each Child(12-17)/
    Kids(2-11)/Infant(0-1) slot's exact age to be selected individually
    (a second requirement discovered right after fixing the count
    alone; a correct COUNT still isn't enough to get a real price).
    CONFIRMED REAL BUG 2026-08-12, booking 3000011: the infant tier
    needs this too (its own `#age-{cabin}-infant-{index}` select,
    options '0'/'1') — initially only child/jrchild were wired up,
    which silently left an infant-carrying booking stuck on the
    occupancy screen the exact same way the child/jrchild gap did
    before that was fixed."""
    counts = {"adult": 0, "child": 0, "jrchild": 0, "infant": 0}
    ages = {"child": [], "jrchild": [], "infant": []}
    # A passenger with no readable age used to be `continue`d SILENTLY, so a
    # 2-guest booking could be priced as 1 guest with nothing recorded. Now
    # counted and returned - see the `dropped` key and
    # msc_occupancy_is_trustworthy().
    dropped = 0
    for p in passengers:
        age = p.get("age")
        if age is None:
            dropped += 1
            continue
        if age >= 18:
            counts["adult"] += 1
        elif age >= 12:
            counts["child"] += 1
            ages["child"].append(age)
        elif age >= 2:
            counts["jrchild"] += 1
            ages["jrchild"].append(age)
        else:
            counts["infant"] += 1
            ages["infant"].append(age)
    ages["child"].sort()
    ages["jrchild"].sort()
    ages["infant"].sort()
    return {
        "counts": counts,
        "ages": ages,
        # How many passengers had no readable age (silently skipped before).
        "dropped": dropped,
        # Total guests this occupancy actually represents, for the
        # cross-check against the invoice's own passenger count.
        "total_guests": sum(counts.values()),
        "passengers_seen": len(passengers),
    }


_PASSENGER_COUNT_RE = re.compile(r"Passengers\s*:?\s*\n\s*(\d+)", re.I)


def msc_invoice_guest_count(summary_text: str | None,
                            breakdown_text: str | None) -> int | None:
    """The guest count as the INVOICE ITSELF states it, independent of
    passenger-row extraction.

    Two independent readings, both confirmed present on real captured
    pages for booking 3000081:
      * summary_text  -> "Passengers :\n2"
      * breakdown_text -> one "Total Adult N" line per guest

    Returns None when neither can be read - the caller must then treat the
    occupancy as unverified rather than assume agreement.
    """
    from_summary = None
    if summary_text:
        m = _PASSENGER_COUNT_RE.search(summary_text)
        if m:
            try:
                from_summary = int(m.group(1))
            except ValueError:
                from_summary = None
    from_breakdown = None
    if breakdown_text:
        n = breakdown_text.count("Total Adult")
        if n > 0:
            from_breakdown = n
    # Prefer agreement; fall back to whichever exists.
    if from_summary and from_breakdown:
        return from_summary if from_summary == from_breakdown else max(
            from_summary, from_breakdown)
    return from_summary or from_breakdown


def msc_occupancy_is_trustworthy(required: dict,
                                 invoice_guests: int | None,
                                 occupancy_fix: dict | None = None,
                                 cabin_count: int | None = None) -> tuple[bool, str]:
    """Whether an occupancy may be used to price a booking.

    THE ANTI-BLIND-SPOT RULE, added 2026-09-01. Counting guests from a
    single source (the passenger rows) has now produced a wrong price twice:
    booking 3000024 (3 kids dropped -> fake $1,929.61) and booking
    3000081 (1 of 2 adults dropped -> fake $267.01, real answer $81.98).
    Both times the count was internally consistent and simply wrong.

    So the count is cross-checked against a figure the invoice states in
    its own words. If they disagree, or the invoice count cannot be read,
    the price is NOT trustworthy and the caller must report
    INSUFFICIENT_DATA instead of a number. Refusing to answer is the only
    safe failure mode here: a fabricated opportunity gets acted on.
    """
    total = required.get("total_guests")
    if total is None:
        total = sum((required.get("counts") or {}).values())
    # ZERO guests is never a valid basis for a price. It means passenger
    # extraction returned nothing at all - which really happened on booking
    # 3000081 at 2026-08-12T10:15:26 (extracted 0 while the invoice said 2).
    if not total:
        return False, (
            "no guests were resolved for this booking, so there is nothing "
            "to price against"
        )
    dropped = required.get("dropped") or 0
    if dropped:
        return False, (
            f"{dropped} passenger(s) had no readable age and were skipped, so "
            f"the occupancy ({total} guest(s)) may be short"
        )
    # MSC'S OWN PRE-FILL is the second source that actually exists, found
    # 2026-09-01 while checking why invoice_guest_count came back None on all
    # three of Neon's bookings. MSC arrives at the occupancy screen already
    # populated FROM THE BOOKING, captured as occupancy_fix["before"]. Across
    # every dated capture of booking 3000081 it read 2 - correct every time,
    # including on the run that produced the fake price.
    #
    # That reframes the bug. On 2026-08-24 the pre-fill said 2 and our own
    # passenger extraction said 1, so the code did not merely fail to read
    # the count - it ACTIVELY REDUCED a correct 2-guest occupancy to 1 and
    # priced that ($3,250.33 against a 2-guest total of $3,517.34 -> fake
    # $267.01; the real answer was $81.98).
    #
    # So a downward correction is the dangerous direction and is refused
    # outright. Upward is harmless: MSC pre-filling fewer guests than the
    # booking has cannot manufacture a saving.
    # MULTI-CABIN comes first, because on such a booking neither the pre-fill
    # nor the booking total means what the price path assumes. Found
    # 2026-09-01 on booking 3000071, one of the three Neon gave a real
    # answer for: its invoice carries TWO cabin rows (Cabin 1 and Cabin 2)
    # but only ONE "Total Adult" line. Every selector in the pricing flow
    # targets cabin 1 only, while `current_value` is the whole-booking total
    # across both cabins - so a today-quote for one cabin was being compared
    # against a two-cabin total. That comparison cannot be right in either
    # direction, whatever the guest count says.
    #
    # It also explains why the pre-fill read 2 on that booking: it tracks the
    # two CABIN rows, not two guests. Checking cabins before guests keeps the
    # pre-fill rule below applied only where it is meaningful.
    if cabin_count and cabin_count > 1:
        return False, (
            f"this booking has {cabin_count} cabins: the pricing flow quotes "
            f"cabin 1 only while the booking total covers all of them, so the "
            f"two figures are not comparable"
        )
    prefill = None
    if occupancy_fix:
        before = occupancy_fix.get("before") or {}
        prefill = sum(v for v in before.values() if isinstance(v, int)) or None
    if prefill and total < prefill:
        return False, (
            f"occupancy was REDUCED below MSC's own pre-fill: MSC arrived with "
            f"{prefill} guest(s) from the booking and passenger extraction "
            f"produced only {total} - this exact downward correction is what "
            f"priced booking 3000081 as 1 adult and reported a fake $267.01"
        )
    if invoice_guests is None and prefill is None:
        return False, (
            f"neither the invoice's passenger count nor MSC's own occupancy "
            f"pre-fill could be read, so the computed occupancy ({total} "
            f"guest(s)) cannot be cross-checked"
        )
    if invoice_guests is None:
        # Cross-checked against the pre-fill instead, which agrees.
        invoice_guests = total if total == prefill else prefill
    if total != invoice_guests:
        return False, (
            f"occupancy MISMATCH: priced {total} guest(s) but the invoice says "
            f"{invoice_guests} - a per-guest price compared against a "
            f"whole-booking total is exactly how booking 3000081 reported a "
            f"fake $267.01 opportunity"
        )
    # THE APPLIED STATE. On 2026-08-24 booking 3000081 passed every check
    # above (2 extracted, 2 on the invoice) and the SCREEN still ended at 1
    # adult. Intent agreeing with the invoice proves nothing about what was
    # actually priced.
    if occupancy_fix:
        applied = occupancy_fix.get("applied_guests")
        if applied is None:
            applied = sum(v for v in (occupancy_fix.get("after") or {}).values()
                          if isinstance(v, int))
        if occupancy_fix.get("stalled"):
            return False, "the occupancy fix stalled before reaching the required count"
        if applied != invoice_guests:
            return False, (
                f"the occupancy screen ended at {applied} guest(s) but the "
                f"invoice says {invoice_guests} - today's quote is for the "
                f"wrong number of people (booking 3000081, 2026-08-24)"
            )
    return True, ""


async def _read_occupancy(page) -> dict:
    """Read the occupancy screen's current per-tier guest counts
    (selectors confirmed live 2026-08-12 against booking 3000024's
    staged occupancy screen: `.occupancy-control-wrap[data-target=...]
    [data-cabin="1"] .occupancy-data`)."""
    return await page.evaluate(
        "(() => { const tiers = ['adult', 'child', 'jrchild', 'infant']; const out = {}; "
        "tiers.forEach(t => { const el = document.querySelector("
        "'.occupancy-control-wrap[data-target=\"' + t + '\"][data-cabin=\"1\"] .occupancy-data'); "
        "out[t] = el ? parseInt(el.textContent.trim(), 10) : null; }); return out; })()"
    )


async def _click_occupancy(page, tier: str, action: str) -> bool:
    clicked = await page.evaluate(
        "(() => { const wrap = document.querySelector("
        "'.occupancy-control-wrap[data-target=\"' + '%s' + '\"][data-cabin=\"1\"]'); "
        "const btn = wrap ? wrap.querySelector('.change-occupancy-btn[data-action=\"%s\"]') : null; "
        "if (btn && !btn.classList.contains('disabled')) { btn.click(); return true; } return false; })()"
        % (tier, action)
    )
    await page.wait_for_timeout(700)
    return bool(clicked)


async def _select_age(page, tier: str, cabin: int, index: int, age: int) -> bool:
    """Fill one Child(12-17)/Kids(2-11) slot's age dropdown — confirmed
    live 2026-08-12, booking 3000024: adding 3 Kids via the +/- counter
    alone (_click_occupancy) isn't enough, MSC also requires each slot's
    exact age selected via a `#age-{cabin}-{tier}-{index}` <select>
    (hidden behind custom styling, same 'needs force=True' pattern as
    the existing select_option_label command) before CONFIRM AND
    PROCEED will actually advance past this screen at all — without
    this, the click silently no-ops and leaves the page on the same
    occupancy screen, which is exactly what happened before this fix
    (rate_tab_match came back 'no rate tabs found' because we were
    still looking at the discount/occupancy screen, not a listing)."""
    sel_id = f"age-{cabin}-{tier}-{index}"
    try:
        await page.select_option(f"#{sel_id}", value=str(age), force=True, timeout=3000)
        await page.wait_for_timeout(400)
        return True
    except Exception:
        return False


async def _apply_voyagers_club(page, passengers: list, cabin: int = 1) -> dict:
    """Enter the real booking's Voyagers Club membership on the dummy
    booking, so the harvested category prices carry the same 5% club
    discount the customer already has.

    WHY THIS EXISTS - the single most consequential MSC bug found so far.
    Staging used to skip this on purpose, to keep today's price
    "undiscounted". But nothing ever removed the discount from the OTHER
    side of the comparison: `current_value` is the customer's real total,
    with their discount already in it. So every booking was scored as
    today's LIST price against the customer's DISCOUNTED price - which can
    almost never show a saving. That is why MSC runs came back with no
    opportunities at all.

    Confirmed on booking 3000081 against Neon's own screenshot of the
    IR2 card: $3,610.66 without the membership entered, $3,435.36 with it,
    against a current total of $3,517.34. The first says "no opportunity";
    the second is a real $81.98.

    Read-only in the sense that matters: this is the dummy/practice booking
    that MSC never records, and entering a membership number changes only
    what price is DISPLAYED. It commits nothing.

    Returns a dict that always records what happened, because a silent
    failure here would put us straight back to comparing a list price
    against a discounted one - the exact bug this fixes. `applied` False
    with a `reason` is a first-class outcome, never an exception.
    """
    member = next((p for p in (passengers or []) if p.get("voyagers_number")), None)
    if member is None:
        return {"applied": False, "reason": "no passenger holds a Voyagers membership",
                "member": None, "verified": False}

    name_parts = (member.get("name") or "").split()
    if len(name_parts) < 2 or not member.get("dob"):
        return {"applied": False, "reason": "member name or date of birth unreadable",
                "member": member.get("voyagers_number"), "verified": False}
    first, last = name_parts[0], name_parts[-1]

    try:
        crown = page.locator(f'.club-btn[data-cabin="{cabin}"]')
        if await crown.count() == 0:
            return {"applied": False, "reason": "no Voyagers Club control on this screen",
                    "member": member.get("voyagers_number"), "verified": False}
        await crown.first.click()
        await page.wait_for_timeout(1200)

        # Set every field through JS rather than typing. A native date
        # picker on #club-dob once intercepted a later click and silently
        # rewrote the DOB to today's date, which failed validation with a
        # generic "FOUND ERROR ON FIELD" - see msc_project_knowledge.md.
        # Setting the value and dispatching the events the page listens for
        # avoids opening the picker at all.
        await page.evaluate(
            """(v) => {
                const set = (sel, val) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                };
                return ['#club-firstname', '#club-lastname', '#club-dob', '#club-card']
                    .map((s, i) => set(s, [v.first, v.last, v.dob, v.card][i]))
                    .every(Boolean);
            }""",
            {"first": first, "last": last, "dob": member["dob"],
             "card": member["voyagers_number"]},
        )
        await page.evaluate(
            "() => { const b = document.querySelector('.club-search-btn');"
            " if (b) b.click(); }"
        )
        await page.wait_for_timeout(2500)

        # Verify against the page's own words. The membership number
        # echoed back is MSC confirming it accepted and matched the
        # member - not us assuming the click worked.
        body = await page.inner_text("body")
        number = member["voyagers_number"]
        verified = number in body and "voyagers club" in body.lower()
        failed = "found error on field" in body.lower()
        return {
            "applied": bool(verified and not failed),
            "reason": ("MSC rejected the membership details" if failed else
                       "" if verified else
                       "membership number was not echoed back by the page"),
            "member": number,
            "member_name": member.get("name"),
            "tier": member.get("voyagers_tier"),
            "verified": bool(verified),
        }
    except Exception as exc:  # noqa: BLE001 - never let this abort a scan
        logger.warning("msc.voyagers_club_entry_failed",
                       error=str(exc)[:200], member=member.get("voyagers_number"))
        return {"applied": False, "reason": f"error entering membership: {exc}"[:200],
                "member": member.get("voyagers_number"), "verified": False}


async def _fix_occupancy(page, passengers: list) -> dict:
    """Correct the occupancy screen to match the real booking's actual
    passengers — both the per-tier COUNTS and, for Child(12-17)/
    Kids(2-11)/Infant(0-1), each slot's exact AGE — BEFORE any price
    gets captured. A no-op (zero clicks) for the common all-adult case —
    only actually touches anything when a real mismatch is found.

    SAFETY GUARD, added 2026-08-12 after a real near-miss (booking
    3000009): an empty `passengers` list means passenger extraction
    FAILED (a timing race, now separately fixed at the source in
    _stage_booking_for_confirm's wait condition) — it does NOT mean the
    booking genuinely has zero guests. Trusting it anyway computed 0
    required adults for a real 2-adult booking and started clicking the
    adult count DOWN toward 0, stopped only by MSC's own UI floor. Never
    again: an empty passenger list means "don't touch occupancy at all,"
    the same as if nothing needed fixing — never a reason to reduce
    anything below what's already on the page."""
    if not passengers:
        before = await _read_occupancy(page)
        return {
            "before": before,
            "required": None,
            "after": before,
            "adjusted": False,
            "ages_filled": {},
            "stalled": False,
            "skipped_empty_passengers": True,
        }

    required = _compute_required_occupancy(passengers)
    counts_required = required["counts"]
    before = await _read_occupancy(page)
    adjusted = False
    stalled = False
    for tier in ("adult", "child", "jrchild", "infant"):
        current = before.get(tier) or 0
        target = counts_required.get(tier, 0)
        action = "add" if target > current else "sub"
        for _ in range(abs(target - current)):
            ok = await _click_occupancy(page, tier, action)
            if not ok:
                stalled = True  # hit the button's disabled state (e.g. a cabin/age cap) before reaching target
                break
            adjusted = True
    after = await _read_occupancy(page) if adjusted else before

    ages_filled = {}
    for tier in ("child", "jrchild", "infant"):
        tier_ages = required["ages"].get(tier, [])
        for i, age in enumerate(tier_ages, start=1):
            ok = await _select_age(page, tier, 1, i, age)
            ages_filled[f"{tier}-{i}"] = age if ok else None
            if not ok:
                stalled = True

    # VERIFY WHAT THE SCREEN ACTUALLY ENDED AT, not what we intended.
    # CONFIRMED on booking 3000081, 2026-08-24: passenger extraction
    # returned 2 guests AND the invoice said 2, so the computed requirement
    # was right - yet the screen finished at adult=1 and today's quote came
    # back $3,250.33 (a ONE-guest price) against a two-guest total of
    # $3,517.34, reported as a fake $267.01 opportunity. Twelve days earlier
    # the same booking reached adult=2 and correctly found nothing
    # ($3,610.66, above the current total).
    #
    # Checking the INTENT can never catch that. Only the applied state can.
    applied_guests = sum(v for v in (after or {}).values() if isinstance(v, int))
    intended_guests = sum(counts_required.values())
    occupancy_applied_ok = applied_guests == intended_guests and not stalled
    return {
        "before": before,
        "required": counts_required,
        "after": after,
        "adjusted": adjusted or bool(ages_filled),
        "ages_filled": ages_filled,
        "stalled": stalled,
        # Carried through so the calculator can refuse to price rather than
        # compare mismatched guest counts. See
        # msc_occupancy_is_trustworthy.
        "applied_guests": applied_guests,
        "intended_guests": intended_guests,
        "occupancy_applied_ok": occupancy_applied_ok,
        "dropped_passengers": required.get("dropped", 0),
        "passengers_seen": required.get("passengers_seen", len(passengers)),
    }


# CONFIRMED REAL BUG, fixed 2026-08-26: the label and type groups used to be
# `[^\n\-]+?` — a character class that EXCLUDES the hyphen — so no amount of
# backtracking could ever match a hyphenated discount name. This file's own
# dropdown-detection regex treats `MIL-CIV` as a real live label (see
# _find_discount_dropdown's /DISCOUNT|TODAY|MIL-CIV/ test), and
# calculator_msc's _parse_rate_pct uses 'MIL-CIV-IL-DSCNT-10%' as its
# documented example — so a real, disclosed, hyphenated discount was silently
# absent from current_discounts, which made `already_has_any_discount` False
# and produced a FALSE "no discount applied, add one" DISCOUNT_ADD
# opportunity. Exactly the false-positive class this file is built to prevent.
# `[^\n]+?` is safe here because the following `\s*-\s*Discount Type:` /
# `\s*-\s*Discount Rate:` literals anchor where each group must stop.
_DISCOUNT_RE = re.compile(
    r"(MSC Club Discount|Discount Description):\s*([^\n]+?)\s*-\s*Discount Type:\s*([^\n]+?)\s*-\s*Discount Rate:\s*([\d.]+)%"
)


def _extract_discounts(breakdown_text: str) -> list:
    """Parse whichever discount-disclosure lines MSC actually prints in
    the Price Breakdown text into structured data, instead of only
    catching a discount when it happens to get mentioned in chat.
    Confirmed two real label formats, and a booking can have BOTH at
    once (stacked) — e.g. booking 3000008 has 'Discount Description:
    SPECIAL OFFER 15% ... 15.0%' AND 'MSC Club Discount: MSCCLUB5 ...
    5.0%' as two separate lines:
      - 'MSC Club Discount: MSCCLUB5 - Discount Type: Percentage -
        Discount Rate: 5.0%'  (the flat Voyagers/Club 5%)
      - 'Discount Description: VOYAGERS EXCLUSIVES - Discount Type:
        Percentage - Discount Rate: 9.75%'  (named promo/senior/etc
        discounts)
    Rates stack MULTIPLICATIVELY, not additively (confirmed real math
    elsewhere in this project) — this only extracts the individual
    components, it does not attempt to combine them.

    IMPORTANT LIMITATION, confirmed against booking 3000021: this can
    return [] despite a booking having a real, verified 5% senior + 5%
    Voyagers discount applied — MSC does NOT always print an explicit
    disclosure line (senior discount in particular never seems to get
    one). An empty list means "no EXPLICITLY DISCLOSED discount found,"
    NOT "no discount applied." The itemized CAB/SRN math against known
    standard rates is still the authoritative check when this comes back
    empty — don't treat empty as proof of an undiscounted booking.

    RETURNS None (NOT []) when breakdown_text itself is falsy — this is
    a DIFFERENT, stronger signal than "confirmed empty," added 2026-08-11
    after a real false-positive incident: _lookup_one_booking now returns
    None for breakdown_text specifically when it could NOT confirm the
    Price Breakdown modal actually finished rendering (as opposed to
    confirming it rendered with genuinely nothing to disclose) — treating
    that the same as [] caused a booking with a real, disclosed discount
    to be reported as a false "no discount, add one" opportunity on 3 of
    5 identical repeated live checks. Callers (core/calculator_msc.py's
    three discount-related checks) must treat None as INSUFFICIENT_DATA,
    never silently default it to []."""
    if breakdown_text is None:
        return None
    if not breakdown_text:
        return []
    discounts = []
    for m in _DISCOUNT_RE.finditer(breakdown_text):
        kind = "club" if m.group(1) == "MSC Club Discount" else "named"
        discounts.append({
            "kind": kind,
            "label": m.group(2).strip(),
            "rate_pct": float(m.group(4)),
        })
    return discounts


# Standard (undiscounted) non-commissionable fare by cruise length, built
# from real captured data — confirmed non-linear, NOT a flat per-diem
# rate. Only lengths actually seen so far are here; an unlisted length
# means the implied-discount math below can't run (INSUFFICIENT_DATA is
# the correct outcome, not a guess).
STANDARD_NCF_BY_NIGHTS = {
    3: 66.00,
    4: 88.00,
    7: 182.00,
}

_SRN_LINE_RE = re.compile(r"SRN\s+Non commissionable fares[^\n]*")


def _extract_duration_nights(text: str):
    """Pull the cruise length from the booking summary's 'Duration:'
    line, e.g. 'Jul 17, 2027 , 8 Days , 7 Nights' -> 7."""
    m = re.search(r"(\d+)\s*Nights?", text or "")
    return int(m.group(1)) if m else None


def _srn_reference_available(summary_text: str) -> bool:
    """Whether STANDARD_NCF_BY_NIGHTS covers this booking's cruise
    duration — i.e. whether _extract_discounts_with_implied could actually
    attempt its SRN-vs-standard math for this booking, not just whether it
    happened to find anything. Senior discount (and Voyagers Exclusive)
    never disclose themselves explicitly, so an empty current_discounts
    list only PROVES "no discount applied" when this comes back True; when
    False, empty means "couldn't check," not "confirmed clean."

    CONFIRMED REAL FALSE POSITIVES, 2026-08-24: bookings 3000031 (9
    nights), 3000018 (19 nights), and 3000017 (10 nights) — none covered
    by STANDARD_NCF_BY_NIGHTS ({3, 4, 7}) — all had senior_count >= 2
    (genuinely eligible) and empty current_discounts, and DISCOUNT_ADD
    confidently recommended adding SENIOR DISCOUNT on all three. Since the
    SRN math never even ran for these lengths, that empty list never
    proved anything — the discount could already be silently applied.
    _check_discount_add now downgrades to INSUFFICIENT_DATA for SENIOR
    DISCOUNT specifically when this comes back False, instead of trusting
    an unverifiable empty list as confirmed-clean."""
    nights = _extract_duration_nights(summary_text)
    return nights is not None and nights in STANDARD_NCF_BY_NIGHTS


def _extract_srn_value(breakdown_text: str):
    """Pull the first per-passenger SRN (non-commissionable fares) dollar
    amount from a Price Breakdown capture, e.g. 'SRN	Non commissionable
    fares	$0.00	$0.00	-	$164.25	$164.25' -> 164.25. Every passenger on
    the same cabin shares the same SRN (confirmed across every real
    booking captured in this project), so the first line is sufficient —
    no need to average or check consistency across passengers."""
    if not breakdown_text:
        return None
    m = _SRN_LINE_RE.search(breakdown_text)
    if not m:
        return None
    amounts = re.findall(r"\$\s*([\d,]+\.\d{2})", m.group(0))
    if not amounts:
        return None
    try:
        return float(amounts[-1].replace(",", ""))
    except ValueError:
        return None


def _extract_discounts_with_implied(summary_text: str, breakdown_text: str):
    """CONFIRMED REAL GAP, closed 2026-08-11: senior discount and
    Voyagers Exclusive both NEVER print an explicit "Discount
    Description"/"MSC Club Discount" disclosure line (confirmed live on
    booking 3000021 for senior, and booking 3000024 for Exclusive — a
    full-page text search for "Discount"/"Exclusiv" found nothing on
    3000024 despite a real, confirmed 9.75% (5%+5%) discount being
    genuinely applied). Relying on _extract_discounts() alone therefore
    produces false "no discount, add one" recommendations on any booking
    carrying one of these silent discounts.

    This generalizes the fix that was previously only planned for senior
    discount specifically: compare the booking's actual SRN against the
    standard undiscounted NCF for its cruise length (STANDARD_NCF_BY_NIGHTS).
    If the actual SRN implies MORE reduction than what's already
    explicitly disclosed accounts for, the residual gap is real — append
    a synthetic {"kind": "implied", ...} entry representing exactly that
    gap (not the whole thing, if something is already disclosed — e.g. a
    booking that discloses a 5% Club discount but whose SRN math implies
    9.75% total has a real 5%-ish undisclosed layer ON TOP of the
    disclosed one, not a second unrelated 9.75%).

    Returns None (not []) when breakdown_text itself wasn't confirmed
    captured — same "don't know" signal _extract_discounts already uses,
    propagated through unchanged. Returns whatever _extract_discounts
    found, unmodified, when duration/SRN can't be read (unknown cruise
    length, or no SRN line found) — this function only ever ADDS
    information, never removes or overrides real disclosed discounts."""
    discounts = _extract_discounts(breakdown_text)
    if discounts is None:
        return None

    nights = _extract_duration_nights(summary_text)
    standard_ncf = STANDARD_NCF_BY_NIGHTS.get(nights) if nights is not None else None
    srn_value = _extract_srn_value(breakdown_text)
    if standard_ncf is None or srn_value is None:
        return discounts

    explained_factor = 1.0
    for d in discounts:
        rate = d.get("rate_pct")
        if rate is not None:
            explained_factor *= (1 - rate / 100)
    actual_factor = srn_value / standard_ncf

    # actual_factor should never be LOWER than explained_factor by more
    # than rounding noise — more discounts only ever reduce price
    # further, they can't increase it. A meaningful gap means something
    # real is reducing SRN beyond what's disclosed. 0.005 tolerance
    # absorbs cent-level rounding on the standard NCF x factor math.
    if actual_factor < explained_factor - 0.005:
        residual_factor = actual_factor / explained_factor if explained_factor > 0 else actual_factor
        implied_pct = round((1 - residual_factor) * 100, 2)
        discounts = discounts + [{
            "kind": "implied",
            "label": (
                f"undisclosed (SRN ${srn_value:.2f} vs ${standard_ncf * explained_factor:.2f} "
                f"expected from disclosed discounts alone)"
            ),
            "rate_pct": implied_pct,
        }]
    return discounts


def _find_today_price(listing_text: str, category: str, is_guaranteed: bool = False):
    """Find the $ price MSC lists for this exact category code in a
    fresh category-listing page (post-Confirm), e.g. '... (IR2) ...
    $ 2,356.76' — the code and its price can be a line or two apart.
    Cents are NOT always shown (some listings show '$ 1,418' with no
    decimal at all, others show '$ 2,356.76') — decimal part is optional,
    matched greedily so a whole-dollar amount isn't cut short.

    Guaranteed Cabin bookings don't have a short code to search for —
    `category` is a type name like 'BALCONY'/'INTERIOR' instead, which
    shows up in the listing as '<TYPE> BELLA (CODE)\\nGuaranteed
    stateroom\\n$PRICE' (confirmed real format, e.g. 'BALCONY BELLA (BB)
    \\nGuaranteed stateroom\\n$ 1,098')."""
    if not listing_text or not category:
        return None
    if is_guaranteed:
        m = re.search(
            rf"{re.escape(category)}\s+BELLA\s*\([A-Z]+\)\s*\n(?:[^\n]*\n)?\$\s*([\d,]+(?:\.\d{{2}})?)",
            listing_text,
            re.IGNORECASE,
        )
        return m.group(1) if m else None
    idx = listing_text.find(f"({category})")
    if idx == -1:
        return None
    m = re.search(r"\$\s*([\d,]+(?:\.\d{2})?)", listing_text[idx:idx + 200])
    return m.group(1) if m else None


_MONEY_NEAR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")


def today_price_detail(listing_text: str, category: str,
                       is_guaranteed: bool = False) -> dict:
    """Everything the listing says near this category, not just one number.

    ADDED 2026-09-01 for a data-collection run. `_find_today_price` returns
    a single scalar - the FIRST dollar figure within 200 characters of the
    category code - and that one number is then compared against the
    booking's whole-invoice total. Three real bookings show that is not
    enough to reproduce the true answer:

        3000081  system $267.01   Neon $81.98 + $25 extra OBC
        3000071  system (none)    Neon $63.24
        3000083  system (none)    Neon $23.66

    Two of those land at ~3.07% of the commissionable cruise fare
    (81.98/2671.40 = 3.069%, 63.24/2052.28 = 3.081%) - agreeing to within
    0.012 points across different bookings, which is a real signal but not
    yet an explained mechanism. A single scalar cannot tell us whether the
    listing price is per-guest or a total, whether it includes taxes/NCF, or
    what OBC it carries - so it cannot be reconciled against the invoice.

    Purely additive and READ-ONLY: it re-reads text already captured and
    changes no decision. `_find_today_price` is deliberately left untouched
    so no existing behaviour moves while evidence is still being gathered.
    """
    out = {
        "matched_price": _find_today_price(listing_text, category, is_guaranteed),
        "window": None,
        "all_prices_near": [],
        "obc_mentions": [],
        "per_guest_hints": [],
    }
    if not listing_text or not category:
        return out
    idx = listing_text.find("(" + category + ")")
    if idx == -1 and is_guaranteed:
        idx = listing_text.upper().find(category.upper())
    if idx == -1:
        return out
    start = max(0, idx - 400)
    window = listing_text[start:idx + 900]
    out["window"] = window
    out["all_prices_near"] = [
        m.group(1) for m in re.finditer(_MONEY_NEAR_RE, window)
    ]
    for line in window.split(chr(10)):
        low = line.lower()
        if any(k in low for k in ("obc", "onboard credit", "on board credit",
                                  "shipboard credit")):
            out["obc_mentions"].append(line.strip()[:120])
        if any(k in low for k in ("per person", "per guest", " pp", "total")):
            out["per_guest_hints"].append(line.strip()[:120])
    return out


def _parse_discount_rules(rules: str) -> dict:
    """Parse DiscountPaxTypeCmd's semicolon-delimited 'rules' field (e.g.
    'NumMinAdt:1;NumMaxAdt:10;...;Cumulability:Yes;...') into a dict.
    Real values can be empty (e.g. 'AgeAdt:') — those come back as ''."""
    out = {}
    for part in (rules or "").split(";"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        out[key.strip()] = value.strip()
    return out


def _extract_discount_catalog(response_body: str) -> list:
    """Parse DiscountPaxTypeCmd's response body into a structured list of
    every discount MSC's backend actually offers for this sailing —
    confirmed 2026-08-11 to be the real source of truth the portal's own
    JS uses to populate every discount UI element (the 'Additional
    Discounts' dropdown AND the crown/Voyagers-Club modal's checkbox).

    The body is wrapped in JS-comment markers ('/*{...}*/', a JSONP-style
    safety wrapper) — confirmed real format from every capture seen so
    far. Real fields per entry (see msc_project_knowledge.md for the full
    breakdown): discCd/paxType (code), discDesc (on-screen dropdown
    label), paxDesc (the REAL program name — diverges from discDesc for
    Voyagers Selection: discDesc says 'SPECIAL OFFER 15%', paxDesc says
    'Voyagers Selection WELCOME'), discRate (numeric %, but '0' for
    senior — see is_variable), club (Yes/No — Voyagers Club membership
    required), isInv (true/false — believed to mean 'variable/computed
    rate' rather than a flat literal, since it's true on SENIOR25/
    TODAY10/MSVG10W/MSVG15W), and rules (semicolon string containing
    Cumulability:Yes/No and AgeAdt).

    Returns [] (not None) on any parse failure — a caller should treat []
    the same as "nothing captured" via the None check happening upstream
    (this function only ever gets called with an actual response body)."""
    if not response_body:
        return []
    try:
        raw = response_body.strip()
        if raw.startswith("/*"):
            raw = raw[2:]
        if raw.endswith("*/"):
            raw = raw[:-2]
        data = json.loads(raw)
        pax_types = (
            data.get("DtsGetDiscountPaxTypeResponse", {}).get("paxType", [])
        )
    except Exception:
        return []

    catalog = []
    for entry in pax_types:
        rules = _parse_discount_rules(entry.get("rules", ""))
        rate_pct = None
        try:
            rate_pct = float(entry.get("discRate"))
        except (TypeError, ValueError):
            pass
        age_min = None
        try:
            age_min = int(rules.get("AgeAdt") or "")
        except (TypeError, ValueError):
            pass
        catalog.append({
            "disc_cd": entry.get("discCd") or entry.get("paxType"),
            "label": entry.get("discDesc"),
            "program_name": entry.get("paxDesc"),
            "rate_pct": rate_pct,
            "requires_club": (entry.get("club") or "").strip().lower() == "yes",
            "cumulable": (rules.get("Cumulability") or "").strip().lower() == "yes",
            "is_variable": (entry.get("isInv") or "").strip().lower() == "true",
            "age_min": age_min,
        })
    return catalog


_CABIN_LINE_RE = re.compile(r"^\s*Cabin\s+(\d+)\s*-", re.IGNORECASE | re.MULTILINE)


def _count_cabins(text: str) -> int:
    """How many cabins this booking has, from MSC's own `Cabin N - ...`
    lines in the booking summary.

    Added 2026-08-27. Counts DISTINCT cabin numbers rather than raw line
    matches, because the same cabin heading can legitimately appear more
    than once on the page (summary plus itemized breakdown) and counting
    lines would overstate it — which would wrongly refuse a normal
    single-cabin booking.

    Returns 0 when the pattern isn't found at all. Callers must treat 0 as
    "unknown", NOT as "one cabin": refusing to guess is the point.
    """
    if not text:
        return 0
    return len({m.group(1) for m in _CABIN_LINE_RE.finditer(text)})


def _is_group_rate(rate_name: str) -> bool:
    """'Group Rates' bookings use a separate block-allocation inventory
    that isn't offered at all in the individual dummy-booking search —
    confirmed real 2026-08-10 on bookings 3000022/3000020, whose rate
    tabs (Escape to Sea, Flash Sale, Brochure Rates, etc.) never included
    anything resembling their own "Group Rates" program. Comparing a
    Group Rates booking against ANY of those tabs is not apples-to-apples
    and shouldn't be attempted — flag it instead of silently comparing
    against the closest-sounding tab."""
    return bool(rate_name) and "group" in rate_name.lower()


_TAB_MATCH_FILLER_WORDS = {
    "flash", "sale", "cruise", "only", "included", "and", "with",
    "to", "the", "rates", "of", "in",
}

# The three real amenity-inclusion tokens confirmed across every rate/tab
# name seen so far (see _select_matching_tab's amenity-signature tier).
_TAB_MATCH_AMENITY_TOKENS = {"drinks", "wifi", "obc"}


def _tab_keywords(s: str) -> set:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in _TAB_MATCH_FILLER_WORDS}


def _is_brochure_rate(tab: str) -> bool:
    """HARD RULE, stated directly by Neon 2026-08-12: 'Brochure Rate' is
    NEVER a valid comparison target, regardless of price — it strips out
    CruiseIntel's commission entirely, so even if it happened to show the
    lowest number, recommending it would cost the agency money rather
    than make any. Confirmed via external research (MSC's own published
    fare-tier language: EB/Best Price Today/Brochure/Promo are distinct
    pricing tiers) that Brochure sits apart from the promotional/EB
    rates this project otherwise treats as interchangeable — this isn't
    a guess, it's a real, separate rate class. Excluded from candidacy
    in EVERY matching tier below, never just the ones added after this
    rule was learned."""
    return "brochure" in tab.lower()


def _dedupe_booking_ids(ids_raw: list) -> tuple:
    """Remove duplicate booking IDs, preserving first-occurrence order.
    Extracted as a pure function (2026-08-13, Phase 0 correctness audit)
    from check_booking_batch2's input parsing, so the fix for a booking ID
    appearing twice — which used to let both tabs process the SAME booking
    concurrently under one login, a more dangerous version of the
    confirmed real 2026-08-10 session/cookie-conflict incident — is
    directly unit-testable. Returns (deduped_ids, duplicate_ids_dropped)."""
    seen_ids: set = set()
    ids: list = []
    duplicate_ids: list = []
    for booking_id in ids_raw:
        if booking_id in seen_ids:
            duplicate_ids.append(booking_id)
            continue
        seen_ids.add(booking_id)
        ids.append(booking_id)
    return ids, duplicate_ids


def _select_matching_tab(rate_name: str, tabs: list) -> tuple:
    """Pure selection logic, split out from _match_rate_tab so it can be
    unit-tested directly against real rate-name/tab-list examples without
    needing a live page. Returns (target_or_None, reason_if_none_or_None).

    Brochure Rate tabs are filtered out before any tier runs — see
    _is_brochure_rate. Five tiers after that, in order — the FIRST one
    that finds a confident match wins:
    1. Exact (case-insensitive) match.
    2. Substring match either direction — real rate names are sometimes
       truncated/reworded slightly between the booking's own detail page
       and the dummy-listing tab labels.
    3. Keyword-subset match (confirmed real gap 2026-08-10, booking
       3000023): "DRINKS AND WIFI INCLUDED" doesn't substring-match
       "FLASH SALE DRINKS AND WIFI", even though they're clearly the
       same product — ignore generic promotional filler words, and
       match only if EVERY one of the rate name's distinctive words
       appears in the tab's label.
    4. Amenity-signature EXACT match (confirmed real ground truth from
       Neon 2026-08-12, booking 3000015): "BALCONY UPGRADE DRINKS
       WIFI" describes a category-upgrade PROMO ("balcony"/"upgrade"),
       not today's tab vocabulary at all — tier 3 fails since those
       words never appear in any tab. But the real comparable PRODUCT is
       defined by what's INCLUDED (drinks + wifi), which Neon confirmed
       directly maps to "FLASH SALE DRINKS AND WIFI". Compares ONLY the
       amenity-inclusion words, ignoring promotional/upsell framing
       entirely — requires an EXACT set match (not subset either
       direction), since a drinks+wifi tab is a genuinely different,
       cheaper product than a drinks+wifi+obc one (confirmed distinct
       earlier: booking 3000010's "CRUISE WITH DRINKS WIFI OBC"
       correctly does NOT match a plain drinks+wifi tab — a partial
       overlap must never count as a match).
    5. Cruise-only-tier fallback (direct instruction from Neon
       2026-08-12: "epic europe escape to sea etc are all the same" —
       confirmed via external research that these are all standard,
       interchangeable MSC promotional/marketing campaign names, not
       different products). When the rate name has NO amenity words at
       all (tier 4 found nothing to go on) and exactly one non-Brochure
       tab ALSO has no amenity words, they're treated as the same
       underlying commissionable rate regardless of campaign name —
       e.g. "EPIC EUROPE SALE" or "BALCONY AT OCEANVIEW PRICE" (a
       category-upsell name, not a campaign name, but still carries no
       amenity info) matching "ESCAPE TO SEA CRUISE ONLY". More than one
       such tab is genuinely ambiguous (which specific cruise-only
       campaign is live can vary) — don't guess which one.
    6. Fuzzy-similarity fallback (RapidFuzz), added 2026-08-25 — only
       ever runs after all five tiers above found nothing AND none of
       them already flagged an ambiguous refusal (never overrides a
       tier's own "refusing to guess"). Catches a real, plausible gap
       none of tiers 1-5 cover: a minor wording/typo variant (confirmed
       real example: "SALE DRINK AND WIFI" — singular "DRINK" — against
       tab "FLASH SALE DRINKS AND WIFI") breaks tier 3's exact-word
       keyword-subset check AND tier 4's amenity-signature check (its
       token set is a literal string set, "drink" != "drinks"), so it
       fell through to "no match" before this tier existed. Only commits
       when the best candidate's token_set_ratio is >=80 AND at least 20
       points clear of the second-best candidate — validated by running
       every existing test case above (including the historical "$654 vs
       $26" and OBC-vs-non-OBC incidents) through this exact threshold
       before adopting it: zero change to any of their outcomes, this
       tier never even fires for them (their fuzzy scores are either
       too low or too close together to clear the bar)."""
    if not tabs:
        return None, "no rate tabs found on this listing"

    candidate_tabs = [t for t in tabs if not _is_brochure_rate(t)]
    if not candidate_tabs:
        return None, f"only Brochure Rate tabs available (never used — see _is_brochure_rate): {tabs}"

    rate_lower = rate_name.lower()
    ambiguous_note: str | None = None
    target = next((t for t in candidate_tabs if t.lower() == rate_lower), None)
    if target is None:
        # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness
        # audit): this used to be `next(...)`, silently committing to
        # whichever substring match happened to appear FIRST in DOM order
        # with zero ambiguity detection — the same bug shape as the
        # historical "$654 vs $26" incident (comparing against the wrong
        # tab produces a plausible-looking wrong price), just never
        # guarded here the way tiers 4/5 below already are. Now collects
        # every substring match and only commits when there's exactly one.
        substring_candidates = [t for t in candidate_tabs if t.lower() in rate_lower or rate_lower in t.lower()]
        if len(substring_candidates) == 1:
            target = substring_candidates[0]
        elif len(substring_candidates) > 1:
            ambiguous_note = f"tier 2 (substring) found {len(substring_candidates)} ambiguous candidates: {substring_candidates}"
    if target is None:
        rate_keywords = _tab_keywords(rate_name)
        if rate_keywords:
            candidates = [t for t in candidate_tabs if rate_keywords.issubset(_tab_keywords(t))]
            if candidates:
                # Prefer the tab with the fewest EXTRA words beyond what
                # matched — the closest overall label, not just any
                # superset match. CONFIRMED REAL RISK, fixed 2026-08-13:
                # this used to be a bare `min(...)`, which silently picks
                # whichever tied winner appears first when two or more
                # candidates share the same minimal extra-word count —
                # the same unguarded-ambiguity bug shape as tier 2 above.
                # Only commit when the minimum is uniquely achieved.
                fewest_extra = min(len(_tab_keywords(t)) for t in candidates)
                tied_best = [t for t in candidates if len(_tab_keywords(t)) == fewest_extra]
                if len(tied_best) == 1:
                    target = tied_best[0]
                    ambiguous_note = None
                else:
                    ambiguous_note = f"tier 3 (keyword-subset) found {len(tied_best)} ambiguous tied candidates: {tied_best}"
    if target is None:
        rate_amenities = _tab_keywords(rate_name) & _TAB_MATCH_AMENITY_TOKENS
        if rate_amenities:
            candidates = [t for t in candidate_tabs if (_tab_keywords(t) & _TAB_MATCH_AMENITY_TOKENS) == rate_amenities]
            if len(candidates) == 1:
                target = candidates[0]
                ambiguous_note = None
            elif len(candidates) > 1:
                ambiguous_note = f"tier 4 (amenity-signature) found {len(candidates)} ambiguous candidates: {candidates}"
        else:
            # Tier 5: rate name carries no amenity signal at all — fall
            # back to whichever non-Brochure tab(s) are ALSO amenity-free
            # ("cruise only" tier), since Neon confirmed those campaign
            # names are interchangeable.
            cruise_only_candidates = [t for t in candidate_tabs if not (_tab_keywords(t) & _TAB_MATCH_AMENITY_TOKENS)]
            if len(cruise_only_candidates) == 1:
                target = cruise_only_candidates[0]
            elif len(cruise_only_candidates) > 1:
                ambiguous_note = (
                    f"tier 5 (cruise-only fallback) found {len(cruise_only_candidates)} "
                    f"ambiguous candidates: {cruise_only_candidates}"
                )
    # Tier 6: fuzzy-similarity fallback — see this function's docstring
    # for the validated 80%-similarity / 20-point-gap thresholds and why
    # they were chosen. Never runs if a prior tier already found a match
    # OR already flagged its own ambiguous refusal (ambiguous_note is
    # only ever set when ">1" candidates tied, never when a tier simply
    # found zero — see tiers 3/4 above).
    if target is None and not ambiguous_note:
        _FUZZY_SCORE_FLOOR = 80
        _FUZZY_GAP_FLOOR = 20
        scored = sorted(
            ((t, fuzz.token_set_ratio(rate_name, t)) for t in candidate_tabs),
            key=lambda pair: -pair[1],
        )
        if scored:
            best_tab, best_score = scored[0]
            second_score = scored[1][1] if len(scored) > 1 else 0
            if best_score >= _FUZZY_SCORE_FLOOR and (best_score - second_score) >= _FUZZY_GAP_FLOOR:
                target = best_tab
            elif best_score >= _FUZZY_SCORE_FLOOR:
                ambiguous_note = (
                    f"tier 6 (fuzzy) found candidates too close to call: "
                    f"{[(t, round(s, 1)) for t, s in scored[:3]]}"
                )
    if target is None:
        if ambiguous_note:
            return None, f"ambiguous match for {rate_name!r} — {ambiguous_note} — refusing to guess"
        return None, f"no tab matches {rate_name!r} (available: {tabs})"
    return target, None


async def _match_rate_tab(page, rate_name: str) -> dict:
    """Find and click the '.cs-price-code-box' promo tab whose text
    matches the booking's own rate name, so the price read afterward is
    for the SAME rate program the booking is actually on — not whichever
    tab happens to be active by default. Returns which tab ended up
    active and whether it was a real match, so a caller can decide
    whether to trust the resulting price. Confirmed real bug this fixes:
    Yacht Club categories can price identically across multiple tabs
    while every other category differs, so tab-matching must always run,
    not just when a price looks suspicious."""
    if not rate_name:
        return {"matched": False, "reason": "booking has no known rate name", "active_tab": None}
    try:
        tabs = await page.evaluate(
            "Array.from(document.querySelectorAll('.cs-price-code-box')).map(t => t.textContent.trim())"
        )
    except Exception as e:
        return {"matched": False, "reason": f"could not read tabs: {e}", "active_tab": None}

    target, reason = _select_matching_tab(rate_name, tabs)
    if target is None:
        return {"matched": False, "reason": reason, "active_tab": None}

    try:
        clicked = await page.evaluate(
            "(name) => { const boxes = Array.from(document.querySelectorAll('.cs-price-code-box')); "
            "const box = boxes.find(b => b.textContent.trim() === name); "
            "if (!box) return false; "
            "const title = box.querySelector('.priceCodeTitle') || box; "
            "title.click(); box.click(); return true; }",
            target,
        )
    except Exception as e:
        return {"matched": False, "reason": f"click failed: {e}", "active_tab": target}

    await page.wait_for_timeout(1500)
    return {"matched": bool(clicked), "reason": None, "active_tab": target}


async def _capture_all_tab_prices(page, category: str, is_guaranteed: bool) -> dict:
    """ADDED 2026-08-12: when _match_rate_tab can't confirm which tab
    matches the booking's own rate (its original promo/rate program is
    no longer offered today at all — confirmed real, current situation
    on several live bookings, e.g. 'EPIC EUROPE SALE' matching none of
    today's tabs), don't just give up with nothing. Click through EVERY
    available tab and capture today's price for this category under
    each one, so whoever reviews the result has real reference numbers
    instead of a dead end — clearly labeled as unconfirmed comparisons
    (none of these tabs is proven to be the same product the booking is
    actually on), never fed into PRICE_MATCH as if it were a confirmed
    match. Returns {tab_name: price_or_None}; never raises.

    Brochure Rate tabs are excluded here too (see _is_brochure_rate) —
    Neon's rule is to never even look at that number, so it shouldn't
    appear even as unconfirmed reference data."""
    try:
        tabs = await page.evaluate(
            "Array.from(document.querySelectorAll('.cs-price-code-box')).map(t => t.textContent.trim())"
        )
    except Exception:
        return {}
    tabs = [t for t in tabs if not _is_brochure_rate(t)]

    prices = {}
    for tab in tabs:
        try:
            clicked = await page.evaluate(
                "(name) => { const boxes = Array.from(document.querySelectorAll('.cs-price-code-box')); "
                "const box = boxes.find(b => b.textContent.trim() === name); "
                "if (!box) return false; "
                "const title = box.querySelector('.priceCodeTitle') || box; "
                "title.click(); box.click(); return true; }",
                tab,
            )
            if not clicked:
                prices[tab] = None
                continue
            await page.wait_for_timeout(1500)
            listing_text = await page.inner_text("body")
            prices[tab] = _find_today_price(listing_text, category, is_guaranteed)
        except Exception:
            prices[tab] = None
    return prices


def _parse_dollars_safe(value):
    """Parse a captured price string ('1,234.56') into a float, or None
    for anything that isn't a clean parse — never raises, since these
    values come from regex captures against live page text that
    sometimes come back None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


# ADDED 2026-08-12, direct instruction from Neon: "paid in full" isn't
# just an exact $0.00 Due Amount. Full detection system:
#   1. Due Amount is $0.00 (or negative, i.e. MSC rendered it that way
#      instead of swapping to an "Overpayment" label).
#   2. Due Amount field is replaced by "Overpayment" wording — the
#      client has paid MORE than the booking's current total, which is
#      paid-in-full and then some.
#   3. Due Amount is a small non-zero residual under this threshold —
#      Neon's own words: "if it is less than 15$ it is paid in full."
#      Threshold lives in core/models.py (MSC_PAID_IN_FULL_DUE_THRESHOLD)
#      so calculator_msc.py's _due_amount_context_note wording stays in
#      sync with this check rather than drifting to its own number.
def is_valid_msc_booking_id(candidate: str) -> bool:
    """Whether a string is plausibly a real MSC booking number.

    ADDED 2026-09-01 after the full audit found a record in
    booking_data.jsonl stored under the booking_id "/*" — a shell glob that
    reached a lookup command as if it were a booking number, and then had a
    full invoice captured against it. It sat in the corpus as a phantom
    booking, and any per-booking analysis silently counted it.

    Real MSC booking numbers are purely numeric and 6-9 digits long across
    every one of the 118 captured bookings. Validating at the point ids are
    parsed is what stops this recurring; the existing bad record is left in
    place (it is real captured data under a wrong key, not something to
    delete quietly) and simply skipped.
    """
    return bool(candidate) and bool(re.fullmatch(r"\d{6,9}", candidate.strip()))


# Invoice line codes, decoded from 100 captured MSC invoices 2026-09-02.
# Column order on every line is:
#     CODE  description  commission$  discount$  comm%  NET  GROSS
# (verified: gross - net == commission, and commission/gross == comm%, on
# bookings with several different commission rates including 17% and 18.84%)
_CRUISE_CODES = frozenset({"CAB", "SRN", "PCH", "OBS"})
# NON-CRUISE ADDED SERVICES. A category quote can never include these, so a
# booking total containing them is not comparable to one - the same
# like-for-like flaw as the club discount, the occupancy and the multi-cabin
# bugs. Found by the reconciliation check below: 4 of 100 invoices failed to
# add up, and every gap was one of these lines (a $112.00 Pisa excursion, a
# $48.00 backstage tour x2, airport transfers, flights).
_ADDED_SERVICE_CODES = frozenset({"ACT", "TRF", "AIR", "MSC"})

_INVOICE_LINE_RE = re.compile(
    r"^([A-Z]{2,4})	(.*?)	\$([\d,]+\.\d{2})	\$([\d,]+\.\d{2})	"
    r"([\d.]+%|-)	\$([\d,]+\.\d{2})	\$([\d,]+\.\d{2})\s*$", re.M)
_INVOICE_TOTAL_RE = re.compile(
    r"^Total (?:Adult|Child|Infant)\s+\d+	\$([\d,]+\.\d{2})	"
    r"\$([\d,]+\.\d{2})	(?:[\d.]+%|-)	\$([\d,]+\.\d{2})	"
    r"\$([\d,]+\.\d{2})\s*$", re.M)


def msc_invoice_components(breakdown_text: str | None) -> dict:
    """Itemise an invoice and check that it ADDS UP.

    ADDED 2026-09-02 at Neon's request to make the scan "as accurate as
    human eyes". The cheapest possible correctness check: MSC prints both
    the component lines and per-guest Total lines, so the components must
    sum to the totals. A misparse, a truncated capture or an unknown line
    code shows up immediately as a gap, instead of silently producing a
    plausible wrong number - which is exactly what the truncated
    `breakdown_text` incident did (an empty current_discounts on a booking
    that really had a disclosed discount, producing a false DISCOUNT_ADD).

    Measured on the corpus: 96 of 100 invoices reconcile to the cent. All
    four failures were real findings, not noise - each gap was an unparsed
    non-cruise service line.

    `reconciles` False is a data-quality signal, never a verdict on the
    booking. `non_cruise_total` above zero means the booking total includes
    excursions/transfers/flights that no category quote can contain.
    """
    out = {
        "by_code": {}, "cruise_total": 0.0, "non_cruise_total": 0.0,
        "non_cruise_codes": [], "stated_total": None, "gap": None,
        "reconciles": False, "unknown_codes": [], "lines": 0,
    }
    if not breakdown_text:
        return out
    by_code: dict[str, float] = {}
    for code, _desc, _comm, _disc, _pct, _net, gross in _INVOICE_LINE_RE.findall(
            breakdown_text):
        by_code[code] = round(by_code.get(code, 0.0) + _parse_money(gross), 2)
        out["lines"] += 1
    out["by_code"] = by_code
    out["cruise_total"] = round(
        sum(v for k, v in by_code.items() if k in _CRUISE_CODES), 2)
    out["non_cruise_total"] = round(
        sum(v for k, v in by_code.items() if k in _ADDED_SERVICE_CODES), 2)
    out["non_cruise_codes"] = sorted(
        k for k, v in by_code.items() if k in _ADDED_SERVICE_CODES and v)
    out["unknown_codes"] = sorted(
        k for k in by_code if k not in _CRUISE_CODES and k not in _ADDED_SERVICE_CODES)
    totals = _INVOICE_TOTAL_RE.findall(breakdown_text)
    if totals:
        stated = round(sum(_parse_money(t[3]) for t in totals), 2)
        out["stated_total"] = stated
        component_sum = round(sum(by_code.values()), 2)
        out["gap"] = round(stated - component_sum, 2)
        out["reconciles"] = abs(out["gap"]) <= 0.02
    return out


def _parse_money(raw: str) -> float:
    return float(str(raw).replace(",", "").replace("$", "").strip())


def _is_paid_in_full(due_amount: float, is_overpayment: bool, threshold: float) -> bool:
    if is_overpayment:
        return True
    if due_amount is not None and due_amount < threshold:
        return True
    return False


async def _capture_msc_response(response, booking_id: str, out_path: str, state: dict = None) -> None:
    """Log MSC backend calls fired during a Confirm click (manual or, as
    of 2026-08-11, automated — see confirm_and_proceed's permission-rule
    note) so the exact request behind it can eventually be replicated
    directly. Same pattern as record_msc_session.py's capture_response(),
    just keyed by booking_id and scoped to WCS servlet calls.

    FIXED 2026-08-10: originally only logged request_post_data, never
    the response body — meaning CabinSelectionConfirmCmd captures (the
    real request behind the Confirm click) recorded exactly what was
    SENT but not what came back, making it impossible to tell whether
    that single call returns the category prices directly or just
    updates server-side state that a follow-up call then reads. Now
    captures response_body the same way record_msc_session.py always
    did, so this gap doesn't recur.

    ADDED 2026-08-11: when this response is DiscountPaxTypeCmd — the real
    per-sailing discount catalog endpoint, confirmed to be the source of
    truth behind every discount UI element including the previously-
    missed 'Voyagers Selection' promo (see msc_project_knowledge.md) —
    and a mutable `state` dict was passed in, parse it immediately and
    stash the result in state['discount_catalog_by_booking'][booking_id]
    so the staging/harvest/check_booking flow can pick it up without a
    second network round-trip. This is a pure addition: when state is
    None (e.g. the legacy open_batch_tabs listener, not yet updated to
    pass it), behavior is identical to before."""
    try:
        if "wcs/stores/servlet" not in response.url:
            return
        request = response.request
        entry = {
            "timestamp": datetime.now().isoformat(),
            "booking_id": booking_id,
            "url": response.url,
            "method": request.method,
            "status": response.status,
            "resource_type": request.resource_type,
        }
        if request.resource_type in ("xhr", "fetch", "document"):
            try:
                entry["request_post_data"] = request.post_data
            except Exception:
                pass
            try:
                body = await response.body()
                if len(body) <= 200_000:
                    entry["response_body"] = body.decode("utf-8", errors="replace")
                else:
                    entry["response_body_truncated"] = True
            except Exception as e:
                # Silently swallowing this before hid why response_body
                # never showed up on a real capture (booking 3000025,
                # 2026-08-10) despite resource_type correctly being 'xhr' —
                # recording the real reason instead of guessing.
                entry["response_body_error"] = str(e)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

        if state is not None and "DiscountPaxTypeCmd" in response.url and entry.get("response_body"):
            catalog = _extract_discount_catalog(entry["response_body"])
            if catalog:
                state.setdefault("discount_catalog_by_booking", {})[booking_id] = catalog
    except Exception:
        # CONFIRMED REAL RISK, fixed 2026-08-13: this used to be a bare
        # `pass` -- any failure capturing a network response (this is a
        # diagnostic/replay log, never the live booking-check path) was
        # completely invisible, with no trace anywhere it happened. Log it
        # instead; still never raises, so it can't affect the calling
        # page.on("response", ...) listener or the booking flow it serves.
        logger.exception("msc.capture_response_failed", booking_id=booking_id)


async def _stage_booking_for_confirm(page, booking_id: str) -> dict:
    """Read-only up to (but never past) the Confirm button: open a
    booking, capture its current category/value, click 'Book Same
    Departure', and stop on the resulting occupancy screen. Never clicks
    CONFIRM AND PROCEED — that click always belongs to Neon."""
    # Single source of truth for this URL is config/settings.py (consolidated
    # 2026-08-11 — was hardcoded identically in three places here before).
    url = settings.msc_booking_search_url.format(booking_id=booking_id)

    async def _wait_for(*markers):
        for _ in range(10):
            body = await page.inner_text("body")
            if any(m in body for m in markers):
                return body
            await page.wait_for_timeout(500)
        return await page.inner_text("body")

    # CONFIRMED REAL BUG, 2026-08-12, booking 3000009: waiting for just
    # "Booking Value" let this proceed on an incomplete capture where the
    # Passenger Details section hadn't rendered yet — the SAME class of
    # timing bug already fixed once in _lookup_one_booking's
    # _wait_for_content (which needed "Duration" required TOGETHER WITH
    # "Booking Value", not as an alternate). Real consequence here was
    # much worse than there: _extract_passengers returned an empty list,
    # _compute_required_occupancy computed 0 adults for a real 2-adult
    # (ages 71/72) booking, and _fix_occupancy started clicking the
    # adult count DOWN toward 0 — only stopped because MSC's own UI
    # floor at 1 adult disabled the button first (surfaced correctly as
    # "stalled", which is what caught this — not the wait logic).
    # _wait_for's *markers is an OR match, so requiring both real content
    # markers together needs its own loop, not just swapping one string
    # in for another in the existing OR-based helper.
    async def _wait_for_booking_page():
        for _ in range(10):
            body = await page.inner_text("body")
            if (
                ("Booking Value" in body and "Passenger Details" in body)
                or "No bookings found" in body
                or "Session Timed Out" in body
            ):
                return body
            await page.wait_for_timeout(500)
        return await page.inner_text("body")

    await page.goto(url, wait_until="domcontentloaded")
    booking_text = await _wait_for_booking_page()
    if (
        "welcome" in page.url
        or "login" in page.url.lower()
        or "ReLogonFormView" in page.url
        or "Session Timed Out" in booking_text
    ):
        await page.goto(url, wait_until="domcontentloaded")
        booking_text = await _wait_for_booking_page()

    if "No bookings found" in booking_text:
        return {"booking_id": booking_id, "found": False}

    # CONFIRMED REAL BUG, 2026-08-12: checking "Session Timed Out" in the
    # text alone misses the far more common real shape of a dead session
    # — silently landing back on /us/welcome with the login modal, which
    # contains none of that text. After the retry above, if we're STILL
    # sitting on welcome/login, the session is dead, full stop — same
    # underlying condition as "Session Timed Out", just a different page.
    # Real symptom: a 6-booking batch all failed as
    # "confirm_button_not_found" because this never triggered the
    # auto-relogin retry _check_booking_msc already has wired up.
    if (
        "Session Timed Out" in booking_text
        or "welcome" in page.url
        or "login" in page.url.lower()
        or "ReLogonFormView" in page.url
    ):
        # Confirmed real 2026-08-10: the persistent session can time out
        # mid-work (idle timeout, separate from the single-session-per-
        # login conflict). A retry alone doesn't fix this — it needs
        # auto_login (see _check_booking_msc's retry-once-via-relogin
        # logic) to actually re-establish the session before trying
        # again. Surfacing this explicitly instead of letting essentials
        # extraction silently return None/None, which looked identical
        # to "found but empty" the first time this happened.
        return {"booking_id": booking_id, "found": False, "status": "session_expired"}

    essentials = _extract_booking_essentials(booking_text)
    passenger_info = _extract_passengers(booking_text)

    if _is_placeholder_departure(booking_text):
        return {
            "booking_id": booking_id,
            "found": True,
            "status": "cancelled_or_postponed_placeholder",
            "departure_year": _extract_departure_year(booking_text),
            "category": essentials["category"],
            "current_value": essentials["value"],
        }

    # ADDED 2026-08-12, real miss caught by Neon (booking 3000012): a
    # plain outright cancellation, not the far-future-placeholder kind
    # above. Checked BEFORE ever clicking "Book Same Departure" — a
    # cancelled booking's $0.00 data was previously fed straight through
    # the whole pipeline and produced a nonsense "opportunity" from
    # garbage numbers, a real actionable false positive, not just a
    # cosmetic miss. Also checks the structural `.BookingStatus` badge
    # (see _read_booking_status_badge) as a second, independent signal —
    # its CSS class alone ('StatusConfirmed') is confirmed misleading
    # even on a canceled booking, only the visible text can be trusted.
    status_badge = await _read_booking_status_badge(page)
    if _is_explicitly_cancelled(booking_text) or (status_badge and "CANCEL" in status_badge):
        return {
            "booking_id": booking_id,
            "found": True,
            "status": "explicitly_cancelled",
            "category": essentials["category"],
            "current_value": essentials["value"],
        }

    clicked = await page.evaluate(
        "(() => { const el = Array.from(document.querySelectorAll('button,a'))"
        ".find(e => e.textContent.trim() === 'Book Same Departure'); "
        "if (el) { el.click(); return true; } return false; })()"
    )
    discount_options = None
    club_discount_offered = None
    occupancy_fix = None
    # Same defaulting as occupancy_fix above: the return dict below sits
    # OUTSIDE `if clicked:`, so anything assigned only inside it would be a
    # NameError on the not-clicked path. Defaulting to an explicit
    # "never attempted" also keeps the meaning honest downstream - the
    # calculator refuses to price a club member whose discount was not
    # entered, and must not read a missing value as "no membership".
    voyagers_fix = {"applied": False, "member": None, "verified": False,
                    "reason": "never reached the discount screen"}
    if clicked:
        # "CONFIRM AND PROCEED" only ever renders once this screen has
        # actually loaded (confirmed against a real recorded session,
        # 2026-08-10) — it never appears on the original booking detail
        # page (whose action button is "BOOK SAME DEPARTURE" instead), so
        # it's a solid third marker alongside the discount-UI text.
        after_click = await _wait_for(
            "Select Special Discounts", "Additional Discounts", "CONFIRM AND PROCEED"
        )
        # Confirmed real false positive 2026-08-10 on booking 3000027: a
        # slow page load (not a genuine dead end — Neon manually clicked
        # through to a real category listing seconds later) left the poll
        # still on the original booking page, which happened to ALSO
        # contain the generic FCC banner text, and got misclassified as
        # fcc_placeholder_rebooking. One retry of the click, with a fresh
        # poll, before concluding anything failed.
        if not (
            "Select Special Discounts" in after_click
            or "Additional Discounts" in after_click
            or "CONFIRM AND PROCEED" in after_click
        ):
            clicked = await page.evaluate(
                "(() => { const el = Array.from(document.querySelectorAll('button,a'))"
                ".find(e => e.textContent.trim() === 'Book Same Departure'); "
                "if (el) { el.click(); return true; } return false; })()"
            )
            after_click = await _wait_for(
                "Select Special Discounts", "Additional Discounts", "CONFIRM AND PROCEED"
            )
        # MSC currently shows a generic "Future Cruise Credit" banner on
        # MANY bookings' pages (confirmed 2026-08-10: two bookings with this
        # exact banner still advanced fine to a real category listing) — so
        # the banner text alone is NOT a reliable signal. What actually
        # matters is whether the click moved the page at all: a genuinely
        # cancelled/placeholder booking's "Book Same Departure" click has
        # nowhere real to go, so the page just stays on the booking summary
        # (no occupancy markers ever appear, even after the poll timeout).
        occupancy_reached = (
            "Select Special Discounts" in after_click
            or "Additional Discounts" in after_click
            or "CONFIRM AND PROCEED" in after_click
        )
        if not occupancy_reached:
            if "placeholder sailing" in after_click or "January, 2049" in after_click:
                status = "fcc_placeholder_rebooking"
            else:
                status = "advance_failed"
            return {
                "booking_id": booking_id,
                "found": True,
                "status": status,
                "category": essentials["category"],
                "current_value": essentials["value"],
            }

        # Fix occupancy to match the real booking's passengers BEFORE
        # capturing anything price-related — see _fix_occupancy's
        # docstring for the real bug this closes (booking 3000024,
        # 2026-08-12: 3 kids silently dropped, dummy quote for 2 guests
        # got compared against the real 5-guest total).
        occupancy_fix = await _fix_occupancy(page, passenger_info["passengers"])

        # Apply the customer's own Voyagers membership so the prices
        # harvested next are comparable with their CURRENT total, which
        # already includes their discount. Skipping this is what made
        # every MSC booking look like "no opportunity" - see
        # _apply_voyagers_club for the confirmed 3000081 figures.
        voyagers_fix = await _apply_voyagers_club(
            page, passenger_info["passengers"])

        # Capture the "Additional Discounts" dropdown options while we're
        # on this screen — needed by calculator_msc.py's DISCOUNT_ADD/
        # DISCOUNT_TIER_UPGRADE checks. Deliberately does NOT select any
        # option or touch the Voyagers Club field — that would make the
        # category prices harvested later reflect a discount, breaking
        # the calculator's "today_base_price must be undiscounted"
        # assumption. Neon must also leave these alone when clicking
        # CONFIRM AND PROCEED for this same reason.
        try:
            raw_options = await page.evaluate(
                "(() => { const sel = Array.from(document.querySelectorAll('select'))"
                ".find(s => Array.from(s.options).some(o => /DISCOUNT|TODAY|MIL-CIV/.test(o.textContent))); "
                "return sel ? Array.from(sel.options).map(o => o.textContent.trim()).filter(Boolean) : null; })()"
            )
            if raw_options is not None:
                # The dropdown's own placeholder text ("Select Special
                # Discounts") comes back as a real <option> and isn't a
                # selectable discount — confirmed it appears in every
                # capture regardless of what's actually offered, so
                # leaving it in would make _check_discount_add() always
                # false-positive.
                seen = set()
                discount_options = []
                for opt in raw_options:
                    if opt.lower().startswith("select special discount") or opt in seen:
                        continue
                    seen.add(opt)
                    discount_options.append(opt)
            else:
                discount_options = None
        except Exception:
            discount_options = None

        # ADDED 2026-08-11, direct instruction from Neon: "always look
        # at this phrase as this is a big indicator" — the literal
        # on-page text "Club discount available, insert Voyagers Club
        # to activate." confirms the flat 5% Voyagers Club discount can
        # genuinely be added to THIS sailing/rate. Since staging never
        # fills in the crown/Voyagers field (deliberately, to keep
        # today's price undiscounted), this phrase reflects the RATE's
        # general eligibility, not the real booking's own current
        # status — that's tracked separately via current_discounts/SRN
        # math. Case-insensitive substring match on the core phrase
        # (not the exact full sentence) to stay robust to minor
        # punctuation/spacing differences, same lesson learned from
        # other text-matching brittleness already found this session.
        club_discount_offered = "club discount available" in after_click.lower()

    return {
        "booking_id": booking_id,
        "found": True,
        "status": "staged",
        "clicked_book_same_departure": clicked,
        "category": essentials["category"],
        "current_value": essentials["value"],
        "is_guaranteed": essentials.get("is_guaranteed", False),
        "rate_name": essentials.get("rate_name"),
        "is_group_rate": _is_group_rate(essentials.get("rate_name")),
        "all_seniors": passenger_info["all_seniors"],
        "senior_count": passenger_info["senior_count"],
        "has_voyagers": passenger_info["has_voyagers"],
        "discount_options": discount_options,
        "club_discount_offered": club_discount_offered,
        "occupancy_fix": occupancy_fix,
        # Cabin count, added 2026-08-27 — computed HERE because this is
        # where the real booking text is available. test_discount_candidate
        # needs it to refuse multi-cabin bookings (every selector in that
        # flow targets cabin 1 only, so a multi-cabin "savings" figure
        # would be wrong by roughly the other cabins).
        "cabin_count": _count_cabins(booking_text),
        # What happened when the customer's Voyagers membership was
        # entered, and therefore whether the prices harvested from this
        # screen already carry the 5% club discount. The calculator MUST
        # know this: comparing a discounted today-price against a
        # discounted current total is valid, comparing a list price
        # against a discounted total is the bug that hid every
        # opportunity.
        "voyagers_fix": voyagers_fix,
        "today_price_includes_club_discount": bool(voyagers_fix.get("applied")),
        # A SECOND, INDEPENDENT guest count - what the booking page itself
        # says, not what passenger-row extraction produced. Required by
        # msc_occupancy_is_trustworthy: counting from one source has now
        # priced two bookings wrong (3000024, three kids dropped, fake
        # $1,929.61; 3000081, one of two adults dropped, fake $267.01
        # against a real $81.98). Both counts were self-consistent and
        # wrong, so only a cross-check can catch it.
        "invoice_guest_count": msc_invoice_guest_count(booking_text, None),
    }


async def _confirm_and_proceed_click(page) -> bool:
    """Click 'CONFIRM AND PROCEED' (dismissing the 'Policy Reminder'
    popup first if it's showing — e.g. the senior-discount-eligibility
    notice, which sits on top of and blocks the real button).

    Extracted out of the confirm_and_proceed command so the exact same
    click logic can be reused by _check_booking_msc's fully-automated
    flow. SAFE to call from automation as of 2026-08-11: Neon added a
    narrowly-scoped autoMode.allow rule to his own ~/.claude/settings.json
    permitting exactly this click via this exact command/module,
    confirmed working live end-to-end (see msc_project_knowledge.md's
    'RESOLVED 2026-08-11' section) — this is no longer restricted to
    Neon's own manual confirm_and_proceed.ps1 trigger."""
    try:
        await page.click("#cabinSelectionDiscountMessageConfirmBtn", timeout=2000)
        await page.wait_for_timeout(500)
    except Exception:
        pass  # popup wasn't showing — fine, proceed directly
    try:
        clicked = await page.evaluate(
            "(() => { const btn = document.querySelector('.confirm-cabin[data-cabin=\"1\"]') "
            "|| Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'CONFIRM AND PROCEED'); "
            "if (btn) { btn.click(); return true; } return false; })()"
        )
    except Exception:
        return False
    if clicked:
        await page.wait_for_timeout(1500)
    return bool(clicked)


def generate_discount_candidates(staged: dict) -> list:
    """From evidence ALREADY captured during staging (discount_options,
    club_discount_offered, senior_count, is_group_rate — see
    _stage_booking_for_confirm), produce the list of individual discount
    candidates actually worth live-testing.

    Deliberately does NOT generate multi-discount combinations (Senior +
    Voyagers together, etc.) — per the explicit instruction not to assume
    two visible discounts can be stacked, and because this project has no
    live-proven evidence yet that even a SINGLE candidate's test pipeline
    is reliable end-to-end (the first live attempt, on 3000026, needed a
    real bug fix before it could read a price at all). Combination
    testing is real future work (see the roadmap), not something to
    guess at today. Every candidate returned here is a single, real,
    on-page-confirmed option — never invented.

    Military discounts are never generated, matching the same hard
    policy _filter_out_disallowed_discounts already enforces in
    core/calculator_msc.py (CruiseIntel does not apply them from the agency
    side regardless of what MSC's dropdown lists). Senior discount is only
    generated when the cabin has at least 2 senior (65+) passengers —
    confirmed hard rule, same as core/calculator_msc.py's
    _filter_out_ineligible_senior_discount (added 2026-08-18 after a real
    false positive on booking 3000030, a single 83-year-old traveling
    alone: MSC's own dropdown lists SENIOR DISCOUNT regardless of party
    composition, but it can't actually be applied to a lone senior)."""
    from core.models import MscDiscountApplicationMethod, MscDiscountCandidate

    if staged.get("is_group_rate"):
        # Confirmed hard rule (core/calculator_msc.py's own
        # _check_discount_add): Group Rate bookings are only ever
        # eligible for the flat 5% Voyagers Club discount, never the
        # dropdown tiers — do not generate dropdown candidates for them.
        candidates = []
    else:
        senior_count = staged.get("senior_count", 0)
        candidates = [
            MscDiscountCandidate(label=opt, method=MscDiscountApplicationMethod.DROPDOWN_OPTION)
            for opt in (staged.get("discount_options") or [])
            if "MIL-CIV" not in opt.upper() and "MILITARY" not in opt.upper()
            and (senior_count >= 2 or "SENIOR" not in opt.upper())
        ]

    # Voyagers Club insertion is intentionally NOT added here — it needs
    # a real member's name/DOB/card number, which requires passenger data
    # this function doesn't receive (see test_voyagers_discount's command
    # handler, which builds that candidate directly from
    # _extract_passengers' output). Signalling eligibility here would
    # invite a caller to construct one without real passenger data.
    return candidates


async def _apply_discount_candidate(page, candidate) -> dict:
    """Actually select ONE discount candidate on the occupancy screen —
    the step this project's automated flow has never done before. Reuses
    the exact same generic <select>-search technique the existing
    `select_option_label` manual command already uses (DROPDOWN_OPTION),
    or the exact selectors already confirmed working in a real manual
    session (VOYAGERS_CLUB_INSERT — see msc_project_knowledge.md) — no
    new browser-control mechanism, per the explicit instruction not to
    build a second one.

    Returns {"success": bool, "reason": str}. Never raises — a failed
    selection must be reported to the caller as INSUFFICIENT_DATA, not
    crash the whole check.

    Does not itself reload core.models (see test_discount_candidate's own
    comment on why that's needed in a long-lived controller process) —
    every real caller goes through test_discount_candidate, which always
    reloads first; this function only needs the already-fresh class for
    a value comparison (str-Enum equality is value-based, not identity-
    based, so this is safe even if somehow called before a reload)."""
    from core.models import MscDiscountApplicationMethod

    if candidate.method == MscDiscountApplicationMethod.DROPDOWN_OPTION:
        try:
            selects = page.locator("select")
            count = await selects.count()
            for i in range(count):
                sel = selects.nth(i)
                options = await sel.locator("option").all_text_contents()
                if candidate.label in [o.strip() for o in options]:
                    await sel.select_option(label=candidate.label, force=True, timeout=10000)
                    await page.wait_for_timeout(1000)
                    # READ-BACK VERIFICATION, added 2026-08-27 (Priority 3).
                    # This used to return success immediately after the call
                    # above. `force=True` deliberately SKIPS Playwright's
                    # actionability checks, so a hidden/disabled/detached
                    # <select> is "selected" as far as Playwright is
                    # concerned while MSC's own JS may never register it —
                    # and the caller then attributed a price delta to a
                    # discount that was never applied. Read the element's
                    # ACTUAL selected label back out of the DOM and require
                    # it to match.
                    try:
                        selected_label = await sel.evaluate(
                            "el => el.selectedIndex >= 0 "
                            "? (el.options[el.selectedIndex].textContent || '').trim() : ''"
                        )
                    except Exception as e:
                        return {
                            "success": False,
                            "reason": f"could not read back the selection to verify it "
                                      f"(so it cannot be trusted): {e}",
                        }
                    if (selected_label or "").strip() != candidate.label.strip():
                        return {
                            "success": False,
                            "reason": f"selection did NOT take: asked for "
                                      f"{candidate.label!r} but the dropdown still reads "
                                      f"{selected_label!r} — MSC did not accept it",
                        }
                    return {
                        "success": True,
                        "reason": f"selected {candidate.label!r} (verified by read-back)",
                        "verified_selection": selected_label,
                    }
            return {"success": False, "reason": f"no <select> found with option labeled {candidate.label!r} ({count} selects checked)"}
        except Exception as e:
            return {"success": False, "reason": f"dropdown selection failed: {e}"}

    if candidate.method == MscDiscountApplicationMethod.VOYAGERS_CLUB_INSERT:
        # Real passenger identity is REQUIRED here — MSC's own member-
        # lookup form needs a real name/DOB/card number to search against.
        # Never invent these; the caller must supply them from this
        # booking's own already-scraped passenger data.
        if not all([candidate.voyagers_first_name, candidate.voyagers_last_name,
                    candidate.voyagers_dob, candidate.voyagers_card_number]):
            return {"success": False, "reason": "VOYAGERS_CLUB_INSERT requires real passenger first/last name, DOB, and card number — none may be guessed"}
        try:
            clicked = await page.evaluate(
                f"(() => {{ const btn = document.querySelector('.club-btn[data-cabin=\"{candidate.cabin_number}\"]'); "
                "if (btn) { btn.click(); return true; } return false; })()"
            )
            if not clicked:
                return {"success": False, "reason": f".club-btn[data-cabin=\"{candidate.cabin_number}\"] not found on this page"}
            await page.wait_for_timeout(500)
            await page.fill("#club-firstname", candidate.voyagers_first_name, timeout=5000)
            await page.fill("#club-lastname", candidate.voyagers_last_name, timeout=5000)
            await page.fill("#club-dob", candidate.voyagers_dob, timeout=5000)
            await page.fill("#club-card", candidate.voyagers_card_number, timeout=5000)
            await page.evaluate("document.querySelector('.club-search-btn')?.click()")

            # OUTCOME VERIFICATION, added 2026-08-27 (Priority 3).
            # This used to be `wait_for_timeout(1500)` followed by an
            # UNCONDITIONAL `success: True` — so a member lookup that came
            # back "not found" was recorded as a successfully applied
            # discount, and the caller then reported a price delta as a
            # confirmed Voyagers Club saving. A fixed sleep is not evidence
            # of anything.
            #
            # Polls for a real outcome instead, and FAILS CLOSED: if
            # neither a success nor a failure marker appears within the
            # window, that is reported as a failure ("could not confirm"),
            # never as success. Marker text is matched loosely because the
            # exact wording is only partly confirmed from one real manual
            # session — an unrecognized outcome therefore lands in the
            # fail-closed branch by design, which is the safe direction.
            _FAIL_MARKERS = (
                "not found", "no member", "no record", "invalid",
                "does not match", "try again", "unable to",
            )
            _OK_MARKERS = (
                "club discount", "member found", "discount applied",
                "voyagers club number",
            )
            deadline = time.monotonic() + 12.0
            last_seen = ""
            while time.monotonic() < deadline:
                await page.wait_for_timeout(500)
                try:
                    body = (await page.inner_text("body")).lower()
                except Exception:
                    continue
                last_seen = body
                if any(m in body for m in _FAIL_MARKERS):
                    matched = next(m for m in _FAIL_MARKERS if m in body)
                    return {
                        "success": False,
                        "reason": f"MSC rejected the Voyagers Club member lookup "
                                  f"(page reported {matched!r}) — the discount was NOT applied",
                    }
                if any(m in body for m in _OK_MARKERS):
                    matched = next(m for m in _OK_MARKERS if m in body)
                    return {
                        "success": True,
                        "reason": f"Voyagers Club member accepted (page confirmed {matched!r})",
                        "verified_selection": matched,
                    }
            return {
                "success": False,
                "reason": "submitted the Voyagers Club member details but MSC showed "
                          "neither a success nor a failure within 12s — failing closed "
                          "rather than assuming it worked "
                          f"(page text was {len(last_seen)} chars)",
            }
        except Exception as e:
            return {"success": False, "reason": f"Voyagers Club insert failed: {e}"}

    return {"success": False, "reason": f"unknown application method {candidate.method!r}"}


_PART_NUMBER_RE = re.compile(r"partNumber=([A-Za-z0-9]+)")
_TOTAL_STATEROOM_PRICE_RE = re.compile(r"Total Stateroom Price:\s*\$?\s*([\d,]+\.\d{2})")


async def _wait_for_post_discount_price(page, category: str, is_guaranteed: bool,
                                         timeout_s: float = 10.0, poll_s: float = 0.5) -> dict:
    """Poll for ANY usable price evidence after confirming a discount
    selection — CONFIRMED REAL BUG, fixed 2026-08-13: the original code
    required a rate/promo tab to be found (`.cs-price-code-box`) as the
    ONLY path to a price, and returned INSUFFICIENT_DATA the instant that
    specific DOM structure was absent, even though a real live test on
    3000026 proved MSC can accept and price a discount selection while
    rendering a DIFFERENT page structure with no tabs at all. Rate tabs
    are one possible validation signal, not a requirement — this tries
    multiple real, evidence-based strategies and only gives up after a
    genuine timeout, using condition-based polling (not a fixed sleep)
    since discount selection may trigger a slower recalculation than the
    no-discount case this pattern was originally tuned for.

    Strategies tried each poll, in order:
      1. The same category-listing price format check_booking's own
         harvest step already trusts (_find_today_price) — works
         whenever rate tabs (or any equivalent per-category listing) are
         present.
      2. "Total Stateroom Price: $X" — the literal, confirmed-real Price
         Breakdown line format (see this project's own forensic capture
         of booking 3000026's breakdown_text) — a fallback in case the
         resulting page shows a breakdown-style total instead of (or in
         addition to) a category-listing row.

    Returns {"price_str": str|None, "source": str|None, "rate_tab_confirmed":
    bool|None, "text_excerpt": str} — never raises, never guesses; a
    timeout with nothing found returns price_str=None for the caller to
    report as POST_PRICE_NOT_FOUND or RECALCULATION_TIMEOUT."""
    import time as _time
    deadline = _time.monotonic() + timeout_s
    last_text = ""
    while True:
        text = await page.inner_text("body")
        last_text = text

        # Tab confirmation is attempted separately by the caller (it needs
        # the real rate_name) — this helper stays rate-name-agnostic so it
        # can be unit-tested purely against page text.
        price_str = _find_today_price(text, category, is_guaranteed)
        if price_str is not None:
            return {"price_str": price_str, "source": "category_listing", "text_excerpt": text[:2000]}

        m = _TOTAL_STATEROOM_PRICE_RE.search(text)
        if m:
            return {"price_str": m.group(1), "source": "price_breakdown", "text_excerpt": text[:2000]}

        if _time.monotonic() >= deadline:
            return {"price_str": None, "source": None, "text_excerpt": last_text[:2000]}
        await page.wait_for_timeout(int(poll_s * 1000))


async def test_discount_candidate(state: dict, booking_id: str, candidate, page=None) -> "MscDiscountTestResult":
    """Determine a discount's REAL dollar effect by actually selecting it
    on MSC's own occupancy screen and reading MSC's own recalculated
    price — never an assumed percentage times the current total.

    CONFIRMED REAL GAP this closes (forensic investigation, bookings
    3000026/74242969): evaluate_msc_booking()'s DISCOUNT_ADD/
    DISCOUNT_TIER_UPGRADE checks only ever detect that a discount is
    ELIGIBLE — they never apply it, so a booking can show `OPPORTUNITY`
    with no dollar figure attached (DISCOUNT_ADD never sets
    estimated_value at all) or a hardcoded assumed rate ("Voyagers Club
    5%" is a literal string in _check_discount_add, not read from the
    page). MSC's own backend represents at least one real discount type
    (Senior) as a non-literal, dynamically-computed rate (discRate='0',
    isInv=true in DiscountPaxTypeCmd) — there is no percentage anywhere
    to parse for it. The only authoritative source of truth is MSC's own
    recalculated price after the discount is actually selected.

    CONFIRMED REAL BUG, fixed 2026-08-13 (first live test, booking
    3000026): every field was previously reset to its model default on
    ANY early return, because each failure path built a brand-new
    MscDiscountTestResult from scratch. `_evidence` below accumulates
    everything actually established as the pipeline proceeds; every
    return path builds the final result FROM `_evidence`, so a later
    failure can never erase an earlier success.

    SAFETY: this reuses the exact "Book Same Departure" duplicate/preview
    flow and the exact "CONFIRM AND PROCEED" click that check_booking/
    check_booking_batch already run, unattended, for every booking this
    project has ever checked (confirmed pre-authorized for automation —
    see _confirm_and_proceed_click's own docstring) — no new commit-style
    action is introduced. This function adds exactly one new step
    (selecting a discount) inside that same already-proven-safe flow.
    Never clicks any actual save/payment/purchase control anywhere in
    MSC's UI — those remain permanently reserved for a human, unchanged
    by this addition."""
    # CONFIRMED REAL RISK, caught before first live use 2026-08-13: the
    # controller process this runs inside stays alive for days
    # (msc_session_controller.py never restarts between commands — that's
    # the whole point, see its own module docstring), so its cached
    # sys.modules['core.models']/['core.calculator'] can predate any of
    # THIS conversation's edits, including the very existence of the
    # classes imported below. _check_booking_msc already reloads
    # core.models/core.calculator_msc for exactly this reason — but not
    # core.calculator itself, which core.calculator_msc imports FROM
    # (`from .calculator import round2, safe_float`), so a stale
    # core.calculator can silently survive even that existing reload.
    # Reload both explicitly here rather than assume either is fresh.
    import core.models
    import core.calculator
    importlib.reload(core.models)
    importlib.reload(core.calculator)
    from core.calculator import round2
    from core.models import MscDiscountTestResult, MscDiscountTestStatus

    if page is None:
        page = state["page"]

    # Accumulates every value actually established, in order, so no
    # early return can ever discard evidence from a step that already
    # succeeded — see this function's own "CONFIRMED REAL BUG" note above.
    _evidence = {
        "price_before": None, "price_after": None, "actual_savings": None,
        "price_source": None,
        "occupancy": None, "category": None, "rate_tab_confirmed": None,
        "application_attempted": False, "application_success": False,
        "confirm_attempted": False, "confirm_success": False,
        "restoration_attempted": False, "restoration_verified": False,
    }

    def _result(status, reason: str) -> "MscDiscountTestResult":
        return MscDiscountTestResult(
            booking_id=booking_id, candidate_label=candidate.label, method=candidate.method,
            status=status, reason=reason, **_evidence,
        )

    try:
        # 1. BASELINE — a fresh, direct lookup of the REAL booking (not
        # the duplicate/preview flow) is the only trustworthy "before"
        # price.
        baseline_data = await _lookup_one_booking(page, booking_id)
        if baseline_data.get("session_expired"):
            # Same one-retry-via-relogin pattern _check_booking_msc already
            # uses — auto_login() runs in the SAME already-open browser
            # using saved credentials; it does not start, restart, or kill
            # any session. Added 2026-08-13 after a live retest of this
            # exact pipeline hit a genuinely expired session (unrelated to
            # any bug in this function) and had no way to recover.
            await auto_login(page)
            baseline_data = await _lookup_one_booking(page, booking_id)
            if baseline_data.get("session_expired"):
                return _result(MscDiscountTestStatus.INSUFFICIENT_DATA, "session still expired after one relogin attempt")
        if "No bookings found" in (baseline_data.get("summary_text") or ""):
            return _result(MscDiscountTestStatus.INSUFFICIENT_DATA, "booking not found")
        baseline_essentials = _extract_booking_essentials(baseline_data["summary_text"])
        price_before = _parse_dollars_safe(baseline_essentials.get("value"))
        if price_before is None:
            return _result(MscDiscountTestStatus.INSUFFICIENT_DATA, "could not read a real baseline price for this booking")
        _evidence["price_before"] = price_before
        _evidence["category"] = baseline_essentials.get("category")
        baseline_part_number_match = _PART_NUMBER_RE.search(page.url)
        baseline_part_number = baseline_part_number_match.group(1) if baseline_part_number_match else None

        # 2. STAGE — reuses the exact same "Book Same Departure" flow
        # every existing check already runs.
        staged = await _stage_booking_for_confirm(page, booking_id)
        if staged.get("status") == "session_expired":
            return _result(MscDiscountTestStatus.INSUFFICIENT_DATA, "session expired during staging")
        if not staged.get("found") or staged.get("status") in (
            "cancelled_or_postponed_placeholder", "explicitly_cancelled",
            "fcc_placeholder_rebooking", "advance_failed",
        ):
            return _result(MscDiscountTestStatus.INSUFFICIENT_DATA, f"could not stage this booking for testing (status={staged.get('status')!r})")
        _evidence["category"] = staged.get("category") or _evidence["category"]
        _evidence["occupancy"] = staged.get("occupancy_fix")

        # OCCUPANCY — price identity includes occupancy (see
        # calculator_msc.py's own long-standing "price and discount are
        # independent levers" principle, extended here to "occupancy and
        # discount are independent too"). If the auto-fix itself couldn't
        # converge on a stable occupancy, any price read afterward isn't
        # safely comparable to baseline.
        occ = staged.get("occupancy_fix") or {}
        if occ.get("stalled"):
            return _result(MscDiscountTestStatus.OCCUPANCY_MISMATCH, "occupancy auto-fix did not converge before staging — a price test here would not be comparable to baseline")

        # OCCUPANCY, second distinct failure mode — added 2026-08-27.
        # `_fix_occupancy` has TWO bad outcomes, not one: `stalled` (above)
        # and `skipped_empty_passengers`, which means passenger extraction
        # FAILED so occupancy was left at whatever MSC auto-filled (adults
        # only). This path only checked `stalled`, so the second one sailed
        # straight through. That is exactly the confirmed 2026-08-12
        # booking-3000024 bug (3 kids silently dropped -> a "$1,929.61
        # opportunity" that was really just missing passengers)
        # reintroduced into the discount path, where the output is a dollar
        # figure labelled CONFIRMED. `_check_booking_msc` already surfaces
        # this as a loud warning; this path printed it nowhere.
        if occ.get("skipped_empty_passengers"):
            return _result(
                MscDiscountTestStatus.OCCUPANCY_MISMATCH,
                "passenger extraction failed so occupancy was left at MSC's auto-filled "
                "default (adults only) — any price read here may be for the wrong number "
                "of guests and is not comparable to baseline",
            )

        # MULTI-CABIN GUARD — added 2026-08-27 (Priority 4).
        # Every occupancy/confirm selector in this file is hardcoded
        # `data-cabin="1"`, `_apply_discount_candidate` takes the FIRST
        # matching <select> (cabin 1's) and ignores candidate.cabin_number
        # entirely, and `_extract_booking_essentials`' category regex
        # matches only the first `Cabin N -` line. So on a 2-cabin
        # booking the baseline value covers BOTH cabins while the discount
        # is applied to one and the post-price is one stateroom — the
        # reported "savings" is roughly half the booking, presented as
        # confirmed. Nothing anywhere detected cabin count.
        #
        # Refuses rather than guesses, per this project's standing rule.
        # Comes from _stage_booking_for_confirm, which computes it from the
        # real booking text (staged has no raw summary_text key).
        cabin_count = staged.get("cabin_count") or 0
        _evidence["cabin_count"] = cabin_count
        if cabin_count > 1:
            return _result(
                MscDiscountTestStatus.INSUFFICIENT_DATA,
                f"this booking has {cabin_count} cabins, and every discount-test "
                f"selector in this flow targets cabin 1 only — the baseline value "
                f"covers all cabins while the discounted price would cover one, so "
                f"any 'savings' figure would be wrong by roughly the other cabins. "
                f"Check this booking by hand.",
            )

        # ELIGIBILITY GATE — added 2026-08-27 (Priority 2).
        # THE ROOT CAUSE, fixed here rather than at the command handler so
        # EVERY caller is covered: `generate_discount_candidates()` holds
        # all the hard eligibility policy (military/MIL-CIV never applied,
        # senior discount requires 2+ seniors, Group Rate capped to the
        # flat Voyagers 5% with no dropdown tiers) but a repo-wide grep
        # confirmed it was called ONLY from tests — never from production.
        # The live `test_discount:<id>:<label>` command built a candidate
        # straight from typed text, so `test_discount:3000030:SENIOR
        # DISCOUNT` on a lone senior — the exact 2026-08-18 false positive
        # that was fixed in the calculator — ran here completely unguarded
        # and produced a CONFIRMED_OPTIMIZATION dollar figure, which looks
        # MORE authoritative than the calculator note that was corrected.
        #
        # Validated against the SAME on-page evidence the tested path uses
        # (staged discount_options / senior_count / is_group_rate), so the
        # two paths cannot drift apart.
        #
        # Voyagers Club INSERT candidates are exempt: by design
        # generate_discount_candidates never emits them (it has no
        # passenger data), so they'd always fail this check. They have
        # their own separate command handler and their own real-member
        # requirement.
        method_value = getattr(getattr(candidate, "method", None), "value", "")
        if method_value != "VOYAGERS_CLUB_INSERT":
            # Distinguish the two failure shapes — they mean different
            # things to the operator and this file's None-vs-[] discipline
            # exists precisely to keep them apart:
            #   discount_options is None -> we never CAPTURED the dropdown
            #     (a scrape problem: MSC's own offers are unknown).
            #   discount_options is a list -> we know what's on offer and
            #     this label isn't eligible (a policy answer).
            # Either way we refuse — if we don't know what MSC offers we
            # cannot confirm eligibility — but the operator needs to know
            # which one it was.
            if staged.get("discount_options") is None:
                logger.warning(
                    "msc.discount_options_not_captured",
                    booking_id=booking_id, requested=candidate.label,
                )
                return _result(
                    MscDiscountTestStatus.INSUFFICIENT_DATA,
                    f"cannot verify {candidate.label!r} is eligible: this booking's "
                    f"discount dropdown was never captured during staging, so MSC's "
                    f"actual offers are unknown. Refusing to apply an unverified "
                    f"discount rather than guessing.",
                )
            allowed = generate_discount_candidates(staged)
            allowed_labels = {
                (getattr(c, "label", "") or "").strip().upper() for c in allowed
            }
            requested = (getattr(candidate, "label", "") or "").strip().upper()
            if requested not in allowed_labels:
                logger.warning(
                    "msc.discount_candidate_rejected_ineligible",
                    booking_id=booking_id, requested=requested,
                    allowed=sorted(allowed_labels),
                    senior_count=staged.get("senior_count"),
                    is_group_rate=staged.get("is_group_rate"),
                )
                return _result(
                    MscDiscountTestStatus.INSUFFICIENT_DATA,
                    f"{candidate.label!r} is NOT an eligible discount for this booking "
                    f"and was refused before any price was read. Eligible options here: "
                    f"{sorted(allowed_labels) or 'none'}. "
                    f"(senior_count={staged.get('senior_count')}, "
                    f"is_group_rate={staged.get('is_group_rate')} — senior discount needs "
                    f"2+ seniors, military/MIL-CIV is never applied by CruiseIntel, and "
                    f"Group Rate bookings are capped at the flat Voyagers 5%.)",
                )

        # 3. APPLY — the one genuinely new step.
        _evidence["application_attempted"] = True
        applied = await _apply_discount_candidate(page, candidate)
        if not applied["success"]:
            return _result(MscDiscountTestStatus.DISCOUNT_APPLICATION_FAILED, f"discount selection failed: {applied['reason']}")
        _evidence["application_success"] = True

        # 4. CONFIRM/ADVANCE — the exact click already pre-authorized and
        # already run, unattended, for every booking this project checks.
        _evidence["confirm_attempted"] = True
        clicked = await _confirm_and_proceed_click(page)
        if not clicked:
            return _result(MscDiscountTestStatus.CONFIRM_FAILED, "could not advance past the occupancy screen after selecting the discount")
        _evidence["confirm_success"] = True

        # 5. READ THE RECALCULATED PRICE — CONFIRMED REAL BUG, fixed
        # 2026-08-13: this used to hard-require a matched rate/promo tab
        # before EVER attempting to read a price. Tab confirmation is now
        # an independent, best-effort confidence signal, not a gate —
        # price and discount remain independent levers (same principle
        # _check_price_match already applies), so a discount-selection
        # page that renders without tabs at all is not, by itself,
        # evidence that no price is available.
        rate_tab_match = await _match_rate_tab(page, staged.get("rate_name")) if staged.get("rate_name") else {"matched": False, "reason": "no rate_name known"}
        _evidence["rate_tab_confirmed"] = bool(rate_tab_match.get("matched"))

        price_evidence = await _wait_for_post_discount_price(
            page, staged["category"], staged.get("is_guaranteed", False),
        )
        if price_evidence["price_str"] is None:
            return _result(
                MscDiscountTestStatus.POST_PRICE_NOT_FOUND,
                "no usable price evidence found after applying the discount and confirming "
                f"(rate tab confirmed: {_evidence['rate_tab_confirmed']}); page text sample: "
                f"{price_evidence['text_excerpt'][:300]!r}",
            )
        price_after = _parse_dollars_safe(price_evidence["price_str"])
        if price_after is None or price_after < 0:
            return _result(MscDiscountTestStatus.POST_PRICE_INVALID, f"recalculated price {price_evidence['price_str']!r} could not be parsed as a valid amount")
        _evidence["price_after"] = price_after
        _evidence["price_source"] = price_evidence.get("source")

        # IDENTITY VALIDATION — same sailing fingerprint pattern already
        # trusted elsewhere in this file (_check_booking_msc's own
        # partNumber cross-check, added after the real 2026-08-10 multi-
        # tab cookie-conflict incident). If it changed mid-experiment,
        # the price we just read may not even belong to this booking.
        current_part_number_match = _PART_NUMBER_RE.search(page.url)
        current_part_number = current_part_number_match.group(1) if current_part_number_match else None
        if baseline_part_number and current_part_number and baseline_part_number != current_part_number:
            return _result(
                MscDiscountTestStatus.IDENTITY_VALIDATION_FAILED,
                f"sailing identity changed mid-experiment (partNumber {baseline_part_number!r} -> {current_part_number!r}) — never trust this price",
            )

        # 6. RESTORATION VERIFICATION — the "Book Same Departure" preview
        # flow is never submitted/saved/paid (this function never clicks
        # any such control), so nothing should have changed on the REAL
        # booking. Prove that directly with a fresh, independent
        # re-lookup, rather than assuming it.
        _evidence["restoration_attempted"] = True
        verification_data = await _lookup_one_booking(page, booking_id)
        verification_essentials = _extract_booking_essentials(verification_data.get("summary_text") or "")
        restoration_verified = (
            not verification_data.get("session_expired")
            and verification_essentials.get("value") == baseline_essentials.get("value")
            and verification_essentials.get("category") == baseline_essentials.get("category")
        )
        if not restoration_verified:
            logger.error(
                "msc.discount_test_restoration_failed",
                booking_id=booking_id, baseline=baseline_essentials, verification=verification_essentials,
            )
            return _result(
                MscDiscountTestStatus.RESTORATION_FAILED,
                "the real booking's own Booking Value/category no longer match the pre-test baseline — "
                "STOP processing this booking, do not trust actual_savings, needs human review immediately",
            )
        _evidence["restoration_verified"] = True

        # 7. COMPARE — only reached once every step above is verified.
        #
        # ⚠ KNOWN MEASUREMENT DEFECT — INVESTIGATED 2026-08-27, DELIBERATELY
        # NOT YET CHANGED (a fix here alters real reported dollars, so it
        # needs the project owner's explicit sign-off first).
        #
        # TRACED FACTS:
        #   price_before  = _parse_dollars_safe(baseline_essentials["value"])
        #                   i.e. the REAL booking's "Booking Value" — the
        #                   locked-in, ALREADY-DISCOUNTED total from when the
        #                   booking was made (read via _lookup_one_booking).
        #   price_after   = _find_today_price(...) on the dummy "Book Same
        #                   Departure" listing — TODAY's price, discount
        #                   applied.
        #
        # So this subtraction conflates TWO independent quantities:
        #   (a) market movement between the booking date and today, and
        #   (b) the discount's actual effect.
        # Only (b) is what a "discount saves you $X" claim means, but the
        # whole delta is reported as (b) and labelled CONFIRMED_OPTIMIZATION.
        #
        # This module's own sibling, core/calculator_msc.py, states the rule
        # being broken here in its `current_base_price` docstring:
        # comparing a discounted current price against an undiscounted
        # today's price is "an apples-to-oranges trap." _check_price_match
        # respects it; this path does not.
        #
        # CORROBORATION from this project's own ground-truth test: booking
        # 3000026, SENIOR DISCOUNT, $2,588.72 -> $2,565.26 = $23.46, which
        # is 0.906%. No senior discount is 0.906% — that figure is a
        # composition/market delta, not a discount.
        #
        # WHERE A VALID CONTROL COULD COME FROM (established, not guessed):
        # _check_booking_msc already reads exactly the number needed — an
        # UNDISCOUNTED today's price for the SAME category — at
        # msc_commands.py's `today_price = _find_today_price(listing_text,
        # staged["category"], ...)`, using the SAME function on the SAME
        # kind of page. No new measurement mechanism is required.
        #
        # THE OBSTACLE: the discount is applied on the occupancy screen
        # BEFORE _confirm_and_proceed_click, while prices only become
        # readable on the listing AFTER it. So the undiscounted listing
        # price cannot be read in the same pass — by the time a price
        # exists, the discount is already on.
        #
        # OPTIONS (for the project owner to choose):
        #   (a) TWO PASSES: pass 1 = stage+confirm with NO discount ->
        #       control (identical to what _check_booking_msc already
        #       produces); pass 2 = stage+confirm WITH the discount ->
        #       treatment. actual_savings = control - treatment. Both reads
        #       come from the same function and page type, so they are
        #       directly comparable. Cost: two stage+confirm cycles per
        #       candidate.
        #   (b) REUSE a recent _check_booking_msc result for the same
        #       booking/category/rate-tab as the control. Cheaper, but
        #       introduces a staleness window that would need a bound.
        #   (c) Read a pre-discount price on the occupancy screen itself.
        #       UNVERIFIED — _wait_for_post_discount_price's known sources
        #       (category listing, "Total Stateroom Price") both appear
        #       post-confirm, so this may not be available at all. Would
        #       need a live check on a real booking to establish.
        #
        # WHICHEVER is chosen, two conditions must hold or the comparison
        # is invalid again: both reads must have the SAME `price_source`
        # (the field already exists and nothing currently checks it), and
        # the SAME rate tab must be active for both.
        actual_savings = round2(price_before - price_after)
        _evidence["actual_savings"] = actual_savings
        final_status = (
            MscDiscountTestStatus.CONFIRMED_OPTIMIZATION if actual_savings > 0
            else MscDiscountTestStatus.CONFIRMED_NO_SAVINGS
        )
        return _result(
            final_status,
            f"MSC recalculated this booking's own category ({staged['category']}) from "
            f"${price_before:.2f} to ${price_after:.2f} (rate tab confirmed: {_evidence['rate_tab_confirmed']})",
        )
    except Exception as e:
        logger.error("msc.discount_test_unexpected_error", booking_id=booking_id, error=str(e))
        return _result(MscDiscountTestStatus.ERROR, f"unexpected error: {e}")


async def _check_booking_msc(state: dict, booking_id: str, page=None) -> dict:
    """Fully automated, single-call check for one booking: lookup ->
    stage -> confirm -> harvest -> evaluate. This is the single-entry-
    point flow the manual stage_booking/confirm_and_proceed/
    harvest_staged_booking sequence was building toward — safe to run in
    a loop across many bookings completely unattended, since the
    permission-rule fix (2026-08-11) removed the last human-click
    requirement.

    `page` defaults to state["page"] (the single-tab case, used by the
    check_booking/check_booking_batch commands). ADDED 2026-08-11 for
    real 2-tab concurrency (check_booking_batch2): pass an explicit page
    so two calls can run truly concurrently via asyncio.gather, each
    fully owning its own tab for the duration of its own call.

    Handles exactly one session-expiry retry via relogin — if the
    session is still dead after that, gives up on this booking rather
    than looping forever, so a batch run degrades to 'skip this one'
    instead of hanging.

    Returns a dict always containing 'booking_id' and 'status'; on
    status == 'checked', also contains 'result' (an MscBookingResult)
    and 'rate_tab_match'."""
    if page is None:
        page = state["page"]

    # Tracked PER PAGE (id(page) -> booking_id), not as one shared
    # state["current_staging_booking_id"] value — required for real
    # 2-tab concurrency: two _check_booking_msc calls now genuinely run
    # at the same time (via asyncio.gather in check_booking_batch2), so
    # a single shared value would be a live race condition where
    # responses from tab A's booking get tagged with whatever booking
    # tab B happens to be on at that instant. The lambda binds `page` via
    # a default arg (p=page) so each registration is stable per-tab.
    state.setdefault("current_staging_booking_id_by_page", {})[id(page)] = booking_id
    # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness audit):
    # discount_catalog_by_booking[booking_id] used to never be cleared
    # between checks of the SAME booking_id — if this specific check's
    # DiscountPaxTypeCmd capture fails or lags (network hiccup, MSC not
    # firing it this time), the poll loop further down would instantly
    # "succeed" against a stale entry from a much earlier, possibly very
    # different, check of this same booking, with no signal that it's
    # stale. Clearing it here — the moment this booking's fresh check
    # actually begins, before lookup/staging ever starts — guarantees
    # that if a fresh capture doesn't arrive this time, the entry stays
    # genuinely absent (-> None -> correctly read as "not captured this
    # time") rather than silently falling back to old data.
    state.setdefault("discount_catalog_by_booking", {}).pop(booking_id, None)
    attached_pages = state.setdefault("capture_listener_attached_pages", set())
    if id(page) not in attached_pages:
        os.makedirs(os.path.dirname(NETWORK_CAPTURE_PATH), exist_ok=True)
        page.on(
            "response",
            lambda r, p=page: track_background_task(
                state.setdefault("_background_tasks", set()),
                asyncio.create_task(
                    _capture_msc_response(
                        r,
                        state.get("current_staging_booking_id_by_page", {}).get(id(p), "unknown"),
                        NETWORK_CAPTURE_PATH,
                        state,
                    )
                ),
            ),
        )
        attached_pages.add(id(page))

    booking_data = await _lookup_one_booking(page, booking_id)
    if booking_data.get("session_expired"):
        await auto_login(page)
        booking_data = await _lookup_one_booking(page, booking_id)
        if booking_data.get("session_expired"):
            return {"booking_id": booking_id, "status": "session_expired_after_relogin"}

    os.makedirs(os.path.dirname(BOOKING_DATA_PATH), exist_ok=True)
    with open(BOOKING_DATA_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(booking_data, ensure_ascii=False) + "\n")

    if "No bookings found" in (booking_data.get("summary_text") or ""):
        return {"booking_id": booking_id, "status": "not_found"}
    if booking_data.get("cancelled_or_postponed_placeholder"):
        return {"booking_id": booking_id, "status": "cancelled_or_postponed_placeholder"}
    if booking_data.get("explicitly_cancelled"):
        return {"booking_id": booking_id, "status": "explicitly_cancelled"}

    staged = await _stage_booking_for_confirm(page, booking_id)
    if staged.get("status") == "session_expired":
        await auto_login(page)
        staged = await _stage_booking_for_confirm(page, booking_id)
        if staged.get("status") == "session_expired":
            return {"booking_id": booking_id, "status": "session_expired_after_relogin"}

    if not staged.get("found"):
        return {"booking_id": booking_id, "status": "not_found"}
    if staged.get("status") in (
        "cancelled_or_postponed_placeholder",
        "explicitly_cancelled",
        "fcc_placeholder_rebooking",
        "advance_failed",
    ):
        return {"booking_id": booking_id, "status": staged["status"], "category": staged.get("category")}

    # Sailing-identity fingerprint, captured right after staging lands on
    # the real occupancy screen (CabinSelectionView?...&partNumber=X,
    # where X encodes ship+date+route). Checked again after Confirm+
    # harvest below — this is the concrete, automatic version of the
    # "cross-check itinerary before trusting it" lesson from the 2026-08-
    # 10 multi-tab cookie-conflict incident (a tab silently showed a
    # completely unrelated sailing/passenger with no visible error).
    # Real risk with 2-tab concurrency specifically, not fully eliminated
    # by the per-page id-tracking above — that fix stops OUR OWN CODE
    # from mislabeling data, but can't stop MSC's backend itself from
    # serving a session-confused response under real concurrent load.
    part_number_match = re.search(r"partNumber=([A-Za-z0-9]+)", page.url)
    expected_part_number = part_number_match.group(1) if part_number_match else None

    clicked = await _confirm_and_proceed_click(page)
    if not clicked:
        return {"booking_id": booking_id, "status": "confirm_button_not_found", "category": staged.get("category")}

    # CONFIRMED REAL BUG, first live batch run 2026-08-11: waiting on
    # discount_catalog_by_booking here is NOT a reliable proxy for "the
    # post-Confirm category listing has rendered" — DiscountPaxTypeCmd
    # fires on the OCCUPANCY SCREEN'S load (during staging), not after
    # Confirm, so that dict entry is already populated before this point
    # and the loop breaks instantly, giving the page zero real time to
    # navigate. Result: three bookings in a row captured the STILL-ON-
    # OCCUPANCY-SCREEN page (still showing "Additional Discounts as
    # applicable") instead of the category/price grid, so today_price
    # came back empty on all three. Poll for the actual listing content
    # instead, same lesson as _stage_booking_for_confirm's _wait_for.
    listing_text = await page.inner_text("body")
    for _ in range(16):
        if (
            "CRU_034" in listing_text
            or "No data found for the given input" in listing_text
            or "Select the Offer" in listing_text
            or "Prices are per stateroom" in listing_text
        ):
            break
        if "Additional Discounts as applicable" not in listing_text:
            break  # left the occupancy screen even if no listing marker matched yet
        await page.wait_for_timeout(500)
        listing_text = await page.inner_text("body")

    discount_catalog = (state.get("discount_catalog_by_booking") or {}).get(booking_id)

    if "CRU_034" in listing_text or "No data found for the given input" in listing_text:
        return {"booking_id": booking_id, "status": "sailing_already_departed_or_no_data"}

    if expected_part_number:
        current_match = re.search(r"partNumber=([A-Za-z0-9]+)", page.url)
        current_part_number = current_match.group(1) if current_match else None
        if current_part_number != expected_part_number:
            # This is the exact corruption signature already seen once on
            # this project (2026-08-10, _ERR_INVALID_COOKIE incident) —
            # surface it loudly and refuse to report a result, rather
            # than silently trusting data that may belong to a different
            # sailing entirely.
            return {
                "booking_id": booking_id,
                "status": "sailing_identity_mismatch",
                "expected_part_number": expected_part_number,
                "current_part_number": current_part_number,
            }

    rate_name = staged.get("rate_name")
    all_tab_prices = None
    if staged.get("is_group_rate"):
        rate_tab_match = {"matched": False, "reason": "Group Rate booking — no individual-search tab exists", "active_tab": None}
    else:
        rate_tab_match = await _match_rate_tab(page, rate_name)
        if rate_tab_match["matched"]:
            listing_text = await page.inner_text("body")
        else:
            # ADDED 2026-08-12: a real, current situation — the booking's
            # own rate/promo isn't offered today at all anymore, not a
            # matching-algorithm failure (confirmed: e.g. "EPIC EUROPE
            # SALE" genuinely isn't among today's tabs). Rather than give
            # up with nothing, capture today's price for this category
            # under EVERY tab that IS offered, so there's real reference
            # data instead of a dead end. None of these is a confirmed
            # match — never fed into PRICE_MATCH as if it were.
            all_tab_prices = await _capture_all_tab_prices(page, staged["category"], staged.get("is_guaranteed", False))
            listing_text = await page.inner_text("body")

    today_price = _find_today_price(listing_text, staged["category"], staged.get("is_guaranteed", False))
    listing_confirmed = "Select the Offer" in listing_text or "Prices are per stateroom" in listing_text

    rate_record = {
        "booking_id": booking_id,
        "captured_at": datetime.now().isoformat(),
        "found": True,
        "category": staged["category"],
        "current_value": staged["current_value"],
        "rate_name": rate_name,
        "is_group_rate": staged.get("is_group_rate", False),
        "rate_tab_match": rate_tab_match,
        "today_price_same_category": today_price,
        # ADDED 2026-09-01 for the data-collection run. The scalar above is
        # the FIRST dollar figure within 200 chars of the category code, and
        # comparing it against the booking's whole-invoice total produced a
        # fake $267.01 on booking 3000081 (real answer $81.98 + $25 OBC).
        # This records the surrounding listing window, EVERY price in it,
        # any OBC wording and any per-person/total wording - so the units
        # can finally be established instead of assumed. Read-only; drives
        # no decision.
        "today_price_detail": today_price_detail(
            listing_text, staged["category"], staged.get("is_guaranteed", False)),
        # Recorded so a run's own output shows whether today's quote covered
        # the same guests as the booking - invisible when 3000081 was
        # priced as 1 guest against a 2-guest invoice total.
        "invoice_guest_count": staged.get("invoice_guest_count"),
        "all_tab_prices": all_tab_prices,
        "listing_text": listing_text[:4000],
        "listing_confirmed": listing_confirmed,
        "discount_options": staged.get("discount_options"),
        "club_discount_offered": staged.get("club_discount_offered"),
        "discount_catalog": discount_catalog,
        "all_seniors": staged.get("all_seniors"),
        "senior_count": staged.get("senior_count"),
        "has_voyagers": staged.get("has_voyagers"),
        "occupancy_fix": staged.get("occupancy_fix"),
    }
    os.makedirs(os.path.dirname(RATE_CHECK_DATA_PATH), exist_ok=True)
    with open(RATE_CHECK_DATA_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rate_record, ensure_ascii=False) + "\n")

    # CONFIRMED REAL GOTCHA, first live batch run 2026-08-11:
    # msc_session_controller.py reloads THIS module (msc_commands)
    # before every command, but that reload does NOT cascade to modules
    # it imports — core.calculator_msc/core.models get imported ONCE
    # (whenever _check_booking_msc first runs) and then stay cached in
    # sys.modules for the rest of the controller process's lifetime.
    # Real symptom: a genuine calculator bugfix (the current_discounts
    # None-vs-[] distinction) was correctly on disk and correctly
    # producing None at the msc_commands.py level, but the ALREADY-
    # CACHED calculator kept collapsing it back to "no discount" anyway,
    # because that calculator code was loaded before the fix was made
    # and a plain re-import doesn't re-read the file. Explicitly reload
    # both modules here (order matters — models before calculator, since
    # calculator imports names from models) so every check_booking call
    # runs against whatever's actually on disk right now, extending the
    # same hot-reload guarantee msc_session_controller.py already gives
    # this file to its own dependencies.
    import core.models
    import core.calculator_msc
    importlib.reload(core.models)
    importlib.reload(core.calculator_msc)
    from core.calculator_msc import evaluate_msc_booking

    essentials = _extract_booking_essentials(booking_data["summary_text"])
    # CONFIRMED REAL BUG, fixed 2026-08-26: this used to RE-DERIVE the
    # discounts from `booking_data["summary_text"]`/`["breakdown_text"]`,
    # which _lookup_one_booking deliberately TRUNCATES to 4000 chars each
    # (see its own return dict). `breakdown_text` is `inner_text("body")` —
    # the whole page plus the itemized per-passenger modal — so past 4000
    # characters the `Discount Description:` / `MSC Club Discount:` /
    # `SRN Non commissionable fares` lines get cut off. The result was an
    # EMPTY current_discounts on a booking that really does have a
    # disclosed discount, which makes `already_has_any_discount` False and
    # produces a FALSE "no discount applied, add one" DISCOUNT_ADD
    # opportunity — the 3000016 / 3000024 incident class.
    #
    # _lookup_one_booking ALREADY computes this correctly on the full,
    # untruncated text and returns it as `current_discounts`. Use that
    # instead of recomputing from the lossy copy. Falls back to the old
    # re-derivation only if the key is genuinely absent (an older captured
    # record replayed through msc_run_calculator.py, for instance).
    current_discounts = booking_data.get("current_discounts")
    if current_discounts is None:
        logger.warning(
            "msc.current_discounts_recomputed_from_truncated_text",
            booking_id=booking_id,
            note="booking_data had no precomputed current_discounts — falling back to the "
                 "truncated summary/breakdown text, which can miss a real disclosed discount",
        )
        current_discounts = _extract_discounts_with_implied(
            booking_data["summary_text"], booking_data.get("breakdown_text")
        )
    due_amount = _parse_dollars_safe(essentials.get("due_amount"))
    # Itemise the invoice once: used for the non-cruise correction below and
    # as a data-quality check (96 of 100 captured invoices reconcile to the
    # cent; every failure so far was a real unparsed line, not noise).
    from core.price_scope import PriceScope

    _invoice_parts = msc_invoice_components(booking_data.get("breakdown_text"))
    if not _invoice_parts.get("reconciles"):
        logger.warning(
            "msc.invoice_does_not_reconcile",
            booking_id=booking_id, gap=_invoice_parts.get("gap"),
            unknown_codes=_invoice_parts.get("unknown_codes"),
            note="component lines do not sum to the stated totals — the "
                 "capture may be truncated or a new line code exists",
        )

    # THE OCCUPANCY CROSS-CHECK, wired in 2026-09-01. It was written but
    # never CALLED - `evaluate_msc_booking` accepted `occupancy_verified`
    # and nothing computed it, so the guard was completely inert in
    # production. Neon's live run on 3000081 / 3000071 / 3000083 is what
    # exposed that: every capture reported invoice_guest_count = None.
    #
    # Computed HERE because this is the one place with both halves in scope:
    # `booking_data` carries the untruncated summary/breakdown text (so the
    # invoice's own passenger count can be read) and `staged["occupancy_fix"]`
    # carries MSC's pre-fill plus the state the occupancy screen actually
    # reached. Every price below is only meaningful if today's quote covered
    # the same guests as the booking.
    occupancy_fix = staged.get("occupancy_fix") or {}
    invoice_guests = staged.get("invoice_guest_count")
    if invoice_guests is None:
        invoice_guests = msc_invoice_guest_count(
            booking_data.get("summary_text"), booking_data.get("breakdown_text")
        )
    occupancy_verified, occupancy_note = msc_occupancy_is_trustworthy(
        {
            "counts": occupancy_fix.get("required") or {},
            "total_guests": occupancy_fix.get("intended_guests"),
            "dropped": occupancy_fix.get("dropped_passengers") or 0,
            "passengers_seen": occupancy_fix.get("passengers_seen"),
        },
        invoice_guests,
        occupancy_fix or None,
        staged.get("cabin_count"),
    )
    if not occupancy_verified:
        logger.warning(
            "msc.occupancy_not_verified",
            booking_id=booking_id,
            note=occupancy_note,
            invoice_guests=invoice_guests,
            occupancy_fix=occupancy_fix,
        )

    result = evaluate_msc_booking(
        booking_id=booking_id,
        occupancy_verified=occupancy_verified,
        occupancy_note=occupancy_note,
        category=staged["category"],
        cancelled_or_postponed=False,
        is_paid_in_full=_is_paid_in_full(
            due_amount, essentials.get("is_overpayment", False), core.models.MSC_PAID_IN_FULL_DUE_THRESHOLD
        ),
        # HARD RULE 2026-09-01: an overpaid booking is not optimizable at all.
        is_overpayment=bool(essentials.get("is_overpayment", False)),
        # Today's quote now carries the customer's own club discount when
        # staging managed to enter it — the difference between a real
        # $81.98 and a false "no opportunity" on booking 3000081.
        customer_has_club_membership=bool(
            (staged.get("voyagers_fix") or {}).get("member")),
        today_price_includes_club_discount=bool(
            staged.get("today_price_includes_club_discount")),
        club_entry_note=str((staged.get("voyagers_fix") or {}).get("reason") or ""),
        # Excursions/transfers/flights in the booking total, backed out so
        # the comparison is against the cruise alone.
        non_cruise_charges=_invoice_parts.get("non_cruise_total") or 0.0,
        # What each side of the comparison covers, so a mismatch in a
        # dimension nobody has written a specific guard for still refuses
        # instead of producing a confident wrong number.
        current_scope=PriceScope(
            guests=msc_invoice_guest_count(
                booking_data.get("summary_text"),
                booking_data.get("breakdown_text")),
            cabins=staged.get("cabin_count"),
            includes_club_discount=True,
            non_cruise_charges=0.0,   # backed out above
            label="the booking's own total",
        ),
        today_scope=PriceScope(
            guests=(staged.get("occupancy_fix") or {}).get("applied_guests"),
            cabins=1,                 # a category quote is always one cabin
            includes_club_discount=bool(
                staged.get("today_price_includes_club_discount")),
            non_cruise_charges=0.0,
            label="today's listing card",
        ),
        due_amount=due_amount,
        current_total_price=_parse_dollars_safe(essentials.get("value")),
        today_base_price=_parse_dollars_safe(today_price),
        current_discounts=current_discounts,
        today_discount_options=staged.get("discount_options"),
        today_discount_catalog=discount_catalog,
        has_voyagers=staged.get("has_voyagers", False),
        senior_count=staged.get("senior_count", 0),
        senior_discount_verifiable=_srn_reference_available(booking_data["summary_text"]),
        today_price_tab_confirmed=bool(rate_tab_match.get("matched")),
        is_group_rate=staged.get("is_group_rate", False),
        club_discount_offered=staged.get("club_discount_offered"),
        final_payment_date_passed=bool(essentials.get("final_payment_date_passed")),
    )

    os.makedirs(os.path.dirname(LIVE_CHECK_RESULTS_PATH), exist_ok=True)
    with open(LIVE_CHECK_RESULTS_PATH, "a", encoding="utf-8") as f:
        # STAMPED, added 2026-09-03. This file is append-only and holds
        # several results per booking, but carried NO timestamp on any of
        # its 154 records — so nothing downstream could tell which result
        # for a booking was the current one. Every reader was reduced to
        # "whichever line I read last", which is only correct while append
        # order happens to match real order. Concurrent scanning (two
        # cruise lines at once, now supported) can interleave writes and
        # break that silently.
        record = json.loads(result.model_dump_json())
        record["captured_at"] = datetime.now().isoformat()
        f.write(json.dumps(record) + "\n")

    return {
        "booking_id": booking_id,
        "status": "checked",
        "result": result,
        "rate_tab_match": rate_tab_match,
        "occupancy_fix": staged.get("occupancy_fix"),
        "all_tab_prices": all_tab_prices,
    }


def _format_discount_test_result(test_result, extra: str = "") -> str:
    """Human-readable summary of an MscDiscountTestResult, distinguishing
    every status from Section 17's expanded set rather than collapsing
    them to one generic message — a human reading this in
    controller_stdout.log/result.txt must be able to tell
    'the discount genuinely doesn't help' apart from 'we couldn't test it'
    apart from 'something is actually wrong, go look at this booking'."""
    from core.models import MscDiscountTestStatus

    bid = test_result.booking_id
    extra_note = f" {extra}." if extra else ""
    if test_result.status in (MscDiscountTestStatus.CONFIRMED_OPTIMIZATION, MscDiscountTestStatus.CONFIRMED_NO_SAVINGS):
        verdict = "CONFIRMED OPTIMIZATION" if test_result.status == MscDiscountTestStatus.CONFIRMED_OPTIMIZATION else "CONFIRMED — no savings"
        return (
            f"{bid}: {verdict} — {test_result.candidate_label!r} tested.{extra_note} "
            f"Before ${test_result.price_before:.2f} -> After ${test_result.price_after:.2f} "
            f"-> Actual savings ${test_result.actual_savings:.2f} (currency: {test_result.currency}, "
            f"rate tab confirmed: {test_result.rate_tab_confirmed}). {test_result.reason}"
        )
    if test_result.status == MscDiscountTestStatus.RESTORATION_FAILED:
        return f"{bid}: RESTORATION_FAILED — STOP, needs human review immediately.{extra_note} {test_result.reason}"
    # Every other status (DISCOUNT_APPLICATION_FAILED, CONFIRM_FAILED,
    # POST_PRICE_NOT_FOUND, POST_PRICE_INVALID, IDENTITY_VALIDATION_FAILED,
    # OCCUPANCY_MISMATCH, INSUFFICIENT_DATA, ERROR) still reports exactly
    # what WAS established (never silently dropped — see MscDiscountTestResult's
    # own docstring) alongside the specific failure reason.
    evidence_bits = []
    if test_result.price_before is not None:
        evidence_bits.append(f"baseline=${test_result.price_before:.2f}")
    if test_result.application_attempted:
        evidence_bits.append(f"discount_applied={test_result.application_success}")
    if test_result.confirm_attempted:
        evidence_bits.append(f"confirmed={test_result.confirm_success}")
    if test_result.price_after is not None:
        evidence_bits.append(f"post_price=${test_result.price_after:.2f}")
    evidence_note = f" [{', '.join(evidence_bits)}]" if evidence_bits else ""
    return f"{bid}: {test_result.status.value}{evidence_note} —{extra_note} {test_result.reason}"


def _format_check_booking_outcome(outcome: dict) -> str:
    booking_id = outcome["booking_id"]
    status = outcome["status"]
    if status != "checked":
        return f"{booking_id}: {status}" + (f" (category={outcome['category']})" if outcome.get("category") else "")
    result = outcome["result"]
    tab_note = (
        f"tab matched: {outcome['rate_tab_match']['active_tab']!r}"
        if outcome["rate_tab_match"].get("matched")
        else f"NO TAB MATCH ({outcome['rate_tab_match'].get('reason')})"
    )
    occ = outcome.get("occupancy_fix")
    occ_note = f" | occupancy corrected {occ['before']} -> {occ['after']}" if occ and occ.get("adjusted") else ""
    if occ and occ.get("stalled"):
        occ_note += " [WARNING: occupancy fix stalled before reaching the required count]"
    if occ and occ.get("skipped_empty_passengers"):
        occ_note += " [WARNING: passenger extraction returned empty — occupancy NOT verified, current on-screen counts trusted as-is]"
    flag = "OPPORTUNITY FOUND" if result.has_any_opportunity else "no opportunity"
    lines = [f"{booking_id} ({result.category}) — {flag} | {tab_note}{occ_note}"]
    for c in result.checks:
        lines.append(f"   {c.type.value}: {c.status.value} — {c.note}")
    all_tab_prices = outcome.get("all_tab_prices")
    if all_tab_prices:
        # ADDED 2026-08-12: the booking's own rate isn't offered today,
        # so no single number is a confirmed match — but here's today's
        # price for this category under every tab that IS available, for
        # manual comparison. Never a substitute for a confirmed
        # PRICE_MATCH, just reference data instead of nothing.
        ref = ", ".join(f"{tab}=${p}" if p else f"{tab}=?" for tab, p in all_tab_prices.items())
        lines.append(f"   (reference only, no confirmed match) today's prices by tab: {ref}")
    return "\n".join(lines)


async def run_command(state: dict, command: str) -> str:
    page = state["page"]

    if command == "list_tabs":
        live_pages = state["context"].pages
        state["pages"] = live_pages  # resync in case a tab was opened manually (not via new_tab)
        lines = [f"  [{i}] {p.url}" for i, p in enumerate(live_pages)]
        return f"{len(live_pages)} tab(s):\n" + "\n".join(lines)

    if command == "new_tab":
        new_page = await state["context"].new_page()
        state["pages"].append(new_page)
        state["page"] = new_page
        return f"opened tab index={len(state['pages']) - 1} (previous tab left open, {len(state['pages'])} tabs total)"

    if command.startswith("close_tabs:"):
        idxs = [int(x.strip()) for x in command[len("close_tabs:"):].split(",") if x.strip()]
        closed = []
        active_page_closed = False
        for idx in idxs:
            try:
                p = state["pages"][idx]
                if p is not None:
                    if p == state["page"]:
                        # Closing the tab a later command's `page = state["page"]`
                        # would use next — confirmed real bug 2026-08-10: this
                        # left state["page"] pointing at a closed page, and
                        # every subsequent command failed with "Target page,
                        # context or browser has been closed" until a manual
                        # switch_tab recovered it.
                        active_page_closed = True
                    await p.close()
                    state["pages"][idx] = None
                    closed.append(idx)
            except Exception as e:
                closed.append(f"{idx} FAILED: {e}")
        if active_page_closed:
            fallback = next((p for p in state["pages"] if p is not None), None)
            if fallback is not None:
                state["page"] = fallback
        return f"closed tabs: {closed}" + (" (active page was among these — switched to a remaining tab)" if active_page_closed else "")

    if command.startswith("switch_tab:"):
        idx = int(command[len("switch_tab:"):])
        state["page"] = state["pages"][idx]
        return f"switched to tab index={idx}, url={state['page'].url}"

    if command.startswith("goto:"):
        url = command[len("goto:"):]
        resp = await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        return f"status={resp.status if resp else None} url={page.url}"

    if command.startswith("click_text:"):
        text = command[len("click_text:"):]
        locator = page.get_by_text(text, exact=True).first
        await locator.click(timeout=10000)
        await page.wait_for_timeout(2000)
        return f"clicked text={text!r} now_url={page.url}"

    if command.startswith("click_selector:"):
        sel = command[len("click_selector:"):]
        await page.click(sel, timeout=10000)
        await page.wait_for_timeout(2000)
        return f"clicked selector={sel!r} now_url={page.url}"

    if command.startswith("select_option_label:"):
        label = command[len("select_option_label:"):]
        selects = page.locator("select")
        count = await selects.count()
        for i in range(count):
            sel = selects.nth(i)
            options = await sel.locator("option").all_text_contents()
            if label in [o.strip() for o in options]:
                await sel.select_option(label=label, force=True, timeout=10000)
                await page.wait_for_timeout(1000)
                return f"selected {label!r} in <select> #{i} of {count}"
        return f"ERROR: no <select> found with an option labeled {label!r} ({count} selects checked)"

    if command.startswith("fill_by_selector:"):
        rest = command[len("fill_by_selector:"):]
        sel, value = rest.split("|", 1)
        await page.fill(sel, value, timeout=10000)
        return f"filled selector={sel!r} with {value!r}"

    if command.startswith("fill_by_placeholder:"):
        rest = command[len("fill_by_placeholder:"):]
        placeholder, value = rest.split("|", 1)
        await page.get_by_placeholder(placeholder, exact=False).first.fill(value)
        return f"filled placeholder~={placeholder!r} with {value!r}"

    if command.startswith("eval:"):
        js = command[len("eval:"):]
        result = await page.evaluate(js)
        return f"eval result: {result}"

    if command.startswith("lookup_booking:"):
        booking_id = command[len("lookup_booking:"):].strip()
        data = await _lookup_one_booking(page, booking_id)
        os.makedirs(os.path.dirname(BOOKING_DATA_PATH), exist_ok=True)
        with open(BOOKING_DATA_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        preview = (data["summary_text"] or "")[:800]
        return f"saved booking {booking_id} to {BOOKING_DATA_PATH}\n---\n{preview}"

    if command.startswith("batch_lookup:"):
        ids = [b.strip() for b in command[len("batch_lookup:"):].split(",")
               if b.strip() and is_valid_msc_booking_id(b)]
        os.makedirs(os.path.dirname(BOOKING_DATA_PATH), exist_ok=True)
        results = []
        for booking_id in ids:
            try:
                data = await _lookup_one_booking(page, booking_id)
                with open(BOOKING_DATA_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                results.append(f"OK   {booking_id}")
            except Exception as e:
                results.append(f"FAIL {booking_id}: {e}")
        return f"batch of {len(ids)} done, saved to {BOOKING_DATA_PATH}:\n" + "\n".join(results)

    if command.startswith("batch_check_today_rate:"):
        ids = [b.strip() for b in command[len("batch_check_today_rate:"):].split(",")
               if b.strip() and is_valid_msc_booking_id(b)]
        os.makedirs(os.path.dirname(RATE_CHECK_DATA_PATH), exist_ok=True)
        results = []
        for booking_id in ids:
            try:
                data = await _check_today_rate(page, booking_id)
                with open(RATE_CHECK_DATA_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                results.append(f"OK   {booking_id} (found={data['found']}, listing_confirmed={data.get('listing_confirmed')})")
            except Exception as e:
                results.append(f"FAIL {booking_id}: {e}")
        return f"batch of {len(ids)} done, saved to {RATE_CHECK_DATA_PATH}:\n" + "\n".join(results)

    if command.startswith("check_today_rate:"):
        booking_id = command[len("check_today_rate:"):].strip()
        data = await _check_today_rate(page, booking_id)
        os.makedirs(os.path.dirname(RATE_CHECK_DATA_PATH), exist_ok=True)
        with open(RATE_CHECK_DATA_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        preview = (data.get("listing_text") or data.get("booking_text") or "")[:800]
        return f"saved rate check for {booking_id}\n---\n{preview}"

    # REMOVED 2026-08-14: the legacy manual multi-tab flow (stage_booking /
    # harvest_staged_booking / open_batch_tabs / harvest_batch_tabs) required
    # Neon to manually click "CONFIRM AND PROCEED" between staging and
    # harvesting every booking -- exactly the human-in-the-loop "watch then
    # review" step this project has moved away from. The fully automated
    # check_booking / check_booking_batch / check_booking_batch2 commands
    # below (added 2026-08-11) do lookup -> stage -> confirm -> harvest ->
    # evaluate in one call with zero human clicks and have been the
    # recommended flow since; this dispatcher no longer exposes the manual
    # commands at all. Their underlying helpers (_stage_booking_for_confirm,
    # _confirm_and_proceed_click, _match_rate_tab, etc.) are unchanged and
    # still power the automated flow.


    if command == "confirm_and_proceed":
        # Historically restricted to Neon's own confirm_and_proceed.ps1
        # trigger (this exact click was blocked from automatic calls by
        # Claude Code's safety classifier). RESOLVED 2026-08-11: Neon
        # added a narrowly-scoped autoMode.allow rule to his own
        # ~/.claude/settings.json permitting this exact click via this
        # exact command/file, confirmed working live — this command can
        # now be triggered by automation too, not just Neon's hotkey.
        clicked = await _confirm_and_proceed_click(page)
        if not clicked:
            return "CONFIRM AND PROCEED button not found on the current page"
        return "clicked CONFIRM AND PROCEED"

    if command.startswith("check_booking:"):
        booking_id = command[len("check_booking:"):].strip()
        try:
            outcome = await _check_booking_msc(state, booking_id)
        except Exception as e:
            return f"{booking_id}: FAIL — {e}"
        return _format_check_booking_outcome(outcome)

    if command.startswith("test_discount:"):
        # See test_discount_candidate's own comment: this long-lived
        # controller process can have a stale core.models cached from
        # before these classes existed — reload before importing them.
        import core.models
        importlib.reload(core.models)
        from core.models import MscDiscountApplicationMethod, MscDiscountCandidate

        rest = command[len("test_discount:"):]
        try:
            booking_id, label = rest.split(":", 1)
            booking_id = booking_id.strip()
            label = label.strip()
        except ValueError:
            return f"ERROR: expected test_discount:<booking_id>:<label>, got {rest!r}"
        candidate = MscDiscountCandidate(label=label, method=MscDiscountApplicationMethod.DROPDOWN_OPTION)
        try:
            test_result = await test_discount_candidate(state, booking_id, candidate)
        except Exception as e:
            return f"{booking_id}: FAIL — {e}"
        os.makedirs(os.path.dirname(LIVE_CHECK_RESULTS_PATH), exist_ok=True)
        with open(LIVE_CHECK_RESULTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(test_result.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return _format_discount_test_result(test_result)

    if command.startswith("test_voyagers_discount:"):
        import core.models
        importlib.reload(core.models)
        from core.models import MscDiscountApplicationMethod, MscDiscountCandidate

        booking_id = command[len("test_voyagers_discount:"):].strip()
        booking_data = await _lookup_one_booking(page, booking_id)
        if booking_data.get("session_expired"):
            return f"{booking_id}: INSUFFICIENT_DATA — session expired reading passenger data"
        passenger_info = _extract_passengers(booking_data.get("summary_text") or "")
        member = next((p for p in passenger_info["passengers"] if p.get("voyagers_number")), None)
        if member is None:
            return f"{booking_id}: INSUFFICIENT_DATA — no passenger on this booking has a Voyagers Club membership on file"
        name_parts = member["name"].strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]
        candidate = MscDiscountCandidate(
            label="Voyagers Club (SPECIAL OFFER)",
            method=MscDiscountApplicationMethod.VOYAGERS_CLUB_INSERT,
            voyagers_first_name=first_name, voyagers_last_name=last_name,
            voyagers_dob=member["dob"], voyagers_card_number=member["voyagers_number"],
        )
        try:
            test_result = await test_discount_candidate(state, booking_id, candidate)
        except Exception as e:
            return f"{booking_id}: FAIL — {e}"
        os.makedirs(os.path.dirname(LIVE_CHECK_RESULTS_PATH), exist_ok=True)
        with open(LIVE_CHECK_RESULTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(test_result.model_dump(mode="json"), ensure_ascii=False) + "\n")
        return _format_discount_test_result(test_result, extra=f"Voyagers Club member {member['name']!r}")

    if command.startswith("check_booking_batch:"):
        ids = [b.strip() for b in command[len("check_booking_batch:"):].split(",")
               if b.strip() and is_valid_msc_booking_id(b)]
        lines = []
        for booking_id in ids:
            try:
                outcome = await _check_booking_msc(state, booking_id)
                lines.append(_format_check_booking_outcome(outcome))
            except Exception as e:
                lines.append(f"{booking_id}: FAIL — {e}")
            # Pacing between bookings — mirrors the ESPRESSO/NCL side's
            # runBatch() sleep(500) between iterations, scaled up a bit
            # since each MSC check does considerably more work per
            # booking (a full stage->confirm->harvest sequence, not a
            # single page read).
            await asyncio.sleep(1.5)
        return f"batch of {len(ids)} done, results appended to {LIVE_CHECK_RESULTS_PATH}:\n\n" + "\n\n".join(lines)

    if command.startswith("check_booking_batch2:"):
        # Two-tab concurrent version, added 2026-08-11 at Neon's direct
        # request ("mscbook does allow that") after the earlier 1-tab-max
        # restriction. NOTE the known risk this doesn't fully eliminate:
        # a real 2026-08-10 incident showed even 2 tabs against the same
        # login CAN trigger a silent MSC-side cookie conflict (a tab
        # showing a completely unrelated sailing, no visible error).
        # Mitigated (not prevented) two ways: (1) capture-listener
        # booking-id tracking is now per-page, not one shared value, so
        # OUR OWN CODE can no longer cross-tag responses between the two
        # tabs; (2) _check_booking_msc now fingerprints the sailing's
        # partNumber right after staging and re-checks it after Confirm —
        # if MSC's backend itself ever serves back a session-confused
        # response, this surfaces as an explicit 'sailing_identity_mismatch'
        # result instead of a silently wrong one.
        # Validated the same way as the other three batch commands — this
        # was the one site the 2026-09-01 booking-id fix missed, and it is
        # the most dangerous one to leave open because it drives two
        # concurrent tabs.
        ids_raw = [b.strip() for b in command[len("check_booking_batch2:"):].split(",")
                   if b.strip() and is_valid_msc_booking_id(b)]
        # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness audit):
        # the same booking ID appearing twice in the input, at positions of
        # different parity (e.g. index 0 and index 1), used to be processed
        # CONCURRENTLY by both tabs at once — the same booking manipulated
        # from two tabs simultaneously under one login, which is a strictly
        # more dangerous version of the confirmed real 2026-08-10 session/
        # cookie-conflict incident this command's own docstring already
        # warns about. Dedupe before splitting work across tabs, preserving
        # first-occurrence input order for the ones that remain.
        ids, duplicate_ids = _dedupe_booking_ids(ids_raw)
        first_page = state["page"]
        if len(state["pages"]) < 2 or state["pages"][1] is None:
            second_page = await state["context"].new_page()
            if len(state["pages"]) < 2:
                state["pages"].append(second_page)
            else:
                state["pages"][1] = second_page
        else:
            second_page = state["pages"][1]

        results = [None] * len(ids)

        async def _worker(worker_page, indices):
            for idx in indices:
                booking_id = ids[idx]
                try:
                    outcome = await _check_booking_msc(state, booking_id, page=worker_page)
                    results[idx] = _format_check_booking_outcome(outcome)
                except Exception as e:
                    results[idx] = f"{booking_id}: FAIL — {e}"
                await asyncio.sleep(1.5)

        # Alternate assignment (0,2,4.. / 1,3,5..) rather than splitting
        # into two contiguous halves — keeps both tabs' expected finish
        # times close together instead of one tab racing ahead and
        # sitting idle while the other works through a full half alone.
        await asyncio.gather(
            _worker(first_page, list(range(0, len(ids), 2))),
            _worker(second_page, list(range(1, len(ids), 2))),
        )
        dedup_note = f"\n\n(NOTE: {len(duplicate_ids)} duplicate booking ID(s) in the input were skipped, not double-processed: {duplicate_ids})" if duplicate_ids else ""
        return (
            f"batch of {len(ids)} done (2 tabs), results appended to {LIVE_CHECK_RESULTS_PATH}:\n\n"
            + "\n\n".join(results) + dedup_note
        )

    if command == "relogin":
        result = await auto_login(page)
        return f"relogin result: {result}"

    if command == "read":
        body_text = await page.inner_text("body")
        return f"url={page.url}\n---\n{body_text[:5000]}"

    if command == "screenshot":
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{stamp}.png")
        await page.screenshot(path=path, full_page=True)
        return f"saved={path}"

    return f"ERROR: unknown command {command!r}"
