# Changelog

All notable changes to this project are recorded here.

## How this works

- The **project version** is the headline: `1.0`, `1.1`, `1.2` …
  Bump the minor for a release with new capability; add a patch (`1.1.1`) for
  a fix-only release.
- Every release has **Added** (what's new) and **Fixed** (what was broken),
  plus **Changed**, **Security** or **Removed** where they apply.
- Work lands under `## [Unreleased]` as it happens, and that heading is
  renamed to the version on release day.
- On release: update `app_version` in `platform/config/settings.py`, then tag
  it — `git tag -a v1.1 -m "v1.1" && git push origin v1.1`.
- **The Chrome extension keeps its own version line** (currently `6.x` in
  `extension/manifest.json`). Chrome refuses an update whose version number
  goes down, so it can never be renumbered to match the project. Bump it only
  when the extension itself changes, and note the bump inside the release.
- Never put a real booking reference, customer or operator name in an entry.
  Use the placeholders described in [`SECURITY.md`](SECURITY.md).

---

## [Unreleased]

### Added
- **`tools/extract_page_metadata.py` and the `booking_group_info` table.** Page snapshots
  carried five fields that existed nowhere in the database — Group ID, Group Name, Group
  Type, Policy Type and Option Date — on 27% of pages. They are now stored per
  `(booking_id, captured_at)`, identifying **348 group bookings**, which is a known cause of
  "Booking restriction - price program change not allowed" and was previously invisible.
  Agent Name and Agency phone are deliberately **not** extracted (personal data, no
  analytical value), and Total Guests is skipped because on ESPRESSO it is group-level, not
  per-cabin — one sampled booking read 237 guests across 100 staterooms.

### Removed
- **`data/pages/` (46,328 HTML/JSON snapshots, 10 GB) deleted on 2026-10-17**, at the
  project owner's explicit instruction and only after coverage was measured rather than
  assumed: `categories_table` 99% already in `market_data`, `booking_summary` 98%,
  `offer_code_comparison` 100%, `after_search` 87% — and the 87% gap is lines that never
  write `market_data` at all, whose outcomes live in `bookings`, not data loss. The five
  fields that were *not* covered were extracted first (above). Disk went 14 GB → 3.5 GB.

  This is a deliberate, recorded exception to the project's standing rule that captured
  data is never deleted. The rule stands; what made this case different is that the
  deletion was preceded by a measurement showing what the files still held.

### Security
- **Three example booking numbers brought onto the placeholder convention.** The extension's
  input placeholder and four docs used bare 7–8 digit numbers whose shape is
  indistinguishable from a real ESPRESSO reference. Checked against every identifier in the
  database and the watchlists (1,693 of them) they match nothing, so this is a consistency
  fix rather than an exposure — but a number that *looks* real costs a reader the same
  verification every time, and a scan that has to pause on a false positive is a scan that
  eventually waves through a true one. They are now `300xxxx` like every other example.
- **`.githooks/install.sh` now warns against a bare surname in the local denylist.**
  Matching is substring, so a common surname fires on ordinary code — a real seeding of
  that file blocked this very commit on `iPlanetDirectoryPro`. The template asks for the
  full name on one line instead, because a hook that cries wolf gets `--no-verify`d, and
  that is how a true match eventually goes through with it.

---

## [1.2] — 2026-10-17

### Added
- **Princess (POLAR) is live — the fifth cruise line.** `scraper/princess.py` reads a
  booking and prices its own category (read-only; it never commits a refare), and
  `core/calculator_princess.py` was written *before* the scraper from figures that close
  exactly, so the decision logic was settled independently of page scraping.
  `CruiseLine.PRINCESS` now exists; behaviour recorded in `docs/PRINCESS_POLAR.md`.
- **The payment-schedule rule** (`core/payment_schedule_rule.py`) — a deposit or payment
  date is never added to a saving, applied across all five lines rather than re-derived
  per scraper.
- **Pricing a perk, not just naming it** (`core/espresso_perk_compare.py`) — reads
  ESPRESSO's Promotions → Compare screen, so a booking that would give up its perk tier
  now reports what *keeping* the perk costs.
- **MSC status match** — `services/status_match_service.py` plus `MSC_Status_Match_Toolkit/`,
  preparing one customer's evidence at a time.
- **HubSpot booking import** (`services/hubspot_client.py`) — booking numbers read from
  HubSpot instead of copy-paste; the token lives in the OS keyring.
- **Saved lists per cruise line** (`gui/saved_list.py`), NCL parallel workers and a
  Canada-market pass, GoCCL review-walk/confirm/category-bar handling, and
  `docs/BUGS_AND_FIXES.md`.
- `tools/export_session.py`, and `platform/CLAUDE.md` recording the project's working rules.

### Security
- **`.gitignore`: booking lists moved into `platform/watchlists/` on 2026-10-16 and the
  existing rules — anchored at `platform/` root — did not reach them. 877 real booking
  numbers across 10 files would have been committed.** The directory is now ignored
  wherever it appears.
- **Exported session transcripts are ignored.** `tools/export_session.py` is source and is
  committed; its output is a raw conversation dump — the 2026-10-07 one was 4.2 MB and
  76k lines, carrying 794 real booking numbers.
- **Real customer PII removed from test fixtures and source.** A saved GoCCL guest page
  carried a named individual's full name, date of birth and loyalty number, repeated in
  the test's expectations and in two source modules. Replaced with a consistent
  placeholder identity, so the tests still assert the same behaviour.
- `*.zip` ignored — an archive duplicated a module whose source sits beside it, and
  carried its `.pyc` files.

### Removed
- The five spent 2026-09-23 headless investigation scripts and `verify_upgrades.py`,
  deleted in v1.1 and restored by a working-copy sync. Re-removed after re-checking that
  nothing imports them.

---

## [1.1] — 2026-10-03

### Added
- **Outcome tracking** (`services/outcome_service.py`). $31,000 of optimizations had been
  reported and none verified. A **Verify** button in the GUI marks one done and removes it
  from the list; separately, if a human applied a reprice without saying so, the next scan
  notices the booking now sits at the quoted price and records it. Run against history this
  found **46 already-applied repricings worth $5,485.90** that the system never knew about.
- **Resumable jobs** — a job that died at booking 400 of 723 used to restart from zero.
- **Retry queue** for errored bookings, most of which are timeouts and session drops.
- **Start logs you in.** Auto-login already worked and was simply never trusted to run;
  the measured cost was 339 stops waiting for a human.
- `core/scan_signature.py` — identifies a scan *request*, so the same list submitted twice
  is not scanned twice.
- `core/calculator_version.py` — fingerprints calculator logic so a change expires stale
  cached verdicts automatically, instead of serving wrong TRAPs until cleared by hand.
- `scraper/click_verify.py` — important actions are verified by their effect rather than
  assumed to have worked because `click()` returned.
- **ESPRESSO perk tiers** — four mutually exclusive tiers (`ALL INC 2PK`, `RETREAT`,
  `NOPERK`, `STANDARD`) derived from the fare name, so a reprice that swaps the client's
  perk is named. All 148 recorded tier changes were downgrades.
- **Dashboard and Logs tabs** (`gui/monitor_tabs.py`), moving logs out of the main window;
  watchdog gained crash, process-death and navigation-retry monitors.
- `docs/ROADMAP.md`, `docs/ESPRESSO_PERK_TIERS.md`, `docs/README.md` (a docs index), and
  dated session handoffs.

### Fixed
- **Bookings were still being left locked.** The release fallback navigated to a
  *relative* URL, which `page.goto()` rejects, so it failed every time that path was taken.
  The September release-in-`finally` fix was working correctly — which is precisely why
  these failures were visible at all.
- **A perk was charged twice** — the same fare name arrived from two sources, once bare and
  once priced, and both were subtracted. 21 bookings corrected, **0 verdicts reversed**:
  the traps were still traps, the magnitudes were wrong.
- **Two dangling asyncio tasks** held no reference, risking collection mid-flight — one on
  GUI shutdown (orphaned browsers, bookings left locked) and one in a GoCCL capture
  listener (silently dropped captured data).
- Test fixtures used a row shape the portal never sends, so tests passed on impossible data
  while real cases went unnoticed.

### Removed
- `platform/scheduler/` — an unreachable APScheduler placeholder, never imported by
  anything outside itself.
- Five spent 2026-09-23 ESPRESSO headless investigation scripts. The question they answered
  is settled, recorded in `docs/ESPRESSO_SESSION_BUGS_2026_09.md`, and enforced by eight
  tests in `test_espresso_headless_blocked_2026_09_23.py`.
- `verify_upgrades.py` — a date-specific checker, unreferenced and superseded by the suite.

> Git history preserves all of the above; nothing captured was deleted.

---

## [1.0] — 2026-10-01

First versioned release. Everything below was already shipped; it is recorded
here so later releases have a baseline to sit on.

### Added

**Cruise lines**
- Royal Caribbean and Celebrity via ESPRESSO, Norwegian via SeaWeb (US and
  Canada accounts), Carnival via GoCCL, MSC via MSC Book.
- Princess (POLAR) package mapping as groundwork — no scraper, not wired in.

**Scanning and orchestration**
- Concurrent multi-line scanning over one shared Chromium, with an isolated
  browser context per line, a CPU/RAM throttle gate and per-line failure
  isolation. Previously impossible: a single scraper slot meant switching
  lines killed the other line's session.
- Scan awareness — per-line freshness windows and outcome caching, after
  measurement showed 41% of a day's scans were redundant.
- Permanent exclusions for paid-in-full and cancelled bookings, checked before
  any browser action, with stored evidence and reversible entries.
- Price-change detection against the previous scan.
- `scan_watchdog.py` — monitors a run while it is in progress.

**Interfaces**
- PySide6 desktop GUI with booking queue, live results and CSV/Excel export.
- FastAPI server and a CLI.
- Chrome MV3 extension (own version line, currently 6.4).

**Business logic**
- Net-saving calculation with trap detection, package tracking, 1–5 confidence
  scoring and paid-in-full detection.
- MSC's three independent opportunity types, since price and discount move
  separately there and MSC never allows an in-portal reprice.
- GoCCL `/review` invoice reading and fare-type scoring.
- Price-scope guards, after nearly every serious defect turned out to be the
  same mistake: comparing two numbers that don't cover the same thing.

**Tooling**
- OS-keychain credential storage, portal-onboarding helpers, history analysis
  and audit scripts.

### Fixed
- **Bookings left locked.** The portal holds a 15-minute lock on every
  retrieved reservation; the release had one call site on the happy path while
  the flow had 15 returns and 6 raises, so most exits skipped it. It now runs
  in a `finally`, with a structural test keeping it there.
- **A self-inflicted navigation race** caused hundreds of aborted navigations
  and phantom "logged out" flashes — the flow left `/home` while its session
  bootstrap redirect was still in flight.
- **Crashes never reached the log**, having been printed to stdout; one killed
  an overnight 721-booking scan invisibly.
- **Session recovery was one-shot**, so every long scan died partway, and the
  interrupted booking produced no row at all.
- **A `print()` on the GUI thread hung the app** — a paused Windows console
  blocks the writer.
- **GUI updates cost 352.8ms per row** (~12.7 minutes of frozen window per
  run), now 0.002ms.
- Net-saving sign inversion, OBC-loss masking, a per-person rate compared
  against a whole-booking total, and currency-aware paid-in-full.

### Security
- `.gitignore` rebuilt as an **allowlist** under `platform/`, after
  individually-named rules let a batch of newly-named files through.
- Pre-commit hook blocking booking references (numeric **and** PNR-style),
  operator names, personal paths, credentials and data files.
- [`SECURITY.md`](SECURITY.md) with the data-handling policy and the incident
  record behind each rule.
- `.gitattributes` line-ending normalisation, so real edits stop hiding inside
  whole-file diff noise.

---

## A note on what is not here

Development happens on a separate machine that is not a git checkout, so work can
exist there before it reaches this repository. Anything not in this file and not in
the repository is backed up nowhere — if you are about to do something substantial,
commit it first.
