# market-data-hub — design

Status: v0.1 design, 2026-10-15. Owner: FiveLimaGolf. Target: a single VPS that is the
market data hub for the trading desk backend (trading_desk_monitor + rub-fixed-income-core).

Inputs to this design (audits, 2026-10-15): market-data-store (curves/FX/fixings loaders),
moex-market-data-mcp (bond catalog/static), 16 other FiveLimaGolf repos, backend
requirements (TDM contract 1.3, core library models), live connectivity tests, integrity
check of the two SQLite snapshots in `data/incoming/`.

## 1. Goals and non-goals

Goals
1. **Library of loaders** (`mdhub.sources` + `mdhub.loaders`) for market data and bond static.
2. **Rules** for update cadence, revisions, dedup, gaps and backfill (`mdhub.schedule`, §6).
3. **PostgreSQL 16** store with full history, imported losslessly from the two SQLite snapshots.
4. **Read API** (FastAPI) + CLI; deployable archive for a fresh VPS (docker compose, plus
   bare-metal systemd variant).

Non-goals (stay in analytics projects): curve bootstrapping, key-rate projection, Z-spreads,
pricing. The hub stores *inputs* (quotes, fixings, published parameters) and official
published values only. Legacy derived tables are kept verbatim in `legacy_*` schemas.

## 2. Principles (non-negotiable)

- **Raw first.** Every fetch is stored in `ops.raw_capture` (gzip payload, SHA-256, URL
  without secrets, HTTP status, content-type, parser version, fetched_at, source clock).
  Normalized rows reference `capture_id`. Re-parsing from raw must be possible.
- **Idempotent, append-only history.** Normalized tables are upserted by natural key; any
  change of a non-meta column on an existing key writes the previous row image to
  `ops.row_history` (generic trigger) → bitemporal "as known at" reconstruction.
  Identical re-loads are no-ops (no history row, no churn).
- **Missing ≠ zero ≠ stale.** Never forward-fill. A failed/empty fetch is recorded as a
  failed run / `ops.data_gap`, never as a value.
- **Fail closed on format drift.** Parsers validate columns/types and raise; the run is
  marked `failed` with the error; nothing partial is committed for that capture.
- **Separate clocks.** `source_ts` (source's own time, e.g. UPDATETIME/SYSTIME), `fetched_at`
  (UTC), `trade_date`/`effective_date` (business date), `published_at` where the source has it.
  All timestamps are `timestamptz`; business dates are `date` in Europe/Moscow.
- **One authoritative source per series**; alternatives stored as separate series with a
  `source` column, never spliced silently.
- **Decimals.** Prices/rates as `numeric` (no float rounding). Units in column names
  (`_pct`, `_bp`, `_rub`), FX as RUB per 1 unit (normalize CBR `Nominal`).
- **Secrets only from env** (names in §7); never logged, never in URLs, never in raw_capture.url.
  Honest `User-Agent: market-data-hub/<version>`. Optional egress proxy via `MDHUB_HTTP_PROXY`.
  MOEX hosts use the bundled Russian Trusted Root CA + certifi.
- **Entitlement-aware.** A 200 response with `text/html` "subscribers only" page from
  MOEX = `EntitlementError`, not data.

## 3. Sources and what they give us (verified 2026-10-15)

| Source | Auth (env) | Use in hub | Freshness |
|---|---|---|---|
| MOEX ISS public `iss.moex.com/iss` | — | bond static + bondization, EOD history (bonds, CETS FX, FORTS, indices), ZCYC, intraday snapshots, indicative FX 14:00/19:00, CETS fixings, RUSFAR | marketdata 15-min delayed; indices/ZCYC current |
| MOEX APIM `apim.moex.com/iss` | `MOEX_API_KEY` (Bearer) | realtime futures (FX futures bid/offer/last/OI), AlgoPack tradestats/obstats/futoi | futures realtime; bonds/FX delayed |
| MOEX Passport | `MOEX_PASSPORT_LOGIN/PASSWORD` | optional; login works, account currently **not** realtime-entitled | delayed |
| MOEX SDFI swap curves `/iss/sdfi/curves` | subscription | catalogue only; loader ready behind feature flag `MDHUB_ENABLE_SDFI` | — |
| Ratescalcs `ratescalcs.ru` | `RATESCALC_USERNAME/PASSWORD` (Basic, GET only) | RUONIA OIS, KRS (IRS vs key rate), CNY xccy vs RUONIA, CNY futures-implied yields snapshot | dealer runs, 08:50–23:00 MSK |
| CBR `cbr.ru` | — | key rate, RUONIA, term RUONIA, official FX (all ccy), liquidity, meeting calendar | daily |
| ECB eurofxref | — | G7 crosses (EUR base) daily + history since 1999 | daily ~16:00 CET |
| Cbonds JSON API | `CBONDS_LOGIN/PASSWORD` | static enrichment (day count, floater conventions, ratings, offers). **Currently HTTP 401** (likely IP allow-list) → optional, auto-disabled on 401 | — |
| OTC Monitor | `OTC_MONITOR_TOKEN` (not provided) | legacy history imported; live loader not ported (prod parser v3–5 not in git) | — |
| Andromeda | — | legacy history imported only (Playwright scraping is dropped) | — |

## 4. Data model (PostgreSQL schemas)

### `ops` — provenance and operations
- `ops.source(code PK, name, base_url, licence_note)`
- `ops.job_run(id, job, started_at, finished_at, status ∈ {running, succeeded, partial, failed, skipped}, params jsonb, rows_inserted, rows_updated, rows_unchanged, error text, host)`
- `ops.raw_capture(id, run_id, source, endpoint, url_redacted, http_status, content_type, fetched_at, source_ts, parser_version, sha256, payload_gz bytea, size_bytes)` — unique (sha256, endpoint, fetched_at). Retention: intraday raw 90 days (configurable), EOD/static raw forever.
- `ops.row_history(id, table_name, row_key jsonb, old_row jsonb, replaced_at, replaced_by_run)` — written by trigger `ops.track_changes()`.
- `ops.data_gap(dataset, key jsonb, gap_date, detected_at, status ∈ {open, backfilled, unfillable}, note)`
- `ops.backfill_task(id, dataset, params jsonb, status, attempts, last_error, created_at, done_at)`
- `ops.schema_migration(version, applied_at, checksum)`

### `ref` — static / reference
- `ref.calendar_day(calendar ∈ {MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION}, day, is_business_day, source, known_at)` — past days from observed trading (+ ISS where available), future from curated data file with version.
- `ref.issuer(issuer_id PK (MOEX emitent_id), inn, ogrn, okpo, name, short_name, country, sector, is_sovereign, …, updated_at)`
- `ref.bond(secid PK, isin, regnumber, issuer_id, name, short_name, bond_type (MOEX label), bond_class (derived enum: ofz_pd, ofz_pk, ofz_in, ofz_ad, sovereign_fx, subfederal, municipal, corporate, exchange_bond, structured, mbs, …), coupon_type ∈ {fixed, floating, indexed_face, zero, variable_known, unknown}, face_value, initial_face_value, face_unit, currency_id (settlement), issue_date, maturity_date (NULL for perpetual), is_perpetual, coupon_frequency, day_count, is_amortizing, is_subordinated, is_structured, qualified_only, high_risk, listing_level, issue_size, primary_board, start_date_moex, status ∈ {active, matured, delisted}, …, source, updated_at, last_seen_run)`
- `ref.bond_board(secid, board, is_primary, lot_size, min_step, decimals, settle_code, currency_id, listed, first_seen, last_seen)`
- `ref.bond_coupon(secid, coupon_no, start_date, pay_date (coupondate), record_date, value_rub/amount, value_prc, face_value, status ∈ {known, projected_by_moex, unknown}, source, updated_at)` PK (secid, coupon_no) — falls back to ordinal by date only when MOEX gives no number (documented).
- `ref.bond_amortization(secid, amort_date, value_prc, value (amount), face_value, data_source)` PK (secid, amort_date)
- `ref.bond_offer(secid, offer_date, offer_type (put/call/buyback/…), price, value, offer_date_start, offer_date_end, agent, status)` — `0000-00-00` → NULL + flagged.
- `ref.bond_rate_formula(secid, benchmark ∈ {KEY_RATE, RUONIA, RUONIA_3M, ZCYC, CPI, …}, spread_bp, multiplier, lag_days, averaging, floor_pct, cap_pct, status ∈ {curated, inferred, partial}, evidence)`
- `ref.bond_rating(secid|issuer_id, agency, rating, outlook, rating_date, source)` (Cbonds when available)
- `ref.futures_contract(secid PK, asset_code, short_name, last_trade_date, last_del_date, step_price, min_step, lot_volume, contract_type ∈ {dated, perpetual}, first_seen, last_seen)`
- `ref.fx_instrument(secid PK, board, base_ccy, quote_ccy, tenor (TOD/TOM/SPT/swap), lot_size, …)`
- `ref.cbr_meeting(meeting_date PK, effective_date, is_estimated, decision, source)`
- `ref.static_snapshot(secid, source, payload_sha256, fetched_at, capture_id)` — per-source observation log for static.

### `md` — market data
- `md.bond_eod(trade_date, secid, board, open, low, high, close, legal_close, waprice, market_price2, market_price3, admitted_quote, prev_price, bid?, offer?, yield_close, yield_at_waprice, yield_to_offer, duration_days, accrued_int, face_value, face_unit, currency_id, volume, value_rub, num_trades, capture_id, run_id, ingested_at)` PK (trade_date, secid, board). Source: ISS `/history/engines/stock/markets/bonds/securities.json?date=` (+ `/history/.../yields`).
- `md.bond_quote_intraday(ts (source UPDATETIME), secid, board, bid, offer, last, waprice, yield, accrued, num_trades, value, latency ∈ {realtime, delayed}, capture_id)` PK (secid, board, ts) — only changed rows stored.
- `md.fx_eod(trade_date, secid, board, open, low, high, close, waprice, volume, value, num_trades, capture_id)` — CETS history (all `*_TOD/_TOM` and crosses).
- `md.fx_intraday(ts, secid, board, bid, offer, last, waprice, latency, capture_id)` PK (secid, board, ts)
- `md.futures_eod(trade_date, secid, open, low, high, close, settle_price, waprice, volume, value, open_position, capture_id)`
- `md.futures_intraday(ts, secid, bid, offer, last, open_position, latency, source_variant, capture_id)` PK (secid, ts, source_variant)
- `md.index_eod(trade_date, secid, open, high, low, close, value, duration, yield, capture_id)` (RUSFAR family, RGBI, RUCBI, MOEXREPO…)
- `md.fixing(index_code, fixing_date, value, effective_date, published_at, source, capture_id)` PK (index_code, fixing_date, source) — KEY_RATE (by effective date), RUONIA, RUONIA_INDEX, TERM_RUONIA_1M/3M/6M, RUSFAR*, SRATE_CNY_ON, CBR repo rates…
- `md.fx_official(effective_date, ccy, nominal, value, rate_per_unit, set_date, source='cbr', capture_id)` PK (effective_date, ccy)
- `md.fx_indicative(trade_date, clearing ∈ {tc, mc}, ccy_pair, rate, source='moex', capture_id)` — MOEX indicative 14:00/19:00 (FX mark candidate).
- `md.ecb_fx(rate_date, ccy, rate_per_eur, capture_id)`
- `md.curve_snapshot(id, source, dataset ∈ {ruonia_ois, key_rate_swap, cny_ruonia_xccy, cny_futures_yields, cfets_fr007_irs, …}, quote_basis, fetched_at, source_ts, content_sha256, parser_version, capture_id, legacy_id)` + `md.curve_quote(snapshot_id, tenor, tenor_months, bid_pct, ask_pct, mid_pct)` — unchanged consecutive snapshots are not duplicated (content hash per source+dataset compared to previous).
- `md.zcyc_snapshot(trade_date, trade_time, source_ts, params jsonb (B1..B3,T1,G1..G9), standard_yields jsonb, is_eod, capture_id)`
- `md.cbr_liquidity(observed_on, …14 metrics…, methodology_version, capture_id)` (+ revisions via row_history)

### `legacy_md`, `legacy_bc`
Verbatim copies (all tables, all rows) of `market-data.sqlite3` and `bond_catalog.sqlite3`
(types mapped TEXT→text, INTEGER→bigint, REAL→double precision, BLOB→bytea). Row counts
verified against `SNAPSHOT.json`. Then mapped into `md`/`ref` by `mdhub.legacy`.

## 5. Package layout

```
src/mdhub/
  config.py            Settings (pydantic-settings), env names in §7
  db/                  pool.py, migrate.py, migrations/NNNN_name.sql
  core/                http.py (HttpClient: retries, rate limit, CA, proxy, entitlement check),
                       capture.py (record raw capture, job_run context manager), upsert.py,
                       timeutil.py, decimalutil.py, bizday.py, redact.py, errors.py,
                       iss.py (ISS block parsing + paging helpers)
  sources/             moex_iss.py, moex_apim.py, moex_passport.py, cbr.py, ecb.py,
                       ratescalcs.py, cbonds.py, sdfi.py — pure fetch + parse, no DB
  loaders/             bonds_static.py, bonds_eod.py, bonds_intraday.py, fx_moex.py,
                       futures.py, indices.py, zcyc.py, cbr.py, ecb.py, swaps.py,
                       cbonds_enrich.py, calendar.py — fetch→capture→parse→upsert
  legacy/              import_sqlite.py (verbatim), map_md.py, map_bc.py
  quality/             checks.py (integrity, freshness, gaps), report.py
  jobs.py              job registry (@job), JobContext, run_job, loader discovery
  schedule.py          cron rules (APScheduler)
  api/                 app.py (FastAPI read API)
  cli.py               `mdhub` (typer); cli_ext/ auto-registered sub-command groups
```

Migration ownership (to avoid conflicts): `0001-0099` core/ops; `0100-0199` ref (static);
`0200-0299` md (market); `0300-0399` legacy; `0400+` api/views.

## 6. Update and storage rules (schedule, Europe/Moscow, business days unless noted)

| Job | When | Source | Notes |
|---|---|---|---|
| `bonds_static` | daily 06:30 full; 09:05–19:05 hourly new-listing probe | ISS securities + bondization | full universe (all boards incl. structured); revisions via row_history |
| `cbonds_enrich` | daily 07:30 | Cbonds | auto-skip with status `skipped` while auth returns 401 |
| `bonds_eod` | 19:20 (prelim) and 07:15 next day for D-1 (final) + D-2 | ISS history by date | final run captures post-session revisions |
| `bonds_intraday` | every 15 min 10:00–19:00 | ISS marketdata (delayed) | store changed rows only |
| `fx_moex_intraday` | every 5 min 07:00–23:55 | ISS CETS marketdata | |
| `fx_moex_eod` | 00:20 (D) and 07:20 (D-1 recheck) | ISS CETS history | |
| `fx_indicative` | 14:10, 19:10 | ISS indicativerates | |
| `futures_intraday` | every 1 min 08:50–23:50 | APIM realtime marketdata (FX futures + perpetuals) | fallback ISS delayed, labelled |
| `futures_eod` | 19:25 and 07:25 next day | ISS FORTS history (settle, OI) | |
| `algopack_fx_futures` | every 5 min (+1 min offset) | APIM tradestats/obstats/futoi | dedup on (secid, interval) |
| `indices_eod` | 12:40 (RUSFAR), 18:10 (RUSFARRT), 19:30 all | ISS index history | |
| `zcyc` | every 15 min 10:00–19:00; EOD 19:05; final 07:10 next day | ISS zcyc | |
| `cbr_daily` | 09:00, 14:30, 16:30, 18:30 (all days) | CBR | key rate, RUONIA (+index, term), official FX (all ccy), liquidity, repo |
| `cbr_meetings` | 1st business day monthly + after each meeting | CBR | |
| `ecb_fx` | 18:30 (all days) | ECB | |
| `swaps_ratescalcs` | every 30 min 09:00–23:00 | Ratescalcs | skip identical content |
| `quality` | 08:30 and 20:30 | DB | freshness & gap detection → `ops.data_gap` + `ops.backfill_task` |
| `backfill_worker` | every 10 min | DB | processes backfill tasks with rate limit |
| `maintenance` | 03:00 daily | DB | raw retention, VACUUM ANALYZE; `pg_dump` 02:00 (keep 14) |

## 7. Configuration (env)

`MDHUB_DATABASE_URL` (required), `MDHUB_TZ=Europe/Moscow`, `MDHUB_HTTP_PROXY` (optional),
`MDHUB_ISS_RPS=3`, `MDHUB_RAW_RETENTION_DAYS_INTRADAY=90`, `MDHUB_API_TOKENS` (comma list,
read API bearer tokens), `MDHUB_ENABLE_SDFI=false`, plus source credentials:
`MOEX_API_KEY`, `MOEX_PASSPORT_LOGIN`, `MOEX_PASSPORT_PASSWORD`, `RATESCALC_USERNAME`,
`RATESCALC_PASSWORD`, `CBONDS_LOGIN`, `CBONDS_PASSWORD`, `OTC_MONITOR_TOKEN` (future).

## 8. Read API (v1, bearer token)

`/health/live`, `/health/ready`, `/v1/coverage`, `/v1/runs`, `/v1/bonds` (static, filters),
`/v1/bonds/{secid}` (+ coupons, amortizations, offers, formula), `/v1/bonds/eod?date=`,
`/v1/bonds/{secid}/history`, `/v1/fx/official`, `/v1/fx/eod`, `/v1/fx/indicative`,
`/v1/futures/eod`, `/v1/fixings`, `/v1/curves/{dataset}?as_of=`, `/v1/zcyc?date=`,
`/v1/calendar`, `/v1/export/tdm?date=` (CSV bundle in TDM 1.3 format). Pagination, `as_of`
(known-at) where history exists. Bundles/manifests are a later iteration.

## 9. Deployment

`deploy/docker-compose.yml`: `postgres:16` (volume, tuned), `mdhub-migrate` (one-shot),
`mdhub-worker` (scheduler), `mdhub-api` (uvicorn behind optional Caddy TLS). `.env` from
`deploy/env.example`. Initial seed: `deploy/seed/` contains `pg_dump -Fc` of the fully
loaded DB → `mdhub restore`. Bare-metal alternative: `deploy/systemd/*.service|*.timer` +
`deploy/install.sh` (Ubuntu 24.04: postgresql-16, uv).

## 10. Testing

Unit tests with recorded fixtures (sanitized real responses) per parser; integration tests
against a real PostgreSQL 16 (via `pgserver` locally / service in CI); idempotency tests
(load twice → no new rows, no history); revision tests (changed value → row_history).
Live smoke tests (`pytest -m live`) hit real endpoints with env credentials.
