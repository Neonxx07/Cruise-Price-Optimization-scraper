"""ESPRESSO Promotions -> Compare: price the booking WITH its perk kept.

WHY (2026-10-13, booking 3000067). The scan only ever priced the default
"Best Rate" column - all 446 allocate calls in data/network_traffic.jsonl
asked for BESTRATE. A booking on ALL INC 2PK was therefore priced WITHOUT
its perk and the perk's value subtracted as an estimate. Neon's recording
and two sanitized HAR files of Promotions -> Compare gave the portal's own
numbers (Y, per person, both guests):

    Best Rate                BESTRATE  1,609.50
    ALL INC 2PK NRD          DJ065340  2,659.50   -> total 5,319.00
    ALL INC 2PK (refundable) DJ028141  2,972.50   -> total 5,945.00

and showed how the portal does it:

  * Promotions page: one checkbox per offer, `input[name="promotions"]`,
    value `CODE^NAME^FIT^FLAG` - FLAG `D` = non-refundable deposit, `N` =
    refundable, empty on STANDARD, absent on Best Rate. "You may select one
    of the available offers, or compare two different offers."
  * Compare (`a.btnCompare`) submits the form (`_eventId=saveChanges`) and
    the Categories page comes back with one column per compared offer.
    **Select (`a.btnSelect`) sits right next to it and is never clicked.**
  * That page carries everything as data: `viewJSON.matrix.matrix[CAT]
    ["INDIVIDUAL-<CODE>"][0].pricing.FIRST_SECOND.price` (per person, taxes
    included - Neon's reprice screen: subtotal 4,735 + taxes 584 = 5,319)
    and `pricePromo.nonRefundablePromo`.

ALL INC 2PK NRD and ALL INC 2PK are DIFFERENT offers with different codes,
and the codes differ per sailing (an older capture shows DI980793), so an
offer is matched by its EXACT name, never by a prefix.

Pure functions only; the browser side lives in scraper/espresso.py.
"""
from __future__ import annotations

# The perk tiers that are a perk to KEEP (NOPERK / STANDARD are not).
PERK_NAMES = ("ALL INC 2PK", "RETREAT")
MAX_COMPARE = 2  # "compare two different offers"


def _norm(name: str | None) -> str:
    return " ".join((name or "").upper().split())


def parse_promotion_value(value: str) -> dict | None:
    """`DJ065340^ALL INC 2PK NRD^FIT^D` -> code/name/scope/flag."""
    parts = (value or "").split("^")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return {
        "value": value,
        "code": parts[0].strip(),
        "name": parts[1].strip(),
        "scope": parts[2].strip() if len(parts) > 2 else "",
        "flag": parts[3].strip() if len(parts) > 3 else "",
    }


def is_perk(name: str | None) -> bool:
    n = _norm(name)
    return any(n == p or n.startswith(p + " ") for p in PERK_NAMES)


def twin_name(name: str) -> str:
    """The other refundability of the same perk: `X NRD` <-> `X`."""
    n = _norm(name)
    return n[:-4] if n.endswith(" NRD") else n + " NRD"


def compare_targets(current_fare_names: list[str],
                    promotion_values: list[str]) -> list[dict]:
    """Which offers to tick: the booking's own perk, exactly as named, then
    its twin (NRD <-> refundable). At most MAX_COMPARE. Only offers that are
    actually on this sailing's Promotions page are returned."""
    offered = {}
    for v in promotion_values:
        p = parse_promotion_value(v)
        if p:
            offered.setdefault(_norm(p["name"]), p)
    wanted: list[str] = []
    for name in current_fare_names:
        if is_perk(name):
            for n in (_norm(name), twin_name(name)):
                if n not in wanted:
                    wanted.append(n)
    return [offered[n] for n in wanted if n in offered][:MAX_COMPARE]


def perk_columns(category_matrix: dict | None, targets: list[dict]) -> list[dict]:
    """The compared columns for ONE category, from
    viewJSON.matrix.matrix[CAT]. Missing column -> left out (never guessed)."""
    out = []
    for t in targets:
        key = f"INDIVIDUAL-{t['code']}"
        entries = (category_matrix or {}).get(key) or []
        if not entries:
            continue
        e = entries[0] or {}
        pricing = (e.get("pricing") or {}).get("FIRST_SECOND") or {}
        promo = e.get("pricePromo") or {}
        status = (e.get("status") or {}).get("I18NName")
        out.append({
            "key": key,
            "code": t["code"],
            "name": t["name"],
            "price_pp": pricing.get("price"),
            "status": status,
            "available": e.get("available"),
            "non_refundable": promo.get("nonRefundablePromo"),
            "price_code": promo.get("priceCode"),
        })
    return out


def worth_repricing(column: dict, old_total: float | None,
                    occupancy: int | None) -> bool:
    """Skip the ~6 s reprice when the page already proves it cannot win.

    FIRST_SECOND is the per-person price of guests 1 and 2, so the pre-check
    is only trusted for a 2-guest booking. Anything else is repriced - an
    unknown is never a reason to skip."""
    price = column.get("price_pp")
    if price is None or column.get("status") in ("WLT", "CLS"):
        return False
    if occupancy == 2 and old_total:
        return price * 2 < old_total - 0.005
    return True


def refundability_note(old_fare_names: list[str], column: dict) -> str:
    """Say it when the cheaper perk column is the NON-refundable one and the
    booking's own perk is refundable."""
    own = [n for n in old_fare_names if is_perk(n)]
    if column.get("non_refundable") and own and not any(
            _norm(n).endswith(" NRD") for n in own):
        return (f" — DEPOSIT BECOMES NON-REFUNDABLE ({', '.join(own)} → "
                f"{column['name']})")
    return ""


def merge_perk_compare(main, record: dict, old_fare_names: list[str]):
    """Choose between the main (Best Rate) verdict and the perk-kept columns.

    A column replaces the main result only when the portal's own invoice
    says OPTIMIZATION and NOTHING is lost on it - no package, no onboard
    credit, no travel protection - and it saves at least as much as the
    main verdict (whose net already has the perk's price taken off).
    Otherwise the main verdict stands and the columns' real figures are
    appended to its note, so they are seen and never silently dropped."""
    from core.models import BookingStatus

    clean = [(c, r) for c, r in (record.get("results") or [])
             if r.status == BookingStatus.OPTIMIZATION
             and not r.lost_pkg_value
             and (r.obc_change or 0) >= 0
             and not r.lost_travel_protection]
    best = max(clean, key=lambda cr: cr[1].net_saving, default=None)
    if best and (main.status != BookingStatus.OPTIMIZATION
                 or best[1].net_saving >= main.net_saving):
        column, chosen = best
        chosen.note = (f"PERK KEPT — {column['name']} (Royal's own total via "
                       f"Promotions → Compare): {chosen.note or ''}"
                       f"{refundability_note(old_fare_names, column)}")
        return chosen

    parts = []
    for c in record.get("columns") or []:
        part = f"{c['name']} {c['price_pp']:,.2f}pp" if c.get("price_pp") is not None else c["name"]
        if c.get("new_total") is not None:
            part += f" → total ${c['new_total']:,.2f} ({c.get('verdict')})"
        elif c.get("repriced") is False:
            part += " (cannot be cheaper)"
        part += refundability_note(old_fare_names, c)
        parts.append(part)
    summary = "; ".join(parts) or "no column"
    main.note = (main.note or "") + f" [PERK COMPARE {record.get('outcome')}: {summary}]"
    return main
