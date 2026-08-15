"""Merge booking_data.jsonl + rate_check_data.jsonl and run
core/calculator_msc.py's evaluate_msc_booking() on every booking that has
a CONFIRMED today's-rate capture, printing the four-check result for each
and saving them to data/msc_control/calculator_results.jsonl (plus a
calculator_results.csv for easy review without reading raw JSON).

Read-only, offline — does no browser automation itself, just reads what
msc_session_controller.py's batch commands (or the fully-automated
check_booking/check_booking_batch commands, added 2026-08-11) have
already collected.

Usage: python msc_run_calculator.py
"""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msc_commands import (
    _extract_booking_essentials,
    _extract_discounts_with_implied,
    _extract_passengers,
    _find_today_price,
    _is_paid_in_full,
)
from core.calculator_msc import evaluate_msc_booking
from core.models import MSC_PAID_IN_FULL_DUE_THRESHOLD

BOOKING_DATA_PATH = "data/msc_control/booking_data.jsonl"
RATE_CHECK_DATA_PATH = "data/msc_control/rate_check_data.jsonl"
RESULTS_PATH = "data/msc_control/calculator_results.jsonl"
RESULTS_CSV_PATH = "data/msc_control/calculator_results.csv"


def _load_last_by_id(path: str) -> dict:
    """Last write per booking_id wins — handles retries the same way the
    rest of this project's batch tooling does."""
    seen = {}
    if not os.path.exists(path):
        return seen
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            seen[entry["booking_id"]] = entry
    return seen


def _parse_dollars(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace(",", ""))


def main():
    bookings = _load_last_by_id(BOOKING_DATA_PATH)
    rate_checks = _load_last_by_id(RATE_CHECK_DATA_PATH)

    results = []
    for bid, rate in rate_checks.items():
        if rate.get("status") == "sailing_already_departed_or_no_data":
            print(f"{bid}: SKIPPED — sailing already departed (CRU_034)")
            continue
        if not rate.get("listing_confirmed"):
            print(f"{bid}: SKIPPED — today's rate capture not confirmed")
            continue

        booking = bookings.get(bid)
        if not booking or "No bookings found" in (booking.get("summary_text") or ""):
            print(f"{bid}: SKIPPED — no matching booking lookup data")
            continue

        essentials = _extract_booking_essentials(booking["summary_text"])
        current_discounts = _extract_discounts_with_implied(booking["summary_text"], booking.get("breakdown_text"))
        # Recomputed fresh rather than trusting booking["all_seniors"] —
        # same staleness issue as today_price below: older captures
        # predate the passenger-DOB parsing being added at all.
        all_seniors = _extract_passengers(booking["summary_text"])["all_seniors"]
        category = essentials.get("category") or rate.get("category")

        # Recompute today's price fresh from the stored raw listing text
        # rather than trusting rate_check_data.jsonl's own cached
        # today_price_same_category field — that field was written by
        # whatever version of _find_today_price was active at capture
        # time, which for older captures predates real bug fixes (the
        # whole-dollar-price regex fix, the Guaranteed Cabin category
        # matching). The raw listing_text itself didn't change, so
        # re-running today's (fixed) logic against it recovers the
        # correct price without needing to re-open a browser tab.
        today_price = _find_today_price(
            rate.get("listing_text"), category, essentials.get("is_guaranteed", False)
        )

        # discount_catalog/has_voyagers were only ever captured by staging
        # runs from 2026-08-11 onward (the DiscountPaxTypeCmd-parsing fix)
        # — older rate_check_data.jsonl entries simply won't have these
        # keys, and .get() correctly falls back to None/False for them
        # rather than erroring, so VOYAGERS_SELECTION reports
        # INSUFFICIENT_DATA on old captures instead of a false NO_OPPORTUNITY.
        due_amount = _parse_dollars(essentials.get("due_amount"))
        result = evaluate_msc_booking(
            booking_id=bid,
            category=category,
            cancelled_or_postponed=booking.get("cancelled_or_postponed_placeholder", False),
            is_paid_in_full=_is_paid_in_full(
                due_amount, essentials.get("is_overpayment", False), MSC_PAID_IN_FULL_DUE_THRESHOLD
            ),
            due_amount=due_amount,
            current_total_price=_parse_dollars(essentials.get("value")),
            today_base_price=_parse_dollars(today_price),
            current_discounts=current_discounts,
            today_discount_options=rate.get("discount_options"),
            today_discount_catalog=rate.get("discount_catalog"),
            has_voyagers=rate.get("has_voyagers", False),
            all_seniors=all_seniors,
            today_price_tab_confirmed=bool((rate.get("rate_tab_match") or {}).get("matched")),
            is_group_rate=rate.get("is_group_rate", False),
            club_discount_offered=rate.get("club_discount_offered"),
        )
        # current_discounts is None (not []) when _extract_discounts
        # couldn't confirm the Price Breakdown modal actually rendered —
        # see its docstring — so this must not iterate it directly.
        # "implied" now counts alongside "named" — as of 2026-08-11, an
        # SRN-math-detected silent discount (see
        # _extract_discounts_with_implied) genuinely does account for
        # the senior-discount blind spot this caveat was built for, so
        # the caveat should only fire when NEITHER caught it.
        has_named_or_implied = any(d.get("kind") in ("named", "implied") for d in (current_discounts or []))
        results.append((result, all_seniors, has_named_or_implied))

    print(f"\n=== {len(results)} booking(s) evaluated ===")
    for result, all_seniors, has_named_or_implied in results:
        flag = "OPPORTUNITY FOUND" if result.has_any_opportunity else "no opportunity"
        print(f"\n{result.booking_id} ({result.category}) — {flag}")
        for c in result.checks:
            print(f"   {c.type.value}: {c.status.value} — {c.note}")
        if all_seniors and not has_named_or_implied:
            print(
                "   CAVEAT: passengers are all 65+ but no discount was found (explicit disclosure OR "
                "SRN-implied) — either genuinely no discount is applied, or the cruise length isn't in "
                "STANDARD_NCF_BY_NIGHTS yet so the implied-discount math couldn't run; verify the SRN "
                "line by hand before trusting DISCOUNT_ADD on this one"
            )

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        for result, _, _ in results:
            f.write(result.model_dump_json() + "\n")
    print(f"\nSaved {len(results)} result(s) to {RESULTS_PATH}")

    with open(RESULTS_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Booking ID", "Category", "Has Opportunity",
            "PRICE_MATCH", "PRICE_MATCH Note",
            "DISCOUNT_ADD", "DISCOUNT_ADD Note",
            "DISCOUNT_TIER_UPGRADE", "DISCOUNT_TIER_UPGRADE Note",
            "VOYAGERS_SELECTION", "VOYAGERS_SELECTION Note",
            "Senior-Blind-Spot Caveat",
        ])
        for result, all_seniors, has_named in results:
            by_type = {c.type.value: c for c in result.checks}
            row = [result.booking_id, result.category, result.has_any_opportunity]
            for check_type in ("PRICE_MATCH", "DISCOUNT_ADD", "DISCOUNT_TIER_UPGRADE", "VOYAGERS_SELECTION"):
                c = by_type.get(check_type)
                row.append(c.status.value if c else "")
                row.append(c.note if c else "")
            row.append("all 65+, no disclosed discount" if (all_seniors and not has_named) else "")
            writer.writerow(row)
    print(f"Saved {len(results)} result(s) to {RESULTS_CSV_PATH}")


if __name__ == "__main__":
    main()
