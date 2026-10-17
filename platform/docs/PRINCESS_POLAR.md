# Princess Cruises — POLAR Online (OneSource)

Everything known about repricing Princess bookings, with where each fact
came from. Started 2026-09-16, rewritten 2026-10-15 from Neon's recordings,
two HAR files, his screenshots, and Princess's own help pages.

**Status:** decision logic built and tested (`core/calculator_princess.py`,
`core/princess_packages.py`). Scraper NOT built. Princess is not yet a
`CruiseLine` (adding it before the scraper would put a half-built tab in the
GUI).

---

## 1. Hard rules for any Princess automation

1. **Never click:** CANCEL BOOKING (it sits directly above REBOOK OR REFARE
   on Booking Information), RESET (on DEMO32 it left "ROLE-0006E INVALID
   DATA ENTERED" and flipped Vacation Protection to Platinum), SUBMIT
   CHANGES, the CANCEL button on any Special Requests row, Select, and
   anything past PRICING DETAIL. CONFIRMATION (wizard step 8) is the commit.
   Leave with EXIT / MAIN MENU.
2. **Never hand-build a POLAR post.** Every step is one form post to
   `POST /polaronline/pres` (a CICS web gateway: `DFH_STATE_TOKEN`,
   `DFH_ENTER`, `DFH_PFnn`, `DFH_NEXTTRANSID.n`), and every page re-posts ALL
   its fields — the REBOOK step re-sends passenger names, phone and ages.
   Drive the real page; read the data out of what comes back.
3. **Never use positional selectors** (`tbody > tr > td …`) near CANCEL
   BOOKING. Field names carry a per-screen prefix (`F120240006_CMRCBI-BOOKNUM`)
   — match the stable suffix (`[name$="-BOOKNUM"]`).
4. **Never use "Category Fares – Best Buy" prices as cash:** the page marks
   them `* = Price net of On Board Credit`.
5. **Credentials only via the OS keyring.** A HAR recorded from the OneSource
   login page contains the password in plain text (the login is a JSON post
   to `gw.api.princess.com/pcl-web/internal/osmf/p1.0/login`); "sanitized"
   export strips cookies, not request bodies. Record HARs after logging in.
6. **Akamai Bot Manager runs on every POLAR page** (`/_bm/get_params` + a
   sensor script) — the family that blocks ESPRESSO headless. Headless must
   be tested (windowed vs headless, field by field), never assumed.

## 2. The path (identical in all six 2026-10-15 recordings)

| # | Screen (page title code) | How | Reads |
|---|---|---|---|
| 1 | Booking Search (RCBI) | booking number → SEARCH | — |
| 2 | **Booking Confirmation (RCBD)** — landing, read-only | | category, status, ship, sail date, CRUISE FARE INFORMATION, PAYMENT HISTORY |
| 3 | Special Services (RCTB) | SPECIAL REQUESTS / GIFTS (`DFH_PF19`) | `showSpecialRequests(...)` lines — "Princess Plus" |
| 4 | Booking Information (RCBA) | BOOKING INFORMATION (`DFH_PF13`) | offer code (`CMRCBA-COUPON`, e.g. PLUS2) |
| 5 | REBOOK OR REFARE = "Change Fare Option" (`CMRCBA-REBOOK=F`) → REBOOK | | — |
| 6 | Voyage Pricing By Ship Zone (RDIM) → SEARCH BY CATEGORY LIST | | — |
| 7 | **Category Fares – Best Buy (RDIC)** | | `displayRow(code, statFIT, statGroup, priceFIT*, priceGroup, promo, …)` |
| 8 | Fare Comparisons (RDLF) — promo list | FARE COMPARISONS (`DFH_PF21`) | promo code, expiry, description, advisory |
| 9 | Important Notices (RDIA) | tick `RECAPFLAG` → SAVE & CONTINUE | — |
| 10 | **Category Fare Comparisons (RDLK)** | two promos side by side | fare per category per promo |
| 11 | **Pricing Detail (RDLE)** | pick category → SAVE & CONTINUE | itemised quote; "Important" FCC pop-up (Ok) on arrival |
| — | EXIT | | |

Every navigation fires a `beforeunload` dialog.

## 3. The category

Princess's page script: `productVal = trim(product.substring(0,2))` — **a
row's category is its first two characters**. "B43 Balcony" submits `B4`;
"DF4 Balcony" submits `DF`. Neon does the same by hand: Ctrl+F the
category, take the row that *begins* with it ("MB4 Suites" contains B4 and
is never it). The page also hard-codes the booking's own category to pre-tick
it — a free cross-check. Refuse on no row or two rows sharing a prefix
(`find_category_row`).

## 4. The money

**Booking Confirmation, CRUISE FARE INFORMATION** (all guests):
`Fare − Base Fare = Req'd Cruise Fees + Incld Gov't Taxes`, and
`Gross = Fare + Packages (+ Services …)`. MN5VMM: 2,862 − 2,062 = 800 =
540.76 + 259.24. DEMO12: 6,358 + 176 = 6,534.
PAYMENT HISTORY: `Total Received + Gross Payment Due = Gross`
(DEMO12 6,493 + 41 = 6,534). Gross Payment Due 0 = paid in full.

**Pricing Detail, per passenger:** `Fare + Transfers/Packages = Gross Fare`;
`Fare − Base = fees + taxes`. DEMO33 KKS DF4: 3,405 − 2,997 = 408 = 213.03 +
194.97. DEMO32 KKS IC: 1,329 + 88 = 1,417; 1,329 − 979 = 350.
POLAR's help: Fare = "the full brochure rate"; Gross Fare = "the total cost
per person, including all charges"; NCF = port charges inside the fare.

**Compare GROSS to GROSS.** Commission is never read.

## 5. Packages — Standard / Plus / Premier

**Neon's rule (2026-10-15):** a Plus booking is priced on the Plus rate, a
Premier booking on the Premier rate, a Standard booking on the Standard rate.
Different packages are different products, never netted
(`fares_are_comparable`).

**Where a booking's package shows** (`booking_tier`, highest wins):
- Special Requests / Gifts: "Princess Plus" / "Princess Premier" lines
  (DEMO12: Princess Plus 1,120.00 × 2 = 16 nights × $70).
- Offer code (POLAR help page `coupon_code_content.html`): **PLUS2** Standard
  → Plus, **PREMIER** Standard → Premier, **PLS2PREM** Plus → Premier,
  OLDPLS2PRM "Plus 60" → Premier; **PLUSUP / PREMUP** are the 3rd/4th-guest
  ("uppers") versions.
- Promo descriptions: "… – PPLUS", "… – PREMIER", "… – STANDARD",
  "UPGRADE STANDARD TO PLUS 70" (M1E, *Automatic*).

**Plus/Premier sit INSIDE the fare.** Princess: the packages "are included in
your total cruise fare when selected". DEMO12 proves it: received + due =
gross 6,534 with Services 0.00, and the FIT quote (M1E applied
automatically) reproduces 6,534 to the cent. The Special Requests line is the
record of the package, not an extra charge. The group quote showed it the
other way round: 4,079 = group price 2,959 + 1,120.

**Package facts** (princess.com, read 2026-10-15; purchases on/after
22 Jul 2025, pre-cruise ≥ 96 h): Plus $70 pp/day ($75 Sun/Star), Premier $105
($110); +$5 onboard or inside 96 h. Applies to guests 1 and 2; guests 3/4
only the same package. Plus → Premier = pay the difference "based on the
original package price". Pre-22-Jul-2025 purchases keep older benefits.
Premier's shore-excursion credit ($100/$200/$300 by length) is not cash.
Refunds on removing a package are not documented.

## 6. Promo codes

"A three-digit promotion code" (POLAR help, `rdlf_aval_content.html`).
Seen so far:

| Code | Description | Source |
|---|---|---|
| KKP / NCC | CYBER SUMMER LTO – PPLUS | DEMO04 |
| KKS / NSC | CYBER SUMMER LTO – STANDARD | DEMO04, DEMO33 |
| KNK / NRE | CYBER SUMMER LTO – PREMIER | DEMO04 |
| KFK / NFK | CYBER SUMMER LTO – SANCTUARY | DEMO04 |
| 2FG | CYBER SUMMER LTO – INSTANT SAVINGS (Automatic) | DEMO33 |
| RG1 | LAST MINUTE DEALS – STANDARD (NBO) | DEMO12 |
| M1E | UPGRADE STANDARD TO PLUS 70 (Automatic) | DEMO12 |
| THN / TND | TRAVEL LEADERS NETWORK LLC (group; TND in CAD) | DEMO12 |

Codes on Pricing Detail passenger rows (applied promos) whose descriptions
were **not shown and are not published**: GCH, GS4, KR1, LGS, NS1, CN6.
"NBO" is not defined anywhere public — do not guess either.

## 7. Method for accuracy

1. **Read data, not pixels:** `displayRow(...)`, `showSpecialRequests(...)`
   and the Pricing Detail rows are JavaScript calls with the figures as
   arguments.
2. **Princess's own search method:** "select two promotions at a time,
   return to this screen, and repeat until you find the lowest fare."
3. **Self-check every booking before trusting a quote:** price the booking's
   OWN category on its OWN promo and require it to equal the booking's gross
   (DEMO12: 6,534 = 6,534). If it doesn't, something unseen differs (vacation
   protection, transfers, credit) — refuse the booking, never guess.
4. **Like for like:** same category (first two characters), same package,
   same vacation protection (POLAR: standard cover "is usually added
   automatically"), no packages/transfers dropped, payment dates not earlier.
5. **Every quote must reconcile** (§4) or it is an error, never a saving.
6. **Onboard credit is not cash**; giving it up needs 3× the cash saving.
7. **Group fares: out of scope for now** (Neon 2026-10-15). They appear only
   when the agency holds group space.
8. Prove headless before using it; capture every page; Neon checks the first
   bookings by hand.

## 7b. BUILT AND RUN LIVE (2026-10-15)

**Code:** `core/princess_pages.py` (page readers), `core/calculator_princess.py`
(decision), `scraper/princess.py` (the walk), `run_princess_check.py`
(`browser` / `check` / `probe`). Tests: `test_princess_pages_2026_10_15.py`,
`test_calculator_princess_2026_10_15.py` (+ the 2026-09-16 package tests).
Neon signs in by hand in the window `run_princess_check.py browser` keeps
open; runs attach to it and never log in or out.

**Read-only pass, 33 bookings:** 32 read, DEMO13 "RCBI-0006E INVALID DATA
ENTERED". Status codes `var bkStatus`: B Booked, G Guarantee (C/W defined by
the page). **12 of them are CAD** — currency is read, never assumed.

**What Gross is:** `Gross = Fare + Packages + Air + Mods + Services +
Miscellaneous` (DEMO27 air 1,433.22; DEMO31 air 6,685.28; Coral/Island Mods
96; DEMO25 Services 1,031.76; MQXP* Miscellaneous 548 with no base fare).
**A refare is compared on Fare + Packages only.** "Total Received (Less )" is
net of commission; **Gross Payment Due 0 = paid in full.**

**The pricing walk, every screen proven live on DEMO24:**
Booking Information → REBOOK = F, both cancel drop-downs (`CANCCODE`,
`CXLREASON`) verified empty → Rebook → Voyage Pricing → Search By Category List
→ **Category Fares – Best Buy** (own category, first two characters) → Pricing
Detail (Best Buy quote) → Back → **Fare Comparisons** → tick the booking's
own-package promos, two at a time (hidden field set to "S", read back) → Save &
Continue → **Important Notices (RDIA)**: each promo's notice recorded, "I have
read each notice" (`RECAPFLAG` → hidden `RECAPPFL = Y`) → `submitRequest()` →
Voyage Pricing → Search By Category List → **Category Fare Comparisons
(RDLK)**: the same `displayRow('BC+',…)` with **single quotes**, column 1 =
left promo (`CATEGORY_SELECTION`), column 2 = right promo
(`CATEGORY_GROUP_SELECTION`, header box `…-FARE2COD`); prices exclude taxes →
own category in the cheaper column (`SELCAT`/`SELCOLUMN` read back) → Pricing
Detail → MAIN MENU.

**Deposit terms come from Important Notices:** DEMO24 — KLS "The deposit of
the fare you have selected is 100% Non-Refundable"; NBS "you have chosen a
Refundable Deposit". Recorded per quote (`deposit_terms`).

**Best Buy can be a different package:** DEMO24's Best Buy for BC was KLP
"FALL SAVINGS LTO – PPLUS" (a Plus fare) on a Standard booking — refused, and
the Standard promos priced instead (KLS 8,474 vs 7,514 booked → no saving).

**More hazards found live:** EXIT on the wizard pages is `logOut(...)` (signs
Neon out) — blocked; Booking Search has three "Search" links (only
`submitBookNum()`); "Manage Booking" is styled upper-case; Pricing Detail's
gross includes Mods (`Fare 4,237 + Mods 48 = Gross Fare 4,285` per guest).

### First full pricing run — 18 bookings, 2026-10-15 (`data/princess_check_20261015T130230`)

No pop-ups, no unexpected screen, nothing changed. **No saving on any.**
- 12 priced like-for-like and dearer today than booked (e.g. DEMO24 Standard
  KLS 8,474 vs 7,514; DEMO34 7,358 vs 6,124; DEMO30 Premier KNL 5,104 vs 4,998).
- 4 refused because the cheaper same-package fare is **waitlisted**: DEMO29
  ("W/", KLS would be 3,496 vs 3,986), DEMO27 ("W/", 1,534 vs 1,876), DEMO21
  ("W/"), DEMO31 (Premier KNL/NNB "W"). Worth re-checking if space opens.
- 2 NOT_PRICEABLE: DEMO23, DEMO22 (Enchanted, CAD) - POLAR listed no fares.
- DEMO25: quote carried Vacation Protection 206.70 vs the booking's 204.30
  (different cover) and was dearer anyway.
Fixes made during the run: Mods and Vacation Protection inside the quote's
gross; `Vacation Protection *` in nested tables on both pages
(`waiverFarePrint`, `insPricing`); waitlist/closed never a saving
(`category_available`); "no fares listed" reported per booking instead of
stopping the run (`NotPriceable`).

## 8. Headless — first measurement (2026-10-15)

Public pages, no login: `/onesource/login` and a POLAR help page loaded in
three modes — headed, headless, headless (`channel="chromium"`). **All three:
HTTP 200, the login form present, no "Access Denied".** ESPRESSO failed at
exactly this stage (headed 200, every headless mode 404 at the CDN edge), so
POLAR has passed the test ESPRESSO failed. NOT yet proven: Akamai also scores
behaviour after login, so the real test is the same bookings windowed and
headless, every captured field compared (as GoCCL, 308 fields, 0
differences). Needs the OneSource login saved in the keyring first.

**No OneSource API for fares.** OneSource's JSON API
(`gw.api.princess.com/pcl-web/internal/...`) handled only login, permissions,
agent profile and the POLAR hand-over token (`agents/POL/token`,
`agents/<id>/P/access`) in the HAR. Every booking and fare screen is POLAR.

## 9. Open questions

- ~~Payment-date rule copied from GoCCL — confirm for Princess.~~ ANSWERED
  2026-10-15: on Princess payment dates are a WATCH item, not a block -
  Neon: "there is payment dates before at the original booking and before
  you save but it changes sometimes however it is a thing we keep an eye on
  but it is not as madnatory as carnival". `decide` notes `WATCH PAYMENT
  DATES …` or reminds to check them; the saving stays an OPTIMIZATION.
- Meanings of GCH, GS4, KR1, LGS, NS1, CN6 and "NBO".
- Where the current promo is shown for a booking that has no Special
  Requests package and no offer code (PRICING SUMMARY, unopened so far).
- **The booking's own CURRENT promo codes** - not on the landing, Special
  Requests or Booking Information pages (DEMO24, DEMO12 2026-10-15). Needed
  for a full "before" record; one recording of where Neon reads them
  (probably PRICING SUMMARY) settles it.
- ~~Whether Akamai lets POLAR run headless.~~ It does - proven 2026-10-15 (section 10).

## Sources

- Neon's recordings and HARs, 2026-09-16 and 2026-10-15 (Downloads).
- POLAR help pages: `onesourcecruises.com/html/tapres/` —
  `coupon_code_content.html`, `rcba_cbok_content.html`,
  `rcbd_cbok_content.html`, `rdle_aval_content.html`,
  `rdlf_aval_content.html`, `rdic_aval_content.html`.
- princess.com Plus & Premier page and its terms and conditions.
- No public projects or APIs for OneSource/POLAR exist (GitHub search,
  2026-10-15).

## 10. In the GUI (2026-10-15)

`scraper/princess.py: PrincessScraper` - a `BaseScraper` wrapping the same
`PolarSession` walk, registered in `BookingService`, `main.py` and the GUI
(`CruiseLine.PRINCESS` gives it its own tab). Results go to `bookings` like
every other line; every page of every walk is kept under
`data/princess_scans/<day>/`, plus `<REF>_result.json`.

- **Check login / Start sign in automatically** (`PrincessScraper.auto_login`,
  login saved with SAVE_LOGIN option 6): OneSource form `#userName` /
  `#password` / "Sign In" (read live, identical headed and headless; wait for
  the page to hydrate first, success = `/onesource/home/pc`), then the
  top-bar BUTTON "Book & Manage Cruises" (never the footer link of the same
  name, which only opens a help page) -> the "POLAR Online" tab is adopted.
  A saved session skips the form. Manual sign-in still works as a fallback.
- **OneTrust cookie banner** covers the bottom of the page in a fresh
  session and caught every click. It is never answered for Neon; every POLAR
  click goes through `PolarSession.press` (scroll to centre, else the
  element's own click()).
- **HEADLESS PROVEN 2026-10-15**: DEMO12 (past final payment), DEMO24 (7,514
  vs 8,474 - no saving), DEMO29 (3,986; cheaper fare waitlisted) windowed and
  headless, automatic sign-in both ways - identical, 0 differences. Princess
  is in `HEADLESS_CAPABLE_LINES`.
- An idle POLAR tab ends on "CICS Web Interface error" -
  `princess.polar_session_expired`; the batch's re-login path signs in again.
- **Statuses**: priced saving → OPTIMIZATION (confidence 5, POLAR's own
  quote); paid in full → PAID_IN_FULL; CX → CANCELLED; waitlisted, past final
  payment, group, no fares → NO_SAVING with the reason; a safe Stop → ERROR
  "nothing pressed", and POLAR is returned to MAIN MENU after every booking.
- **Open in portal** is not available for Princess (POLAR is one CICS session
  per sign-in; a second tab would share its state token).
