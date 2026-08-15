<div align="center">

# ⚓ Cruise Price Intelligence System (Playwright + AI Optimization)

**Automated repricing intelligence for Royal Caribbean, Celebrity, Norwegian, Carnival & MSC Cruises**

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
4. Log into your cruise portal (ESPRESSO, NCL SeaWeb, or GoCCL)
5. Click the extension icon → paste booking numbers → Run Check

### Python Platform

```bash
cd platform
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Start the API server (opens Swagger docs at /docs)
python main.py api

# Or run a CLI scan (--cruise-line: ESPRESSO, NCL, or GOCCL)
python main.py scan --bookings "1234567,7654321" --cruise-line ESPRESSO -o results.csv
```

MSC uses a separate, session-driven workflow rather than a one-shot CLI scan — see [`DOCUMENTATION.md`](DOCUMENTATION.md#msc-cruises-reference) and `platform/msc_session_controller.py`.

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
| Carnival (GoCCL) | GoCCL | ✅ | ✅ |
| MSC Cruises | MSC Book | — | ✅ |

> **MSC is architecturally different from the rest.** MSC never allows a direct in-portal reprice — the platform surfaces price-match, discount-add, and discount-tier-upgrade opportunities, but applying any of them requires an agent to call MSC by phone. See [`DOCUMENTATION.md`](DOCUMENTATION.md#msc-cruises-reference) for the full reference.

---

## Documentation

- [`DOCUMENTATION.md`](DOCUMENTATION.md) — full technical reference: architecture, every selector/constant/function, business logic, storage schema, bug history, known open issues, and the MSC-specific reference
- [`RECREATE_PROMPT.md`](RECREATE_PROMPT.md) — a self-contained prompt that can rebuild the whole system from scratch
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — guidelines for adding a new cruise line or otherwise contributing
- [`HOW_TO_CHECK_A_BOOKING.md`](HOW_TO_CHECK_A_BOOKING.md) — the plain-English manual process the ESPRESSO automation is based on

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
| API | — | FastAPI |
| Database | chrome.storage | SQLAlchemy + SQLite |
| Logging | Console | structlog (JSON) |

---

## License

[MIT License](LICENSE) — use it, modify it, ship it.

---

## 💖 Support This Project

<div align="center">

If this project saved you time or money, donations are welcome:

[![Donate with PayPal](https://img.shields.io/badge/PayPal-Donate-00457C.svg?logo=paypal&logoColor=white)](https://paypal.me/neonx07)
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
