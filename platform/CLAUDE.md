# CruiseIntel — working agreement

A cruise-travel-agency repricing platform. It scans real client bookings
on live agent portals and reports where a booking can be repriced cheaper.

**The money is real and the bookings are real clients'.** A wrong figure
is worse than no figure, and a wrong action is worse than a missed one.

---

## Priorities, in this order

**Correctness > Safety > Reliability > Recovery > Performance.**

Skipping a booking safely always beats acting on it wrongly.

---

## Hard rules — do not break these

1. **Never delete captured data.** Not scans, not snapshots, not
   market_data rows — they are the corpus used to make results better.
   Never quietly reduce fidelity either. If data is wrong, **record which
   rows are wrong**; do not remove them.
2. **The destination is an AI agent - CHANGED by Neon 2026-10-15** (it
   replaces his 2026-08-25 "no AI/LLM anywhere" rule): "the roadmap at the
   end is make an ai agent doing all of this instead of a static codes
   running". It is roadmap phase **P5** (docs/ROADMAP.md). Until P5 is
   designed and Neon approves it, the scanners stay as they are: no model
   calls dropped in piecemeal. Every other hard rule here binds the agent
   too, enforced in code, never only in a prompt. Any API key goes through
   the OS keyring like every credential (rule 11).
3. **Never guess a selector.** Verify against real captured data in
   `cruise_intel.db` or `data/` first.
4. **Never click a final purchase/confirm button.** Mutating clicks are
   never retried — a retried submit can double-submit against a real
   booking. Navigations (GETs) may be retried.
5. **ESPRESSO never runs headless.** Re-tested 8/8: every headless mode
   404s at the CDN edge. The guard is pinned by tests.
6. **Never log into ESPRESSO by hand while a scan is running.** One active
   session per account — doing so causes a real logout.
7. **A confirmed PAID_IN_FULL or CANCELLED booking is never re-scanned.**
   Non-negotiable. It is still **reported** every run, from the database.
8. **`release_booking` stays in `check_booking`'s `finally`.** It once had
   one call site against 21 exit paths and left 389 bookings locked.
9. **Agency commission is collected and stored, never read** by status,
   saving or recommendation logic. CruiseIntel reports the price difference;
   Neon calculates commission himself.
10. **No anti-bot evasion, ever.**
11. **Credentials never appear in chat or code** — `save_login.py` + the OS
    keyring only.
12. **OBC is not cash.** A cash price increase is never an optimization,
    however much OBC is gained.
13. **Never add a deposit or bring a payment date nearer.** Neon,
    2026-10-15. A repricing that adds a deposit, moves a pending payment,
    or makes the final payment due EARLIER is NO_SAVING, whatever it
    saves. The ONLY allowed change is the final payment moving LATER. An
    unread schedule is never assumed unchanged. One function for every
    line: `core/payment_schedule_rule.py`, checked before any money figure.
    **Princess is the exception** (Neon, same day: "a thing we keep an eye
    on but it is not as madnatory as carnival"): the same function runs,
    but a change is written into the note (`WATCH PAYMENT DATES`), never
    turned into NO_SAVING.
14. **Green means an available lower price - nothing else.** Neon,
    2026-10-15: "THROUGH THE WHOLE PROJECT GREEN IS FOR AVAIABLE LOWER
    RPICES ONLY AND PRICE DIFFERNCES". A lead without a proven lower price
    is never green (MSC discount leads are ORANGE, unconfirmed candidates
    yellow, status panels blue). Pinned by
    `tests/test_green_means_lower_price_2026_10_15.py`.
15. **Save the codes BEFORE and AFTER for every line, every booking.**
    Neon, 2026-10-15: "REALLY REALLY IMPORTNANT". `old_promos` /
    `new_promos` (ESPRESSO fare names, NCL promo codes, GoCCL "CODE NAME"
    offers, Princess offer codes + package / quote promos) and MSC
    `discounts_before` / `discounts_today` - also on paid-in-full and
    cancelled rows when the page showed them. Pinned by
    `tests/test_before_after_every_line_2026_10_15.py`.

---

## Environment

```
Project    <your-checkout>/platform
Python     C:\cruisevenv\venv\Scripts\python.exe
Tests      python -m pytest tests/ -q          (1,980 passing, 2026-10-16)
Lint       python -m ruff check .              (narrow gate: F, E9, B002/B006/B012)
GUI        START_GUI.bat   or   python -m gui.main
Database   cruise_intel.db  (SQLite)
Log        data/cruiseintel.log  (JSON lines)
```

**Not a git repo.** There is no undo. Before changing data, write a
reversible record of what you changed.

Windows: the Bash tool is Git Bash. Use the **Edit tool** for multi-line
Python — heredocs have corrupted files here twice by eating `\n`.

---

## Read these first

| File | What it is |
|---|---|
| `docs/BUGS_AND_FIXES.md` | **Every defect, root cause and fix.** Read before changing anything. |
| `docs/ROADMAP.md` | What is built, what is next, priorities |
| `docs/ESPRESSO_PERK_TIERS.md` | The four mutually-exclusive perk tiers and the invoice sign rules |
| `docs/MSC_DISCOUNT_RULES.md` | MSC discount stacking and reprice rules |
| `docs/HANDOFF_2026_10_01.md` | The last full state handoff |

---

## Architecture

```
gui/            PySide6 + qasync desktop app (windows.py, main.py, monitor_tabs.py)
services/       booking_service (the batch engine), exclusion_service,
                cache_service, outcome_service, excel/csv export
scraper/        base.py (shared) + espresso.py, ncl.py, goccl.py, princess.py
core/           calculator.py (ALL money logic), models.py, booking_features.py
models/         database.py — SQLAlchemy async + aiosqlite
```

**Cruise lines:** ESPRESSO (Royal Caribbean/Celebrity, the main one), NCL,
GoCCL (Carnival), MSC (separate controller), Princess POLAR (in the GUI
from 2026-10-15; automatic OneSource sign-in, headless proven -
docs/PRINCESS_POLAR.md).

**The flow:** `BookingService._run_batch` drives a scraper's
`check_booking` per booking → `core/calculator.py` decides the status →
result persisted → GUI/export reports it.

---

## How to find bugs here

**Audit the live data, not the code.** Every defect found on 2026-10-07
was proven against `cruise_intel.db` or `data/cruiseintel.log` *before*
anything was changed. Code review tells you what could be wrong; the
corpus tells you what is. Three of those four had been sitting in captured
data for weeks.

Useful starting points:

```bash
# what a run actually did
grep '"event": "batch.complete"' data/cruiseintel.log | tail -5

# event tallies for a day
grep "2026-10-07" data/cruiseintel.log | grep -oE '"event": "[^"]+"' | sort | uniq -c | sort -rn

# invariants worth re-running
sqlite3 cruise_intel.db "select count(*) from bookings where status='OPTIMIZATION' and new_total>old_total"
sqlite3 cruise_intel.db "select cruise_line,reason,count(*) from permanent_exclusions group by 1,2"
```

### Three failure patterns that recur

1. **A fix applied to one scraper and never carried to the others.** Both
   NCL defects found on 2026-10-07 were ESPRESSO fixes nobody carried
   over. **When you fix a scraper, check the other two.**
2. **A capability that exists but is never called.** `auto_login()` read as
   "we have auto-login" for weeks while nothing called it mid-scan. **Grep
   for call sites, not definitions.**
3. **State cached on `self` outliving its booking.** See
   `BaseScraper.PER_BOOKING_STATE`.

---

## Testing conventions

- **Assert the outcome, not the mechanism.** A test that pinned
  `_read_payment_status()` call counts passed happily while the feature
  did nothing.
- **Never match comments.** For ordering assertions, strip comments with
  `tokenize` or use AST. String `index()` has matched its own comments
  three times in this project.
- **Prefer corpus-wide invariants.** The strongest tests here run every
  capture the project holds through a rule — e.g. "no capture may produce
  an OPTIMIZATION at a higher price."
- **Mutation-test a new guard**: reintroduce the bug, confirm the test
  fails, restore.
- Name files `test_<subject>_<YYYY_MM_DD>.py`, and put the evidence — real
  booking ids, real counts, the log lines — in the module docstring. That
  is what makes them readable a month later.
- **A test defending a behaviour is not evidence the behaviour is
  correct.** A 2026-08-13 audit blessed a price-increase-as-optimization
  bug and wrote a test to lock it in.

---

## Before saying something is fixed

Tests passing is not the same as working. Two price fixes passed their
tests and did nothing live. **Name the log line that will prove it**, then
wait for a real run.

### Proven live 2026-10-08

ESPRESSO re-login mid-scan (`espresso.relogin_verified ok=True` ×2), NCL
cancelled detection (`ncl.cancelled` ×2), NCL permanent exclusions (94
`exclusion.recorded cruise_line=NCL`), restricted/WLT prices (63/63 WLT
rows carry a price).

### Currently unproven (added 2026-10-08)

| Fix | The line that proves it |
|---|---|
| Re-login waits for the SSO chain to land (BUGS 3.8) | `espresso.auto_login result=ALREADY_LOGGED_IN via=sso_redirect_landed`, and `result=OK` where `FILLED_AWAITING_MFA` + `relogin_mfa_completed waited_s≈10` used to appear |
| GUI shows login trouble | `login_session_lost` / `login_recovered` in `actions.jsonl`, and the status line during a drop |
| MSC status-match flag | `STATUS MATCH:` leading the Note of an MSC row |
| GoCCL candidates confirmed on /review (2026-10-12, ROADMAP P2.2) | **PROVEN LIVE 2026-10-12** (data/goccl_live_20261012T133708): DEMO40 $56.00 and DEMO43 $56.22 confirmed on review, matching Neon's own figures to the cent; DEMO15 correctly no further saving. Signed in by hand — no GoCCL credential is saved yet (SAVE_LOGIN.bat option 5) |
| ESPRESSO perk kept via Promotions → Compare (2026-10-13, booking 3000067; `core/espresso_perk_compare.py`, `EspressoScraper._perk_compare`) | `espresso.perk_compare outcome=DONE` with a `new_total` on a column, and a Note starting `PERK KEPT —`. First run supervised: Select sits next to Compare and must never be clicked |
| GoCCL "Run hidden (headless)" in the GUI (2026-10-15) | a GoCCL GUI run with the box ticked producing `goccl.result` lines and the same verdicts as a windowed run |
| GoCCL 3 bookings at once (not built - test first) | `scratchpad/goccl_parallel_test.py`: `differences_vs_gui_today` empty and no errors, run only when no GoCCL scan is active |
| ESPRESSO re-add note + TRAP no longer charge a re-addable perk twice (2026-10-13) | an ESPRESSO Note containing `is the saving with it kept` |
| Payment schedule rule checked before the invoice sum; TFPA FunPlay read as a credit (2026-10-15, DEMO16, BUGS 1.13) | a GoCCL Note containing `PAYMENT SCHEDULE: a DEPOSIT would be added` (DEMO16 on its next scan), and no `unrecognised codes ['TFPA']` |
| ESPRESSO searches from /home - reservations.do now forwards to /logout (2026-10-16, BUGS 3.10, the real "logging in and out") | **Proven on 2 bookings outside the GUI.** In a GUI run: `espresso.navigate search_on=here/home`, no `espresso.session_lost` between bookings, and no `espresso.search_box_not_on_home` |
| ESPRESSO re-login + one automatic second login (2026-10-15, BUGS 3.9) | `espresso.relogin_verified ok=True`; the proof now checks the search box on /home (3.10) |
| Princess in the GUI (2026-10-15, `PrincessScraper`) | a GUI Princess run with `princess.auto_login result=OK`, `princess_result` actions and rows in `bookings` with cruise_line=PRINCESS. The scraper itself is proven live outside the GUI (auto sign-in, POLAR, 3 bookings windowed = headless) |
| MSC Old/New prices + orange discount leads (2026-10-15) | new `msc_results` rows with `current_total_price` / `today_price` filled, and MSC discount-lead rows orange, not green |
| NCL Canada pass in the NCL tab + USD/CAD on every NCL row (2026-10-16) | `gui.ncl_other_market_pass market=CA`, then rows whose Note starts `[CA account, CAD]`. Proven live outside the GUI: 3001051 found on CA after US said not-found |
| NCL "3 at once" - separate logins, headless (2026-10-16) | `gui.ncl_parallel_start workers=3`; no `batch.fatal`. Proven live through BookingQueueManager: 9 bookings, 3 logins, all results, 108 s incl. the CA pass |
| GUI "Open in portal" button (2026-10-09; ESPRESSO, NCL, GoCCL — not MSC yet) | `gui.open_in_portal_ok` with no `espresso.session_lost` after it. Opens a new tab in the line's own browser, never a second login |

Re-login itself is **proven**: 2026-10-09 15:32 and 15:33 `espresso.relogin_verified ok=True`. Those two drops were caused by a manual login during the run (Neon) — rule 6, not a code fault.

---

## Working with Neon

- He checks results against the portal by hand and catches real errors.
  **Investigate a report even when the premise turns out to be wrong** —
  that is how the $97,565 under-quoting bug was found.
- Report outcomes faithfully. If a fix failed, say so with the numbers.
- Corrections of your own earlier claims: state them plainly once, then
  move on.
- He writes quickly and in caps when something matters. Treat emphasis as
  signal about priority, not tone.
