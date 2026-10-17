"""Princess (POLAR Online) - read a booking and price its own category, safely.

Built 2026-10-15 from Neon's DEMO12 HAR and the live read-only run on 33 of
his bookings. Neon: "please do not cancel anything or save any prices please
do not ruin the booking". docs/PRINCESS_POLAR.md has the whole record.

THE WALK (every control proven in the HAR, nothing else is ever pressed):
  MAIN MENU -> Manage Booking -> booking number -> Search      (submitBookNum)
  Booking Confirmation: read      -> Special Requests / Gifts  (DFH_PF19) -> Back (DFH_PF12)
                                  -> Booking Information       (DFH_PF13)
  Booking Information: REBOOK drop-down = F "Change Fare Option", BOTH cancel
      drop-downs (CANCCODE, CXLREASON) verified EMPTY, then "Rebook".
      Cancel Booking and Rebook run the SAME action - doEnter(document.CMRCBA,
      'DFH_ENTER') - the drop-downs decide; hence the dedicated guard.
  Voyage Pricing By Ship Zone -> Search By Category List       (searchByCategory())
  Category Fares - Best Buy: tick the booking's own category (first two
      characters), verify SELCAT/SELFARE, Save & Continue      (submitWithSelection)
  Pricing Detail: read the quote -> Back (DFH_PF12)
  Category Fares -> Fare Comparisons (DFH_PF21): read the promo list -> Back
  -> MAIN MENU.
NEVER: Cancel Booking, Save / confirmChanges / doDOBCheck, Reset (DFH_PF5),
Submit Changes, Save & Continue on Pricing Detail (doSaveAndContinue - it moves
toward booking), FCC's, Select, any Special Requests CANCEL, CONFIRMATION.
Princess's information boxes (showPOLAlert) are closed only when their text is
one of the known notices; anything else stops the run.

Writes nothing to cruise_intel.db. Every page visited is saved.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date

from core import princess_pages as pages
from core.calculator_princess import (
    PrincessBooking,
    PrincessQuote,
    PrincessVerdict,
    booking_tier,
    decide,
    find_category_row,
    parse_polar_date,
)
from core.calculator import GOCCL_CONFIRMED_CONFIDENCE
from core.models import BookingResult, BookingStatus, CruiseLine
from core.princess_packages import STANDARD, package_tier
from scraper.base import BaseScraper
from utils.logging import get_logger

logger = get_logger(__name__)

CDP_URL = "http://127.0.0.1:9333"
LOGIN_URL = "https://www.onesourcecruises.com/onesource/login"


class Stop(Exception):
    """Something on the page was not what the HAR proved - stop, touch nothing."""


class NotPriceable(Exception):
    """A fact about THIS booking (no fares listed, category not offered) - it
    is reported and the run moves on. Never used for a page that looks wrong:
    that is Stop. DEMO23 2026-10-15: Category Fares came back with every
    displayRow blank and "A category selection is required"."""


#: label -> (href token that MUST be present or None, page-title part it may be clicked on)
ALLOWED: dict[str, tuple[str | None, str | tuple[str, ...] | None]] = {
    "MAIN MENU": (None, None),
    "Manage Booking": (None, "MAIN MENU"),
    "Search": ("submitBookNum()", "Booking Search"),
    "Special Requests / Gifts": ("DFH_PF19", "Booking Confirmation"),
    "Booking Information": ("DFH_PF13", "Booking Confirmation"),
    "Back": ("DFH_PF12", None),
    "Search By Category List": ("searchByCategory()", "Voyage Pricing"),
    # Category Fares -> Pricing Detail; Fare Comparisons -> submits the ticked
    # promos. NEVER on Pricing Detail, where it is doSaveAndContinue().
    "Save & Continue": ("submitWithSelection('DFH_ENTER')",
                        ("Category Fares - Best Buy", "Fare Comparisons")),
    "Fare Comparisons": ("DFH_PF21", "Category Fares"),
}
FORBIDDEN = ("doEnter", "confirmChanges", "doDOBCheck", "doSaveAndContinue", "DFH_PF5",
             "DFH_PF16", "CANC", "callDREV", "DFH_PF3", "submitChanges", "Cancel",
             "logOut")    # EXIT on the wizard pages is logOut(...) - it signs Neon out

#: Princess's showPOLAlert boxes, read from the live pages 2026-10-15:
#: (heading, message start) -> kind. Their OK button sits in no form.
KNOWN_NOTICES = {
    ("Notification", "Any changes to your booking that result in a balance due"): "PAST_FINAL_PAYMENT",
    ("Notification", "To edit, cancel, or pay this booking please call Princess Reservations"):
        "CALL_RESERVATIONS",
    ("Notification", "This booking is in Matched status"): "MATCHED_WAITLIST",
    ("Important", "Amount received may include a payment Future Cruise Credit"): "FCC_NOTICE",
    ("Important", "Guest has used a FCD payment"): "FCD_NOTICE",
}

_NOTICE_JS = r"""
() => {
  const b = [...document.querySelectorAll('button')].find(
      x => /^\s*ok\s*$/i.test(x.textContent) && x.offsetParent !== null);
  if (!b) return null;
  const box = b.parentElement;
  return {inForm: !!b.closest('form'), heading: ((box.querySelector('h2') || {}).textContent || '').trim(),
          text: ((box.querySelector('p') || {}).textContent || box.innerText || '').replace(/\s+/g, ' ').trim()};
}
"""


@dataclass
class PrincessCheck:
    ref: str
    outcome: str = ""
    notices: list[str] = field(default_factory=list)
    landing: dict = field(default_factory=dict)
    special_requests: list[dict] = field(default_factory=list)
    offer_codes: list[str] = field(default_factory=list)
    tier: str | None = None
    tier_basis: str = ""
    category_row: str | None = None
    best_buy_net_of_obc: float | None = None
    quote: dict = field(default_factory=dict)
    promo_list: list[dict] = field(default_factory=list)
    quote_tier: str | None = None
    compares: list[dict] = field(default_factory=list)
    verdict: dict = field(default_factory=dict)
    why: str = ""


class PolarSession:
    """One signed-in POLAR page. Never logs in - Neon signs in by hand (no
    OneSource credential is saved yet)."""

    def __init__(self, page, out_dir: str):
        self.page = page
        self.out_dir = out_dir
        self.check: PrincessCheck | None = None
        self.dialogs: list[dict] = []
        page.on("dialog", self._on_dialog)

    # -- plumbing ------------------------------------------------------------

    def _on_dialog(self, dialog):
        self.dialogs.append({"type": dialog.type, "message": dialog.message[:200]})
        # beforeunload = POLAR's normal "leave this page?". Anything else is
        # unexpected: dismiss it, never confirm an unknown question.
        asyncio.ensure_future(dialog.accept() if dialog.type == "beforeunload" else dialog.dismiss())

    async def title(self) -> str:
        return await self.page.title()

    async def press(self, loc) -> None:
        """Click ONE already-verified element, clear of OneTrust's banner.

        Live 2026-10-15, first fully automatic session: the click timed out
        because "<div id="onetrust-consent-sdk"> subtree intercepts pointer
        events" - OneSource's cookie banner, fixed over the bottom of the
        page. Neon's own browser had answered it; a fresh session has not.
        The banner is NEVER answered here (consent is his choice): the target
        is scrolled to the middle of the screen, and if the banner still
        covers it the element's own click() runs - the same element, already
        checked against the allow-list by the caller."""
        # Never act on a page still changing (DEMO34 2026-10-16: 30 s
        # timeout "waiting for navigation to finish" on the next control).
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        try:
            await loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'center'})",
                               timeout=10000)
        except Exception:
            pass
        try:
            await loc.click(timeout=5000)
        except Exception as exc:
            if "onetrust" not in str(exc).lower():
                raise
            await loc.evaluate("e => e.click()", timeout=15000)

    async def save(self, step: str) -> str:
        html = await self.page.content()
        if self.check:
            with open(os.path.join(self.out_dir, f"{self.check.ref}_{step}.html"), "w",
                      encoding="utf-8") as fh:
                fh.write(html)
        return html

    async def wait_title(self, part: str, timeout_s: float = 45) -> bool:
        for _ in range(int(timeout_s * 4)):
            if part.lower() in (await self.title()).lower():
                return True
            await asyncio.sleep(0.25)
        return False

    async def clear_notices(self):
        """Close Princess's information box - only a KNOWN one."""
        for _ in range(3):
            info = await self.page.evaluate(_NOTICE_JS)
            if not info:
                return
            kind = next((k for (head, start), k in KNOWN_NOTICES.items()
                         if info["heading"] == head and info["text"].startswith(start)), None)
            if info["inForm"] or not kind:
                raise Stop(f"unknown pop-up, not touched: {info['heading']!r} {info['text'][:160]!r}")
            if self.check is not None and kind not in self.check.notices:
                self.check.notices.append(kind)
            await self.press(self.page.locator("button", has_text=re.compile(r"^\s*ok\s*$", re.I)).filter(
                visible=True).first)
            await self.page.wait_for_timeout(400)

    async def safe_click(self, label: str):
        """Click the ONE kind of link with this text, on the page it belongs to."""
        if label not in ALLOWED:
            raise Stop(f"refused: '{label}' is not on the allow-list")
        need, on_page = ALLOWED[label]
        await self.clear_notices()
        here = await self.title()
        pages_ok = (on_page,) if isinstance(on_page, str) else on_page
        if pages_ok and not any(p.lower() in here.lower() for p in pages_ok):
            raise Stop(f"refused: '{label}' is only pressed on {pages_ok}, this is '{here}'")
        words = r"\s+".join(re.escape(w) for w in label.split())
        links = self.page.locator("a").filter(has_text=re.compile(rf"^\s*{words}\s*$", re.I))
        cands = []
        for i in range(await links.count()):
            href = (await links.nth(i).get_attribute("href")) or ""
            if need is None or need in href:
                cands.append((links.nth(i), href))
        if not cands:
            raise Stop(f"no '{label}' link{' carrying ' + need if need else ''} on '{here}'")
        hrefs = {h for _, h in cands}
        if len(hrefs) != 1:
            raise Stop(f"'{label}' links do different things: {sorted(hrefs)}")
        href = hrefs.pop()
        if any(f in href for f in FORBIDDEN):
            raise Stop(f"'{label}' href {href!r} looks unsafe")
        visible = [loc for loc, _ in cands if await loc.is_visible()]
        if not visible:
            raise Stop(f"'{label}' is not visible on '{here}'")
        async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
            await self.press(visible[0])
        await self.page.wait_for_timeout(600)
        await self.clear_notices()

    # -- the walk ------------------------------------------------------------

    async def open_booking(self, ref: str) -> str:
        if "MAIN MENU" not in await self.title():
            await self.safe_click("MAIN MENU")
        if not await self.wait_title("MAIN MENU"):
            raise Stop("could not reach MAIN MENU")
        # The title arrives before the menu does (DEMO36 2026-10-16: MAIN
        # MENU title, no links at all yet -> "no 'Manage Booking' link").
        try:
            await self.page.locator("a").filter(
                has_text=re.compile(r"^\s*Manage\s+Booking\s*$", re.I)).first.wait_for(
                state="visible", timeout=30000)
        except Exception:
            pass                      # safe_click below names the problem
        await self.safe_click("Manage Booking")
        if not await self.wait_title("Booking Search"):
            raise Stop("Manage Booking did not open Booking Search")
        box = self.page.locator('input[name$="-BOOKNUM"]')
        if await box.count() != 1:
            raise Stop("booking number box not found exactly once")
        await box.fill(ref)
        await self.safe_click("Search")
        return await self.save("1_landing")

    async def rebook_change_fare(self):
        """The ONE place 'Rebook' is pressed. Cancel Booking runs the same
        doEnter - so every drop-down is checked right before the click."""
        if "Booking Information" not in await self.title():
            raise Stop("Rebook refused: not on Booking Information")
        state = await self.page.evaluate("""() => {
            const v = s => { const e = document.querySelector(s); return e ? e.value : null; };
            return {cancel: v('[name$="-CANCCODE"]'), reason: v('[name$="-CXLREASON"]'),
                    rebook: v('[name$="-REBOOK"]')}; }""")
        if state["cancel"] is None or state["rebook"] is None:
            raise Stop(f"Rebook refused: drop-downs not found {state}")
        if (state["cancel"] or "").strip() or (state["reason"] or "").strip():
            raise Stop(f"Rebook refused: a CANCEL drop-down has a value {state}")
        await self.page.locator('select[name$="-REBOOK"]').select_option(value="F")
        after = await self.page.evaluate("""() => {
            const e = document.querySelector('select[name$="-REBOOK"]');
            const c = document.querySelector('[name$="-CANCCODE"]');
            const r = document.querySelector('[name$="-CXLREASON"]');
            return {rebook: e.value, label: e.options[e.selectedIndex].text,
                    cancel: c ? c.value : null, reason: r ? r.value : ''}; }""")
        if (after["rebook"] != "F" or "fare" not in after["label"].lower()
                or (after["cancel"] or "").strip() or (after["reason"] or "").strip()):
            raise Stop(f"Rebook refused after selecting: {after}")
        links = self.page.locator("a").filter(has_text=re.compile(r"^\s*Rebook\s*$", re.I))
        if await links.count() != 1:
            raise Stop("Rebook link not found exactly once")
        if (await links.first.get_attribute("href")) != "javascript:doEnter(document.CMRCBA,'DFH_ENTER')":
            raise Stop("Rebook link is not the proven doEnter(document.CMRCBA,'DFH_ENTER')")
        async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
            await self.press(links.first)
        await self.page.wait_for_timeout(600)
        await self.clear_notices()
        if not await self.wait_title("Voyage Pricing"):
            raise Stop(f"Rebook did not open Voyage Pricing - now on '{await self.title()}'")

    async def select_own_category(self, category: str) -> dict:
        html = await self.save("5_category_fares")
        rows = pages.category_rows(html)
        if not rows:
            raise NotPriceable("POLAR listed no fares for this sailing (every category row blank)")
        row, why = find_category_row([r["row"] for r in rows], category)
        if row is None:
            raise NotPriceable(why)
        pre = pages.preselected_category(html)
        if pre and pre != category:
            raise Stop(f"the page marks {pre} as the booking's category, the landing page said {category}")
        radio = self.page.locator(f'input[name="CATEGORY_SELECTION"][value="{category}"]')
        if await radio.count() != 1:
            raise Stop(f"FIT radio for {category} not found exactly once")
        await self.press(radio)
        sel = await self.page.evaluate("""() => ({
            cat: (document.querySelector('[name$="-SELCAT"]') || {}).value,
            fare: (document.querySelector('[name$="-SELFARE"]') || {}).value})""")
        if (sel["cat"] or "").strip() != category or (sel["fare"] or "").strip() != "1":
            raise Stop(f"selection not set as expected: {sel}")
        return next(r for r in rows if r["row"] == row)

    async def tick_promos(self, codes: list[str]) -> None:
        """On Fare Comparisons, tick exactly `codes` (at most two - "Select up
        to 2 Promos"). Each row is displayCheckBox('KLS', '<hidden field>', ...):
        the visible box has no name, its onClick names the hidden field, and
        validateCheckBox writes 'S' there. Read back before anything is sent."""
        if not 1 <= len(codes) <= 2:
            raise Stop(f"refused: tick 1 or 2 promos, not {codes}")
        if "Fare Comparisons" not in await self.title():
            raise Stop("refused: promos are ticked only on Fare Comparisons")
        html = await self.page.content()
        fields = dict(re.findall(r"displayCheckBox\('([A-Z0-9]{3})',\s*'([^']+)'", html))
        for code in codes:
            if code not in fields:
                raise Stop(f"promo {code} is not on this sailing's list")
        state = await self.page.evaluate(
            "(f) => Object.fromEntries(f.map(n => [n, (document.querySelector(`input[name='${n}']`) || {}).value]))",
            list(fields.values()))
        for code, name in fields.items():
            want = code in codes
            have = (state.get(name) or "").strip().upper() == "S"
            if want != have:
                box = self.page.locator(f"input[type=checkbox][onclick*=\"{name}\"]")
                if await box.count() != 1:
                    raise Stop(f"checkbox for {code} not found exactly once")
                await self.press(box)
                await self.clear_notices()
        after = await self.page.evaluate(
            "(f) => Object.fromEntries(f.map(n => [n, (document.querySelector(`input[name='${n}']`) || {}).value]))",
            list(fields.values()))
        ticked = sorted(c for c, n in fields.items() if (after.get(n) or "").strip().upper() == "S")
        if ticked != sorted(codes):
            raise Stop(f"promo ticks read back as {ticked}, wanted {sorted(codes)}")

    async def check_booking(self, ref: str, today: date) -> PrincessCheck:
        c = self.check = PrincessCheck(ref=ref)
        html = await self.open_booking(ref)
        if "Booking Confirmation" not in pages.title(html):
            c.outcome = "NOT_OPENED"
            body = re.sub(r"\s+", " ", (await self.page.inner_text("body")))
            # POLAR states why: "RCBI-0006E INVALID DATA ENTERED - ..." (DEMO13
            # 2026-10-16). The first 200 characters of the page cut it off.
            c.why = pages.polar_message(body) or body[:200]
            return c
        c.landing = pages.booking_confirmation(html)
        lines = c.landing["fare_lines"]

        await self.safe_click("Special Requests / Gifts")
        if not await self.wait_title("Special Services"):
            raise Stop("Special Requests did not open")
        c.special_requests = pages.special_requests(await self.save("2_special_requests"))
        await self.safe_click("Back")
        await self.wait_title("Booking Confirmation")

        await self.safe_click("Booking Information")
        if not await self.wait_title("Booking Information"):
            raise Stop("Booking Information did not open")
        c.offer_codes = pages.offer_codes(await self.save("3_booking_information"))
        c.tier = booking_tier(c.offer_codes, [s["description"] for s in c.special_requests])
        c.tier_basis = "offer code / special requests"
        if c.tier is None:
            # Neon's own method: he finds Plus/Premier in the special requests
            # or the offer code. Both read, neither shows a package -> Standard.
            c.tier, c.tier_basis = STANDARD, "no Plus/Premier in special requests or offer code"

        skip = self._why_not_priced(c, lines, today)
        if skip:
            c.outcome, c.why = "NOT_PRICED", skip
            await self.safe_click("Back")
            return c

        await self.rebook_change_fare()
        await self.save("4_voyage_pricing")
        await self.safe_click("Search By Category List")
        if not await self.wait_title("Category Fares"):
            raise Stop("Search By Category List did not open Category Fares")
        row = await self.select_own_category(c.landing["category"])
        c.category_row = row["row"]
        c.best_buy_net_of_obc = row["price_fit_net_of_obc"]
        await self.safe_click("Save & Continue")
        if not await self.wait_title("Pricing Detail"):
            raise Stop(f"Save & Continue did not open Pricing Detail - '{await self.title()}'")
        c.quote = pages.pricing_detail(await self.save("6_pricing_detail"))
        await self.safe_click("Back")
        await self.wait_title("Category Fares")
        await self.safe_click("Fare Comparisons")
        if await self.wait_title("Fare Comparisons"):
            c.promo_list = pages.promo_list(await self.save("7_fare_comparisons"))
            await self.safe_click("Back")
        c.verdict = self._verdict(c, lines, today)
        c.quote_tier = c.verdict.get("quote_tier")
        c.verdict["source"] = "Best Buy"
        c.verdict["category_status"] = row.get("status_fit")
        self._never_unavailable(c.verdict)

        # Best Buy is POLAR's cheapest fare for the category - and can be a
        # different package (DEMO24 2026-10-15: KLP "FALL SAVINGS LTO - PPLUS"
        # on a Standard booking). So the booking's own package is priced too,
        # two promos per pass (POLAR's help: "select two promotions at a
        # time, return to this screen, and repeat").
        codes = self.same_package_promos(c.promo_list, c.tier)
        pairs = [codes[i:i + 2] for i in range(0, len(codes), 2)][:3]
        verdicts = [c.verdict]
        for pair in pairs:
            res = await self.price_promo_pair(ref, pair, c.landing["category"])
            if res.get("quote"):
                v = self._verdict(c, lines, today, res["quote"])
                v["source"] = f"compare {'+'.join(pair)} -> {res['promo']} (column {res['column']})"
                v["deposit_terms"] = res.get("deposit_terms")
                v["category_status"] = res.get("status")
                self._never_unavailable(v)
                terms = (res.get("deposit_terms") or {}).get(res["promo"])
                if v["status"] in ("OPTIMIZATION", "UNCONFIRMED") and terms == "NON_REFUNDABLE":
                    v["note"] += (f" - {res['promo']} carries a 100% NON-REFUNDABLE deposit "
                                  "(POLAR's Important Notices)")
                res["verdict"] = v
                verdicts.append(v)
            c.compares.append({k: v for k, v in res.items() if k != "quote"})
        wins = [v for v in verdicts if v["status"] in ("OPTIMIZATION", "UNCONFIRMED")]
        if wins:
            c.verdict = max(wins, key=lambda v: v["saving"])
        else:
            same_pkg = [v for v in verdicts if v.get("quote_tier") == c.tier] or verdicts
            c.verdict = min(same_pkg, key=lambda v: v["quote_fare_plus_packages"])
        c.verdict["all_quotes"] = [(v["source"], v["quote_fare_plus_packages"], v["status"])
                                   for v in verdicts]
        c.outcome = "PRICED"
        return c

    async def price_promo_pair(self, ref: str, codes: list[str], category: str) -> dict:
        """A fresh walk: booking -> Rebook -> Category Fares -> Fare
        Comparisons -> tick `codes` -> Important Notices -> Category Fare
        Comparisons -> the booking's own category in the cheaper column ->
        Pricing Detail. Proven step by step on DEMO24 2026-10-15."""
        tag = "_".join(codes)
        html = await self.open_booking(ref)
        if "Booking Confirmation" not in pages.title(html):
            raise Stop(f"{ref} did not reopen")
        await self.safe_click("Booking Information")
        if not await self.wait_title("Booking Information"):
            raise Stop("Booking Information did not open")
        await self.rebook_change_fare()
        await self.safe_click("Search By Category List")
        if not await self.wait_title("Category Fares"):
            raise Stop("Category Fares did not open")
        await self.safe_click("Fare Comparisons")
        if not await self.wait_title("Fare Comparisons"):
            raise Stop("Fare Comparisons did not open")
        await self.tick_promos(codes)
        await self.safe_click("Save & Continue")
        await self.page.wait_for_timeout(800)
        notices = {}
        if "Important Notices" in await self.title():
            notices = await self.acknowledge_notices()
        if "Voyage Pricing" in await self.title():
            await self.safe_click("Search By Category List")
        if not await self.wait_title("Category Fare Comparisons"):
            raise Stop(f"compare {tag} did not reach Category Fare Comparisons - '{await self.title()}'")
        html = await self.save(f"9_compare_{tag}")
        promos = pages.comparison_promos(html)
        rows = pages.comparison_rows(html)
        row, why = find_category_row([r["row"] for r in rows], category)
        out = {"codes": codes, "promos": promos,
               "deposit_terms": {k: pages.deposit_terms(v) for k, v in notices.items()}}
        if row is None:
            out["skipped"] = why
            return out
        r = next(x for x in rows if x["row"] == row)
        out["row"] = r
        priced = [(r[f"price_{n}"], n, name, promos[n - 1], r[f"status_{n}"])
                  for n, name in ((1, "CATEGORY_SELECTION"), (2, "CATEGORY_GROUP_SELECTION"))
                  if r[f"price_{n}"] and len(promos) >= n]
        # Only space the booking can move into - never a waitlisted or closed
        # column (DEMO29 / DEMO27 2026-10-15 read 'W/' on the cheaper promo).
        cols = [c for c in priced if pages.category_available(c[4])]
        out["unavailable"] = [(c[3], c[4], c[0]) for c in priced if c not in cols]
        if not cols:
            out["skipped"] = (f"{category} has no available space on {promos} "
                              f"({', '.join(f'{p} {s!r}' for p, s, _ in out['unavailable']) or 'no price'})")
            return out
        _, col, name, promo, status = min(cols)
        radio = self.page.locator(f'input[name="{name}"][value="{category}"]')
        if await radio.count() != 1:
            raise Stop(f"{name} radio for {category} not found exactly once")
        await self.press(radio)
        sel = await self.page.evaluate("""() => ({
            cat: (document.querySelector('[name$="-SELCAT"]') || {}).value,
            col: (document.querySelector('[name$="-SELCOLUMN"]') || {}).value})""")
        if (sel["cat"] or "").strip() != category or (sel["col"] or "").strip() != str(col):
            raise Stop(f"comparison selection not set as expected: {sel}")
        await self.safe_click("Save & Continue")
        if not await self.wait_title("Pricing Detail"):
            raise Stop(f"Save & Continue did not open Pricing Detail - '{await self.title()}'")
        out.update(column=col, promo=promo, status=status,
                   quote=pages.pricing_detail(await self.save(f"10_pricing_{promo}")))
        return out

    async def acknowledge_notices(self) -> dict[str, str]:
        """Important Notices (RDIA): record each promo's notice, tick "I have
        read each notice listed above" (setRecap -> hidden RECAPPFL = 'Y'),
        then Save & Continue (submitRequest() - refuses without the tick).
        Proven live 2026-10-15 on DEMO24: KLS "The deposit of the fare you
        have selected is 100% Non-Refundable", NBS "you have chosen a
        Refundable Deposit"."""
        if "Important Notices" not in await self.title():
            raise Stop("refused: not on Important Notices")
        html = await self.save("8_important_notices")
        notices = pages.promo_notices(html)
        box = self.page.locator('input[type=checkbox][name="RECAPFLAG"]')
        if await box.count() != 1:
            raise Stop("RECAPFLAG checkbox not found exactly once")
        if not await box.is_checked():
            await self.press(box)
        flag = await self.page.evaluate(
            "() => (document.querySelector('[name$=\"-RECAPPFL\"]') || {}).value")
        if (flag or "").strip() != "Y":
            raise Stop(f"acknowledgement did not register (RECAPPFL={flag!r})")
        link = self.page.locator("a").filter(has_text=re.compile(r"^\s*Save\s+&\s+Continue\s*$", re.I))
        if await link.count() != 1 or (await link.first.get_attribute("href")) != "javascript:submitRequest()":
            raise Stop("Important Notices Save & Continue is not the proven submitRequest()")
        async with self.page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
            await self.press(link.first)
        await self.page.wait_for_timeout(600)
        await self.clear_notices()
        return notices

    async def probe_compare(self, ref: str, codes: list[str]) -> str:
        """Walk to Fare Comparisons, tick `codes`, Save & Continue - and STOP
        on whatever screen comes next, saving it. Used to learn the screens
        the HAR never captured (Important Notices, Category Fare Comparisons)
        before any code clicks on them."""
        self.check = PrincessCheck(ref=ref)
        html = await self.open_booking(ref)
        if "Booking Confirmation" not in pages.title(html):
            raise Stop(f"{ref} did not open")
        await self.safe_click("Booking Information")
        if not await self.wait_title("Booking Information"):
            raise Stop("Booking Information did not open")
        await self.rebook_change_fare()
        await self.safe_click("Search By Category List")
        if not await self.wait_title("Category Fares"):
            raise Stop("Category Fares did not open")
        await self.safe_click("Fare Comparisons")
        if not await self.wait_title("Fare Comparisons"):
            raise Stop("Fare Comparisons did not open")
        await self.save("P1_fare_comparisons")
        await self.tick_promos(codes)
        await self.safe_click("Save & Continue")
        await self.page.wait_for_timeout(1000)
        if "Important Notices" in await self.title():
            self.check.landing["promo_notices"] = await self.acknowledge_notices()
        await self.save("P3_after_notices")
        if "Voyage Pricing" in await self.title():
            await self.safe_click("Search By Category List")
            await self.page.wait_for_timeout(1000)
        await self.save("P4_category_fare_comparisons")
        return await self.title()

    @staticmethod
    def _why_not_priced(c: PrincessCheck, lines: dict, today: date) -> str:
        land = c.landing
        st = land["status"]
        if st["cancelled"] or st["waitlisted"]:
            return f"status {st['code']}"
        if land.get("gross_payment_due") is not None and land["gross_payment_due"] <= 0.01:
            return "paid in full"
        fd = parse_polar_date(land.get("final_due"))
        if fd and fd < today:
            return f"past final payment ({land.get('final_due')})"
        if land.get("group"):
            return f"group booking {land['group']} - out of scope for now (Neon)"
        if not lines.get("Base Fare (Incld in Cruise Fare)"):
            return "no base fare on the booking - unusual, left for Neon"
        return ""

    @staticmethod
    def _verdict(c: PrincessCheck, lines: dict, today: date, q: dict | None = None) -> dict:
        land = c.landing
        q = c.quote if q is None else q
        descs = {p["code"]: p["description"] for p in c.promo_list}
        codes = [code for pax in q.get("promo_codes", []) for code in pax]
        quote_descs = [descs[code] for code in dict.fromkeys(codes) if code in descs]
        tiers = [t for t in (package_tier(d) for d in quote_descs) if t]
        quote_tier = booking_tier(promo_descriptions=quote_descs) if tiers else None
        L = q.get("lines", {})
        booking = PrincessBooking(
            booking_id=c.ref, gross=lines.get("Gross") or 0.0, fare=lines.get("Fare"),
            category=land["category"] or "", currency=land.get("currency") or "?", tier=c.tier,
            packages=lines.get("Packages") or 0.0, obc=lines.get("Onboard Credit") or 0.0,
            non_cruise_taxes=lines.get("Non-Cruise Gov't Taxes & Fees") or 0.0,
            gross_payment_due=land.get("gross_payment_due"),
            final_due=parse_polar_date(land.get("final_due")),
            vacation_protection=lines.get("Vacation Protection") or 0.0)
        quote = PrincessQuote(
            promo_code=codes[0] if codes else "?",
            promo_description="; ".join(quote_descs) or "promos not listed",
            category=land["category"] or "", currency=q.get("currency") or "??", tier=quote_tier,
            fare_pp=L.get("Fare", []), gross_total=q.get("totals", {}).get("Gross Fare", 0.0),
            base_pp=L.get("Base Fare (Incld in Cruise Fare)", []),
            fees_pp=L.get("Req'd Cruise Fees & Expenses", []),
            taxes_pp=L.get("Incld Gov't Taxes & Fees", []),
            transfers_pp=L.get("Transfers/Packages", []), obc_pp=L.get("Onboard Credit", []),
            vacation_protection_pp=L.get("Vacation Protection", []),
            mods_pp=L.get("Mods", []),
            non_cruise_taxes_pp=L.get("Non-Cruise Gov't Taxes & Fees", []))
        v: PrincessVerdict = decide(booking, quote, today=today)
        same = abs(booking.cruise_total - quote.cruise_total) < 0.01
        return {"status": v.status, "saving": v.saving, "note": v.note,
                "booking_fare_plus_packages": booking.cruise_total,
                "quote_fare_plus_packages": quote.cruise_total,
                "quote_matches_booking": same, "quote_codes": codes,
                "quote_descriptions": quote_descs, "quote_tier": quote_tier}

    @staticmethod
    def _never_unavailable(v: dict) -> None:
        """A fare in a waitlisted or closed category is not a saving anyone
        can take today (DEMO29 / DEMO27 2026-10-15: 'W/' on KLS)."""
        if v.get("status") in ("OPTIMIZATION", "UNCONFIRMED") and not pages.category_available(v.get("category_status")):
            v["status"], v["saving"] = "NO_SAVING", 0.0
            v["note"] = (f"no saving - the cheaper fare's category status is "
                         f"{v.get('category_status')!r} (not available space); was: {v['note']}")

    @staticmethod
    def same_package_promos(promo_list: list[dict], tier: str | None) -> list[str]:
        """The promos to compare: the booking's own package (Neon: plus ->
        plus rate, premier -> premier, standard -> standard), not expired, not
        an Automatic add-on (M1E 'UPGRADE STANDARD TO PLUS 70' applies itself)."""
        out = []
        for p in promo_list:
            adv = (p.get("advisory") or "").lower()
            if "expired" in adv or "automatic" in adv:
                continue
            if tier and package_tier(p.get("description")) == tier:
                out.append(p["code"])
        return out


# ── The GUI scraper (2026-10-15) ───────────────────────────────────────────
#
# Neon: "we need to finish it now so we start scanning with it tomorrow and
# implement in the gui". The walk is PolarSession's, unchanged - the one
# proven read-only on 33 of his bookings. This class only (1) finds the
# signed-in POLAR tab in the line's own browser, (2) runs one booking, and
# (3) maps the outcome onto the BookingResult every other line produces.
#
# SIGN-IN IS BY HAND for now: Check login opens OneSource, Neon signs in
# and clicks "Book & Manage Cruises"; POLAR opens in a new tab, and the
# login check passes the moment that tab exists. OneSource auto-sign-in is
# not built (no captured login form that is safe to read - the only HAR of
# it holds his password in plain text and is never opened).

_POLAR_TITLE = "POLAR Online"


def _scan_dir() -> str:
    """data/princess_scans/<day> - every page of every walk is kept
    (CLAUDE.md rule 1: never delete, never reduce fidelity)."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "princess_scans", date.today().isoformat())
    os.makedirs(root, exist_ok=True)
    return root


def check_to_result(c: PrincessCheck):
    """A PrincessCheck as a BookingResult. Pure - tested without a browser."""
    land = c.landing or {}
    # BEFORE / AFTER (Neon 2026-10-15: save before and after for every
    # line). BEFORE is what POLAR shows about the booking's own fare: its
    # offer codes (PLUS2, PREMIER...) and the package tier they imply. The
    # booking's CURRENT promo codes are not on any page this walk opens
    # (landing, Special Requests, Booking Information - checked on DEMO24
    # and DEMO12), so they are not invented here: open item in
    # docs/PRINCESS_POLAR.md. AFTER is the quote's own promo codes.
    before = ", ".join(c.offer_codes or [])
    if c.tier:
        before = f"{before} [{c.tier} package]".strip()
    v_ = c.verdict or {}
    after_codes = list(dict.fromkeys(v_.get("quote_codes") or []))
    after = ", ".join(after_codes)
    if v_.get("quote_descriptions"):
        after += f" ({'; '.join(v_['quote_descriptions'])})"
    base = {"cruise_line": CruiseLine.PRINCESS, "booking_id": c.ref,
            "price_category": land.get("category"),
            "currency": land.get("currency") or "UNKNOWN",
            "old_promos": before, "new_promos": after}
    notices = f" [POLAR notices: {', '.join(c.notices)}]" if c.notices else ""

    if c.outcome == "NOT_OPENED" and "INVALID DATA ENTERED" in (c.why or "").upper():
        # Nothing is broken: POLAR rejects the number on this login (DEMO13,
        # 2026-10-15 and -16). Same status NCL uses for "not found here".
        return BookingResult(**base, status=BookingStatus.NOT_ON_THIS_ACCOUNT,
                             note=f"POLAR does not accept {c.ref} on this OneSource login "
                                  f"({c.why}) - check the booking number")
    if c.outcome == "NOT_OPENED":
        return BookingResult(**base, status=BookingStatus.ERROR,
                             error=f"POLAR did not open the booking: {c.why}",
                             note="booking not opened on POLAR")
    if c.outcome == "STOPPED":
        return BookingResult(**base, status=BookingStatus.ERROR,
                             error=f"stopped safely, nothing pressed: {c.why}",
                             note=f"stopped safely - {c.why}{notices}")
    if c.outcome == "NOT_PRICEABLE":
        return BookingResult(**base, status=BookingStatus.NO_SAVING,
                             note=f"not priceable - {c.why}{notices}")
    if c.outcome == "NOT_PRICED":
        st = land.get("status") or {}
        why = c.why or ""
        if why == "paid in full":
            status = BookingStatus.PAID_IN_FULL
        elif st.get("cancelled"):
            status = BookingStatus.CANCELLED
        else:
            status = BookingStatus.NO_SAVING   # waitlisted, past final, group
        return BookingResult(**base, status=status, note=f"not priced - {why}{notices}")

    v = c.verdict or {}
    status = {"OPTIMIZATION": BookingStatus.OPTIMIZATION,
              "PAID_IN_FULL": BookingStatus.PAID_IN_FULL,
              "ERROR": BookingStatus.ERROR}.get(v.get("status"), BookingStatus.NO_SAVING)
    saving = float(v.get("saving") or 0.0) if status == BookingStatus.OPTIMIZATION else 0.0
    old = v.get("booking_fare_plus_packages") or 0.0
    new = v.get("quote_fare_plus_packages") or old
    codes = "+".join(dict.fromkeys(v.get("quote_codes") or [])) or None
    note = (f"{v.get('note', '')} [{c.tier or 'unknown'} package, priced via "
            f"{v.get('source', '?')}; fare + packages only - air, mods and services "
            f"excluded]{notices}")
    return BookingResult(
        **base, status=status, new_price_category=codes,
        old_total=float(old), new_total=float(new) if status == BookingStatus.OPTIMIZATION else float(old),
        price_drop=saving, net_saving=saving,
        # Priced on POLAR's own Pricing Detail - an exact quote, not an
        # estimate, so the same top score as a GoCCL review-confirmed price.
        confidence=GOCCL_CONFIRMED_CONFIDENCE if status == BookingStatus.OPTIMIZATION else 0,
        note=note.strip(),
        error=v.get("note") if status == BookingStatus.ERROR else None)


class PrincessScraper(BaseScraper):
    """Princess POLAR Online, read-only. See PolarSession for every control
    that is ever pressed, and FORBIDDEN for the ones that never are."""

    cruise_line = CruiseLine.PRINCESS

    def __init__(self):
        super().__init__()
        self._polar: PolarSession | None = None

    async def _polar_page(self):
        """The signed-in POLAR tab in this browser, or None."""
        try:
            pages_now = list(self.page.context.pages)
        except Exception:
            return None
        for p in pages_now:
            try:
                if (await p.title()).startswith(_POLAR_TITLE):
                    return p
            except Exception:
                continue
        return None

    async def _check_login(self) -> bool:
        """Logged in = a POLAR Online tab exists. Switches this scraper onto
        it, so the scan and the login check look at the same tab."""
        page = await self._polar_page()
        if page is None:
            # An idle POLAR tab ends on "CICS Web Interface error" (seen live
            # 2026-10-15, hours after the read-only run): signed out, the
            # tab still at /polaronline/pres. Named so the log says why.
            for p in list(self.page.context.pages):
                try:
                    if "polaronline" in (p.url or "") and "CICS" in await p.title():
                        logger.warning("princess.polar_session_expired", url=p.url[:80])
                        self.log_action("polar_session_expired")
                        break
                except Exception:
                    continue
            return False
        if page is not self._page:
            self._page = page
            self._polar = None
        return True

    # OneSource's sign-in form, read from the live page 2026-10-15 - windowed
    # and headless identical: input#userName, input#password, and one
    # button[type=submit] "Sign In" in the form posting to /onesource/login.
    _OS_USER = "input#userName"
    _OS_PASS = "input#password"
    _OS_SUBMIT = "button[type=submit]"

    async def auto_login(self) -> str:
        """Sign in to OneSource with the credential saved by SAVE_LOGIN
        (option 6). NEVER RAISES; never logs or returns the credential.
        Returns OK (POLAR open), SIGNED_IN_NO_POLAR, ALREADY_LOGGED_IN,
        NO_CREDENTIALS_SAVED, LOGIN_REJECTED, NO_LOGIN_FORM or ERROR: ..."""
        try:
            import keyring

            from config.settings import settings

            user = keyring.get_password(settings.princess_credential_service, "username")
            pwd = keyring.get_password(settings.princess_credential_service, "password")
            if not user or not pwd:
                return "NO_CREDENTIALS_SAVED"
            if await self._check_login():
                return "ALREADY_LOGGED_IN"
            if "/onesource/home" not in (self.page.url or ""):
                if await self.page.locator(self._OS_USER).count() != 1:
                    await self.navigate(settings.princess_login_url)
                # WAIT FOR THE FORM TO HYDRATE. Live 2026-10-15: the first
                # attempt filled the server-rendered inputs before the page's
                # script took them over, the values were wiped, and the
                # submit went out empty. Waiting for the field and for the
                # network to settle, then reading the values back, fixed it
                # (second attempt: POST .../osmf/p1.0/login 200 ->
                # /onesource/home/pc "Home - Onesource").
                # A saved session redirects /onesource/login -> /onesource/home
                # a moment AFTER the page loads (live 2026-10-15, windowed and
                # headless) - so wait for EITHER the form or home.
                for _ in range(40):                 # up to 20 s
                    if "/onesource/home" in (self.page.url or ""):
                        break
                    if await self.page.locator(f"{self._OS_PASS}:visible").count():
                        break
                    await asyncio.sleep(0.5)
                await self._settle_after_signin()
            if "/onesource/home" not in (self.page.url or ""):
                box_u = self.page.locator(self._OS_USER)
                box_p = self.page.locator(self._OS_PASS)
                submit = self.page.locator(self._OS_SUBMIT).filter(
                    has_text=re.compile(r"^\s*Sign In\s*$", re.I))
                if await box_u.count() != 1 or await box_p.count() != 1 or await submit.count() != 1:
                    logger.warning("princess.auto_login", result="NO_LOGIN_FORM",
                                   url=self.page.url[:80])
                    return "NO_LOGIN_FORM"
                await box_u.fill(user)
                await box_p.fill(pwd)
                held = await self.page.evaluate(
                    "() => [document.querySelector('#userName').value.length,"
                    " document.querySelector('#password').value.length]")
                if held != [len(user), len(pwd)]:
                    logger.warning("princess.auto_login", result="FORM_DID_NOT_HOLD", held=held)
                    return "ERROR: the sign-in form did not keep the typed values"
                await submit.click()
                for _ in range(90):                 # up to 45 s to reach the home page
                    await asyncio.sleep(0.5)
                    if "/onesource/home" in (self.page.url or ""):
                        break
                await self._settle_after_signin()
            if "/onesource/home" not in (self.page.url or ""):
                logger.warning("princess.auto_login", result="LOGIN_REJECTED",
                               url=self.page.url[:80])
                return "LOGIN_REJECTED"
            opened = await self._open_polar()
            result = "OK" if opened else "SIGNED_IN_NO_POLAR"
            logger.info("princess.auto_login", result=result, url=self.page.url[:80])
            return result
        except Exception as exc:  # noqa: BLE001 - a login must never crash a run
            logger.warning("princess.auto_login_failed", error=str(exc)[:200])
            return f"ERROR: {exc}"[:200]

    async def _settle_after_signin(self) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    async def _open_polar(self) -> bool:
        """Reach POLAR from the OneSource home page - what Neon does by hand.

        Read from the live signed-in page 2026-10-15: the top bar has a
        BUTTON whose text is exactly "Book & Manage Cruises" (no href). The
        footer has a LINK with the same words that only opens a help page
        (/booking-tools/booking-help) - so only a <button> is accepted, and
        only when there is exactly one visible. POLAR opens in a new tab or
        in this one; either way the tab titled "POLAR Online" is adopted."""
        if await self._check_login():
            return True
        if "/onesource/home" not in (self.page.url or ""):
            return False
        button = self.page.locator("button:visible").filter(
            has_text=re.compile(r"^\s*Book\s*&\s*Manage\s+Cruises\s*$", re.I))
        if await button.count() != 1:
            logger.warning("princess.open_polar", result="BUTTON_NOT_FOUND_ONCE",
                           count=await button.count())
            return False
        await button.click()
        for _ in range(90):                          # up to 45 s for POLAR
            await asyncio.sleep(0.5)
            if await self._check_login():
                logger.info("princess.open_polar", result="OK")
                return True
        logger.warning("princess.open_polar", result="POLAR_DID_NOT_OPEN")
        return False

    def _session(self) -> PolarSession:
        # One PolarSession per page: it registers a dialog handler on the
        # page, and a second one would answer the same dialog twice.
        if self._polar is None or self._polar.page is not self.page:
            self._polar = PolarSession(self.page, _scan_dir())
        else:
            self._polar.out_dir = _scan_dir()
        return self._polar

    async def check_booking(self, booking_id: str, capture_market_data: bool = False):
        self.reset_per_booking_state()
        ref = (booking_id or "").strip().upper()
        if not await self._check_login():
            raise RuntimeError("Not logged in to Princess POLAR - press Check login, sign in "
                               "to OneSource and click Book & Manage Cruises")
        s = self._session()
        today = date.today()
        self.log_action("princess_check", booking_id=ref)
        try:
            c = await s.check_booking(ref, today)
        except NotPriceable as exc:
            c = s.check if s.check and s.check.ref == ref else PrincessCheck(ref=ref)
            c.outcome, c.why = "NOT_PRICEABLE", str(exc)
        except Stop as exc:
            c = s.check if s.check and s.check.ref == ref else PrincessCheck(ref=ref)
            c.outcome, c.why = "STOPPED", str(exc)
            try:
                await s.save("STOPPED_page")
            except Exception:
                pass
        finally:
            # Leave POLAR on its menu between bookings, never mid-wizard.
            try:
                if "MAIN MENU" not in await s.title():
                    await s.safe_click("MAIN MENU")
            except Exception:
                pass
        try:
            with open(os.path.join(s.out_dir, f"{ref}_result.json"), "w", encoding="utf-8") as fh:
                json.dump(c.__dict__, fh, indent=1, default=str)
        except Exception:
            pass
        result = check_to_result(c)
        self.log_action("princess_result", booking_id=ref, outcome=c.outcome,
                        status=result.status.value, saving=result.net_saving)
        return result


async def run(refs: list[str], out_dir: str, today: date | None = None) -> list[PrincessCheck]:
    """Check `refs` in the signed-in window polar_browser keeps open."""
    from playwright.async_api import async_playwright

    today = today or date.today()
    os.makedirs(out_dir, exist_ok=True)
    results: list[PrincessCheck] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = None
        for p in ctx.pages:
            if (await p.title()).startswith("POLAR Online"):
                page = p
        if page is None:
            raise Stop("no POLAR tab open - sign in and click Book & Manage Cruises first")
        s = PolarSession(page, out_dir)

        def dump():
            with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
                json.dump({"results": [r.__dict__ for r in results], "dialogs": s.dialogs},
                          fh, indent=1, default=str)

        for n, ref in enumerate(refs, 1):
            try:
                r = await s.check_booking(ref, today)
            except NotPriceable as exc:
                r = s.check or PrincessCheck(ref=ref)
                r.outcome, r.why = "NOT_PRICEABLE", str(exc)
            except Stop as exc:
                r = s.check or PrincessCheck(ref=ref)
                r.outcome, r.why = "STOPPED", str(exc)
                await s.save("STOPPED_page")
                results.append(r)
                dump()
                print(f"{n}/{len(refs)} {ref}: STOPPED - {exc}", flush=True)
                break
            results.append(r)
            dump()
            v = r.verdict
            print(f"{n}/{len(refs)} {ref}: {r.outcome} {r.why or ''} tier={r.tier} "
                  f"{(v.get('status') or '')} saving={v.get('saving')} "
                  f"now={v.get('booking_fare_plus_packages')} quote={v.get('quote_fare_plus_packages')} "
                  f"codes={v.get('quote_codes')} notices={r.notices}", flush=True)
            await asyncio.sleep(2)
        if "MAIN MENU" not in await s.title():
            try:
                await s.safe_click("MAIN MENU")
            except Stop:
                pass
        dump()
    return results
