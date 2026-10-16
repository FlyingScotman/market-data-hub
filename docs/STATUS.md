# STATUS — paused 2026-10-15 (evening)

## Done
- Audits (all in `docs/audit/`): 01 market-data-store, 02 bond static (moex-market-data-mcp),
  03 backend requirements, 04 DB integrity (report written; agent stopped right after),
  05 connectivity, 06 other FiveLimaGolf repos.
- Repo created: https://github.com/FiveLimaGolf/market-data-hub (private, empty remote).
  Local git initialised here, per-repo identity FiveLimaGolf. Nothing committed yet.
- Decisions by owner: GitHub account FiveLimaGolf; DB = PostgreSQL 16.
- `docs/DESIGN.md` — architecture, schema, schedule rules (v0.1).
- Snapshots extracted to `data/incoming/{md,bc}/` (SHA-256 verified vs MANIFEST).
- Local Postgres for tests: `uv run --python 3.12 --with pgserver` works (PG 16.2), no Docker on this Mac.

## Interrupted
- Foundation agent (pyproject, core infra, migrations, CLI, tests) was stopped mid-way:
  only `pyproject.toml`, `uv.lock`, `.python-version`, `README.md`, `src/mdhub/__init__.py`,
  `src/mdhub/certs/` exist. Re-run it from the start (spec: DESIGN.md + prompt in session).

## Next
1. Foundation (core/db/http/capture/upsert/iss/jobs/cli/tests).
2. In parallel: bonds static loader; MOEX market loaders (bonds EOD/intraday, CETS FX, FORTS,
   indices, ZCYC); official/other (CBR, ECB, Ratescalcs, Cbonds-optional, calendar);
   legacy SQLite → Postgres import; API + scheduler + deploy.
3. Backfill (bonds EOD from 2024-01-01, FX/futures/indices/ZCYC history), integrity re-check,
   pack VPS archive (code + pg_dump seed).

## Open items for owner
- Cbonds API → HTTP 401 for any login (likely IP allow-list of old server).
- MOEX Passport login has no realtime entitlement; APIM realtime only for shares/futures.
- MOEX SDFI swap curves (CNY OIS vs RUSFAR CNY, xccy) need a subscription.
- OTC Monitor token not provided; prod parser v3–5 and ~/.hermes wrappers exist only on old server.
- Ratescalcs password appears weak (occurs in plain page text) — consider rotation.

`.handoff/` (gitignored) holds connectivity probe scripts and DB-check intermediates.
