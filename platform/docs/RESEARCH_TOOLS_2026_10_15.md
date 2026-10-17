# Tools and patterns to make CruiseIntel more stable — research 2026-10-15

Neon: *"do a google search and do a deep research ... specially on github to
make this project more concrete stable and future proof faster"*. Licences,
versions and dates were read from PyPI / the GitHub API on 2026-10-15.
Written under the old no-AI rule; AI now lives in ROADMAP P5.

## Verified gaps in this project (checked against the files)

- **The database had one backup** — 29 MB from 2026-08-27 against a 495 MB
  `cruise_intel.db`. A verified backup was taken the same day:
  `data/backups/cruise_intel.db.backup_20261015_163410` (integrity ok, 18,387
  booking rows). Scheduled backups are still to be built (item 1).
- **Old logs are deleted** — `utils/logging.py` rotates 10 MB × 5 and drops the
  oldest, against rule 1 (never delete captured data). Item 2.
- **Money is stored as float** — 18 `Float` columns, no `Numeric`. Item 6.

## Top 10, by stability gained ÷ effort

| # | What | Pain point it hits | Effort |
|---|---|---|---|
| 1 | Scheduled online backups with stdlib `sqlite3.backup()` (Litestream optional, Apache-2.0) | the corpus has one stale backup | S |
| 2 | `concurrent-log-handler` (Apache-2.0) with gzip, rotations moved to a permanent `data/log_archive/` | the audit log deletes itself | S |
| 3 | A Playwright trace per booking, saved only on ERROR / re-login / unreconciled invoice (`tracing.start_chunk/stop_chunk`) | login and navigation races found only after the fact | S–M |
| 4 | `faulthandler_timeout` in pytest.ini, then pytest-timeout, pytest-randomly, pytest-xdist (all MIT) | the test file that ends the run early; slow suite; order-dependent tests | S |
| 5 | Hypothesis property tests (MPL-2.0) for the money rules — e.g. new > old ⇒ never OPTIMIZATION, OBC never cash, no earlier due date, no CAD + USD | false savings | M |
| 6 | A strict, Decimal-typed pydantic model between scraper and calculator; `Numeric` columns added beside the floats (never dropped) | float money, mixed currency, None in figures | M |
| 7 | `stamina` retries (MIT) only on GET navigations, an AST test that no click is retried; `asyncio.TaskGroup` + `asyncio.timeout` per booking | aborted navigations, dangling tasks, rule 4 | S–M |
| 8 | Snapshot tests over the captured corpus (inline-snapshot / syrupy, MIT) and HAR replay (`route_from_har`) | fixes that pass tests and do nothing live | M |
| 9 | Datasette (on the backup copy, read-only) with saved invariant queries; DuckDB over the JSON logs; lnav | invariants re-checked by hand | S |
| 10 | Portal drift detection from stored `aria_snapshot` templates per key page; skip the booking on mismatch | redesigns, generated ids | M |

Also: pin dependencies with `uv pip compile` (requirements.txt has only `>=`).

## Considered and rejected

- Stealth / anti-bot tools, CAPTCHA solvers, proxy rotation — hard rule 10.
- Dramatiq, ARQ, SAQ, procrastinate — need Redis/Postgres. huey — sync worker,
  no async Playwright. persist-queue / litequeue — the in-house resumable jobs
  already cover it.
- mutmut (needs fork / WSL), pytest-forked / pytest-isolate (fork; not
  Windows), pytest-rerunfailures (hides flakiness).
- Alembic batch mode — rebuilds tables by copying; too risky on the corpus.
- GlitchTip / self-hosted Sentry / OpenTelemetry — need servers; the existing
  crash logging + watchdog cover one machine.
- Existing cruise travel-agent automation on GitHub: none reusable ("cruisingpower"
  0 repos; one 0-star Playwright repo).

Sources: github.com/hynek/stamina · github.com/HypothesisWorks/hypothesis ·
github.com/simonw/datasette · github.com/15r10nk/inline-snapshot ·
github.com/pytest-dev/pytest-timeout · github.com/Preston-Landers/concurrent-log-handler ·
github.com/benbjohnson/litestream · github.com/tstack/lnav · github.com/duckdb/duckdb ·
github.com/astral-sh/uv · playwright.dev/python/docs/api/class-tracing ·
playwright.dev/python/docs/aria-snapshots · docs.pytest.org/en/stable/how-to/failures.html ·
docs.python.org/3/library/sqlite3.html
