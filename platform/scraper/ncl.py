"""NCL scraper — Norwegian Cruise Line via SeaWeb portal.

Ported from adapter_ncl.js. Uses Playwright to automate the SeaWeb flow:
search → read booking data → scrape addons → enter edit mode →
navigate categories → find cheapest → calculate → ALWAYS cancel edit.

⚠️ THE 30-MINUTE LOCK: Entering edit mode locks the booking for 30 minutes.
The finally block ALWAYS cancels edit to release the lock, even on error.
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

from .base import BaseScraper

logger = get_logger(__name__)


class NclScraper(BaseScraper):
    """Scraper for NCL SeaWeb portal."""

    cruise_line = CruiseLine.NCL

    async def _search_booking(self, booking_id: str) -> None:
        """Submit booking ID on the SeaWeb search page."""
        await self.page.fill("#SWXMLForm_SearchReservation_ResID", booking_id)
        # Try primary lookup button, then fallback
        if await self.page.query_selector("#lookup-button"):
            await self.page.click("#lookup-button")
        else:
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
                        invoiceTotal: d.bi?.InvoiceTotal || d.baseInvoice?.INVOICE_TOTAL || 0,
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
        """Scrape addon table from the summary page."""
        result = await self.page.evaluate("""
            (() => {
                try {
                    let table = document.querySelector(
                        '#transformation > div > div > div:nth-child(3) > div.content.clearfix > table'
                    );
                    if (!table) {
                        for (const t of document.querySelectorAll('table')) {
                            const headers = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim());
                            if (headers.some(h => h.includes('Addon Name') || h.includes('Addon'))) {
                                table = t; break;
                            }
                        }
                    }
                    if (!table) return [];
                    const addons = [];
                    for (const row of table.querySelectorAll('tbody tr')) {
                        const cells = row.querySelectorAll('td, th');
                        if (cells.length >= 2) {
                            const name = cells[1]?.textContent?.trim();
                            const qty = parseInt(cells[2]?.textContent?.trim()) || 1;
                            if (name && name.length > 2) addons.push({ name, qty });
                        }
                    }
                    return addons;
                } catch(e) { return []; }
            })()
        """)
        return result or []

    async def _switch_to_edit_mode(self) -> bool:
        """Click Switch to Edit Mode. Returns True if edit mode activated."""
        has_btn = await self.page.query_selector("#res-switch-edit")
        if has_btn:
            await self.page.click("#res-switch-edit")
            await self.wait_for("#res-edit-save, a[href*='storeBooking']", timeout=12000)
            return True
        return False

    async def _cancel_edit(self) -> None:
        """ALWAYS call this to release the 30-minute booking lock."""
        try:
            cancel_btn = await self.page.query_selector("#res-edit-cancel")
            if cancel_btn:
                await cancel_btn.click()
                logger.info("ncl.unlock", method="#res-edit-cancel")
                await asyncio.sleep(0.5)
                return

            # Fallback: text match
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
        """Select a category via SlickGrid. Returns True on success."""
        result = await self.page.evaluate(f"""
            (async () => {{
                try {{
                    const categories = window.VX?.get('_form_12');
                    if (!categories) return false;
                    const idx = categories.findIndex(c => c.Category === '{target_cat}');
                    if (idx < 0) return false;
                    if (!categories[idx].HasAvailability) return false;
                    try {{
                        const gridEl = document.querySelector('[id*="SWXMLForm_SelectCategory_category"]');
                        if (gridEl && window.$ && window.$(gridEl).data) {{
                            const grid = window.$(gridEl).data('SlickGrid');
                            if (grid?.scrollRowIntoView) {{
                                grid.scrollRowIntoView(idx, false);
                                await new Promise(res => setTimeout(res, 400));
                            }}
                        }}
                    }} catch(e) {{}}
                    await new Promise(res => setTimeout(res, 400));
                    const viewport = document.querySelector('.slick-viewport');
                    if (!viewport) return false;
                    for (const row of viewport.querySelectorAll('.slick-row')) {{
                        const catLink = row.querySelector('.slick-cell.l0 a.infolink, .slick-cell:first-child a');
                        if (catLink && catLink.textContent.trim() === '{target_cat}') {{
                            const selectBtn = row.querySelector('a[data-link-action="select"], a.navlink');
                            if (selectBtn) {{ selectBtn.click(); await new Promise(r => setTimeout(r, 600)); return true; }}
                        }}
                    }}
                    return false;
                }} catch(e) {{ return false; }}
            }})()
        """)
        return bool(result)

    async def _read_new_total(self, category: str) -> dict:
        """Read updated ResTotal from VX grid."""
        return await self.page.evaluate(f"""
            (() => {{
                const cats = window.VX?.get('_form_12');
                if (!cats) return {{ resTotal: 0, currentPromo: '' }};
                const cat = cats.find(c => c.Category === '{category}');
                return {{ resTotal: parseFloat(cat?.ResTotal) || 0, currentPromo: cat?.CurrentPromo || '' }};
            }})()
        """)

    async def check_booking(self, booking_id: str) -> BookingResult:
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
            # Step 1: Navigate
            logger.info("ncl.navigate", booking_id=booking_id)
            await self.navigate(settings.ncl_search_url)
            await self.wait_for("#SWXMLForm_SearchReservation_ResID", timeout=15000)

            # Step 2: Search
            logger.info("ncl.search", booking_id=booking_id)
            await self._search_booking(booking_id)

            try:
                await self.wait_for(
                    ".item.current, #res-switch-edit, #res-edit-save",
                    timeout=20000,
                )
            except Exception:
                error_text = await self.page.evaluate("""
                    (() => {
                        for (const sel of ['.error', '.alert', '#pageMessages', '.swmessage']) {
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

            if preload.get("isPaid"):
                return make_paid_in_full_result(
                    booking_id, preload.get("category"), CruiseLine.NCL, preload.get("invoiceTotal", 0),
                )

            current_category = preload.get("category")
            old_total = preload.get("invoiceTotal", 0)
            current_promos = preload.get("currentPromos", "")
            logger.info("ncl.booking_info", booking_id=booking_id, category=current_category, total=old_total)

            # Step 4: Scrape addons
            addons = await self._scrape_addons()
            logger.info("ncl.addons", booking_id=booking_id, count=len(addons))

            # Step 5: Enter edit mode (LOCKS booking for 30 min)
            in_edit_mode = await self._switch_to_edit_mode()
            logger.info("ncl.edit_mode", booking_id=booking_id, locked=in_edit_mode)

            # Step 6: Category tab
            await self._click_category_tab()

            # Step 7: Read categories
            cat_data = await self._read_category_data()
            if not cat_data.get("ok"):
                raise RuntimeError(f"Cannot read categories: {cat_data.get('error')}")

            categories = cat_data["categories"]
            current = next((c for c in categories if c["category"] == current_category), None)
            if not current:
                raise RuntimeError(f"Category '{current_category}' not found in grid data")

            # Step 8: Find cheapest available
            cheaper = sorted(
                [
                    c for c in categories
                    if c["resTotal"] > 0
                    and c["resTotal"] < current["resTotal"]
                    and c["status"] == "OK"
                    and c["hasAvailability"]
                ],
                key=lambda c: -c["resTotal"],  # Highest-cheaper first (smallest drop)
            )

            if not cheaper:
                logger.info("ncl.no_cheaper", booking_id=booking_id, category=current_category)
                return calculate_ncl(
                    booking_id, current_category, old_total, old_total,
                    addons, current_promos, current_promos,
                )

            target = cheaper[0]
            logger.info("ncl.select", booking_id=booking_id, target=target["category"], price=target["resTotal"])

            # Step 9: Select category
            selected = await self._select_category(target["category"])
            if not selected:
                raise RuntimeError(f"Category selection failed for '{target['category']}'")
            await asyncio.sleep(0.8)

            # Step 10: Read new total
            new_data = await self._read_new_total(target["category"])
            new_total = new_data.get("resTotal", target["resTotal"])
            new_promos = new_data.get("currentPromo", target.get("currentPromo", ""))

            # Step 11: Calculate
            result = calculate_ncl(
                booking_id, current_category, old_total, new_total,
                addons, current_promos, new_promos,
            )
            result.new_price_category = target["category"]
            logger.info("ncl.result", booking_id=booking_id, status=result.status.value, net=result.net_saving)
            return result

        except Exception as e:
            logger.error("ncl.error", booking_id=booking_id, error=str(e))
            return make_error_result(booking_id, current_category, CruiseLine.NCL, str(e))

        finally:
            # ALWAYS UNLOCK — even on success, error, and exception
            if in_edit_mode:
                await self._cancel_edit()
