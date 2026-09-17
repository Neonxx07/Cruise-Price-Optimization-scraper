"""Verify every fix from the 2026-09-01..15 work is actually LIVE.

Neon: "make sure that all the updates and bug fixes are made and done and
now rerun everything so we start fresh with the new upgrades".

Checks the shipped code, not the tests. Written because this project has
repeatedly had a fix that was written, documented and unit-tested while
being completely inert in production - `msc_occupancy_is_trustworthy` sat
uncalled for a day, the overpayment flag reached only one check, and the
NCL fix itself was correct on disk for three hours while a live process ran
the old code from memory.

READ-ONLY. Exits non-zero if anything is missing.
"""
import ast
import pathlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).resolve().parent

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def src(rel):
    try:
        return (HERE / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


# ── the outage ──────────────────────────────────────────────────────
ncl = src("scraper/ncl.py")
check("NCL: balance_is_all_commission assigned before it is logged",
      ncl.find("balance_is_all_commission = (") < ncl.find('"ncl.commission"'),
      "the 2026-09-15 outage - failed all 135 bookings")

check("GUI: warns when the running code is stale",
      "def stale_modules(" in src("gui/windows.py")
      and "stale_modules()" in src("gui/windows.py"),
      "what actually cost the 2-hour NCL run")

# ── lint gate ───────────────────────────────────────────────────────
check("ruff config present and defect-only",
      '"F",' in src("ruff.toml") and '"E501"' not in src("ruff.toml"))

# ── MSC correctness ─────────────────────────────────────────────────
msc = src("msc_commands.py")
calc_msc = src("core/calculator_msc.py")
check("MSC: Voyagers Club membership entered during staging",
      "_apply_voyagers_club" in msc and "voyagers_fix = await" in msc,
      "the like-for-like bug that hid 67 bookings' opportunities")
check("MSC: occupancy guard is CALLED, not just defined",
      msc.count("msc_occupancy_is_trustworthy(") >= 2)
check("MSC: overpayment is a hard stop",
      "is_overpayment:" in calc_msc and "OVERPAID" in calc_msc)
check("MSC: non-cruise charges backed out",
      "non_cruise_charges" in calc_msc and "non_cruise_charges" in msc)
check("MSC: invoice reconciliation available and used",
      "def msc_invoice_components(" in msc
      and "msc_invoice_components(" in src("msc_run_calculator.py") + msc)
check("MSC: zero booking total treated as not captured",
      "current_total_price <= 0" in calc_msc)
check("MSC: booking-id validation on every batch command",
      msc.count("is_valid_msc_booking_id(b)") >= 4)
check("MSC: PriceScope backstop wired into price match",
      "scopes_comparable" in calc_msc and "PriceScope(" in msc)

# ── live vs replay parity ───────────────────────────────────────────
def kwargs_at(rel, fname="evaluate_msc_booking"):
    seen = set()
    try:
        tree = ast.parse(src(rel))
    except SyntaxError:
        return seen
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            nm = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if nm == fname:
                seen |= {k.arg for k in node.keywords if k.arg}
    return seen


GUARDS = {"is_overpayment", "occupancy_verified", "customer_has_club_membership",
          "today_price_includes_club_discount", "non_cruise_charges",
          "current_scope", "today_scope"}
live = kwargs_at("msc_commands.py") & GUARDS
replay = kwargs_at("msc_run_calculator.py") & GUARDS
check("live and replay paths apply the SAME guards",
      live == replay == GUARDS,
      f"live-only={sorted(live - replay)} replay-only={sorted(replay - live)}")

# ── other cruise lines ──────────────────────────────────────────────
# Structural, not a text match: the note's wording is split across two
# f-string lines, and a phrase search reported a false FAIL on a fix that
# was present and passing its own tests. Check the GUARD, not the prose.
_goccl = src("core/calculator.py")
check("GoCCL: a candidate with no offer code is not a saving",
      'offer_code = str(cheapest.get("offer_code")' in _goccl
      and "if not offer_code:" in _goccl,
      "$3,180 of $4,100 GoCCL ever claimed was unactionable")

# ── sorting and freshness ───────────────────────────────────────────
gui = src("gui/windows.py")
check("GUI: booking ids sort numerically",
      "_booking_id_sort_value" in gui)
check("GUI: status sorts by importance, not alphabetically",
      "_STATUS_RANK" in gui and "_row_rank" in gui)
check("GUI: table arrives arranged (best findings first)",
      "sortByColumn(2" in gui)
check("GUI: MSC rows show their dollar value",
      "best_value" in gui and "PRICE_MATCH" not in gui.split("best_lever")[0][-200:])
check("data: live check results are timestamped",
      'record["captured_at"]' in msc)
check("data: calculator report is timestamped",
      "generated_at" in src("msc_run_calculator.py"))
check("data: freshest record chosen by DATE, not read order",
      "captured_at" in src("msc_run_calculator.py").split("def _load_last_by_id")[1][:1800])

# ── efficiency ──────────────────────────────────────────────────────
base = src("scraper/base.py")
# Neon 2026-09-15: "do not delete the data because we use it to use it
# make the project better and the resu,lts better". Both of these assert
# the OPPOSITE of what they did an hour earlier - I had shipped a pruner
# and a smaller screenshot to save disk, and both cost captured data.
check("failure screenshots capture the WHOLE page",
      "full_page=True" in base.split("failures_dir = os.path.join")[1][:1600],
      "a viewport shot loses everything below the fold")
check("failure snapshots are NOT auto-deleted",
      "MAX_FAILURE_SNAPSHOTS_PER_BOOKING = None" in base
      and "_prune_failure_snapshots(" not in
          base.split("failures_dir = os.path.join")[1][:2500],
      "captured failures are the corpus this project mines")
check("database: composite index declared",
      "ix_bookings_line_created_at" in src("models/database.py"))

db = HERE / "cruise_intel.db"
if db.exists():
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("database: composite index EXISTS on the live database",
          "ix_bookings_line_created_at" in names)
    stuck = con.execute(
        "SELECT COUNT(*) FROM scan_jobs WHERE status IN ('RUNNING','PENDING')"
    ).fetchone()[0]
    check("database: no scan job stuck RUNNING/PENDING", stuck == 0,
          f"{stuck} stuck job(s) would confuse the next run")
    con.close()

# ── report ──────────────────────────────────────────────────────────
width = max(len(n) for n, _, _ in results)
print("=" * (width + 12))
print("UPGRADE VERIFICATION")
print("=" * (width + 12))
failed = 0
for name, ok, detail in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}")
    if not ok:
        failed += 1
        if detail:
            print(f"        {detail}")
print("-" * (width + 12))
print(f"  {len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
