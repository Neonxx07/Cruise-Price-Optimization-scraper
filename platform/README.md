# Cruise Intelligence System

> Enterprise-grade repricing intelligence for Royal Caribbean, Celebrity, Norwegian,
> Carnival & MSC Cruises.

Evolved from the CruiseIntel Chrome Extension into a scalable, production-ready Python system.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI REST API                         │
│           /api/scan  /api/bookings  /api/export              │
├──────────────────────────────────┬─────────────────────────┤
│        Booking Service           │     Cache Service       │
│   (orchestration + persist)      │     (TTL-based)         │
├───────────────────┬──────────────┴─────────────────────────┤
│                     │                                        │
│  ┌─────────────┐   │   ┌──────────────┐                    │
│  │  ESPRESSO   │   │   │     NCL      │                    │
│  │  Scraper    │   │   │   Scraper    │  ← Playwright      │
│  └─────────────┘   │   └──────────────┘                    │
│                     │                                        │
├─────────────────────┴────────────────────────────────────────┤
│              Price Calculator + Confidence Scorer             │
│              (core business logic from extension)             │
├──────────────────────────────────────────────────────────────┤
│                SQLite / PostgreSQL Database                    │
│       bookings · price_history · scan_jobs · cache            │
└──────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

Create a `.env` file (optional — all settings have defaults):

```env
BROWSER_HEADLESS=true
BROWSER_USER_DATA_DIR=/path/to/chrome/profile
LOG_LEVEL=INFO
```

### 3. Run the API Server

```bash
python main.py api
# API docs at http://127.0.0.1:8000/docs
```

### 4. Run a CLI Scan

```bash
python main.py scan --bookings "4097990,64756965" --cruise-line ESPRESSO -o results.csv
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/scan` | Submit booking IDs for scanning |
| `GET` | `/api/scan/{job_id}` | Poll scan status + results |
| `POST` | `/api/scan/stop` | Stop a running scan |
| `GET` | `/api/bookings` | List all checked bookings |
| `GET` | `/api/bookings/{id}` | Booking detail |
| `GET` | `/api/bookings/{id}/history` | Price history |
| `POST` | `/api/export/csv` | Export results as CSV |
| `GET` | `/api/health` | Health check |

---

## Project Structure

**Packages**

```
core/        business logic — calculators, perk tiers, price scope,
             scan signatures, calculator versioning, models
scraper/     Playwright scrapers (ESPRESSO, NCL, GoCCL) + browser pool,
             smart locator, click verification
services/    orchestration, caching, exclusions, outcomes, exports,
             multi-line coordination, resource governor
gui/         PySide6 desktop app (windows, queue manager, monitor tabs)
api/         FastAPI server + routes
models/      SQLAlchemy database models
config/      Pydantic settings (env-based)
utils/       structured logging, retry
tests/       the suite — one module per behaviour, dated by discovery
docs/        see docs/README.md for the index
```

**Entry points** — run these from `platform/`

| | |
|---|---|
| `python -m gui.main` · `START_GUI.bat` | desktop app (the usual way in) |
| `python main.py scan …` · `python main.py api` | CLI and API server |
| `python easy_menu.py` · `START.bat` | console menu, no commands to type |
| `python scan_watchdog.py` | watch a run in progress from a second terminal |
| `python save_login.py` / `clear_login.py` | store or remove a portal login (OS keyring) |

**MSC session tooling** — MSC is driven by a long-lived browser session rather
than a one-shot scrape, so it has its own entry points: `msc_session_controller.py`
(holds the session), `msc_commands.py` (command dispatch), `msc_run_calculator.py`,
`msc_dedupe_data.py`, `record_msc_session.py`.

**Operational tools** — read-only unless stated

| | |
|---|---|
| `analyze_history.py` | report over already-collected scan data |
| `cross_line_audit.py` · `msc_audit.py` | audit stored results for scope mismatches |
| `run_health.py` | catch a run that has silently gone wrong, while it runs |
| `run_ncl_live_check.py` | one watched NCL booking, full Playwright trace |
| `run_persistent_watchlist_scan.py` | long-running watchlist scan |
| `check_bookings_now.py` | run the production scraper over a list |
| `discover_portal_fields.py` | rank candidate form fields on a new portal |
| `split_ncl_watchlist_by_account.py` | split a watchlist by NCL market account |
| `rebuild_export.py` · `cleanup_test_pollution.py` | recovery/remediation (writes) |

Local-only data — `data/`, `cruise_intel.db*`, `browser-profile/`, `reports/`
and any watchlist file — is git-ignored by design. See
[`SECURITY.md`](../SECURITY.md).

## Building a Standalone Executable

```bash
pip install pyinstaller
pyinstaller --onefile --name cruise-intel run.py
# Output: dist/cruise-intel (or dist/cruise-intel.exe on Windows)
```

> **Note:** Playwright requires browser binaries. For standalone distribution, set `BROWSER_USER_DATA_DIR` to use the system's installed Chrome.

---

## Scalability Roadmap

### Phase 1 — Local Tool (Current)
- SQLite database, single-user, CLI + API
- Runs on any machine with Python

### Phase 2 — Multi-User SaaS
- Swap SQLite → PostgreSQL
- Add user authentication (JWT / OAuth)
- Add a React dashboard frontend
- Deploy to AWS ECS / GCP Cloud Run

### Phase 3 — Cloud-Native Platform
- Move scrapers to AWS Lambda / Cloud Functions
- Add Redis for job queues and caching
- Celery for distributed task processing
- WebSocket for real-time scan progress

### Phase 4 — API Monetization
- Tiered API access (free/pro/enterprise)
- Rate limiting and API key management
- Stripe integration for billing
- Multi-tenant architecture

### Phase 5 — Intelligence Platform
- ML-based price prediction (linear regression → LSTM)
- Alerting system (email/Slack/webhook)
- Historical price analytics dashboard
- Cruise line coverage expansion

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Scraping | Playwright (async) |
| API | FastAPI |
| Database | SQLAlchemy 2.0 + SQLite/PostgreSQL |
| Logging | structlog (JSON) |
| Config | pydantic-settings |
| Packaging | PyInstaller |

---

## License

[MIT](../LICENSE), same as the rest of the repository.

> This line previously read "Proprietary — internal use only", which contradicted
> the MIT `LICENSE` at the repository root. The root licence governs.
