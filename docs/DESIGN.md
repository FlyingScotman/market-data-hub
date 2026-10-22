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
5. **Published snapshots**: immutable, content-addressed static and market snapshots under the
   hub-owned Market Snapshot Contract v1 (§11) - the only thing the desk backend reads.

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
| MOEX APIM `apim.moex.com/iss` | `MOEX_API_KEY` (Bearer) | realtime futures (FX futures bid/offer/last/OI), AlgoPack FO tradestats + FUTOI (rolling 90-day window; order-book obstats not collected, dropped in 0423) | futures realtime; bonds/FX delayed |
| MOEX Passport | `MOEX_PASSPORT_LOGIN/PASSWORD` | optional; login works, account currently **not** realtime-entitled | delayed |
| MOEX SDFI swap curves `/iss/sdfi/curves` | subscription | catalogue only; loader ready behind feature flag `MDHUB_ENABLE_SDFI` | — |
| Ratescalcs `ratescalcs.ru` | `RATESCALC_USERNAME/PASSWORD` (Basic, GET only) | RUONIA OIS, KRS (IRS vs key rate), CNY xccy vs RUONIA, CNY futures-implied yields snapshot | dealer runs, 08:50–23:00 MSK |
| CBR `cbr.ru` | — | key rate, RUONIA, term RUONIA, official FX (all ccy, from 1998), liquidity, meeting calendar, refinancing rate 1992–2015 (closed series) | daily |
| Rosstat `rosstat.gov.ru` | — | monthly CPI to the previous month since 1991 (`ipc_mes_MM-YYYY.xlsx`, sheet `01`; the TLS chain omits the Russian Trusted Sub CA, bundled in `mdhub/certs`) | monthly, mid next month |
| ECB eurofxref | — | G7 crosses (EUR base) daily + history since 1999; **does not answer from the hub VPS** (WEB-4) | daily ~16:00 CET |
| ECB Data Portal `data-api.ecb.europa.eu` (`ecb_data_portal`) | — | €STR (`EST.B.EU000A2X2A25.WT`, primary), reference rates `EXR` (fallback host of `ecb_fx`, same values), AAA euro area par yields `YC` (proxy curve `eur_aaa_par`) | €STR D at 08:00 CET on D+1; EXR ~16:00 CET; YC daily |
| Bundesbank `api.statistiken.bundesbank.de` (`bundesbank`) | — | €STR republication `BBMMB/D.EU000A2X2A25.WT` (fallback 1); German Federal par yields `BBSIS ZAR` (fallback curve `eur_bund_par`). Not reachable from the development environment: format from documentation, verify with `mdhub probe-sources` | daily |
| DBnomics `api.db.nomics.world` (`dbnomics`) | — | €STR mirror `ECB/EST/B.EU000A2X2A25.WT` (fallback 2; may lag). Not verified live | daily |
| FRED `fred.stlouisfed.org/graph/fredgraph.csv` (`fred`, no key; the JSON API needs one) | — | €STR (fallback 3), SOFR + averages/index (fallback 1), H.10 noon buying rates `DEX*` (cross kind `fred_h10`). Not verified live | daily; H.10 weekly |
| NY Fed Markets API `markets.newyorkfed.org` (`nyfed`) | — | SOFR (+ percentiles, volume), SOFR 30/90/180-day averages and index (administrator, primary) | D at ~08:00 New York on D+1 |
| SNB data portal `data.snb.ch` (`snb`) | — | SARON (cube `snbgwdzid`; SIX administers and fixes it at the Zurich close, the SNB republishes; one source; not reachable from the development environment, check with `probe-sources`) | D in the evening of D |
| U.S. Treasury `home.treasury.gov` (`us_treasury`) | — | daily par yield curve XML (proxy curve `usd_ust_par`, **not OIS**) | ~15:30 New York, posted in the evening |
| CBR `/hd_base/infl/` (`cbr`) | — | official CPI y/y (`Инфляция, % г/г`, Rosstat figure) from 2013-09 → `CPI_RU_YOY` | monthly, after the Rosstat release |
| Cbonds JSON API | `CBONDS_LOGIN/PASSWORD` | static enrichment (day count, floater conventions, ratings, offers). **Currently HTTP 401** (likely IP allow-list) → optional, auto-disabled on 401 | — |
| OTC Monitor `otc-monitor.ru/api/bond/` | `OTC_MONITOR_TOKEN` (Bearer; 401 without) | RUONIA OIS (17 tenors 1W–10Y), KRS (13, 3M–10Y), CNY xccy vs RUONIA (14, 1W–5Y), CNY OIS vs RUSFAR CNY (8, 1W–1Y; new, live only) — one JSON list of ~440 rows (bonds, FX, spreads, curves); legacy history 2026-09-04 → 10-15 imported. **No history endpoint** (`?date` ignored, `/api/history` 404) | current state only, polled every 15 min; source clock `date_update` (MSK); two-sided during the day, mid-only off-hours and on some CNY tenors |
| НКР `www.ratings.ru` | — | credit ratings: public list exports `/issues.php` (issue ratings, ISIN, issuer INN) and `/issuers.php` (issuer ratings, INN/OGRN, outlook, sector) — job `ratings_agencies` (WEB-18) | daily |
| НРА `www.ra-national.ru` | — | credit rating actions since 2020: Excel export linked from `/ratings/` (ISIN, INN, status, outlook, watch; the link's `security_key` is read from the page each run, never stored) | daily |
| Эксперт РА `raexpert.ru` | — | credit ratings without a free list (export/API are paid): 12 rating-list pages + sitemap -> press releases -> company / region / bond pages with full rating history; crawl with a request budget, 0.5 rps, no query-string URLs (robots.txt) | daily, incremental |
| АКРА `www.acra-ratings.ru` | — | **not loaded**: the site's TLS chain omits its intermediate (GlobalSign GCC R46 OV TLS CA 2025), so no verified connection; registered in `ops.source` only | — |
| ФНС (ЕГРЮЛ, pb/bo.nalog.ru), e-disclosure, НРД | — | **not loaded**: unreachable from the build environment (FNS: connection reset; e-disclosure: anti-bot 403; НРД ЦКИ: paid). Issue terms enter through the curated file with evidence (job `bond_terms_curated`) | — |
| Andromeda | — | history only (Playwright scraping is dropped): RUB OIS/KRS from the legacy archive; CNY OIS vs RUSFAR CNY (2023-12-12 →) and CNY xccy vs RUONIA (2023-07-10 →) from the owner's export, imported with `mdhub legacy andromeda-cny` (re-runnable for later exports) | EOD, mid-only; not scheduled |

## 4. Data model (PostgreSQL schemas)

### `ops` — provenance and operations
- `ops.source(code PK, name, base_url, licence_note)`
- `ops.job_run(id, job, started_at, finished_at, status ∈ {running, succeeded, partial, failed, skipped}, params jsonb, rows_inserted, rows_updated, rows_unchanged, error text, host, trigger ∈ {schedule, catch_up, backfill, cli, api, other})` — `trigger` (0421; NULL for older rows) records who started the run; job health counts only `schedule`/`catch_up` (and NULL) runs as "the scheduled fire ran".
- `ops.raw_capture(id, run_id, source, endpoint, url_redacted, http_status, content_type, fetched_at, source_ts, parser_version, sha256, payload_gz bytea, size_bytes)` — unique (sha256, endpoint, fetched_at). Retention: intraday raw 90 days (configurable), AlgoPack raw (all its endpoints, incl. backfill `*_history`) the AlgoPack rolling window (§6), other EOD/static raw forever.
- `ops.row_history(id, table_name, row_key jsonb, old_row jsonb, replaced_at, replaced_by_run)` — written by trigger `ops.track_changes()` (`AFTER UPDATE` only: deletes, e.g. retention, write no history). Images of AlgoPack rows before the rolling window are deleted with the rows.
- `ops.data_gap(dataset, key jsonb, gap_date, detected_at, status ∈ {open, backfilled, unfillable, not_expected}, note)` — `unfillable` is terminal (the source confirmed "no data" and at least one **business day** of the dataset's calendar has passed since the day, `mdhub.core.gaps`); data that appears later still turns it `backfilled`. A gap the scan no longer reports becomes `backfilled` only when the table has ≥ `min_rows_per_day` rows for it, otherwise `not_expected` (calendar corrected, e.g. a holiday; re-opened if expected and missing again). An `open` gap of a rolling-window dataset dated before its window also becomes `not_expected` (note "outside the rolling N-day window"; §6), never `unfillable` (the source may have the data) nor `backfilled`; `unfillable` gaps stay. `ops.v_coverage` counts `open_gaps`, `unfillable_gaps`, `not_expected_gaps`; `ops.v_open_gaps` lists `open` only.
- `ops.backfill_task(id, dataset, params jsonb {date_from, date_to}, status, attempts, last_error, created_at, done_at, not_before, started_at, last_run_id, source)` — one task per (backfill job, range): datasets sharing a job share tasks.
- `ops.dataset_status` (latest check per dataset), `ops.quality_report` (one report per `quality` run, incl. job health and catch-up); views `ops.v_coverage`, `ops.v_job_health`, `ops.v_open_gaps`, `ops.v_backfill_queue`.
- `ops.legacy_import` (SQLite import bookkeeping), `ops.schema_migration(version, applied_at, checksum)`
- Observability (0610, [ADMIN_DESIGN](ADMIN_DESIGN.md) §3.2): `ops.api_request(ts, token_id, token_name, client_ip, method, path, query, status, duration_ms, response_bytes, request_id, user_agent)` — one row per read-API request except `/health/*`, written in batches by the API (every second or 200 rows, off the request path); `path` is the route template (`/v1/bonds/{secid}`, `<unmatched>`), `query` has credential-like values replaced by `***`, tokens are never stored. `ops.worker_heartbeat(instance_id, process, host, pid, version, started_at, beat_at, stopped_at, running_jobs jsonb)` — upserted every 30 s by `mdhub scheduler`; a stale `beat_at` without `stopped_at` = the process died. `ops.source_probe(run_id, source, endpoint, ts, ok, http_status, latency_ms, auth ∈ {ok, rejected, not_configured, n/a}, tls_expires_at, error)` — job `source_probe` (§6). `ops.job_source(job, source)` maps loader jobs to their sources; view `ops.v_source_status` = per source the last probe, the last ok probe and the last `succeeded`/`partial` run of a job reading it. All three tables: retention `MDHUB_OPS_LOG_RETENTION_DAYS` (90) in `maintenance mode=retention`.
- `ops.published_snapshot(id = sha256 of the canonical bundle, schema_version, kind, cut_type, business_date, cut_ts, known_at, static_snapshot_id, supersedes, content_sha256, calendar_fingerprint, sources, datasets, excluded, excluded_count, manifest, payload_gz, size_bytes, gzip_bytes, run_id, published_at)` (0510) — immutable published snapshots (§11); not `ref.static_snapshot`.

### `admin` — secrets and identities (0600+)
- `admin.api_token(id, name, token_sha256 bytea unique, prefix, scopes text[] ⊆ {read, snapshots, export}, created_by, created_at, expires_at, revoked_at, last_used_at, last_used_ip)` — named read-API tokens (§8); names unique among live tokens. Users, sessions and the audit log of the admin console follow in phase B ([ADMIN_DESIGN](ADMIN_DESIGN.md) §3.1).
- **Never readable by the read-only consumer role.** `deploy/sql/grant_readonly.sql` skips the schema; because the owner's global default privileges (`readonly_defaults.sql`) also reach objects created in `admin`, every admin migration ends with `CALL admin.restrict_access()` (revokes everything in `admin` from `PUBLIC` and all roles but the owner), and `mdhub migrate`, `grant_readonly.sql` and `readonly_defaults.sql` call it again (seed restores). `tests/test_deploy.py::test_readonly_role_cannot_read_admin` enforces it.

### `ref` — static / reference
- `ref.calendar_day(calendar ∈ {MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION, TARGET}, day, source, is_business_day, is_deleted, known_at, ingested_at, version, note)` PK (calendar, day, source) — rows are never deleted: a day the ISS calendar stops listing is soft-deleted (`is_deleted`, a versioned change with the old image in `ops.row_history`; readers skip it, a relisted day is re-activated); `ingested_at` = version time (0500) — past days from observed trading / RUONIA observations (from 2019-01-01), the days the official MOEX ISS calendar lists (never a synthesized year), future days from the curated data file; TARGET: a computed rule for all days (`rule_target`). **Precedence by source, not recency**: `ref.calendar_source_rank()` observed > moex_iss_calendar > curated > rule_* > curated_unverified (`known_at` breaks ties; `rule_*` 0422); view `ref.calendar_effective`, `mdhub.core.bizday.DbCalendar`; days without rows: Monday–Friday.

  | calendar | past days (observed) | future days | used by |
  |---|---|---|---|
  | `MOEX_STOCK` | `observed_moex_stock`: IMOEX or RGBI history row | `moex_iss_calendar` (listed days only), `curated` / `curated_unverified` | default; MOEX stock/bond datasets |
  | `MOEX_FX` | `observed_moex_fx`: CETS CNYRUB_TOM history row | same | MOEX FX datasets |
  | `MOEX_FORTS` | `observed_moex_forts`: Si futures volume > 0 | same | FORTS datasets |
  | `RU_PRODUCTION` | `curated` (0.3.1): the official production calendar 2019– (Labour Code art. 112 + Government transfer decrees); RUONIA only cross-checks it | `curated` (Government decrees) | CBR datasets, meeting effective dates |
  | `TARGET` (0422) | — (rule only) | `rule_target` for past and future days: weekends + computed TARGET closing days (Jan 1, Good Friday, Easter Monday — Gregorian computus —, May 1, Dec 25, Dec 26), from 2002-01-01 (or `--since` if later) through 2037-12-31 (or the end of next year if later) | `ecb_fx` |

  Observed rows cover every day from the start to the last observed day (no trading / fixing → `false`); curated and ISS rows are written only for later days. The curated file runs to **2037** (WEB-15): 2028–2037 are `curated_unverified` (provisional, lowest precedence; the static snapshot reports them through `provisional_from`) — RU_PRODUCTION from the Labour Code art. 112 rule alone (the Government decree's moves of January weekend days and its working Saturdays are not known in advance), MOEX_* from the MOEX practice rule of 2027 (closed on weekday official holidays and Dec 31). TARGET is rule-only: the rule is exact (ECB rate dates 2015-01-02..2026-10-15 match it day for day: the 57 weekdays without rates are exactly the closing days), and deriving it from `md.ecb_fx` would turn a failed ECB load into a holiday. ECB data only **cross-checks** the rule on every calendar refresh: a TARGET business day without ECB rates (inside the ECB data range, past the 1-business-day no-data grace) or ECB rates on a closing day → warning and calendar run `partial`; the calendar is not changed. `ecb_fx` used Monday–Friday before 0422, so every TARGET holiday ended `unfillable`; the quality scan closes those gaps as `not_expected` once the TARGET calendar is seeded.

  **RU_PRODUCTION is curated, not observed (0.3.1, review §2.5 item 6).** Until 0.2.0 its past days came from `observed_cbr_ruonia` (a RUONIA fixing exists), so a business day without a fixing became a holiday and a missing RUONIA was invisible: on production the last working day of every year 2019–2025 (31.12.2019, 31.12.2020, 30.12.2021, 30.12.2022, 29.12.2023, Sat 28.12.2024, 30.12.2025) and the 2020 presidential non-working days 30.03–03.04 and 24.06 were "holidays". The curated file now carries the official production calendar of 2019–2025 too (checked against the ConsultantPlus production calendar; equal to the RUONIA days except those dates), its rows cover past days (`OFFICIAL_CURATED_CALENDARS`), and the refresh soft-deletes the `observed_cbr_ruonia` rows (versioned). RUONIA dates only cross-check the calendar: a fixing on a non-business day → warning, run `partial`; a business day without a fixing is a gap of `cbr_ruonia` (`/v1/gaps`, backfill → `unfillable` when CBR confirms no value). **Known limitation (observed MOEX calendars):** past MOEX days come from trading history: if that load failed and the day is still empty when the calendar refresh runs, the day becomes a non-business day.
- `ref.weekend_session(calendar, day, source, trade_session_date, is_deleted, known_at, ingested_at, note, capture_id, run_id)` PK (calendar, day, source) (0421; soft deletes 0500) — days on which a MOEX engine trades although they are not settlement business days (ISS `engines/{engine}/calendar.json` rows `is_work_day=1, reason='W'`; trades belong to `trade_session_date`). Stored for every listed day of the year (past ones too: the observed calendars cannot see them); future rows ISS no longer lists are soft-deleted. **Trading day of an engine** = business day (`ref.calendar_effective`) or a day listed here (`CalendarPredicate.is_trading_day`); `ref.calendar_day` keeps these days non-business. Trading days date quotes ("previous session") and gate the FORTS polls; business days drive EOD trade dates, `days_back` and gap expectations. Calendar refresh: an ISS engine answer with < 5 rows of its engine or none in the current year is treated as incomplete — listed days are written, nothing is deleted (warning, run `partial`).
- `ref.issuer(issuer_id PK (MOEX emitent_id), inn, ogrn, okpo, name, short_name, country, sector, is_sovereign, …, updated_at)` — `name`/`inn`/`okpo` from the bond directory (`bonds_static`); `short_name`, `ogrn`, `oksm`, `country` (ISO 3166-1 alpha-2 of OKSM, `mdhub/data/oksm_iso3166.json`) from `ref.issuer_card` (0110: one `/iss/emitters/{id}.json` card per issuer of a publishable bond, job `issuers_static`, refreshed weekly or when `/iss/emitters.json` shows a changed TITLE/SHORT_TITLE/INN; raw capture per card; `last_seen` = cache time). One writer per column, so the two jobs never overwrite each other. `sector` / `sector_source` (`curated` per-INN entry, `group` curated issuer group, `okved` OKVED2 mapping, `rule` bond-class or name rule, `agency` industry a rating agency publishes for the INN — `agency_industry_rules`, only where no curated / group / OKVED / class / name rule assigns one; agencies that disagree assign nothing; WEB-18) are assigned by `issuers_static` from the versioned `mdhub/data/sector_map.json` (desk taxonomy of 19 codes; no match → NULL); `okved` is not loaded (FNS registers are not reachable from the hub). Issuer groups (`group_id`, `group_name`, `group_relation`, `is_state_owned`, `group_evidence`) come from the curated `mdhub/data/issuer_groups_curated.json` (keyed by INN, MOEX emitter id cross-check, evidence per row; no free machine-readable source); an SPV of a group takes the group's sector.
- `ref.bond(secid PK, isin, regnumber, issuer_id, name, short_name, bond_type (MOEX label), bond_class (derived enum: ofz_pd, ofz_pk, ofz_in, ofz_ad, sovereign_fx, subfederal, municipal, corporate, exchange_bond, structured, mbs, …), coupon_type ∈ {fixed, floating, indexed_face, zero, variable_known, unknown}, face_value, initial_face_value, face_unit, currency_id (settlement), issue_date, maturity_date (NULL for perpetual), is_perpetual, coupon_frequency, day_count, is_amortizing, is_subordinated, is_structured, qualified_only, high_risk, listing_level, issue_size, primary_board, start_date_moex, status ∈ {active, matured, delisted}, …, source, updated_at, last_seen_run)` — `coupon_type_basis` ∈ {moex_label, benchmark, ofz_series, schedule, schedule_constant, none}: `schedule_constant` (0421) = an active unlabelled bond classified `fixed` because it has no benchmark / COUPON_DETAILS and no rate formula from any source, ≥ 2 paid coupons, one constant `value_prc` over all paid and future coupons, and a CBR key-rate change (`md.fixing` KEY_RATE) after the start of its first and on/before the start of its last paid coupon period (a key-rate floater would show different paid rates); never for structured notes. Seed 2026-10-16: 335 of the 433 active `unknown/schedule` bonds qualify (mostly subfederal/municipal/corporate amortizers). `amortizer_constant_rate` (0426, WEB-13 HS6) = an active amortizer by MOEX BOND_TYPE (`Амортизируемая облигация` / `Амортизируемые облигации`) without COUPON_DETAILS, COUPON_BENCHMARK or a rate formula whose fully valued schedule carries one positive rate, classified `fixed` without the key-rate-change test (new issues have no paid coupons); weaker evidence than `schedule_constant`, visible in the basis; never for structured notes.
- `ref.bond_board(secid, board, is_primary, lot_size, min_step, decimals, settle_code, currency_id, listed, first_seen, last_seen)`
- `ref.bond_coupon(secid, coupon_no, start_date, pay_date (coupondate), record_date, amount, value_rub, value_prc, face_value, face_unit, status ∈ {known, current_published, projected_by_moex, unknown}, is_deleted, source, updated_at)` PK (secid, coupon_no) — `coupon_no` = position in the complete MOEX schedule. Status semantics for consumers: `known` = fixed published amount or paid coupon; `current_published` = the current floater period as MOEX shows it, not proven fixed in advance (may be a projection; `known` only with a curated formula stating a fixing before the period); `projected_by_moex` = MOEX forward-fill / in-arrears / indexed face / unknown coupon type; `unknown` = no value. An active bond without MOEX fixed/floating labels is never `fixed` just because its schedule is fully valued.
- `ref.bond_amortization(secid, amort_date, value_prc, value (amount), face_value, data_source)` PK (secid, amort_date)
- `ref.bond_offer(secid, offer_date, offer_type (put/call/buyback/…), price, value, offer_date_start, offer_date_end, agent, status)` — `0000-00-00` → NULL + flagged.
- `ref.bond_rate_formula(secid, source ∈ {moex_iss, cbonds, curated, legacy_bc}, benchmark ∈ {KEY_RATE, RUONIA, RUONIA_3M, ZCYC, CPI, …}, benchmark_raw, spread_bp, multiplier, lag_days, averaging, floor_pct, cap_pct, variable_rate_type, fixing_day, status ∈ {curated, inferred, partial}, evidence, is_deleted)` PK (secid, source); view `ref.bond_rate_formula_resolved` (conventions curated > cbonds > moex_iss > legacy_bc; benchmark curated > moex_iss > cbonds > legacy_bc).
- `ref.bond_rating(secid, agency, scale, source, issuer_id, rating, outlook, rating_date, watch, action, grade, rating_type, evidence)` PK (secid, agency, scale, source) — issue ratings: Cbonds (`cbonds`) and, since 0120 (WEB-18), the agencies' disclosure mapped by ISIN (sources `nkr`, `nra`, `raexpert`; agency = display name НКР / НРА / Эксперт РА, scale `national_ru`, evidence `mapping_basis = isin`).
- `ref.issuer_rating(issuer_id, agency, scale, source, rating_type, rating, grade, outlook, watch, action, rating_date, evidence)` (0120) — agency issuer ratings mapped by INN (`mapping_basis = inn`); RA Expert regions (no INN) through the ISINs of their rated bonds when those belong to one MOEX issuer (`region_bond_isin`).
- `ref.agency_rating(agency, object_kind ∈ {issuer, issue}, object_key (ISIN / INN / agency id), rating_type, scale, …, rating, grade, outlook, watch, action, action_raw, rating_date, press_release, industry, is_withdrawn, details)` (0120) — latest state per rated object exactly as the agency publishes it; `ref.rating_action(…, action_date, action_ref)` — history of actions (NRA export rows, RA Expert page history, NKR states as observed); `ref.agency_page(agency, url, kind ∈ {release, company, region, bond}, page_date, discovered_from, linked_at, fetched_at, http_status, content_sha256, error, capture_id)` — RA Expert crawl bookkeeping. `grade` = letter grade by `mdhub.sources.ratings_common.normalize_grade` (documented rule; never a guess); `action` only as published. Withdrawn ratings stay (`is_withdrawn`, `rating` NULL). Agency data: internal desk use; the agencies' terms forbid redistribution.
- `ref.bond_curated_term(secid, term ∈ {floater_formula, call, offer, replacement_bond}, term_key, value, evidence, file_version, is_deleted)` (0121) — issue terms read from issue documents, evidence per row (document URL, title, page, quoted clause, retrieval date); job `bond_terms_curated` from `mdhub/data/bond_terms_curated.json` also writes `ref.bond_rate_formula` (source `curated`, status `curated`) and `ref.bond_offer` (source `curated`, basis `document`).
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
- `md.algopack_fo_tradestats` PK (secid, interval_ts) and `md.algopack_futoi` PK (ticker, interval_ts, clgroup) — AlgoPack 5-minute bars of FX futures (`*_rub_per_unit` normalized prices). **Rolling window, no history beyond it**: only `trade_date` within the last `MDHUB_ALGOPACK_RETENTION_DAYS` (90) calendar days is kept (§6 `maintenance`); row history stays on for revisions inside the window. `md.algopack_fo_obstats` (order-book stats) was dropped in 0423 (owner decision, AlgoPack scope option b; it was ~2 GB of the seed).
- `md.index_eod(trade_date, secid, board, open, high, low, close, value, volume, duration_days, yield_pct, basis ∈ {history, marketdata}, source_ts, capture_id)` PK (trade_date, secid) (RUSFAR family, RGBI, RUCBI, MOEXREPO…); `basis = marketdata` is a same-day value later replaced by history.
- `md.fixing(index_code, fixing_date, source, value, unit ∈ {pct, index}, effective_date, published_on, published_at, details jsonb, capture_id)` PK (index_code, fixing_date, source) — KEY_RATE (by effective date), RUONIA (+ details: volume, deals, percentiles), RUONIA_INDEX, TERM_RUONIA_1M/3M/6M, CBR_DEPOSIT_STANDING / CBR_LENDING_STANDING (lending: column 7 "Основной механизм" from 2023-10-16, column 9 before — one column per date, never spliced), CBR_REFINANCING_RATE (one row per calendar day 1992-01-01..2015-12-31, `details` = period and normative act; from 2016 the refinancing rate **is** KEY_RATE — Указание 3894-У — and is not duplicated), CPI_RU_MOM (source `rosstat`, `unit = index`: CPI of the month to the previous month in %, `fixing_date` = first day of the month, `details.period`, `published_on` = Rosstat release date of the newest month only — older months have none; chain the months for any period — OFZ-IN face indexation, CPI-linked coupons), CPI_RU_YOY (source `cbr`, `unit = pct`: official CPI y/y − 100 as CBR republishes it, from 2013-09; cross-checked against the Rosstat m/m chain), ESTR (WEB-16; source = the host that served: `ecb_data_portal`, `bundesbank`, `dbnomics` or `fred`; % p.a. by TARGET day), SOFR / SOFR30DAVG / SOFR90DAVG / SOFR180DAVG (% p.a.) / SOFRINDEX (`unit = index`) (source `nyfed` or `fred`), SARON (contract 1.4 package P6; source `snb`, % p.a. by Zurich business day), legacy RUSFAR/other sources…
- `md.fx_fixing(fixing_date, secid, rate, rate_pct)` PK (fixing_date, secid) — MOEX currency-market fixings: USDFIXME (2013-03 → 2024-06-11), EURFIXME, CNYFIXME (→ 2025-04), EURUSDFIXME, USDCNYFIXME, HKDFIXME, TRYFIXME, SRATE_* (USD term rates to 2024; SRATE_CNY_ON current). The fixing list (`?date=`) is read at both ends of a range and every 28 days between, so fixings discontinued inside a backfill range are found.
- `md.fx_official(effective_date, ccy, nominal, value, rate_per_unit, set_date, source='cbr', capture_id)` PK (effective_date, ccy)
- `md.fx_indicative(trade_date, clearing ∈ {tc, mc}, ccy_pair, rate, source='moex', capture_id)` — MOEX indicative 14:00/19:00 (FX mark candidate).
- `md.ecb_fx(rate_date, ccy, rate_per_eur, capture_id)` — ECB reference rates; the capture's source (`ecb` eurofxref or `ecb_data_portal` EXR, the fallback host) tells which host served a day.
- `md.fx_cross(rate_date, base_ccy, quote_ccy, kind, source, rate, details, capture_id)` PK (rate_date, base_ccy, quote_ccy, kind) (0260, WEB-16) — non-RUB crosses besides the ECB, one series per `kind`, never merged: `cbr_cross` (two CBR official rates in force on the effective date, divided), `moex_indicative_cross` (two MOEX 19:00 indicative rates), `moex_cets` (traded CETS cross outright TOM, close per unit, days with trades only — none since 2024-06-11), `fred_h10` (Fed H.10 via FRED, raw capture). Derived kinds: 10 significant digits half-even, inputs in `details`, no capture. Pairs EUR/USD, GBP/USD, USD/CNY, USD/JPY, USD/CHF, USD/HKD, USD/INR, USD/KZT, USD/TRY, USD/AED, EUR/CNY (market convention). Reader `md.fx_cross_known_at` (0530).
- `md.curve_snapshot(id, source, dataset ∈ {ruonia_ois, key_rate_swap, cny_ruonia_xccy, cny_rusfar_ois, cny_futures_yields, usd_futures_yields, cfets_fr007_irs, …}, quote_basis ∈ {bid_ask_mid (every quote two-sided), mid_only (at least one mid-only quote; two-sided quotes then carry `extra.quote_basis`)}, fetched_at, snapshot_date, source_ts, market_closed, content_sha256, parser_version, label, meta jsonb, capture_id, legacy_id)` + `md.curve_quote(snapshot_id, tenor, position, tenor_months, tenor_days, pillar_date, bid_pct, ask_pct, mid_pct, extra jsonb)` — live loaders do not duplicate unchanged consecutive snapshots (content hash per source+dataset compared to the latest). History imports (legacy mapping, `andromeda-cny`) write one snapshot per day and revise it in place; row history is enabled on both tables (0424), and quotes removed by a revision are written to `ops.row_history` by the importer.
- `md.zcyc_snapshot(trade_date, trade_time, source_ts, params jsonb (B1..B3,T1,G1..G9), standard_yields jsonb, check_max_error_bp, check_tolerance_bp, is_eod, capture_id)` PK (trade_date, trade_time) — published yields must be reproduced from the params; rejected publications go to `md.zcyc_reject(capture_id PK, trade_date, trade_time, max_error_bp, reason)`.
- `md.cbr_liquidity(observed_on, …14 metrics (columns 2..15 by position, labels checked against the live header)…, methodology_version, revision_no, capture_id)` (+ revisions via row_history)
- Read helpers (0410): `md.v_bond_eod` (EOD + yields), `md.v_fx_cbr_latest`, as-known-at functions `md.bond_eod_known_at`, `md.bond_eod_yield_known_at`, `md.fixing_known_at`, `md.v_bond_eod_known_at`.
- As-known-at readers (0500, generated by `ops.create_known_at_function`): `ref.{issuer,bond,bond_board,bond_coupon,bond_amortization,bond_offer,bond_rate_formula,fx_instrument,cbr_meeting,calendar_day,weekend_session}_known_at(T)` (+ `ref.issuer_rating_known_at`, `ref.bond_rating_known_at` re-created by 0540, `ref.bond_curated_term_known_at` 0541), `ref.bond_rate_formula_resolved_known_at(T)` (+ `benchmark_raw`, `benchmark_source`), `ref.calendar_effective_known_at(T)`, and dated `md.{fx_official,fx_eod,fx_indicative,ecb_fx,index_eod,zcyc_snapshot}_known_at(T, from, to)`. Rule (as 0410): the image of a key at T is the old image of the earliest history row replaced after T, else the current row, if its version time (`ingested_at`; calendars: `ingested_at`, else `known_at` for pre-0500 images) is ≤ T. Pre-0500 calendar hard deletes left `_deleted` images and replay through the same rule.

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
  quality/             checks.py (integrity, freshness, gaps), report.py,
                       source_probe.py (job source_probe)
  core/heartbeat.py    worker heartbeat (ops.worker_heartbeat)
  snapshot/            canonical.py (canonical JSON, sha256, gzip), schema.py (contract
                       validation), build.py, static.py, market.py (builders, as known at),
                       publish.py (ops.published_snapshot), runner.py (build + publish),
                       readiness.py (eod_final inputs, corrections after backfills)
  jobs.py              job registry (@job), JobContext, run_job, running_jobs, loader discovery
  schedule.py          cron rules (APScheduler)
  api/                 app.py (FastAPI read API), auth.py (tokens, scopes),
                       requestlog.py (ops.api_request batch writer), columnar.py +
                       routes/history.py (/v1/history/*)
  cli.py               `mdhub` (typer); cli_ext/ auto-registered sub-command groups
```

Migration ownership (to avoid conflicts): `0001-0099` core/ops; `0100-0199` ref (static);
`0200-0299` md (market); `0300-0399` legacy; `0400-0499` api/views; `0500-0599` snapshot layer
(as-known-at readers, published snapshots);
`0600-0699` admin console and observability (ADMIN_DESIGN §3).

## 6. Update and storage rules (schedule, Europe/Moscow, business days unless noted)

| Job | When | Source | Notes |
|---|---|---|---|
| `bonds_static` | daily 06:30 full (~14 min at 3 rps); 09:05–19:05 hourly new-listing probe (`bonds_static_probe`) | ISS securities + bondization | full universe (all boards incl. structured); revisions via row_history; raw captures committed per fetch phase, writes in one transaction |
| `issuers_static` | daily 07:00 (after `bonds_static`) | ISS `/emitters.json` (list diff, ~14 pages) + `/emitters/{id}.json` (cards: new, changed, then stale > 7 days, ≤ 200 per run) | `ref.issuer_card` → `ref.issuer` short name / OGRN / country; a failed card → `partial` |
| `cbonds_enrich` | daily 07:30 | Cbonds | auto-skip with status `skipped` while auth returns 401 |
| `ratings_agencies` | daily 06:50 (before `issuers_static`, which reads the agencies' industries) | НКР exports (2 requests), НРА page + export (2), Эксперт РА lists (12) + sitemap (2) + new releases and the pages they link, at most `max_requests` (400) per run at 0.5 rps | -> `ref.agency_rating`, `ref.rating_action`, mapped to `ref.bond_rating` / `ref.issuer_rating`; one agency failing -> `partial`; result reports the coverage of active non-structured bonds. Initial RA Expert load: `-p max_requests=6000` once (releases of `since_days`=400) |
| `bond_terms_curated` | daily 06:55 | package file `mdhub/data/bond_terms_curated.json` | curated issue terms with evidence -> `ref.bond_curated_term`, `ref.bond_rate_formula` (curated), `ref.bond_offer` (curated); fail closed on a bad entry; unknown SECID -> `partial` |
| `bonds_eod` | 19:20 (prelim, D) and 07:15 next day (final, D-1 + D-2) | ISS history by date | final run captures post-session revisions; backfill tasks of 5 days |
| `bonds_intraday` | every 15 min 10:00–19:00 | ISS marketdata (delayed) | store changed rows only |
| `fx_moex_intraday` | every 5 min 07:00–23:55 (MOEX_FX) | ISS CETS marketdata | change-only |
| `fx_moex_eod` | 00:20 (prelim, the session that just ended) and 07:20 (final, D-1 recheck) | ISS selt history | |
| `fx_indicative` | 14:10, 19:10 | ISS indicativerates + fixings | |
| `futures_intraday` | every 1 min 08:50–23:50 on MOEX_FORTS trading days (business days + weekend sessions) | APIM realtime marketdata (FX futures + perpetuals) | fallback ISS delayed, labelled |
| `futures_eod` | 19:25 (prelim, D) and 07:25 next day (final, D-1) | ISS FORTS history (settle, OI) | backfill tasks of 10 days |
| `algopack_fx_futures` | every 5 min, minutes 1-56/5, 07:01–23:56 on MOEX_FORTS trading days (business days + weekend sessions) | APIM FO tradestats + FUTOI (no obstats since 0423) | incremental polls read the latest bars (`latest=1`, all contracts; ~3 requests); every `MDHUB_ALGOPACK_FULL_REREAD_MINUTES` (30) and whenever the interval check finds holes the poll re-reads the whole trading day per FX contract and FUTOI ticker (`from=` first weekend-session day before today, else today; ~75 requests), captures `intraday:reread:*` (7-day retention). ≈ 3.1k APIM requests per trading day (≈ 5.4k with obstats, ≈ 28k with a re-read on every poll). Dedup on (secid, interval); backfill tasks of 3 days. **Rolling window** of `MDHUB_ALGOPACK_RETENTION_DAYS` (90) calendar days, see below |
| `indices_eod` | 12:40 (RUSFAR), 18:10 (RUSFARRT), 19:30 all; `days_back=[0,1]` | ISS index history (+ same-day marketdata) | `indices_all` freshness lag 2 business days (D-1 history arrives with the 12:40 run) |
| `zcyc` | every 15 min 10:00–19:00 (intraday); EOD 19:05; final 07:10 next day | ISS zcyc | |
| `cbr_daily` | 09:00, 14:30, 16:30, 18:30 (all days) | CBR | key rate, RUONIA (+index, term), official FX (all ccy), liquidity, standing rates; refinancing rate only while not stored or for an explicit range before 2016. FX ranges: record dates from USD `XML_dynamic`; short ranges one `XML_daily` per record date, longer ones `XML_daily` samples (first record of each month, bisected to the exact record where the daily set changes) + one `XML_dynamic` per member currency (exact `Id` only — R01100 also lists R01100Z), cross-checked on every sample (fail closed): 1998–2026 in ~500 requests instead of ~7,000. A rate is in force until the next daily-set record; a currency missing from it has no rate from then on (never carried to its own next record) |
| `cbr_meetings` | 09:10 on the 1st business day of the month (calendar refresh + meetings); 15:00 daily `post_meeting` (no-op unless a meeting in the last 4 days is unconfirmed) | CBR + MOEX ISS calendars | refreshes `ref.calendar_day` first, then meetings, then resolves effective dates |
| `ecb_fx` | 18:30 (all days) | ECB | |
| `cbr_inflation` | 10:15, 19:45 (all days) | CBR `/hd_base/infl/` | `CPI_RU_YOY`; chain cross-check with `CPI_RU_MOM` (±0.05 pp → warning, `partial`); dataset `cbr_cpi_yoy` monthly from 2013-09 |
| `estr_fixing` | 10:20, 13:20 (all days) | ECB Data Portal → Bundesbank → DBnomics → FRED (`mdhub.loaders._fallback`) | last 10 days (empty series: from 2019-10-01); first answering source wins, stored under its own `source`; every attempt in the result (`tried`); `MDHUB_SKIP_SOURCES` drops sources; dataset `estr` (TARGET, any source) |
| `sofr_fixing` | 16:30, 22:30 (all days) | NY Fed → FRED | SOFR + averages + index; datasets `sofr` (Mon–Fri; U.S. holidays → `unfillable`), `sofr_averages` |
| `saron_fixing` | 19:20, 09:20 (all days) | SNB | SARON; dataset `saron` (Zurich business days; holidays → `unfillable`) |
| `fx_cross` | 09:15, 18:45, 19:20 (all days) | stored CBR / MOEX rows; FRED H.10 | `md.fx_cross`; a FRED failure is logged, derived kinds stored anyway |
| `proxy_curves` | 07:40, 23:40 (all days) | US Treasury; ECB Data Portal → Bundesbank | `md.curve_snapshot` datasets `usd_ust_par`, `eur_aaa_par` (fallback `eur_bund_par`): one snapshot per curve date, `fetched_at` 23:59:59 MSK of that date (synthetic: EOD cuts only), `meta.proxy`; revisions in place with row history |
| `rosstat_cpi` | 10:05, 19:35 (all days) | Rosstat price page → newest `ipc_mes_MM-YYYY.xlsx` | identical workbook skipped (SHA-256); Dec/Dec chain cross-check per complete year (fail closed); dataset `rosstat_cpi` monthly, lag 50 days |
| `swaps_ratescalcs` | every 30 min 09:00–23:00 | Ratescalcs | skip identical content (first poll of a Moscow day stores it anyway) |
| `swaps_otc_monitor` | every 15 min 09:00–20:00 (MOEX_STOCK business days; legacy collector polled 10:00–19:45) | OTC Monitor `/api/bond/` (one GET) | datasets `ruonia_ois`, `key_rate_swap`, `cny_ruonia_xccy`, `cny_rusfar_ois` (source `otc_monitor`, `DatasetSpec` `otc_monitor_<dataset>`, frequency `event`). Exact tenor set and sanity bounds `lo ≤ bid ≤ offer ≤ hi` per curve (RUB 0–100; CNY −50–300: negative CNY rates and >200% RUSFARCNY spikes are real 2024 quotes): a failing curve is not stored and the run ends `partial`. Content-hash dedup per (source, dataset) continues the legacy-mapped history (same hash, tenor order and basis rules), except that the first poll of a Moscow day stores the curve again even when unchanged (a market cut reads only snapshots fetched on its business date; WEB-13); one raw capture per fetch, only when a curve is stored. Missing token → `skipped`; HTTP 401/403 → `failed` |
| `quality` | 08:30 and 20:30 | DB | freshness & gap detection → `ops.data_gap` + `ops.backfill_task`; job health (only `schedule`/`catch_up` runs count as a fire that ran); re-runs scheduled fires skipped with `lock_busy` (trigger `catch_up`) |
| `backfill_worker` | every 10 min (all days) | DB | processes backfill tasks (8-minute budget), yields to waiting scheduled runs |
| `source_probe` | every 5 min (all days) | every source | one lightweight request per source → `ops.source_probe` (reachability, HTTP status, latency, credentials ok / rejected / not configured, TLS certificate expiry, sanitized error); no retries, 15 s timeout, no raw capture. Ratescalcs only Mon–Fri 08:50–23:00 MSK (service hours); Cbonds at most hourly (one `get_emissions` query, `limit=1`: a rejected login must not hit the provider every 5 min). `-p sources=cbr,ecb`, `-p force=true` for manual runs |
| `maintenance` | 02:00 `backup` (pg_dump, keep 14); 03:00 `retention` (all days) | DB | observability rows (`ops.api_request`, `ops.source_probe`, `ops.worker_heartbeat`) older than `MDHUB_OPS_LOG_RETENTION_DAYS` (90); raw retention of `intraday:reread:` captures (7 days, `MDHUB_RAW_RETENTION_DAYS_REREAD`) and other `intraday:` captures (90 days); intraday market snapshots (30 days, `MDHUB_SNAPSHOT_RETENTION_DAYS_INTRADAY`; static and EOD snapshots are kept forever); AlgoPack rolling window (`MDHUB_ALGOPACK_RETENTION_DAYS`, 90 calendar days): tradestats/FUTOI rows with `trade_date` before the window start (one date per committed delete), their `ops.row_history` images, AlgoPack raw captures (all `algopack_fx_futures` endpoints, incl. `*_history`) fetched before 00:00 MSK of the window start, plus backfill `*_history` captures whose requested range (`till=`) ends before the window start whenever they were fetched; orphaned `running` job runs (>12 h) → failed, VACUUM ANALYZE |
| `snapshot_static` | 07:45 `daily` (`MDHUB_SNAPSHOT_STATIC_TIME`) | DB | static snapshot after `bonds_static` 06:30 and `cbonds_enrich` 07:30 (§11); static changes during the day are published by the next market cut (`change`) |
| `snapshot_market` | 19:45 `eod_preliminary` (D), 08:00 `eod_final` (D-1), every 30 min 10:00–19:00 `intraday` (`MDHUB_SNAPSHOT_*`) | DB | market cuts (§11); three lock classes (`snapshot_market:intraday`, `:eod`, `:rebuild` = explicit `day`/`known_at`) so a manual rebuild never blocks a scheduled cut (0.3.1, review §6.5); EOD rules wait for the lock and are caught up; an intraday cut waits up to half its slot (≤ 10 min), and a cut that still cannot run opens an `ops.data_gap` `snapshot_market_intraday` (key `cut`), closed when the cut is replayed; `eod_final` waits up to `MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES` (30) for the final `bonds_eod` / `zcyc` runs of D (§11 "Input readiness") |
| `snapshot_rebuild` | not scheduled: the backfill job of dataset `snapshot_eod_final` | DB | rebuilds published `eod_final` cuts of `date_from..date_to` (correction only when the content changed); queued by the backfill worker after a backfill that changed inputs of such a day (§11 "Corrections after backfills") |

**Curve gaps (WEB-15, review §6.2).** The per-source curve datasets (`ratescalcs_*`, `otc_monitor_*`) are `event` datasets (freshness only). The curves every market cut requires are also registered dataset-level: `curves_ruonia_ois` and `curves_key_rate_swap` (`md.curve_snapshot`, `business_daily` on MOEX_STOCK from 2026-01-05, max lag 1): a business day without a stored snapshot from **any** source is an `ops.data_gap` (`/v1/gaps`) and makes the dataset stale. No backfill job (the live sources have no history): the gap stays `open` until a snapshot of that day is stored (history import), which closes it as `backfilled`. Dataset `snapshot_eod_final` (`ops.published_snapshot`, `event`, lag 4 days) shows the freshness of the published `eod_final` cuts in `/v1/coverage`.

**Lock contention.** Every job holds advisory lock `mdhub.job:<name>` (a job may split it into
classes by parameters, `JobSpec.lock_key`: `snapshot_market:intraday|eod|rebuild`). A scheduled fire
waits up to 15 minutes for it (polling rules — AlgoPack, Ratescalcs, OTC Monitor, the backfill worker,
source probes — do not wait; intraday snapshot cuts wait up to half their slot and record a missed cut
as a gap, `Rule.missed_gap`). Backfill tasks of heavy datasets are short and yield to a waiting scheduled run. A fire
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

**History depth of fixings (2026-10-17).** Floater and FX-bond coupons go back to 2009 (ДОМ.РФ 15:
refinancing rate + 2.5 % until 2015, key rate since), CPI-linked bonds to 2010, ZCYC-linked to 2016
and FX-denominated coupons to 1998, so the series start at: KEY_RATE 2013-09-17 (introduction),
CBR_REFINANCING_RATE 1992, RUONIA 2010-01-11, CPI_RU_MOM 1991, CBR official FX 1998 (after the
redenomination; 17–59 currencies a day, floor 15), ZCYC 2014-01-06 (first ISS publication), MOEX
indices (RUSFAR, MOEXREPO, RGBI, …) 2014-01-06 (~190 indices a day then, floor 150), MOEX FX fixings
2013-03. Not covered: the two floaters with MOEX benchmark `OTHER` (Ситимат НН, КВС 01 — reference
rate not identified).

`DatasetSpec.history_end` marks a **closed series** (`cbr_refinancing_rate`): the expected latest
date never moves past it, so it is never stale and the gap scan stops at its end.

**AlgoPack rolling window (0423, owner decision "option b").** AlgoPack keeps only 5-minute FO
tradestats and FUTOI of FX futures for the last `MDHUB_ALGOPACK_RETENTION_DAYS` (default 90)
**calendar days**: window = `today − (N − 1)` .. today (MSK; `mdhub.datasets.rolling_window_start`,
e.g. 2026-07-19..2026-10-16). Calendar rather than business days, so that the window does not
depend on calendar data (a failed calendar refresh cannot widen or shrink it) and uses the same
arithmetic as raw retention; 90 calendar days ≈ 62 FORTS business days. Order-book obstats are not
collected (table, history, raw captures and bookkeeping dropped by migration 0423). Generic
mechanism: `DatasetSpec.rolling_window_setting` names the `Settings` field holding N
(`algopack_retention_days` for `algopack_fo_tradestats` and `algopack_futoi`):
- `maintenance mode=retention` deletes rows before the window, their row-history images and the
  AlgoPack raw captures fetched before it (§6 table). History stays enabled on both tables: the
  trigger is `AFTER UPDATE` only, so the deletes write no `ops.row_history` rows.
- The quality gap scan never starts before the window (also with `lookback_days`); `open` gaps
  dated before it are closed as `not_expected` (note "outside the rolling N-day window"; not
  `unfillable` — the source may still have the day — and not `backfilled`); `unfillable` gaps are
  left as they are; `pending` backfill tasks of the dataset that end before the window are
  `cancelled`; queueing never looks before it.
- `algopack_fx_futures date_from=… date_to=…` clamps `date_from` to the window start (warning,
  result `requested_from`) and refuses a range that ends before it (`JobParamError`: run `failed`,
  a backfill task `failed` without retries). After a clamped backfill the worker closes the
  still-missing pre-window gaps as `not_expected`.
- Scheduled polls, the whole-day re-read every `MDHUB_ALGOPACK_FULL_REREAD_MINUTES` and the
  7-day retention of `intraday:reread:*` captures are unchanged.

## 7. Configuration (env)

`MDHUB_DATABASE_URL` (required), `MDHUB_TZ=Europe/Moscow`, `MDHUB_HTTP_PROXY` (optional),
snapshot cuts (§11; owner decision DEC04, defaults): `MDHUB_SNAPSHOT_STATIC_TIME=07:45`,
`MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME=19:45`, `MDHUB_SNAPSHOT_EOD_FINAL_TIME=08:00`,
`MDHUB_SNAPSHOT_INTRADAY_MINUTES=30` (divides 60; 0 = off), `MDHUB_SNAPSHOT_INTRADAY_WINDOW=10:00-19:00`,
`MDHUB_SNAPSHOT_RETENTION_DAYS_INTRADAY=30`, `MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES=30` (`eod_final` input
readiness, 0 = no wait), `MDHUB_SNAPSHOT_AUTOCORRECT=true` (rebuild published `eod_final` cuts after
backfills that changed their inputs),
`MDHUB_ISS_RPS=3`, `MDHUB_RAW_RETENTION_DAYS_INTRADAY=90`, `MDHUB_RAW_RETENTION_DAYS_REREAD=7`
(`intraday:reread:` captures), `MDHUB_ALGOPACK_FULL_REREAD_MINUTES=30`,
`MDHUB_ALGOPACK_RETENTION_DAYS=90` (AlgoPack rolling window, calendar days, §6), `MDHUB_API_TOKENS` (comma list,
read API bearer tokens: `token` → name `env:N`, or `name=token`; §8), `MDHUB_OPS_LOG_RETENTION_DAYS=90`
(API request log, source probes, heartbeats), `MDHUB_ENABLE_SDFI=false`, `MDHUB_BACKUP_DIR=/var/backups/mdhub`
(pg_dump target of `maintenance mode=backup` / `mdhub ops backup`), `MDHUB_API_POOL_SIZE=8`
(read-API connection pool), `MDHUB_LOG_LEVEL=INFO`, `MDHUB_HTTP_TIMEOUT=30`, plus source
credentials: `MOEX_API_KEY`, `MOEX_PASSPORT_LOGIN`, `MOEX_PASSPORT_PASSWORD`,
`RATESCALC_USERNAME`, `RATESCALC_PASSWORD`, `CBONDS_LOGIN`, `CBONDS_PASSWORD`,
`OTC_MONITOR_TOKEN`. Empty values count as unset (`env_ignore_empty`).

## 8. Read API (v1, bearer token)

Unauthenticated: `/health/live`, `/health/ready` (DB reachable, migrations applied with matching
checksums; reports the running `version`, from the git tag via hatch-vcs). Everything under `/v1`
needs `Authorization: Bearer <token>`, including the OpenAPI document `/v1/openapi.json`:

- **Named tokens** (`admin.api_token`, preferred): `mdh_` + base32 of 32 random bytes, created with
  `mdhub tokens create NAME [--scope read|snapshots|export] [--expires-days N]` (the full token is
  printed once; only its SHA-256 and the 8-character prefix are stored), listed with
  `mdhub tokens list`, revoked with `mdhub tokens revoke NAME|ID`. The API looks the SHA-256 up
  (indexed), compares with `hmac.compare_digest` and caches lookups (also misses) for 30 s:
  revocation takes effect within 30 s, expiry immediately. A database error during the lookup →
  503.
- **`MDHUB_API_TOKENS`** keeps working unchanged (all scopes): entries are `token` (shown as
  `env:1`, `env:2`, … by position) or `name=token`. Checked first, against every entry.
- **Scopes:** `read` = all of `/v1`; `snapshots` = `/v1/snapshots*` only (inside replica); `export`
  = `/v1/export/*` only (TDM CSV); `/v1/openapi.json` accepts any scope. Missing scope → 403.
- **Request log:** every request except `/health/*` goes to `ops.api_request` (§4) with the token
  name and id (never the token), client IP (uvicorn `--proxy-headers`), route template, redacted
  query, status, duration, size, request id and user agent; `admin.api_token.last_used_at/_ip`
  follow from the same batch writer.

Endpoints: `/v1/coverage`, `/v1/runs`, `/v1/runs/health`, `/v1/gaps` (ordered by dataset, date,
key), `/v1/bonds` (static, filters), `/v1/bonds/{secid}` (+ issuer, boards, coupons with status,
amortizations, offers, formula resolved + per source, ratings, issuer_ratings), `/v1/bonds/eod?date=` (all boards,
joined yields; `known_at=`), `/v1/bonds/{secid}/history`, `/v1/fx/official`, `/v1/fx/eod`,
`/v1/fx/indicative`, `/v1/fx/ecb`, `/v1/futures/eod`, `/v1/futures/contracts`, `/v1/fixings`
(`known_at=`), `/v1/curves`, `/v1/curves/{dataset}?as_of=`, `/v1/zcyc?date=`, `/v1/indices/eod`,
`/v1/calendar` (`ref.calendar_effective`: source precedence), `/v1/snapshots?kind&cut_type&business_date&latest`,
`/v1/snapshots/{id}/manifest`, `/v1/snapshots/{id}/bundle` (§11), the columnar history endpoints
`/v1/history/index`, `/v1/history/bonds`, `/v1/history/curves/{dataset}`, `/v1/history/zcyc`,
`/v1/history/fixings`, `/v1/history/fx` (below; kinds `ecb` and the `md.fx_cross` kinds since WEB-16), `/v1/export/tdm?date=` (zip of TDM
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

**History endpoints (WEB-15; reference [HISTORY_API.md](HISTORY_API.md)).** `/v1/history/*` (scope `read`) return
history for many instruments per call in a columnar envelope `{dataset, columns, rows, as_of, known_at,
data_version, next, meta}`: rows rendered by PostgreSQL (`json_build_array`, exact numeric digits),
keyset cursors bound to their query (`next` → `cursor=`), at most 50,000 rows per page (default 10,000),
500 SECIDs / 200 fixing codes / 100 FX pairs per call. A weak `ETag` from a cheap version query of the
selection (row counts + latest write per table) and `Last-Modified` (= `data_version`) let a poll end
with `304` before any row is read; gzip on `Accept-Encoding`. `known_at=` reads through the same
as-known-at readers as the snapshot builders (curves: `fetched_at`/`ingested_at` ≤ `known_at`), so a
consumer can reproduce what a cut saw. Bonds use the snapshot BondQuote field names (1.2), main board by
default (`marketprice_board`, else `primary_board`); curves give per (day, source) the end-of-day quote set
of the EOD cuts (`rule=eod`, the same SQL `mdhub.snapshot.market.curve_eod_order`: the latest quote set
with bid/ask whose source clock is on the day at or before `MDHUB_CURVE_EOD_CUTOFF`, 19:00 MSK, else the
latest of the day) or every stored snapshot (`rule=all`); ZCYC the end-of-day
publication (else the latest, labelled); fixings `md.fixing` plus the RUSFAR family with the 1.2 units;
FX CBR official (by effective date), MOEX CETS EOD outrights (exact per-unit values) and the MOEX
indicative rates. Index for the curve EOD rule: migration 0427.

**Curve sources.** `ruonia_ois`, `key_rate_swap` and `cny_ruonia_xccy` have two live sources —
`ratescalcs` (dealer run pages every 30 min, no source clock; XCCY tenors ON–12M) and
`otc_monitor` (every 15 min, source clock `date_update`; XCCY 1W–5Y) — plus `andromeda`
(mid-only EOD history: RUB OIS/KRS from the legacy archive to 2026-09-10; CNY from the owner's
export, `mdhub legacy andromeda-cny`, see below). They are separate series: the hub never
merges or chooses, and
`/v1/curves/{dataset}` returns one snapshot per source (`source=` filters). A consumer that needs
one curve should take, at its cut `T`: (1) `otc_monitor` if its snapshot at `T` has a `source_ts`
on T's Moscow business day and, for OIS/KRS, `quote_basis = bid_ask_mid` (a `mid_only` OIS/KRS
snapshot is the provider's off-hours mark); else (2) `ratescalcs` if its snapshot at `T` was
fetched on that day; else (3) `andromeda` for that day (EOD snapshots carry a synthetic
`fetched_at` of 23:59:59 MSK, so only end-of-day cuts see them; no look-ahead for intraday cuts);
else (4) no value for the day (no forward fill). `cny_rusfar_ois` (CNY OIS vs RUSFAR CNY, 12
tenors 1W–5Y in andromeda, 8 in OTC Monitor): `andromeda` history 2023-12-12 → 2026-10-16 (the
2026-10-16 row is the export's intraday state at 18:05:15 MSK) plus `otc_monitor` live from
2026-10-16. `cny_ruonia_xccy`: `andromeda` history from 2023-07-10 (1W–5Y) extends the
`otc_monitor` history (2026-09-22 →); on the 14 overlapping days the two differ by a median 15 bp
(max 85 bp). The andromeda CNY rows keep quality flags in `meta` (jumps > 500 bp, whole-number or
flat placeholder-like curves, e.g. 2026-03-18; MAPPING.md) — a consumer that takes marks from
them should check `meta.quality_flags` / `meta.quality_note`. `cny_futures_yields` / `usd_futures_yields`
only from `ratescalcs`. The TDM export does not export swap curves (they need bootstrapping).

## 9. Deployment

`deploy/docker-compose.yml`: `postgres:16` (volume, tuned), `mdhub-migrate` (one-shot),
`mdhub-worker` (scheduler), `mdhub-api` (uvicorn behind optional Caddy TLS). `.env` from
`deploy/env.example`. The database must be UTF8 (compose initdb uses `--encoding=UTF8`; bare metal `createdb -E UTF8 -T template0`): `mdhub migrate` and `restore_seed.sh` refuse any other encoding (with `SQL_ASCII` psycopg returns text as bytes). Shipping: `deploy/make_bundle.sh` builds
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

## 11. Published snapshots (Market Snapshot Contract v1)

The hub's dispatch layer (system ADR-002, doc 02 §3/§6; milestone M1). The desk backend reads
only immutable snapshots; the TDM CSV export stays until it does.

**Contract.** JSON Schema and golden fixtures in `schemas/market_snapshot/v1/` (see its
README; `schema_version` `market_snapshot/1.3` since WEB-16: cross-rate kinds `cbr_cross`,
`moex_indicative_cross`, `moex_cets`, `fred_h10` with `source` / `labels`, and the static part since
WEB-18: agency ratings with issuer scope and `grade`, replacement bonds, `issue_size_moex`, document
evidence of curated terms; 1.2 since WEB-15: linker
/ WAP analytics in `source_analytics`, `Fixing.hub_ingested_at`, explicit RUSFAR units): envelope/manifest, static objects (Instrument, CouponPeriod, Redemption, Offer,
FloaterFormula, Issuer, Calendar, IdMap) and market objects (BondQuote EOD/intraday,
CurveQuoteSet, ZcycParams, Fixing, FxRate, CrossRate, CbrMeeting). Decimals are strings with
the stored digits, units in field names, instants UTC. The wheel carries the schemas as
`mdhub/schemas`; every bundle is validated before it is stored (fail closed).

**Identity and storage.** `ops.published_snapshot` (0510). id = sha256 of the canonical bundle
(sorted keys, no whitespace, UTF-8, decimals as strings); `payload_gz` = deterministic gzip of
those bytes. Rows are immutable (trigger: no UPDATE; DELETE only for intraday market rows,
by retention). A cut is (`kind`, `cut_type`, `business_date`, `cut_ts`): rebuilding it with the
same data content (`content_sha256`) publishes nothing (`unchanged`); different content
publishes a correction whose `supersedes` names the current snapshot of the cut (at most one
successor per snapshot; the old one stays readable).

**As known at.** Builders read every table through the as-known-at readers (§4, 0500) at
`known_at`, inside one `REPEATABLE READ` transaction; insert-only tables (`md.curve_snapshot`,
`md.bond_quote_intraday`) filter on `ingested_at`. Rebuilding a cut with the same `known_at`
reproduces it byte for byte, except for rows whose writing transaction was still open at
`known_at` (they carry an earlier `ingested_at`: long `bonds_static` transactions). Revisions of
`md.zcyc_snapshot.is_eod` and same-`ts` revisions of intraday quotes are not versioned.

**Static snapshot.** Universe: bonds `active` as known, plus `matured` with maturity within 31
days before the business date, plus alive bonds that MOEX does not trade (`delisted` with a
maturity on/after the business date, or perpetual; label `not_traded`, WEB-13 HS2). A static linked to an EOD market cut also retains every known
instrument with an EOD price or yield on that cut's business date, read at the same `known_at`.
Older matured and delisted instruments are labelled `retained_for_market_quote`; their
stored status/terms are preserved, not rewritten into a historical claim. Plain daily static
and intraday retain the default universe. Missing reference instruments remain explicitly
excluded; the builder never invents terms. This makes a backfilled EOD a reconstruction from
current knowledge, not a point-in-time archive of terms. Inside the universe nothing is excluded:
`coupon_type='unknown'`, perpetuals, missing day counts and partial formulas carry labels;
floater benchmarks are verbatim with the formula status. Calendars come from
`ref.calendar_effective` only (never `ref.weekend_session`): per calendar the covered range,
holidays, working weekends, `provisional_from` (first `curated_unverified` day), version (latest
row change) and fingerprint; one calendar version + fingerprint per snapshot. Seed scale
(2026-10-16): 4,224 instruments, 104k coupons, 26k redemptions; 42 MB canonical, 2.2 MB gzip;
first publication ~25 s (schema validation dominates).

**Market snapshot.** Every market build first builds the static data as known at the same
instant; `ensure_static` reuses the static snapshot with that content (any `known_at` ≤ the
cut's) or publishes it (`cut_type = change`). The market snapshot names it in
`static_snapshot_id` and quotes only its instruments (`excluded`: `not_in_static_snapshot`).
EOD cuts: bond EOD + yields of D, curve sets fetched on D (latest per source/dataset, labels
`mid_only`, `source_clock_not_on_business_date`), the EOD ZCYC publication, fixings of
[D-400, D+7] (RUSFAR family from `md.index_eod`), CBR official FX in force on [D, D+7], MOEX FX
EOD (RUB outrights; swaps, non-RUB pairs, metals excluded with reasons), indicative 14:00/19:00,
ECB of [D-7, D] plus the `md.fx_cross` kinds of [D-7, D] (1.3), CBR meetings. Proxy curve sets
(`meta.proxy`) carry the label `proxy_<proxy>`; €STR / SOFR / `CPI_RU_YOY` are ordinary fixings. Intraday cuts: latest intraday bond quotes of D, curves, latest
ZCYC, fixings of [D-200, D+7] (floater coupon-period history, TDM HR3; was D-7), FX. Coverage (expected/missing curve series, `required_without_any_source` for
`ruonia_ois`/`key_rate_swap` — stale when a cut has neither source —, latest
fixing dates, universe without quote) and stale reasons are per dataset in the manifest; no
forward fill.

**Schedule (DEC04 open: defaults, configurable, §7).**

| Cut | Default (MSK) | Business date | Why |
|---|---|---|---|
| static `daily` | 07:45 | today | after `bonds_static` 06:30 (~14 min) and `cbonds_enrich` 07:30 |
| market `eod_preliminary` | 19:45 | D = today | after bonds EOD 19:20 (re-checks the ISS history until 19:40; 0.3.1), FORTS 19:25, indices 19:30, ZCYC EOD 19:05, CBR/ECB 18:30 |
| market `eod_final` | 08:00 | D-1 (previous MOEX_STOCK business day) | after the final loads 07:10–07:25 |
| market `intraday` | every 30 min 10:00–19:00 | today | the bonds intraday / ZCYC poll window |

Rules are built from the settings by `mdhub.schedule.snapshot_rules`
(`effective_rules(settings)` is what the scheduler and job health use). `cut_ts` = the nominal
cut: `cut` (default: the configured time of `daily` / `eod_preliminary` / `eod_final`) MSK on
the business date (`eod_final`: on the next business day), whatever the run day, so a later
rebuild of D corrects the same cut; `intraday` / `change` without `cut`: the fire minute.
`known_at` = the run's start (replay: `-p known_at=...`). Manual: `mdhub run snapshot_market -p mode=eod_final -p day=2026-10-15`.

**Preliminary bonds (0.3.1, review §6.4).** ISS publishes the bonds history of D only overnight
(`history/engines/stock/markets/bonds/dates.json` ends at D-1 during the evening; on production the
19:20 `bonds_eod` prelim run wrote 0 rows every day and ended `succeeded`, and the 19:45 cut had no
bonds). The prelim run re-reads the history cursor every `MDHUB_BONDS_EOD_PRELIM_RETRY_MINUTES` (5)
until it has `MDHUB_BONDS_EOD_PRELIM_MIN_ROWS` (1000) rows or `MDHUB_BONDS_EOD_PRELIM_DEADLINE` (19:40)
passes; a business day still short ends `partial` and takes one more marketdata snapshot (with
`LCLOSEPRICE`, stored as `md.bond_quote_intraday.legal_close`, 0435). The `eod_preliminary` cut then
publishes `bond_quotes` from the latest intraday state of D when it has more rows than the history:
coverage `basis = intraday_close` (else `moex_history`), stale reason, fields of the same meaning only
(`legal_close_pct` = LCLOSEPRICE, `close_pct` = LAST, `waprice_pct`, `market_price3_pct` =
MARKETPRICETODAY — never MARKETPRICE, the previous day's — open/low/high, volume, value, trades, MOEX
yields; accrued, face, `market_price2_pct` null). `eod_final` always uses the history.

**Curve EOD rule (0.3.1, review §2.5 item 3).** EOD cuts (and `/v1/history/curves` `rule=eod`) take per
(source, dataset) the latest quote set **with bid/ask** whose source clock (else fetch time) is on D at
or before `MDHUB_CURVE_EOD_CUTOFF` (19:00 MSK); without one, the latest set of D (OTC Monitor's
off-hours mid-only mark, andromeda's end-of-day export) labelled `mid_only`. Before, the latest set of
the day was taken: OTC Monitor's ~18:00–19:00 mid-only set in most `eod_final` cuts. Intraday cuts take
the latest set. The rule and cutoff are in the `curve_quote_sets` coverage (`eod_rule`,
`eod_cutoff_msk`).

**Input readiness (WEB-15, review §6.3).** The scheduled `eod_final` cut of D waits up to
`MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES` (default 30; job parameter `wait_minutes`) until the final
`bonds_eod` and `zcyc` runs of D have loaded (`mdhub.snapshot.readiness`: a `succeeded`/`partial` run
that finished after D ended and covered D - explicit dates, or a final mode whose `days_back` contains
D), re-checking every 30 s. It then builds as known at that moment; an input still missing adds a stale
reason `input not ready: ...` to `bond_quotes` / `zcyc_params` and the run ends `partial` (nothing is
invented; stale reasons are manifest facts, not content). Only the regular cut of the previous business
day is checked; a replay with `known_at` uses the job log up to `known_at`, so it stays deterministic.

**Corrections after backfills (WEB-15, P1-3).** When a backfill task's run changed rows of a job that
feeds the `eod_final` cut of its own date (`bonds_eod`, `zcyc`, `indices_eod`, `fx_moex_eod`,
`fx_indicative`, `cbr_daily`, `ecb_fx`, `rosstat_cpi`) and `MDHUB_SNAPSHOT_AUTOCORRECT` is on, the
backfill worker queues the published `eod_final` cuts of the business dates in the task's range
(dataset `snapshot_eod_final`, job `snapshot_rebuild`, tasks of at most 5 days). `snapshot_rebuild`
rebuilds each current cut with its own `cut_ts` as known now: unchanged content publishes nothing,
changed content publishes a correction that `supersedes` it. Days without a published `eod_final`
are never built automatically. Known limit: windowed inputs (the 400-day fixing window, the CBR FX
forward window, ECB lookback) of *later* cuts are not followed; rebuild those with
`mdhub run snapshot_rebuild -p date_from=... -p date_to=...`.

**Retention.** Static and EOD snapshots are kept forever (daily static ≈ 2.2 MB gzip). Intraday
market snapshots are deleted after `MDHUB_SNAPSHOT_RETENTION_DAYS_INTRADAY` (30) days by
`maintenance mode=retention` (by `cut_ts`; corrections of a cut share its age).

**Endpoints (bearer token).** `GET /v1/snapshots?kind&cut_type&business_date&latest` (newest
first; `latest=true`: only the newest matching snapshot that nothing supersedes; `rows` per
dataset, `superseded_by`), `GET /v1/snapshots/{id}/manifest` (`data` = manifest incl.
`snapshot_id`; `meta`: `published_at`, `superseded_by`, sizes), `GET /v1/snapshots/{id}/bundle`
(`application/gzip`, `ETag: "<id>"`, `If-None-Match` → 304, immutable caching; verify
`sha256(gunzip(body)) == id`).
