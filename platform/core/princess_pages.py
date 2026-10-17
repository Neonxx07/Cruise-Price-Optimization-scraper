"""Readers for POLAR Online pages - pure functions over the page's own HTML.

Every pattern here was read off Neon's DEMO12 HAR (2026-10-15) and checked
against the 32 bookings of the live read-only run the same day
(data/princess_readonly_20261015T*). POLAR writes its data either as plain
`<td><strong>Label</strong></td><td>value</td>` rows or as arguments of its
own JavaScript calls (displayRow, showSpecialRequests, displayCheckBox,
showPassengerRow) - both are read as data, never from pixels.
See docs/PRINCESS_POLAR.md.
"""
from __future__ import annotations

import html as _html
import re

_WS = re.compile(r"\s+")


def _text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<script.*?</script>", " ", fragment)
    return _WS.sub(" ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def money(text: str | None) -> float | None:
    """'6,534.00' -> 6534.0; '665.50-' -> -665.5; '325.00 USD' -> 325.0; else None."""
    if text is None:
        return None
    t = re.sub(r"[A-Z$,]", "", text.upper()).strip()
    neg = t.endswith("-")
    t = t.rstrip("-").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def currency(page_html: str, section: str) -> str | None:
    """The 'Currency USD' line right under a section heading (CRUISE FARE
    INFORMATION on the landing page, PASSENGER PRICING on Pricing Detail).
    None if not shown - never assumed USD (group TND is CAD)."""
    k = page_html.find(section)
    if k < 0:
        return None
    m = re.search(r"Currency\s+([A-Z]{3})\b", _text(page_html[k:k + 3000]))
    return m.group(1) if m else None


def title(page_html: str) -> str:
    m = re.search(r"<title>([^<]*)</title>", page_html)
    return _WS.sub(" ", m.group(1)).strip() if m else ""


# -- Booking Confirmation (RCBD) - the booking's own landing page ------------

def booking_status(page_html: str) -> dict:
    """POLAR writes it into the page: var bkStatus = "B"; isCanxld = ("C"==
    bkStatus); isWtLstd = ("W"==bkStatus). Seen live: B Booked, G Guarantee."""
    st = re.search(r'var\s+bkStatus\s*=\s*"([^"]*)"', page_html)
    days = re.search(r'var\s+daysOut\s*=\s*t\("\s*(\d*)"\)', page_html)
    code = st.group(1).strip() if st else None
    return {"code": code, "cancelled": code == "C", "waitlisted": code == "W",
            "guarantee": code == "G",
            "days_out": int(days.group(1)) if days and days.group(1) else None}


def _labelled_cells(segment: str) -> dict[str, str]:
    """Rows of <td>Label</td> ... <td>value</td>: first label wins."""
    out: dict[str, str] = {}
    for row in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", segment):
        cells = [_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row.group(1))]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            label = re.sub(r"\s*\*\s*$", "", cells[0]).strip()
            out.setdefault(label, cells[-1])
    return out


_SUMMARY_IDS = {"Group": "grpInfoVal"}
_SUMMARY_LABELS = ("Booking", "Category", "Cabin", "Ship", "Sail Date", "Voyage", "Route")


def booking_confirmation(page_html: str) -> dict:
    """Everything the landing page says about the booking and its price.

    `fare_lines` holds EVERY line of CRUISE FARE INFORMATION, because Gross is
    not the fare: live 2026-10-15, Gross = Fare + Packages + Air + Mods +
    Services + Miscellaneous (DEMO27 air 1,433.22; DEMO24 mods 96.00; DEMO25
    services 1,031.76)."""
    out: dict = {"status": booking_status(page_html)}
    for label in _SUMMARY_LABELS:
        m = re.search(rf'(?is)>\s*{re.escape(label)}\s*</td>(.*?)<td[^>]*content[^>]*>(.*?)</td>',
                      page_html)
        out[label.lower().replace(" ", "_")] = _text(m.group(2)) if m else None
    g = re.search(r'id="grpInfoVal"[^>]*>(.*?)</td>', page_html, re.S)
    out["group"] = (_text(g.group(1)) or None) if g else None

    k = page_html.find("CRUISE FARE INFORMATION")
    e = page_html.find("COMMISSION INFORMATION", k)
    cells = _labelled_cells(page_html[k:e]) if k >= 0 else {}
    fare_lines = {}
    for label, value in cells.items():
        amount = money(value.split(";")[-1])     # "Air *" carries a document.write(...) prefix
        if amount is not None and not re.fullmatch(r"\d{2}[A-Z]{3}\d{2}", label):
            fare_lines[label] = amount
    # Vacation Protection's label sits in a nested table, so the row reader
    # misses it; the page's print copy carries it plainly (DEMO25 2026-10-15:
    # <tr id="waiverFarePrint"> ... Vacation Protection </b></td><td> 204.30).
    vp = re.search(r'(?is)<tr id="waiverFarePrint">.*?</b>\s*</td>\s*<td[^>]*>(.*?)</td>', page_html)
    if vp and money(_text(vp.group(1))) is not None:
        fare_lines["Vacation Protection"] = money(_text(vp.group(1)))
    out["fare_lines"] = fare_lines
    out["currency"] = currency(page_html, "CRUISE FARE INFORMATION")

    # PAYMENT HISTORY: read each label's own row - the words "PAYMENT HISTORY"
    # also occur earlier in the page's script, so a section slice misses it.
    out["gross_payment_due"] = money(_row_value(page_html, "Gross Payment Due"))
    out["net_payment_due"] = money(_row_value(page_html, "Net Payment Due"))
    out["total_received"] = money(_row_value(page_html, "Total Received"))
    out["final_due"] = _row_value(page_html, "Final Due Date")
    return out


def _row_value(page_html: str, label: str) -> str | None:
    """The cell after `<strong>Label…</strong></td>` (label may run on: 'Total
    Received (Less )')."""
    m = re.search(rf"(?is)<strong>\s*{re.escape(label)}.*?</strong>\s*</td>\s*<td[^>]*>(.*?)</td>",
                  page_html)
    return (_text(m.group(1)) or None) if m else None


# -- Special Requests / Gifts (RCTB) ----------------------------------------

def special_requests(page_html: str) -> list[dict]:
    """showSpecialRequests(field, description, ' ', pax, date, qty, price, currency, ...)"""
    out = []
    for m in re.finditer(r'showSpecialRequests\(\s*"[^"]*"\s*,\s*"([^"]*)"\s*,\s*\'[^\']*\'\s*,'
                         r'\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,'
                         r'\s*"([^"]*)"', page_html):
        if not m.group(1).strip():
            continue
        out.append({"description": m.group(1).strip(), "pax": m.group(2).strip(),
                    "date": m.group(3).strip(), "qty": m.group(4).strip(),
                    "price": money(m.group(5)), "currency": m.group(6).strip()})
    return out


# -- Booking Information (RCBA) --------------------------------------------

def offer_codes(page_html: str) -> list[str]:
    """The OFFER CODE boxes (`...CMRCBA-COUPON`), e.g. PLUS2 / PLS2PREM / PREMIER."""
    # The real <input> tags only - the page's script also contains the text
    # '-COUPON" value="' inside string literals.
    return re.findall(r'<input name="F\d+_CMRCBA-COUPON" value="([A-Z0-9]{1,10})\s*"', page_html)


# -- Category Fares - Best Buy (RDIC) ----------------------------------------

def category_rows(page_html: str) -> list[dict]:
    """displayRow(code, statFIT, statGroup, priceFIT, priceGroup, promo, promoGroup, ...).

    The FIT price is marked "* = Price net of On Board Credit" - never cash.
    The radio value is the first two characters (displayRadio:
    `trim(product.substring(0,2))`)."""
    out = []
    # Category Fares writes displayRow("B43","3",...); Category Fare
    # Comparisons writes displayRow('BC+','9',...) - either quote.
    arg = r"""["']([^"']*)["']"""
    for m in re.finditer(r"displayRow\(" + r"\s*,\s*".join([arg] * 7), page_html):
        code = m.group(1)
        if not code.strip() or code.strip().startswith("Y"):
            continue
        out.append({"row": code.strip(), "category": code.strip()[:2].strip(),
                    "status_fit": m.group(2).strip(), "status_group": m.group(3).strip(),
                    "price_fit_net_of_obc": money(m.group(4).replace("*", "")),
                    "price_group": money(m.group(5)), "promo_fit": m.group(6).strip(),
                    "promo_group": m.group(7).strip()})
    return out


def preselected_category(page_html: str) -> str | None:
    """displayRadio pre-ticks the booking's own category: productVal==trim("B4")."""
    m = re.search(r'productVal==trim\("([^"]*)"\)', page_html)
    return m.group(1).strip() if m and m.group(1).strip() else None


# -- Fare Comparisons (RDLF) - the promo list --------------------------------

def promo_list(page_html: str) -> list[dict]:
    """displayCheckBox('M1E', ...) then cells: code, expiry, description, advisory."""
    out = []
    for m in re.finditer(r"displayCheckBox\('([A-Z0-9]{3})'", page_html):
        tail = page_html[m.end():m.end() + 2500]
        cells = [_text(c) for c in re.findall(r"(?is)<div[^>]*>(.*?)</div>", tail)][:4]
        if len(cells) < 3:
            continue
        out.append({"code": m.group(1).strip(), "expires": cells[1],
                    "description": cells[2], "advisory": cells[3] if len(cells) > 3 else ""})
    return out


# -- Pricing Detail (RDLE) - the itemised quote ------------------------------

_PRICING_LABELS = ("Fare", "Base Fare (Incld in Cruise Fare)", "Air", "Vacation Protection",
                   "Transfers/Packages", "Mods", "Gross Fare", "Onboard Credit",
                   "Req'd Cruise Fees & Expenses", "Incld Gov't Taxes & Fees", "FCC Discount",
                   # Taxes on transfers/packages - INSIDE Gross Fare. Missing
                   # until 2026-10-16: 5 of 51 first-GUI-run quotes failed
                   # "does not add up" by exactly 5.00 per guest (DEMO28:
                   # Fare 1,685 + Transfers 93 + this 5 = Gross 1,783).
                   "Non-Cruise Gov't Taxes & Fees")


def polar_message(text: str) -> str | None:
    """POLAR's own coded message, e.g. 'RCBI-0006E INVALID DATA ENTERED -
    BOOKING SEARCH OPTIONS' (DEMO13 2026-10-16), else None."""
    m = re.search(r"\b[A-Z]{4}-\d{4}[A-Z]\b[^.|]{0,90}", text or "")
    return m.group(0).strip() if m else None


def pricing_detail(page_html: str) -> dict:
    """Per-passenger lines (the Total column dropped) and each guest's promo codes."""
    lines: dict[str, list[float]] = {}
    totals: dict[str, float] = {}
    for row in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", page_html):
        # "Vacation Protection *" - POLAR marks some labels with an asterisk
        # (DEMO25 2026-10-15: 103.35 pp of automatically added cover).
        cells = [re.sub(r"\s*\*\s*$", "", _text(c)) for c in
                 re.findall(r"(?is)<td[^>]*>(.*?)</td>", row.group(1))]
        label_idx = next((i for i, c in enumerate(cells) if c in _PRICING_LABELS), None)
        if label_idx is None or cells[label_idx] in lines:
            continue
        values = cells[label_idx + 1:]
        if len(values) < 2:
            continue
        total = money(values[-1])
        per_pax = [money(v) for v in values[:-1]]
        lines[cells[label_idx]] = [v for v in per_pax if v is not None]
        if total is not None:
            totals[cells[label_idx]] = total
    # Vacation Protection's label sits in a nested table, which ends the row
    # reader's row early; the amounts follow in <tr id="insPricing">'s own
    # cells (DEMO25 2026-10-15: 103.35 103.35 ... 206.70).
    ins = re.search(r'(?is)<tr id="insPricing".*?</table>\s*</td>(.*?)</tr>', page_html)
    if ins and not lines.get("Vacation Protection"):
        values = [_text(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", ins.group(1))]
        if len(values) >= 2:
            lines["Vacation Protection"] = [v for v in (money(x) for x in values[:-1]) if v is not None]
            total = money(values[-1])
            if total is not None:
                totals["Vacation Protection"] = total
    pax_codes = []
    for m in re.finditer(r"showPassengerRow\((.*?)\);", page_html, re.S):
        args = re.findall(r'"([^"]*)"', m.group(1))
        try:
            start = max(i for i, a in enumerate(args) if a.endswith("-GRATUITY")) + 1
        except ValueError:
            continue
        pax_codes.append([a.strip() for a in args[start:]
                          if re.fullmatch(r"[A-Z0-9]{3}", a.strip())])
    return {"lines": lines, "totals": totals, "promo_codes": pax_codes,
            "currency": currency(page_html, "PASSENGER PRICING")}


# -- Important Notices (RDIA) -----------------------------------------------

def promo_notices(page_html: str) -> dict[str, str]:
    """'Fare Code: KLS  Notice: ... 100% Non-Refundable.' per promo, as the
    page renders it (showRemarks(promo, [...]) writes the rows)."""
    text = _text(page_html)
    out = {}
    for m in re.finditer(r"Fare Code:\s*([A-Z0-9]{3})\s*Notice:\s*(.*?)(?=Fare Code:|I have read each notice|$)",
                         text):
        out[m.group(1)] = re.sub(r"\*+", "", m.group(2)).strip()
    return out


def deposit_terms(notice: str) -> str | None:
    """NON_REFUNDABLE / REFUNDABLE from an Important Notices line, else None."""
    n = (notice or "").upper()
    if "NON-REFUNDABLE" in n and "OVERRIDES" not in n:
        return "NON_REFUNDABLE"
    if "REFUNDABLE DEPOSIT" in n:
        return "REFUNDABLE"
    return None


def category_available(status: str | None) -> bool:
    """Space the booking can actually move into. POLAR's help (RDIC): the
    Status column "shows available space"; "W means waitlist and C means
    closed". Live 2026-10-15: '9', '3', '1' (counts), 'G' (guarantee), 'W/',
    'B/2'. Only a count or a guarantee is available - anything else,
    including a status not yet seen, is not (DEMO29 and DEMO27: their own
    category read 'W/' on the cheaper promo)."""
    s = (status or "").strip().upper()
    return bool(re.fullmatch(r"\d+|G|G/G", s))


# -- Category Fare Comparisons (RDLK) - two promos side by side -------------

def comparison_promos(page_html: str) -> list[str]:
    """The two compared promos, left then right: the header reads 'Promo KLS'
    and the right one sits in an editable box (Promo [NBS] UPDATE)."""
    # <strong>Promo</strong> &nbsp;KLS ... <strong>Promo</strong> &nbsp;<input
    # id="promoInput" name="F040290003_CMRDLK-FARE2COD" value="NBS" ...>
    left = re.search(r"<strong>Promo</strong>\s*&nbsp;\s*([A-Z0-9]{3})\b", page_html)
    right = re.search(r'<input[^>]*name="[^"]*-FARE2COD"[^>]*value="([A-Z0-9]{3})\s*"', page_html)
    return [x.group(1) for x in (left, right) if x]


def comparison_rows(page_html: str) -> list[dict]:
    """Same displayRow(...) as Category Fares: column 1 = left promo,
    column 2 = right promo. Prices are per person, taxes and port expenses
    EXCLUDED ("Additional Taxes, Fees & Port Expenses: **") - only Pricing
    Detail gives a comparable figure."""
    out = []
    for r in category_rows(page_html):
        out.append({"row": r["row"], "category": r["category"],
                    "status_1": r["status_fit"], "price_1": r["price_fit_net_of_obc"],
                    "status_2": r["status_group"], "price_2": r["price_group"]})
    return out
