# CruiseIntel roadmap

*Written 2026‑10‑01. Every priority below is justified by a measurement from
`cruise_intel.db` or `data/cruiseintel.log`, not by reading code. Where a claim
has no number behind it, it says so.*

**Ordering principle:** a feature is worth nothing while scans don't finish.
P0 is reliability. Everything else waits.

---

## Status at a glance

| | Item | Status |
|---|---|---|
| `P0.1` | Auto‑login and auto‑resume | ✅ done |
| `P0.2` | Resumable jobs | ✅ done |
| `P0.3` | Retry queue | ✅ done |
| `P0.4` | Root‑cause the ESPRESSO session collapse | ◻ open |
| `P0.5` | The release that fails | ✅ fixed, pending live verification |
| `P1.1` | Outcome tracking | ✅ done |
| `P1.2` | Calculator version stamp in the cache | ✅ done |
| `P1.3` | Agency commission | — out of scope |
| `P1.4` | Why we under-quoted | ✅ done |
| `P2.1` | Turn MSC on | ⚠ persistence built, not yet scanning |
| `P2.2` | Revive GoCCL | ◻ open |
| `P2.3` | Build Princess POLAR | ◻ open |
| `P3.1` | Scheduling | ◻ open |
| `P3.2` | A run you can judge from the log | ✅ done |
| `P3.3` | `structure_watch` drift alerts | ✅ done |
| `P4.1` | Make `release_booking` faster | ⚠ partial |
| `P4.2` | Price the perk automatically | ◻ open |
| `P4.3` | Export | ⚠ partial |
| `P4.4` | Backfill permanent exclusions | ◻ open |
| `P4.5` | Revisit price prediction | ◻ open |
| `P4.6` | Audit the silent exception swallows | ◻ open |

**8 of 21 complete**, 3 partial. Detail below — each item states what it is measured against and what "done" means.

---

## P0 — Scans must finish

> **CORRECTED 2026-10-01.** An earlier version of this section read "30 jobs
> queued 9,520 bookings and scanned none of them." **That was wrong**, and
> the error was mine: `progress_done` measures *bookkeeping*, not work.
>
> `_update_job_in_db` is called **exactly once**, in the `finally` at the end
> of a run. A hard death writes nothing, and `reconcile_stale_jobs` then
> marks the row FAILED with `progress_done = 0`. Measured against each job's
> own booking list:
>
> | job | recorded | actually scanned |
> |---|---|---|
> | NCL 2026-09-30 | 0 of 189 | **189 (100%)** |
> | ESPRESSO 2026-09-30 | 0 of 723 | 530 (73%) |
> | ESPRESSO 2026-09-18 | 0 of 721 | 559 (77%) |
> | ESPRESSO 2026-09-22 | 0 of 721 | 156 (21%) |
>
> Across the zero-progress jobs sampled: recorded as **0 of 4,097**, actually
> **2,280 scanned**. One NCL job finished *completely* and is on record as a
> total failure.
>
> This does not remove P0 — it sharpens it. Work IS being lost (the 21% job
> is real), but the headline number was measuring the wrong thing, and no
> resume is possible while progress is only written at the end. **Persist
> progress as it happens and resumability falls out of it.**
>
> Lesson, consistent with METHOD below: a stored counter is a claim, not
> evidence. Check it against what actually landed in the data.

### P0.1 — Auto‑login and auto‑resume ✅ BUILT 2026-10-01

**Neon, 2026‑10‑01:** *"we need to eleminiate check logg in unless this logg
in pops up in case we need to log in manually if it fails on its own, and the
log in especially in esspresso that the script will log in and resume
automatically."*

**Evidence it is worth doing, and that it is close:**

| event | count | meaning |
|---|---|---|
| `login.required` | 339 | a scan stopped and waited for a human |
| `login_check.waiting` | 171 | the GUI sat waiting on the button |
| `espresso.auto_login` | 29 | auto‑login actually ran |
| `espresso.auto_login_failed` | **1** | …and almost never failed |

**Auto‑login already works.** It is simply not trusted to run at the moments
that matter. The credentials are in the OS keyring, `auto_login` returns `OK`
/ `ALREADY_LOGGED_IN` / `FILLED_AWAITING_MFA`, and the 2026‑09‑30 fix already
made **Start** attempt a login instead of refusing.

**Target behaviour**

- **Remove "Check login" from the normal path.** It stays only as a manual
  override, never as a step the user is told to perform.
- On **every** `login.required`, session drop, or `batch.session_expired`:
  attempt `auto_login` → on success **resume from the booking that was
  interrupted**, not from the start.
- Surface a prompt **only** when auto‑login cannot finish on its own:
  - `FILLED_AWAITING_MFA` — a real case, already observed; the credential was
    filled and the portal wants a second factor.
  - `auto_login_failed` — password changed, account locked, portal down.
- When a prompt is unavoidable, say **which line** and **why**, and resume by
  itself the moment the session is live. The user should never have to press
  Start again.
- Respect the existing rate limit (`_RECOVERY_MIN_GAP_S=180`,
  `_RECOVERY_MIN_BOOKINGS=3`, `_RECOVERY_MAX=12`). Auto‑login must not become
  a login loop — ESPRESSO throttles, and 4 logins in a day preceded the
  2026‑09‑30 collapse.

> ⚠ **This depends on P0.2.** "Log in and resume automatically" without
> resumability just means logging in and *restarting*. Build P0.2 first, or
> build them together.

**Done when:** a full ESPRESSO run survives a mid‑scan session drop with no
human input, and `login_check.waiting` stops appearing in a normal run.

---

### P0.2 — Resumable jobs ✅ BUILT 2026-10-01

A job that dies at booking 400 of 723 restarts at zero. That is most of the
57% loss above.

The data to resume **already exists and is unused**: `scan_jobs` stores
`booking_ids_json`, `progress_done`, `progress_total`, and `status`.

**Target behaviour**

- A job records which booking IDs are **done**, not just how many.
- A `FAILED` / `STOPPED` / orphaned `RUNNING` job can be resumed, skipping
  completed bookings.
- Resume on app start: an interrupted job offers to continue.
- The freshness cache and permanent exclusions already prevent redundant
  work — resume should ride on them, not duplicate them.

**Done when:** killing the app mid‑scan and reopening it continues where it
stopped.

---

### P0.3 — Retry queue ✅ BUILT 2026-10-01

**509 ERROR rows, no second attempt.** The top causes are retryable:

| cause | count |
|---|---|
| `wait_for_selector` timeout 60s | 117 |
| "Cannot read categories: VX._form_12 not available" | 58 |
| "Session logged out while searching" | 45 |
| `wait_for_selector` timeout 25s | 45 |

A session logout is not a result — it is a reason to try again. (The 135
`balance_is_all_commission` errors are **not** in scope: all 135 are from
2026‑09‑15 and the bug was fixed that day.)

**Target behaviour:** failed bookings go to a retry queue, attempted again at
the end of the run with a bounded retry count, and only then recorded as
ERROR. Mutating clicks are still **never** retried.

---

### P0.4 — Root‑cause the ESPRESSO session collapse

**Still unexplained.** On 2026‑09‑30 every login died in 60–95 seconds and
**0 of 723 bookings completed** across two runs, with no change to the session
code. Likely the author of most of those 30 dead jobs.

**Diagnose before coding** — close every other CruisingPower tab, wait 20–30
minutes, log in once by hand, and watch whether the session survives two
minutes with the scanner off. That single observation separates "something
else holds the account" from "we are being throttled".

---

### P0.5 — The release that fails

**Fixed 2026‑09‑30, listed so it is verified in the next real run.**
`release_booking`'s fallback path navigated to a **relative** URL, which
`page.goto()` rejects — 21 failures, bookings left locked the full 15 minutes,
live until found. See [ESPRESSO_PERK_TIERS.md](ESPRESSO_PERK_TIERS.md)'s
sibling note in `espresso-release-always` memory.

**Done when:** a full run shows `espresso.booking_release_failed` = 0.

---

## P1 — Know whether any of it worked

### P1.1 — Outcome tracking ✅ BUILT 2026-10-01

**145 ESPRESSO + 54 NCL + 5 GoCCL optimizations worth $31,000 — and the
schema has no column recording whether a single one was acted on.**

`optimization_outcomes` table + `services/outcome_service.py` + a **Verify
selected** button. A verified opportunity leaves the GUI list; the booking
keeps being scanned, because its price can drop again.

**The auto-detection Neon specified works and paid for itself immediately.**
The signal: a later scan's `old_total` equals an earlier optimization's
`new_total` — the quoted price became the price being paid. Run over the full
history: **46 applied repricings worth $5,485.90 that nothing had recorded.**

Realised savings now reported separately from savings *found*. Only APPLIED
counts; a human reviewing a TRAP and declining saved nothing.

Why this is P1 and not P3:

- It is the difference between *"we found $31k"* and *"we saved $31k."*
- It settles the open perk question in **one booking** — do the beverage and
  Wi‑Fi components actually come off on commit, or does only the fare label
  change? Nobody knows, and the invoice cannot say.
- It is the missing **label** for price prediction. The 2026‑09‑21 attempt
  measured a temporal AUC of 0.686 (0.549 without scan‑cadence artifacts)
  partly because there was no real outcome to predict against.

### P1.2 — Calculator version stamp in the cache ✅ BUILT 2026-10-01

A calculator change silently invalidates every cached verdict, and **TRAP is a
cacheable status**. Hit twice on 2026‑09‑30; both times cache entries had to be
cleared by hand, and a missed one would have served a verdict the current code
disagrees with.

Stamp a calculator version into each cache row; a mismatch is a miss.

---

### P1.3 — Agency commission: OUT OF SCOPE

**Neon 2026-10-01, deciding this:** *"ignore any agency commission in the
road map we will calculate it but please do not include it in the scans or
python scripts our main focus is the prices difference."*

Recorded here **so it is not rediscovered and built by mistake.** Repricing
does reduce the agency's commission — booking 3001021's ledger row shows a
$114.00 client saving against an $18.24 commission drop — but the agency
calculates that itself, outside this project.

**Do not** add commission fields, commission maths, or commission columns to
the scanner, the calculator, the database or the GUI. CruiseIntel reports the
**price difference**. That is the whole job.

### P1.4 — Why we under-quoted ✅ SOLVED 2026-10-01

**Answer: by exactly the value of the promo our own note said to re-add.**

| booking | we quoted | with it re-added | Neon's actual |
|---|---|---|---|
| 3001021 | $48 | **$114** | **$114** |
| 3001022 | $50 | **$116** | **$116** |

Both gaps are the value of `Email Bonus NRD` — the fare the result already
carried as `re-add: Email Bonus NRD`. The calculator identified it and left
its money out of the figure, so **every quote carrying a re-addable fare read
low**.

Measured: **401 bookings carry a re-addable promo with a real dollar value,
totalling $97,565** absent from quoted savings. Email Bonus NRD (234),
BONUS SAV NRD (91), WEEKENDSAV NRD (42), BOOKNOWSAVNRD (26).

The note now states the ceiling:

```
optimized $50 — re-add: Email Bonus NRD
              (worth $66.00 — saving becomes $116.00 if re-added)
```

**`net_saving` is NOT inflated by it**, deliberately. Re-adding is a manual
step on the Promotions screen and it can fail; promising money that is not
secured is the OBC mistake reversed, so a re-addable promo can never flip a
verdict. Where no priced `CRUISE_PROMO` line exists the note names the fare
and stops — never guess a value.

Tests: `tests/test_readd_value_is_quoted_2026_10_01.py` (10), two of them
pinned to Neon's real achieved figures.

**Found by accident**, which is worth recording: he reported 3001023 and
3001022 as wrongly showing OPTIMIZATION. The screenshots were a different
rate selection — his own correction — but investigating anyway produced
the second data point that explained the first.

## P2 — Switch on what is already built and idle

| line | bookings in DB | last scanned |
|---|---|---|
| ESPRESSO | 1,040 | today |
| NCL | 235 | today |
| **GoCCL** | **5** | **2026‑07‑27** |
| **MSC** | **0** | **never** |
| Princess POLAR | — | not in `CruiseLine` |

### P2.1 — Turn MSC on ✅ PERSISTENCE BUILT 2026-10-01

**CORRECTED.** This section claimed MSC "has never scanned a booking into
this database." Half right. MSC *has* run — the log shows
`msc_live.browser_started` with a restored session and `msc_live.auto_login_ok`.
Two separate things were wrong, and only one is still live:

1. **`msc.voyagers_club_entry_failed` ×2** — a Playwright actionability
   timeout on `.club-btn[data-cabin="1"]`: the element resolved, the click
   timed out. **Already fixed on 2026-09-22**, the same day it happened
   (real click for 4s, then a JS dispatch). Those log lines predate the fix.
   Nothing to do.

2. **MSC results were never written anywhere.** `MscLiveService.run_batch`
   built `MscCheckOutcome` objects, passed them to the GUI and returned
   them. Nothing persisted one. **Every MSC scan ever run evaporated when
   the window closed** — that is the zero in bookings, scan_jobs,
   price_history and market_data.

**Built:** an `msc_results` table, written per booking.

Deliberately NOT folded into `bookings`. An MSC evaluation is four
independent checks whose `estimated_value` fields carry **different units**
— dollars for PRICE_MATCH, percentage points for DISCOUNT_TIER_UPGRADE,
none for DISCOUNT_ADD or VOYAGERS_SELECTION. Flattening that into
old_total/new_total/net_saving would invent figures MSC never produced, and
the codebase already flags that unit mix as a landmine for any future
aggregator. The checks are stored whole, each with its unit; the fields that
generalise (status, category, has_any_opportunity, opportunity_types) are
promoted to columns so MSC is queryable alongside the other lines.

`INSUFFICIENT_DATA` is never stored as `NO_OPPORTUNITY` — "could not
check" and "checked and found nothing" are different claims.

**Still open:** the MSC discount functions built and tested on 2026-09-23
are still not wired into DISCOUNT_ADD / VOYAGERS_SELECTION. And MSC still
needs a real run to prove the loop end to end — it has history to
accumulate into now, which it never had before.

### P2.2 — Revive GoCCL

Confirmed live 2026‑09‑18, **5 bookings, dormant for two months**. `/review`
was never wired into production.

### P2.3 — Build Princess POLAR

Reverse‑engineered 2026‑09‑16 — 8‑step refare wizard, fares on CATEGORY FARE
COMPARISONS — and never started. **Hazard already documented: CANCEL BOOKING
sits next to REBOOK.** Treat that as a hard constraint from day one.

> **Every new line must use the `finally:` release pattern from day one.**
> ESPRESSO left 389 of 624 bookings locked because it did not.

---

## P3 — Make it run itself

### P3.1 — Scheduling

Scan days: `10‑01, 09‑30, 09‑29, 09‑28,` **5‑day gap**, `09‑23, 09‑22, 09‑21,
09‑18, 09‑17, 09‑16, 09‑15,` **18‑day gap**, `08‑28`.

Prices move daily; they are checked when someone remembers. A nightly
scheduled scan is the single change that converts this from a tool into a
service.

**It is only safe once P0.4 is answered.** An unattended scan that dies
partway and sits waiting for a login is worse than no scan — and on
2026‑09‑30 exactly that happened twice, 0 of 723 bookings, cause still
unknown. P0.1 (Start logs in by itself) and P0.2 (resume) remove most of the
risk, but neither helps if the account cannot hold a session at all.

### P3.2 — A run you can judge from the log ✅ BUILT 2026-10-01

`batch.complete` carried three fields — job_id, status, total — so the log
could not answer "was that run healthy?". Nineteen of them, none informative.

**Still not a notification.** Those were removed on 2026-09-30 because
closing the GUI looked like a crash. This is one structured log line, which
the watchdog parses and a human reads afterwards. Reconstructed against the
real 2026-10-01 16:14 run:

```
requested 306   checked 295   unfinished 11
statuses  NO_SAVING 184 · PAID_IN_FULL 53 · WLT 39 · OPTIMIZATION 10 · TRAP 9
optimizations 10   savings $543.00   errors 0   login_blocked 0
duration 5,870.8s   avg 19.9s/booking
```

`login_blocked` gets its own field rather than hiding inside the error
count: it is the one failure a human fixes in ten seconds.

Derived from `job` alone — `_run_batch`'s counters are declared inside the
`try` and are unbound if it fails early, and this runs in the `finally`.
Guarded, because a summary must never be what breaks a finally block.

### P3.3 — `structure_watch` drift alerts ✅ BUILT 2026-10-01

**CORRECTED.** This section claimed it "has never once captured a baseline".
Wrong — the baselines were written 2026-08-27/28 and the check has worked
since. What actually happened: it fired **six times** (09-28, 09-30, 10-01,
two elements each) and nobody acted, because the warning carried only a name
and a file path.

**It was right every time.** Baseline:
`textbox "Search by Reservation ID, Name or Date"`. Neon's 2026-09-30
recording: `"Find by Reservation ID, Name, Date, etc..."`. And the search
button's FALLBACK selector is that exact old string
(`[aria-label="Search by Reservation ID, Name or Date"]`) — rotted, while
the primary `#searchReservationBtn` quietly carried everything. Catching a
dead fallback before the primary dies too is the whole point.

The alert now carries a unified diff. **The replacement selector was
deliberately not guessed**: in August the accessible name came from an `img`
alt, not an `aria-label`, so the right fallback is a question for a live
page. `tests/test_structure_drift_diff_2026_10_01.py` records the rot and
says so.

## P4 — Speed and leverage, once it is reliable

### P4.1 — Make `release_booking` faster ⚠ INSTRUMENTED 2026-10-01, not yet optimised

**Re-measured over 1,369 real bookings:**

| stage | median | max |
|---|---|---|
| **total per booking** | **16.39s** | 64.68s |
| **release_booking** | **10.20s** | 25.45s |
| search | 3.57s | 52.14s |
| navigate_reservations | 1.11s | 4.08s |
| navigate_home | 1.07s | 2.17s |
| check_login_1 | 0.10s | 1.73s |

`release_booking` is **62% of scan time** — higher than the 46.7% in the
September handoff, because it now runs on EVERY booking rather than the 38%
that used to reach it. Halving it roughly halves a run.

**Deliberately not optimised yet.** The 10s is spread across four steps
(open the dialog, await it, click Exit, settle the page) and nothing said
which owns it. Guessing is how a release gets broken, and a missed release
means a booking locked 15 minutes — the V-VIP bug.

So the four steps are now timed into their own `espresso.release_timings`
line, kept separate from the existing `release_booking` stage so the 1,369
historical records stay comparable. **The next real ESPRESSO run says which
step to attack.**

### P4.2 — Price the perk automatically

The scanner flags *that* a perk tier is given up (134 bookings) but never
*what keeping it costs* — that number lives only on the Promotions → Compare
screen. Neon's 2026‑09‑30 recording has every selector, and the flow mutates
nothing. Blocked on P0.4: it means more pages per booking against a portal
whose session behaviour is not yet understood.

### P4.3 — Export ⚠ partly exists

**Correction to an earlier claim.** There *is* an "Export report" button
(`_on_export`, CSV + XLSX via `services/excel_export.py`). What is missing is
anything **scheduled or sent** — the export only happens when someone presses
it, so it belongs with P3.1/P3.2 rather than here.

### P4.4 — Backfill permanent exclusions

**446 PAID_IN_FULL and 7 CANCELLED bookings** are not excluded — all
historical, from before the feature shipped on 2026‑09‑30. Backfilling saves
real scan time.

⚠ **Needs a decision, not just code.** `record_paid_in_full` deliberately
refuses without readable evidence — that guard exists because of the false
$400 on booking 3001001, which had two cents outstanding. Writing 446
permanent "never scan again" rows on weaker historical evidence is a judgement
call.

### P4.5 — Revisit price prediction

Not viable as measured on 2026‑09‑21 (temporal AUC 0.686; 0.549 without
scan‑cadence artifacts; 81% of prices never move). Root cause was **missing
features**, and 14 driver columns are now captured per scan. Worth re‑measuring
once P1.1 provides real outcomes. **No LLM — hard rule.**

### P4.6 — Audit the silent exception swallows

**68 handlers whose entire body is `pass`.** Many are legitimate — crash
handlers that must not raise, cleanup paths. But this is the same class as the
bug that hid the overnight scan crash: an error never recorded cannot be
alerted on. Worth a pass with judgement, not a blanket rewrite.

---

## Hard rules this roadmap must not break

- **No AI/LLM/API keys anywhere, ever.**
- **Never delete captured data**, and never quietly reduce its fidelity.
- **ESPRESSO never headless** — re‑tested three ways; every headless mode 404s
  at the CDN edge.
- **Never move `release_booking` out of `check_booking`'s `finally`.**
- **A confirmed PAID_IN_FULL or CANCELLED booking is never rescanned** —
  cancellations are still *reported* every run, from the register.
- **Never click final purchase/confirm controls**; mutating clicks are never
  retried.
- **A positive `CRUISE_PROMO` is the price of a perk — keep subtracting it.**
- **Passwords via `save_login.py` + OS keyring only**, never in chat.
- **Never log into ESPRESSO by hand while a scan is running** — one session
  per account.

---

## Method

Every fix in this project that stuck came from **measuring the log or the
database**, not from reading code and guessing. **Comments in this codebase
have lied at least six times**, each describing an intention the code
contradicted, each hiding a real bug for months.

Two test anti‑patterns have bitten repeatedly: **matching prose instead of
code** (strip comments via AST), and **character‑distance windows**
(`assert X in src[i:i+400]`). And: **a status code is evidence; a URL that
failed to change is not.**
