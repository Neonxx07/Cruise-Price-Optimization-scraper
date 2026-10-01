# ESPRESSO session, login and GUI-hang bugs — September 2026

Five bugs, fixed 2026-09-23 and 2026-09-28. Four of them are the **same root
cause wearing different clothes**, and the fifth had nothing to do with the
scraper at all.

Every figure below is measured from `data/cruiseintel.log`, not estimated.

---

## The one root cause: asking a page a question before it stopped moving

`BaseScraper.navigate()` waits for `domcontentloaded`, which fires long
before ESPRESSO's pages finish their own session bootstrap and redirect
chains. Four separate faults all came from reading a page mid-flight and
believing what it said.

| # | Symptom | Where the question was asked too early |
|---|---|---|
| 1 | 268 aborted navigations, 42% retry rate | navigating away from `/home` after 136 ms |
| 2 | Operator logged out 4× during testing | deep-linking into WebFlow after an OAuth landing |
| 3 | Scan died on a **successful** re-login | `_check_login()` 0 s after `auto_login()` |
| 4 | **Auto-login silently did nothing** | `_check_login()` 2 s after browser start |

The fix in every case is `EspressoScraper._settle_navigation()` — poll
`page.url` until it stops changing, bounded, never raises. It polls the URL
rather than using `networkidle`, which never settles on a portal that
long-polls.

---

## Bug 1 — aborted navigations (fixed 2026-09-23)

**Symptom.** 271 navigation retries in one run, 268 of them
`net::ERR_ABORTED`, every one on `reservations.do`.

**Cause.** The flow loaded `/home`, spent a median **136 ms** in
`_check_login`, then immediately navigated to `reservations.do` — while
`/home`'s own session-bootstrap redirect was still in flight. That redirect
cancelled our navigation.

The correlation was decisive. Time spent on `/home` before leaving:

| | median on /home | cost of next navigation |
|---|---:|---:|
| aborted (270 bookings) | **668 ms** | 3130 ms |
| clean (374 bookings) | **1068 ms** | 1015 ms |

The bookings that left `/home` **fastest** are exactly the ones that broke.

**Fix.** `_settle_navigation()` after `navigate(/home)` and **before**
`_check_login`.

**Verified live, 2026-09-28:**

```
                navigations   retries          ERR_ABORTED
before (09-23)         775    273 (35.2%)              268
after  (09-28)         297      1 ( 0.3%)                0
```

---

## Bug 2 — the `/logout` loop (fixed 2026-09-23)

**Symptom.** *"after i log in you go to a link ends with /logout and i have
to login again"* — four times in a row.

**Cause.** The portal arms this on **every** page load (documented at
`scraper/espresso.py:430`):

```javascript
setTimeout(function(){
    window.location.href = window.Base.flowExecutionURL + "&_eventId=logout"
}, 1830000);
```

`flowExecutionURL` is a **Spring WebFlow execution key**. Deep-linking
straight to `reservations.do` right after an OAuth landing enters that flow
from outside, and an invalid flow execution resolves to exactly that logout
event. It was our navigation triggering the portal's own logout.

**Fix.** Never navigate the operator's browser after a login lands. The
session lives in **cookies**, which can be read from the browser context
without touching the page.

> **RULE: never deep-link into ESPRESSO's WebFlow from a freshly
> authenticated browser.**

---

## Bug 3 — recovery gave up after a successful re-login (fixed 2026-09-28)

**Symptom.** *"the esspresso login issue still exists and i needed to login
twice"*. The log contradicted itself:

```
15:00:16  batch.session_expired_recovering
15:00:30  espresso.auto_login        result=OK      <- re-login SUCCEEDED
15:00:30  batch.session_recovery_gave_up            <- ...and gave up anyway
```

**Two stacked causes.**

1. `_check_login()` ran in the **same second** as `auto_login()`, while the
   SSO chain (`login → auth → oauth/callback → app`) was still in flight. It
   sampled a page mid-hop and returned False on a session that was fine.
2. Even when `recovered` was True, the code **fell through** into the
   "could not get back in" block, which sets `job.error` and `break`s the
   batch. The comment there read *"Fall through: … the rest of the batch
   continues"* — describing behaviour the code did not have.

**Fix.** Settle, then poll `_check_login` for ~15 s; and add the missing
`continue` so a successful recovery carries on.

---

## Bug 4 — auto-login silently did nothing (fixed 2026-09-28)

**Symptom.** *"i have entered the username and password in the cmd and now i
have opened the esspreesso but the project did not log in automatically"*.

**Cause.**

```
18:05:33  browser.started      restored_session=True
18:05:35  espresso.auto_login  result=ALREADY_LOGGED_IN    <- 2 seconds later
18:14:12  login.required       url=.../login               <- it was on the login page
```

`auto_login()` opens by asking "am I already logged in?" — a legitimate
guard, because ESPRESSO allows **one session per account** and re-submitting
a login over a live one is risky. But it asked **two seconds after the
browser started**, while a restored-but-dead session was still redirecting
itself to `/login`. It sampled a half-loaded page, concluded it was
authenticated, and **returned without ever filling the credential**. It does
not run twice, so the GUI sat polling for a human who should not have been
needed.

**Fix.** `_settle_navigation()` before the `ALREADY_LOGGED_IN` check. The
early return is preserved; it simply cannot fire on a page mid-redirect.

**Verified live, 2026-09-28 18:29:**

```
18:29:42  espresso.auto_login_form_found   user_field=... pass_field=...
18:29:53  espresso.auto_login              result=OK
18:29:53  login_check.success              via=auto_login
```

`auto_login_form_found` had never appeared before — the false
`ALREADY_LOGGED_IN` returned before the code ever reached the form.

---

## Bug 5 — the GUI hang. Not a scraper bug at all (fixed 2026-09-28)

**Symptom.** *"the gui is not responding"*. Windows agreed:
`IsHungAppWindow = True`, window not pumping messages, **CPU 0.0 %**.

**Diagnosis.** `py-spy dump` on the frozen process:

```
Thread 5628 (idle)
    _on_login_check (gui\windows.py:657)      <- print("GUI: _on_login_check entered")
    _run (asyncio\events.py:94)
    timerEvent (qasync\__init__.py:307)
```

Stack **identical across dumps**, **zero child processes** — the browser had
never launched. The UI thread was blocked inside `print()`.

**Cause.** The GUI starts from a `.bat` through `cmd.exe`, so it owns a
console window. **Windows QuickEdit pauses console output the moment anyone
clicks or selects text in that window**, and a paused console blocks whoever
is writing to it. One stray click froze the entire application.

**Fix.** All **8** `print()` calls on UI-thread paths replaced with logger
calls. The log goes to a rotating **file**, which no mouse can pause. A test
enumerates `print()` calls across the GUI modules by AST and fails if any
return.

**Immediate unstick if it ever happens again:** click the console window and
press `Esc`.

---

## Also fixed the same fortnight

### Bookings left locked (2026-09-23)

ESPRESSO holds a **15-minute lock** on any retrieved reservation.

```
bookings opened : 624
  released      : 235
  LEFT LOCKED   : 389    (62%)
release events  : 293 released, 1 skipped, 0 FAILED
```

The release mechanism was never broken — it was barely ever *called*.
`release_booking` had **one call site** on the happy path while
`check_booking` had **15 returns and 6 raises**; 19 of the 20 exits skipped
it. The comment above that call claimed it ran "for every branch above".

**Fix.** `check_booking` is a thin wrapper with the release in a `finally`.
Idempotent via `_released_for`, set only on a **confirmed** release.
**Verified live:** 15/15 raise paths went through the `finally`.

> **RULE: never move the release back onto a branch.**

### GUI heaviness (2026-09-28)

`_update_queue_view` built a `QWidget + QHBoxLayout + QLabel (+ QPushButton)`
per row and rebuilt the whole list whenever **any** booking changed state.

| | 721 rows |
|---|---:|
| widget per row (old) | **352.8 ms** |
| plain text item | 32.0 ms |
| **in-place update (now)** | **0.002 ms** |

~2,163 rebuilds per 721-booking run × 353 ms ≈ **12.7 minutes of frozen
window per scan**, on the UI thread. Rows are now plain items updated in
place; a full rebuild happens only when the booking set changes, or when the
widget has got out of step with what we think is in it.

---

## Tests

| file | covers |
|---|---|
| `test_espresso_settle_navigation_2026_09_23.py` | bug 1, ordering guard |
| `test_espresso_release_always_2026_09_23.py` | the release `finally` |
| `test_session_recovery_2026_09_28.py` | bug 3, both halves |
| `test_autologin_settle_2026_09_28.py` | bug 4 |
| `test_no_stdout_on_ui_thread_2026_09_28.py` | bug 5 |
| `test_gui_results_table.py` | in-place queue updates |

---

## What this fortnight should teach the next person here

**Comments lied five times.** "Placed here so it runs for every branch
above", "Fall through: the rest of the batch continues", "never works
headless" (true, but untested against new headless) — each described an
intention the code did not implement, and each hid a real bug for months.
**Verify claims against `data/cruiseintel.log` and `cruise_intel.db` before
trusting them — including comments, docstrings, and test docstrings.**

**Measure before blaming.** "The GUI is slow/hung" was unfalsifiable until
`py-spy dump` turned it into a line number in ten seconds. "Headless is
blocked" only became solid once it was tested anonymously, with a stale
session, and with a live warm session.

**A status code is evidence; a URL that failed to change is not.** One test
here reported `SUPPORTED` for headless because a 404 error page served at
the requested URL satisfied a "did we get bounced?" check.
