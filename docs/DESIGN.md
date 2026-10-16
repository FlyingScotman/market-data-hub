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
| OTC Monitor `otc-monitor.ru/api/bond/` | `OTC_MONITOR_TOKEN` (Bearer; 401 without) | RUONIA OIS (17 tenors 1W–10Y), KRS (13, 3M–10Y), CNY xccy vs RUONIA (14, 1W–5Y), CNY OIS vs RUSFAR CNY (8, 1W–1Y; new, live only) — one JSON list of ~440 rows (bonds, FX, spreads, curves); legacy history 2026-09-04 → 10-15 imported. **No history endpoint** (`?date` ignored, `/api/history` 404) | current state only, polled every 15 min; source clock `date_update` (MSK); two-sided during the day, mid-only off-hours and on some CNY tenors |
| Andromeda | — | legacy history imported only (Playwright scraping is dropped) | — |

## 4. Data model (PostgreSQL schemas)

### `ops` — provenance and operations
- `ops.source(code PK, name, base_url, licence_note)`
- `ops.job_run(id, job, started_at, finished_at, status ∈ {running, succeeded, partial, failed, skipped}, params jsonb, rows_inserted, rows_updated, rows_unchanged, error text, host, trigger ∈ {schedule, catch_up, backfill, cli, api, other})` — `trigger` (0421; NULL for older rows) records who started the run; job health counts only `schedule`/`catch_up` (and NULL) runs as "the scheduled fire ran".
- `ops.raw_capture(id, run_id, source, endpoint, url_redacted, http_status, content_type, fetched_at, source_ts, parser_version, sha256, payload_gz bytea, size_bytes)` — unique (sha256, endpoint, fetched_at). Retention: intraday raw 90 days (configurable), EOD/static raw forever.
- `ops.row_history(id, table_name, row_key jsonb, old_row jsonb, replaced_at, replaced_by_run)` — written by trigger `ops.track_changes()`.
- `ops.data_gap(dataset, key jsonb, gap_date, detected_at, status ∈ {open, backfilled, unfillable, not_expected}, note)` — `unfillable` is terminal (the source confirmed "no data" and at least one **business day** of the dataset's calendar has passed since the day, `mdhub.core.gaps`); data that appears later still turns it `backfilled`. A gap the scan no longer reports becomes `backfilled` only when the table has ≥ `min_rows_per_day` rows for it, otherwise `not_expected` (calendar corrected, e.g. a holiday; re-opened if expected and missing again). `ops.v_coverage` counts `open_gaps`, `unfillable_gaps`, `not_expected_gaps`; `ops.v_open_gaps` lists `open` only.
- `ops.backfill_task(id, dataset, params jsonb {date_from, date_to}, status, attempts, last_error, created_at, done_at, not_before, started_at, last_run_id, source)` — one task per (backfill job, range): datasets sharing a job share tasks.
- `ops.dataset_status` (latest check per dataset), `ops.quality_report` (one report per `quality` run, incl. job health and catch-up); views `ops.v_coverage`, `ops.v_job_health`, `ops.v_open_gaps`, `ops.v_backfill_queue`.
- `ops.legacy_import` (SQLite import bookkeeping), `ops.schema_migration(version, applied_at, checksum)`

### `ref` — static / reference
- `ref.calendar_day(calendar ∈ {MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION, TARGET}, day, source, is_business_day, known_at, version, note)` PK (calendar, day, source) — past days from observed trading / RUONIA observations (from 2019-01-01), the days the official MOEX ISS calendar lists (never a synthesized year), future days from the curated data file; TARGET: a computed rule for all days (`rule_target`). **Precedence by source, not recency**: `ref.calendar_source_rank()` observed > moex_iss_calendar > curated > rule_* > curated_unverified (`known_at` breaks ties; `rule_*` 0422); view `ref.calendar_effective`, `mdhub.core.bizday.DbCalendar`; days without rows: Monday–Friday.

  | calendar | past days (observed) | future days | used by |
  |---|---|---|---|
  | `MOEX_STOCK` | `observed_moex_stock`: IMOEX or RGBI history row | `moex_iss_calendar` (listed days only), `curated` / `curated_unverified` | default; MOEX stock/bond datasets |
  | `MOEX_FX` | `observed_moex_fx`: CETS CNYRUB_TOM history row | same | MOEX FX datasets |
  | `MOEX_FORTS` | `observed_moex_forts`: Si futures volume > 0 | same | FORTS datasets |
  | `RU_PRODUCTION` | `observed_cbr_ruonia`: a RUONIA fixing (`md.fixing`) | `curated` (Government decrees) | CBR datasets, meeting effective dates |
  | `TARGET` (0422) | — (rule only) | `rule_target` for past and future days: weekends + computed TARGET closing days (Jan 1, Good Friday, Easter Monday — Gregorian computus —, May 1, Dec 25, Dec 26), from 2002-01-01 (or `--since` if later) through the end of next year | `ecb_fx` |

  Observed rows cover every day from the start to the last observed day (no trading / fixing → `false`); curated and ISS rows are written only for later days. TARGET is rule-only: the rule is exact (ECB rate dates 2015-01-02..2026-10-15 match it day for day: the 57 weekdays without rates are exactly the closing days), and deriving it from `md.ecb_fx` would turn a failed ECB load into a holiday. ECB data only **cross-checks** the rule on every calendar refresh: a TARGET business day without ECB rates (inside the ECB data range, past the 1-business-day no-data grace) or ECB rates on a closing day → warning and calendar run `partial`; the calendar is not changed. `ecb_fx` used Monday–Friday before 0422, so every TARGET holiday ended `unfillable`; the quality scan closes those gaps as `not_expected` once the TARGET calendar is seeded.

  **Known limitation (observed calendars):** `RU_PRODUCTION` past days come from `observed_cbr_ruonia` (and the MOEX calendars from trading history): if a RUONIA load failed and the day is still empty when the calendar refresh runs, that day becomes a non-business day (a "holiday"), and a gap of a dataset on that calendar is then closed as `not_expected` instead of staying open. Not changed: there is no exact rule for the Russian production calendar (Government transfer decrees).
- `ref.weekend_session(calendar, day, source, trade_session_date, known_at, note, capture_id, run_id)` PK (calendar, day, source) (0421) — days on which a MOEX engine trades although they are not settlement business days (ISS `engines/{engine}/calendar.json` rows `is_work_day=1, reason='W'`; trades belong to `trade_session_date`). Stored for every listed day of the year (past ones too: the observed calendars cannot see them); future rows ISS no longer lists are deleted. **Trading day of an engine** = business day (`ref.calendar_effective`) or a day listed here (`CalendarPredicate.is_trading_day`); `ref.calendar_day` keeps these days non-business. Trading days date quotes ("previous session") and gate the FORTS polls; business days drive EOD trade dates, `days_back` and gap expectations. Calendar refresh: an ISS engine answer with < 5 rows of its engine or none in the current year is treated as incomplete — listed days are written, nothing is deleted (warning, run `partial`).
- `ref.issuer(issuer_id PK (MOEX emitent_id), inn, ogrn, okpo, name, short_name, country, sector, is_sovereign, …, updated_at)`
- `ref.bond(secid PK, isin, regnumber, issuer_id, name, short_name, bond_type (MOEX label), bond_class (derived enum: ofz_pd, ofz_pk, ofz_in, ofz_ad, sovereign_fx, subfederal, municipal, corporate, exchange_bond, structured, mbs, …), coupon_type ∈ {fixed, floating, indexed_face, zero, variable_known, unknown}, face_value, initial_face_value, face_unit, currency_id (settlement), issue_date, maturity_date (NULL for perpetual), is_perpetual, coupon_frequency, day_count, is_amortizing, is_subordinated, is_structured, qualified_only, high_risk, listing_level, issue_size, primary_board, start_date_moex, status ∈ {active, matured, delisted}, …, source, updated_at, last_seen_run)` — `coupon_type_basis` ∈ {moex_label, benchmark, ofz_series, schedule, schedule_constant, none}: `schedule_constant` (0421) = an active unlabelled bond classified `fixed` because it has no benchmark / COUPON_DETAILS and no rate formula from any source, ≥ 2 paid coupons, one constant `value_prc` over all paid and future coupons, and a CBR key-rate change (`md.fixing` KEY_RATE) after the start of its first and on/before the start of its last paid coupon period (a key-rate floater would show different paid rates); never for structured notes. Seed 2026-10-16: 335 of the 433 active `unknown/schedule` bonds qualify (mostly subfederal/municipal/corporate amortizers).
- `ref.bond_board(secid, board, is_primary, lot_size, min_step, decimals, settle_code, currency_id, listed, first_seen, last_seen)`
- `ref.bond_coupon(secid, coupon_no, start_date, pay_date (coupondate), record_date, amount, value_rub, value_prc, face_value, face_unit, status ∈ {known, current_published, projected_by_moex, unknown}, is_deleted, source, updated_at)` PK (secid, coupon_no) — `coupon_no` = position in the complete MOEX schedule. Status semantics for consumers: `known` = fixed published amount or paid coupon; `current_published` = the current floater period as MOEX shows it, not proven fixed in advance (may be a projection; `known` only with a curated formula stating a fixing before the period); `projected_by_moex` = MOEX forward-fill / in-arrears / indexed face / unknown coupon type; `unknown` = no value. An active bond without MOEX fixed/floating labels is never `fixed` just because its schedule is fully valued.
- `ref.bond_amortization(secid, amort_date, value_prc, value (amount), face_value, data_source)` PK (secid, amort_date)
- `ref.bond_offer(secid, offer_date, offer_type (put/call/buyback/…), price, value, offer_date_start, offer_date_end, agent, status)` — `0000-00-00` → NULL + flagged.
- `ref.bond_rate_formula(secid, source ∈ {moex_iss, cbonds, curated, legacy_bc}, benchmark ∈ {KEY_RATE, RUONIA, RUONIA_3M, ZCYC, CPI, …}, benchmark_raw, spread_bp, multiplier, lag_days, averaging, floor_pct, cap_pct, variable_rate_type, fixing_day, status ∈ {curated, inferred, partial}, evidence, is_deleted)` PK (secid, source); view `ref.bond_rate_formula_resolved` (conventions curated > cbonds > moex_iss > legacy_bc; benchmark curated > moex_iss > cbonds > legacy_bc).
- `ref.bond_rating(secid|issuer_id, agency, rating, outlook, rating_date, source)` (Cbonds when available)
- `ref.futures_contract(secid PK, asset_code, short_name, is_fx, base_ccy, quote_ccy, last_trade_date, last_del_date, step_price, min_step, lot_volume, contract_type ∈ {dated, perpetual}, spec_source, first_seen, last_seen)`
- `ref.fx_instrument(secid, board, kind ∈ {outright, swap, fixing, wap, other}, base_ccy, quote_ccy, tenor, near_leg, far_leg, lot_size, …, settle_date, first_seen, last_seen)` PK (secid, board) — `settle_date` is an observation column (history meta; refreshed without counting a change).
- `ref.cbr_meeting(meeting_date PK, effective_date, is_estimated, is_extraordinary, decision, decision_rate_pct, prev_rate_pct, title, source)` — `effective_date` = next RU business weekday (calendar), never a weekend; confirmed from the KEY_RATE series.
- `ref.static_snapshot(secid, source, endpoint, payload_sha256, capture_id, first_seen_at, last_seen)` — per-source observation log for static; `ref.static_conflict(secid, field, source_a, source_b, value_a, value_b, selected_source, resolved)` — cross-source disagreements.

### `md` — market data
- `md.bond_eod(trade_date, secid, board, open, low, high, close, legal_close, waprice, market_price2, market_price3, admitted_quote, prev_price, bid?, offer?, yield_close_pct, yield_at_waprice, yield_to_offer, duration_days, accrued_int, face_value, face_unit, currency_id, volume, value, num_trades, capture_id, run_id, ingested_at)` PK (trade_date, secid, board). Source: ISS `/history/engines/stock/markets/bonds/securities.json?date=`. `yield_close_pct` (YIELDCLOSE) is the yield of the last trade to maturity even with an offer ahead.
- `md.bond_eod_yield(trade_date, secid, board, yield_date, yield_date_type (MATDATE/OFFER/…), price, effective_yield_pct, waprice, effective_yield_waprice_pct, duration_days, zspread_bp, gspread_bp, yield_to_offer_pct, …)` PK (trade_date, secid, board) — ISS `/history/.../yields` (EFFECTIVEYIELD to MOEX's yield date: nearest offer while ahead, else maturity).
- `md.bond_quote_intraday(ts (source UPDATETIME), secid, board, bid, offer, last, waprice, yield, accrued, num_trades, value, latency ∈ {realtime, delayed}, capture_id ON DELETE SET NULL)` PK (secid, board, ts) — only changed rows stored; `md.bond_quote_last` = last state per (secid, board) for change detection. Raw endpoint `intraday:bonds_marketdata` (retention).
- `md.fx_eod(trade_date, secid, board, open, low, high, close, waprice, volume, value, num_trades, capture_id)` — CETS history (all `*_TOD/_TOM` and crosses).
- `md.fx_intraday(ts, secid, board, source, source_variant, bid, offer, last, waprice, …, latency, capture_id)` PK (secid, board, source, source_variant, ts) — change-only, row history on (same-ts updates keep the old image). `ts` of a quote later than SYSTIME = the previous **trading day** of the engine (business day or weekend session, `mdhub.core.timeutil.previous_session_day`); a changed row older than the last stored `ts` of its series is logged. CETS has no weekend sessions; bond quotes (`md.bond_quote_intraday`) use business days (`BONDS_TRADE_WEEKEND_SESSIONS = False`: bond boards are assumed not to trade in the stock engine's weekend sessions).
- `md.futures_eod(trade_date, secid, open, low, high, close, settle_price, waprice, volume, value, open_position, capture_id)`
- `md.futures_intraday(ts, secid, source, source_variant, bid, offer, last, open_position, latency, capture_id)` PK (secid, source, source_variant, ts) — row history on. Dated with FORTS trading days: the Monday 08:50 poll that still shows the Sunday session's last trade dates it Sunday, not Friday.
- `md.algopack_fo_tradestats` / `md.algopack_fo_obstats` PK (secid, interval_ts) and `md.algopack_futoi` PK (ticker, interval_ts, clgroup) — AlgoPack 5-minute bars of FX futures (`*_rub_per_unit` normalized prices).
- `md.index_eod(trade_date, secid, board, open, high, low, close, value, volume, duration_days, yield_pct, basis ∈ {history, marketdata}, source_ts, capture_id)` PK (trade_date, secid) (RUSFAR family, RGBI, RUCBI, MOEXREPO…); `basis = marketdata` is a same-day value later replaced by history.
- `md.fixing(index_code, fixing_date, source, value, unit ∈ {pct, index}, effective_date, published_on, published_at, details jsonb, capture_id)` PK (index_code, fixing_date, source) — KEY_RATE (by effective date), RUONIA (+ details: volume, deals, percentiles), RUONIA_INDEX, TERM_RUONIA_1M/3M/6M, CBR_DEPOSIT_STANDING / CBR_LENDING_STANDING (lending: column 7 "Основной механизм" from 2023-10-16, column 9 before — one column per date, never spliced), legacy RUSFAR/other sources…
- `md.fx_fixing(fixing_date, secid, rate, rate_pct)` PK (fixing_date, secid) — MOEX currency-market fixings (SRATE_CNY_ON).
- `md.fx_official(effective_date, ccy, nominal, value, rate_per_unit, set_date, source='cbr', capture_id)` PK (effective_date, ccy)
- `md.fx_indicative(trade_date, clearing ∈ {tc, mc}, ccy_pair, rate, source='moex', capture_id)` — MOEX indicative 14:00/19:00 (FX mark candidate).
- `md.ecb_fx(rate_date, ccy, rate_per_eur, capture_id)`
- `md.curve_snapshot(id, source, dataset ∈ {ruonia_ois, key_rate_swap, cny_ruonia_xccy, cny_rusfar_ois, cny_futures_yields, usd_futures_yields, cfets_fr007_irs, …}, quote_basis ∈ {bid_ask_mid (every quote two-sided), mid_only (at least one mid-only quote; two-sided quotes then carry `extra.quote_basis`)}, fetched_at, snapshot_date, source_ts, market_closed, content_sha256, parser_version, label, meta jsonb, capture_id, legacy_id)` + `md.curve_quote(snapshot_id, tenor, position, tenor_months, tenor_days, pillar_date, bid_pct, ask_pct, mid_pct, extra jsonb)` — unchanged consecutive snapshots are not duplicated (content hash per source+dataset compared to previous).
- `md.zcyc_snapshot(trade_date, trade_time, source_ts, params jsonb (B1..B3,T1,G1..G9), standard_yields jsonb, check_max_error_bp, check_tolerance_bp, is_eod, capture_id)` PK (trade_date, trade_time) — published yields must be reproduced from the params; rejected publications go to `md.zcyc_reject(capture_id PK, trade_date, trade_time, max_error_bp, reason)`.
- `md.cbr_liquidity(observed_on, …14 metrics (columns 2..15 by position, labels checked against the live header)…, methodology_version, revision_no, capture_id)` (+ revisions via row_history)
- Read helpers (0410): `md.v_bond_eod` (EOD + yields), `md.v_fx_cbr_latest`, as-known-at functions `md.bond_eod_known_at`, `md.bond_eod_yield_known_at`, `md.fixing_known_at`, `md.v_bond_eod_known_at`.

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
                       ratescalcs.py, otc_monitor.py, cbonds.py, sdfi.py — pure fetch + parse,
                       no DB
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
| `bonds_static` | daily 06:30 full (~14 min at 3 rps); 09:05–19:05 hourly new-listing probe (`bonds_static_probe`) | ISS securities + bondization | full universe (all boards incl. structured); revisions via row_history; raw captures committed per fetch phase, writes in one transaction |
| `cbonds_enrich` | daily 07:30 | Cbonds | auto-skip with status `skipped` while auth returns 401 |
| `bonds_eod` | 19:20 (prelim, D) and 07:15 next day (final, D-1 + D-2) | ISS history by date | final run captures post-session revisions; backfill tasks of 5 days |
| `bonds_intraday` | every 15 min 10:00–19:00 | ISS marketdata (delayed) | store changed rows only |
| `fx_moex_intraday` | every 5 min 07:00–23:55 (MOEX_FX) | ISS CETS marketdata | change-only |
| `fx_moex_eod` | 00:20 (prelim, the session that just ended) and 07:20 (final, D-1 recheck) | ISS selt history | |
| `fx_indicative` | 14:10, 19:10 | ISS indicativerates + fixings | |
| `futures_intraday` | every 1 min 08:50–23:50 on MOEX_FORTS trading days (business days + weekend sessions) | APIM realtime marketdata (FX futures + perpetuals) | fallback ISS delayed, labelled |
| `futures_eod` | 19:25 (prelim, D) and 07:25 next day (final, D-1) | ISS FORTS history (settle, OI) | backfill tasks of 10 days |
| `algopack_fx_futures` | every 5 min, minutes 1-56/5, 07:01–23:56 on MOEX_FORTS trading days (business days + weekend sessions) | APIM tradestats/obstats/futoi | incremental polls read the latest bars (`latest=1`, all contracts; ~4 requests); every `MDHUB_ALGOPACK_FULL_REREAD_MINUTES` (30) and whenever the interval check finds holes the poll re-reads the whole trading day per FX contract and FUTOI ticker (`from=` first weekend-session day before today, else today; ~140 requests), captures `intraday:reread:*` (7-day retention). ≈ 5.4k APIM requests per trading day (was ≈ 28k). Dedup on (secid, interval); backfill tasks of 3 days |
| `indices_eod` | 12:40 (RUSFAR), 18:10 (RUSFARRT), 19:30 all; `days_back=[0,1]` | ISS index history (+ same-day marketdata) | `indices_all` freshness lag 2 business days (D-1 history arrives with the 12:40 run) |
| `zcyc` | every 15 min 10:00–19:00 (intraday); EOD 19:05; final 07:10 next day | ISS zcyc | |
| `cbr_daily` | 09:00, 14:30, 16:30, 18:30 (all days) | CBR | key rate, RUONIA (+index, term), official FX (all ccy), liquidity, standing rates |
| `cbr_meetings` | 09:10 on the 1st business day of the month (calendar refresh + meetings); 15:00 daily `post_meeting` (no-op unless a meeting in the last 4 days is unconfirmed) | CBR + MOEX ISS calendars | refreshes `ref.calendar_day` first, then meetings, then resolves effective dates |
| `ecb_fx` | 18:30 (all days) | ECB | |
| `swaps_ratescalcs` | every 30 min 09:00–23:00 | Ratescalcs | skip identical content |
| `swaps_otc_monitor` | every 15 min 09:00–20:00 (MOEX_STOCK business days; legacy collector polled 10:00–19:45) | OTC Monitor `/api/bond/` (one GET) | datasets `ruonia_ois`, `key_rate_swap`, `cny_ruonia_xccy`, `cny_rusfar_ois` (source `otc_monitor`, `DatasetSpec` `otc_monitor_<dataset>`, frequency `event`). Exact tenor set and sanity bounds `lo ≤ bid ≤ offer ≤ hi` per curve (RUB 0–100; CNY −50–300: negative CNY rates and >200% RUSFARCNY spikes are real 2024 quotes): a failing curve is not stored and the run ends `partial`. Content-hash dedup per (source, dataset) continues the legacy-mapped history (same hash, tenor order and basis rules); one raw capture per fetch, only when a curve changed. Missing token → `skipped`; HTTP 401/403 → `failed` |
| `quality` | 08:30 and 20:30 | DB | freshness & gap detection → `ops.data_gap` + `ops.backfill_task`; job health (only `schedule`/`catch_up` runs count as a fire that ran); re-runs scheduled fires skipped with `lock_busy` (trigger `catch_up`) |
| `backfill_worker` | every 10 min (all days) | DB | processes backfill tasks (8-minute budget), yields to waiting scheduled runs |
| `maintenance` | 02:00 `backup` (pg_dump, keep 14); 03:00 `retention` (all days) | DB | raw retention of `intraday:reread:` captures (7 days, `MDHUB_RAW_RETENTION_DAYS_REREAD`) and other `intraday:` captures (90 days), orphaned `running` job runs (>12 h) → failed, VACUUM ANALYZE |

**Lock contention.** Every job holds advisory lock `mdhub.job:<name>`. A scheduled fire waits up to
15 minutes for it (polling rules — intraday snapshots, AlgoPack, Ratescalcs, OTC Monitor, the backfill worker —
do not wait). Backfill tasks of heavy datasets are short and yield to a waiting scheduled run. A fire
that still cannot run is recorded as `skipped` with error `lock_busy: …`; job health reports it as
`missed` and the next `quality` run re-executes it (`days_back` resolved against the fire date).
Every run records its origin in `ops.job_run.trigger` (scheduler `schedule`, quality catch-up
`catch_up`, backfill task `backfill`, `mdhub run`/`mdhub ops` `cli`): for "did the fire run" job
health considers only `schedule`/`catch_up` runs, so a backfill or CLI run of the same job after a
lock-busy skip (or after a fire the scheduler never attempted) no longer hides the missed fire. A
waiter re-announces itself on every poll (`mdhub.jobwait:<name>`) until it gets the lock.

**Weekend sessions (known limitation).** Polls of `futures_intraday` and `algopack_fx_futures`
run on weekend-session days and the Monday re-read covers the preceding weekend-session dates, but
the quality gap scan does **not** expect data on weekend-session days for any dataset
(`futures_eod`: ISS FORTS history attributes weekend trades to the next session date; AlgoPack's
`tradedate` attribution of weekend-session bars is unverified). A weekend session whose polls all
failed is therefore not reported as a gap; `interval_holes` still checks each polled day.

**Backfill contract.** A dataset's `backfill_job` accepts `date_from`/`date_to`; a day for which the
source confirms "no data" is recorded with `mdhub.core.gaps.record_no_data` (`open` until at least
one **business day of the dataset's calendar** lies between the day and today — so Friday found
empty by Monday's final run stays open and Tuesday decides, likewise D-1 = 12-30 seen on 01-11 —
then `unfillable`), not as a partial run. A partial run that leaves gaps of its range
open counts as a failed attempt (backoff, then `failed` and a 7-day cooldown).

## 7. Configuration (env)

`MDHUB_DATABASE_URL` (required), `MDHUB_TZ=Europe/Moscow`, `MDHUB_HTTP_PROXY` (optional),
`MDHUB_ISS_RPS=3`, `MDHUB_RAW_RETENTION_DAYS_INTRADAY=90`, `MDHUB_RAW_RETENTION_DAYS_REREAD=7`
(`intraday:reread:` captures), `MDHUB_ALGOPACK_FULL_REREAD_MINUTES=30`, `MDHUB_API_TOKENS` (comma list,
read API bearer tokens), `MDHUB_ENABLE_SDFI=false`, `MDHUB_BACKUP_DIR=/var/backups/mdhub`
(pg_dump target of `maintenance mode=backup` / `mdhub ops backup`), `MDHUB_API_POOL_SIZE=8`
(read-API connection pool), `MDHUB_LOG_LEVEL=INFO`, `MDHUB_HTTP_TIMEOUT=30`, plus source
credentials: `MOEX_API_KEY`, `MOEX_PASSPORT_LOGIN`, `MOEX_PASSPORT_PASSWORD`,
`RATESCALC_USERNAME`, `RATESCALC_PASSWORD`, `CBONDS_LOGIN`, `CBONDS_PASSWORD`,
`OTC_MONITOR_TOKEN`. Empty values count as unset (`env_ignore_empty`).

## 8. Read API (v1, bearer token)

Unauthenticated: `/health/live`, `/health/ready` (DB reachable, migrations applied with matching
checksums). Everything under `/v1` needs `Authorization: Bearer <token>` (`MDHUB_API_TOKENS`),
including the OpenAPI document `/v1/openapi.json`.

Endpoints: `/v1/coverage`, `/v1/runs`, `/v1/runs/health`, `/v1/gaps` (ordered by dataset, date,
key), `/v1/bonds` (static, filters), `/v1/bonds/{secid}` (+ issuer, boards, coupons with status,
amortizations, offers, formula resolved + per source, ratings), `/v1/bonds/eod?date=` (all boards,
joined yields; `known_at=`), `/v1/bonds/{secid}/history`, `/v1/fx/official`, `/v1/fx/eod`,
`/v1/fx/indicative`, `/v1/fx/ecb`, `/v1/futures/eod`, `/v1/futures/contracts`, `/v1/fixings`
(`known_at=`), `/v1/curves`, `/v1/curves/{dataset}?as_of=`, `/v1/zcyc?date=`, `/v1/indices/eod`,
`/v1/calendar` (`ref.calendar_effective`: source precedence), `/v1/export/tdm?date=` (zip of TDM
1.3 CSV files + `manifest.json`; the MOEX 19:00 indicative `md_fx` candidate only with
`fx_candidate=true`; CLI `mdhub export tdm --fx-candidate`).

Conventions: `{"data": ..., "meta": {...}}`; dates ISO 8601, timestamps with offset; `numeric`
values as JSON numbers with exactly the stored digits (no float rounding); lists page with
`limit`/`offset` (`meta.next_offset`, max 10 000); strong `ETag` + `If-None-Match`; SECIDs are
case-sensitive (FORTS `SiZ6`, index `2xEQT`) and matched exactly (`/v1/bonds/{secid}` falls back to a
case-insensitive match only when unique). As-of: `known_at=` (offset required) reconstructs values
as known at that time from `ops.row_history` (bonds EOD, fixings); `/v1/curves/{dataset}?as_of=`
returns the latest snapshot with `fetched_at <= as_of`, one per source. TDM export rules: no forward fill; coupon
rate/amount only for `known` coupons (others empty, counted in `manifest.json` `withheld_values`);
`md_bond_quotes.ytm` = MOEX effective yield only when its yield date is the nearest offer ahead
(else maturity) and the bond is fixed/zero coupon, never YIELDCLOSE.

**Curve sources.** `ruonia_ois`, `key_rate_swap` and `cny_ruonia_xccy` have two live sources —
`ratescalcs` (dealer run pages every 30 min, no source clock; XCCY tenors ON–12M) and
`otc_monitor` (every 15 min, source clock `date_update`; XCCY 1W–5Y) — plus legacy `andromeda`
(mid-only history to 2026-09-10). They are separate series: the hub never merges or chooses, and
`/v1/curves/{dataset}` returns one snapshot per source (`source=` filters). A consumer that needs
one curve should take, at its cut `T`: (1) `otc_monitor` if its snapshot at `T` has a `source_ts`
on T's Moscow business day and, for OIS/KRS, `quote_basis = bid_ask_mid` (a `mid_only` OIS/KRS
snapshot is the provider's off-hours mark); else (2) `ratescalcs` if its snapshot at `T` was
fetched on that day; else (3) no value for the day (no forward fill). `andromeda` serves history
before the live sources only. `cny_rusfar_ois` (CNY OIS vs RUSFAR CNY) exists only from
`otc_monitor`, live from 2026-10-16 with no history; `cny_futures_yields` / `usd_futures_yields`
only from `ratescalcs`. The TDM export does not export swap curves (they need bootstrapping).

## 9. Deployment

`deploy/docker-compose.yml`: `postgres:16` (volume, tuned), `mdhub-migrate` (one-shot),
`mdhub-worker` (scheduler), `mdhub-api` (uvicorn behind optional Caddy TLS). `.env` from
`deploy/env.example`. Shipping: `deploy/make_bundle.sh` builds
`market-data-hub-<YYYYMMDD>.tar.gz` = `git archive HEAD` + `db/mdhub.dump` (`pg_dump -Fc
--no-owner --no-acl` of the fully loaded DB) + `SEED_REPORT.md` (`mdhub report seed`, same
snapshot) + `MANIFEST.txt` + `SHA256SUMS`; `deploy/restore_seed.sh` (compose, or
`--database-url` for any PostgreSQL 16) restores it (pg_restore `--no-owner --no-privileges`,
`mdhub migrate`, `ops.schema_migration` checksum check, read-only grants). Ad-hoc dump: `mdhub ops backup` (same as `maintenance mode=backup`). After
restoring an older seed run `mdhub migrate`, then `mdhub calendar seed` (calendars from 2019,
TARGET rule from 2002,
repairs ISS-calendar rows, fills `ref.weekend_session` from the ISS calendar) and the next
`bonds_static` full run (coupon status semantics, `schedule_constant` classification) — or
`mdhub run bonds_static -p mode=full` right away.
Bare-metal alternative: `deploy/systemd/*.service|*.timer` + `deploy/install.sh` (Ubuntu 24.04:
postgresql-16, uv).

## 10. Testing

Unit tests with recorded fixtures (sanitized real responses) per parser; integration tests
against a real PostgreSQL 16 (via `pgserver` locally / service in CI); idempotency tests
(load twice → no new rows, no history); revision tests (changed value → row_history).
Live smoke tests (`pytest -m live`) hit real endpoints with env credentials.
