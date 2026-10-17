# ESPRESSO perk tiers — what a reprice actually gives up

*Established 2026‑09‑30, from booking 3001020 and the portal's own Promotions
screen. Read this before changing anything in `core/calculator.py` that touches
`_get_packages`, `_is_re_addable`, or `perk_tier_change`.*

---

## The short version

A Celebrity fare carries exactly one **perk tier**, and repricing can silently
swap it. The invoice will not tell you — you have to read the fare name.

| tier | what the client gets |
|---|---|
| `ALL INC 2PK` | All Incl Bev and Wifi |
| `RETREAT` | Retreat beverage and wifi |
| `NOPERK` | No Perk Rate |
| `STANDARD` | — |

They are **mutually exclusive**: on the Promotions screen no tier appears in
another tier's "Combined With" column. Every *savings* offer — `BONUS SAV`,
`BOGO75`, `MilitarySav`, `SAVEUPTO100` — combines with every tier, so swapping
those is not a perk change.

**Across all 3,074 captured reprice responses: 148 tier changes, every one a
downgrade.** `ALL INC 2PK → NOPERK` (134) and `NOPERK → STANDARD` (14). A
reprice has never once handed a client a better perk.

Full matrix, with the Compare figures, in
[`data/reference/espresso_offer_matrix_2026_09_30.json`](../data/reference/espresso_offer_matrix_2026_09_30.json).

---

## The case that exposed it: booking 3001020

Neon checked it by hand and said it was not more expensive — *"it is the same
price because of allinc2pk nrd."* The tool had reported:

```
old 3804.66 -> new 3258.66      raw drop        546.00
lost_pkg_names: ["ALL INC 2PK NRD", "ALL INC 2PK NRD ($1260.00)"]
net_saving: -1974.00            status: TRAP
```

`546 − 1260 − 1260 = −1974` exactly. **The same line was charged twice** — once
as a lost package, once as a lost fare. The duplicate had been sitting in
`lost_pkg_names` the whole time: the same name from two sources, one bare and
one priced.

| | |
|---|---|
| reported | TRAP −$1,974 |
| **correct** | **TRAP −$714** |

$546 drop, $1,260 perk given up. Still a trap — **the verdict was right, the
magnitude was wrong by $1,260**, and the magnitude is what Neon judges by.

Corpus‑wide: **21 bookings corrected, $26,887 of phantom loss removed, 0
verdicts reversed.**

---

## Why a positive `CRUISE_PROMO` stays subtracted

`ALL INC 2PK NRD` appears on the invoice as **+$1,260** — a *charge*, not a
discount. It is the **price of the perk**, and it must keep counting against
the saving.

The Promotions **Compare** screen proves it: under `ALL INC 2PK NRD`, prices
sit **above** Best Rate.

| category | Best Rate | ALL INC 2PK NRD |
|---|---|---|
| A2 Aqua Class | $5,328.00 | **$6,210.00** |
| A1 Prime AquaClass | $9,433.00 | $9,433.00 |
| SC Sunset Concierge | $5,855.00 | $5,855.00 |
| all suites (PS·RS·HS·CS·AS·SS·S1·W) | — | **CLS** |

You *pay* for all‑inclusive. Give up the tier, give up the perk.

### ⚠ Dead end — do not retry

A "fix" that **excluded positive `CRUISE_PROMO` lines from `_get_packages`**,
reasoning that dropping a charge saves money, was written, tested green, and
reverted the same day. It is wrong:

- `price_drop` **already carries** the saving from not paying the charge.
  Removing the perk's value as well counts the same event twice, in the other
  direction.
- It turned 3001020 into a **$546 OPTIMIZATION** — advice to trade drinks and
  Wi‑Fi for $546.

`test_a_paid_for_perk_is_still_subtracted` pins this shut.

### ⚠ `newInvoice` components are not evidence

It is tempting to argue "the perk survives, `BEVCLINGRA9` and `BASICWIFCH9` are
still in the new invoice." **They always are.** In **148 of 148** tier changes
the COMPONENT lines are *identical* in old and new — including the 14
`NOPERK → STANDARD` moves where the perk plainly cannot survive.

**The reprice modal echoes the booking's existing components into the
preview.** Only the fare tier decides. `test_new_invoice_components_are_not_evidence_a_perk_survives`
will fail loudly if the portal ever stops echoing, which is the signal to
re‑derive this section rather than edit the test.

---

## How to check a booking by hand

All four steps are read‑only. Nothing is submitted.

1. **Sidebar → Promotions**
   `/espresso/protected/reservations.do?execution=…&_eventId=linkToPromotionList`
2. Tick the tier you want to price — e.g. `ALL INC 2PK NRD` — alongside
   **Best Rate** (`#checkbox-2`)
3. **Compare** (`#promoFare00`, `a.btnCompare`)
4. Read **your own category row** in the new
   `Individual - <offer>` column. For 3001020 that is **X · Guarantee Veranda**

If that figure is about what the client pays now, it is the same price and
there is nothing to do.

**Two traps when reading it:**

- the Compare grid is **per guest**, not the booking total;
- an offer can be **CLS (closed)** on a category even while it is live on the
  sailing — every suite is CLS for `ALL INC 2PK NRD` on this one.

The offer is **re‑addable**: the same screen puts it back on the booking
(`_eventId=fareDetails&fareCodes=DI980793`). Re‑adding costs the $1,260 again,
which is exactly why this reads as "about the same price" rather than a win.

---

## What the tool does, and does not do

**Does — automatically, on every scan, with no extra page loads.** The tier is
derived from `oldFares`/`newFares`, which are already captured. A flagged
booking reads:

```
trap - do not reprice — re-add: ALL INC 2PK NRD, BONUS SAV
  — PERK TIER ALL INC 2PK → NOPERK
    (confirm on the Promotions screen before repricing; the offer can be
     re-added there)
```

**134 bookings** in the corpus are in this position.

**Does not.** The scanner never opens Promotions or Compare — verified, the
only references in the repo are comments. So it reports *that* a tier is being
given up, never *what keeping it would cost*. That number exists only on the
Compare screen.

Wiring it in is a real option: Neon's recording of 2026‑09‑30 has every
selector, and the flow mutates nothing. It is not built, deliberately — it
means driving the live portal through extra pages per booking, and the session
collapse of 2026‑09‑30 is still unexplained.

**Open question, one observation away from settled:** do the beverage/Wi‑Fi
components *actually* come off on commit, or does only the fare label change?
Reprice one of these and look at the booking afterwards, and it is answered.

---

## Where this lives in code

| what | where |
|---|---|
| tier detection | `core/calculator.py` · `PERK_TIERS`, `_perk_tier`, `perk_tier_change` |
| the perk stays charged | `core/calculator.py` · `_get_packages` |
| never charge a name twice | `core/calculator.py` · `already_counted` |
| `ALL INC` is re‑addable | `core/calculator.py` · `_READDABLE_PATTERNS` |
| tests (15) | `tests/test_all_inc_2pk_perk_tier_2026_09_30.py` |
| offer matrix | `data/reference/espresso_offer_matrix_2026_09_30.json` |

### A note on the test fixture

This bug survived for months because `tests/test_calculator.py` built a perk as

```python
{"paxId": "total", "type": "CRUISE_PROMO", "name": ..., "amount": +594}
```

— a row the portal **never sends**. Measured: all 46,573 real `paxId == "total"`
rows are **untyped**; all 22,839 `CRUISE_PROMO` and all 7,626 `COMPONENT` rows
are **per‑passenger**. The tests passed on impossible data while 167 real cases
went unnoticed.

The fixture now emits the real shape — an untyped summary row plus a typed
per‑passenger row. Every test that broke passes again unmodified, which is what
confirms real perk detection is intact.
