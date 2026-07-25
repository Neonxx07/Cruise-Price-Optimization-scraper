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

from .base import BaseScraper

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
        self.guests_count = guests_count if guests_count is not None else settings.goccl_default_guests_count

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
        """Reads the current booking's price, offer code, category, and stateroom type
        from the booking summary bar. Selectors confirmed via DevTools Recorder export."""
        gross_text = await self.page.inner_text("span.price__number")
        current_price = self._parse_price(gross_text)

        offer_code = await self.page.inner_text(
            "[data-component='category-rate-header-rate-name']"
        )
        category_value = await self.page.inner_text(
            "article.booking-details-bar__category--category span.booking-details-bar__category-value"
        )
        stateroom_type = await self.page.inner_text(
            "span.booking-details-bar__category-metaname"
        )

        return {
            "current_price_gross": current_price,
            "current_offer_code": offer_code.strip(),
            "current_category": category_value.strip(),
            "current_stateroom_type": stateroom_type.strip(),
        }

    async def open_modify_booking(self) -> None:
        modify_btn = self.page.get_by_role("link", name="Modify Booking")
        await modify_btn.wait_for(state="visible")
        await modify_btn.click()
        await self.page.wait_for_load_state("networkidle")

    async def open_change_offer_rate(self) -> None:
        change_rate_btn = self.page.get_by_role("button", name=re.compile("Change Offer/Rate", re.IGNORECASE))
        await change_rate_btn.wait_for(state="visible")
        await change_rate_btn.click()
        await self.page.wait_for_selector("section.rate__container, div[class*='rate']")

    async def read_offer_code_comparison(self) -> list[OfferCodeOption]:
        """Reads the offer-code comparison screen. Confirmed container:
        section.rate__container, with stateroom-type prices as numbered buttons
        (1=Upper/Lower, 2=Interior, 3=Ocean View, 4=Balcony, 5=Suite — order
        confirmed from a recorded click on button index 4 landing on Balcony)."""
        stateroom_order = ["UPPER_LOWER", "INTERIOR", "OCEAN_VIEW", "BALCONY", "SUITE"]

        offer_rows = await self.page.query_selector_all("section.rate__container > div > div")
        results = []
        for row in offer_rows:
            name_el = await row.query_selector("h6, .offer-name")
            offer_name = (await name_el.inner_text()).strip() if name_el else ""
            code_el = await row.query_selector(".offer-code, [data-component='offer-code']")
            offer_code = (await code_el.inner_text()).strip() if code_el else ""

            buttons = await row.query_selector_all("button")
            for idx, button in enumerate(buttons):
                if idx >= len(stateroom_order):
                    break
                price_el = await button.query_selector("span.price__number")
                if not price_el:
                    continue
                price_text = await price_el.inner_text()
                results.append(OfferCodeOption(
                    offer_name=offer_name,
                    offer_code=offer_code,
                    stateroom_type=stateroom_order[idx],
                    price_per_person=self._parse_price(price_text),
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
            )
            logger.info("goccl.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
            self.log_action("result", booking_id=booking_id, status=result.status.value, net_saving=result.net_saving)
            return result

        except Exception as e:
            logger.error("goccl.error", booking_id=booking_id, error=str(e))
            self.log_action("error", booking_id=booking_id, error=str(e))
            await self.dump_failure_snapshot(booking_id, "check_booking_failed", str(e))
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
