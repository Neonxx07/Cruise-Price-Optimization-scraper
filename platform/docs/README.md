# Platform documentation

Start with the roadmap; the rest are reference and history.

## Current

| Document | What it is |
|---|---|
| [ROADMAP.md](ROADMAP.md) | **Start here.** Prioritised P0–P4, every item backed by a figure from the database or the log |
| [ESPRESSO_PERK_TIERS.md](ESPRESSO_PERK_TIERS.md) | **Read before touching `core/calculator.py`.** The four mutually exclusive perk tiers, why a positive `CRUISE_PROMO` stays subtracted, and a documented dead end that tested green and was wrong |
| [MSC_DISCOUNT_RULES.md](MSC_DISCOUNT_RULES.md) | Which MSC discounts combine, which are agency-side, and which never apply |
| [ESPRESSO_SESSION_BUGS_2026_09.md](ESPRESSO_SESSION_BUGS_2026_09.md) | The measured session and navigation failures, and what fixed them — including the evidence that ESPRESSO cannot run headless |

## Session handoffs

Chronological working notes. Each carries the hard rules, what was fixed with
its measurements, and what was still open at the time. The **newest is the
authoritative one**; older entries are kept because they record *why* a rule
exists, which the rule itself does not.

| Date | Handoff |
|---|---|
| 2026-10-01 | [HANDOFF_2026_10_01.md](HANDOFF_2026_10_01.md) |
| 2026-09-30 | [HANDOFF_2026_09_30.md](HANDOFF_2026_09_30.md) |
| 2026-09-23 | [HANDOFF_2026_09_23.md](HANDOFF_2026_09_23.md) |

## Conventions

- **Measure, don't guess.** Every claim in these documents should trace to
  `cruise_intel.db` or the run log. Comments in this codebase have described
  behaviour the code contradicted more than once, each time hiding a real bug
  for months.
- **No real booking references.** Use the placeholders described in
  [`SECURITY.md`](../../SECURITY.md) — `300xxxx` for numeric, `DEMOnn` for
  PNR-style. This repository is public.
- **Record dead ends as prominently as fixes.** A wrong approach that tested
  green is the one most likely to be re-invented.
