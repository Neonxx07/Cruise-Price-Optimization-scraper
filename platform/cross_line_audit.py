"""Cross-line data audit — everything, not just MSC.

Neon 2026-09-03: "review everything and check all the issues and check what
to and what can we make better".

This session's MSC work found that almost every serious defect was one
mistake: comparing two figures that don't cover the same thing. The point of
this script is to look for the SAME class of problem in the stored results
for ESPRESSO, NCL and GoCCL — by checking the numbers against each other
rather than trusting any single field.

Arithmetic self-consistency is the tool: every row claims
old_total, new_total and net_saving, so they must agree. A row where they
don't is either a calculator bug or a persistence bug, and either way it is
a number someone might act on.

READ-ONLY. Opens the SQLite file directly, writes nothing.
"""
import os
import pathlib
import sqlite3
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = str(pathlib.Path(__file__).resolve().parent / "cruise_intel.db")
if not os.path.exists(DB):
    print(f"database not found at {DB}")
    sys.exit(0)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cols = {r["name"] for r in con.execute("PRAGMA table_info(bookings)")}
rows = list(con.execute("SELECT * FROM bookings"))

print("=" * 78)
print(f"CROSS-LINE AUDIT — {len(rows):,} stored booking results")
print("=" * 78)

by_line = Counter(r["cruise_line"] for r in rows)
print("\nROWS BY LINE")
for line, n in by_line.most_common():
    print(f"   {n:>6,}  {line}")

by_status = Counter(f'{r["cruise_line"]}/{r["status"]}' for r in rows)
print("\nSTATUS BY LINE (top 14)")
for k, n in by_status.most_common(14):
    print(f"   {n:>6,}  {k}")

issues = defaultdict(list)


def num(r, key):
    v = r[key] if key in cols else None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


for r in rows:
    bid, line, status = r["booking_id"], r["cruise_line"], r["status"]
    old, new = num(r, "old_total"), num(r, "new_total")
    net, drop = num(r, "net_saving"), num(r, "price_drop")
    obc = num(r, "obc_change")
    lost = num(r, "lost_pkg_value")
    key = (line, bid)

    # 1. the row's own arithmetic must agree
    if None not in (old, new, drop) and new > 0:
        if abs((old - new) - drop) > 0.02:
            issues["drop_disagrees_with_totals"].append(
                (line, bid, f"old {old:,.2f} - new {new:,.2f} = {old-new:,.2f} "
                            f"but price_drop = {drop:,.2f}"))
    if None not in (net, drop, obc, lost):
        if abs(net - (drop + obc - lost)) > 0.02:
            issues["net_saving_formula_broken"].append(
                (line, bid, f"net {net:,.2f} != drop {drop:,.2f} "
                            f"+ obc {obc:,.2f} - lost {lost:,.2f}"))

    # 2. impossible or implausible magnitudes
    if net is not None and old is not None and old > 0 and net > 0:
        if net > old * 0.60:
            issues["saving_over_60pct_of_booking"].append(
                (line, bid, f"net {net:,.2f} on a {old:,.2f} booking "
                            f"({net/old*100:.0f}%)"))
    if old is not None and old <= 0 and status not in ("ERROR", "SKIPPED"):
        issues["zero_or_negative_old_total"].append((line, bid, f"old_total={old}"))
    if net is not None and net < 0 and status == "OPTIMIZATION":
        issues["negative_saving_marked_optimization"].append(
            (line, bid, f"net={net:,.2f}"))

    # 3. an OPTIMIZATION with nothing to act on
    if status == "OPTIMIZATION" and (net is None or abs(net) < 0.01):
        issues["optimization_with_no_value"].append((line, bid, f"net={net}"))

    # 4. self-declared-unconfirmed rows
    note = (r["note"] or "") if "note" in cols else ""
    if "UNCONFIRMED" in note.upper() and status == "OPTIMIZATION":
        issues["unconfirmed_but_marked_optimization"].append(
            (line, bid, f"net={net}"))

print("\nISSUES")
LABELS = {
    "drop_disagrees_with_totals":
        "price_drop does not equal old_total - new_total",
    "net_saving_formula_broken":
        "net_saving != price_drop + obc_change - lost_pkg_value",
    "saving_over_60pct_of_booking":
        "saving is >60% of the booking — the shape of every scope bug so far",
    "zero_or_negative_old_total":
        "old_total is zero/negative but the row is not an error",
    "negative_saving_marked_optimization":
        "negative saving stored as an OPTIMIZATION",
    "optimization_with_no_value":
        "OPTIMIZATION with no dollar value attached",
    "unconfirmed_but_marked_optimization":
        "note says UNCONFIRMED yet status is OPTIMIZATION",
}
for key, label in LABELS.items():
    got = issues.get(key) or []
    if not got:
        print(f"   ok    {label}")
        continue
    lines_hit = Counter(g[0] for g in got)
    print(f"\n   {len(got):>4}  {label}")
    print(f"         by line: {dict(lines_hit)}")
    for line, bid, detail in got[:6]:
        print(f"           {line} {bid}: {detail}")
    if len(got) > 6:
        print(f"           ... and {len(got)-6} more")

# 5. money currently claimed, and how much of it is soft
print("\nMONEY CLAIMED")
for line in by_line:
    tot = con.execute(
        "SELECT SUM(net_saving) s, COUNT(*) n FROM bookings "
        "WHERE cruise_line=? AND status='OPTIMIZATION' AND net_saving>0",
        (line,)).fetchone()
    soft = con.execute(
        "SELECT SUM(net_saving) s, COUNT(*) n FROM bookings "
        "WHERE cruise_line=? AND status='OPTIMIZATION' AND net_saving>0 "
        "AND UPPER(COALESCE(note,'')) LIKE '%UNCONFIRMED%'", (line,)).fetchone()
    if tot["n"]:
        print(f"   {line:<10} ${tot['s'] or 0:>12,.2f} across {tot['n']:>4} rows"
              f"   of which UNCONFIRMED: ${soft['s'] or 0:>10,.2f} "
              f"({soft['n']} rows)")
con.close()
