# Rebuild Prompt: CruiseIntel Optimization Monorepo

You are building a complete monorepo from scratch. There is no existing source code available to you — this document is the entire specification. Read it in full before writing any code. Everything in this prompt is precise and load-bearing: exact selectors, exact constants, exact formulas, exact schemas. Do not approximate or "improve" values unless a section explicitly tells you to fix something. Where this document gives you literal strings, URLs, CSS selectors, JSON shapes, or numeric constants, reproduce them exactly.

## 0. Why this exists (read this first — it is the whole point of the system)

This is a monorepo for a cruise travel agency's internal tool. Travel agents book cruise cabins for clients on Royal Caribbean, Celebrity, Norwegian Cruise Line, Carnival, and MSC. After booking, prices can drop before final payment, sometimes enough that switching the client to a cheaper cabin category (or adding/upgrading a discount, for MSC) saves real money — but the decision isn't simple, because switching categories can also forfeit onboard credit (OBC) or bundled packages/perks the client already had, and MSC layers its own hard rules on top (paid-in-full, a final-payment-date cutoff, senior-discount eligibility, Voyagers Club enrollment status). This tool automates: logging into the cruise line's B2B agent portal, looking up a booking, reading its current price/perks/discounts, checking cheaper available categories or addable discounts, and calculating whether acting on one is a genuine net win once OBC/package losses and each cruise line's own hard rules are accounted for — flagging it for a human agent to actually execute (this tool never auto-submits a reprice or a discount change; a human always makes the final call and clicks "confirm," or places the phone call MSC's discount changes require).

There are four portals to support: Royal Caribbean/Celebrity's "ESPRESSO" B2B portal (cruisingpower.com, a Spring-Web-Flow-token-based classic web app with an Angular reprice modal — **this portal must NEVER run headless, at login or otherwise; this is a hardcoded, non-configurable rule, not a default**), Norwegian Cruise Line's "SeaWeb Agents" portal (seawebagents.ncl.com, a SilverStripe app using a `window.__preloaded_data` blob and a **virtualized** SlickGrid category picker, with a 30-minute edit-mode booking lock that MUST always be cancelled if the flow doesn't complete, to avoid leaving a client's real booking locked — and note NCL is a **SAME-CATEGORY reprice**, not a category-upgrade search, and the portal raises browser dialogs constantly, both of which are easy to get wrong: **see §5.2-CORRECTED before writing any NCL code**), Carnival Cruise Line's "GoCCL Navigator" portal (a category-upgrade-shaped problem like ESPRESSO/NCL, but structurally weaker — see §5B, it can only ever produce an unconfirmed candidate, never a fully confirmed opportunity), and MSC Cruises' own portal (a structurally different problem — price-match, discount-add, discount-tier-upgrade, and Voyagers-Club-selection, not a category-upgrade search — see §5A).

There are two parallel implementations, built deliberately in lockstep so a fix in one is mirrored in the other: a Chrome MV3 extension (JavaScript, `chrome.scripting.executeScript` DOM automation, for quick single-agent interactive use) and a Python/Playwright platform (headless-capable browser automation, SQLite persistence, CLI + optional FastAPI + optional PySide6 desktop GUI, for batch/scheduled/multi-agent use) — this parity applies fully to **ESPRESSO**; MSC and GoCCL are Python-platform-only today (MSC also has its own PySide6 GUI integration, §9.10) and do not have Chrome-extension adapters built for them yet. **NCL parity is currently BROKEN in the real system** (the extension still implements the disproven category-switching model — see §5.2-CORRECTED); in this rebuild, implement NCL correctly per §5.2-CORRECTED in **both** halves so the parity guarantee actually holds.

**Hard project-wide rule, non-negotiable: build NOTHING in this system that requires an LLM/AI model or an API key — not the live scan pipeline, and not even one-time, human-supervised tooling (e.g. onboarding a new cruise line's portal).** Every problem in this system that looks like a good fit for an LLM (fuzzy text matching, "read this messy text and tell me what it means," heuristically guessing what an unfamiliar page's fields are for) has a classical, deterministic solution instead — a real parsing library, fuzzy string similarity (e.g. RapidFuzz), frequent-pattern mining (e.g. `mlxtend`), ARIA-accessibility-tree snapshot diffing, or plain statistics. See §5A.7 (RapidFuzz rate-tab matching), §9.1 (ARIA structure-drift monitoring), and §9.11 (classical field-finder for new-portal onboarding) for concrete examples to follow the pattern of, not to treat as the only allowed instances of it.

Build both halves of this monorepo, in lockstep, matching the specification below exactly.

---

## 1. Tech stack

**Chrome extension** (`extension/`):
- Manifest V3, vanilla JavaScript, no build step, no frameworks, no bundler.
- `chrome.scripting.executeScript` for in-page DOM automation (MAIN world execution).
- `chrome.storage.local` / `chrome.storage.session` for persistence.
- Classic (non-module) service worker using `importScripts`.

**Python platform** (`platform/`):
- Python 3.11+ (GUI subsystem specifically documented against Python 3.14 in its own separate venv — see §9).
- Playwright (async API, Chromium only) for browser automation.
- Pydantic v2 for data models; pydantic-settings for config.
- SQLAlchemy 2.0 (async) + `aiosqlite` for persistence, default SQLite, swappable to Postgres via one connection-string setting.
- FastAPI + uvicorn for the optional REST API.
- structlog for structured logging.
- openpyxl for `.xlsx` export.
- PySide6 + qasync for the optional desktop GUI.
- PyInstaller for optional standalone `.exe` packaging.
- `keyring` for OS-native encrypted credential storage (Windows Credential Manager / macOS Keychain / Linux Secret Service) — never store a credential in a file or the database.
- `price-parser` and `dateparser` for robust text-to-price/date extraction out of messy captured page text (MSC especially) — prefer these over a hand-rolled regex/format-list ONLY when the source text is already isolated to a single value; see the caveat in §5A.4/§10 about NOT using this approach on text that mixes multiple unrelated numbers together.
- `rapidfuzz` for classical fuzzy string similarity (rate-tab matching fallback tier, §5A.7; new-portal field-finder, §9.11) — plain text-similarity scoring, not AI. See the project-wide no-AI rule in §0.
- `mlxtend` for classical frequent-itemset/association-rule mining (`analyze_history.py`, §9.11) — not machine learning.
- `pandas` as a support dependency for the above two.

---

## 2. Repo layout

Create this exact top-level structure (siblings, no shared package):

```
/
├── README.md                     (root readme: monorepo overview, quick starts for both halves)
├── CONTRIBUTING.md                (contribution guide incl. "adding a new cruise line" recipe)
├── extension/
│   ├── manifest.json
│   ├── background.js              (service worker — "Traffic Cop")
│   ├── popup.html
│   ├── popup.js
│   ├── calculator.js               (pure business-logic functions, no chrome.* calls)
│   ├── adapter_espresso.js         (ESPRESSO/cruisingpower.com DOM+fetch automation)
│   ├── adapter_ncl.js              (NCL/seawebagents.ncl.com DOM+JS-global automation)
│   └── icon.png                    (48px only)
└── platform/
    ├── main.py                     (CLI entry point: api / login / scan / watch subcommands)
    ├── run.py                      (PyInstaller entrypoint shim)
    ├── easy_menu.py                 (console menu wrapper around main.py's async functions)
    ├── requirements.txt
    ├── README.md
    ├── START.bat                    (CLI/menu launcher, local venv)
    ├── START_GUI.bat                (GUI launcher, external C:\cruisevenv\venv)
    ├── __init__.py
    ├── core/
    │   ├── __init__.py
    │   ├── models.py                (Pydantic: BookingResult, BookingStatus, CruiseLine, ScanJob, ScanJobStatus; also MSC's OWN MscBookingResult/MscCheck/MscOpportunityType/MscCheckStatus — see §5A.0, deliberately not squeezed into BookingResult)
    │   ├── calculator.py             (calculate_espresso, calculate_ncl, calculate_goccl, sentinel-result factories)
    │   ├── calculator_msc.py         (evaluate_msc_booking + the four independent MSC checks — see §5A)
    │   └── confidence.py             (calc_confidence + ConfidenceResult dataclass)
    ├── scraper/
    │   ├── __init__.py
    │   ├── base.py                   (BaseScraper: Playwright lifecycle, action log, snapshots, ARIA structure-drift monitoring — §9.1)
    │   ├── espresso.py                (EspressoScraper — force-overrides headless=False always, see §11)
    │   ├── ncl.py                    (NclScraper)
    │   ├── goccl.py                   (GoCCLScraper — Carnival, §5B)
    │   └── smart_locator.py            (classical, non-AI field-finder for new-portal onboarding — §9.11)
    ├── services/
    │   ├── __init__.py
    │   ├── booking_service.py         (BookingService — ESPRESSO/NCL/GoCCL orchestration)
    │   ├── cache_service.py            (CacheService — TTL cache)
    │   ├── csv_export.py               (export_results_csv)
    │   ├── excel_export.py             (export_results_excel)
    │   └── msc_live_service.py          (MscLiveService — MSC's own GUI-facing session driver, §9.10; MUST importlib.reload() its command module per the incident described there)
    ├── msc_commands.py                 (MSC's command-file-protocol automation: auto_login, check_booking/check_booking_batch/check_booking_batch2, rate-tab matching, occupancy correction — see §5A in full)
    ├── msc_session_controller.py        (console-driven long-running controller for msc_commands.py, re-imports it fresh per command)
    ├── save_login.py / clear_login.py    (credential storage for MSC + ESPRESSO — §9.11)
    ├── discover_portal_fields.py         (interactive CLI wrapper around scraper/smart_locator.py — §9.11)
    ├── analyze_history.py                (read-only scan-history analysis + classical pattern mining — §9.11)
    ├── models/
    │   ├── __init__.py
    │   └── database.py                (SQLAlchemy: BookingRecord, PriceHistory, ScanJobRecord, CacheEntry, MarketDataRecord)
    ├── api/
    │   ├── __init__.py
    │   ├── main.py                     (FastAPI app factory + lifespan)
    │   ├── routes.py                    (all /api/* endpoints)
    │   └── schemas.py                   (Pydantic request/response models)
    ├── config/
    │   ├── __init__.py
    │   └── settings.py                  (pydantic-settings Settings, singleton `settings`)
    ├── utils/
    │   ├── __init__.py
    │   ├── logging.py                    (structlog setup_logging/get_logger)
    │   └── retry.py                       (retry_async)
    └── gui/
        ├── __init__.py
        ├── main.py                        (PySide6 app entrypoint, qasync event loop)
        ├── windows.py                      (MainWindow — the only widget class)
        ├── queue_manager.py                 (BookingQueueManager — GUI scan driver)
        └── scan_adapter.py                   (GuiScanAdapter — export-only wrapper)
```

---

## 3. Business logic spec — reproduce these exactly, in both JS (`calculator.js`) and Python (`core/calculator.py` + `core/confidence.py`)

This is the single most important section. The Chrome extension and Python platform must compute byte-identical results given identical inputs.

### 3.1 Enums

`CruiseLine`: `ESPRESSO`, `NCL`, `GOCCL`. (MSC is tracked the same way for scraper/service wiring purposes, but its RESULT shape is deliberately NOT `BookingResult` — see §5A.0 for why, and use `core/calculator_msc.py`'s own `MscBookingResult`/`MscCheck`/`MscOpportunityType`/`MscCheckStatus` models instead.)

`BookingStatus` — exactly 7 members, in this declared order:
```
OPTIMIZATION, TRAP, NO_SAVING, ERROR, WLT, PAID_IN_FULL, SKIPPED_TODAY
```
(The extension additionally has a transient UI-only `CHECKING` status used solely for in-progress popup cards — it is never a value calculator.js/calculator.py itself produces.)

`ScanJobStatus` (Python only, for batch job tracking): `PENDING, RUNNING, COMPLETED, FAILED, STOPPED`.

### 3.2 `BookingResult` — the canonical output shape (Python Pydantic model; JS object with equivalent camelCase keys)

| Field (Python snake_case / JS camelCase) | Type | Default | Meaning |
|---|---|---|---|
| `cruise_line` / `cruiseLine` | enum | required | ESPRESSO or NCL |
| `status` | enum | required | one of the 7 statuses |
| `note` | str | `""` | human-readable one-line summary |
| `error` | str\|None | `None` | exception message when status==ERROR |
| `booking_id` / `bookingId` | str | required | reservation ID |
| `price_category` / `priceCategory` | str\|None | `None` | current price category code |
| `new_price_category` / `newPriceCategory` | str\|None | `None` | category selected as cheaper |
| `old_total` / `oldTotal` | float | `0.0` | invoice total before reprice |
| `new_total` / `newTotal` | float | `0.0` | invoice total after hypothetical reprice |
| `price_drop` / `priceDrop` | float | `0.0` | `old_total - new_total` |
| `obc_change` / `obcChange` | float | `0.0` | new OBC minus old OBC (ESPRESSO only; always 0.0 for NCL) |
| `net_saving` / `netSaving` | float | `0.0` | final net figure |
| `lost_pkg_value` / `lostPkgValue` | float | `0.0` | dollar value of forfeited packages/addons |
| `lost_pkg_names` / `lostPkgNames` | list[str] | `[]` | names of lost packages/addons |
| `lost_fares` / `lostFares` | list[str] | `[]` | fares lost, judged NOT re-addable (ESPRESSO) |
| `re_addable_fares` / `reAddableFares` | list[str] | `[]` | fares lost but heuristically re-addable (ESPRESSO) |
| `gained_fares` / `gainedFares` | list[str] | `[]` | new fares present after reprice, absent before (ESPRESSO) |
| `confidence` | int | `0` | 1-5 star score |
| `old_cruise_fare` / `oldCruiseFare` | float | `0.0` | cruise-fare-only line, old invoice |
| `new_cruise_fare` / `newCruiseFare` | float | `0.0` | cruise-fare-only line, new invoice |
| `fare_change_pct` / `fareChangePct` | float | `0.0` | `(new-old)/old * 100`, rounded 2dp |
| `checked_at` / `checkedAt` | datetime | now (Python only) | timestamp of analysis |

Python: `model_config = {"from_attributes": True}`. Also implement `ScanJob` (Python only): `job_id, booking_ids, cruise_line, status=PENDING, results=[], progress_done=0, progress_total=0, current_booking_id=None, started_at=None, completed_at=None`.

### 3.3 Shared numeric helpers

```
safe_float(v): float(v), NaN -> 0.0, TypeError/ValueError -> 0.0
round2(x): round(safe_float(x) * 100) / 100          # NOT Decimal rounding — plain round()
norm_str(s): (s or "").strip().upper()
```

### 3.4 ESPRESSO fee/package classification

```python
ESPRESSO_FEE_TYPES = frozenset([
    "VACATION_TOTAL", "OBC_TOTAL", "PORT_CHARGE", "PORT_EXPENSES",
    "GOVERNMENT_TAX", "TAXES_AND_FEES", "NCF", "NCCF", "CRUISE",
    "CRUISEFARE", "GRATUITIES", "TAX", "FEE",
])
_FEE_NAME_PREFIX_RE = r"^(NCCF|NCF|PORT|TAX|FEE|GOVERNMENT|GRATUIT)"
```

`is_espresso_fee(item)`: true if ANY of:
1. `norm_str(item.type)` is in `ESPRESSO_FEE_TYPES`.
2. `norm_str(item.name or item.normalizedName)` matches `_FEE_NAME_PREFIX_RE` at the start.
3. that same normalized name contains `" OBC"`, or ends with `"OBC"`, or starts with `"OBC "`.

`get_total(items, fee_type)`: `safe_float(amount)` of the first item where `item.paxId == "total"` AND `norm_str(item.type) == fee_type`; else `0.0`.

`get_cruise_fare(items)`:
- Pass 1: first item with `paxId == "total"` and RAW (case-sensitive, not normalized) `type` in `("CRUISE", "CRUISEFARE", "cruise")` → return its amount.
- Pass 2 (fallback heuristic): among `paxId == "total"` items whose `norm_str(type)` is NOT in `skip = {"VACATION_TOTAL","OBC_TOTAL","TAXES_AND_FEES","PORT_CHARGE","PORT_EXPENSES","GOVERNMENT_TAX","NCF","NCCF"}`, return the largest `amount` found (init `best=0.0`, keep only if `amount > best`).

`get_packages(items)`: items where `paxId == "total"` AND `safe_float(amount) > 0` AND NOT `is_espresso_fee(item)`.

### 3.5 Re-addable fare heuristic (exact regex set)

```python
_READDABLE_PATTERNS = [r"email", r"bonus", r"promo", r"loyalty", r"coupon"]  # all case-insensitive
```
`is_re_addable(fare_name)`: true if ANY pattern matches (search, not fullmatch) anywhere in the name. A lost fare matching one of these is bucketed as "re-addable" (agent can typically reapply it) rather than "truly lost".

### 3.6 NCL addon value table

```python
NCL_ADDON_VALUES = {
    "wi-fi": 150, "wifi": 150, "internet": 150,
    "dining": 80, "specialty dining": 80, "restaurant": 80,
    "beverage": 200, "bar": 200, "drink": 200, "open bar": 200,
    "excursion": 50, "shore": 50,
}
_DOLLAR_PATTERN = r"\$(\d+)"
```
`ncl_addon_value(addon_name)`: lowercase the name; if it contains a literal `$NNN` pattern, use that parsed integer (wins over the table); else, iterating the table in insertion order, return the first entry whose key is a substring of the lowercased name; else `0`.

### 3.7 THE core net-saving formula (ESPRESSO)

```
net_saving = price_drop + obc_change - lost_pkg_value
```
where `price_drop = round2(old_total - new_total)`, `obc_change = round2(new_obc - old_obc)` (negative = OBC was reduced/lost), `lost_pkg_value = round2(sum of amounts of packages present in old invoice, normalized-name-absent from new invoice)`. `old_total`/`new_total` come from `get_total(items, "VACATION_TOTAL")`; `old_obc`/`new_obc` from `get_total(items, "OBC_TOTAL")`.

Fare-name diffing (separately from packages): `old_fare_names`/`new_fare_names` come from `data.oldFares`/`data.newFares` arrays (`.name` field, only where truthy). `all_lost_fares` = old fare names whose normalized form is absent from the new-fare normalized-name set. Split into `re_addable_fares` (matches §3.5) and `lost_fares` (does not — the "truly lost" bucket). `gained_fares` = new fare names whose normalized form is absent from the old-fare normalized-name set.

`re_add_note = " — re-add: " + ", ".join(re_addable_fares)` if any exist, else `""`.

### 3.8 `OBC_LOSS_MIN_RATIO = 3.0`

Comment to preserve verbatim in code: *"Minimum ratio of (price drop) to (OBC lost) before a repricing that forfeits OBC is treated as a genuine optimization rather than a wash."*

### 3.9 ESPRESSO status classification — EXACT branch order (first match wins)

```python
if net > 0 and lost_pkg_value > 0 and net < lost_pkg_value:
    status = TRAP
    note = f"trap - losing ${round(lost_pkg_value)} perk for only ${round(net)} net{re_add_note}"

elif net > 0 and obc_change < 0 and price_drop < abs(obc_change) * OBC_LOSS_MIN_RATIO:
    status = NO_SAVING
    note = f"no saving — ${round(price_drop)} drop costs ${round(abs(obc_change))} OBC (need {OBC_LOSS_MIN_RATIO:.0f}x){re_add_note}"

elif net > 0:
    status = OPTIMIZATION
    note = f"optimized ${round(net)}{re_add_note}"

elif price_drop > 0 and net <= 0:
    status = TRAP
    note = f"trap - do not reprice{re_add_note}"

else:
    status = NO_SAVING
    extra = " — can re-add: " + ", ".join(re_addable_fares) if re_addable_fares else ""
    note = f"no saving{extra}"
```

Rationale (preserve as code comments, verbatim):
- Branch 1 (perk trap): *"Net saving is positive on paper, but it's smaller than the value of a package being given up to get it — the client is trading a perk worth more than the 'win' itself. Confirmed against a real case: $50 net saving from losing a $594 all-inclusive drink package is not a real optimization."*
- Branch 2 (OBC trap → downgrades to NO_SAVING): *"Net is positive on paper, but a chunk of it is OBC being forfeited rather than a real fare reduction — confirmed against a real case: a $300 price drop that cost $250 of OBC (net $50) is only a ~1.2x margin, not a safe trade. Only worth recommending once the price drop clears the OBC being given up by OBC_LOSS_MIN_RATIO."* This is the OBC_LOSS_MIN_RATIO downgrade rule: an OPTIMIZATION-shaped result (net>0) gets explicitly downgraded to NO_SAVING whenever `price_drop < abs(obc_change) * 3.0`.

Whole function wrapped in try/except → on any exception, return `BookingResult(cruise_line=ESPRESSO, status=ERROR, error=str(e), booking_id=booking_id, price_category=price_category)`.

After classification: `old_cruise = get_cruise_fare(old_items)`, `new_cruise = get_cruise_fare(new_items)`, `conf = calc_confidence(old_cruise, new_cruise, net, old_total, lost_pkg_value, obc_change)` (§3.11). Populate `confidence=conf.score`, `old_cruise_fare`, `new_cruise_fare`, `fare_change_pct=conf.fare_change_pct` on the returned `BookingResult`.

Function signature: `calculate_espresso(raw_data: dict, booking_id: str, price_category: str | None = None) -> BookingResult`. First line of body: `data = raw_data.get("result", raw_data)` (unwrap optional `"result"` envelope from the API response). Then `old_items = (data.get("oldInvoice") or {}).get("invoiceItems", [])`, same pattern for `new_items`/`"newInvoice"`.

### 3.10 NCL calculation — DIFFERENT (narrower) net formula

Signature: `calculate_ncl(booking_id, price_category, invoice_total, new_res_total, addons=None, old_promos="", new_promos="") -> BookingResult`.

```python
old_total = round2(invoice_total)
new_total = round2(new_res_total)
price_drop = round2(old_total - new_total)
lost_fobc = "FOBC" in old_promos.upper() and "FOBC" not in new_promos.upper()
```
For each unique addon (de-duped by `name`, preserving first-occurrence order): `is_obc_cert = bool(re.search(r"On-Board Credit Certificate", name, re.I) or re.search(r"OBC Certificate", name, re.I))`. If `is_obc_cert and lost_fobc`: `val = ncl_addon_value(name)`; if `val > 0`, add to `lost_addon_value` and append `f"{name} (${val})"` to `lost_addon_names`.

Important narrowness note to preserve: only OBC-certificate-named addons ever count toward loss, and only if the FOBC promo code itself was also lost — wifi/dining/beverage/excursion addons are priced by `ncl_addon_value` but never actually subtracted unless they ALSO match the OBC-certificate name regex. Do not "fix" this into a broader loss model — it is how the original behaves and both implementations must match it.

```python
lost_addon_value = round2(lost_addon_value)
net = round2(price_drop - lost_addon_value)   # NCL's net formula — no OBC term, unlike ESPRESSO
```

Classification:
```python
if net > 0:
    status = OPTIMIZATION
    addon_note = " — verify addons: " + ", ".join(lost_addon_names) if lost_addon_names else ""
    note = f"NCL optimized ${round(net)}{addon_note}"
elif price_drop > 0 and net <= 0:
    status = TRAP
    note = f"NCL trap — price drop offset by addon loss: {', '.join(lost_addon_names)}"
else:
    status = NO_SAVING
    note = "NCL no saving"
```

NCL confidence is a SEPARATE, simpler heuristic — NOT `calc_confidence`:
```python
if price_drop > 0 and lost_addon_value == 0:            confidence = 5
elif price_drop > 0 and lost_addon_value < price_drop:   confidence = 4
elif price_drop > 0 and lost_addon_value >= price_drop:  confidence = 2
else:                                                     confidence = 2
```
Return `obc_change=0.0` always for NCL. `new_price_category` is NOT set inside `calculate_ncl` — the caller (scraper) sets `result.new_price_category = target_category` after the fact. Whole function wrapped in try/except → same ERROR-result fallback pattern (cruise_line=NCL).

### 3.11 Confidence scoring — `calc_confidence` (ESPRESSO only)

```python
@dataclass
class ConfidenceResult:
    score: int
    fare_change_pct: float
    old_cruise_fare: float
    new_cruise_fare: float

def calc_confidence(old_cruise_fare, new_cruise_fare, net_saving, old_total, lost_pkg_value, obc_change) -> ConfidenceResult:
```
Entire body in try/except → fallback `ConfidenceResult(score=3, fare_change_pct=0.0, old_cruise_fare=0.0, new_cruise_fare=0.0)`.

1. `fare_change_pct = (new_cruise_fare - old_cruise_fare) / old_cruise_fare if old_cruise_fare > 0 else 0.0` (raw ratio, NOT yet ×100).
2. `net_pct = net_saving / old_total if old_total > 0 else 0.0`.
3. `pts = 0`:
   - Fare direction (mutually exclusive if/elif chain):
     - `fare_change_pct < -0.02` → `pts += 2`
     - `elif fare_change_pct < 0` → `pts += 1`
     - `elif fare_change_pct > 0.15` → `pts -= 2`
     - `elif fare_change_pct > 0.05` → `pts -= 1`
     - (exactly 0, or in `(0, 0.05]` → no adjustment)
   - Net saving impact:
     - `net_pct > 0.05` → `pts += 2`
     - `elif net_pct > 0.02` → `pts += 1`
   - Package/OBC stability (independent checks, not elif):
     - `if lost_pkg_value <= 0: pts += 1`
     - `if obc_change >= 0: pts += 1`
4. Points → stars lookup:
```python
pts_to_stars = {-2: 1, -1: 1, 0: 2, 1: 2, 2: 2, 3: 3, 4: 4, 5: 5, 6: 5}
clamped = max(-2, min(6, pts))
score = pts_to_stars.get(clamped, 3)
```
5. Safety caps, applied AFTER the lookup, in order:
   - `if fare_change_pct >= 0.05 and score > 3: score = 3`
   - `if fare_change_pct > 0.10 and lost_pkg_value > 0: score = min(score, 2)`
6. Return `ConfidenceResult(score=score, fare_change_pct=round(fare_change_pct * 100, 2), old_cruise_fare=old_cruise_fare, new_cruise_fare=new_cruise_fare)` — note the RETURNED `fare_change_pct` is scaled ×100 and rounded 2dp (percentage points), while ALL internal comparisons above use the raw fraction.

### 3.12 Sentinel-result factory functions (implement all of these, both languages)

- `make_wlt_result(booking_id, price_category, cruise_line)`: `status=WLT`, `note="WLT - waitlisted"`, all money fields 0.
- `make_paid_in_full_result(booking_id, price_category, cruise_line, old_total=0)`: `status=PAID_IN_FULL`, `note="💳 Fully paid — repricing unavailable"`, `old_total=old_total`, rest 0.
- `make_no_price_change_result(booking_id, price_category, cruise_line, price=0)`: `status=NO_SAVING`, `note=f"no saving — price unchanged (${price:,.2f})"`, `old_total=price, new_total=price`. Docstring to preserve verbatim: *"The category's price-quote total exactly matches the current price — confirmed via the page's own displayed price (sb.summary.price.price vs sb.summary.price.allocationPrice), not the reprice-modal API, which returns a short, non-JSON body in exactly this scenario and was previously misdiagnosed downstream as an expired token."*
- `make_skip_reprice_result(booking_id, price_category, cruise_line)`: `status=NO_SAVING`, `note="Booking restriction — price program change not allowed"`. Docstring verbatim: *"ESPRESSO's API explicitly returned skipRepriceModal — a deliberate 'this booking has a restriction that blocks repricing' response, not an error (confirmed against the portal's own 'Booking Restriction: Changing price pgm is not allowed' message). No point retrying."*
- `make_skipped_result(booking_id, price_category, cruise_line, hours_ago)`: `h = round(hours_ago, 1)`; `status=SKIPPED_TODAY`, `note=f"Checked {h}h ago — no saving cached"`.
- `make_error_result(booking_id, price_category, cruise_line, error_msg)`: `status=ERROR`, `note=error_msg`, `error=error_msg`.

---

## 4. ESPRESSO automation flow spec

### 4.0 SAFETY BOUNDARY — reproduce this constraint literally, as a comment at the top of both `adapter_espresso.js` and `scraper/espresso.py`

> *"This scraper must never interact with `#repriceModalAcceptBtn1` / `#repriceModalAcceptBtn2` ('Continue with New Rate') or any other control that commits a new rate to a live booking. That is confirmed (directly from the portal's own markup) to be the actual save action. Everything this file does — including the direct `showRepriceModalCheck` fetch call — stops at reading the Rate Comparison data (old/new invoice, OBC, offers). The equivalent of clicking 'Continue' on the categories page (`#submitToContinue`, `_eventId=saveCategories`) is simulated read-only via the allocate fetch call; the equivalent of clicking 'Continue with New Rate' must never be added here. That step is reserved for a human, in the real portal, permanently."*

### 4.1 Selector constants (Mantine-migration fallback — implement both, comma-joined `querySelector`/Playwright selector)

```
SEARCH_INPUT_SELECTOR  = '#reservationid, [data-qa="secure.espresso.input.reservation.search"]'
SEARCH_BUTTON_SELECTOR = '#searchReservationBtn, [aria-label="Search by Reservation ID, Name or Date"]'
```
Comment to preserve: *"ESPRESSO's reservation search box was rebuilt on Mantine at some point — the old plain #reservationid input/#searchReservationBtn button no longer exist on the redesigned page. Mantine assigns a fresh autogenerated id per render, so the stable hook is the data-qa attribute, not an id. Both selectors are tried together (old first) so this keeps working if either version of the page is ever served."*

### 4.2 URLs

```
ESPRESSO_HOME_URL = "https://secure.cruisingpower.com/home"
ESPRESSO_BASE_URL = "https://secure.cruisingpower.com/espresso/protected/reservations.do"
```

### 4.3 Login check

`is logged in` iff current URL contains `cruisingpower.com` AND does NOT contain `login` or `signin`. If URL contains `login`/`signin` → not logged in, raise/return an explicit "Not logged in — please log into ESPRESSO first" error.

### 4.4 Full per-booking flow, in exact order

1. **Navigate home first, not directly to reservations.** Comment to preserve: *"Go through the portal home page first, the same path a human takes right after login — deep-linking straight to reservations.do skips whatever session/flow initialization /home does, and appears to be what was causing the forced logouts and desynced execution tokens seen during testing."* Navigate to `ESPRESSO_HOME_URL`, check login, then navigate to `ESPRESSO_BASE_URL`, check login again.
2. **Search**: wait for `SEARCH_INPUT_SELECTOR` (use a long, dedicated timeout here — 60000ms — because login may still be mid-OAuth-redirect-chain). Clear the field, fill it with the booking ID, click `SEARCH_BUTTON_SELECTOR`. Wait for `#sideBar, [id*="sideBar"]` (timeout 15000ms).
3. **Read price category**: primary source is hidden field `#currentPriceCat` (`.value.trim()`); if empty/absent, fall back in order to `#groupInfoBlock > section.category.borderRight > div.priceCategory > span.value.ng-binding`, then `[class*="priceCategory"] [class*="value"]`, then `.priceCategory .value`. Poll for up to `scraper_category_poll_timeout_ms` (default 8000ms), because the value can briefly be an unrendered Angular template placeholder (regex `^\{\{.*\}\}$`) before client-side templating finishes — do not accept a value matching that placeholder pattern; keep polling every 200ms until a real value appears or the deadline passes.

**Group bookings need no special-casing — confirmed against real captured data.** A booking whose page title reads "Group Booking Summary" instead of "Reservation Summary" (i.e. a group reservation) uses this exact same flow with no changes: `#currentPriceCat`, the "Categories" link, and the category table all behave identically. The only observed difference is cosmetic: the top-of-page reservation-status widget's Angular binding can fail to render in a scraped snapshot (literal `{{sb.reservation.status...}}` template text instead of a resolved value) while the rest of the page — price, category, guests — renders normally; that widget is display-only and nothing in this flow reads it, so it's safe to ignore. An early batch run's failure on two group bookings turned out to be an unrelated transient `Failed to fetch` network error on the reprice API call, not a group-booking incompatibility — confirmed by re-running the identical flow against the same two bookings and getting clean `skip_reprice`/`WLT` results.
4. **Click Categories**: find an `<a>` whose exact trimmed text is `"Categories"`; fallback `#sideBar a[href*="catAvail"]`; fallback `a[href*="categor"]`. Click it. Wait for `#catAvailCategoryList, [id*="catAvail"]` (timeout 12000ms).
5. **WLT check — MUST run only after the categories table has loaded, never before.** For the row whose `td.c1 div.categoryIcon span, .categoryIcon span` text equals the current price category, read `td.c2.rooms .svCabin .status, .svCabin .status` text; if it equals exactly `"WLT"`, short-circuit the whole flow and return `make_wlt_result(...)` — do not throw, do not retry.
6. **Select the category radio + read execution token/selectionJSON**: parse the execution token from the URL via regex `/execution=(e\d+s\d+)/`. Read the `input.selectionJSON, input[name*="selectionJSON"]` value as a baseline. Find the category row matching the current price category; find its radio input (`input[name="rbCategorySelection"][data-columnindex="0"]`, fallback any `input[type="radio"]` in that row); set `.checked = true`, dispatch `mousedown`, `mouseup`, `.click()`, a synthetic `change` event, and an `input` event (all `bubbles:true`) — this thoroughness is deliberate, to trigger every possible Angular/jQuery listener style. Then poll every 100ms up to 2000ms for the `selectionJSON` field's value to change from the baseline AND not equal `"[]"`. Add one more fixed 150ms settle delay after the poll resolves. Read the final `selectionJSON` value and the radio's value.
7. **Execute the two real API calls** (see §4.5 below) using the token, selectionJSON, and radio value.
8. **THEN, and only then**, if the response is short (`< 300` chars) or not-ok, run the paid-status check and/or the displayed-price check (§4.6) to disambiguate what a short/failed response means — see the critical constraint in §4.7.
9. If the response is a normal-length, parseable JSON body → call `calculate_espresso(data, booking_id, price_category)`.

### 4.5 The two real API calls (exact endpoints, exact bodies, exact headers)

**Call 1 — Allocate** (read-only equivalent of clicking "Continue" / `#submitToContinue` on the categories page):
- `POST /espresso/protected/reservations.do?execution=<token>&_eventId=allocate&ajaxSource=true`
- Body (URL-encoded form, `URLSearchParams`): `columnSelection=on&rbCategorySelection=<radio>&_eventId=saveCategories&categorySingleViewFormModel.selectionJSON=<selectionJSON>`
- Headers: `Content-Type: application/x-www-form-urlencoded; charset=UTF-8`, `X-Requested-With: XMLHttpRequest`
- `credentials: 'include'`
- If not ok → `{ok:false, error:'Allocate HTTP <status>'}`.
- Fixed 300ms delay before the next call.

**Call 2 — Reprice check**:
- `POST /espresso/protected/repriceModalController.do/showRepriceModalCheck?execution=<token>`
- Body: literal string `execution=<token>` (NOT URL-encoded form data — a raw body).
- Headers: `Content-Type: application/x-www-form-urlencoded`, `X-Requested-With: XMLHttpRequest`, `Accept: application/json, text/javascript, */*`
- `credentials: 'include'`
- If not ok → `{ok:false, error:'Reprice HTTP <status>'}`.
- Read response text; try `JSON.parse`; on success → `{ok:true, data:parsed, dataLength:text.length}`; on parse failure → `{ok:false, error:'Not JSON: ' + text.substring(0,200)}`.

Both calls must run inside the page's own JS context (`page.evaluate` / MAIN-world `chrome.scripting.executeScript`) using the page's own `fetch()`, not a Python/Node HTTP client, so that session cookies and Referer/Origin are correct.

Special response value: if the parsed JSON has `data.key == "skipRepriceModal"`, treat this as a deliberate non-error signal — return `make_skip_reprice_result(...)`.

### 4.6 Displayed-price fallback check (`read_top_prices` / `fn_espresso_readTopPrices`)

Selector: `a.viewPriceQuoteLink.fit`. Disambiguate the two matching links by inspecting each one's `ng-show` attribute: the one containing `sb.summary.price.price` is the current price; the one containing `sb.summary.price.allocationPrice` is the newly-selected-category price. Parse each by stripping all non-digit/`.`/`-` characters and `parseFloat`; `NaN` → `null`.

Paid-status check (`check_paid_status`): try, in order — (1) `[class*="totalPrice"] .amount, .total-price .amount, #totalPrice` vs `[class*="paymentsReceived"] .amount, .payments-received .amount, #paymentsReceived`, numeric compare `total>0 && paid>=total`; (2) body-text regex `/paid\s+in\s+full/i` fallback returning `totalPrice: 0` (amount unknown).

### 4.7 CRITICAL CONSTRAINT — the price-check-based diagnostics must run only AFTER the real API calls, never before

State this explicitly as a hard rule in the code (comment) and in your own execution: **never call `read_top_prices`/`check_paid_status` before the allocate + showRepriceModalCheck fetch calls have both completed.** Reasoning to preserve verbatim: *raw `fetch()` calls made directly against these endpoints bypass Angular's own model/scope update cycle — the displayed price on the page is driven by Angular bindings that only refresh once Angular itself processes a response, which these raw fetches don't trigger. Reading the displayed price BEFORE the real API calls run always returns stale (pre-reprice) data, which will make `currentPrice` and `allocationPrice` appear equal even when they are not, producing a false "no price change" verdict that masks a real optimization.* This exact regression happened once during the original system's development and must not be reintroduced: only treat a short/ambiguous API response's price-check confirmation as authoritative because it runs strictly after Call 1 and Call 2 above, never as a pre-check shortcut.

The exact short-response handling order, once both API calls have returned:
1. If `dataLength < 300` (or the call failed): check paid-status first. If paid, return `make_paid_in_full_result(...)` using the total detected on the page.
2. Else (still short, not paid): call `read_top_prices()`. If both `currentPrice` and `allocationPrice` are non-null and `abs(current - alloc) < 0.01`, return `make_no_price_change_result(booking_id, price_category, ESPRESSO, current_price)`.
3. Else if the API call was not ok → raise/throw with the API's error message (feeds into the retry loop below, which re-navigates for a fresh execution token).
4. Else (short but claims ok:true, genuinely puzzling) → raise/throw `"API returned only {n} chars — body: {body}"`.
5. If `dataLength >= 300` and ok → parse normally and run `calculate_espresso`.

### 4.8 Retry wrapper

Wrap the entire per-booking attempt (steps in §4.4) in a retry helper: **3 attempts, 3000ms delay** between attempts (Python: exponential backoff ×1.5 on top — 3.0s, then 4.5s; the JS extension uses a flat 3000ms, not exponential — implement each language's historical behavior respectively). Each retry attempt re-navigates fully from scratch (fresh Spring Web Flow execution token, since tokens are single-use). Catch broad `Exception`; on the final attempt's failure, re-raise/reject with the last error.

---

## 5. NCL automation flow spec

> **⚠ READ §5.2-CORRECTED FIRST.** Everything in §5.2 steps 8-11 below describes a model that was PROVEN WRONG against the real portal on 2026-08-26. §5.2-CORRECTED supersedes it. The rest of §5 (the lock, the URL, steps 1-7) is confirmed correct and still applies.

### 5.2-CORRECTED  SUPERSEDING CORRECTION — NCL is a SAME-CATEGORY reprice, and the portal is dialog-heavy

Confirmed 2026-08-26, both by the project owner directly and by a real recorded portal session plus repeated live runs that reproduced four independently-known expected results exactly. **Build this, not §5.2 steps 8-11.**

**1. The comparison axis.** NCL's real opportunity is *"has the price of the category this booking is ALREADY in dropped since it was booked."* It is NOT "find a cheaper different category and switch into it." Do not implement a cross-category search, a "cheapest but not absolute cheapest" sort, or any category-switching selection logic — that was the original mistake. Correct steps 8-11:

8. Read the category grid data model (`window.VX.get('_form_12')`) as in step 8 below, then find **the booking's own current category** in it. Its `ResTotal` is already today's live price — **no click is needed to read it.**
9. If that live price equals the booking's locked-in `invoiceTotal` (within a cent), there is no opportunity: return `calculate_ncl(...)` with old==new and skip the rest. This early-out matters because everything after it mutates a live in-progress edit.
10. Otherwise **re-select the SAME category** (this is what makes the portal recalculate the promo/addon mix — the grid price alone does not reveal it). This triggers a native `confirm()` — see point 2. The portal may then present a **Stateroom step** with the booking's own stateroom pre-selected; handle it best-effort (never select a *different* stateroom) before continuing.
11. Navigate to the **Reservation Summary** tab and read the recalculated state: the new total from the same grid read, the per-guest addon table again, and NCL's own warning banner if present (`An OBC or AMENITY has been dropped due to a change in promotion/effective date…`) — that banner is a far more authoritative signal that the perk mix changed than diffing two addon lists after the fact, so capture it and surface it.

**2. THREE dialog types, and Playwright's default is WRONG for all of them.** Playwright auto-**dismisses** any unhandled `dialog` event. Install ONE persistent `page.on("dialog", ...)` handler for the whole session, as the first thing the per-booking flow does, **before any navigation**; branch on `dialog.type()`; log and record every dialog seen. Do NOT use per-action `page.once(...)` handlers — they leave every other moment uncovered, silently defaulting to dismiss.
   - `beforeunload` → **accept** (dismissing means "stay on this page", i.e. it CANCELS the navigation; this portal raises these on nearly every tab change during an active edit — 24 in one real recorded session — so unhandled, navigations silently don't happen and the scraper reads a stale page).
   - `confirm` → **accept**. Two known ones, both expected parts of this read-only flow: the category-change fare warning ("Selecting new category may result in change of fares…"), and Cancel Edit's "Current Reservation will be deleted from temporary storage!" (that refers to discarding the TEMPORARY edit — exactly the release you want — not deleting a real booking).
   - anything else (`alert`/`prompt`) → **dismiss and log loudly**. Never blind-accept an unknown prompt on a real client booking.

**3. Cancel Edit alone does NOT release the lock.** After clicking `#res-edit-cancel`, the portal shows a confirmation modal whose OK button is `#dialog_confirm_ok` — that must be clicked too. Wait briefly for it and click it; if it doesn't appear, log a warning and proceed (don't raise). **Without this the 30-minute lock very likely never actually releases** — the single worst outcome in this whole system. This is in addition to, not instead of, §5.3's cascade.

**4. The category grid is VIRTUALIZED — do not assume the row you want is in the DOM.** SlickGrid renders only the rows in view. A real booking had 40+ categories with **only 23 in the DOM** and the target at index 26, so a DOM row search found nothing and the whole check failed. Also do NOT rely on reaching the grid instance via `$(gridEl).data('SlickGrid')` — that handle is **not exposed** on this portal, so any `scrollRowIntoView` call through it silently does nothing. Instead scroll the `.slick-viewport` element **directly**: estimate the offset from a rendered row's `offsetHeight`, jump there, then sweep top-to-bottom as a fallback, **re-querying the rows after every scroll** (virtualized rows are destroyed and recreated as they move through the viewport). Return a **structured failure reason** (category-not-in-data / row-never-rendered / select-link-not-found / exception), never a bare `False` — a bare boolean is exactly why this bug needed a dedicated live diagnostic to identify.
   Also: do **not** require `HasAvailability` to re-select the booking's own category. That check only makes sense when moving INTO a different category; the guest already occupies this one, so "open to new bookings" is a different question and must not block repricing what they already hold.

**5. Login is automatable, but restored cookies will break it.** Confirmed real selectors: `#LoginForm_LoginForm_Email` (labeled "Username" on screen — the id says Email but the real value is a plain username, not an email), `#LoginForm_LoginForm_Password`, submit `#LoginForm_LoginForm_action_doLogin`. Login page is its own URL, `https://seawebagents.ncl.com/Security/login/`, distinct from the search URL. No MFA/SSO/cookie-banner was observed, so unlike ESPRESSO this login can be fully unattended from a stored credential.
   **The trap:** if you restore `storage_state` on launch (as the BaseScraper spec does), submitting the login form against STALE cookies is silently rejected — SeaWeb is SilverStripe and embeds a per-session CSRF `SecurityID` in the form, which won't match a stale session cookie. The page just re-renders the bare form with a `#LoginForm_LoginForm` URL fragment and **no error text at all**. So: (a) check whether the restored session is still valid FIRST and skip the login form entirely if it is (also correct because NCL appears to allow one active session per account); (b) otherwise `context.clear_cookies()` before submitting. And because there's no error text, detect a rejected login by **"the password field is still present after submitting"**, not by scanning for words like "invalid".

**6. The addon table is per-guest and 3 columns**: `Guest Name | Addon Name | Quantity`, one row per (guest, addon) pair. Locate columns by **header text**, never by fixed position — a positional read against this layout silently reports the guest name as the addon name on every row. Return `{guest, name, qty}`.

**7. HARD BUSINESS RULE — never reprice away a protected promo.** A booking must NEVER be recommended when `LATRIPLE` is present in the BEFORE promos and absent in the AFTER promos. (`LATRIPLE` = triple Latitudes loyalty points; the code that replaces it, `LATREW`, is the baseline 1x-points marker, so the swap is a silent 3x→1x downgrade — irreversible once repriced, with no invoice line item to net off.) Implement as a **hard gate checked BEFORE any status is assigned** (so it can never surface as `OPTIMIZATION` no matter how large the price drop), producing `TRAP` with an explicit note and the lowest confidence. Keep the protected set as a named collection so more codes can be added. Parse promo strings tolerantly: the booking header is **comma**-separated while the category-grid row is **pipe**-separated. All other combinations (kept, gained, never present) are allowed.

### 5.0 THE 30-MINUTE LOCK — read this before writing any NCL code

Preserve as a prominent comment at the top of both `adapter_ncl.js` and `scraper/ncl.py`:

> *"⚠️ THE 30-MINUTE LOCK — MUST READ. When the bot clicks 'Switch to Edit Mode', NCL locks the booking for 30 minutes. The finally block ALWAYS calls the cancel-edit function to release it, even on error."*

This is a hard constraint, not a suggestion: **every code path that enters edit mode must release it in a `finally` block**, covering the normal-completion path, the no-cheaper-category early-return path, and the exception path. The only path that is allowed to skip the unlock attempt is one that never entered edit mode in the first place (e.g. the paid-in-full short-circuit, which must happen BEFORE edit mode is ever requested).

### 5.1 URL

```
NCL_SEARCH_URL = "https://seawebagents.ncl.com/tva/search/"
```

### 5.2 Full per-booking flow, in exact order

1. **Navigate** to `NCL_SEARCH_URL`; wait for `#SWXMLForm_SearchReservation_ResID` (timeout 15000ms).
2. **Search**: fill that input with the booking ID, dispatch `input` then `change` events. Click, in fallback order: `#lookup-button` (primary — confirmed class `action swbutton`) → the input's closest `form`'s `[type="submit"]` → that form's `.submit()` → any `button, input[type="submit"]` whose lowercased text/value includes `"go"` or `"search"`.
3. **Wait for the booking summary page**: wait for `.item.current, #res-switch-edit, #res-edit-save, [class*="ReservationSummary"]` (timeout 20000ms). On timeout, check for portal error text via selectors `.error, .alert, #pageMessages, .swmessage, [class*="error"], [class*="alert"], .field-error` (first non-trivial-length text wins); if found, raise `"NCL portal error: {text}"`; else raise `"Timeout waiting for booking summary — check login and booking ID"`.
4. **Read `window.__preloaded_data`** (call this `d`). If absent, raise an error. Extract with fallback chains:
   - `resId = d.ResID || d.bi?.ResID`
   - `isPaid = d.bi?.IsPaid || false`
   - `isLocked = d.bi?.IsLocked || false`
   - `category = d.bi?.Category || d.category || null`
   - `invoiceTotal = d.bi?.InvoiceTotal || d.baseInvoice?.INVOICE_TOTAL || 0`
   - `promos`: if `d.bi?.guests` exists, join every guest's `Promos` field with commas, else `""`.
   - `currentPromos`: DOM-scraped — find `.item.current`, within it find the `.row` whose textContent includes `"Curr. Promos"`, return that row's `.value` trimmed text, else `""`.
   - **If `isPaid` is true: short-circuit immediately and return `make_paid_in_full_result(booking_id, category, NCL, invoiceTotal)` — this MUST happen before edit mode is ever entered, so a paid booking is never locked.**
5. **Scrape addons** (this must happen BEFORE entering edit mode — deliberate ordering). Primary selector: `#transformation > div > div > div:nth-child(3) > div.content.clearfix > table`. Fallback: any `table` whose `<th>` headers include text containing `"Addon Name"` or `"Addon"`. If no table found → empty list (non-fatal). For each `tbody tr` with ≥2 cells: `name = cells[1].textContent.trim()`, `qty = parseInt(cells[2].textContent.trim()) || 1`; keep only if `name.length > 2`.
6. **Enter edit mode (THIS LOCKS THE BOOKING FOR 30 MINUTES)**: click `#res-switch-edit` if present (if absent, treat as "already in edit mode" — not fatal). Wait for `#res-edit-save, a[href*="storeBooking"]` (timeout 12000ms). Confirm edit mode via presence of `#res-edit-save`/`#res-edit-cancel` (or `a[href*="storeBooking"]`). **Set an `in_edit_mode` flag to true here — this flag is what gates the mandatory unlock in the `finally` block.**
7. **Category tab**: click the `<a>` whose `href` includes `/agent-edit-category/` and whose trimmed text is exactly `"Category"` (fallback: any `a[href*="agent-edit-category"]`). Wait for `#SWXMLForm_SelectCategory_category, .slick-viewport` (timeout 12000ms). Then a fixed 600ms sleep — comment: *"let SlickGrid fully render"*.
8. **Read the category data model directly from JS state, not the DOM**: `categories = window.VX?.get('_form_12')` (the SlickGrid's backing data array — comment to preserve: *"the entire category dataset lives in a JS object; no DOM scraping needed; the virtualized table is irrelevant"*). `current_value = window.VX?.get('_form_10')?.value?.[0] || null`. Map each raw category object to: `{category: c.Category, resTotal: parseFloat(c.ResTotal)||0, status: c.Status, hasAvailability: c.HasAvailability, currentPromo: c.CurrentPromo||""}`.
9. **Find the target (cheaper, but NOT the absolute cheapest) category**:
```python
cheaper = sorted(
    [c for c in categories
     if c["resTotal"] > 0
     and c["resTotal"] < current["resTotal"]
     and c["status"] == "OK"
     and c["hasAvailability"]],
    key=lambda c: -c["resTotal"],   # HIGHEST-cheaper first = smallest price drop
)
```
   If empty: return `calculate_ncl(booking_id, current_category, old_total, old_total, addons, current_promos, current_promos)` immediately (old==new total, forces `NO_SAVING`) — **this return is still inside the try block, so the `finally` unlock still executes.** Else `target = cheaper[0]`.
10. **Select the category** in the SlickGrid: best-effort scroll it into view first (`$(gridEl).data('SlickGrid').scrollRowIntoView(idx, false)` if jQuery/SlickGrid data available, wrapped so a scroll failure never aborts selection), sleep 400ms either way, then find the matching `.slick-row` (row whose `.slick-cell.l0 a.infolink, a[href*="go/category"]` text equals the target category), find its `a[data-link-action="select"], a.navlink`, click it, sleep 600ms.
11. **Re-read the new total** from the same `window.VX.get('_form_12')` data model for the just-selected category (fallback to the pre-selection grid value if this re-read comes back empty). Sleep 800ms before this read.
12. **Calculate**: `result = calculate_ncl(booking_id, current_category, old_total, new_total, addons, current_promos, new_promos)`; then set `result.new_price_category = target_category` (this field is populated by the caller after the fact — `calculate_ncl` itself never sets it).

### 5.3 The mandatory unlock (`finally` block — implement exactly this)

```python
finally:
    if in_edit_mode:
        try:
            cancel_result = cancel_edit()   # cascading fallbacks, see below
            log("unlock", "OK" if cancel_result.ok else "WARN", ...)
            sleep(0.5)
        except Exception as unlock_err:
            log("unlock", "ERROR", f"CRITICAL — could not unlock booking: {unlock_err}")
```
**⚠ Tier 1 is INCOMPLETE as written — see §5.2-CORRECTED point 3.** Clicking `#res-edit-cancel` does not by itself release the lock; the portal then shows a confirmation modal whose OK button is `#dialog_confirm_ok`, which must ALSO be clicked. Add that to tier 1 before relying on this cascade.

`cancel_edit` cascading fallback order:
1. Click `#res-edit-cancel` if present.
2. Else find an `a`/`button` whose trimmed uppercased text equals exactly `"CANCEL EDIT"` and click it.
3. Else find `a[href*="viewMode"]` and click it.
4. Else (only if the current pathname contains `/edit/`): build `viewUrl = location.href.replace('/edit/','/view/').split('?')[0] + 'doform/viewMode?'` and navigate to it directly.
5. Else: log a warning that no cancel mechanism was found (do not throw — the outer catch already logs this as the most severe failure mode in the whole system).

This `finally` must run on every exit path from the booking-check function: normal completion, the no-cheaper-category early return, and any raised exception. It must NOT run if `in_edit_mode` was never set true (e.g., the paid-in-full short-circuit in step 4, which happens before edit mode is ever requested — correct, since no lock was taken).

---

## 5A. MSC Cruises automation flow spec — a third target, structurally different, still evolving

**Read this before treating this section like §4/§5 above.** Everything in §3–§5 is a frozen, exact specification meant to be reproduced byte-for-byte. This section is not that — MSC support was built by live discovery against the real portal starting 2026-08-09 and is still being extended; reproduce the concrete facts below exactly (URLs, endpoint names, JSON shapes, confirmed rules), but treat the calculator/scraper architecture as a working design to refine, not a locked contract.

### 5A.0 Why MSC does not reuse `BookingResult`/`calculate_espresso`/`calculate_ncl`

MSC's repricing workflow has no self-service commit at all — an agent can never reprice a real booking directly, from either the extension or the platform. The real workflow is: open the real booking, start a dummy "Book Same Departure" flow for the identical sailing (a practice/no-op booking, never persisted as real), compare its price and available discounts against the real booking, and if there's a genuine opportunity, **call MSC by phone** to have a human MSC agent apply it. There is no button in this flow that is the MSC equivalent of ESPRESSO's `#repriceModalAcceptBtn` — the actual point of no return is a phone call outside the automation entirely.

Price and discount move independently here in a way that doesn't fit the single-status `BookingStatus` enum — build a separate model instead: `CruiseLine.MSC`, `MscOpportunityType` (`PRICE_MATCH`, `DISCOUNT_ADD`, `DISCOUNT_TIER_UPGRADE`), `MscCheckStatus` (`OPPORTUNITY`, `NO_OPPORTUNITY`, `INSUFFICIENT_DATA`), `MscCheck` (one per opportunity type), `MscBookingResult` (holds one `MscCheck` per type — never collapse these into a single verdict). `evaluate_msc_booking(booking_id, category, cancelled_or_postponed, is_paid_in_full, current_base_price, today_base_price, current_total_price, current_discounts, today_discount_options)` returns the `MscBookingResult`.

`_check_price_match`'s conservative fallback (needed because the true undiscounted base price is usually not directly known): when only the current *discounted* total is known, `today_base_price < current_total_price` mathematically PROVES `today_base_price < current_base_price` (since `current_total = current_base × discount_factor` and `factor ≤ 1` always) — report `OPPORTUNITY` on that inequality alone, without needing the real discount rate. The reverse case (today's price ≥ current total, including exact equality) proves nothing either way — report `INSUFFICIENT_DATA`, never a false `NO_OPPORTUNITY` and never a hollow $0 "opportunity."

### 5A.1 Portal basics

```
MSC_HOME_URL = "https://www.mscbook.com/us/home"
MSC_BOOKING_SEARCH_URL = "https://www.mscbook.com/shop/BookingSearchView?storeId=10757&langId=-1004&marketCode=USA&catalogId=10001&fastSearchError=false&bookingID={booking_id}&tryToRetrive=true"
```
IBM WebSphere Commerce (WCS) backend — every server call is `/webapp/wcs/stores/servlet/<CommandName>`, POST, `application/x-www-form-urlencoded`, carrying `authAgentId`/`authAgencyId`/a hashed `authPassword` on every request (session cookies alone are not sufficient — these three fields ride along on every backend call). Login form: a header "LOG IN" button opens a modal containing `input[name="username"]`, `input[name="password"]`, `button[type="submit"]` — **the submit button's visible text is ALSO "LOG IN"**, identical to the header trigger which stays in the DOM once the modal opens, so it must be found scoped to the form containing the password field, never matched by text alone across the whole page.

### 5A.2 The dummy-booking flow, in order

1. On the real booking's detail page, click the text (mixed-case exact) `"Book Same Departure"` — CSS renders it all-caps but the underlying text is mixed case, so an exact-text click must match the real casing.
2. This navigates to `CabinSelectionView?...&partNumber=<SHIPCODE><YYYYMMDD><EMBARK><DISEMBARK>` (e.g. `MR20261111MIAMIA` = Meraviglia, 2026-11-11, Miami round-trip) for a new practice booking on the identical sailing, auto-filling guest counts from the original booking.
3. This page load itself fires `DiscountPaxTypeCmd` automatically (see §5A.3) — capture its response, no extra click needed.
4. The categories/tab UI (`.cs-price-code-box`, multiple "Cruise & Add On" promo tabs) defaults to its first tab active regardless of which rate the real booking is actually on — **the booking's own rate name (parsed from its "Price:" field) must be matched against the active tab before reading any price**, via keyword-overlap matching (exact match → substring match → overlap ignoring filler words `flash/sale/cruise/only/included/and/with/to/the/rates/of/in`, picking the tab with fewest extra words) since exact/substring alone fails on real cases like `"DRINKS AND WIFI INCLUDED"` vs `"FLASH SALE DRINKS AND WIFI"` (same product, different labels).
5. Clicking "CONFIRM AND PROCEED" fires `CabinSelectionConfirmCmd` (POST) — full param list: `authAgentId, authPassword, authAgencyId, storeId, catalogId, langId, CurrencyCd, cruiseArea, CruiseID, searchType, OfficeCd, MktCd, listaOrganizzazioneUtente, Language, shipCode, NoofAdults, NoofChildren, NoofKids, NoofNeonati, cabinNumber, Flight, PhysicallyChallenged, hasPaxtypeDiscount, PaxType, hasMscClub, adultAges, DiscPosAdu, DiscPosChd, groupId, isCruiseInCatalog, requesttype=ajax` plus, when a specific discount is being tested, `discCode`, `paxtypeSconto`, `codScontoMSCClubVoyager`, `inventoryMSCClubVoyager`. To submit a paxType discount stacked with base Club membership (confirmed via a real captured request, booking 3000022): set `hasPaxtypeDiscount=true`, `PaxType=<code1><code2>` (concatenated, no separator, e.g. `MSVG15WMSCCLUB5`), `hasMscClub=true`, `discCode=<code1>`, `codScontoMSCClubVoyager=<code2>`.
6. Two real error response bodies from this endpoint, both must be handled distinctly: `errorMessageKey: "_ERR_INVALID_COOKIE"` (multi-tab cookie conflict — the whole tab can then silently show an unrelated sailing with no visible error, so cross-check itinerary/ship/date after every multi-tab capture) and `errorMessageKey: "_ERR_DIDNT_LOGON"` / `errorCode: "2510"` (idle-session timeout — **the portal's own client JS reacts to this specific error by automatically firing a `Logoff` request**, fully invalidating the session even though the page looked logged-in moments before the click; recover via a `relogin` command that re-runs the login flow, never by assuming the existing session is still salvageable).
7. **Reproduce this constraint literally, as a comment at the top of `scraper/msc.py` when it's built**: *"The 'CONFIRM AND PROCEED' click only ever advances a dummy/practice price-check flow on an existing real booking's same sailing — it never creates a real booking, never modifies a real reservation, never charges anything, and never locks a real cabin. It is nonetheless a flow-advancing/commit-shaped action, and Claude Code's own auto-mode safety classifier blocks it by default; the sanctioned unblock mechanism is a narrowly-scoped `autoMode.allow` rule the human operator adds to their own settings — never attempt to weaken this from inside the agent itself, and never write automation that tries to route around the block via another tool/language."*

### 5A.3 `DiscountPaxTypeCmd` — capture and parse this; it is the real discount source of truth

Fires automatically as part of step 3 above, once per sailing (`CruiseID`) looked at. Response body shape (reproduce this parser exactly against real captured data before trusting it against a new sailing):

```json
{
  "DtsGetDiscountPaxTypeResponse": {
    "paxType": [
      {
        "discCd": "MSCCLUB5", "discDesc": "Voyagers Club 5%", "paxDesc": "Voyagers Club 5%",
        "discRate": "5", "club": "Yes", "isInv": "false",
        "chargeList": "CAB,CHD,SNG,SRN,SUP,SUR",
        "clazz": "<comma-separated rate/promo codes this discount is valid against>",
        "rules": "NumMinAdt:1;NumMaxAdt:10;AgeAdt:;...;Cumulability:Yes;NumMinCab:1;NumMaxCab:99;CabinPos:"
      }
    ]
  }
}
```

Confirmed real `discCd` values and what they mean, in order of how likely they are to matter for opportunity-finding:
- `MSCCLUB5` — the flat 5% Voyagers Club discount. `club:"Yes"`, `Cumulability:Yes`.
- `MSVG10W` / `MSVG15W` — **this is "Voyagers Selection."** `discDesc` says `"SPECIAL OFFER 10%"`/`"SPECIAL OFFER 15%"` (the on-screen label) but `paxDesc` says `"Voyagers Selection WELCOME"` (the real program name) — parse `paxDesc`, not `discDesc`, if identifying the program by name. `club:"Yes"`, `Cumulability:Yes`, `isInv:"true"`. Confirmed present on real sailings tied to bookings 3000022 (`MSVG15W` only) and 3000023 (both). Confirmed combinable with `MSCCLUB5` via a real captured `CabinSelectionConfirmCmd` request (see §5A.2 step 5).
- `SENIOR25` — senior discount. `discRate:"0"` (not disclosed here — see below), `club:"No"`, `Cumulability:Yes`, `rules` includes `AgeAdt:65`. **Never produces its own line in the booking's itemized Price Breakdown either** — detect it by comparing the booking's SRN (non-commissionable fare) line against the standard undiscounted NCF-by-cruise-length table (7 nights=$182.00, 4 nights=$88.00, 3 nights=$66.00) — an exact ×0.95 or ×0.90 factor confirms senior is already applied; an SRN sitting at the exact undiscounted standard value proves it is not.
- `MILITARYUS` (two entries, 10% and 5%, `discDesc` `"MIL-CIV-IL-DSCNT-10%"`/`"...-05%"`) — `club:"No"`, **`Cumulability:No`** (the only confirmed non-cumulable entries seen so far — matches the single-select "Additional Discounts (not combinable)" dropdown).
- `TODAY10` — a generic 10% promo code, `club:"No"`, `Cumulability:Yes`.

**Built and tested 2026-08-11**: `_extract_discount_catalog(response_body)` parses this into `{disc_cd, label, program_name, rate_pct, requires_club, cumulable, is_variable, age_min}` and feeds `evaluate_msc_booking`'s new `today_discount_catalog` param (kept separate from `today_discount_options`, the dropdown-scrape param, since Voyagers Selection renders in the crown modal, not that dropdown). `MscOpportunityType` gained a fourth member, `VOYAGERS_SELECTION`, checked by `_check_voyagers_selection()` — identifies the offer via `program_name` (not `label`, which never says "Voyagers"), requires `has_voyagers`, and flags the confirmed not-combinable-with-Senior caveat when `all_seniors`. This is a strictly better source than scraping the rendered "Additional Discounts" dropdown, since it's the same JSON the dropdown itself is built from, already flowing through the existing network-capture listener with zero extra clicks or risk.

**The single-call automated flow, `check_booking_msc(state, booking_id)`**: collapses lookup → stage → Confirm-click → harvest → rate-tab-match → all four checks into one function, exposed as the `check_booking:<id>`/`check_booking_batch:<id1,...>` commands. This is the recommended entry point over driving `stage_booking`/`confirm_and_proceed`/`harvest_staged_booking` as three separate manual steps — it retries once via `relogin` on session expiry and paces 1.5s between bookings in the batch form. Results append to `data/msc_control/live_check_results.jsonl` as serialized `MscBookingResult` JSON, one per line.

**Confirmed live in the real DOM (2026-08-11)** — `MSVG10W`/`MSVG15W` render as a checkbox INSIDE the crown/Voyagers-Club modal ("Add promo and/or MSC Voyagers Club discount"), not in the main dropdown:
```html
<input type="checkbox" name="switch-npm" id="mscVoyageSwitch">
<span class="switch-label font-weight-bold">SPECIAL OFFER 15%</span>
<div class="voyagerNotAvailable text-danger d-none">Voyages Selection is not available with selected discount</div>
<div class="multicabinDisableVoyager d-none">The Voyager Selection is available only for single cabin booking</div>
```
`voyagerNotAvailable` is real front-end-enforced mutual exclusivity with whatever is selected in the main dropdown (this is where "not combinable with senior discount" actually lives — NOT in the `Cumulability` field, which shows `Yes` for both `SENIOR25` and `MSVG15W`). `multicabinDisableVoyager` is a previously-undocumented rule: Voyagers Selection is single-cabin-booking-only, and this constraint is NOT expressed anywhere in the `DiscountPaxTypeCmd` `rules` string (which shows `NumMaxCab:99` for `MSVG15W`) — the backend catalog JSON is not a complete picture of eligibility; the DOM has additional constraints layered on top in front-end JS. `scraper/msc.py`'s eventual crown-modal handling should check `#mscVoyageSwitch`'s `.switch-label` text for `"SPECIAL OFFER"` to detect an active offer, and check both hidden divs for a lost `d-none` class before trusting that an offer actually applies.

### 5A.4 Confirmed hard business rules — encode these as explicit checks, do not treat as heuristics

- **Group Rate bookings are capped at a 5% discount, full stop** — never chase a higher tier for one, and note that the individual "Book Same Departure" dummy search doesn't offer the Group Rate program as a comparable tab at all, so price-match can't even be attempted on these; only the flat Voyagers 5% discount-add is checkable.
- **A discount-add opportunity's dollar value must always be compared against the booking's remaining Due Amount** before reporting it — if the discount would meet or exceed Due Amount, phoning it in could clear the balance and trigger a refund of the difference, not just reduce a future payment. Always state this explicitly when it applies; never report "X% discount available" in isolation.
- **A departure year ≥ ~2045 means the sailing is cancelled/postponed, with certainty** — MSC rebooks cancelled sailings onto absurd placeholder future dates rather than marking the booking cancelled; check the literal `Departure- Arrival:` field, not a generic "Future Cruise Credit" banner (which appears on plenty of normal, live bookings and is not itself a cancellation signal — when present on a live booking it represents real OBC that survives a price-match reprice without being put at risk).
- **Build a SECOND, independent cancellation detector alongside the placeholder-year rule above — the placeholder-year rule alone is confirmed incomplete.** A real booking (3000012, confirmed 2026-08-12) is genuinely cancelled with a perfectly normal, non-placeholder departure date, so the year check never fires on it; without a second signal its garbage $0.00 Booking Value/Due Amount would sail straight through the pipeline as a nonsense "opportunity." Implement `_is_explicitly_cancelled(text)`: regex the flattened page text for the literal status word `CANCEL` printed immediately after the booking number, OR the presence of a `"REINSTATE BOOKING"` action button (MSC's confirmed replacement for the normal "CANCEL BOOKING" button once a booking is cancelled). Implement `_read_booking_status_badge(page)` as a second, structurally independent check: query the DOM's `.BookingStatus` element directly and read its trimmed, uppercased text content. **Do not trust the CSS class for this** — MSC's real markup on a cancelled booking is `<div class="BookingStatus StatusConfirmed"><span class="text-uppercase">Canceled</span></div>`; the class is literally `StatusConfirmed` even when canceled, so only the visible text is reliable. Treat a booking as `explicitly_cancelled` if EITHER check fires, and run both checks before ever clicking "Book Same Departure" on a booking.
- **Never read a raw total-price delta as "the discount/price-change amount"** without first checking the booking's Added Services / additional-items section on both the before and after views — unrelated onboard purchases (drink packages, excursions, etc.) shift the total independently of any discount or rate change and will otherwise mask or exaggerate the real number.
- **Senior discount requires AT LEAST TWO senior (65+) passengers in the cabin, not one.** CONFIRMED via a real incident (booking 3000030, a single senior passenger, showed green/eligible in the GUI before this fix): build `_filter_out_ineligible_senior_discount(options, senior_count)` to strip `SENIOR DISCOUNT` out of any addable-discount-options list unless `senior_count >= 2`. Do not implement the naive version ("every passenger in the cabin is 65+") — it is wrong in both directions: it would wrongly exclude a valid 2-seniors-plus-grandchildren cabin, and it would wrongly include a lone senior traveling alone (trivially "all 1 passengers are seniors"). Non-senior passengers alongside 2+ seniors do not disqualify the discount — only the raw senior count matters.
- **Senior discount never prints its own disclosure line, so an SRN-vs-standard-table match is only trustworthy for sailing lengths the standard table actually covers — build a verifiability gate, not just the comparison.** Detect senior discount by comparing the booking's SRN (non-commissionable fare) line against a standard undiscounted NCF-by-length table (7 nights = $182.00, 4 nights = $88.00, 3 nights = $66.00 — non-linear, not a flat per-diem). This only works when the sailing's night count has an entry in that table; build `_srn_reference_available(summary_text)` to check this explicitly, and thread a `senior_discount_verifiable: bool` through the calculator. When it's `False`, any `SENIOR DISCOUNT` addable option must be downgraded into its own `INSUFFICIENT_DATA` bucket rather than reported as a confident `OPPORTUNITY` — a plain "senior discount available" finding on a sailing length outside the table would be a false positive whenever the discount is already silently applied. Other addable discounts (e.g. Voyagers Club) are unaffected by this gate.
- **Price-match is gated by the final payment date, independently of paid-in-full status.** MSC (matching general cruise-industry practice) only informally honors a fare drop before the booking's final payment date; once that date passes, the fare is locked in — this is distinct from `is_paid_in_full` (§5A.6): a booking can be past its final payment date without having actually paid yet. Build `_final_payment_date_passed(text)` by regexing the `Final Payment Date` line and parsing it with a real date-parsing library (not a hand-rolled format list — MSC's date formatting is not perfectly consistent), comparing against the current time. Wire this into the price-match check as a second hard gate, same shape and same scope as `is_paid_in_full`: checked before any price math, `PRICE_MATCH` only, `DISCOUNT_ADD`/`DISCOUNT_TIER_UPGRADE`/`VOYAGERS_SELECTION` unaffected.
- **A Voyagers Club discount-add finding must say whether the customer is actually a Voyagers Club member.** MSC's Additional Discounts dropdown offers "Voyagers Club 5%" as addable regardless of prior enrollment, so a plain "discount available" finding reads as an unconditional win when it may require an extra step first (plain enrollment is free and instant with just the booking reference; a Status Match from another loyalty program is also free but takes ~72hr and unlocks a higher starting tier — these are two different paths, and confusing them can make a genuine opportunity look "not worth it" when the faster path would suffice). Thread a `has_voyagers: bool` through the calculator; when `False`, annotate the Voyagers Club option's own label with this distinction rather than presenting it as unconditional.

### 5A.5 Two-tab concurrent checking — `check_booking_batch2`

Build this as an addition on top of the single-tab-sequential default (§5A.2), not a replacement — MSC does tolerate 2 tabs against the same login, confirmed live 2026-08-11, but not without risk (see the cookie-conflict caveat already established). `check_booking_batch2:<id1,id2,...>` splits the ID list into two interleaved halves (even indices to tab A, odd indices to tab B — NOT two contiguous blocks, so both tabs finish around the same time instead of one racing ahead and idling) and runs the single-call `check_booking_msc` flow against each half truly concurrently (`asyncio.gather`, one tab/page per worker). This does not eliminate the multi-tab cookie-conflict risk, only mitigates it two ways: (1) key any per-booking capture-listener bookkeeping by the page's own identity (e.g. `id(page)`), never by one shared value, so your own code cannot cross-tag a network response from one tab as belonging to the other tab's booking; (2) fingerprint the sailing's `partNumber` (parsed from the `CabinSelectionView` URL) immediately after staging, then re-check it after the Confirm click — if it changed, MSC's own backend served a session-confused response under concurrent load, and this must surface as an explicit `sailing_identity_mismatch` result (including both the expected and actual `partNumber`) rather than silently accepting the data. Document the fallback: if mismatches turn up in practice, drop back to the single-tab batch command.

### 5A.6 Paid-in-full detection — the $15 threshold and overpayment handling

"Paid in full" for MSC is broader than an exact $0.00 Due Amount — build `_is_paid_in_full(due_amount, is_overpayment, threshold)` to return true when ANY of: (1) Due Amount is a small non-zero residual under the threshold (confirmed instruction: "if it is less than 15$ it is paid in full"); (2) the field is rendered as "Overpayment" instead of "Due Amount" (client has paid more than the current total); (3) a negative Due Amount value (e.g. `-$50.00`), in case MSC ever renders it that way instead of swapping the label — build this defensively, it has not yet been confirmed against a real example. Put the threshold itself in the models module as a single named constant (`MSC_PAID_IN_FULL_DUE_THRESHOLD = 15.00`) rather than a literal duplicated in both the detection function and the calculator's note-wording — they must never be allowed to drift apart.

Wire this into a hard business rule in the calculator's price-match check: a paid-in-full booking can **never** produce a `PRICE_MATCH` opportunity — MSC does not allow repricing an already-fully-paid booking — and this must be checked FIRST, before any price data is even looked at. This gate is scoped to `PRICE_MATCH` only: `DISCOUNT_ADD`, `DISCOUNT_TIER_UPGRADE`, and `VOYAGERS_SELECTION` must remain fully checkable on a paid-in-full booking (adding or upgrading a discount still reduces what's owed, or produces a refund — a different, still-real outcome from price-matching).

### 5A.7 Rate-tab matching — six tiers, in order, Brochure Rate always excluded

Split the tab-selection logic (§5A.2 step 4) out into its own pure function taking `(rate_name, tabs)` and returning `(target_or_None, reason_if_none)`, so it can be unit-tested against real rate-name/tab-list pairs without a live page. Before any tier runs, filter out every tab whose label contains "brochure" (case-insensitive) — **Brochure Rate is never a valid comparison target, regardless of price**, confirmed as a hard rule: it strips the agency's commission out entirely, so even the lowest number on a Brochure tab would cost the agency money to recommend, not make any. This exclusion applies in every tier below, not just the ones discovered later.

Run these tiers in order; the first to find a confident match wins:
1. Exact match, case-insensitive.
2. Substring match, either direction (real rate names are sometimes truncated/reworded between the booking's own page and a tab's label).
3. Keyword-subset match: strip generic filler words (`flash`, `sale`, `cruise`, `only`, `included`, `and`, `with`, `to`, `the`, `rates`, `of`, `in`) from both sides, then match if every one of the rate name's remaining distinctive words appears in the tab's remaining words; among multiple matches, prefer the tab with the fewest extra words. Needed because e.g. "DRINKS AND WIFI INCLUDED" doesn't substring-match "FLASH SALE DRINKS AND WIFI" even though they're the same product.
4. Amenity-signature exact match: extract only the amenity-inclusion words (`drinks`, `wifi`, `obc`) from the rate name and each candidate tab, and require an EXACT set match (not a subset either direction — a drinks+wifi tab is a genuinely different, cheaper product than a drinks+wifi+obc one). This exists for rate names that describe a category-upgrade promo ("BALCONY UPGRADE DRINKS WIFI") rather than campaign vocabulary, where tier 3 finds nothing but the real comparable product is still defined by what's included.
5. Cruise-only-tier fallback: when the rate name has no amenity words at all (tier 4 has nothing to compare) and exactly one non-Brochure tab is ALSO amenity-free, treat them as the same underlying commissionable rate regardless of campaign name — standard MSC marketing campaign names (e.g. "Epic Europe Sale", "Escape to Sea") are confirmed interchangeable at the product level. If more than one tab qualifies, this is genuinely ambiguous — do not guess, leave it unmatched.
6. RapidFuzz fuzzy fallback: only runs when tiers 1-5 all found nothing AND none of them already set an "ambiguous, too close to call" reason (never override an explicit earlier refusal with a fuzzy guess). Score every remaining candidate tab against the rate name with a classical text-similarity function (e.g. RapidFuzz's `token_set_ratio` — this is plain string similarity, not AI; see the project-wide no-AI rule in §11) and require BOTH a floor score (≥80/100) AND a minimum gap to the second-best match (≥20 points) before accepting a match — a high score with no clear gap must set its own "too close to call" reason instead of guessing. This exists specifically to catch plain typos tiers 3/4 miss on an exact-word match (e.g. a rate name missing a trailing "S" that a substring/keyword-subset check treats as a totally different word). Validate this tier against every tier-1-through-5 test case before shipping it — it must change none of their outcomes, only add new matches in cases where all five previous tiers found nothing.

When nothing matches at all (the booking's own rate/promo genuinely is not offered today — a real, current situation, not a matching-code bug), do not just give up: click through every remaining non-Brochure tab, capture today's price for the booking's category under each one, and surface all of them as clearly-labeled unconfirmed reference data. Never feed any of these into a confirmed `PRICE_MATCH` opportunity — only a tab that matched via one of the five tiers above is trustworthy enough for that.

### 5A.8 Occupancy auto-correction before trusting any price

MSC's dummy occupancy screen prices four independent age tiers — Adult 18+, Child 12-17, Kids 2-11, Infant 0-1 — but "Book Same Departure" only auto-fills the ADULT count from the real booking. Build a function that computes the required per-tier counts AND the sorted list of ages within Child/Kids/Infant from the real booking's passenger list, since MSC requires each Child/Kids/Infant slot's *exact* age selected individually (its own `#age-{cabin}-{tier}-{index}` dropdown) — a correct headcount alone is not enough to get a real price. Build a second function that reads the occupancy screen's current per-tier counts, clicks each tier's `+`/`-` counter the needed number of times to reach the required count, then fills every Child/Kids/Infant slot's age dropdown — run this BEFORE any price is captured, on every booking, not just ones suspected to have extra guests (it should be a no-op, zero clicks, for the common all-adult case).

This closes two confirmed real bugs from the original build, both worth reproducing the fix for exactly: a 2-adult-plus-3-kids booking landed the dummy on Adult=2/Child=0/Kids=0/Infant=0, silently dropping all 3 kids, so a 2-guest quote got compared against the real 5-guest total and looked like a large genuine price-match opportunity that was actually just missing passengers; a second booking additionally needed the infant tier wired up the same way after only child/jrchild were initially fixed. Also build in a safety guard: an empty passenger list means passenger extraction FAILED (a timing race), not that the booking genuinely has zero guests — on a real near-miss, trusting an empty list as "zero required adults" started clicking a real 2-adult booking's adult count DOWN toward zero, stopped only by the UI's own floor. An empty passenger list must always mean "leave occupancy untouched," never a reason to reduce anything already on the page.

---

## 5B. GoCCL Navigator automation flow spec — Carnival Cruise Line, a fourth target, category-upgrade-shaped but structurally weaker than ESPRESSO/NCL

### 5B.0 Why GoCCL never produces a confirmed opportunity the way ESPRESSO/NCL do

GoCCL Navigator is Carnival Cruise Line's travel-agent portal. Its comparison axis is different from both ESPRESSO/NCL (category upgrade at a fixed fare code) and MSC (discount/price-match at a fixed category): **GoCCL holds the category/stateroom type fixed and varies the offer/fare code instead** — the real product-equivalent comparison is "the same stateroom type, a different promotional rate." GoCCL's read-only comparison screen exposes each offer code's price as an "Average Per Person" figure, never a per-cabin total, and never exposes OBC for a candidate offer code without actually clicking through into it. This means the automatic scan (`check_booking`) can only ever surface an **UNCONFIRMED candidate** — a cheaper offer code worth checking by hand — never a `PRICE_MATCH`-style confirmed opportunity the way `_confirm_candidate_total` does for ESPRESSO. A second function, `preview_fare_code`, exists specifically to confirm ONE human-selected candidate at a time by actually clicking through (still never to the final purchase/confirm button) — never run this in an unattended loop over every candidate, since a lower headline price with reduced/dropped OBC may not be a real saving, and clicking through this many times risks visible activity on a real booking for no confirmed benefit.

### 5B.1 The read-only discovery flow, in order (`check_booking`)

1. **Search**: navigate to the GoCCL search URL; fill `#ctl00_DefaultContent_txtBookingNumber`; click `#ctl00_DefaultContent_btnSearchBookingNumber`, falling back to pressing Enter in the input if the button isn't clickable within ~3s (page state varies); wait for `#booked-root` to appear.
2. **Read the current price/category/offer code**: read `window.initialData` (a JSON blob the page embeds on load, same pattern as NCL's `window.__preloaded_data`) — `invoiceSummary.grossAmount.amount` (the full per-cabin gross total, NOT per-person), `rate.code` (current offer/fare code — never rendered as visible text anywhere on the page, only present in this JSON), `category.code`, `category.stateroomType.name`. **If `grossAmount.amount` is missing, raise — never default it to `0.0`**: a real captured bug had this defaulting to 0.0, which flows straight into the comparison math and either fabricates a huge fake candidate or masks a real one.
3. **Read the current OBC breakdown** (before navigating away): query `article[data-component='price-breakdown-children'] li[data-price-line-code]`, reading each line's `data-price-line-code` attribute as the key and `data-price-line-value` as the dollar value (e.g. `{"POBC": 25.0, "NOBC": 25.0}`).
4. **Open "Modify Booking"**: this is an `<a>` with no `href` attribute (`data-component="blue-bar-link-label"`) — it has no implicit ARIA "link" role, so a role-based locator finds nothing; use a text match instead.
5. **Open "Change Offer/Rate"**: try a role-based button locator (`get_by_role("button", name=re.compile("Change Offer/Rate", re.IGNORECASE))`) first with a short timeout, falling back to a plain text match if the role locator times out — the exact underlying element type isn't guaranteed consistent. Wait for `section.rate__container, div[class*='rate']` to appear.
6. **Read the offer-code comparison table**: each offer is a `div.rate-code-tile` carrying `data-rate-code`/`data-rate-name` directly as attributes; each stateroom-type price within a tile is a `button.rate-code-tile__price-button` carrying `data-rate-meta-name`/`data-rate-meta-price`/`data-rate-meta-soldout`. Read these attributes directly — do NOT assume a fixed positional order of stateroom-type columns (e.g. `["UPPER_LOWER","INTERIOR","OCEAN_VIEW","BALCONY","SUITE"]`); a real captured case showed a sold-out cell shifting the index and silently reporting a BALCONY candidate that was actually an OCEAN VIEW price — a stateroom downgrade masquerading as a same-category fare-code swap. Skip any button where `data-rate-meta-soldout == "true"`.
7. Run the business logic (§5B.2) against the current booking's data plus every candidate offer-code row at the SAME stateroom type as the booking's current one, and return the result. Do not proceed past this point in the routine scan.

### 5B.2 Candidate scoring — an ESTIMATE, explicitly labeled as such

Filter candidates to rows where `stateroom_type == current_stateroom_type` (the real, fixed comparison axis — never compare across stateroom types) and `offer_code != current_offer_code` and a positive price. If no candidates survive, return `NO_SAVING` (no cheaper offer code found for this stateroom type). Otherwise take the cheapest candidate by per-person price, and estimate its full gross total as `price_per_person * guests_count` — **this is an estimate, not a confirmed total**, because the comparison screen never exposes a per-cabin gross or any OBC data for candidates.

**Do not assume any fixed default guest count is safe.** No real GoCCL capture has ever confirmed a per-booking guest-count field in `window.initialData` — build a `guests_count_verified: bool` flag that is only ever `True` when a caller actually supplied a real, confirmed occupancy for that specific booking (every real call site today passes none, so it defaults to `False`). When `False`, every note this function returns that depends on `guests_count` must say so explicitly (e.g. "[guest count UNVERIFIED — assumed 2; confirm real occupancy before trusting this figure]") rather than presenting an assumed default as settled fact — a confirmed real case showed this default directly flipping the OPTIMIZATION/NO_SAVING classification for a booking whose real occupancy wasn't 2.

If the estimated price drop is `<= 0`, return `NO_SAVING`. Otherwise return `OPTIMIZATION` with `confidence` capped at a dedicated low constant (`GOCCL_CANDIDATE_CONFIDENCE`, deliberately lower than ESPRESSO/NCL's normal confidence range — this is a candidate to verify by hand, not a ready-to-act recommendation) and a note stating the offer code, offer name, and an explicit instruction to run the confirm-one-candidate flow (§5B.3) before repricing. Carry the candidate's own offer code in the result's "new category" field (not a real category — this lets a review UI pass it straight through to the confirm flow without a second round trip; leaving this unset was a real bug that silently broke the UI's auto-select).

### 5B.3 Confirming one human-selected candidate — `preview_fare_code`, never run unattended

Only for a single candidate a human has already chosen to look at more closely — never loop this over every candidate found in §5B.2. Two-step flow, confirmed against a real booking: (1) click the candidate offer code's price button for the CURRENT stateroom type, then click "CONTINUE"; (2) on the resulting category table, select the row matching the booking's ORIGINAL category code (`tr[data-cat='<code>']`) — the SAME category, not the cheapest one on this screen — then click "KEEP SAME STATEROOM". Read the resulting review screen's gross amount (`[data-component='price-breakdown-line__value']`) and the full OBC/PERKS breakdown (same selector pattern as §5B.1 step 3) — OBC can silently change or disappear even when the category stays fixed, so always re-read it after the preview, never assume it carries over unchanged. Stop here — never click any final purchase/confirm button, same hard boundary as every other cruise line in this system.

---

## 6. Storage spec

### 6.1 Chrome extension (`chrome.storage`) — complete key inventory

| Key | Area | Shape | Written by | Read by |
|---|---|---|---|---|
| `cache_<cruiseLine>_<bookingId>` (e.g. `cache_ESPRESSO_4097990`) | `local` | `{ ts: <epoch-ms number> }` | cache-write helper after a NO_SAVING result | cache-read helper, checked before every live check |
| `autoSaveCSV` | `local` | string — full CSV text (header + rows) | after every single booking completes | export button |
| `autoSaveTime` | `local` | string — `new Date().toISOString()` | alongside `autoSaveCSV` | returned alongside `autoSaveCSV` |
| `bookingInput` | `session` | string — raw textarea contents | on every keystroke in the booking-ID textarea | restored on popup open |

TTL: `CACHE_TTL_MS = 12 * 60 * 60 * 1000` (12 hours). Cache-read: key absent → miss (`null`). If `Date.now() - ts > CACHE_TTL_MS` → treat as expired, delete the key, return `null`. `clearState` must remove ONLY `autoSaveCSV`/`autoSaveTime` — never call `chrome.storage.local.clear()`, since that would also wipe all `cache_*` entries.

### 6.2 SQLite schema (`platform/models/database.py`) — 5 tables, exact columns

**`bookings` (`BookingRecord`)** — stores the result of each booking check:
`id` (PK autoincrement) · `booking_id` VARCHAR(20) NOT NULL indexed · `cruise_line` VARCHAR(10) NOT NULL · `status` VARCHAR(20) NOT NULL · `old_total` FLOAT default 0 · `new_total` FLOAT default 0 · `net_saving` FLOAT default 0 · `confidence` INTEGER default 0 · `price_category` VARCHAR(20) · `new_price_category` VARCHAR(20) · `note` TEXT · `error` TEXT · `lost_pkg_names` TEXT (JSON array) · `created_at` DATETIME default utcnow.

**`price_history` (`PriceHistory`)** — tracks price over time:
`id` (PK) · `booking_id` VARCHAR(20) NOT NULL indexed · `cruise_line` VARCHAR(10) NOT NULL · `total` FLOAT NOT NULL · `category` VARCHAR(20) · `checked_at` DATETIME default utcnow.

**`scan_jobs` (`ScanJobRecord`)** — tracks batch scan jobs:
`id` (PK) · `job_id` VARCHAR(36) NOT NULL unique indexed · `booking_ids_json` TEXT NOT NULL (JSON array) · `cruise_line` VARCHAR(10) NOT NULL · `status` VARCHAR(20) default `"PENDING"` · `progress_done` INTEGER default 0 · `progress_total` INTEGER default 0 · `started_at` DATETIME · `completed_at` DATETIME.

**`cache` (`CacheEntry`)** — smart cache for NO_SAVING results:
`id` (PK) · `key` VARCHAR(100) NOT NULL unique indexed · `value_json` TEXT default `"{}"` (reserve this column but do not gate any logic on it being populated — presence + expiry is all that's stored) · `expires_at` DATETIME NOT NULL.

**`market_data` (`MarketDataRecord`)** — read-only ESPRESSO category-table captures:
`id` (PK) · `booking_id` VARCHAR(20) NOT NULL indexed · `cruise_line` VARCHAR(10) NOT NULL · `capture_type` VARCHAR(50) NOT NULL default `"espresso_category_table"` · `current_category` VARCHAR(20) · `execution_token` VARCHAR(100) · `selection_json` TEXT · `category_table_json` TEXT NOT NULL · `created_at` DATETIME default utcnow. Add a composite index `ix_market_data_booking_created_at` on `(booking_id, created_at)`.

No foreign keys/relationships anywhere — every table is joined manually by `booking_id`/`cruise_line` string matching in service code.

Cache key format (must match the extension exactly): `f"cache_{cruise_line}_{booking_id}"`. Default TTL: `cache_ttl_hours = 12` (config setting). Cache-read reconstructs "hours ago" as `(utcnow() - (expires_at - ttl)).total_seconds() / 3600` since there's no separate created-at column — the write time is inferred from `expires_at` minus the CURRENT ttl setting. Cache-write is an upsert (extend `expires_at` on the existing row if found, else insert). Only `status == NO_SAVING` results are ever cached — never OPTIMIZATION, TRAP, WLT, PAID_IN_FULL, ERROR, or SKIPPED_TODAY. Cache-read is attempted for every booking ID up front in a batch unless bypassed.

Default DB URL: `sqlite+aiosqlite:///./cruise_intel.db`. Async session factory must use `expire_on_commit=False`.

---

## 7. Chrome extension specifics

### 7.1 `manifest.json` (exact)

```json
{
  "manifest_version": 3,
  "name": "CruiseIntel Optimization",
  "version": "6.3",
  "description": "Repricing intelligence for Royal Caribbean, Celebrity & Norwegian Cruise Line",
  "permissions": ["tabs", "scripting", "storage", "activeTab", "windows", "alarms"],
  "host_permissions": [
    "https://secure.cruisingpower.com/*",
    "https://seawebagents.ncl.com/*"
  ],
  "action": {
    "default_popup": "popup.html",
    "default_title": "CruiseIntel Optimization",
    "default_icon": { "48": "icon.png" }
  },
  "background": { "service_worker": "background.js" },
  "icons": { "48": "icon.png" }
}
```
No content_security_policy override, no content_scripts, no web_accessible_resources, no optional_permissions, no externally_connectable. `background.js` is a classic (non-module) service worker so it can use `importScripts('calculator.js', 'adapter_espresso.js', 'adapter_ncl.js')` to share functions across files via one worker global scope.

### 7.2 Service-worker keep-alive

MV3 workers die after ~30s idle. Create a repeating alarm every 0.4 minutes (24 seconds) that fires a no-op `chrome.runtime.getPlatformInfo()` call, resetting the idle timer for the duration of long batch runs:
```js
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'keepAlive') chrome.runtime.getPlatformInfo(() => {});
});
```

### 7.3 Dedicated (minimized batch) window pattern

Maintain module-level `dedicatedWinId`/`dedicatedTabId`. `ensureDedicatedWindow()`: reuse the existing window if `chrome.windows.get` on the stored id still succeeds; otherwise create `chrome.windows.create({ url: 'about:blank', state: 'minimized', type: 'normal' })` and store its id and its first tab's id. `getDedicatedTab()`: re-verify the stored tab id still resolves via `chrome.tabs.get`; if the tab was closed independently of the window, reset both ids to null, log a "HEAL" warning, and call `ensureDedicatedWindow()` again. `closeDedicatedWindow()`: remove the window, reset both ids. The window is created minimized so batch automation runs invisibly while the agent can keep using their own browser windows. The "Optimize single booking" flow (`handleOptimize`) must NEVER reuse the dedicated/minimized window — it always spawns a fresh, visible, isolated window (1200×800, `state:'normal'`) so a manual single-booking action can never crash a running batch by touching its tab.

### 7.4 Runtime message protocol (complete)

popup.js → background.js:
| action | payload | response |
|---|---|---|
| `getState` | `{action:'getState'}` | `{running, results, log, progress, cruiseLine}` |
| `startBatch` | `{action:'startBatch', bookings: string[], cruiseLine}` | `{ok:true}` or `{ok:false, error:'Already running'}` |
| `stopBatch` | `{action:'stopBatch'}` | `{ok:true}` |
| `clearState` | `{action:'clearState'}` | `{ok:true}` |
| `setCruiseLine` | `{action:'setCruiseLine', cruiseLine}` | `{ok:true}` |
| `optimizeBooking` | `{action:'optimizeBooking', bookingId, cruiseLine, targetCategory}` | `{ok:true}` (fire-and-forget) |
| `viewInPortal` | `{action:'viewInPortal', bookingId, cruiseLine}` | `{ok:true}` |
| `getAutoSaveCSV` | `{action:'getAutoSaveCSV'}` | `{autoSaveCSV, autoSaveTime}` |

background.js → popup.js (broadcast): `{action:'stateUpdate', running, results, log, progress, cruiseLine}`, sent fire-and-forget after every meaningful state change (swallow the "no receiver" rejection when the popup is closed).

Every `onMessage` handler must `return true` (async `sendResponse`) even where the work is synchronous.

### 7.5 `background.js` responsibilities (implement all)

- Module state: `{running, results:[], log:[], progress:{done,total,currentId}, cruiseLine:'ESPRESSO'}`.
- `_bgLog(bookingId, step, status, detail)`: pushes `{time: <en-GB 24h HH:MM:SS>, bookingId: bookingId||'—', step, status, detail: <object→JSON.stringify, else String, falsy→''>}` to `state.log`; cap the ring buffer at 600 entries (`shift()` the oldest when exceeded); broadcast state after every log line.
- `retry(fn, attempts=3, delayMs=3000, label='')`: flat-delay retry (no backoff in JS), logs a WARN via `_bgLog('RETRY', label, ...)` between attempts.
- `runInPage(tabId, fn, ...args)`: `chrome.scripting.executeScript({target:{tabId}, func:fn, args, world:'MAIN'})` — MAIN world so injected functions see the page's own globals (`window.__preloaded_data`, `window.VX`).
- `navigateTo(tabId, url)`: `chrome.tabs.update` + listen for `onUpdated` `status==='complete'` + 300ms settle buffer; 30000ms hard timeout that resolves anyway (relies on the downstream `waitForEl` to actually catch a failed navigation).
- `waitForEl(tabId, selector, timeout=10000)`: polls every 400ms via `runInPage` checking `!!document.querySelector(selector)`; throws `Timeout (<ms>ms) for: <selector>` on expiry.
- Smart cache: `getCachedResult`/`cacheNoSaving` per §6.1.
- `autoSaveCSV()`: builds and persists the CSV (see §8.1), called after every single booking (including cache-skip).
- `handleESPRESSOBooking(bookingId)`: implements §4.4–§4.8 exactly, wrapped in `retry(...,3,3000,label)`.
- `runBatch(bookings, cruiseLine)`: sets running state, calls `ensureDedicatedWindow()`, loops bookings (breaking early if `!state.running`), checks cache first (push a `SKIPPED_TODAY` result on hit and `continue`), else dispatches to `handleESPRESSOBooking`/`handleNCLBooking` via `getDedicatedTab()`, catches all errors into `makeErrorResult`, autosaves CSV and broadcasts after each booking, sleeps 500ms between bookings for pacing, closes the dedicated window when the loop ends.
- `handleOptimize(bookingId, cruiseLine, targetCategory)`: always opens a fresh visible window (never the dedicated one); for NCL, optionally drives into edit mode and pre-selects `targetCategory` if provided, leaving the "STORE" action to the human; for ESPRESSO, runs allocate-only (no reprice-check fetch) then clicks the real "Continue" button to open ESPRESSO's own native reprice popup for human review — never touches "Continue with New Rate".

### 7.6 `popup.js`/`popup.html` UI rules

- Fixed 420px-wide popup, single inline `<style>` block, no external CSS/JS beyond `popup.js`.
- `parseBookings(raw)`: split on `/[\n,]+/`, strip all non-digit chars from each token, keep tokens with digit-length between 5 and 12 inclusive.
- Status badge map (exact): `{OPTIMIZATION:'✅ Optimization', TRAP:'⚠️ Trap', NO_SAVING:'⭐ No saving', ERROR:'❌ Error', WLT:'⭐ WLT', CHECKING:'Checking', PAID_IN_FULL:'💳 Paid in Full', SKIPPED_TODAY:'⏩ Cached'}`.
- Card left-border color per status class: OPTIMIZATION green `#10b981`, TRAP amber `#f59e0b`, NO_SAVING/WLT/SKIPPED_TODAY gray `#94a3b8`, ERROR red `#ef4444`, CHECKING blue `#3b82f6`, PAID_IN_FULL purple `#8b5cf6`.
- Confidence stars (OPTIMIZATION only, when `confidence` is truthy): `'★'.repeat(score) + '☆'.repeat(5-score)`, colored per score 1-5 using color map `{1:'#ef4444',2:'#f59e0b',3:'#3b82f6',4:'#10b981',5:'#059669'}` and matching light background map `{1:'#fee2e2',2:'#fef3c7',3:'#dbeafe',4:'#d1fae5',5:'#a7f3d0'}`.
- Action buttons suppressed entirely for `CHECKING, ERROR, SKIPPED_TODAY, PAID_IN_FULL`. OPTIMIZATION-status cards get an "⚡ Open Reprice Popup" button in addition to the always-present "🌐 View in Portal" button. OPTIMIZATION cards are inserted at the top of the results list; everything else appends to the bottom.
- Summary row: counts of OPTIMIZATION / TRAP / (NO_SAVING+WLT+SKIPPED_TODAY combined) / PAID_IN_FULL / ERROR, plus a total-savings dollar figure summed over OPTIMIZATION rows only.
- Activity log panel: toggle button, monospace terminal styling, auto-scroll to bottom, entries rendered as `<time> [<status>] <bookingId> <step>: <detail>` with status-colored bracket (`OK`→green, `ERROR`→red, else amber).
- CSV export button requests `getAutoSaveCSV`, downloads as `cruiseintel_<YYYY-MM-DD>.csv` using the CURRENT export moment's date (not the stored `autoSaveTime`).
- Booking-ID textarea persists live to `chrome.storage.session` on every keystroke and restores on popup open.

---

## 8. Export spec

### 8.1 CSV format — MUST be identical, field-for-field, between the extension and the platform, AND must include every field Excel has (see the fix in §10)

Header row (12 columns — this is the FIXED, complete set both implementations must produce):
```
Booking ID, Cruise Line, Status, Net Saving, Old Total, New Total, Category, New Category, Note, Lost Packages, Lost Fares, Re-addable Fares, Gained Fares, Confidence, Checked At
```
(That is 15 columns once Lost Fares/Re-addable Fares/Gained Fares/Confidence/Checked At are included — see §10.3; do not ship the historically-incomplete 12-column CSV described below as your final target, it is documented here only so you understand what NOT to copy.)

Per-row formatting rules to follow: currency fields (`net_saving`, `old_total`, `new_total`) formatted with exactly 2 decimal places, no currency symbol, no thousands separator. `price_category`/`new_price_category` fall back to `""` when absent. List fields (`lost_pkg_names`, `lost_fares`, `re_addable_fares`, `gained_fares`) joined with a bare pipe `"|"` (no surrounding spaces) for consistency with the extension's original CSV format. `checked_at` as full ISO-8601, or empty string if unset. `confidence` written as a raw integer. Quote every field (CSV `QUOTE_ALL` semantics) — matches the extension's own `'"' + v.replace(/"/g,'""') + '"'` escaping.

### 8.2 Excel (`.xlsx`) format — 17 columns, exact

```
Booking ID, Cruise Line, Status, Confidence, Old Total ($), New Total ($), Price Drop ($), OBC Change ($),
Net Saving ($), Category, New Category, Note, Lost Packages, Lost Fares, Re-addable Fares, Gained Fares, Checked At
```
List fields joined with `" | "` (WITH surrounding spaces — deliberately different punctuation from the CSV's bare `"|"`, preserve this difference). No number formatting is applied to currency cells — raw floats, the `"($)"` suffix in the header is the only visual currency indicator.

Row background fill by status (hex, solid fill): `OPTIMIZATION` `C6EFCE` (green), `TRAP` `FFEB9C` (amber), `NO_SAVING` `F2F2F2` (grey), `ERROR` `FFC7CE` (red), `WLT`/`PAID_IN_FULL`/`SKIPPED_TODAY` all `DDEBF7` (light blue — these three are visually indistinguishable from each other, distinguished only by the Status column text). Header row fill `1F3864`, white bold Calibri 10pt font. Data font Calibri 10pt; bold it specifically in the Booking ID, Status, and Net Saving ($) columns. Center-align Status, Confidence, Category, New Category columns; left-align everything else; all cells get a thin border on all four sides. Column widths (17 values, A–Q): `[14, 12, 14, 10, 13, 13, 13, 13, 13, 10, 12, 30, 24, 24, 24, 24, 20]`. Freeze header row (`A2`), apply an auto-filter over the full used range.

Sort rows by status priority first (`OPTIMIZATION=0, TRAP=1, WLT=2, PAID_IN_FULL=3, NO_SAVING=4, SKIPPED_TODAY=5, ERROR=6`, unknown→9), then by `net_saving` descending within each status group.

Add a second "Summary" sheet with rows, in order: Total Checked, Optimizations, Traps, WLT, Paid In Full, No Saving, Skipped Today, Errors, and "Total Savings Found ($)" (sum of `net_saving` over OPTIMIZATION rows only, rounded 2dp). Bold labels.

### 8.3 Export triggers

Extension: CSV only, autosaved to `chrome.storage.local` after every booking, downloaded on demand via the popup's export button as `cruiseintel_<today's-date>.csv`.

Platform: CSV/Excel are both on-demand, explicit exports — CLI `scan`/`watch` subcommands accept `--output`/`-o` (CSV path) and `--excel`/`-x` (xlsx path) flags; the GUI's export button writes both `./reports/scan_results.csv` and `./reports/scan_results.xlsx` unconditionally (fixed filenames, always overwritten) with no file picker.

---

## 9. Python platform specifics

### 9.1 `BaseScraper` (Playwright lifecycle)

Chromium only. `start(headless=None)`: launches with `settings.browser_headless` unless overridden per-call; supports proxy config (`proxy_url`/`proxy_username`/`proxy_password`); restores `storage_state.json` from `settings.browser_user_data_dir` if present (explicit Playwright `storage_state` snapshot/restore, NOT relying on a Chromium user-data-dir, because Chromium itself marks real SSO session cookies like ESPRESSO's `iPlanetDirectoryPro`/`LtpaToken2` as session-only and wipes them from its own on-disk cookie store on clean shutdown even inside a persistent profile — explicit snapshot/restore bypasses that regardless of how the cookie is flagged); sets `page.set_default_timeout(settings.scraper_timeout_ms)` (default 30000ms). `stop()`: saves `storage_state` back to disk, then closes context/browser/playwright, swallowing and logging errors at each step, always nulling references in `finally`.

Implement action logging (`log_action`) — every navigate/search/click/API-call step recorded in-memory and optionally appended to `raw_dump_dir/actions.jsonl`, with an optional `on_action` callback (used by the GUI's activity-log panel). Implement optional full-capture mode (`capture_everything`): page snapshots (HTML + a generic table/label-value/body-text extraction — do NOT hardcode per-portal selectors for this, since the point is to capture whatever's there for offline analysis) and network traffic capture (request/response bodies up to 200KB, else marked truncated). Implement `dump_failure_snapshot` — always active whenever `raw_dump_dir` is set (not gated on `capture_everything`), since a failure is exactly the moment you need to see what the browser was looking at; save a full-page screenshot, HTML, and a small JSON with the URL and error.

**ARIA structure-drift monitoring** (`check_structure_drift(name, selector)`, applies to every scraper, not one cruise line specifically): Playwright's own `locator.aria_snapshot()` (zero new dependency) captures a structural snapshot of a selector's accessibility tree. On first call for a given `name`, write it to a baseline file (e.g. `data/structure_baselines/<name>.yaml`) and return "baseline created." On every later call, compare today's snapshot against the saved baseline; if it no longer matches, log a warning and record a `structure_changed` action — an early-warning signal that a portal's DOM has shifted in a way that could silently break a hand-written selector, without waiting for an actual scrape failure to surface it. Gate this to run at most once per scraper session per `name` (track checked names in a set on the instance) so it doesn't re-run on every retry within the same run. Wire it into each scraper's search/lookup step, watching the selectors that step depends on (e.g. ESPRESSO's search input and search button). This is a diagnostic aid, not a gate — never block or fail a scrape because structure drifted; only log it.

### 9.2 `EspressoScraper`/`NclScraper`

Implement `check_booking(booking_id, capture_market_data=False) -> BookingResult` per §4 and §5 respectively. `EspressoScraper.check_booking` wraps its per-attempt logic in `retry_async` (module `utils/retry.py`, §9.5) and does NOT itself catch the final exception — let it propagate to the caller. `NclScraper.check_booking` never raises — every code path (including the outer exception handler) returns a `BookingResult`, because the mandatory `finally`-block unlock must run regardless.

### 9.3 `services/cache_service.py` — `CacheService`

`__init__(ttl_hours=None)`: `self.ttl = timedelta(hours=ttl_hours or settings.cache_ttl_hours)` (default 12). `get(cruise_line, booking_id)`: builds key, looks up `CacheEntry`; miss → `None`; expired (`utcnow() > expires_at`) → delete the row, return `None`; else return `{"hours_ago": round(...,1)}` computed as described in §6.2. `set_no_saving(cruise_line, booking_id)`: upsert, extending `expires_at` in place if the key already exists. `clear_all()` / `cleanup_expired()`: bulk delete helpers, each returning the deleted row count. The *policy* of which statuses get cached lives in the caller (`BookingService`), not in `CacheService` itself — `CacheService` is a dumb keyed TTL store.

### 9.4 `services/booking_service.py` — `BookingService` (the orchestration core)

This is the Python equivalent of `background.js`'s `runBatch`. Implement:

- **One long-lived live scraper** (`self._live_scraper`), reused across scans for the same cruise line — replaying saved session cookies into a brand-new browser process is what triggers ESPRESSO's bot detection, so keep one continuous browser instance from login through every subsequent scan. `get_or_create_scraper(cruise_line, headless=None)`: reuse if the cruise line matches; otherwise stop the old one and start fresh. `close_live_scraper()`: call on app shutdown.
- **`has_live_session(cruise_line)`** — see the required bug FIX in §10 (do not just implement the naive version).
- **`check_login(cruise_line, timeout_minutes=15.0)`**: always opens/reuses the live scraper with `headless=False` (see the guardrail in §11), navigates to the cruise line's landing URL, polls every 5 seconds up to the timeout using a URL heuristic for NCL (`"login"`/`"signin"` absent from URL) or the scraper's own `_check_login()` for ESPRESSO. Leaves the browser open afterward regardless of outcome.
- **`start_scan(booking_ids, cruise_line, on_progress=None, bypass_cache=False, raw_dump_dir=None, capture_market_data=False, capture_everything=False, on_action=None, keep_browser_open=False) -> ScanJob`**: builds a `ScanJob`, registers it, persists the initial row, fires the actual work as a detached `asyncio.create_task` (not awaited), returns the job immediately (still `RUNNING`, `progress_done=0`).
- **`_run_batch(...)`** — the actual worker loop: for each booking ID, check the cooperative stop flag first; check cache (skip-and-record `SKIPPED_TODAY` on hit, but do NOT persist cache hits to the `bookings`/`price_history` tables); on a cache miss, call `scraper.check_booking(...)`, catching any exception into `make_error_result`; persist every live result to `bookings` and (if `old_total > 0`) `price_history`; persist market-data captures when requested; cache-write only for `status == NO_SAVING` and only when `not bypass_cache`. Track consecutive `ERROR` results; once `consecutive_failures >= scraper_cooldown_after_failures` (default 3), sleep `scraper_cooldown_seconds` (default 120.0) and reset the counter; otherwise sleep a **randomized** `random.uniform(scraper_interbooking_delay_min_s, scraper_interbooking_delay_max_s)` (default 4.0–9.0s) between bookings — deliberately mimicking a human agent's pace rather than a fixed interval, to avoid degrading portal sessions/tokens. On any fatal exception in the loop, mark the job `FAILED` and, if `keep_browser_open`, close the live scraper so the next scan starts clean. In `finally`, stop the scraper if it was NOT meant to stay open, mark `completed_at`, persist the final job row, and clear the stop flag.
- **`stop_scan(job_id)`**: cooperative — flips a flag checked only at the top of the next loop iteration.
- **`get_all_bookings(cruise_line=None, limit=100)`** / **`get_price_history(booking_id)`**: read-only DB projections (see §6.2 tables) — `get_all_bookings` orders newest-first and excludes `error`/`lost_pkg_names` from its projected dict; `get_price_history` orders oldest-first, no limit.

### 9.5 `utils/retry.py` — `retry_async`

```python
async def retry_async(fn, *args, attempts=3, delay_s=3.0, backoff=1.5, label="") -> T:
```
Catches broad `Exception`; on a non-final attempt logs a warning and sleeps `current_delay` (then multiplies it by `backoff`, i.e. exponential: 3.0s → 4.5s → 6.75s...); on the final attempt logs an error; after the loop, re-raises the last exception. Uses plain stdlib `logging`, NOT structlog (this asymmetry is intentional — preserve it).

### 9.6 `utils/logging.py` — structlog setup

`setup_logging(level="INFO", log_file="")`: shared processor chain `[merge_contextvars, add_log_level, TimeStamper(fmt="iso"), StackInfoRenderer(), format_exc_info]`; console output via `ConsoleRenderer(colors=stderr.isatty())` routed through a `logging.StreamHandler(stderr)`; if `log_file` is set, add a second `FileHandler` rendering `JSONRenderer()` output (JSON to file, colorized human-readable to console, sharing the same base processors). `get_logger(name)` returns `structlog.get_logger(name)`. All application logging must go through this — including the GUI, which must independently call `setup_logging` at its own entrypoint (it does not inherit CLI configuration).

Use exactly this dotted lowercase event-naming convention with structured kwargs (not interpolated strings) throughout: e.g. `logger.info("espresso.navigate_home", booking_id=booking_id)`, `logger.warning("ncl.unlock_failed", msg=...)`, `logger.error("batch.fatal", job_id=..., error=...)`. Reproduce the full event-name inventory implied by §4/§5/§9 above (navigate/search/category/api_calls/result for ESPRESSO; navigate/search/booking_info/addons/edit_mode/no_cheaper/select/result/unlock for NCL; batch.stopped/cached/checking/error/cooldown/fatal/complete/stop_requested and login_check.success/waiting/timeout for the booking service; cache.set for the cache service).

### 9.7 `config/settings.py` — full field list with exact defaults

| Field | Default |
|---|---|
| `app_name` | `"Cruise Intelligence System"` |
| `app_version` | `"1.0.0"` |
| `debug` | `False` |
| `api_host` | `"127.0.0.1"` |
| `api_port` | `8000` |
| `database_url` | `"sqlite+aiosqlite:///./cruise_intel.db"` |
| `browser_user_data_dir` | `<platform-dir>/browser-profile` |
| `browser_headless` | `True` |
| `scraper_timeout_ms` | `30000` |
| `scraper_retry_attempts` | `3` |
| `scraper_retry_delay_ms` | `3000` |
| `scraper_login_timeout_ms` | `60000` |
| `scraper_category_poll_timeout_ms` | `8000` |
| `scraper_interbooking_delay_min_s` | `4.0` |
| `scraper_interbooking_delay_max_s` | `9.0` |
| `scraper_cooldown_after_failures` | `3` |
| `scraper_cooldown_seconds` | `120.0` |
| `proxy_url` / `proxy_username` / `proxy_password` | `""` each |
| `cache_ttl_hours` | `12` |
| `espresso_home_url` | `"https://secure.cruisingpower.com/home"` |
| `espresso_base_url` | `"https://secure.cruisingpower.com/espresso/protected/reservations.do"` |
| `ncl_search_url` | `"https://seawebagents.ncl.com/tva/search/"` |
| `log_level` | `"INFO"` |
| `log_file` | `""` |

Support `.env` loading, case-insensitive env vars, singleton `settings = Settings()`.

### 9.8 FastAPI (`api/`)

Endpoints (prefix `/api`):
- `GET /api/health` → `{status:"ok", version, uptime_seconds}`.
- `POST /api/scan` → body `{booking_ids: list[str] (1-100), cruise_line: "ESPRESSO"|"NCL" default ESPRESSO}` → starts a scan, returns the job.
- `GET /api/scan/{job_id}` → job status/results, 404 if unknown.
- `POST /api/scan/stop` → body `{job_id}`, graceful/deferred stop, 404 if not running.
- `GET /api/bookings?cruise_line=&limit=100` → list of booking records.
- `GET /api/bookings/{booking_id}` → matching records, 404 if none.
- `GET /api/bookings/{booking_id}/history` → price history, 404 if none.
- `POST /api/export/csv` → body `{job_id, cruise_line}` → CSV text for that job's results; 400 if no `job_id` given.

CORS wide open (`allow_origins=["*"]`) — comment it as development-only. Single module-level `BookingService()` instance shared across requests (no per-request DI). Lifespan: `setup_logging` + `init_db()` on startup; no shutdown logic beyond a stub comment.

### 9.9 CLI (`main.py`) — 4 subcommands

- **`api`**: `--host`, `--port`, `--reload` → `uvicorn.run("api.main:app", ...)`.
- **`login`**: `--cruise-line` (default ESPRESSO), `--timeout-minutes` (default 15.0). Always runs headed (`headless=False`, hardcoded, no flag to override — see §11).
- **`scan`**: `--bookings` (comma-separated) or `--bookings-file` (blank/`#`-prefixed lines ignored, takes precedence over `--bookings`), `--cruise-line`, `--output`/`-o`, `--excel`/`-x`, `--capture-raw DIR`, `--capture-market-data`, `--capture-everything` (requires `--capture-raw`, defaults its dir to `"data"` if unset). Prints grouped results with status icons (`✅ OPTIMIZATION, ⚠️ TRAP, ⏭ WLT/NO_SAVING, ❌ ERROR, 💳 PAID_IN_FULL, ⏩ SKIPPED_TODAY`) in the order `OPTIMIZATION, TRAP, WLT, PAID_IN_FULL, NO_SAVING, SKIPPED_TODAY, ERROR`, then a total-savings summary line.
- **`watch`**: `--bookings`/`--bookings-file`, `--cruise-line`, `--interval-minutes` (default 60), `--duration-hours` (default 8.0, `0` = run until Ctrl+C), `--max-passes`, `--output-dir` (default `"watch_runs"`), plus the same capture flags. Always passes `bypass_cache=True` to every pass. Writes a per-pass CSV/xlsx and appends OPTIMIZATION/TRAP hits to `<output_dir>/alerts.log` in the format `f"{started.isoformat()}Z  {status:12s}  {booking_id:12s}  ${net_saving:>10.2f}  {note}"`.

`run.py`: a PyInstaller entrypoint shim that path-fixes `sys.path` and calls `main.main()`. `easy_menu.py`: a plain-language console menu (Log in / Check now / Watch overnight / Edit booking list / Exit) that drives the same async CLI functions via `SimpleNamespace` in place of `argparse.Namespace`. When building this, make sure every attribute the underlying `_run_scan`/`_run_watch` functions read off `args` is actually set on the `SimpleNamespace` you construct (including `capture_market_data`/`capture_everything`) — do not reproduce a version that omits them.

### 9.10 PySide6 desktop GUI (`gui/`)

Single window (`MainWindow`), no `QThread` anywhere — all async work (`check_login`, `start_processing`) runs through `qasync`'s `QEventLoop`, using coroutine methods decorated with both `@Slot()` and `@asyncSlot()`. `gui/main.py`: reconfigures stdout/stderr to UTF-8 before importing Qt (Windows defaults to cp1252, which can't encode the emoji this GUI prints), calls `setup_logging` independently, sets the high-DPI attribute BEFORE constructing `QApplication`, installs an asyncio exception handler that prints the full traceback, and drives the whole app via `with loop: loop.run_forever()` (never calls `app.exec()` directly).

`MainWindow`: booking-ID input + "Add to queue" / bulk textarea + "Add list", cruise-line selector, "Check login" button (must succeed before "Start" is allowed — enforce via `has_live_session`), "Start"/"Stop", a "Force live recheck" checkbox (maps to `bypass_cache`), a "Collect market data" checkbox (default checked), a "Capture everything" checkbox, a live activity-log panel fed by the scraper's `on_action` callback, a queue list (each row a custom widget showing `"<id> [<STATUS>]"` with a small remove "x" button only while still `QUEUED`), a 4-column results table (Booking ID / Status / Net Saving / Confidence) with per-status row coloring (OPTIMIZATION green, TRAP red, NO_SAVING yellow, ERROR light gray, everything else default), and an export button that writes both `./reports/scan_results.csv` and `./reports/scan_results.xlsx` unconditionally. Implement the two-phase `closeEvent` pattern (ignore → async-close the live browser session → `QApplication.instance().quit()` → accept) so the live Playwright session is always saved/closed before the process exits. Format net-saving text as `"+$X.XX saved"` / `"-$X.XX more expensive"` / `"$0.00"` — never a bare signed number. Sum "Total savings" over `OPTIMIZATION`-status rows only (a NO_SAVING row's `net_saving` can be negative, meaning repricing would cost more — summing that in would make totals misleadingly negative). Disable Start/Add/Login controls while a scan is running (running "Check login" concurrently with an active scan risks a second session knocking the first one out from under it on ESPRESSO). `queue_manager.py`'s `start_processing` always passes `keep_browser_open=True` to `BookingService.start_scan` (the GUI's whole point is a persistent shared session — the CLI's one-shot runs always pass `False`), and polls the shared `ScanJob` object every 0.5 seconds to detect newly-completed results and drive UI callbacks (accept this ≤0.5s latency as by design, not a bug to fix).

Note the separate-environment packaging requirement: because PySide6 ships files with paths long enough to exceed Windows `MAX_PATH` when nested under a deeply-pathed repo, the GUI launcher script should set up its venv at a short external path (e.g. `C:\cruisevenv\venv`), pinned to a specific recent Python version, installing `requirements.txt` plus `PySide6` and `qasync` explicitly (they are not in `requirements.txt` itself) plus `playwright install chromium`. The CLI/menu launcher uses an ordinary in-repo `venv/` and does not need PySide6/qasync at all.

**MSC in the same GUI**: MSC does not reuse `BookingResult`/`BookingService` (see §5A.0) — give it its own service class (an `MscLiveService`, holding its own long-lived MSC session, with `ensure_started()`/`stop()`/`check_login()`/`run_batch()`) and wire it into `MainWindow` as a sibling to the ESPRESSO/NCL path, branching the cruise-line selector, login-check, and start/results handling per selected cruise line rather than trying to force MSC's four-check-type result shape into the same table/export code as `BookingResult`. **Critical requirement, learned the hard way in the original build — build this in from day one, do not discover it via a live incident**: any service class that imports a module containing the business-logic checks (in the original, `msc_commands.py`) and then holds a long-lived process open across an entire GUI session will keep executing whatever version of that module was in memory at import time — editing the file on disk does NOT change what an already-running process executes. If the equivalent of a hot-reloadable command module exists in your rebuild, call `importlib.reload()` on it at the top of every method that (a) is the first thing invoked after a session starts (e.g. a login check) and (b) sits inside any loop that processes multiple bookings — this is exactly the fix that closed a real incident where a long-running GUI process kept silently executing pre-fix logic for days, showing a stale, wrong "opportunity" on a specific real booking, and later hard-failing an entire batch run with a `TypeError` from a renamed parameter that had already been fixed on disk.

### 9.11 Credential storage, history analysis, and new-portal onboarding tooling

**Credential storage** (`save_login.py`/`clear_login.py`, plus optional `.bat`/`.command` double-click wrappers for Windows/macOS): a menu-driven script, one per cruise line supported, using the `keyring` library (Windows Credential Manager / macOS Keychain / Linux Secret Service — `keyring` picks whichever is native) and `getpass()` so nothing typed is ever echoed to the screen, logged, or written to any file. Storage is encrypted by the OS itself and tied to the local device — there is no exportable artifact this produces; it cannot be copied to a server, USB drive, or another machine and read there. Strip bracketed-paste escape sequences some terminals wrap pasted text in (`\x1b[200~`/`\x1b[201~`) before saving, since a pasted password is a normal and expected input method here, not an edge case to leave unhandled. A cruise line requiring MFA at login (ESPRESSO) only gets its username/password pre-filled this way — saving a credential does not make MFA-gated login fully unattended; say so explicitly in the tool's own output so this limitation is never assumed away.

**History analysis** (`analyze_history.py`, read-only against the existing database/JSONL history — never collects new data): status/opportunity breakdowns, savings totals, hit rates by cruise line and price category, and frequent-pattern mining (via a classical association-rule-mining library — e.g. `mlxtend`'s `apriori`/`association_rules`, NOT machine learning) looking for above-baseline combinations (e.g. "category X + condition Y correlates with an opportunity at 3x the category's own baseline rate") worth a human's closer look. Dedupe mined rules that cover an identical set of underlying rows, keeping whichever has the simplest antecedent; always show raw occurrence counts alongside percentages, and flag small-sample rules (e.g. under 10 occurrences) rather than presenting them with the same confidence as a well-supported one.

**New-portal onboarding** (a classical, non-AI "smart field finder," per the project-wide no-AI rule in §0): a function that, given a live Playwright `page`, an ARIA role (textbox/button/etc.), and a list of keywords, enumerates every element of that role on the current page, reads each one's visible contextual attributes (id, name, placeholder, aria-label, title, a `data-qa`-style test-hook attribute if the portal uses one, associated `<label>` text), scores each element's combined context text against the keywords using a classical fuzzy-string-similarity function (e.g. RapidFuzz's `WRatio`), and returns the top-N candidates ranked by score, each with a suggested selector (prefer `id`, then a stable test-hook attribute, then `name`, falling back to a positional selector only when nothing else is present). This is the same category of heuristic a password manager's autofill already uses to guess "this is the username field" — plain scoring over attributes already on the page, not a model reading and understanding it. Wrap this in an interactive CLI that opens a real, visible browser at a given URL, lets a human navigate and log in themselves (the tool never fills in or clicks anything on its own), and on request ranks candidates for common categories (booking search box, username, password, submit button) or a custom role+keyword query — always to be verified against the real page before any suggested selector becomes actual scraper code, the same discipline already required for anything a session-recording tool like `playwright codegen` generates. Validate the scoring function's ranking logic against real, already-confirmed field attributes from your own existing scrapers (synthetic data is fine — no live browser needed for this) before ever trusting it on a genuinely unfamiliar portal.

---

## 10. Fixes to bake in from day one

These were bugs discovered the hard way in the original system. Get them right immediately in this rebuild — do not reintroduce them and then "fix" them later.

1. **ESPRESSO Mantine-migration selector fallback** (§4.1): always try both the legacy `#reservationid`/`#searchReservationBtn` selectors AND the Mantine `data-qa`/`aria-label` selectors, comma-joined, old-first, in every place the search box is touched.
2. **Login always runs in a visible (non-headless) browser window, hardcoded, with no flag to override it** (§9.9, §11). A hidden login window can't be logged into by a human, and MFA/SSO steps require a real window.
3. **`OBC_LOSS_MIN_RATIO = 3.0` downgrade rule** (§3.8, §3.9 branch 2): an on-paper-positive net saving must be downgraded from OPTIMIZATION to NO_SAVING whenever the raw price drop is less than 3× the absolute OBC being forfeited. Do not skip this branch or treat it as optional.
4. **The price-verification diagnostic (`read_top_prices`/paid-status check) must run only AFTER the real allocate + showRepriceModalCheck API calls, never before** (§4.7). Build this ordering into the code structure itself (e.g., make the diagnostic functions simply unreachable/unimportable before the API-call step in your control flow), not just as a comment, so this exact regression cannot recur.
5. **`has_live_session(cruise_line)` must actually verify the browser/page connection is alive** — this was a known, unfixed bug in the original: it only checked `self._live_scraper is not None and self._live_scraper.cruise_line == cruise_line`, a pure in-memory reference check that would still report `True` even if the underlying Playwright browser process had crashed or been closed externally. **Fix this in the rebuild**: have `has_live_session` additionally verify the browser is actually connected — e.g. check `self._live_scraper._browser is not None and self._live_scraper._browser.is_connected()` (Playwright's `Browser.is_connected()`), and treat a disconnected/crashed browser the same as "no live session" (returning `False`, and clearing the stale `_live_scraper` reference so the next `get_or_create_scraper` call starts fresh rather than reusing a dead handle).
6. **CSV export parity with Excel export**: the original CSV export was missing `Lost Fares`, `Re-addable Fares`, and `Gained Fares` (and also lacked `Price Drop ($)`/`OBC Change ($)`) versus the Excel export, purely because nobody had gotten around to adding them — not an intentional design choice. **Fix this in the rebuild**: make the CSV export include every field the Excel export includes (adjust column punctuation per §8.1/§8.2 as specified — bare `|` for CSV list fields, `" | "` for Excel list fields — but do not omit any field from either format).
7. **NCL's cheaper-category selection deliberately picks the smallest available price drop, not the absolute cheapest** (§5.2 step 9: sort by `resTotal` descending among qualifying categories, take the first). This is intentional conservatism, not a bug — preserve it exactly; do not "improve" it into picking the globally cheapest category.
8. **The WLT check on ESPRESSO must run strictly after the category-availability table has loaded**, never before (§4.4 step 5) — short-circuit via a sentinel/early-return, not an exception, since a legitimately waitlisted category should not burn a retry attempt.
9. **`easy_menu.py`'s constructed `SimpleNamespace` args must include every attribute the underlying scan/watch functions read** (`capture_market_data`, `capture_everything`, etc.) — the original had a latent `AttributeError` risk here from an incomplete namespace; make sure yours is complete.

---

## 11. Explicit guardrails/constraints — non-negotiable, verify these at the end

- **Login must always run in a visible, non-headless browser window.** A human must be able to see and interact with it (MFA, SSO redirects, CAPTCHAs). No code path — CLI flag, GUI button, config setting — may force login to run headless.
- **A human always makes the final reprice decision.** Neither the extension nor the platform may ever click, simulate-click, or otherwise trigger the actual "commit the new rate" controls: ESPRESSO's `#repriceModalAcceptBtn1`/`#repriceModalAcceptBtn2` ("Continue with New Rate"), or NCL's actual booking-save/"STORE" action. Automation may open the review UI (ESPRESSO's native reprice popup via the read-only allocate call + "Continue"; NCL's category picker with a target category pre-selected) and stop there, leaving the final confirming click to the agent.
- **NCL edit-mode must always be cancelled if the flow does not complete successfully**, via the mandatory `finally`-block unlock described in §5.3, on every code path that ever set `in_edit_mode = True`. A booking left locked for 30 minutes because of a crash is an unacceptable outcome.
- **Price-verification/diagnostic logic must never gate before the real API calls on ESPRESSO** — see §4.7 and fix #4 above.
- **No AI/LLM/API key anywhere in this system, for anything, including one-time onboarding tooling.** See the full statement and rationale in §0. This is a blanket rule, not scoped by risk level or supervision — do not build an exception for a "one-time, human-supervised, never touches live scans" case; the classical techniques in §5A.7 and §9.11 are the required substitute, not an optional one.
- **MSC's `PRICE_MATCH`/`DISCOUNT_ADD`/`DISCOUNT_TIER_UPGRADE`/`VOYAGERS_SELECTION` checks must stay independent, deterministic, and never AI-assisted** — same rule as above, called out specifically here because MSC's checks are the ones most likely to look like a good fit for fuzzy/LLM-based judgment (senior-discount blind spots, ambiguous rate-tab names). They are not: every one of the rules in §5A.4 and §5A.7 is a concrete, testable branch, and must stay that way.

---

## 12. Explicitly out of scope — do NOT build these now

- **Any form of fully-autonomous reprice submission, or autonomous discount/price-match changes on MSC.** Do not implement, even as an opt-in "advanced" feature, a code path that clicks ESPRESSO's "Continue with New Rate", NCL's booking-save/STORE action, or that submits an MSC discount/price change without a human placing the actual phone call. This is a hard product boundary, not a missing feature to fill in later.
- **Cruise lines beyond the four specified in §0 (ESPRESSO, NCL, GoCCL, MSC).** ESPRESSO, NCL, GoCCL, and MSC are all in scope for this rebuild and specified in full above (§4, §5, §5A, §5B) — do not treat any of them as a stretch goal. Do architect the calculator/scraper layers so that adding a FIFTH cruise line later is a matter of (1) a new `adapter_<line>.js` following `adapter_espresso.js`'s pattern (for a line that gets a Chrome-extension adapter at all — MSC/GoCCL don't have one today), (2) a new `platform/scraper/<line>.py` extending `BaseScraper`, (3) a new `calculate_<line>()`/`evaluate_<line>_booking()` function in `core/calculator.py`/a dedicated `core/calculator_<line>.py`, (4) doc updates — future candidates mentioned by the project owner include the OneSource portal (Princess, Cunard, Holland America) and Silversea (understood to sit under the existing ESPRESSO/CruisingPower umbrella rather than needing a wholly separate adapter) — but do not implement any of these five further-out lines now.
- **A scheduler subsystem.** An APScheduler-based `scheduler/` module (periodic cache cleanup + a stub watchlist check) existed early on and was deliberately deleted as dead code on 2026-08-11 after confirming zero references anywhere in the running application. Do not build a `scheduler/` module, do not add APScheduler to the dependency list, and do not wire any periodic-job runner into the FastAPI lifespan or any other startup path in this rebuild.
- **Any AI/LLM-based approach to any part of this system.** See §0 and the guardrail above — this includes an LLM-based "watcher" layer proposed but deliberately deferred by the project owner for a future continuously-scanning MSC server; do not build any part of that now either, and do not substitute an LLM for any of the classical techniques specified in §5A.7/§9.11 even as a "better" alternative.

---

## 13. Acceptance checklist — self-verify against every item below before declaring the rebuild done

- [ ] `manifest.json` matches §7.1 exactly (permissions, host_permissions, single 48px icon, no CSP override).
- [ ] `calculator.js` and `core/calculator.py` produce byte-identical `net_saving`, `status`, and `note` strings given identical raw invoice-item input, for both ESPRESSO and NCL.
- [ ] `calculate_espresso`/`calculateESPRESSO` applies the `OBC_LOSS_MIN_RATIO = 3.0` downgrade rule exactly as in §3.9 branch 2 (net positive + OBC forfeited + price drop under 3× the OBC lost → NO_SAVING, not OPTIMIZATION).
- [ ] The perk-trap branch (§3.9 branch 1) correctly downgrades a positive net to TRAP whenever it's smaller than the value of a lost package.
- [ ] `calc_confidence`/`calcConfidence` reproduces the exact point table, clamp range `[-2,6]`, lookup table, and the two post-lookup safety caps in §3.11, in that order.
- [ ] The re-addable-fare regex set is exactly `email|bonus|promo|loyalty|coupon` (case-insensitive, substring match), and lost fares are correctly split into `lost_fares` vs `re_addable_fares`.
- [ ] The ESPRESSO price-check/paid-status diagnostic (§4.6) runs only AFTER both the allocate and showRepriceModalCheck fetch calls have completed — never before, and this ordering is structurally enforced, not just commented.
- [ ] The ESPRESSO flow reads the price category with BOTH the hidden-field and CSS-fallback selectors, and polls past the unrendered-template-placeholder state rather than accepting `{{...}}` as a value.
- [ ] The ESPRESSO WLT check runs strictly after the category-availability table has loaded, and short-circuits via early return rather than an exception.
- [ ] The two real ESPRESSO API calls match §4.5 exactly: same endpoints, same body encoding, same headers, 300ms delay between them, executed via the page's own `fetch()`/JS context (not an out-of-page HTTP client).
- [ ] Neither the extension nor the platform contains any code path that clicks or simulates clicking ESPRESSO's `#repriceModalAcceptBtn1`/`#repriceModalAcceptBtn2`, or NCL's actual save/STORE action.
- [ ] NCL edit-mode entry sets an `in_edit_mode` flag, and a `finally` block unconditionally attempts the cancel-edit cascade (§5.3) whenever that flag is true — verified across all three exit paths: normal completion, the no-cheaper-category early return, and any raised exception.
- [ ] NCL's paid-in-full short-circuit happens strictly BEFORE edit mode is ever requested, so a paid booking is never locked.
- [ ] NCL's cheaper-category selection sorts descending by `resTotal` among qualifying rows and picks the first (smallest drop), not the globally cheapest category.
- [ ] The cache key format `cache_<cruiseLine>_<bookingId>` and 12-hour TTL match exactly between `chrome.storage.local` and the SQLite `cache` table, and only `NO_SAVING` results are ever cached in either implementation.
- [ ] The SQLite schema has all 5 tables (`bookings`, `price_history`, `scan_jobs`, `cache`, `market_data`) with every column listed in §6.2, including the composite index on `market_data`.
- [ ] `has_live_session` in the rebuilt `BookingService` actually verifies the underlying browser connection is alive (e.g. via `Browser.is_connected()`), not merely that the Python reference is non-`None` — this is a deliberate fix versus the original's known bug.
- [ ] CSV export includes every field the Excel export includes (Lost Fares, Re-addable Fares, Gained Fares, Price Drop, OBC Change, Confidence, Checked At), closing the gap that existed in the original.
- [ ] CSV list-fields use bare `|` and Excel list-fields use `" | "` (space-pipe-space) — the punctuation difference is intentional and preserved.
- [ ] Excel export's status-based row coloring, sort order, column widths, and Summary sheet match §8.2 exactly.
- [ ] Login (CLI `login` subcommand, `BookingService.check_login`, and the GUI's "Check login" button) always launches a visible, non-headless browser with no override flag anywhere.
- [ ] `retry_async`/`retry()` implement the exact attempt counts and delay/backoff behavior specified in §4.8 and §9.5 (JS: flat 3000ms × 3 attempts; Python: 3.0s→4.5s exponential backoff ×1.5 across 3 attempts).
- [ ] No code path anywhere implements or exposes an autonomous reprice-submission action; every automated flow stops at presenting the review UI/data to a human.
- [ ] The repo layout matches §2 file-for-file (every listed module exists; nothing critical is missing).
- [ ] MSC's senior-discount filter requires `senior_count >= 2`, not "every passenger is 65+" and not "any passenger is 65+" (§5A.4) — verified against a single-senior-passenger test case (must NOT be eligible) and a 2-seniors-plus-grandchildren test case (must be eligible).
- [ ] MSC's `DISCOUNT_ADD` downgrades a `SENIOR DISCOUNT` option to `INSUFFICIENT_DATA` whenever the booking's sailing length isn't in the standard NCF reference table (§5A.4's verifiability gate) — never reports it as a confident `OPPORTUNITY` in that case.
- [ ] MSC's `PRICE_MATCH` check has TWO independent hard gates to `NO_OPPORTUNITY` — `is_paid_in_full` (§5A.6) and `final_payment_date_passed` (§5A.4) — checked before any price math, and scoped to `PRICE_MATCH` only (the other three MSC check types are unaffected by either gate).
- [ ] MSC's rate-tab matching has all SIX tiers (§5A.7), in order, with Brochure Rate excluded before any tier runs; the RapidFuzz fallback tier (6) requires both a score floor and a gap-to-second-place floor, and does not change the outcome of any tier-1-through-5 test case.
- [ ] GoCCL's `check_booking` never returns a confirmed `PRICE_MATCH`/`OPTIMIZATION`-with-full-confidence result — only an UNCONFIRMED candidate at capped confidence (§5B.2), and `guests_count_verified` is `False` (with an explicit "unverified" note) for every real call site that doesn't pass a confirmed occupancy.
- [ ] No file anywhere in the rebuilt system calls an LLM/AI API or reads an AI-provider API key — grep the finished codebase for anything resembling an LLM client import or an `_API_KEY` config field tied to an AI provider; it should find nothing (this project's own no-AI rule, §0).
- [ ] The classical field-finder (§9.11) ranks a real, known field correctly above plausible decoys using only RapidFuzz scoring over element attributes — verified with a unit test using synthetic data mirroring a real confirmed field from one of this system's own scrapers, not just eyeballed against a live page.
- [ ] **NCL compares the SAME category, not a cheaper different one** (§5.2-CORRECTED point 1) — there is no cross-category search or "cheapest but not absolute cheapest" sort anywhere in the NCL flow, and the booking's own category's grid `ResTotal` is read without any click.
- [ ] **NCL installs ONE persistent `page.on("dialog")` handler before any navigation** (§5.2-CORRECTED point 2), branching on `dialog.type()`: `beforeunload`→accept, `confirm`→accept, everything else→dismiss+log. No per-action `page.once` dialog handlers anywhere.
- [ ] **NCL's cancel-edit clicks `#dialog_confirm_ok` after `#res-edit-cancel`** (§5.2-CORRECTED point 3) — verified by confirming the booking is genuinely out of edit mode afterward, not just that the first click succeeded.
- [ ] **NCL's category selection scrolls `.slick-viewport` directly and re-queries rows after each scroll** (§5.2-CORRECTED point 4) — verified against a booking whose target category sits beyond the ~23 initially-rendered rows. Does NOT depend on `$(el).data('SlickGrid')`, and does NOT require `HasAvailability` for the booking's own category. Returns a structured failure reason, never a bare boolean.
- [ ] **NCL login checks for an existing valid session first, and clears cookies before submitting the form otherwise** (§5.2-CORRECTED point 5); a rejected login is detected by the password field still being present, not by scanning for error words (the portal shows none).
- [ ] **NCL's addon table is parsed by header text into `{guest, name, qty}`** (§5.2-CORRECTED point 6) — verified against a real 3-column `Guest Name | Addon Name | Quantity` table, with a test that would fail if columns were read positionally.
- [ ] **`calculate_ncl` blocks any reprice that would lose `LATRIPLE`** (§5.2-CORRECTED point 7) — hard gate before status assignment, returns `TRAP`, and correctly allows the kept/gained/never-present cases. Promo parsing handles both comma- and pipe-separated strings.
- [ ] `navigate()` retries transient navigation failures with backoff but re-raises dead-browser errors immediately — and **no mutating click (confirm / cancel-edit / submit) is wrapped in retry logic anywhere**, since a retried submit could double-submit against a real booking.
