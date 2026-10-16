# Developing market-data-hub

Companion to [`DESIGN.md`](DESIGN.md) (the spec). This file covers how to extend the code
base: sources, loaders/jobs, migrations, CLI commands, and the conventions every module
must follow.

## Setup and checks

```bash
uv sync --python 3.12                # Python 3.12 (.python-version); system python is newer
uv run ruff format && uv run ruff check
uv run mypy src                      # strict
uv run pytest -q                     # starts a throw-away PostgreSQL 16 via pgserver
uv run pytest -m live                # opt-in smoke tests against real endpoints (env creds)
```

Tests need no Docker and no network: `tests/conftest.py` starts PostgreSQL with `pgserver`
(data dir in a pytest tmp dir), applies all migrations to a template database once per
session and clones a fresh database for every test. Fixtures:

| fixture | what |
|---|---|
| `db_url` | conninfo of a fresh, fully migrated database (dropped after the test) |
| `empty_db_url` | fresh database **without** migrations |
| `conn` | `psycopg.Connection[DictRow]` to `db_url` (non-autocommit; you commit) |
| `db` | `mdhub.db.Database` for `db_url` |
| `settings` | `Settings(database_url=db_url)`, no credentials |

HTTP is mocked with `respx` (`@respx.mock`). Store sanitized real responses as fixtures under
`tests/fixtures/<source>/` and parse them in unit tests. Never commit credentials.

## Layout (who owns what)

```
src/mdhub/
  config.py        Settings (env, DESIGN §7)
  db/pool.py       Database: connection factory / pool (dict rows, session TZ = UTC)
  db/migrate.py    migration runner;  db/migrations/NNNN_name.sql
  core/http.py     HttpClient (TLS w/ Russian CA, retries, rate limit, entitlement check)
  core/capture.py  job_run(), record_capture(), load_capture_payload()
  core/upsert.py   upsert_rows(), touch_last_seen(), UpsertResult
  core/iss.py      MOEX ISS JSON helpers (blocks, paging, converters)
  core/timeutil.py MSK/UTC clocks, business-day arithmetic
  core/decimalutil.py, core/errors.py, core/redact.py, core/bizday.py
  jobs.py          @job registry, JobContext, run_job(), discover_jobs()
  schedule.py      RULES (DESIGN §6) + APScheduler worker
  cli.py           `mdhub` CLI;  cli_ext/  auto-registered sub-command groups
  sources/         pure fetch + parse per provider (no DB)
  loaders/         fetch -> capture -> parse -> upsert; @job functions (auto-discovered)
```

## Conventions (non-negotiable, DESIGN §2)

- **Raw first.** Record every HTTP response with `record_capture()` *before* parsing it, in
  the same transaction as the rows parsed from it. Normalized rows carry `capture_id` (and
  `run_id`). `endpoint` is a stable logical name (`bonds_history`, `cets_marketdata`), not
  the URL. Parser changes bump the module's `PARSER_VERSION`.
- **Fail closed.** Parsers validate blocks/columns/types (`parse_block(..., required=...)`,
  `require_columns`, converters) and raise `FormatDriftError`; never return partial data.
  The run is then `failed` and nothing from that capture is committed.
- **Missing ≠ zero ≠ stale. No forward fill.** If a value is absent, write no row (or NULL in
  that column). Record expected-but-missing data as `ops.data_gap` / a failed or `partial`
  run (`ctx.run.mark_partial("...")`), never as a value copied from an earlier date.
- **Decimals.** Prices, rates, amounts are `numeric` in SQL and `Decimal` in Python. Convert
  JSON numbers with `to_decimal()` (goes through `str()`), never `Decimal(float)` or `float`.
- **Units in column names**: `_pct` (percent, 14.5 = 14.5 %), `_bp` (basis points), `_rub`
  (roubles); FX is RUB per **1** unit of foreign currency (divide CBR values by `Nominal`).
- **Clocks.** All timestamps are aware (`timestamptz`). `fetched_at` is UTC (`utc_now()`),
  `source_ts` is the source's own clock (ISS times are MSK wall clock: `to_datetime()`,
  `msk_datetime()`), business dates (`trade_date`, `effective_date`) are `date` in
  Europe/Moscow (`msk_today()`, `ctx.business_dates()`). DB sessions run with
  `TimeZone = UTC`: in SQL never cast `timestamptz` to `date` without
  `AT TIME ZONE 'Europe/Moscow'`.
- **One authoritative source per series**; an alternative source is a separate series with a
  `source` column, never spliced in silently.
- **Secrets** only from env via `Settings` (`SecretStr`). Call `.get_secret_value()` at the
  point of use (header / auth object), never put secrets in URLs, log messages or exception
  texts. `redact_url()` / `sanitize_text()` are applied to stored URLs, `ops.job_run.error`
  and log output, but do not rely on them.
- **HTTP** only through `HttpClient` (`ctx.http(...)`): honest UA, rate limits, retries, no
  cross-host redirects, Russian Trusted Root CA + certifi. Use `get_json()` for JSON so an
  HTML "subscribers only" page raises `EntitlementError`.
- **Idempotency.** Write normalized tables only with `upsert_rows()` keyed by the natural key;
  identical reloads must be no-ops. Enable row history on every revisable table.

## Adding a source

1. Register the provider code in `ops.source` in a migration of your range, e.g.
   `INSERT INTO ops.source (code, name, base_url, licence_note) VALUES (...) ON CONFLICT DO NOTHING;`
   Seeded already: `moex_iss`, `moex_apim`, `moex_passport`, `moex_sdfi`, `ratescalcs`,
   `cbr`, `ecb`, `cbonds`, `otc_monitor`, `andromeda`.
2. Add `src/mdhub/sources/<name>.py`: functions that take an `HttpClient` (and explicit
   arguments) and return the raw `httpx.Response` plus parsed, typed records. No DB access,
   no `Settings()` reads inside parsers. Credentials come in as arguments (built from
   `ctx.settings` by the loader).
3. Add per-host rates to `default_rate_limits()` in `core/http.py` if the default (5 rps)
   is not appropriate.
4. Unit-test the parser against sanitized fixtures; test format drift raises.

## Adding a migration

- File `src/mdhub/db/migrations/NNNN_short_name.sql` (`^\d{4}_[a-z0-9_]+\.sql$`). Number
  ranges (DESIGN §5): **0001-0099** core/ops, **0100-0199** ref, **0200-0299** md,
  **0300-0399** legacy, **0400+** api/views. Pick the next free number in your range; two
  files with the same number are rejected.
- Each file runs in one transaction; applied files are checksummed — **never edit an applied
  migration**, add a new one. No psql meta-commands, no `CREATE INDEX CONCURRENTLY`.
- Schema-qualify every object. Standard columns for normalized tables:
  `run_id bigint`, `capture_id bigint REFERENCES ops.raw_capture(id)`,
  `ingested_at timestamptz NOT NULL DEFAULT now()`, optionally `updated_at timestamptz`,
  `last_seen_run bigint`, `last_seen` — these are the **meta columns** that never count as a
  data change (`core.upsert.META_COLUMNS`, `ops.track_changes()`).
- Enable history on revisable tables (keys = natural key):
  `CALL ops.enable_history('md.bond_eod', ARRAY['trade_date','secid','board']);`
  Extra table-specific meta columns: third argument, e.g.
  `CALL ops.enable_history('ref.calendar_day', ARRAY['calendar','day'], ARRAY['known_at']);`
- Apply with `uv run mdhub migrate` (advisory-locked; safe to run concurrently).

## Adding a loader / job

Loaders live in `src/mdhub/loaders/<family>.py` and are imported automatically
(`pkgutil`, see `mdhub.jobs.discover_jobs`; modules starting with `_` are skipped). Register
each job with `@job(name, description)`. Scheduled job names are fixed in
`mdhub.schedule.KNOWN_JOBS`; use exactly those.

```python
from datetime import date

from mdhub.core.capture import record_capture
from mdhub.core.iss import ISS_BASE_URL, IssPage, fetch_all, to_date, to_decimal
from mdhub.core.upsert import upsert_rows
from mdhub.jobs import JobContext, job

PARSER_VERSION = "1"
KEY = ["trade_date", "secid", "board"]


@job("bonds_eod", "MOEX bonds end-of-day history by trade date")
def bonds_eod(
    ctx: JobContext,
    *,
    mode: str = "final",
    days_back: list[int] | None = None,
    day: date | None = None,
) -> None:
    dates = [day] if day else ctx.business_dates(days_back or [1])
    with ctx.http(base_url=ISS_BASE_URL) as http:
        for d in dates:
            with ctx.connection() as conn:  # one transaction per date
                rows = []

                def on_page(page: IssPage) -> None:
                    cap = record_capture(
                        conn,
                        ctx.run.id,
                        "moex_iss",
                        "bonds_history",
                        None,
                        page.response,
                        PARSER_VERSION,
                    )
                    rows.extend(
                        {
                            "trade_date": to_date(r["TRADEDATE"]),
                            "secid": r["SECID"],
                            "board": r["BOARDID"],
                            "close": to_decimal(r["CLOSE"]),
                            "capture_id": cap,
                            "run_id": ctx.run.id,
                        }
                        for r in page.rows
                    )

                fetch_all(
                    http,
                    "/history/engines/stock/markets/bonds/securities.json",
                    {"date": d.isoformat()},
                    required=["TRADEDATE", "SECID", "BOARDID", "CLOSE"],
                    on_page=on_page,
                )
                ctx.run.add_counts(upsert_rows(conn, "md.bond_eod", rows, KEY, run_id=ctx.run.id))
```

`JobContext` gives `settings`, `db`/`connection()`, `http(**kwargs)`, `run`
(`id`, `add_counts`, `mark_partial`, `mark_skipped`), `logger`, `today()`,
`is_business_day()`, `business_dates(days_back)`. `run_job` already wraps the call in
`job_run` (one `ops.job_run` row) and a per-job advisory lock — do not open another
`job_run`. Raise `JobSkipped("reason")` to end as `skipped` (e.g. Cbonds 401, feature flag
off). Any other exception marks the run `failed` and is re-raised.

**Parameters.** Keyword-only, with defaults and type annotations; CLI strings are coerced
(`int`, `float`, `bool`, `Decimal`, `date`, `datetime`, `X | None`, `list[X]` as `a,b`).
Shared names:

| param | meaning |
|---|---|
| `mode: str` | run variant from the schedule: `full`/`probe`, `prelim`/`final`, `intraday`/`eod`/`final`, `post_meeting`, `backup`/`retention` |
| `days_back: list[int]` | business-day offsets, 0 = run date; resolve with `ctx.business_dates()` |
| `group: str` | instrument subset (indices: `rusfar`, `rusfarrt`, `all`) |
| `keep: int` | backups to keep (maintenance) |
| `day: date` | explicit single business date (manual runs, backfill) |
| `date_from` / `date_to: date` | explicit range (manual runs, `ops.backfill_task.params`) |

A job must accept every parameter its rules in `mdhub.schedule.RULES` pass;
`tests/test_schedule.py::test_registered_jobs_accept_rule_params` enforces it. Run a job
manually: `uv run mdhub run bonds_eod -p day=2026-10-15 -p mode=final`.

## Schedule

`mdhub/schedule.py` holds `RULES` (job → cron rules with params, day filter `business` /
`all` / `first_business_of_month`, calendar, `gate_offset_days`) per DESIGN §6. Cron is
5-field crontab in Europe/Moscow. Business-day gating uses `ref.calendar_day` when present
(latest `known_at` per day), else Monday–Friday. `uv run mdhub scheduler` runs it; rules of
jobs that are not registered are skipped with a warning. `uv run mdhub jobs list` shows the
registry, cron rules, import failures and schedule problems.

## Adding CLI commands

Create `src/mdhub/cli_ext/<module>.py` exposing `app = typer.Typer(...)` and optionally
`name = "group-name"` (default: module name with `_` → `-`). It is registered automatically
as `mdhub <group-name> ...`:

```python
import typer

name = "legacy"
app = typer.Typer(help="Legacy SQLite import.", no_args_is_help=True)


@app.command("import")
def import_cmd(path: str) -> None: ...
```

Build `Settings()` / `Database.from_settings()` inside the command, never at import time. A
module that fails to import is reported on stderr and skipped. Core commands: `migrate`,
`jobs list`, `run`, `scheduler`, `db-info`.

## Environment

DESIGN §7 plus two operational knobs: `MDHUB_LOG_LEVEL` (default `INFO`) and
`MDHUB_HTTP_TIMEOUT` (seconds, default 30). See `mdhub/config.py`.
