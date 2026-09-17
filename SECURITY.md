# Security & Data Handling

This repository is **public**. It automates work against live travel-agent
booking portals, so the working environment around it holds real customer and
operator data. None of that data belongs here.

This document is the rule set, and — more usefully — the record of how it has
actually gone wrong, since every rule below exists because something slipped.

---

## What must never be committed

| Category | Examples | Where it should live instead |
|---|---|---|
| **Booking references** | a bare 5–9 digit number, or a 6-character PNR-style code (GoCCL/Princess) | Use placeholders: `3000xxx` (numeric), `DEMOnn` (alphanumeric) |
| **Customer / operator identity** | passenger names, the agent's own name, agent login, agency id | Say "the project owner" / "the operator" |
| **Credentials** | portal passwords, API keys, tokens, private keys | OS keychain via [`platform/save_login.py`](platform/save_login.py) |
| **Session state** | `storage_state*.json`, cookie jars | `platform/browser-profile/` (git-ignored) |
| **Captured data** | `*.jsonl`, page/HTML/screenshot captures, `data/` | `platform/data/` (git-ignored) |
| **Databases** | `cruise_intel.db` and any `*.db.backup_*` copy | stays local, git-ignored |
| **Booking-ID input lists** | any watchlist file | git-ignored by allowlist (see below) |
| **Local paths** | `C:\Users\<name>\...`, `/Users/<name>/...` | derive from `__file__` or the script location |

## The three defences

**1. `.gitignore` — allowlist, not blocklist.**
Loose files under `platform/` are ignored by default, and only genuine source
(`requirements*.txt`) is re-admitted. This inversion was forced by experience:
the rules used to name files individually, and a new batch of ad-hoc lists
(`Watchlistncl_us.txt`, `ncl_diag.txt`, `ncl_pilot.txt`, plus a 29 MB
`cruise_intel.db.backup_*`) carried **167 real booking numbers** straight past
them. Naming each new file always loses that race.

**2. The pre-commit hook — install it, once per clone.**

```bash
./.githooks/install.sh
```

Git deliberately does not install hooks on clone, so **every fresh clone needs
this**, including after a history rewrite. It blocks the six categories above
by *shape*, and reports file and line.

For exact private values (your real name, agent login, agency id), add them to
the local denylist the installer creates:

```
.git/sensitive-terms.txt      # inside .git/ — never committed
```

The hook itself is public, so it contains no real values — only patterns.
A genuine false positive can be bypassed with `git commit --no-verify`.

**3. `.gitattributes` — line-ending normalisation.**
Not a privacy control directly, but a review one: a Windows round-trip used to
rewrite whole files as CRLF, so git reported unchanged files as fully
rewritten (`msc_commands.py`: 6,226 changed lines when 82 were real). Real
edits hide inside noise like that, and hidden edits are where mistakes live.

---

## Incident record

Kept deliberately, because the pattern matters more than any single event.

| Date | What happened | Lesson now encoded |
|---|---|---|
| 2026-08-15 | Real booking IDs in doc/code comments as worked examples | Use placeholders; hook rule 3 |
| 2026-08-15 | `.gitignore` narrowed to track `data/msc_control/` as "work product" — it was live scrape output | Blanket-ignore captured data |
| 2026-08-15 | Operator's real name and `C:\Users\<name>\` path in a script and a stray transcript | Hook rules 2 and 4 |
| 2026-08-27 | 5 real IDs found still public after 12 days — the scan's ID list was built from watchlists only and never included the database | Scans must draw from *every* source |
| 2026-08-29 | 167 real IDs in newly-named watchlist files; 29 MB DB backup didn't match `*.db` | `.gitignore` inverted to an allowlist |
| 2026-09-17 | **Alphanumeric** references (GoCCL/Princess) found public — every prior scan matched only 5–9 digit numbers | Hook rule 3 covers PNR shapes |
| 2026-09-17 | A `git add -A` committed two stray transcripts that had been excluded by hand every session | Named in `.gitignore`; hook rule 1 |

Two themes run through all of it:

- **A scan is only as good as its reference list.** Two separate exposures
  came from scanning for the wrong *shape* of identifier, not from failing to
  scan. Both were invisible until something unrelated surfaced them.
- **A manual step that works every time still fails eventually.** Excluding
  those two files by hand worked for a month and then didn't. Anything relied
  on repeatedly should be mechanical.

---

## If something does get committed

1. **Don't force-push reflexively.** Removing it from the current tree is a
   one-commit fix; rewriting history is disruptive and invalidates every clone.
2. Fix the working tree first and push that, so the current code is clean.
3. Then decide on history. A rewrite (`git filter-repo --replace-text`) plus a
   force-push is the only way to remove it from past commits — but everyone
   must re-clone, and it **cannot undo the exposure**. Anything public should
   be treated as disclosed: forks, existing clones and caches keep their copy.
4. If real customer data was exposed, that is a data-protection matter, not
   just a git one. Handle it as such, independently of the cleanup.
