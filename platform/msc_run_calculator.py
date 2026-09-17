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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msc_commands import (
    _count_cabins,
    msc_invoice_components,
    msc_invoice_guest_count,
    _extract_booking_essentials,
    _extract_discounts_with_implied,
    _extract_passengers,
    _find_today_price,
    _is_paid_in_full,
    _srn_reference_available,
    msc_occupancy_is_trustworthy,
)
from core.calculator_msc import evaluate_msc_booking
from core.price_scope import PriceScope
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
            bid = entry.get("booking_id")
            if not bid:
                continue
            # NEWEST BY TIMESTAMP, not by read position. Added 2026-09-03.
            #
            # This used to keep whichever record it read LAST, which is only
            # the newest while the file's append order matches real
            # chronological order. It does not always: booking_data.jsonl
            # and rate_check_data.jsonl each contain 4 timestamp inversions.
            # Today those inversions fall BETWEEN bookings rather than
            # within one, so last-wins happened to be right on all 118/89
            # bookings — correct by luck, not construction. Concurrent
            # scanning (two cruise lines at once, now supported) interleaves
            # writes and would break it silently, picking a stale capture
            # and reporting an old price as today's.
            previous = seen.get(bid)
            if previous is None:
                seen[bid] = entry
                continue
            new_ts = entry.get("captured_at") or ""
            old_ts = previous.get("captured_at") or ""
            # An unstamped record loses to a stamped one; between two
            # unstamped records the later line still wins, preserving the
            # old behaviour for pre-timestamp captures.
            if new_ts >= old_ts:
                seen[bid] = entry
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
        # Recomputed fresh rather than trusting booking["senior_count"]/
        # ["all_seniors"] — same staleness issue as today_price below:
        # older captures predate the passenger-DOB parsing being added at
        # all (and older ones still won't have senior_count specifically —
        # see core/calculator_msc.py's 2026-08-18 all_seniors -> senior_count
        # correction, booking 3000030).
        senior_count = _extract_passengers(booking["summary_text"])["senior_count"]
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

        # THE OCCUPANCY CROSS-CHECK, added here 2026-09-03 by the call-site
        # parity test, which found this path applied every other guard and
        # not this one. Replaying booking 3000081's 2026-08-24 capture
        # through this script would therefore still have produced the fake
        # $267.01 that the live path refuses — the report contradicting the
        # scanner on the very case the guard was written for.
        _occ = rate.get("occupancy_fix") or {}
        _cabins = _count_cabins((booking.get("summary_text") or "")
                                + chr(10) + (booking.get("breakdown_text") or ""))
        occupancy_verified, occupancy_note = msc_occupancy_is_trustworthy(
            {
                "counts": _occ.get("required") or {},
                "total_guests": _occ.get("intended_guests"),
                "dropped": _occ.get("dropped_passengers") or 0,
                "passengers_seen": _occ.get("passengers_seen"),
            },
            msc_invoice_guest_count(booking.get("summary_text"),
                                    booking.get("breakdown_text")),
            _occ or None,
            _cabins,
        )

        result = evaluate_msc_booking(
            booking_id=bid,
            category=category,
            cancelled_or_postponed=booking.get("cancelled_or_postponed_placeholder", False),
            is_paid_in_full=_is_paid_in_full(
                due_amount, essentials.get("is_overpayment", False), MSC_PAID_IN_FULL_DUE_THRESHOLD
            ),
            is_overpayment=bool(essentials.get("is_overpayment", False)),
            due_amount=due_amount,
            current_total_price=_parse_dollars(essentials.get("value")),
            today_base_price=_parse_dollars(today_price),
            current_discounts=current_discounts,
            today_discount_options=rate.get("discount_options"),
            today_discount_catalog=rate.get("discount_catalog"),
            has_voyagers=rate.get("has_voyagers", False),
            senior_count=senior_count,
            senior_discount_verifiable=_srn_reference_available(booking["summary_text"]),
            today_price_tab_confirmed=bool((rate.get("rate_tab_match") or {}).get("matched")),
            is_group_rate=rate.get("is_group_rate", False),
            club_discount_offered=rate.get("club_discount_offered"),
            final_payment_date_passed=bool(essentials.get("final_payment_date_passed")),
            # PARITY WITH THE LIVE PATH, added 2026-09-03. These guards were
            # added to _check_booking_msc but not here, so replaying a stored
            # capture through this script produced a DIFFERENT verdict than
            # the run that captured it — a report disagreeing with the scan
            # that produced it is worse than either being wrong alone.
            #
            # Every one of them is derived from the stored record, so an old
            # capture that predates the field degrades to "unknown" and the
            # guard reports INSUFFICIENT_DATA rather than a false verdict.
            occupancy_verified=occupancy_verified,
            occupancy_note=occupancy_note,
            customer_has_club_membership=bool(next(
                (p for p in (_extract_passengers(booking["summary_text"])
                             .get("passengers") or [])
                 if p.get("voyagers_number")), None)),
            today_price_includes_club_discount=bool(
                rate.get("today_price_includes_club_discount")),
            club_entry_note=str(
                (rate.get("voyagers_fix") or {}).get("reason") or ""),
            non_cruise_charges=msc_invoice_components(
                booking.get("breakdown_text")).get("non_cruise_total") or 0.0,
            current_scope=PriceScope(
                guests=msc_invoice_guest_count(
                    booking.get("summary_text"), booking.get("breakdown_text")),
                cabins=_cabins,
                includes_club_discount=True,
                non_cruise_charges=0.0,
                label="the booking's own total",
            ),
            today_scope=PriceScope(
                guests=(rate.get("occupancy_fix") or {}).get("applied_guests"),
                cabins=1,
                includes_club_discount=bool(
                    rate.get("today_price_includes_club_discount")),
                non_cruise_charges=0.0,
                label="today's listing card",
            ),
        )
        # current_discounts is None (not []) when _extract_discounts
        # couldn't confirm the Price Breakdown modal actually rendered —
        # see its docstring — so this must not iterate it directly.
        # "implied" now counts alongside "named" — as of 2026-08-11, an
        # SRN-math-detected silent discount (see
        # _extract_discounts_with_implied) genuinely does account for
        # the senior-discount blind spot this caveat was built for, so
        # the caveat should only fire when NEITHER caught it. Gated on
        # senior_count >= 2 (not just "any senior"), matching the real
        # eligibility rule corrected 2026-08-18 — a lone senior was never
        # actually eligible, so there's nothing to caveat for them.
        has_named_or_implied = any(d.get("kind") in ("named", "implied") for d in (current_discounts or []))
        results.append((result, senior_count >= 2, has_named_or_implied))

    print(f"\n=== {len(results)} booking(s) evaluated ===")
    for result, senior_eligible, has_named_or_implied in results:
        flag = "OPPORTUNITY FOUND" if result.has_any_opportunity else "no opportunity"
        print(f"\n{result.booking_id} ({result.category}) — {flag}")
        for c in result.checks:
            print(f"   {c.type.value}: {c.status.value} — {c.note}")
        if senior_eligible and not has_named_or_implied:
            print(
                "   CAVEAT: this cabin has 2+ senior (65+) passengers but no discount was found (explicit "
                "disclosure OR SRN-implied) — either genuinely no discount is applied, or the cruise length "
                "isn't in STANDARD_NCF_BY_NIGHTS yet so the implied-discount math couldn't run; verify the "
                "SRN line by hand before trusting DISCOUNT_ADD on this one"
            )

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    # Stamped so a saved report can be told apart from a later one. This
    # file is rewritten whole on each run and previously carried no date at
    # all, which made a stale report on disk indistinguishable from a fresh
    # one — and it is the file the CSV is built from.
    generated_at = datetime.now().isoformat()
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        for result, _, _ in results:
            record = json.loads(result.model_dump_json())
            record["generated_at"] = generated_at
            f.write(json.dumps(record) + "\n")
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
        for result, senior_eligible, has_named in results:
            by_type = {c.type.value: c for c in result.checks}
            row = [result.booking_id, result.category, result.has_any_opportunity]
            for check_type in ("PRICE_MATCH", "DISCOUNT_ADD", "DISCOUNT_TIER_UPGRADE", "VOYAGERS_SELECTION"):
                c = by_type.get(check_type)
                row.append(c.status.value if c else "")
                row.append(c.note if c else "")
            row.append("2+ seniors, no disclosed discount" if (senior_eligible and not has_named) else "")
            writer.writerow(row)
    print(f"Saved {len(results)} result(s) to {RESULTS_CSV_PATH}")


if __name__ == "__main__":
    main()
