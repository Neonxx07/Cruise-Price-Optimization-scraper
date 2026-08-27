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

> Booking numbers shown anywhere in this repo are **fake demo values**. Real reservation
> numbers, scan output, captured pages, and the local database are git-ignored by design —
> see [`.gitignore`](.gitignore).

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

## What's New

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
| Norwegian (NCL) | SeaWeb Agents | ✅ | ✅ | ✅ |
| Carnival (GoCCL) | GoCCL | ✅ | ✅ | ✅ |
| MSC Cruises | MSC Book | — | ✅ | ✅ |

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
