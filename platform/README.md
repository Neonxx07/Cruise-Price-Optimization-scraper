# Cruise Intelligence System

> Enterprise-grade repricing intelligence for Royal Caribbean, Celebrity, Norwegian, Carnival & MSC Cruises.

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
│  ┌───────────┐ ┌─────────┐ ┌─────────┐ ┌──────────────┐    │
│  │ ESPRESSO  │ │   NCL   │ │  GoCCL  │ │  MSC (via    │    │
│  │  Scraper  │ │ Scraper │ │ Scraper │ │  session     │    │
│  │           │ │         │ │         │ │  controller) │    │
│  └───────────┘ └─────────┘ └─────────┘ └──────────────┘    │
│         all Playwright-driven; MSC uses a long-lived         │
│         browser session + command dispatch, not a            │
│         one-shot scrape (see DOCUMENTATION.md)                │
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
python main.py scan --bookings "1234567,7654321" --cruise-line ESPRESSO -o results.csv
```

### 5. MSC Cruises (separate workflow)

MSC never allows a direct in-portal reprice, so it isn't driven through `main.py scan` —
instead `msc_session_controller.py` keeps one long-lived browser session open and
`msc_commands.py` dispatches read-only lookup/check commands against it (see
`DOCUMENTATION.md`'s "MSC Cruises Reference" section for the full flow). Credentials
are stored via `msc_save_credentials.py` using the OS credential manager (`keyring`),
never in a file.

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

```
├── core/               # Business logic (calculator, calculator_msc, confidence, models)
├── scraper/            # Playwright scrapers (ESPRESSO, NCL, GoCCL)
├── msc_session_controller.py  # Long-lived MSC browser session
├── msc_commands.py            # MSC command dispatch (lookup/check/discount parsing)
├── msc_save_credentials.py / msc_clear_credentials.py  # OS credential manager setup
├── msc_run_calculator.py      # Offline reprocessing of captured MSC data
├── msc_dedupe_data.py         # Collapse captured MSC data to latest-per-booking
├── api/                # FastAPI server + routes
├── services/           # Orchestration, caching, CSV export
├── models/             # SQLAlchemy database models
├── utils/              # Retry, structured logging
├── config/             # Pydantic Settings (env-based)
├── main.py             # CLI entry point
├── run.py              # PyInstaller entry point
└── requirements.txt
```

---

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

Proprietary — Internal use only.
