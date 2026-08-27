"""Read-only intelligence report over everything this project has already
collected — cruise_intel.db (ESPRESSO/NCL/GoCCL) and data/msc_control/
(MSC) — so scanning effort can be pointed at what actually pays off
instead of treating every booking/check type as equally likely.

Never touches a browser, never writes anywhere except optionally a report
file when --save is passed. Safe to run any time, as often as wanted.

Usage:
    python analyze_history.py
    python analyze_history.py --save reports/intelligence_report.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

# Windows attaches a cp1252 console by default, which can't encode the
# emoji used in the report below (💰, ⚠) — same fix as gui/main.py.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = "cruise_intel.db"
MSC_DIR = "data/msc_control"


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def _load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ── ESPRESSO / NCL / GoCCL (cruise_intel.db) ────────────────────────────


def espresso_section(out: list[str]) -> None:
    if not os.path.exists(DB_PATH):
        out.append("No cruise_intel.db found — skipping ESPRESSO/NCL/GoCCL section.\n")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM bookings")
    total, first, last = c.fetchone()
    if not total:
        out.append("cruise_intel.db has no booking rows yet.\n")
        return

    out.append("=" * 60)
    out.append("ESPRESSO / NCL / GoCCL  (cruise_intel.db)")
    out.append("=" * 60)
    out.append(f"{total} booking-checks recorded, {first} to {last}\n")

    # Status breakdown, overall
    c.execute("SELECT status, COUNT(*) FROM bookings GROUP BY status ORDER BY COUNT(*) DESC")
    rows = c.fetchall()
    out.append("Status breakdown (all-time):")
    for status, n in rows:
        out.append(f"   {status:16s} {n:5d}  ({_pct(n, total)})")

    # Total savings delivered
    c.execute("SELECT SUM(net_saving), COUNT(*), AVG(net_saving), MIN(net_saving), MAX(net_saving) FROM bookings WHERE status='OPTIMIZATION'")
    total_saving, opt_count, avg_saving, min_saving, max_saving = c.fetchone()
    out.append("")
    if opt_count:
        out.append(f"💰 Real savings found: ${total_saving:.2f} across {opt_count} booking(s)")
        out.append(f"   avg ${avg_saving:.2f}, smallest ${min_saving:.2f}, largest ${max_saving:.2f}")
    else:
        out.append("💰 No OPTIMIZATION rows recorded yet.")

    # The dual-rate-column (c3) caveat — how much of the reported savings
    # figure might be incomplete because a second rate-program column
    # was never evaluated (see scraper/espresso.py's _DUAL_RATE_COLUMN_NOTE).
    if opt_count:
        c.execute("SELECT COUNT(*) FROM bookings WHERE status='OPTIMIZATION' AND note LIKE '%NOT evaluated%'")
        (caveat_count,) = c.fetchone()
        if caveat_count:
            out.append(
                f"   ⚠ {caveat_count}/{opt_count} of those ({_pct(caveat_count, opt_count)}) also had an "
                "unevaluated second rate-program column (c3) — the real number could be even higher on those"
            )

    # Hit rate by cruise line
    out.append("")
    out.append("By cruise line:")
    c.execute(
        "SELECT cruise_line, status, COUNT(*) FROM bookings GROUP BY cruise_line, status"
    )
    by_line: dict[str, Counter] = defaultdict(Counter)
    for line, status, n in c.fetchall():
        by_line[line][status] += n
    for line, counts in by_line.items():
        line_total = sum(counts.values())
        opt = counts.get("OPTIMIZATION", 0)
        err = counts.get("ERROR", 0)
        out.append(f"   {line:10s} {line_total:5d} checked — {opt} optimization ({_pct(opt, line_total)}), {err} error ({_pct(err, line_total)})")

    # Price category hit rates — where OPTIMIZATION actually concentrates,
    # for categories checked often enough to mean something (>=10 checks).
    out.append("")
    out.append("Price categories worth watching (>=10 checks, sorted by OPTIMIZATION rate):")
    c.execute(
        "SELECT price_category, status, COUNT(*) FROM bookings WHERE price_category IS NOT NULL AND price_category != '' GROUP BY price_category, status"
    )
    by_cat: dict[str, Counter] = defaultdict(Counter)
    for cat, status, n in c.fetchall():
        by_cat[cat][status] += n
    cat_stats = []
    for cat, counts in by_cat.items():
        cat_total = sum(counts.values())
        if cat_total < 10:
            continue
        opt = counts.get("OPTIMIZATION", 0)
        cat_stats.append((cat, cat_total, opt))
    cat_stats.sort(key=lambda x: (-x[2] / x[1], -x[1]))
    for cat, cat_total, opt in cat_stats[:15]:
        marker = " <-- 0 hits despite volume" if opt == 0 and cat_total >= 30 else ""
        out.append(f"   {cat:10s} {cat_total:4d} checked, {opt} optimization ({_pct(opt, cat_total)}){marker}")

    # Error clustering by day — surfaces a bad run (session logout, portal
    # issue) instead of it hiding inside an "X errors, normal noise" line.
    out.append("")
    c.execute("SELECT DATE(created_at), COUNT(*) FROM bookings WHERE status='ERROR' GROUP BY DATE(created_at) ORDER BY COUNT(*) DESC LIMIT 5")
    error_days = c.fetchall()
    if error_days:
        out.append("Days with the most ERROR results (investigate if unexpectedly high):")
        for day, n in error_days:
            out.append(f"   {day}: {n} error(s)")

    # Repeat-booking flip rate — does re-checking the same booking ever
    # actually change the answer, or is a second pass wasted time.
    out.append("")
    c.execute(
        "SELECT booking_id, cruise_line, status, created_at FROM bookings ORDER BY booking_id, created_at"
    )
    by_booking: dict[tuple, list[str]] = defaultdict(list)
    for bid, line, status, created_at in c.fetchall():
        by_booking[(bid, line)].append(status)
    repeats = {k: v for k, v in by_booking.items() if len(v) > 1}
    flips_to_optimization = sum(
        1 for statuses in repeats.values()
        if "OPTIMIZATION" in statuses[1:] and statuses[0] != "OPTIMIZATION"
    )
    if repeats:
        out.append(
            f"Repeat checks: {len(repeats)} booking(s) checked more than once; "
            f"{flips_to_optimization} of those later flipped to OPTIMIZATION after starting as something else "
            f"({_pct(flips_to_optimization, len(repeats))} of repeats) — this is the real payoff rate of re-scanning."
        )

    out.append("")
    conn.close()


def espresso_pattern_mining_section(out: list[str]) -> None:
    """Multi-factor pattern mining (association rules) over the SAME
    cruise_intel.db data espresso_section() already summarizes one column
    at a time. Finds combinations a single-column breakdown can't surface
    on its own — e.g. a category that only clears the ">=10 checks"
    volume filter above when combined with cruise_line. Uses mlxtend's
    apriori/association_rules (classical frequent-itemset mining, not an
    LLM or a trained model) — added 2026-08-25 as part of the "AI-smarter
    without a real model" effort.

    Deliberately shows raw counts alongside confidence/lift, not just
    percentages — a 66.7% "hit rate" reads very differently once you see
    it's 4 bookings out of 6. Never state a rule's confidence without its
    support count right next to it."""
    if not os.path.exists(DB_PATH):
        return
    try:
        import pandas as pd
        from mlxtend.frequent_patterns import apriori, association_rules
    except ImportError:
        out.append("mlxtend/pandas not installed — skipping pattern-mining section (pip install mlxtend).\n")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT cruise_line, status, price_category, note FROM bookings")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return

    records = []
    for cruise_line, status, price_category, note in rows:
        items = {f"line={cruise_line}": True}
        if price_category:
            items[f"cat={price_category}"] = True
        if note and "NOT evaluated" in note:
            items["has_c3_caveat"] = True
        items["OPTIMIZATION"] = status == "OPTIMIZATION"
        records.append(items)
    df = pd.DataFrame(records).fillna(False).astype(bool)

    # Lowest support that could possibly matter: at least 3 co-occurrences.
    # Below that, "found a pattern" is indistinguishable from noise.
    min_occurrences = 3
    min_support = min_occurrences / len(df)
    frequent = apriori(df, min_support=min_support, use_colnames=True)
    if frequent.empty:
        return
    rules = association_rules(frequent, metric="lift", min_threshold=1.0, num_itemsets=len(frequent))
    rules = rules[rules["consequents"] == frozenset({"OPTIMIZATION"})].copy()
    if rules.empty:
        return
    rules["antecedent_count"] = (rules["support"] / rules["confidence"] * len(df)).round().astype(int)
    rules["hit_count"] = (rules["support"] * len(df)).round().astype(int)
    rules = rules.sort_values("lift", ascending=False)

    # Dedupe: when a more specific antecedent (e.g. "line=GOCCL, cat=HI")
    # covers the EXACT same rows as a simpler one (e.g. "cat=HI" — true
    # whenever a category only ever appears on one cruise line), keep
    # only whichever has the FEWEST antecedent items. Otherwise the
    # report doubles up on what's really one finding.
    by_signature: dict[tuple, "pd.Series"] = {}
    for _, r in rules.iterrows():
        signature = (r["antecedent_count"], r["hit_count"])
        existing = by_signature.get(signature)
        if existing is None or len(r["antecedents"]) < len(existing["antecedents"]):
            by_signature[signature] = r
    deduped_rules = sorted(by_signature.values(), key=lambda r: -r["lift"])

    out.append("=" * 60)
    out.append("PATTERN MINING — multi-factor combinations (ESPRESSO/GoCCL)")
    out.append("=" * 60)
    out.append(
        f"Association rules (mlxtend apriori) over {len(df)} bookings, "
        f"min {min_occurrences} co-occurrences — raw counts shown, not just rates:\n"
    )
    shown = 0
    for r in deduped_rules:
        if shown >= 12:
            break
        antecedents = ", ".join(sorted(frozenset(r["antecedents"])))
        caveat = " [SMALL SAMPLE — treat as a lead, not a conclusion]" if r["antecedent_count"] < 10 else ""
        out.append(
            f"   {antecedents} => OPTIMIZATION | {r['hit_count']}/{r['antecedent_count']} "
            f"({r['confidence']*100:.1f}%), {r['lift']:.1f}x baseline rate{caveat}"
        )
        shown += 1
    if shown == 0:
        out.append("   No combinations cleared the minimum sample size yet.")
    out.append("")


# ── MSC (data/msc_control/*.jsonl) ──────────────────────────────────────


def msc_section(out: list[str]) -> None:
    results = _load_jsonl(os.path.join(MSC_DIR, "live_check_results.jsonl"))
    if not results:
        out.append("No data/msc_control/live_check_results.jsonl found — skipping MSC section.\n")
        return

    # De-dupe to the latest check per booking, same convention the rest of
    # this project uses (last write wins).
    latest: dict[str, dict] = {}
    for r in results:
        latest[r["booking_id"]] = r
    checked = list(latest.values())

    out.append("=" * 60)
    out.append("MSC  (data/msc_control/live_check_results.jsonl)")
    out.append("=" * 60)
    dates = [r.get("checked_at", "") for r in results if r.get("checked_at")]
    out.append(f"{len(results)} checks recorded ({len(checked)} unique bookings), {min(dates)} to {max(dates)}\n")

    any_opp = sum(1 for r in checked if r.get("has_any_opportunity"))
    out.append(f"💰 {any_opp}/{len(checked)} unique bookings ({_pct(any_opp, len(checked))}) have at least one live opportunity right now")

    # Per-check-type hit rate — which of the four checks is actually
    # productive vs mostly INSUFFICIENT_DATA (a data-collection gap, not
    # a real "nothing here").
    out.append("")
    out.append("By check type (most recent check per booking):")
    by_type: dict[str, Counter] = defaultdict(Counter)
    for r in checked:
        for chk in r.get("checks", []):
            by_type[chk["type"]][chk["status"]] += 1
    for check_type, counts in by_type.items():
        total = sum(counts.values())
        opp = counts.get("OPPORTUNITY", 0)
        insuff = counts.get("INSUFFICIENT_DATA", 0)
        out.append(
            f"   {check_type:22s} {opp:4d} opportunity ({_pct(opp, total)}), "
            f"{insuff:4d} insufficient-data ({_pct(insuff, total)}) of {total}"
        )
        if insuff and _pct_value(insuff, total) >= 30:
            out.append(f"      ⚠ {_pct(insuff, total)} insufficient-data — a real data-collection gap, not a finding")

    # Duration blind-spot exposure — how many checked bookings sit outside
    # STANDARD_NCF_BY_NIGHTS and are therefore unverifiable for a silent
    # senior discount (see core/calculator_msc.py's 2026-08-24 fix).
    booking_data = {d["booking_id"]: d for d in _load_jsonl(os.path.join(MSC_DIR, "booking_data.jsonl"))}
    import re
    covered_nights = {3, 4, 7}
    outside = 0
    checked_for_duration = 0
    for bid in latest:
        d = booking_data.get(bid)
        if not d:
            continue
        m = re.search(r"(\d+)\s*Nights?", d.get("summary_text", "") or "")
        if not m:
            continue
        checked_for_duration += 1
        if int(m.group(1)) not in covered_nights:
            outside += 1
    if checked_for_duration:
        out.append("")
        out.append(
            f"Senior-discount blind spot exposure: {outside}/{checked_for_duration} bookings "
            f"({_pct(outside, checked_for_duration)}) have a cruise length outside the standard-fare "
            "reference table {3,4,7 nights} — DISCOUNT_ADD can't verify senior discount isn't already "
            "applied for these; worth extending STANDARD_NCF_BY_NIGHTS if this share is high"
        )

    # Most common addable discount options — a free, no-LLM preview of
    # what the offer-code glossary work would find at scale.
    out.append("")
    rate_data = _load_jsonl(os.path.join(MSC_DIR, "rate_check_data.jsonl"))
    latest_rate: dict[str, dict] = {}
    for r in rate_data:
        latest_rate[r["booking_id"]] = r
    option_counts: Counter = Counter()
    for r in latest_rate.values():
        for opt in (r.get("discount_options") or []):
            option_counts[opt] += 1
    if option_counts:
        out.append("Most common discount-dropdown options seen across all checked bookings:")
        for opt, n in option_counts.most_common(10):
            out.append(f"   {opt:30s} {n:4d}")

    out.append("")


def _pct_value(part: int, whole: int) -> float:
    return part / whole * 100 if whole else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Intelligence report over already-collected scan data.")
    parser.add_argument("--save", metavar="PATH", help="Also write the report to this file.")
    args = parser.parse_args()

    out: list[str] = []
    out.append(f"CruiseIntel Intelligence Report — generated {datetime.now().isoformat(timespec='seconds')}\n")
    espresso_section(out)
    espresso_pattern_mining_section(out)
    msc_section(out)

    report = "\n".join(out)
    print(report)

    if args.save:
        os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\nSaved to {args.save}")


if __name__ == "__main__":
    main()
