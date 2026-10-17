# CruiseIntel — the defect register

Every bug this project has found, what actually caused it, and what was
done about it. Newest first within each section.

**Why this file exists.** Most of these defects were *classes*, not
one-offs: the same mistake made in two scrapers, or a fix applied to one
cruise line and never carried to the others. Reading this before changing
anything is how you avoid re-finding them.

**How almost all of them were found.** By querying the live database
(`cruise_intel.db`) or the log (`data/cruiseintel.log`) — not by reading
code. Code review tells you what *could* be wrong. The corpus tells you
what *is* wrong. Several defects below sat in data this project had been
capturing for weeks before anyone read it.

---

## Table of contents

1. [Money and calculation](#1-money-and-calculation)
2. [Wrong data attributed to the wrong booking](#2-wrong-data-attributed-to-the-wrong-booking)
3. [Session, login and logout](#3-session-login-and-logout)
4. [Scans that did not finish, and did not say so](#4-scans-that-did-not-finish-and-did-not-say-so)
5. [Booking status misclassification](#5-booking-status-misclassification)
6. [Browser, navigation and locking](#6-browser-navigation-and-locking)
7. [GUI and reporting](#7-gui-and-reporting)
8. [Process, crash and observability](#8-process-crash-and-observability)
9. [Mistakes made *while fixing*](#9-mistakes-made-while-fixing)
10. [Open questions](#10-open-questions)

---

## 1. Money and calculation

### 1.1 A price INCREASE was shown as a green OPTIMIZATION
**Found** 2026-10-01 by Neon, on sight, within hours of the scan.
*"3001014 THIS BOOKING IS SHOWING A HIGHER PRICE AND IT IS GREEN … SUCH
MISTAKE CAN NEVER HAPPEN AGAIN IN OUR PROJECT!!!"*

**Cause.** `net_saving` folded OBC into the cash figure, so a $247 price
rise that gained $300 OBC scored +$53 and passed the OPTIMIZATION test.

**The part that matters.** A 2026-08-13 audit had already looked at this
behaviour, **blessed it, and written a test to lock it in**. A test
defending a behaviour is not evidence the behaviour is correct.

**Fix.** A cash-increase guard sits **first** in the status chain in
`core/calculator.py` — before the perk-trap and OBC-loss checks. The money
column shows `+$247.00 more expensive` rather than a positive net.

**Rule.** OBC is not cash. It cannot pay the invoice and is usually
onboard-only and non-refundable. A cash price increase is never an
optimization, however much OBC is gained.

**Guard.** `tests/test_cash_increase_never_optimization_2026_10_01.py` —
including a **corpus-wide invariant**: no capture we hold may produce an
OPTIMIZATION at a higher price. That one is the real guard.

### 1.2 Canadian dollars were added to American ones
**Found** 2026-10-07, in the database.

**Cause.** `total_optimization_savings()` sums every `net_saving` and
every caller prints a `$` in front of the answer. OPTIMIZATION rows:
USD 73 ($6,914), **CAD 35 (5,923 CAD)**, UNKNOWN 51, unset 80. ESPRESSO
alone holds 2,992 USD and 623 CAD rows, so even one tab mixes them.

**Why it survived.** Flagged in the function's own docstring on
2026-08-13 and deliberately deferred, for a good reason: filtering to USD
would have **silently dropped** results from a total the operator relies
on — a worse error than the one being fixed.

**The unlock.** *Splitting* drops nothing. Every result still appears,
under the currency it was recorded in; one whose currency was never
established appears as UNKNOWN instead of being counted as dollars.

**Fix.** `savings_by_currency()` + `format_savings_by_currency()` in
`core/calculator.py`; both GUI summaries, the CLI totals, and a new
**Currency column** in the Excel and CSV exports. A single known currency
reads exactly as it always did.

**Guard.** `tests/test_savings_by_currency_2026_10_07.py` (14).

### 1.3 ALL INC 2PK counted twice — a $546 saving read as −$1,974
**Found** 2026-09-30 → corrected 2026-10-05.

**Cause.** The same perk was charged once as a lost package and again as
a lost fare. `546 − 1260 − 1260 = −1974`; the true figure is **−714**.

**Fix.** An `already_counted` set prevents a name being charged twice.

**Dead end, documented so nobody repeats it.** My proposed fix — exclude a
positive `CRUISE_PROMO` as a surcharge — produced a +$546 OPTIMIZATION.
Neon's Compare screenshot (A2: $6,210 vs $5,328) proved the perk is
*paid for*. **A positive `CRUISE_PROMO` is the PRICE of a perk — keep
subtracting it.** Reverted.

### 1.4 We under-quoted every booking carrying a re-addable promo
**Found** 2026-10-01. Booking 3001021: we quoted **$48**, Neon achieved
**$114**. Booking 3001022: we quoted **$50**, the portal showed **$116**.
**$66 both times** — exactly the value of `Email Bonus NRD`.

**Cause.** The calculator *identified* the re-addable promo (the note
already said `re-add: Email Bonus NRD`) and left its money out of the
figure. **401 bookings carry a priced re-addable promo, $97,565 of value.**

**Fix.** The note now states the ceiling: `(worth $66.00 — saving becomes
$116.00 if re-added)`. **`net_saving` is NOT inflated** — re-adding is a
manual step that can fail, and promising unsecured money is §1.1 in
reverse. A re-addable promo can never flip a verdict.

**Correction, 2026-10-13.** This was right for discounts and wrong for
perks: §1.9. The $97,565 total included perks, which come back at their
price, so it is overstated. It is re-measured in §10.

**How it was found.** Neon reported two bookings as wrongly showing
OPTIMIZATION; the screenshots turned out to be a different rate (his own
correction). Investigating anyway produced the second data point that
explained the first. **Investigate the report even when the premise turns
out to be wrong.**

### 1.5 A false $400 saving on a booking with two cents outstanding
Booking 3001001. *"We could not see the balance"* had become *"it owes
nothing."* Fixed by making an unreadable payment panel a refusal, never a
zero — see §5.3.

### 1.6 Unconfirmed GoCCL candidates counted as banked savings
**$4,100 of $13,053.81 all-time reported savings — 31.4% — came from 5
GoCCL rows whose own note read "UNCONFIRMED".** Two were re-scanned the
same day and came back NO_SAVING. DEMO02 was counted twice.

**Fix.** `unconfirmed_candidate_total()` reports them separately. A row
that says "verify this before trusting it" is not a realised saving.

### 1.14 NCL false TRAPs: every add-on "lost", commission priced as a loss (2026-10-16, found by Neon)
**Found** on 3001044 and 3001045 (*"this is an optimization because
nothing was lost"*). TRAP "$120 price drop wiped out by $200 lost: $100 PER
CABIN BONUS COMMISSION ($100), $100 PER CABIN ONBOARD CREDIT ($100)" - and
the note listed EVERY add-on as lost (Wi-Fi x4, photo package, excursion
credit...). Promos identical; Neon's re-priced screenshot: $6,236 vs
$6,356, every add-on still there. **7 bookings** in the corpus: 3001044,
-58, -70, -83, -97 (one group, $120 each, today) and 3001039 / 3001040
($187 each, 2026-08-28).
**Three causes, three guards:**
1. The after-change wait matched `.item.current` - on EVERY SeaWeb page (HAR
   of 3001047) - so add-ons were read off a page still loading, and "no
   table" returned `[]` ("none"). Now: wait for the summary's own
   `<h2>Reservation Summary</h2>` + `/summary/`; no table off the summary =
   `None` ("could not read").
2. "$100 PER CABIN BONUS COMMISSION" - the AGENCY's commission - was priced
   as a client loss. Now never priced (rule 9), listed for information only.
3. If EVERY add-on still reads as lost, re-read once; if still so, treat as
   unread (`None`) - never a confident TRAP. Unread add-ons on an
   OPTIMIZATION now say UNCONFIRMED (yellow, not green). Names compared
   ignoring spacing/case.
Pinned by `tests/test_ncl_false_trap_3001044_2026_10_16.py` (3001045
through the real calculator = $120 OPTIMIZATION; mutation-tested).
**Separate, not yet built:** 3001047's TRAP is real by today's logic but
Price Programs turns it into a $129 saving - see docs/NCL_PRICE_PROGRAMS.md.

### 1.13 A fare that ADDED A DEPOSIT shown as an $868 saving (2026-10-15, found by Neon)
**Found** on DEMO16 (GoCCL): GUI OPTIMIZATION "$868.00 saved", $1,396.00 →
$528.00, 4C → FSA (CARNIVAL PLAYERS CLUB FUNMATCH). Neon's FSA review:
**Deposit $200.00 due Jul 28, 2026**, Final Payment $428.00 due Jul 12,
2027, Payments Applied $100.00. The booking (captured initialData) had no
deposit pending: `closestPaymentDueBy` = `finalPaymentDueDate` = 2027-07-12.

**Root cause — order of checks.** `apply_goccl_review` checked that the
invoice adds up *before* the payment schedule. FSA's invoice carries
`TFPA` "TARGETED FUNPLAY AMOUNT" $50 — a casino credit outside GRSS
(369.50 + 158.50 = 528.00) — so the sum read 578 ≠ 528, the function
returned "unconfirmed", and the deposit check below it **never ran**. The
row kept its $868 and its OPTIMIZATION label.

**Fix — the payment schedule rule, now a HARD RULE (Neon 2026-10-15):**
*"we can never add deposit or payment dates changes … unless … the final
payment is further not nearer"*. `core/payment_schedule_rule.py`, one
function for every line: a deposit added, a pending payment moved, or any
payment nearer → NO_SAVING; the final payment moving LATER is the only
allowed change and is named in the note; an unreadable schedule is never
assumed unchanged. GoCCL checks it **first**, before any money figure.
Princess uses the same function as a **watch note, not a block** (Neon,
same day: "it is a thing we keep an eye on but it is not as madnatory as
carnival") — `WATCH PAYMENT DATES …`, or a reminder to check them, since
POLAR's Pricing Detail shows no dates (its old "earlier" checks never fired:
the quote dates were never filled).
TFPA is now read as a credit (`ReviewInvoice.funplay`): never cash, never
in net, losing it needs 3x the cash like OBC. Pinned by
`tests/test_payment_schedule_rule_2026_10_15.py`; mutation-tested (the old
check order fails `…deposit_refusal_comes_before_the_invoice_sum_check`).
**Not covered:** ESPRESSO, NCL and MSC never see the new fare's schedule
before it is saved — no data to apply the rule to (open item, §10).

### 1.12 NCL called a perk loss a saving (2026-10-15, found by Neon)
**Found** on 3001042: *"it is not optimization at all … this is false
positive"*. The scan said OPTIMIZATION $84.78 ($3,206.11 → $3,121.33) while
its own note said "ALSO LOSES (value not readable): More at Sea Specialty
Dining 2 Meals, Wi-Fi 150 mins", "LOSES PROMO(S): DISC35, DISXIN2" and NCL's
banner "An OBC or AMENITY has been dropped". Neon re-added DISXIN on Price
Programs: invoice $3,201.33, with **DIN SRV CHG $80.00** back — the real
saving was **$4.78**. The drop *was* the perk.

**Cause.** A 2026-08-27 decision, pinned by a test: a lost perk whose value
the page does not show stays OPTIMIZATION "because we cannot prove a dollar
loss", with a verify-by-hand note. **All 3** NCL OPTIMIZATIONs that day were
this shape (3001056 and 3001057: Wi-Fi + Excursion Credit for $60).

**Fix.** A positive net that loses an unpriced perk is **NO_SAVING**, naming
the perks and pointing at Price Programs; NCL's own "OBC or AMENITY has been
dropped" banner also forces NO_SAVING (`scraper/ncl.py`). The 08-27 test now
asserts the opposite, with this evidence; mutation-checked. Not built yet:
re-adding the promo on Price Programs automatically (as ESPRESSO's Compare
step does) — needs that page's markup first. The three stored rows stay as
captured until the next scan replaces them.

### 1.11 ESPRESSO never priced a booking with its perk kept (2026-10-13)
**Found** by Neon on booking 3000067: *"there is a lower price again of
600 … are we catching this with the network?"* We were not. All **446**
allocate calls in `data/network_traffic.jsonl` priced the **BESTRATE**
column. A booking on ALL INC 2PK was only ever priced *without* its perk,
and the perk's price subtracted as an estimate. The cheaper version of
the same perk was invisible.

**The evidence.** Neon's recording plus two sanitized HAR files of
Promotions → Compare, Y guarantee, per person, both guests:

| Column | Code | Y | Allocate total |
|---|---|---|---|
| Best Rate | BESTRATE | 1,609.50 | 3,219.00 |
| ALL INC 2PK NRD | DJ065340 | 2,659.50 | **5,319.00** |
| ALL INC 2PK (refundable) | DJ028141 | 2,972.50 | **5,945.00** |

He had moved the booking to the refundable ALL INC 2PK at $5,945. The NRD
version of the same perk was **$626** cheaper. The difference is the
discounts each one combines with (BOGO75 NRD vs BOGO50). Both columns
share the same base price code `LM767049`. The totals include taxes
(his reprice screen: subtotal 4,735 + taxes 584 = VACATION TOTAL 5,319),
the same basis as the scan's `VACATION_TOTAL`.

**How the portal does it**, read from the HARs:
- `_eventId=linkToPromotionList` opens one checkbox per offer.
  Each value is `CODE^NAME^FIT^FLAG`, where `D` = non-refundable deposit
  and `N` = refundable.
- **Compare** (`a.btnCompare#promoFare00`) posts `_eventId=saveChanges`
  with up to two offers.
- **Select (`a.btnSelect#promoFare01`) sits right next to it.** The
  portal's text: "You may select one of the available offers, or compare
  two different offers."
- The returned Categories page carries every price as data:
  `viewJSON.matrix.matrix[CAT]["INDIVIDUAL-<CODE>"][0].pricing.FIRST_SECOND.price`,
  plus `pricePromo.nonRefundablePromo`.
- **`ALL INC 2PK NRD` and `ALL INC 2PK` are different offers with
  different codes, and codes change per sailing.** An older capture shows
  DI980793. Match the exact name, never a prefix.

**Fix — built, not yet proven live.** `EspressoScraper._perk_compare`
plus `core/espresso_perk_compare.py`. It runs only when the Best Rate
reprice drops ALL INC 2PK or RETREAT.
1. Open Promotions. Tick the booking's own perk and its NRD/refundable
   twin by exact name, with Best Rate unticked.
2. Read the ticks back (`_check_promotions_ticked`).
3. Click Compare. The guard refuses anything not labelled "Compare" or
   carrying `btnSelect`; it is mutation-tested.
4. Read the category's columns from `viewJSON`.
5. Reprice only a column that can win (a 2-guest pre-check; any other
   occupancy is always repriced). The reprice uses that column's own
   radio, and the selection must name that column's code.

A column replaces the verdict **only** when Royal's invoice says
OPTIMIZATION and nothing is lost: no package, no onboard credit, no
travel protection. Otherwise its figures are appended as
`[PERK COMPARE …]`. A move to the NRD twin says **"DEPOSIT BECOMES
NON-REFUNDABLE"**. Everything is captured: market_data payload
`perk_compare`, and raw_responses wrapped as `{"perk_compare": …}` so the
main reprice stays the last plain capture. Off-switch:
`settings.espresso_perk_compare`. Tests:
`test_espresso_perk_compare_2026_10_13.py` (16), including a fake
ESPRESSO page where Select is wired to a counter. Proof line: see §10.

### 1.10 A re-addable perk was charged twice — TRAP hid real savings (2026-10-13)
**Found** by Neon on 3000067: *"previously it was not showing when the
booking was at 7k"*. The scan said TRAP on 2026-10-09 and 10-12: *"losing
$2100 perk for only $1769 net"*.

**Cause.** `net` already has the perk's price taken off
(`drop − lost_pkg_value`), and the portal re-adds the perk for exactly
that price (§1.9). The TRAP rule `net < lost_pkg_value` then charged it a
second time, so a booking needed a drop above **2 × the perk** to show.
**$1,769 was a real saving with ALL INC 2PK NRD kept.** The corpus has
8 TRAP rows on 2 bookings: 3000067 ($1,769) and 3001070 ($142).

**Fix.** When every lost perk is re-addable and no travel protection is
lost, `net > 0` is an OPTIMIZATION. A perk that cannot come back stays a
TRAP, as the rule was written for ($50 net for a $594 drink package).
Pinned by `test_3000067_on_2026_10_12_was_a_saving_with_the_perk_kept`;
the test fails against the old rule (mutation-checked).

### 1.9 Re-adding a perk was promised for free (2026-10-13)
**Found** from Neon's 3000067 recording. The note said `re-add: ALL INC
2PK NRD (worth $2,100.00 — saving becomes $4,437.00 if re-added)`. The
portal priced the perk at **$1,050 pp = $2,100**, exactly its positive
`CRUISE_PROMO` line, already in `lost_pkg_value`. So `net_saving` $2,337
(7,656 − 5,319) **was** the saving with the perk kept, and $4,437
promised the perk back for nothing. 189 latest ESPRESSO rows carried a
"saving becomes" note.

**Cause.** §1.4 added `abs()` of every re-addable item. That is right for
a discount (`Email Bonus NRD` −66: re-adding lowers the price) and wrong
for a perk (a positive `CRUISE_PROMO` is its price — §1.3).

**Fix.** Only a negative (discount) line comes back as money. A perk now
reads `re-adding X puts its $P price back — $N is the saving with it
kept`, or `keeping it costs $N more than now` on a TRAP. 3001036's note
had said "saving becomes $1,557"; keeping the perk actually costs $445
more. Tests: `test_readd_perk_is_not_free_2026_10_13.py`. Proof line: an
ESPRESSO Note containing `is the saving with it kept`.

### 1.8 GoCCL hid every same-code reprice (2026-10-12, found live)
`calculate_goccl` dropped any offer whose code equalled the booking's
(`offer_code != current_offer_code`). Carnival reuses a code for a new sale
at a new price: DEMO40 sat on OCS "FUN ON THE WAY EARLY SAVER SALE" at
$1,556 while today's OCS ("LOOK FORWARD TO FUN & EARLY SAVER SAVINGS")
priced it at $1,500. The scan reported the Super Saver as "the only cheaper
fare" and missed it; Neon had found $56 (DEMO43 the same, $56.22). **Fix:**
the filter is gone — price decides. After it, the live run confirmed both
on Carnival's review to the cent. Pinned by
`test_demo58_the_same_code_at_todays_price_is_the_candidate`.

### 1.7 Voyagers Selection recommended on top of a military discount (2026-10-08)
Rule MSC-D9 (Neon): a Voyagers discount cannot be applied on top of
military. Booking 3001058 (`MIL-CIV-IL-DSCNT-10%`, Voyagers members) was
reported `VOYAGERS_SELECTION: OPPORTUNITY — MSVG10W (10%) … confirmed to
stack with the base 5% Club discount` in `live_check_results.jsonl`.
`_check_voyagers_selection` counted only `SPECIAL OFFER` / `VOYAGERS
EXCLUSIV` as already applied. 2 of 133 captured MSC bookings carry a
military discount (3001058, 3001062). **Fix:** one rule,
`core/calculator_msc.military_discounts` + `military_rule_note`, used by
`DISCOUNT_ADD` (before its Group Rate branch), `VOYAGERS_SELECTION`, the
scan's result note, the GUI batch log (`msc.military_discount`) and the
status-match flag. Same day, rule MSC-D10 (Neon): `DISCOUNT_TIER_UPGRADE`
may suggest swapping military out for a higher discount **only when a
passenger is a Voyagers member** — it used to suggest it for anyone.
Detail in `docs/MSC_DISCOUNT_RULES.md` §2, MSC-D9 and MSC-D10.

---

## 2. Wrong data attributed to the wrong booking

### 2.1 Per-booking state leaked between bookings — 42 bookings affected
**Found** 2026-10-07, in the database.

**Cause.** A scraper object outlives the booking it scrapes — one instance
checks hundreds in a row — so anything cached on `self` survives into the
next booking. `BookingService` reads several of those attributes *after*
`check_booking` returns and writes them keyed to **that** booking:

```python
await self._save_market_data_to_db(result, scraper.last_market_data)
extract_booking_features(..., market_data=..., initial_data=...)
```

NCL sets `last_market_data` at the very **end** of `check_booking`, after
several early returns. A booking taking one of those returns kept the
previous booking's panel.

**Evidence.** 21 byte-identical payloads — same final-payment date, same
amount — stored against **42 different bookings**. Every group pairs a
real booking with a `NOT_ON_THIS_ACCOUNT` one. It repeats in every scan
since the capture was added.

**Why it survived.** ESPRESSO had been fixed **three times, one attribute
at a time, on three separate days** — each fix correct, none general. NCL
and GoCCL never got the same treatment.

**Fix.** `BaseScraper.reset_per_booking_state()` + the `PER_BOOKING_STATE`
tuple. Every scraper clears every cached attribute in one place; a new
attribute is protected by adding its name to the tuple. ESPRESSO resets
per booking **and** per retry attempt.

**Data.** The affected rows are listed in
`data/reference/misattributed_market_data_2026_10_07.json` — **not
deleted**, per the standing rule.

**Guard.** `tests/test_per_booking_state_is_reset_2026_10_07.py` (9),
mutation-tested: reintroducing the NCL bug makes it fail.

### 2.2 Feature fields read after the page had navigated away
`read_feature_fields()` was called from `BookingService` *after*
`check_booking` ended — and `check_booking` ends with `release_booking()`,
which navigates away. The read landed on the wrong page and silently
produced NULLs. **Fix:** capture while still on the booking page, hand the
value back on the scraper.

### 2.3 The price fix that silently did nothing
**Found** 2026-10-05, in the log, after a full run.

Restricted (`skipRepriceModal`) and waitlisted (WLT) bookings got no
price: **0 of 135 and 0 of 86**, with `total_price=None` and **zero
exceptions**. `_read_payment_status()` ran fine and was pointed at the
wrong page — `_click_categories()` had already navigated away from the
Reservation Summary.

**Fix.** Both branches use the value captured while the summary was on
screen. And `last_payment_status` gained a per-booking reset, because
`BookingService` decides a **permanent** paid-in-full exclusion on it.

---

## 3. Session, login and logout

### 3.1 Nothing ever tried to log back in — 242 bookings abandoned
**Found** 2026-10-07. Neon: *"we are still having the esspresso login bug
issue idk when u will fix it"* — the same day a run checked 451 of 769.

**Cause.** `_check_login` is a **detector**, and it was wired straight to
a `raise`. `auto_login()` existed the whole time and was called from
exactly one place: the GUI's Start button. Nothing re-tried it mid-scan.

```
14:45:59  gui.start_auto_login_ok          <- a login DID work
16:47:41  login.required  .../login
16:47:47  batch.error 3001037 "Not logged in"
16:49:31  batch.complete  checked=451 requested=769
```

**Why nobody built it.** The assumption that ESPRESSO always demands MFA.
The log says otherwise — across every recorded `auto_login`:
**OK 19 · ALREADY_LOGGED_IN 8 · FILLED_AWAITING_MFA 13 · NO_LOGIN_FORM 1.**
**Two thirds need no human at all.**

**Fix.** `EspressoScraper._ensure_logged_in()` re-logs in and **verifies**
the result. On MFA it waits a bounded 3 minutes — **once per scan**, via
`_mfa_wait_exhausted`, never 3 minutes × every remaining booking.
`NO_CREDENTIALS_SAVED` is never retried.

**Lesson.** *A capability existing is not the same as it being called.*
Grep for **call sites**, not definitions.

### 3.2 The phantom logout — 268 aborted navigations
**Root cause** 2026-09-23: mostly **not** a logout. The code left `/home`
after 136 ms and its in-flight redirect aborted the next navigation.
**Fix:** `_settle_navigation()`.

**Standing rule:** never log into ESPRESSO by hand during a scan — the
portal allows one active session, so that *causes* a real logout.

### 3.3 The SSO hop read as a logout
`auth.cruisingpower.com` contains neither "login" nor "signin" and *is* on
cruisingpower.com, so a pure URL test returned True while the login form
was on screen — then the scan drove into the real login wall. That is the
double-login Neon kept hitting. **Fix:** three checks, cheapest first,
the decisive one being *is a password field present*, plus a settle window
so a redirect in flight is not called a logout.

### 3.4 A login form mid-hydration read as a logout
`/home` renders a login form for a moment while it bootstraps. Sampling at
that instant reported "not logged in" while the scan carried on fine.
**Fix:** a ~6 s settle window; a genuine logout keeps its form for the
whole window.

### 3.5 The 30.5-minute auto-logout
**Root cause** 2026-09-21: the portal arms a client-side auto-logout on
**every page load**. Explains years of login/logout bugs.
**Fix:** `keep_session_alive()`.

### 3.6 A false ALREADY_LOGGED_IN made auto_login do nothing
`_check_login` ran two seconds after the browser started, while a
restored-but-dead session was still redirecting itself to `/login`. It
sampled a half-loaded page, reported authenticated, and `auto_login`
returned without ever filling the credential. **Fix:** settle first.

### 3.7 ESPRESSO cannot run headless
**Re-tested** 2026-09-23, 8/8 trials: headed 200, **all** headless modes
(shell, `channel=chromium`, `channel=chrome`) 404 at the CDN edge. The new
headless mode does not help. A guard is pinned by tests. Not a bug to fix
— a constraint to respect.

### 3.8 Re-logins judged before the sign-in had landed (2026-10-08)
One 769-booking scan, 16:30–17:03 UTC: the session dropped ~every 80 s,
6 bookings failed, and the log blamed MFA. It was not MFA. The re-login
loop from 3.1 worked (`espresso.relogin_verified ok=True` ×2 — its first
live proof) but three judgements around it were wrong:
1. `auto_login` judged ~1 s after submitting, before the OAuth chain
   (login → auth.cruisingpower.com → `/oauth/callback` → app) landed:
   `FILLED_AWAITING_MFA` then `relogin_mfa_completed waited_s=10.1`, **8×
   in a row** — plain logins mislabelled as MFA.
2. On `/oauth/callback?code=…` there is no password box because nothing
   needs typing; `NO_LOGIN_FORM` was returned and treated as final (5×).
3. One real 3-min MFA timeout (16:45) set `_mfa_wait_exhausted`, which
   nothing ever cleared, so every later re-login was skipped (8×).

**Fix:** `_wait_for_login_outcome` gives the chain `_SSO_LANDING_SECONDS`
(30 s) before and after submitting; the flag clears on any confirmed
login; every step is sent to the GUI (`login_*` actions → status line).
Pinned by `tests/test_espresso_login_landing_2026_10_08.py`, which replays
the chain with fake cruisingpower.com pages; with the wait removed it
reproduces the live `NO_LOGIN_FORM` and `FILLED_AWAITING_MFA` exactly.
**What started it is not in our logs:** the session was being ended ~60 s
after each successful re-login, the shape of a second login to the same
account (rule 6). **Unproven live** — see `CLAUDE.md`.

### 3.9 A re-login "verified" by re-reading the page it landed on (2026-10-15)
**Found** from Neon: *"esspresso is still having a log in issue i need to
log in manually usually twice"*. The log, Oct 13–15:
1. **The session ends about every 60 minutes**, active or not: 14 Oct
   15:31 → 16:33 → 17:34 → 18:37; 13 Oct 17:40 → 18:42; 15 Oct 15:42 →
   16:28 → 17:37. `keep_session_alive` (§3.5) does not prevent it — it is a
   different, absolute limit. Cause **unproven**.
2. **The first re-login after it often reports OK without holding.**
   `espresso.relogin_verified ok=True` was logged **6 ms** after
   `auto_login result=OK` — `_check_login` re-reading the same page, which
   cannot fail. The search then waited 60 s and found `/login`:
   14 Oct 17:34:17 OK → 17:35:47 form again (second login held);
   15:26:37 → 15:27:50; 13 Oct 17:40:16 → 17:41:27; 15 Oct 16:28–16:35
   **six** OK-and-verified logins in a row on booking 3001038, 3 attempts
   burned per booking.

**Fix.** `_prove_session()` opens reservations and requires the search box
(25 s) — the page the scan actually needs. If it is not there, `auto_login`
runs **once more** immediately (the second login Neon was doing by hand);
a second attempt that asks for MFA goes to the normal MFA wait. New log
lines: `espresso.relogin_not_holding landed=… now=…`,
`espresso.relogin_second`, and `relogin_verified` now carries `landed` and
`proved_on`; `auto_login result=OK` carries the landing `url`. **Those URLs
are what will show why the first login does not hold** — no capture of it
exists yet. Pinned by `test_espresso_relogin_and_incomplete_jobs_2026_10_07.py`
(`…redone_once_unattended`, `…second_login_that_asks_for_mfa…`).
Rule 6 still applies: a hand login during a scan kicks the scan's session.


### 3.10 We logged ourselves out: reservations.do now forwards to /logout (2026-10-16, ROOT CAUSE)
Neon: *"can we fix this for good!!! again log in issue"* / *"it keeps logging
in and out !!"* - and nobody else was signed in. Traced live (read-only
diagnostics, GUI stopped), every login:

    13.7s  /home                       login OK (iPlanetDirectoryPro set)
    16.1s  GET reservations.do       -> 302 ../../SecureLogin.do
           GET SecureLogin.do        -> 301 /logout
    18.5s  endSession                -> 302 /login      <- our own navigation

**ESPRESSO moved "Search by Reservation ID, Name or Date" onto /home**, and
its old reservations page now forwards to LOGOUT. The scan opened
reservations.do for every booking, so every login was ended ~3 s after it
landed; the second login of §3.9 could never help, and §3.9's *proof* step
opened reservations.do too - it logged us out each time it ran. Ruled out on
the way: a second session (one CruiseIntel process; Neon: nobody signed in),
stale `LtpaToken2`/`WASPostParam` from the restored storage_state (removing
them changed nothing, run C). ESPRESSO's own /home also calls `/undefined`
every ~9 s, which bounces to /logout as a background fetch - harmless, the
session stayed up.

**Fix:** `EspressoScraper._go_to_search()` - search where the box is (/home);
reservations.do only as a logged fallback (`espresso.search_box_not_on_home`)
if /home ever shows no box. Used by `_check_booking_inner`, `_prove_session`
and Open in portal. **Proven live:** 3001041 (OPTIMIZATION $50) and 3001035
(NO_SAVING) checked and released from /home, session still up 2 min later,
no re-login. Pinned by `tests/test_espresso_search_on_home_2026_10_16.py`
(AST; mutation-tested - the old navigation fails 2 of 3).

---

## 4. Scans that did not finish, and did not say so

### 4.1 "COMPLETED" meant "the loop ended"
**Found** 2026-10-07. Neon: *"there s stilll bookings not scanned in this
run."*

**Cause.** `if job.status not in (STOPPED, FAILED): job.status = COMPLETED`
— however many bookings were left. Two mechanisms key off that word and
both were misled:

* `recent_identical_scan` suppresses a rescan for 2 h — Neon pressed
  Start twice to pick up the leftovers and was refused
  (`minutes_remaining: 81`) on the strength of a run that never touched
  them.
* `resumable_jobs` selects `status != COMPLETED` — the job most worth
  resuming was invisible to it.

Both docstrings already said an interrupted run must not count. One line
made it count anyway.

**Scale.** **13 ESPRESSO jobs back to 2026-09-22** were marked COMPLETED
while abandoning bookings — two having checked **1 of 723**. *Not one
ESPRESSO run in three weeks had genuinely finished.*

**Fix.** `ScanJobStatus.INCOMPLETE`. Both mechanisms then work as already
documented, with no further change. The 13 historical rows were relabelled
(status column only; no result altered or removed).

### 4.2 A retried booking could paper over one never reached
**My own error, same day.** The first version counted `len(job.results)`,
and the retry pass appends a *second* result per re-attempted booking. A
run that retried 5 and never reached 5 others had 769 results for 769
requested and called itself complete. **Fix:** count **distinct ids**.

### 4.3 A dead session ground on instead of stopping
There was no abort at all — only a cooldown (3 failures → 120 s) that
slows the grinding without ever stopping it. §3.1 made the hopeless case
*slower*, since each remaining booking now pays for a login attempt.
**Fix:** `_LOGIN_GIVE_UP_AFTER = 3` consecutive **login-shaped** failures
ends the run. Bookings are left unchecked (resumable) rather than marked
ERROR, and the operator gets one clear reason instead of 318 identical
failures.

### 4.4 18 of 52 jobs stuck at RUNNING forever
If `scraper.stop()` raised — a dead browser, exactly the situation that
path exists to clean up after — nothing below it ran: the job was never
marked complete and its stop flag never popped. A stuck-RUNNING job also
makes the GUI poll forever. **Fix:** each step independently guarded in
`finally`.

### 4.5 41% of a day's scanning was redundant
The cache gate was `status == NO_SAVING`, so PAID_IN_FULL and other
short-circuit outcomes were re-scanned every run. **Fix:** every cacheable
outcome is remembered.

### 4.6 All 414 cache rows expired, growing forever
`cleanup_expired()` existed with **zero callers**, so eviction only
happened lazily for a key someone looked up. **Fix:** once per batch.

---

## 5. Booking status misclassification

### 5.1 NCL could not report a cancelled booking at all
**Found** 2026-10-07.

ESPRESSO checks `is_cancelled()` before every pricing gate. **NCL had
nothing.** A cancelled booking has no balance, so `net_due <= 0.01` made
it "cruise line fully paid" and it was filed as **PAID IN FULL**.

The portal states the status plainly in `payment.bookingFacts.Status`,
present in **1,478 of 1,478** NCL captures and never read:

```
BOOKED 1295    CLOSED 133    CANCELED 32    TERMINATED 18
```

Cross-checked against what we reported: **CANCELED → PAID_IN_FULL**
(3001043, 3001046), TERMINATED → PAID_IN_FULL ×6, CLOSED →
PAID_IN_FULL ×41.

**Fix.** CANCELED is checked **before** the paid-in-full gate (same
ordering as ESPRESSO), after the market-data capture so nothing is lost.
**CLOSED and TERMINATED are deliberately NOT interpreted** — nobody knows
what they mean, and guessing is how this started.

### 5.2 ESPRESSO filed cancelled bookings as PAID_IN_FULL
The same defect, found 2026-09-22. ESPRESSO renders "N/A" for every price
when `sb.reservation.status` is `'CX'`, while the payment panel still
reads Final Payment Due 0.00 — which `is_paid_in_full()` accepted. Booking
3001005 was filed as "Fully paid" four times. 27 bookings needed a
re-scan. **Status CX = CANCELLED and MUST be reported.**

### 5.3 NCL never honoured the never-rescan rule
**Found** 2026-10-07. `record_paid_in_full` reads
`scraper.last_payment_status`; **`NclScraper` never set it**, so every NCL
booking failed the first guard.

```
permanent_exclusions:  ESPRESSO 215 PAID_IN_FULL, 1 CANCELLED
                       NCL        0
log, one day:          368 x exclusion.refused payment_panel_unreadable
```

122 of the 188 bookings in that NCL run were paid-in-full and every one
was re-scanned in full.

**Fix.** NCL publishes its payment evidence in the same shape ESPRESSO
does. **This could not be fixed before §5.1** — it would have written
those wrong calls into a permanent, unrecoverable exclusion.
`record_paid_in_full` now also refuses any reservation whose stated portal
status is not `BOOKED`.

### 5.4 Paid-in-full missed when a small balance remained
Booking 3000057: $43 outstanding on $1,818 is 2.4%, inside the project's
5% tolerance, but NCL's gate was only `preload["isPaid"]` — false while
*any* balance remains. **Fix:** `is_paid_in_full()` is the one canonical
rule and is applied to NCL too, so the two lines cannot drift.

### 5.5 Status-match eligibility must not treat unverified capture as clean
Audit 2026-10-08: the MSC status-match assessor relied on passenger and
discount payload shapes and did not explicitly reject a capture marked
`session_expired`. Missing or malformed membership/discount data could
therefore be misread as absence or raise instead of producing a safe
verdict. **Fix:** expired or malformed evidence returns `CHECK_MANUALLY`;
any captured current discount returns `NOT_ELIGIBLE`; an empty discount
list is eligible only when the SRN reference also proves no silent discount
is applied. The MSC GUI displays this shared verdict and reason in its
results row, and the same note is persisted with the MSC scan. A corpus
check after the change audited the latest 136 bookings: all 5
still-classified `ELIGIBLE` rows had no recorded discounts or Voyagers
membership.

---

### 5.6 MSC: paid in full detected but not obeyed; reprice rule never called (2026-10-15, found by Neon)
**Found** on 3001068 (Due $5.48, *"this is paid in full fix that"*),
3001066 (Due $6.18) and 3001067 (*"SPECIAL OFFER 15% … can be applied but
… useless"*). `msc_results` had `is_paid_in_full=1` **and**
`has_any_opportunity=1` on 5 rows / 4 bookings (3001064 ×2, 3001063,
3001068, 3001066): the 2026-08-12 rule let the three discount checks run
on a paid booking, and each carried an unpriced VOYAGERS_SELECTION.
3001067 (and every MSC discount lead) was never priced at all:
`msc_discount_beats_current()` — RULE MSC-D1/D2, written 2026-09-22 for
3000005 — **had no production call site** (failure pattern 2).

**Fix.** `evaluate_msc_booking`: paid in full returns no checks and no
opportunity, note `PAID IN FULL — not optimizable` (replaces the 08-12
rule; hard rule 7). `_apply_reprice_rule` holds every discount
OPPORTUNITY to MSC-D1/D2 with the rate now carried on `MscCheck.
discount_pct` and the member's club 5% re-applied multiplicatively:
repriced below the fare → OPPORTUNITY with figures; at/above → NO_OPPORTUNITY
"NOT WORTH IT"; unpriced (occupancy unverified, group rate, wrong tab,
unknown rate) → INSUFFICIENT_DATA "NOT PRICED". **One exception, from Neon
the same day:** the club 5% on a booking holding no discount stays a lead
when unpriced — 3001065 *"this is accurate"*. Pinned by
`tests/test_msc_paid_in_full_and_reprice_2026_10_15.py`.
**Open:** MSC's paid-in-full threshold is a flat $15 (Neon 2026-08-12);
every other line uses `max($25, 5% of total)`. 3001067 ($25 due) is
paid in full under one rule and not the other.

### 5.7 MSC read both prices and showed neither (2026-10-15, found by Neon)
*"it is not showing old price and new price and it is not collecting data
and prices at all"*. It WAS collecting them - 3001059 in
`data/msc_control/rate_check_data.jsonl`: booked 3,654.79, today 3,516.26 -
but `MscBookingResult` had no field for them, `msc_results` no column, and
the GUI hard-coded Old/New to "—". **Fix:** `current_total_price`,
`today_price`, `due_amount` on the result (stamped on every return path),
three nullable columns (additive migration), shown as Old / New.
Pinned by `tests/test_msc_prices_shown_2026_10_15.py`. Rows written before
the fix stay "—" (never back-filled with invented figures).

### 5.8 Green meant "something", not "a lower price" (2026-10-15, Neon)
MSC rows turned green on ANY opportunity, including an unpriced "add a
discount" lead; the status panel painted RUNNING pure green. Neon: *"green
is for avaiable lower prices only and price differences"* (CLAUDE.md rule
14). **Fix:** `core.calculator_msc.msc_row_status` - OPPORTUNITY (green)
only for a price in figures; DISCOUNT_OPPORTUNITY orange; RUNNING blue.
Pinned by `tests/test_green_means_lower_price_2026_10_15.py`.

### 5.9 Before/after codes were not saved for most lines (2026-10-15, Neon)
*"MAKE SURE THAT FOR ALL THE CRUISE LINES WE ARE SAVING BEFORE AND AFTER
LIKE ESSPRESSO AND NCL LIKE DIXIN LATREW"*. Measured in `bookings`:
`old_promos`/`new_promos` filled on ESPRESSO **0 of 15,346** (both fare
lists were in hand; only the difference was stored), NCL 977 of 2,883
(none on 217 paid / 2 cancelled rows since 10-08), GoCCL **0 of 158**;
MSC had no column. **Fix:** ESPRESSO stores both full fare lists; GoCCL
"CODE NAME" before/after via a wrapper on every `calculate_goccl` return
(and its paid-in-full exit); NCL keeps the held promos on paid/cancelled
rows; Princess stores offer codes + package before and the quote's promos
after; MSC `discounts_before` / `discounts_today` (columns + CSV + Note).
**Open:** Princess's *current* promo codes are on no page the walk opens
(checked DEMO24, DEMO12) - needs a recording of where POLAR shows them.
Pinned by `tests/test_before_after_every_line_2026_10_15.py`.

## 6. Browser, navigation and locking

### 6.1 389 of 624 bookings left LOCKED for 15 minutes
**V-VIP.** `release_booking` had **one call site against 21 exit paths**.
**Fix:** release is a `finally`. **Never move it back onto a branch.**

### 6.2 …and then the release itself failed 21 times
`release_booking`'s fallback navigated to a **relative** URL, which
`page.goto()` rejects. **Fix:** resolve via `document.baseURI`.
**Confirmed fixed in a real run: 0 failures across 542 releases.**

### 6.3 Dialogs auto-dismissed, cancelling navigation silently
NCL raises a `beforeunload` dialog on essentially any navigation during an
edit; an unhandled one is auto-**dismissed** by Playwright, so the
navigation is silently cancelled. **Fix:** a session-wide dialog handler
installed before any navigation.

### 6.4 Two dangling asyncio tasks
GUI shutdown and the GoCCL advisory listener. A task with no strong
reference is a real candidate for "Task was destroyed but it is pending!",
and any exception it raises is reported nowhere. **Fix:**
`track_background_task()`.

### 6.5 The startup restore task was untracked
**Found** 2026-10-07. The task that exists *precisely* so a completed scan
is not shown as "zero optimization" (2026-08-28: a 769-booking run finding
19 optimizations worth $3,175 looked empty) was itself fire-and-forget. If
it threw, the tables stayed blank and **nothing was logged**. **Fix:**
tracked, and per-panel, so one line's failure does not cost the others.

---

## 7. GUI and reporting

| Defect | Cause | Fix |
|---|---|---|
| Verify button overlapped the table | Qt minimum-size overflow | footer re-laid out; the table's 240px floor is itself a fix and must stay |
| Dashboard frozen, no error | `AttributeError: _CHROME_CENSUS_EVERY` hidden by a broad `except` | constant added to `MainWindow`; a precedence bug (`A or B and C`) fixed with it |
| Logs as a tab replaced the view being watched | wrong shape — logs are read *while* watching a scan | a **button** opening a non-modal window |
| "Scan complete" on a run that abandoned 318 bookings | see §4.1 | an explicit INCOMPLETE message naming the count and what to do |
| `-$79.00 saved` | sign followed the price delta, not the savings polarity | documented at `_format_net_saving`; **still open**, Neon's call |
| Money columns headed "($)" with no currency | see §1.2 | Currency column in both exports; per-currency totals |
| An UNCONFIRMED GoCCL candidate looked like money (2026-10-15) | kept out of the total since §1.6, but its row kept the OPTIMIZATION green in the GUI and in Excel | pale yellow `UNCONFIRMED` tint in both (`is_unconfirmed_candidate`); `test_unconfirmed_is_not_green_2026_10_15.py` |
| GoCCL note advertised an offer that could never apply (DEMO45, 2026-10-15) | "a cheaper 'P2P' saves $382" was a rate-page teaser never taken to review; P2P only offers category BL, "Carnival chooses your stateroom", so the 8A/8B cabin could not be kept. Neon went looking for it | a skipped offer is named with its reason only — no dollar figure ("its price and whether it offers the booking's own category were not checked"). The $122 O8S verdict was right |
| GoCCL could not run headless from the GUI (2026-10-15) | the toggle was NCL-only, though GoCCL was proven headless 2026-10-12 (308 fields, 0 differences) | `HEADLESS_CAPABLE_LINES = (NCL, GOCCL)` for the box and both login call sites; ESPRESSO still refused. GoCCL headless needs the saved session from one windowed login |

---

### 7.x A pop-up on one tab killed the scans on the others (2026-10-16, found by Neon)
*"esspresso results are not showing ... is ncl working?"* with four lines
running. At 13:55:10 Princess's Start showed `QMessageBox.question("Code
changed since this window opened")` - a modal `.exec()` inside an `async`
slot runs a NESTED Qt event loop; qasync resumed other tabs' tasks inside it
and Python 3.14 refused: *"Cannot enter into task <...CruiseLinePanel.
_on_start()...> while another task ... is being executed"*, then *"Task was
destroyed but it is pending! <...playwright Connection.run()...>"*.
ESPRESSO's Start task - the poll loop that copies results into the table -
died: the batch kept scanning and SAVING (56 rows in `bookings`) while the
table showed 2 rows and 87 bookings "RUNNING". NCL's Playwright connection
was destroyed: its job froze at 45/232 with 0 results saved. (The dialog
itself was triggered by my mutation test touching `scraper/espresso.py`
after the restart - the GUI was right to ask.) **Fix:** every pop-up inside
an async handler uses `CruiseLinePanel._notify` / `_ask` (`QMessageBox.open()`
plus an awaited Future - no nested loop); `processEvents()` became
`await asyncio.sleep(0)`. Pinned by
`tests/test_no_nested_loop_in_async_gui_2026_10_16.py` (AST over gui/*.py,
mutation-tested; and `_ask` clicked Yes/No for real).

### 7.y Saved booking list per line, silent de-duplication (2026-10-16, Neon)
*"everyday i keep opening copy paste for 4 cruise lines ... make a saving
list for each cruise line ... silentley dedplicate"*. `gui/saved_list.py`:
`data/saved_lists/<LINE>.txt`, one booking per line, editable; every booking
added (typed, pasted, file) is saved, duplicates dropped silently (case and
spaces ignored); removals go to `<LINE>.removed.log`. GUI: "Load saved list
(N)" queues the whole list; the "Duplicate booking" / "No new bookings"
pop-ups became status text. Seeded from that day's scans: ESPRESSO 785,
NCL 232, GOCCL 140, PRINCESS 113, MSC 49. Pinned by
`tests/test_saved_list_2026_10_16.py`.

## 8. Process, crash and observability

- **An invisible `RuntimeError` killed an overnight scan.** Unhandled
  errors now land in the log with tracebacks (`crash.unhandled`), and a
  watchdog ALARMs on first crash and on process death.
- **`batch.complete` could not answer "was that run healthy?"** — it
  logged three fields. `run_summary()` now records requested / checked /
  unfinished / statuses / optimizations / savings / errors /
  login_blocked / duration / attempts.
- **A clean shutdown exited with code 1**, making every ordinary quit look
  like a crash. Fixed to an explicit 0.
- **`structure_watch` cost 61 s per session** — two watched selectors
  inherited the 30 s action timeout. Capped at 4 s, once per session.

---

### 8.x The test suite killed itself (found 2026-10-15)
Every full run of the second half ended at ~97% with exit code 0 and no
summary; eight pytest processes from earlier runs (some from 2026-10-13)
were still hung. Cause: `test_tabbed_gui_2026_08_28.py::
test_shutdown_covers_every_panel` ran the real `_shutdown_all()`, which arms
the GUI's 3-second force-exit `threading.Timer` -> `os._exit(0)` 3 s later,
mid-suite. **Fix:** the test sets `win._force_exit` before shutdown and
cancels the timer. First complete run after: 1,951 passed. Also: tests that
read `gui/windows.py` by line number fail if that file is edited while they
run - never edit source during a suite run.

### 8.y start_scan swapped `market` and `headless` (latent until 2026-10-16)
`start_scan` passed `..., keep_browser_open, headless, market` POSITIONALLY to
`_run_batch(..., keep_browser_open, market, headless)`. The GUI passed None
for both, so nothing showed - until the first live NCL 3-at-once run: both
helper jobs died `'bool' object has no attribute 'upper'` (headless=True in
the market slot), and any `market="CA"` scan would have run on the US
account. **Fix:** keyword arguments. Pinned by
`test_ncl_parallel_workers_2026_10_16.py::..._unswapped` (mutation-tested).

### 8.z NCL "3 at once" and the Canada account (2026-10-16, Neon)
**Never tabs on one login:** NCL keeps ONE current reservation per session -
3 tabs returned 3001055's data for 3001052. **Separate logins work:** 30
bookings, 28 succeeded both ways with 0 differences, 230 s vs 645 s.
Built as round-robin shards: the tab's own browser + 2 helper services, each
its own headless login and session file (`storage_state_NCL_w2/_w3.json`);
NCL tab checkbox "3 at once", enabled only with "Run hidden".
**Canada in the same tab:** after the US pass every NOT_ON_THIS_ACCOUNT
booking is re-checked on the CA login (SAVE_LOGIN option 4), cache bypassed;
NCL rows now carry USD / CAD (were all UNKNOWN; the 2026-10-07 audit had
caught CAD added to USD). "No market" now means US - it used to reuse
whatever was live, which after a CA pass would have been the CA login.
Pinned by `test_ncl_canada_pass_2026_10_16.py`, `test_ncl_parallel_workers_2026_10_16.py`.

## 9. Mistakes made *while fixing*

Recorded because they are cheap to repeat.

1. **A test that pinned the broken mechanism.**
   `test_both_sentinel_paths_read_the_payment_panel` asserted
   `_read_payment_status()` was called ≥3 times. It passed happily while
   the feature did nothing (§2.3). **Assert the outcome, not the
   mechanism.**
2. **Ordering tests via `src.index(...)` matched my own comments** —
   three times in one session. One long-standing test had been passing
   *for the wrong reason*: it compared character offsets and was matching
   a comment, not code. **Strip comments (tokenize) or use AST.**
3. **Counting rows instead of distinct ids** (§4.2).
4. **Three measurements of my own were wrong**: `progress_done` measured
   bookkeeping not work; MSC had run but never persisted; `structure_watch`
   had captured after all.
5. **`release_booking` was called the biggest speed lever at 62% of scan
   time.** Instrumentation said: real release **3.06 s**, stage **9.58 s**,
   gap **6.03 s on 358 of 359 bookings** — `_confirm_candidate_total()`.
   Optimising on the stored number would have attacked a 3-second problem.
6. **Heredoc `\n` escaping corrupted files**, twice. **Use the Edit tool
   for multi-line Python.**
7. **Pinning "the newest capture"** broke a test the moment a new scan
   landed. Pin the capture **day**. **Made again 2026-10-13**:
   `test_3000067_quotes_the_saving_with_the_perk_kept` read the newest
   capture and broke when Neon's 2026-10-14 scan saved one taken after he
   had moved the booking to $5,319. Now pinned to 2026-10-13; the 10-14
   capture has its own test (net $0.00 with the perk kept). Still on the
   newest-capture pattern: `test_readd_value_is_quoted_2026_10_01`.
8. **My 2026-10-12 GoCCL work left 5 tests red, unnoticed for a day.**
   - Three `test_cross_line_audit_2026_09_03` tests built their candidate
     on a booking with no fare name. The new "unknown → Pack & Go is a
     downgrade" rule stopped them before the offer-code rule they exist to
     pin. Fixed in the fixture: same fare type on both sides.
   - `last_review_choice` was added to `PER_BOOKING_STATE` but not
     initialised in `BaseScraper.__init__`, and was still cleared by hand.
     Now initialised in the base. The per-candidate clear inside one
     booking is `{}`, which every consumer reads like `None`.

   **Run the full suite after every change, not just the new tests.**
9. **A background full run piped through `tail` lost pytest's summary**,
   twice, and read as a crash. Every file passed when run on its own.
   Run the suite in halves **by file** (`pytest @files.txt`), not by test
   id: parametrized ids shift whenever a new `page.evaluate` is added
   (`test_evaluate_js_syntax`).

---

## 10. Open questions

- **ESPRESSO Promotions → Compare is built but not proven live** (§1.11).
  Proof: `espresso.perk_compare outcome=DONE` with a `new_total` on a
  column, and a Note starting `PERK KEPT —`. Untested: whether Compare
  accepts two perks with Best Rate unticked (both HARs had Best Rate + one
  perk). If it refuses, the outcome reads `TICK_MISMATCH` or
  `COMPARE_DISABLED` and the step must fall back to one Compare per perk.
  First run supervised.
- **The 2026-10-01 "$97,565 under-quoted" figure (§1.4) is overstated.**
  It summed `abs()` of every re-addable item, perks included, and §1.9
  proved a perk comes back at its price, not free. Re-measure with
  discounts (negative `CRUISE_PROMO`) and perks (positive) kept apart.
- **GoCCL: `TFPA` breaks the invoice check.** Neon's DEMO20 review of FZD
  (2026-10-13): `TARGETED FUNPLAY AMOUNT $100.00` is a casino FunPlay
  CREDIT listed among the price lines but outside the gross (like onboard
  credit), so `core/goccl_review.reconciles()` adds it as a charge, finds
  $2,296 against GRSS $2,196 and leaves the candidate unconfirmed. DEMO16's
  −$50 is the same line on FSA Players Club. Fix: treat TFPA as a perk
  line, never as cash. FZD was $34 dearer anyway, so no saving was lost.
  Casino-restricted offers ("qualifying VIFP Club Member") also need
  keeping out of savings.
- **`test_lookup_reads_the_record_and_screenshots_it[saved_page]` stalls
  at the end of a long run** (seen 2026-10-13 and 10-15) but passes alone
  (`test_vifp_guest_lookup_2026_10_08.py`, 32/32). It opens a real
  Chromium; probably an event-loop or browser leak from earlier tests.
  Run the suite in halves by file until it is found.
- **Why does the ESPRESSO session collapse?** (ROADMAP P0.4) Still
  unexplained. Survivable now, not absent. Needs ~20 minutes of manual
  diagnosis, no code: close every other CruisingPower tab, wait, log in by
  hand, watch two minutes with the scanner off.
- **NCL records no currency.** Its portal renders `$` for both the US and
  CA accounts and never a code, so it cannot be read from the page. The
  market (`US`/`CA`) is known and would settle it — **Neon's call, not a
  guess this project should make.**
- **What do CLOSED and TERMINATED mean on an NCL reservation?** 133 and 18
  captures respectively. Currently reported as before and refused
  permanence.
- **`_confirm_candidate_total()` is ~6 s per booking** — the real speed
  lever, not yet optimised.
- **~20 clicks are still unverified** — they need real post-click signals
  from a live page.
- **67 silent `except: pass` sites.** Most are legitimate best-effort
  probes; they have never been audited one by one (ROADMAP P4.6).
- **2026-10-07 fixes: three now PROVEN live on 2026-10-08** — re-login
  (`espresso.relogin_verified ok=True` ×2), NCL cancelled detection
  (`ncl.cancelled` ×2), NCL exclusions (94 `exclusion.recorded
  cruise_line=NCL`); WLT rows all carry a price (63/63). See `CLAUDE.md`.
- **OPEN, found 2026-10-08: the NCL tab ran the ESPRESSO queue.** At 14:27
  UTC the NCL tab held the same 769 IDs as ESPRESSO and searched 10 of them
  on NCL before being stopped by hand. 10 wrong rows, all
  `cruise_line='NCL' status='NOT_ON_THIS_ACCOUNT'` between 14:28:18 and
  14:32:25 (3001024, 3001025, 3001026, 3001027, 3000064, 3001028, 3001029,
  3001030, 3001031, 3001032) — kept, not deleted. For 3001031 and 3001032
  the wrong row is newer than the real ESPRESSO one. Proposed guard, not
  built: refuse Start when queued IDs do not fit the line (NCL always 8
  digits, ESPRESSO ≤7 in the whole corpus) or were seen under another line.
