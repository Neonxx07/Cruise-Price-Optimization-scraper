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
- **ESPRESSO perk tiers.** Four mutually exclusive tiers (`ALL INC 2PK`,
  `RETREAT`, `NOPERK`, `STANDARD`) derived from the fare name, with the tier
  change named on any affected result. Across 3,074 captured reprice
  responses, all 148 tier changes were downgrades — a reprice has never once
  improved a client's perk.

### Fixed
- **A perk was charged twice.** The same fare name arrived from two sources,
  once bare and once priced, and both were subtracted. Corpus-wide this
  removed phantom loss from 21 bookings with **0 verdicts reversed** — the
  traps were still traps, the magnitudes were wrong.
- **Test fixtures used a row shape the portal never sends.** A perk was built
  as a typed `paxId: "total"` row; measured against real data, every
  `paxId: "total"` row is untyped and every typed row is per-passenger. The
  tests passed on impossible data while real cases went unnoticed.

> Not yet committed — this work currently exists only on the development
> machine. See the note at the end of this file.

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

Some work exists only on the development machine and has never been committed.
It is listed under **[Unreleased]** above so it is at least recorded. Anything
not in this file and not in the repository is backed up nowhere.
