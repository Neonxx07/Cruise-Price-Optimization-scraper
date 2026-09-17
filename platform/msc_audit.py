"""Full audit of every stored MSC booking under the corrected logic.

Neon, 2026-09-01: "can you look at everything now and do a full audit and
fix all the issues and all errors problems in the bookings."

Context for why this is worth re-running from scratch: all three bookings
Neon gave as ground truth this session have now been resolved, and two of
them turned out not to be optimizable at all -

    3000081  $81.98  VALID - and now reproduced exactly
    3000071  $63.24  OVERPAID, not optimizable (Neon)
    3000083  $23.66  PAID IN FULL, due $0.96 (Neon)

So every conclusion drawn earlier from those two numbers was drawn from
bookings that should never have been in the sample. This re-audits
everything against the corrected calculator rather than patching forward
from those results.

READ-ONLY. Recomputes from the raw captured text every time rather than
trusting cached fields - the standard pattern for this data, because older
records were written before several parser fixes existed.
"""
import glob
import json
import pathlib
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.models  # noqa: E402
from core.calculator_msc import evaluate_msc_booking  # noqa: E402
from msc_commands import (  # noqa: E402
    _count_cabins,
    _extract_booking_essentials,
    _extract_discounts_with_implied,
    _extract_passengers,
    _is_paid_in_full,
    _is_placeholder_departure,
    _parse_dollars_safe,
    msc_invoice_guest_count,
    is_valid_msc_booking_id,
    msc_occupancy_is_trustworthy,
)

BASE = str(pathlib.Path(__file__).resolve().parent)


def load(path_glob, need):
    out = {}
    for path in glob.glob(path_glob):
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            # Skip phantom ids such as the "/*" glob that once reached a
            # lookup command and had an invoice captured against it.
            if (d.get("booking_id") and d.get(need)
                    and is_valid_msc_booking_id(d["booking_id"])):
                # NEWEST BY TIMESTAMP, not by read position — see
                # msc_run_calculator._load_last_by_id for why read order is
                # only accidentally correct today and what breaks it.
                bid = d["booking_id"]
                prev = out.get(bid)
                if prev is None or (d.get("captured_at") or "") >= (
                        prev.get("captured_at") or ""):
                    out[bid] = d
    return out


bookings = load(BASE + r"\data\msc_control\*.jsonl", "summary_text")
rates = load(BASE + r"\data\msc_control\rate_check_data.jsonl", "found")

print("=" * 78)
print(f"MSC FULL AUDIT — {len(bookings)} bookings with an invoice, "
      f"{len(rates)} with a rate check")
print("=" * 78)

issues = defaultdict(list)
verdicts = Counter()
rows = []

for bid, bd in sorted(bookings.items()):
    summary = bd.get("summary_text") or ""
    breakdown = bd.get("breakdown_text")
    ess = _extract_booking_essentials(summary)
    pax = _extract_passengers(summary)
    rate = rates.get(bid) or {}
    occ = rate.get("occupancy_fix") or {}

    due = _parse_dollars_safe(ess.get("due_amount"))
    overpaid = bool(ess.get("is_overpayment"))
    pif = _is_paid_in_full(due, overpaid, core.models.MSC_PAID_IN_FULL_DUE_THRESHOLD)
    cabins = _count_cabins(summary + "\n" + (breakdown or ""))
    member = next((p for p in pax["passengers"] if p.get("voyagers_number")), None)
    category = rate.get("category") or ess.get("category")
    today = _parse_dollars_safe(rate.get("today_price_same_category"))
    current = _parse_dollars_safe(ess.get("value"))

    # --- non-actionable states, in the order the calculator applies them
    if overpaid:
        verdicts["OVERPAID — not optimizable"] += 1
        issues["overpaid"].append(bid)
        continue
    if _is_placeholder_departure(summary):
        verdicts["cancelled/postponed placeholder"] += 1
        issues["cancelled"].append(bid)
        continue
    if cabins > 1:
        verdicts["multi-cabin — cannot be priced"] += 1
        issues["multi_cabin"].append(bid)
        continue
    if pif:
        verdicts["paid in full"] += 1
        issues["paid_in_full"].append(bid)
        # still evaluated below — paid in full softens PRICE_MATCH but the
        # discount levers can still be real, so it is not a hard stop

    # --- data-quality problems that block a verdict
    if not rate:
        verdicts["no rate check captured"] += 1
        issues["no_rate_check"].append(bid)
        continue
    if not category:
        verdicts["category unreadable"] += 1
        issues["no_category"].append(bid)
        continue
    if today is None:
        verdicts["today price not found"] += 1
        issues["no_today_price"].append(bid)
        continue

    # --- the club-discount comparability gate, the big fix this session
    if member and not rate.get("today_price_includes_club_discount"):
        issues["needs_recapture_with_club"].append(bid)

    occ_ok, occ_note = msc_occupancy_is_trustworthy(
        {"counts": occ.get("required") or {},
         "total_guests": occ.get("intended_guests"),
         "dropped": occ.get("dropped_passengers") or 0},
        msc_invoice_guest_count(summary, breakdown), occ or None, cabins)
    if not occ_ok:
        issues["occupancy_unverified"].append((bid, occ_note[:60]))

    result = evaluate_msc_booking(
        booking_id=bid,
        category=category,
        is_paid_in_full=pif,
        is_overpayment=overpaid,
        due_amount=due,
        current_total_price=current,
        today_base_price=today,
        current_discounts=_extract_discounts_with_implied(summary, breakdown),
        today_discount_options=rate.get("discount_options"),
        today_discount_catalog=rate.get("discount_catalog"),
        has_voyagers=pax["has_voyagers"],
        senior_count=pax["senior_count"],
        is_group_rate=bool(rate.get("is_group_rate")),
        club_discount_offered=rate.get("club_discount_offered"),
        today_price_tab_confirmed=bool((rate.get("rate_tab_match") or {}).get("matched")),
        occupancy_verified=occ_ok,
        occupancy_note=occ_note,
        customer_has_club_membership=bool(member),
        today_price_includes_club_discount=bool(
            rate.get("today_price_includes_club_discount")),
    )
    if result.has_any_opportunity:
        verdicts["OPPORTUNITY"] += 1
        best = max((c for c in result.checks if c.estimated_value),
                   key=lambda c: c.estimated_value, default=None)
        rows.append((bid, category, current, today,
                     best.type.value if best else "-",
                     best.estimated_value if best else None))
    else:
        verdicts["no opportunity"] += 1

print("\nVERDICTS")
for k, v in verdicts.most_common():
    print(f"   {v:>4}  {k}")

print("\nISSUES FOUND")
labels = {
    "overpaid": "OVERPAID — hard stop (rule added today)",
    "cancelled": "cancelled/postponed placeholder departure",
    "multi_cabin": "MULTI-CABIN — quote covers cabin 1, total covers all",
    "paid_in_full": "paid in full — price match suppressed",
    "no_rate_check": "never rate-checked",
    "no_category": "category could not be read",
    "no_today_price": "today's price not found in the listing",
    "needs_recapture_with_club": "MEMBER, but today's price has no club discount",
    "occupancy_unverified": "occupancy could not be verified",
}
for key, label in labels.items():
    got = issues.get(key) or []
    if not got:
        continue
    print(f"\n   {len(got):>4}  {label}")
    sample = got[:8]
    print("         " + ", ".join(
        s[0] if isinstance(s, tuple) else s for s in sample)
        + (" ..." if len(got) > 8 else ""))

print("\nOPPORTUNITIES UNDER THE CORRECTED LOGIC")
if rows:
    print(f"   {'booking':<10} {'cat':<6} {'current':>10} {'today':>10} "
          f"{'lever':<22} {'value':>9}")
    for bid, cat, cur, tod, lever, val in sorted(
            rows, key=lambda r: -(r[5] or 0))[:20]:
        print(f"   {bid:<10} {str(cat):<6} {cur or 0:>10,.2f} {tod or 0:>10,.2f} "
              f"{lever:<22} {(val or 0):>9,.2f}")
    print(f"\n   total: ${sum(r[5] or 0 for r in rows):,.2f} across {len(rows)}")
else:
    print("   none")
