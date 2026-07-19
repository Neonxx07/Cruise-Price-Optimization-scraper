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
    make_paid_in_full_result,
    make_skip_reprice_result,
    make_wlt_result,
)
from core.models import BookingResult, BookingStatus, CruiseLine
from utils.logging import get_logger
from utils.retry import retry_async

from .base import BaseScraper

logger = get_logger(__name__)

# Matches an unrendered Mustache/Angular-style template placeholder,
# e.g. "{{sb.reservation.category.priceCategory}}", so we can tell it
# apart from a real category code like "ZI".
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"^\{\{.*\}\}$")


def _is_template_placeholder(value: str) -> bool:
    return bool(_TEMPLATE_PLACEHOLDER_RE.match(value.strip()))


class EspressoScraper(BaseScraper):
    """Scraper for ESPRESSO (Royal Caribbean / Celebrity) portal."""

    cruise_line = CruiseLine.ESPRESSO

    async def _check_login(self) -> bool:
        """Verify user is logged in, based on whatever page we're currently on."""
        url = self.page.url
        if "login" in url or "signin" in url:
            logger.warning("login.required", msg="Not logged in — please log into ESPRESSO first")
            return False
        return "cruisingpower.com" in url

    async def _search_booking(self, booking_id: str) -> None:
        """Submit a booking ID in the search form."""
        # Login can still be mid-way through ESPRESSO's OAuth SSO redirect
        # chain (login -> auth.cruisingpower.com -> oauth callback ->
        # reservations.do) when we get here — that can take longer than
        # the generic 30s action timeout, so wait for the search box
        # itself with a dedicated, longer timeout before touching it.
        await self.wait_for("#reservationid", timeout=settings.scraper_login_timeout_ms)
        await self.page.fill("#reservationid", "")
        await self.page.fill("#reservationid", booking_id)
        await self.page.click("#searchReservationBtn")
        await self.wait_for("#sideBar, [id*='sideBar']", timeout=15000)

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

    async def _capture_category_table(self, current_category: str | None = None) -> dict:
        """Capture the loaded ESPRESSO category table state without mutating the page."""
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
                        rows.push({{
                            category,
                            status,
                            radioValue: radio?.value || null,
                            radioChecked: Boolean(radio?.checked),
                            promo: row.querySelector('.promo, .currentPromo')?.textContent?.trim() || '',
                            rowText: row.innerText.trim(),
                        }});
                    }}
                }}
                const token = location.href.match(/execution=(e\\d+s\\d+)/)?.[1] || null;
                const selectionJSON = document.querySelector('input.selectionJSON, input[name*="selectionJSON"]')?.value || '';
                return {{
                    ok: true,
                    currentCategory: {json.dumps(current_category)},
                    executionToken: token,
                    selectionJSON,
                    rows,
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

    async def check_booking(self, booking_id: str, capture_market_data: bool = False) -> BookingResult:
        """
        Full ESPRESSO booking check flow.

        Steps: navigate → login check → search → read category →
        load categories → WLT check → execute API → calculate result.
        """
        price_category: str | None = None
        self.last_market_data = None

        async def _attempt():
            nonlocal price_category

            # Go through the portal home page first, the same path a human
            # takes right after login — deep-linking straight to
            # reservations.do skips whatever session/flow initialization
            # /home does, and appears to be what was causing the forced
            # logouts and desynced execution tokens seen during testing.
            logger.info("espresso.navigate_home", booking_id=booking_id)
            await self.navigate(settings.espresso_home_url)
            if not await self._check_login():
                raise RuntimeError("Not logged in — please log into ESPRESSO first")

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

            if capture_market_data:
                self.last_market_data = await self._capture_category_table(price_category)
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

            if not api_result.get("ok"):
                # Check paid status before failing
                if (api_result.get("dataLength") or 0) < 300:
                    paid = await self._check_paid_status()
                    if paid and paid.get("isPaid"):
                        return {"_paidInFull": True, "oldTotal": paid.get("totalPrice", 0)}
                logger.warning("espresso.api_call_failed", booking_id=booking_id, error=api_result.get("error"))
                raise RuntimeError(api_result.get("error", "API failed"))

            if (api_result.get("dataLength") or 0) < 300:
                paid = await self._check_paid_status()
                if paid and paid.get("isPaid"):
                    return {"_paidInFull": True, "oldTotal": paid.get("totalPrice", 0)}
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
        if api_result.get("_skipRepriceModal"):
            return make_skip_reprice_result(booking_id, price_category, CruiseLine.ESPRESSO)

        result = calculate_espresso(api_result["data"], booking_id, price_category)
        logger.info("espresso.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
        return result
