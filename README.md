<div align="center">

# ⚓ Cruise Price Optimization

**Automated repricing intelligence for Royal Caribbean, Celebrity & Norwegian Cruise Line**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Chrome Extension](https://img.shields.io/badge/Chrome-Extension-4285F4?logo=googlechrome&logoColor=white)]()
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)]()

</div>

---

## What Is This?

A **monorepo** containing two versions of the same cruise booking optimization tool:

| Project | Directory | Technology | Use Case |
|---------|-----------|------------|----------|
| 🧩 **Browser Extension** | [`extension/`](extension/) | JavaScript · Chrome MV3 | Quick checks from your browser |
| 🐍 **Python Platform** | [`platform/`](platform/) | Playwright · FastAPI · SQLAlchemy | Batch processing, API, automation |

Both share the **same core business logic** — detecting price drops, tracking package losses, calculating net savings, and scoring optimization confidence.

---

## How It Works

```
┌─────────────────────────────────────────────────────┐
│              Cruise Booking Portal                    │
│         (ESPRESSO / NCL SeaWeb)                       │
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
   Popup UI              REST API
                        + Database
                        + CSV Export
```

### Core Features

- ✅ **Price drop detection** — compares old vs new invoice totals
- ⚠️ **Trap detection** — catches price drops that lose packages (net loss)
- 📦 **Package tracking** — identifies lost/gained packages and their values
- ⭐ **Confidence scoring** — 1-5 star reliability rating per optimization
- 💳 **Paid-in-full detection** — skips bookings that can't be repriced
- 🔄 **Smart caching** — avoids rechecking recently-checked bookings
- 📋 **CSV export** — download results for reporting

---

## Quick Start

### Chrome Extension

1. Clone this repo
2. Open `chrome://extensions` → Enable **Developer Mode**
3. Click **Load Unpacked** → select the `extension/` folder
4. Log into your cruise portal (ESPRESSO or NCL SeaWeb)
5. Click the extension icon → paste booking numbers → Run Check

### Python Platform

```bash
cd platform
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Start the API server (opens Swagger docs at /docs)
python main.py api

# Or run a CLI scan
python main.py scan --bookings "4097990,64756965" --cruise-line ESPRESSO -o results.csv
```

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

| Cruise Line | Portal | Extension | Platform |
|-------------|--------|-----------|----------|
| Royal Caribbean | ESPRESSO (CruisingPower) | ✅ | ✅ |
| Celebrity Cruises | ESPRESSO (CruisingPower) | ✅ | ✅ |
| Norwegian (NCL) | SeaWeb Agents | ✅ | ✅ |

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Want to add a new cruise line?** Check the contributing guide — it's designed to be extensible.

### Areas Where You Can Help

- 🚢 **New cruise line adapters** (MSC, Carnival, Princess, etc.)
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
| API | — | FastAPI |
| Database | chrome.storage | SQLAlchemy + SQLite |
| Scheduling | chrome.alarms | APScheduler |
| Logging | Console | structlog (JSON) |

---

## License

[MIT License](LICENSE) — use it, modify it, ship it.

---

<div align="center">

**Built for travel agents who want to save their clients money. ⚓**

</div>
