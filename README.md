<div align="center">

# ⚓ Cruise Price Intelligence System (Playwright + AI Optimization)

**Automated repricing intelligence for Royal Caribbean, Celebrity, Norwegian, Carnival & MSC Cruises**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-4285F4?logo=googlechrome&logoColor=white)]()
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)]()
[![Desktop GUI](https://img.shields.io/badge/Desktop%20GUI-PySide6-41CD52?logo=qt&logoColor=white)]()

</div>

---

## What Is This?

A **monorepo** containing two versions of the same cruise booking optimization tool:

| Project | Directory | Technology | Use Case |
|---------|-----------|------------|----------|
| 🧩 **Browser Extension** | [`extension/`](extension/) | JavaScript · Chrome MV3 | Quick checks from your browser |
| 🐍 **Python Platform** | [`platform/`](platform/) | Playwright · FastAPI · SQLAlchemy · PySide6 | Batch processing, desktop GUI, API, automation |

Both share the **same core business logic** — detecting price drops, tracking package losses, calculating net savings, and scoring optimization confidence.

---

## How It Works

```
┌─────────────────────────────────────────────────────┐
│              Cruise Booking Portal                    │
│    (ESPRESSO / NCL SeaWeb / GoCCL / MSC Book)         │
└──────────────────┬──────────────────────────────────┘
                   │ Scrape prices
        ┌──────────┴──────────┐
        ▼                     ▼
  ┌───────────┐        ┌───────────┐
  │ Extension │        │ Platform  │
  │ (Browser) │        │ (Python)  │
  └─────┬─────┘        └─────┬─────┘
        │                     │
        ▼                     ▼
  ┌───────────────────────────────────┐
  │     Price Comparison Engine        │
  │  net = priceDrop + OBC - lostPkg   │
  │  confidence = 1-5 stars            │
  └───────────────────────────────────┘
        │                     │
        ▼                     ▼
   Popup UI          Desktop GUI · REST API
                     + Database + CSV/Excel
```

### Core Features

- ✅ **Price drop detection** — compares old vs new invoice totals
- ⚠️ **Trap detection** — catches price drops that lose packages (net loss)
- 📦 **Package tracking** — identifies lost/gained packages and their values
- ⭐ **Confidence scoring** — 1-5 star reliability rating per optimization
- 💳 **Paid-in-full detection** — skips bookings that can't be repriced
- 🔄 **Smart caching** — avoids rechecking recently-checked bookings
- ⚡ **Concurrent multi-line scanning** — ESPRESSO + MSC + NCL together on one shared browser,
  CPU/RAM-throttled so the machine stays usable
- 🖥️ **Desktop GUI** — queue bookings, watch a scan live, export results
- 🔐 **OS-keychain credential storage** — no passwords in files or env vars
- 📋 **CSV / Excel export** — download results for reporting

---

## Quick Start

### Chrome Extension

1. Clone this repo
2. Open `chrome://extensions` → Enable **Developer Mode**
3. Click **Load Unpacked** → select the `extension/` folder
4. Log into your cruise portal (ESPRESSO, NCL SeaWeb, or GoCCL)
5. Click the extension icon → paste booking numbers → Run Check

### Python Platform

```bash
cd platform
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Desktop GUI (recommended)
python -m gui.main

# Start the API server (opens Swagger docs at /docs)
python main.py api

# Or run a CLI scan (--cruise-line: ESPRESSO, NCL, or GOCCL)
python main.py scan --bookings "1234567,7654321" --cruise-line ESPRESSO -o results.csv
```

> **Contributors: install the git hooks first.**
>
> ```bash
> ./.githooks/install.sh
> ```
>
> Booking numbers shown anywhere in this repo are **fake demo values** (`300xxxx`,
> `DEMOnn`). Real reservation numbers, scan output, captured pages, session cookies and the
> local database are git-ignored by design, and a pre-commit hook blocks them from being
> committed by accident. See [`SECURITY.md`](SECURITY.md).

---

## 🖥️ Desktop GUI

A PySide6 desktop app (**CruiseIntel Desktop Scanner**) — the easiest way to run the platform,
and the only front end that drives every supported cruise line including MSC.

```bash
cd platform
python -m gui.main          # any OS
START_GUI.bat               # Windows double-click launcher
```

| Feature | Detail |
|---------|--------|
| **Booking queue** | Add one at a time or paste in bulk; remove selected or clear the whole queue |
| **Cruise line selector** | Pick the portal before scanning — the GUI routes to the right engine |
| **Login check** | Verifies the saved session is still valid *before* a scan starts, so a batch doesn't die halfway |
| **Live results table** | Rows fill in as each booking finishes; Stop halts cleanly between bookings |
| **Headless toggle (NCL only)** | "Run hidden (headless)" appears only for NCL, where it was measured end-to-end. ESPRESSO can never be headless (Akamai); MSC/GoCCL are untested, so no toggle is offered |
| **MSC support** | Routed through `services/msc_live_service.py` rather than the shared scan pipeline, because an MSC result carries three-to-four independent opportunity checks instead of one net-saving figure |
| **Export** | Writes `reports/scan_results.csv` + `.xlsx` (MSC results export separately, with their own columns) |

> The GUI's first-run setup installs PySide6 into a short venv path (`C:\cruisevenv`) on
> Windows — PySide6 ships filenames long enough to hit the `MAX_PATH` limit under a deeply
> nested project folder. See [`platform/START_GUI.bat`](platform/START_GUI.bat).

---

## 🔐 Login & Credential Storage

Credentials go into your **operating system's own secure store** via
[`keyring`](https://pypi.org/project/keyring/) — Windows Credential Manager, macOS Keychain,
or Linux Secret Service, whichever is native. Nothing is written to a file, an env var, or
this repo, and the stored secret is encrypted by the OS and bound to your user account on
that one machine — there is no exportable artifact to leak.

```bash
cd platform
python save_login.py        # store a login   (or: SAVE_LOGIN.bat / SAVE_LOGIN.command)
python clear_login.py       # remove a login  (or: CLEAR_LOGIN.bat / CLEAR_LOGIN.command)
```

Both scripts are menu-driven and cover **MSC**, **ESPRESSO** (Royal Caribbean / Celebrity),
and **NCL**. Double-clickable launchers ship for Windows (`.bat`) and macOS (`.command`).

**Design notes worth knowing:**

- **Never paste a password into a chat** — with this tool or any assistant. Run these
  scripts yourself, in your own terminal. Input is masked and never echoed, logged, or printed.
- **Paste actually works.** `getpass()` silently truncated a pasted password to a single
  character in some Windows console hosts — a real failure that caused every subsequent login
  to fail with no visible cause. Password entry now reads directly off the console input
  buffer (`msvcrt.getwch()`), echoes one `*` per character with a live count so a bad paste is
  obvious immediately, and falls back to `getpass()` when stdin isn't a genuine console
  (Git Bash, SSH, piped input). Bracketed-paste escape sequences are stripped too.
- **MFA still needs a human.** ESPRESSO requires MFA at login, so a saved credential does
  **not** make it unattended — it only stores the secret. NCL's login is likewise
  human-driven today. MSC is the one line with a fully automated `auto_login()`.

Session cookies are snapshotted separately to `platform/browser-profile/` (git-ignored),
because Chromium marks the portals' SSO cookies session-only and wipes them on shutdown.

---

## ⚓ MSC: `confirm_and_proceed` and the Read-Only Check Flow

MSC is architecturally different from every other line here: **the portal never allows a
direct in-portal reprice.** The tool surfaces opportunities; a human agent applies them by
phone. Because of that, the MSC flow is deliberately read-only, and the one flow-advancing
click it needs is called out explicitly rather than buried.

**How a check runs.** The automation opens the real booking, starts a *dummy* "Book Same
Departure" flow for the identical sailing (this creates no reservation and needs no cleanup),
and compares that quote against the booking. Three independent opportunity types are always
evaluated separately — price-match, discount-add, and discount-tier-upgrade — plus a
Voyagers Selection check, because price and discount move independently on MSC.

**What `confirm_and_proceed` is.** Reaching the price/category grid inside that dummy flow
requires clicking MSC's **"CONFIRM AND PROCEED"** button (dismissing the "Policy Reminder"
popup first if it's showing). Despite the commit-sounding name, this advances the *dummy*
quote only — it never touches a save, payment, or purchase control on the real reservation.

```
lookup_booking → stage_booking → confirm_and_proceed → harvest → evaluate
                                        ▲
                    the one flow-advancing click; dummy quote only
```

- `check_booking:<id>` runs that entire sequence unattended, and
  `check_booking_batch:<id1,id2,...>` loops it over a list.
- [`platform/confirm_and_proceed.ps1`](platform/confirm_and_proceed.ps1) remains as a manual
  trigger: it sends the click into the already-running browser session via the controller's
  `command.txt`/`result.txt` protocol, so you can advance a staged booking by hotkey instead
  of hunting for the button on screen. It resolves its own path from the script location, so
  it works from any checkout.
- Every automated check ends with an independent re-lookup of the real booking to prove
  nothing was committed. If that verification fails the result is `RESTORATION_FAILED` — not
  a savings figure — and that booking should not be processed further without human review.

Commands that genuinely would commit a change (`CruiseCabinLockCmd` /
`CabinSelectionAddCabinOrder`) are treated as the MSC equivalent of ESPRESSO's
"Continue with New Rate" and are **never** triggered automatically.

---

## 🔒 Keeping Real Data Out of a Public Repo

This tool runs against live agent portals, so the environment around it holds real customer
and operator data — none of which belongs in a public repository. Three defences, each one
added after something actually slipped through:

| Defence | What it does |
|---|---|
| **`.gitignore` allowlist** | Loose files under `platform/` are ignored by default; only genuine source is re-admitted. The old file-by-file rules lost the race when 167 real booking numbers arrived in newly-named watchlists. |
| **Pre-commit hook** | Blocks booking references (numeric *and* PNR-style), operator/customer names, absolute personal paths, credentials, and data/DB/session files — reporting file and line. |
| **`.gitattributes`** | Normalises line endings, so a Windows round-trip stops reporting unchanged files as fully rewritten and hiding real edits in the noise. |

```bash
./.githooks/install.sh     # once per clone — git never installs hooks automatically
```

The installer also creates `.git/sensitive-terms.txt`, a **local** denylist for exact private
values (your real name, agent login, agency id). It lives inside `.git/`, so it is never
committed — the hook itself is public and matches only on shape.

Full policy, plus the incident record that produced these rules:
**[`SECURITY.md`](SECURITY.md)**.

---

## What's New

### Scan watchdog — a third eye on a running scan

[`scan_watchdog.py`](platform/scan_watchdog.py) watches a scan *while it runs* rather than
reporting after the fact, which matters when a run takes hours. Monitors cover session
health, error streaks, slowdown, status mix, structure drift, feature-capture coverage,
cancellations and advisories.

### GoCCL: the review invoice and fare-type scoring

- [`core/goccl_review.py`](platform/core/goccl_review.py) — the `/review` invoice is the only
  *confirmed* price Carnival exposes, reached on the safe path ("Keep Same Stateroom", never
  selecting a different cabin).
- [`core/goccl_fare_types.py`](platform/core/goccl_fare_types.py) — ranks fare types by what a
  cheaper rate actually costs the customer, feeding the 1–5 confidence score, so a headline
  discount with restrictive terms can't outrank a genuinely better fare.

### MSC extras and per-scan feature capture

- [`core/msc_booking_extras.py`](platform/core/msc_booking_extras.py) — parses perks, shipboard
  credit and applied discounts from the bottom of the booking page, where MSC actually puts them.
- [`core/booking_features.py`](platform/core/booking_features.py) — captures the factors that
  move a cruise price on every scan, across all lines, as groundwork for prediction.

### Reliability fixes

ESPRESSO auto-logout and SSO-race handling, session recovery, cancelled-booking detection,
currency-aware paid-in-full, and a GUI Start re-entrancy guard (a second Start click during a
modal could destroy the running batch).

### NCL headless toggle — and why only NCL

The GUI now offers **"Run hidden (headless)"**, and the checkbox appears *only* when NCL is
selected. That scope is measured, not assumed: the same three bookings run headless and
headed returned identical totals **and** identical category counts, driving the whole flow —
Switch to Edit Mode, the SlickGrid read, the price comparison, and cancel-and-release — not
merely opening a booking.

- **ESPRESSO can never run headless.** Akamai bot detection breaks it, and
  `scraper/base.py` enforces that regardless of the setting or any `headless` argument a
  caller passes.
- **MSC and GoCCL simply haven't been tested this way**, so no toggle is offered. Shipping an
  untested toggle would just be inviting the next silent failure.

### Princess (POLAR) — groundwork, not yet usable

[`core/princess_packages.py`](platform/core/princess_packages.py) maps Princess Plus/Premier
fare packages, because POLAR's promo list describes fares by package name and two Princess
fares are usually **not** comparable without knowing what each package contains. No scraper,
no enum entry, not yet wired into the pipeline.

### Sensitive-data guardrails

A pre-commit hook ([`.githooks/pre-commit`](.githooks/pre-commit)) plus
[`SECURITY.md`](SECURITY.md). Two failure modes drove this: a scan is only as good as its
reference list — one exposure persisted 12 days because the ID list never included the
database, and another because every scan matched only 5–9 digit numbers while GoCCL and
Princess use PNR-style codes — and a manual exclusion step that worked for a month
eventually didn't. Both are now mechanical.

### Concurrent multi-line scanning

ESPRESSO, MSC and NCL now run **at the same time, over one shared browser**. Previously this
wasn't merely slow, it was impossible: `BookingService` held a single scraper slot and stopped
the running scraper whenever a different cruise line was requested, killing the previous line's
logged-in session.

- [`scraper/browser_pool.py`](platform/scraper/browser_pool.py) — one Chromium process with an
  isolated `BrowserContext` per cruise line. Contexts are Playwright's isolation primitive
  (cookies/localStorage/cache are per-context), so three agent accounts never see each other's
  session. Replaces one full Chromium *per line* — on a 4-core machine, the difference between
  a usable PC and an unusable one.
- [`services/resource_governor.py`](platform/services/resource_governor.py) — a CPU/RAM-aware
  gate every worker awaits before starting a booking (`max_cpu_percent` 85, `max_ram_percent`
  93), plus a single-instance guard so two coordinators can't fight over the same portal
  sessions. The goal is smoothness and reliability, not maximum concurrency.
- [`services/multi_line_coordinator.py`](platform/services/multi_line_coordinator.py) — a global
  in-flight semaphore (`max_concurrent_bookings`) plus a per-line semaphore so no single line
  hogs every slot, per-line queues, and per-line failure isolation: a stuck line has *its*
  context recycled while the others keep going.

> Deliberately **no** calculator, scraper, or export logic was touched. Every booking still runs
> through the same `check_booking(...)` and the same calculators — this only decides *when* and
> *with which browser* a booking runs, so results cannot change because of it.

### NCL: multi-market accounts and payment rules

- **US / Canada (CAD) markets.** NCL runs a *separate* SeaWeb agent account per market, so a
  Canadian booking checked against the US login returns "Reservation is not found" — it isn't
  missing, it's on the other account (25 of 101 errors in one real run). `NclScraper(market=...)`
  selects the account; `ncl_default_market` keeps existing callers working.
- [`split_ncl_watchlist_by_account.py`](platform/split_ncl_watchlist_by_account.py) — derives
  per-market watchlists from results already recorded in the database, instead of hand-sorting
  booking IDs.
- **Collectable-savings rule.** A price drop is now capped by what's actually still owed: only
  the outstanding balance is collectable, and the optimization note says so explicitly rather
  than advertising a saving the client can't realise. Paid-in-full tolerance and two confirmed
  OBC false positives were fixed alongside it.

### Housekeeping

- [`cleanup_test_pollution.py`](platform/cleanup_test_pollution.py) — removes junk rows a test
  suite wrote into the production database. A `monkeypatch.setattr(..., raising=False)` targeted
  a method that doesn't exist, so the typo was silently ignored and 78 fake ERROR rows were
  written for booking IDs "A", "B" and "C".
- **Repo hygiene** — `.gitattributes` normalises line endings (a Windows round-trip was
  reporting unchanged files as fully rewritten), and `.gitignore` now *allowlists* booking-ID
  input lists rather than naming them one at a time.
- **10 new test modules** — NCL markets, OBC, payment rules, multi-line concurrency, GUI results
  table, preflight/file-load, MSC eligibility, and JS syntax.

<details>
<summary><strong>Earlier releases</strong></summary>

- **NCL brought online** — same-category reprice redesign, dialog-handling safety fixes, and
  corrected search/add-on selectors, plus
  [`run_ncl_live_check.py`](platform/run_ncl_live_check.py): a single *watched*, non-headless,
  one-booking live run that captures a full Playwright trace for replay.
- **Desktop GUI** — MSC wired in via `msc_live_service.py`, live login check, bulk queue
  editing, CSV/Excel export.
- **Cross-platform login tooling** — `save_login.py` / `clear_login.py` with OS-keychain
  storage and a Windows console paste fix.
- **Portal-onboarding helpers** — [`scraper/smart_locator.py`](platform/scraper/smart_locator.py)
  and [`discover_portal_fields.py`](platform/discover_portal_fields.py) rank candidate form
  fields on an unfamiliar portal by text similarity (RapidFuzz — deterministic, no AI/API key)
  to speed up writing a new adapter. Suggestions are drafting aids: verify against a real
  captured page before shipping a selector.
- **`analyze_history.py`** — read-only report over already-collected scan data, so scanning
  effort can be aimed at what actually pays off.
- **New tests** — NCL scraper, GoCCL, smart locator, and a production-hardening suite.

</details>

---

## API Endpoints (Platform)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/scan` | Submit booking IDs for scanning |
| `GET` | `/api/scan/{job_id}` | Poll scan status + results |
| `POST` | `/api/scan/stop` | Stop a running scan |
| `GET` | `/api/bookings` | List all checked bookings |
| `GET` | `/api/bookings/{id}/history` | Price history over time |
| `POST` | `/api/export/csv` | Export results as CSV |
| `GET` | `/api/health` | Health check |

---

## Supported Cruise Lines

| Cruise Line | Portal | Extension | Platform | GUI |
|-------------|--------|-----------|----------|-----|
| Royal Caribbean | ESPRESSO (CruisingPower) | ✅ | ✅ | ✅ |
| Celebrity Cruises | ESPRESSO (CruisingPower) | ✅ | ✅ | ✅ |
| Norwegian (NCL) | SeaWeb Agents (US + Canada/CAD) | ✅ | ✅ | ✅ |
| Carnival (GoCCL) | GoCCL | ✅ | ✅ | ✅ |
| MSC Cruises | MSC Book | — | ✅ | ✅ |
| Princess | POLAR | — | 🚧 in progress | — |

> **Princess is not usable yet.** What exists is the hard part of the domain research —
> [`core/princess_packages.py`](platform/core/princess_packages.py) maps the Plus/Premier
> fare packages so two Princess fares are never compared as if their package contents
> matched. There is no POLAR scraper, no `CruiseLine.PRINCESS`, and nothing imports the
> module yet. Treat it as groundwork, not a supported line.

> **NCL uses a separate agent account per market.** A Canadian booking checked against the US
> login reports "Reservation is not found" — it's on the other account, not missing. Set the
> market with `NclScraper(market="CA")` or `ncl_default_market`.

> **MSC never allows a direct in-portal reprice** — opportunities are surfaced for an agent
> to apply by phone. See [MSC: `confirm_and_proceed`](#-msc-confirm_and_proceed-and-the-read-only-check-flow)
> above and the [full reference](DOCUMENTATION.md#msc-cruises-reference).

---

## Documentation

- [`DOCUMENTATION.md`](DOCUMENTATION.md) — full technical reference: architecture, every
  selector/constant/function, business logic, storage schema, bug history, known open issues,
  and the MSC-specific reference
- [`RECREATE_PROMPT.md`](RECREATE_PROMPT.md) — a self-contained prompt that can rebuild the
  whole system from scratch
- [`SECURITY.md`](SECURITY.md) — what must never be committed, the three defences that
  enforce it, and the incident record behind each rule
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — guidelines for adding a new cruise line
- [`HOW_TO_CHECK_A_BOOKING.md`](HOW_TO_CHECK_A_BOOKING.md) — the plain-English manual process
  the ESPRESSO automation is based on

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Want to add a new cruise line?** Check the contributing guide — it's designed to be extensible.

### Areas Where You Can Help

- 🚢 **New cruise line adapters** (Princess, Holland America, Silversea, etc.)
- 🧪 **Testing** — unit tests for the calculator engine
- 🎨 **Extension UI** — dark mode, better UX
- 📊 **Dashboard** — React frontend for the API
- 🤖 **ML predictions** — price trend forecasting
- 📖 **Documentation** — tutorials, API examples

---

## Tech Stack

| Component | Extension | Platform |
|-----------|-----------|----------|
| Language | JavaScript | Python 3.11+ |
| Browser Automation | Chrome MV3 APIs | Playwright |
| Desktop UI | — | PySide6 + qasync |
| API | — | FastAPI |
| Database | chrome.storage | SQLAlchemy + SQLite |
| Credentials | — | keyring (OS secure store) |
| Logging | Console | structlog (JSON) |

---

## License

[MIT License](LICENSE) — use it, modify it, ship it.

---

## 💖 Support This Project

<div align="center">

If this project saved you time or money, donations are welcome:

[![Donate USDC on Solana](https://img.shields.io/badge/USDC-Solana-9945FF.svg?logo=solana&logoColor=white)](#-support-this-project)

**USDC (Solana):**

```
HqGsXodbkTRcMUwaP3fs1LQ9XJneKBGewwJPh4P5QVAH
```

</div>

---

<div align="center">

**Built for travel agents who want to save their clients money. ⚓**

</div>
