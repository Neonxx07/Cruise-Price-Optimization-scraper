"""ESPRESSO scraper — Royal Caribbean & Celebrity Cruises.

Ported from adapter_espresso.js. Uses Playwright to automate the
ESPRESSO portal flow: login check → search → read category →
load categories table → click radio → execute API calls → parse results.
"""

from __future__ import annotations

import asyncio
import json

from config.settings import settings
from core.calculator import calculate_espresso, make_paid_in_full_result, make_wlt_result
from core.models import BookingResult, BookingStatus, CruiseLine
from utils.logging import get_logger
from utils.retry import retry_async

from .base import BaseScraper

logger = get_logger(__name__)


class EspressoScraper(BaseScraper):
    """Scraper for ESPRESSO (Royal Caribbean / Celebrity) portal."""

    cruise_line = CruiseLine.ESPRESSO

    async def _check_login(self) -> bool:
        """Verify user is logged into ESPRESSO."""
        url = self.page.url
        if "cruisingpower.com" in url and "login" not in url and "signin" not in url:
            return True
        if "login" in url or "signin" in url:
            logger.warning("login.required", msg="Not logged in — please log into ESPRESSO first")
            return False
        # Navigate and check
        await self.navigate(settings.espresso_base_url)
        await asyncio.sleep(2)
        url = self.page.url
        return "cruisingpower.com" in url and "login" not in url

    async def _search_booking(self, booking_id: str) -> None:
        """Submit a booking ID in the search form."""
        await self.page.fill("#reservationid", "")
        await self.page.fill("#reservationid", booking_id)
        await self.page.click("#searchReservationBtn")
        await self.wait_for("#sideBar, [id*='sideBar']", timeout=15000)

    async def _read_category(self) -> str | None:
        """Read the current price category from the booking page."""
        # Method 1: Hidden input
        cat = await self.page.evaluate("""
            (() => {
                const h = document.getElementById('currentPriceCat');
                if (h?.value?.trim()) return h.value.trim();
                const s = document.querySelector('[class*="priceCategory"] [class*="value"]')
                       || document.querySelector('.priceCategory .value');
                return s?.textContent?.trim() || null;
            })()
        """)
        return cat

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
        """Check if booking is fully paid."""
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

    async def check_booking(self, booking_id: str) -> BookingResult:
        """
        Full ESPRESSO booking check flow.

        Steps: navigate → login check → search → read category →
        load categories → WLT check → execute API → calculate result.
        """
        price_category: str | None = None

        async def _attempt():
            nonlocal price_category

            logger.info("espresso.navigate", booking_id=booking_id)
            await self.navigate(settings.espresso_base_url)

            if not await self._check_login():
                raise RuntimeError("Not logged in — please log into ESPRESSO first")

            logger.info("espresso.search", booking_id=booking_id)
            await self._search_booking(booking_id)

            price_category = await self._read_category()
            logger.info("espresso.category", booking_id=booking_id, category=price_category)

            # Click categories and load the table
            await self._click_categories()

            # WLT check (AFTER categories table is loaded — fix from v6.3)
            if price_category and await self._check_wlt(price_category):
                return {"_wlt": True}

            page_data = await self._read_page_data(price_category)
            if not page_data.get("executionToken"):
                raise RuntimeError("No execution token in URL")

            logger.info("espresso.api_calls", booking_id=booking_id, token=page_data["executionToken"])
            api_result = await self._execute_api_calls(
                page_data["executionToken"],
                page_data["selectionJSON"],
                page_data["radioValue"],
            )
            if not api_result.get("ok"):
                # Check paid status before failing
                if (api_result.get("dataLength") or 0) < 300:
                    paid = await self._check_paid_status()
                    if paid and paid.get("isPaid"):
                        return {"_paidInFull": True, "oldTotal": paid.get("totalPrice", 0)}
                raise RuntimeError(api_result.get("error", "API failed"))

            if (api_result.get("dataLength") or 0) < 300:
                paid = await self._check_paid_status()
                if paid and paid.get("isPaid"):
                    return {"_paidInFull": True, "oldTotal": paid.get("totalPrice", 0)}
                raise RuntimeError(f"API returned only {api_result.get('dataLength')} chars — token likely expired")

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

        result = calculate_espresso(api_result["data"], booking_id, price_category)
        logger.info("espresso.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
        return result
