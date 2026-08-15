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

import re
from dataclasses import dataclass, field

from config.settings import settings
from core.calculator import calculate_goccl, make_error_result
from core.models import BookingResult, CruiseLine
from utils.logging import get_logger

from .base import BaseScraper, is_dead_browser_error

logger = get_logger(__name__)

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

    async def search_booking(self, booking_number: str) -> None:
        self.log_action("navigate", booking_id=booking_number, url=settings.goccl_search_url)
        await self.navigate(settings.goccl_search_url, wait_until="networkidle")
        booking_input = self.page.locator("#ctl00_DefaultContent_txtBookingNumber")
        await booking_input.click()
        await booking_input.fill(booking_number)
        self.log_action("search_booking", booking_id=booking_number)
        # Try the explicit search button first; fall back to Enter if it's not
        # present/clickable — matches the resilient pattern from the reference
        # Playwright test, since the exact submit mechanism can vary by page state.
        try:
            await self.page.click("#ctl00_DefaultContent_btnSearchBookingNumber", timeout=3000)
        except Exception:
            await booking_input.press("Enter")
        await self.page.wait_for_selector("#booked-root")
        await self.dump_page_snapshot(booking_number, "after_search")

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

        return {
            "current_price_gross": float(gross),
            "current_offer_code": rate.get("code") or "",
            "current_category": category.get("code") or "",
            "current_stateroom_type": stateroom_type.get("name") or "",
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
        await modify_btn.click()
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
        await change_rate_btn.click()
        await self.page.wait_for_selector("section.rate__container, div[class*='rate']")

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
                guests_count=self.guests_count,
                guests_count_verified=self.guests_count_verified,
            )
            logger.info("goccl.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
            self.log_action("result", booking_id=booking_id, status=result.status.value, net_saving=result.net_saving)
            return result

        except Exception as e:
            logger.error("goccl.error", booking_id=booking_id, error=str(e))
            self.log_action("error", booking_id=booking_id, error=str(e))
            await self.dump_failure_snapshot(booking_id, "check_booking_failed", str(e))
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
