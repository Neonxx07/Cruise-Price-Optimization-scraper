"""Price comparison and optimization engine.

Ported from calculator.js — the core business logic of the system.
Contains both ESPRESSO (Royal Caribbean / Celebrity) and NCL (Norwegian)
calculation engines.
"""

from __future__ import annotations

import re
from datetime import datetime

from .confidence import calc_confidence
from .models import BookingResult, BookingStatus, CruiseLine, InvoiceItem


# ── Utility Functions ───────────────────────────────────────────


def safe_float(value) -> float:
    """Safely parse any value to float, defaulting to 0."""
    try:
        result = float(value)
        return 0.0 if result != result else result  # NaN check
    except (TypeError, ValueError):
        return 0.0


def round2(x) -> float:
    """Round to 2 decimal places."""
    return round(safe_float(x) * 100) / 100


def norm_str(s: str | None) -> str:
    """Normalize a string: strip + uppercase."""
    return (s or "").strip().upper()


# ── ESPRESSO Fee Detection ──────────────────────────────────────

ESPRESSO_FEE_TYPES = frozenset([
    "VACATION_TOTAL", "OBC_TOTAL", "PORT_CHARGE", "PORT_EXPENSES",
    "GOVERNMENT_TAX", "TAXES_AND_FEES", "NCF", "NCCF", "CRUISE",
    "CRUISEFARE", "GRATUITIES", "TAX", "FEE",
])

_FEE_NAME_PREFIX_RE = re.compile(r"^(NCCF|NCF|PORT|TAX|FEE|GOVERNMENT|GRATUIT)")


def _is_espresso_fee(item: dict) -> bool:
    """Check if an invoice item is a standard fee (not a package)."""
    item_type = norm_str(item.get("type", ""))
    if item_type in ESPRESSO_FEE_TYPES:
        return True
    name = norm_str(item.get("name", "") or item.get("normalizedName", ""))
    if _FEE_NAME_PREFIX_RE.match(name):
        return True
    if " OBC" in name or name.endswith("OBC") or name.startswith("OBC "):
        return True
    return False


def _get_total(items: list[dict], fee_type: str) -> float:
    """Get the total-row amount for a specific fee type."""
    for item in items:
        if item.get("paxId") == "total" and norm_str(item.get("type", "")) == fee_type:
            return safe_float(item.get("amount", 0))
    return 0.0


def _get_cruise_fare(items: list[dict]) -> float:
    """Extract cruise fare from invoice items."""
    # Try direct match first
    for item in items:
        if item.get("paxId") == "total" and (item.get("type", "") or "") in (
            "CRUISE", "CRUISEFARE", "cruise"
        ):
            return safe_float(item.get("amount", 0))

    # Fallback: largest non-fee total
    skip = frozenset([
        "VACATION_TOTAL", "OBC_TOTAL", "TAXES_AND_FEES",
        "PORT_CHARGE", "PORT_EXPENSES", "GOVERNMENT_TAX", "NCF", "NCCF",
    ])
    best = 0.0
    for item in items:
        if item.get("paxId") != "total":
            continue
        if norm_str(item.get("type", "")) in skip:
            continue
        amount = safe_float(item.get("amount", 0))
        if amount > best:
            best = amount
    return best


def _get_packages(items: list[dict]) -> list[dict]:
    """Get all package (non-fee) items with positive amounts."""
    return [
        item
        for item in items
        if item.get("paxId") == "total"
        and safe_float(item.get("amount", 0)) > 0
        and not _is_espresso_fee(item)
    ]


# ── Re-Addable Fare Detection ──────────────────────────────────

_READDABLE_PATTERNS = [
    re.compile(r"email", re.IGNORECASE),
    re.compile(r"bonus", re.IGNORECASE),
    re.compile(r"promo", re.IGNORECASE),
    re.compile(r"loyalty", re.IGNORECASE),
    re.compile(r"coupon", re.IGNORECASE),
]


def _is_re_addable(fare_name: str) -> bool:
    """Check if a fare can likely be re-added after repricing."""
    return any(p.search(fare_name) for p in _READDABLE_PATTERNS)


# ── ESPRESSO Calculator ────────────────────────────────────────


def calculate_espresso(raw_data: dict, booking_id: str, price_category: str | None = None) -> BookingResult:
    """
    Analyze an ESPRESSO booking response and determine optimization status.

    This is the main ESPRESSO calculation engine, ported from calculateESPRESSO()
    in the original calculator.js.

    Args:
        raw_data: Raw API response from ESPRESSO reprice modal.
        booking_id: The booking ID.
        price_category: Current price category code.

    Returns:
        BookingResult with status, savings, confidence, and details.
    """
    try:
        data = raw_data.get("result", raw_data)
        old_items = (data.get("oldInvoice") or {}).get("invoiceItems", [])
        new_items = (data.get("newInvoice") or {}).get("invoiceItems", [])

        old_total = _get_total(old_items, "VACATION_TOTAL")
        new_total = _get_total(new_items, "VACATION_TOTAL")
        old_obc = _get_total(old_items, "OBC_TOTAL")
        new_obc = _get_total(new_items, "OBC_TOTAL")

        price_drop = round2(old_total - new_total)
        obc_change = round2(new_obc - old_obc)

        # Package loss detection
        old_pkgs = _get_packages(old_items)
        new_pkg_names = set(
            norm_str(i.get("name", "") or i.get("normalizedName", ""))
            for i in _get_packages(new_items)
        )
        new_pkg_names.discard("")

        lost_pkgs = [
            i for i in old_pkgs
            if norm_str(i.get("name", "") or i.get("normalizedName", ""))
            and norm_str(i.get("name", "") or i.get("normalizedName", "")) not in new_pkg_names
        ]
        lost_pkg_value = round2(sum(safe_float(i.get("amount", 0)) for i in lost_pkgs))
        lost_pkg_names = [
            i.get("name", "") or i.get("normalizedName", "")
            for i in lost_pkgs
            if i.get("name") or i.get("normalizedName")
        ]

        net = round2(price_drop + obc_change - lost_pkg_value)

        # Fare analysis
        old_fare_names = [f.get("name", "") for f in (data.get("oldFares") or []) if f.get("name")]
        new_fare_names = [f.get("name", "") for f in (data.get("newFares") or []) if f.get("name")]
        new_fare_set = set(norm_str(f) for f in new_fare_names)
        old_fare_set = set(norm_str(f) for f in old_fare_names)
        all_lost_fares = [f for f in old_fare_names if norm_str(f) not in new_fare_set]
        re_addable_fares = [f for f in all_lost_fares if _is_re_addable(f)]
        truly_lost_fares = [f for f in all_lost_fares if not _is_re_addable(f)]
        gained_fares = [f for f in new_fare_names if norm_str(f) not in old_fare_set]

        # Status determination
        re_add_note = (" — re-add: " + ", ".join(re_addable_fares)) if re_addable_fares else ""

        if net > 0:
            status = BookingStatus.OPTIMIZATION
            note = f"optimized ${round(net)}{re_add_note}"
        elif price_drop > 0 and net <= 0:
            status = BookingStatus.TRAP
            note = f"trap - do not reprice{re_add_note}"
        else:
            status = BookingStatus.NO_SAVING
            extra = (" — can re-add: " + ", ".join(re_addable_fares)) if re_addable_fares else ""
            note = f"no saving{extra}"

        # Confidence scoring
        old_cruise = _get_cruise_fare(old_items)
        new_cruise = _get_cruise_fare(new_items)
        conf = calc_confidence(old_cruise, new_cruise, net, old_total, lost_pkg_value, obc_change)

        return BookingResult(
            cruise_line=CruiseLine.ESPRESSO,
            status=status,
            note=note,
            booking_id=booking_id,
            price_category=price_category,
            old_total=old_total,
            new_total=new_total,
            price_drop=price_drop,
            obc_change=obc_change,
            net_saving=net,
            lost_pkg_value=lost_pkg_value,
            lost_pkg_names=lost_pkg_names,
            lost_fares=truly_lost_fares,
            re_addable_fares=re_addable_fares,
            gained_fares=gained_fares,
            confidence=conf.score,
            old_cruise_fare=conf.old_cruise_fare,
            new_cruise_fare=conf.new_cruise_fare,
            fare_change_pct=conf.fare_change_pct,
        )

    except Exception as e:
        return BookingResult(
            cruise_line=CruiseLine.ESPRESSO,
            status=BookingStatus.ERROR,
            error=str(e),
            booking_id=booking_id,
            price_category=price_category,
        )


# ── NCL Addon Valuation ────────────────────────────────────────

NCL_ADDON_VALUES: dict[str, int] = {
    "wi-fi": 150, "wifi": 150, "internet": 150,
    "dining": 80, "specialty dining": 80, "restaurant": 80,
    "beverage": 200, "bar": 200, "drink": 200, "open bar": 200,
    "excursion": 50, "shore": 50,
}

_DOLLAR_PATTERN = re.compile(r"\$(\d+)")


def _ncl_addon_value(addon_name: str | None) -> int:
    """Estimate dollar value of an NCL addon by its name."""
    lower = (addon_name or "").lower()
    match = _DOLLAR_PATTERN.search(lower)
    if match:
        return int(match.group(1))
    for key, val in NCL_ADDON_VALUES.items():
        if key in lower:
            return val
    return 0


# ── NCL Calculator ──────────────────────────────────────────────


def calculate_ncl(
    booking_id: str,
    price_category: str | None,
    invoice_total: float,
    new_res_total: float,
    addons: list[dict] | None = None,
    old_promos: str = "",
    new_promos: str = "",
) -> BookingResult:
    """
    Analyze an NCL booking and determine optimization status.

    Ported from calculateNCL() in the original calculator.js.

    Args:
        booking_id: The booking ID.
        price_category: Current category code.
        invoice_total: Current invoice total.
        new_res_total: New total after category switch.
        addons: List of addon dicts with 'name' and 'qty'.
        old_promos: Current promo codes string.
        new_promos: New promo codes string.

    Returns:
        BookingResult with status, savings, and details.
    """
    try:
        old_total = round2(invoice_total)
        new_total = round2(new_res_total)
        price_drop = round2(old_total - new_total)

        lost_addon_value = 0.0
        lost_addon_names: list[str] = []
        old_promo_str = (old_promos or "").upper()
        new_promo_str = (new_promos or "").upper()
        lost_fobc = "FOBC" in old_promo_str and "FOBC" not in new_promo_str

        if addons:
            seen: set[str] = set()
            unique_addons = []
            for a in addons:
                name = a.get("name", "")
                if name not in seen:
                    seen.add(name)
                    unique_addons.append(a)

            for a in unique_addons:
                name = a.get("name", "")
                is_obc_cert = bool(
                    re.search(r"On-Board Credit Certificate", name, re.IGNORECASE)
                    or re.search(r"OBC Certificate", name, re.IGNORECASE)
                )
                if is_obc_cert and lost_fobc:
                    val = _ncl_addon_value(name)
                    if val > 0:
                        lost_addon_value += val
                        lost_addon_names.append(f"{name} (${val})")

        lost_addon_value = round2(lost_addon_value)
        net = round2(price_drop - lost_addon_value)

        # Status determination
        if net > 0:
            status = BookingStatus.OPTIMIZATION
            addon_note = (
                " — verify addons: " + ", ".join(lost_addon_names)
            ) if lost_addon_names else ""
            note = f"NCL optimized ${round(net)}{addon_note}"
        elif price_drop > 0 and net <= 0:
            status = BookingStatus.TRAP
            note = f"NCL trap — price drop offset by addon loss: {', '.join(lost_addon_names)}"
        else:
            status = BookingStatus.NO_SAVING
            note = "NCL no saving"

        # Confidence scoring (simplified for NCL)
        if price_drop > 0 and lost_addon_value == 0:
            confidence = 5
        elif price_drop > 0 and lost_addon_value < price_drop:
            confidence = 4
        elif price_drop > 0 and lost_addon_value >= price_drop:
            confidence = 2
        else:
            confidence = 2

        return BookingResult(
            cruise_line=CruiseLine.NCL,
            status=status,
            note=note,
            booking_id=booking_id,
            price_category=price_category,
            old_total=old_total,
            new_total=new_total,
            price_drop=price_drop,
            obc_change=0.0,
            net_saving=net,
            lost_pkg_value=lost_addon_value,
            lost_pkg_names=lost_addon_names,
            confidence=confidence,
        )

    except Exception as e:
        return BookingResult(
            cruise_line=CruiseLine.NCL,
            status=BookingStatus.ERROR,
            error=str(e),
            booking_id=booking_id,
            price_category=price_category,
        )


# ── Helper Constructors ────────────────────────────────────────


def make_wlt_result(booking_id: str, price_category: str | None, cruise_line: CruiseLine) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.WLT,
        note="WLT - waitlisted", booking_id=booking_id, price_category=price_category,
    )


def make_paid_in_full_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, old_total: float = 0,
) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.PAID_IN_FULL,
        note="💳 Fully paid — repricing unavailable",
        booking_id=booking_id, price_category=price_category, old_total=old_total,
    )


def make_skipped_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, hours_ago: float,
) -> BookingResult:
    h = round(hours_ago, 1)
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.SKIPPED_TODAY,
        note=f"Checked {h}h ago — no saving cached",
        booking_id=booking_id, price_category=price_category,
    )


def make_error_result(
    booking_id: str, price_category: str | None, cruise_line: CruiseLine, error_msg: str,
) -> BookingResult:
    return BookingResult(
        cruise_line=cruise_line, status=BookingStatus.ERROR,
        note=error_msg, error=error_msg,
        booking_id=booking_id, price_category=price_category,
    )
