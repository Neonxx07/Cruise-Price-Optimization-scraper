# MSC discount rules — map, evidence, and build plan

Written 2026-09-22, from Neon's rules and booking **3000005**. Plan first,
per his instruction: *"do not just code it make a plan map document and start
building code after arranging the infromation."*

---

## 1. The rule that changes everything

> **(b) reprice at today's 2,109.54 rate with the discount applied AND THE
> CUSTOMER HAS A LOWER PRICE** — Neon, 2026-09-22

Adding a discount does **not** take a percentage off the customer's existing
fare. It **re-prices the booking at today's rate** and then applies the
discount. So a discount is only worth having when the repriced total lands
**below** what they already pay.

### Booking 3000005, the case that exposed it

| | |
|---|---:|
| current booking total | **1,756.42** |
| today, same category BL3 | **2,109.54** |
| today − current | **+353.12** |

| discount applied to today's price | result | vs current |
|---|---:|---:|
| 5% Voyagers Club | 2,004.06 | **+247.64** |
| 10% Voyagers Selection | 1,898.59 | **+142.17** |
| **both stacked (15%)** | **1,793.11** | **+36.69** |

**Every one leaves the customer worse off.** Yet the run reported:

```
DISCOUNT_ADD          OPPORTUNITY   "Voyagers Club 5% available today"
VOYAGERS_SELECTION    OPPORTUNITY   "MSVG10W (10%) available on this sailing"
```

Both are false. This is the same failure class as MSC's fabricated $267.01
and GoCCL's +$184-that-was-really-−$6: **two numbers compared that do not
cover the same thing** — a discount percentage quoted against a base nobody
checked.

---

## 2. Rules, as stated

| ID | Rule | Source |
|---|---|---|
| **MSC-D1** | Applying a discount reprices at **today's** rate, then discounts it | Neon, explicit |
| **MSC-D2** | A discount is an opportunity **only if** `today × (1 − pct) < current_total` | follows from D1 |
| **MSC-D3** | After final payment: **no price match**; discounts may still be applied — D2 still governs | Neon |
| **MSC-D4** | `SPECIAL OFFER n%` (MSC Selection) is revealed by the **crown icon** | Neon |
| **MSC-D5** | `SPECIAL OFFER` and the senior discount are **mutually exclusive** — the block bites only once the senior discount is **actually applied**, not merely because a senior is aboard | Neon, corrected 2026-09-22 |
| **MSC-D6** | The promotional slot holds **one** discount — *"u can choose between one only i usually choose whatever is higher"*. Pick the highest; never sum | Neon |
| **MSC-D7** | `MSCCLUB5` (5% loyalty) is a **separate slot** and stacks with the promotional one | evidence, 13 bookings |
| **MSC-D8** | A **purchased** `OBS` line sits inside the booking total; subtract it before comparing against a cabin-only quote | evidence, 12 bookings |

---

## 3. What already exists (do not rebuild)

| Piece | Where | State |
|---|---|---|
| `SPECIAL OFFER n%` label parsing | `calculator_msc.py` | 5/10/15% seen; 415 occurrences in captures |
| MSVG10W/MSVG15W ↔ SPECIAL OFFER mapping | `calculator_msc.py:24` | present |
| "already applied" guard | `_ALREADY_APPLIED_LABELS` | present |
| Senior needs **2+** seniors aged 65+ | `_filter_out_ineligible_senior_discount` | present, correct |
| Club-discount comparability guard | `calculator_msc.py:351` | present, correct |
| Final-payment gate on PRICE_MATCH | `_check_price_match` | present, correct |
| Voyagers crown entry | `msc_commands.py:_apply_voyagers_club` | click fixed 2026-09-22 |

**Missing — the whole of this document:**

- No check compares a discounted **today** price against the **current** total
  (verified: no `today_price`/`current_total` reference in any discount check)
- No senior exclusion on `SPECIAL OFFER` (MSC-D5)
- No senior-vs-special-offer percentage comparison (MSC-D6)

---

## 4. Decision map

```
booking
  ├─ cancelled? ──────────────────────────► CANCELLED        (already built)
  ├─ paid in full? ───────────────────────► PAID_IN_FULL     (already built)
  │
  ├─ PRICE_MATCH
  │     final payment passed? ────────────► NO_OPPORTUNITY   (already built, MSC-D3)
  │     today < current? ─────────────────► OPPORTUNITY
  │
  └─ DISCOUNT checks  (DISCOUNT_ADD / VOYAGERS_SELECTION / TIER_UPGRADE)
        │
        ├─ today price unknown? ──────────► INSUFFICIENT_DATA      ← NEW
        ├─ club held but not in quote? ───► INSUFFICIENT_DATA (already built)
        │
        ├─ senior on booking?
        │     └─ SPECIAL OFFER excluded (MSC-D5)                   ← NEW
        │        take max(senior%, other eligible%) (MSC-D6)       ← NEW
        │
        └─ best eligible pct
              today × (1 − pct) < current? ─► OPPORTUNITY, value = current − repriced
              otherwise ───────────────────► NO_OPPORTUNITY,                ← NEW
                                             "discount does not beat the
                                              fare they already hold"
```

---

## 5. Source of truth

| Field | From | Never |
|---|---|---|
| current total | booking invoice (`current_value`) | inferred from a percentage |
| today's price | `today_price_same_category`, same category only | a different category |
| discount % | the label's printed percentage | assumed from a code name |
| senior count | passenger ages, 65+ | `all_seniors` (wrong: 1 senior passes trivially) |
| eligibility | MSC's own disclosed options | our own catalogue alone |

**Missing stays missing.** No today price ⇒ `INSUFFICIENT_DATA`, never
`NO_OPPORTUNITY` — silence is not evidence of absence.

---

## 6. Failure scenarios the build must prevent

| # | Scenario | Without the fix |
|---|---|---|
| F1 | today > current, discount offered | **false OPPORTUNITY** — 3000005, customer $36.69 worse off |
| F2 | senior booking, SPECIAL OFFER listed | recommends an **inapplicable** discount |
| F3 | senior 10% vs special 15%, senior-ineligible | recommends the **smaller** discount |
| F4 | today price unreadable | silently `NO_OPPORTUNITY` — hides a real opportunity |
| F5 | discount already applied | double-counts it |

---

## 7. Build order

1. `msc_discount_beats_current()` — pure, tested against 3000005's real figures
2. Wire into `DISCOUNT_ADD` and `VOYAGERS_SELECTION`
3. Senior exclusion (MSC-D5) + higher-percentage rule (MSC-D6)
4. Regression tests per failure scenario above
5. Re-run 3000005 and confirm it flips to NO_OPPORTUNITY

---

## 8. Open questions — answered from evidence 2026-09-22

Investigated per Neon: *"OBC or perks are usually at the down of the page the
original booking page usually written investigate this with the bookings u
have."* Corpus: 133 bookings with a price breakdown in
`data/msc_control/booking_data.jsonl`. He was right about the location — perk
lines sit a **median 0.70 of the way down** the page text.

### Q1 — do discounts stack multiplicatively or additively? **Multiplicatively**

`VOYAGERS EXCLUSIVES` is printed by MSC at **9.75%**, in 9 bookings, and it
**never** appears alongside `MSCCLUB5`. 1 − (0.95 × 0.95) = **9.75%**;
additive would be 10.00%. MSC merges two 5% discounts into one line using its
own multiplicative arithmetic.

Strong, but circumstantial — the captures show post-discount money only, so
the club+special-offer pairing is not arithmetically proven. **Never assume
9.75% = 10%**: a catalogue lookup would have been wrong by 0.25pp on 9 bookings.

### Q2 — does repricing reset OBC / perks? **Two different answers**

`OBS` is the perk line code, and the split is clean:

| | bookings | in the total? | at risk on a reprice |
|---|---:|---|---|
| **Purchased** OBS — `Shipboard Credit 25 USD - Non Refundable`, `Hotel service charge`, `FUN PASS 60` | 12 | **yes**, $4.50–$614.81 | the customer paid for it |
| **Free** OBS — `FANTASTICA/BELLA/AUREA/SUITE EXPERIENCE BENEFITS`, drinks packages, `Shipboard credit 50 Eur/Usd - 40 Gbp` | 93 | no, $0.00 | bundled with the **fare code** — a different fare can drop it silently |

**The purchased kind is a live correctness bug.** Booking 3000077 prints
338.20 + **25.00** + 75.00 + 62.70 = 500.90 — the credit is provably inside
the stateroom total. Comparing that total against a cabin-only quote for today
is the same failure class as the fabricated $267.01: two numbers that do not
cover the same thing. Across the corpus **$2,060.81** sits inside booking
totals this way; the largest single booking carries **$614.81**.

`purchased_extras_total()` in `core/msc_booking_extras.py` extracts it.

### Q3 — is SPECIAL OFFER excluded for any senior, or only when applied?

**Only when the senior discount is actually applied** — Neon, and the
evidence agrees: **0 of 133** bookings carry both a senior discount and a
special offer, so the exclusivity is real, but it is exclusivity between
*applied* discounts, not a ban on senior passengers.

My first implementation excluded SPECIAL OFFER whenever any passenger was 65+.
That would have suppressed a usable 15% on every booking with a senior aboard.
Corrected the same day; `senior_discount_applied()` now reads the fact off the
page instead of inferring it from ages.

---

## 9. What the bottom of the page turned out to be worth

The same block carries the **already-applied discounts, named and rated by
MSC** — an authoritative "already applied" signal that beats inference:

```
MSC Club Discount: MSCCLUB5 - Discount Type: Percentage - Discount Rate: 5.0%
Discount Description: VOYAGERS EXCLUSIVES - ... - Discount Rate: 9.75%
```

Every combination observed in 133 bookings:

| combination | bookings |
|---|---:|
| `VOYAGERS EXCLUSIVES` 9.75% alone | 9 |
| `MSCCLUB5` 5% alone | 8 |
| `MSCCLUB5` + `SPECIAL OFFER 15%` | 6 |
| `MSCCLUB5` + `SPECIAL OFFER 10%` | 5 |
| `MSCCLUB5` + `SPECIAL OFFER 5%` | 1 |
| `MSCCLUB5` + `SENIOR DISCOUNT 10%` | 1 |

Two slots, not one: **loyalty** (MSCCLUB5, always 5%) stacks with **one
promotional** discount (SPECIAL OFFER / SENIOR / VOYAGERS). Neon's "one only"
governs the promotional slot — confirmed, never once violated.

## 10. Still open

- Arithmetic proof of multiplicative stacking for club + special offer
  (needs one booking captured with its pre-discount base showing).
- Whether a reprice onto a different fare code drops **free** perks — the
  captures show what a booking *holds*, not what a reprice *does* to it.
  Needs one before/after reprice observation.
- `msc_discount_beats_current` / `best_eligible_discount` /
  `purchased_extras_total` are built and tested but **not yet wired** into
  `DISCOUNT_ADD` / `VOYAGERS_SELECTION`.
