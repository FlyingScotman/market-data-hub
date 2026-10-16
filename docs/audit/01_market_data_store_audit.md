# Audit 01 — `market-data-store` (FiveLimaGolf/market-data-store)

Audit date: 2026-10-15. Auditor scope: read-only.

- Repo clone: `scratchpad/ref/market-data-store` at `main` = `ec22738` (2026-10-15 16:28 +0300), package version `0.13.0` (`pyproject.toml:3`). 29 commits; first commit `9fc82e6 "baseline: production state as of 2026-09-11"`.
- DB snapshot: `/Users/badtako/Code/python/market-data-server/data/incoming/md/market-data/market-data.sqlite3` (232 MB, exported 2026-10-15T16:16Z, opened with `mode=ro`). `SNAPSHOT.json` says all rows are preserved but **all raw payload columns were blanked** (`curve_snapshots.raw_payload`, `cbr_liquidity_captures.raw_payload`, `gcurve_snapshots.raw_payload`, `intraday_quote_captures.raw_payload`, `normalized_base_curve_bundles.raw_payload`, `rubik_curve_artifacts.serialized_payload`); hashes kept. Re-parsing from this export is impossible.
- All file:line references below are relative to the repo root unless prefixed.

---

## 0. Executive summary

1. **What it really is.** A single-SQLite, cron-driven *RUB rates* store plus a heavy analytics layer (RUONIA discount bootstrap, key-rate projection, meeting-path MILP, bond Z-spread/DM service, MCP server, Telegram reports). Raw market data is a minority of the code: of ~14k src lines, ~3k are loaders; `fixed_income.py` (2542), `storage.py` (2605, half of which is curve building + migrations), `curves.py`, `key_rate_projection.py`, `meeting_key_rate_path.py`, `curve_worlds.py`, `release_seed.py`, `rubik_archive.py` are analytics/distribution.
2. **Raw data actually captured in production** (DB): RUONIA OIS and KRS (key-rate IRS) curves from 3 sources (Ratescalcs, OTC Monitor, Andromeda archive), XCCY CNY/RUONIA curve (OTC Monitor, **written by code that is not in the repo**), minute-level FX spot + FX futures (USD/EUR/CNY vs RUB only; Si/Eu/CR front-3 + USDRUBF/EURRUBF/CNYRUBF), MOEX G-curve parameters (~15-min), CBR RUONIA / key rate / term RUONIA / meeting calendar / liquidity balance, and a few external base curves (US/DE govt proxies, CFETS FR007, one SPFI RUSFARCNY email).
3. **Bonds are essentially not covered as stored data.** The generic `instruments / price_observations` layer exists, but has **0 rows in production**; bond quotes are fetched on demand (never persisted) by the analytics service; bond reference data lives in a different repo (`moex-market-data-mcp` bond catalog DB).
4. **Repo != production.** The prod DB contains OTC Monitor parser versions 3–5, an `otc_monitor/cnyruonia_xccy` dataset, `mid_only` OTC snapshots and `cfets_chinamoney` `curve_snapshots` — none of which any branch of the repo can produce (repo OTC `PARSER_VERSION = 2`, `src/market_data_store/otc_monitor.py:20`; README says XCCY is ignored, `README.md:142-143`). Conversely the repo has migrations 18–19 (`price_observation_revisions`, `index_fixing_revisions`, `src/market_data_store/storage.py:1565-1566`) that the prod DB has not applied (prod `schema_migrations` stops at 17). Cron wrappers live outside the repo in `~/.hermes/scripts/…`.
5. **Strong reusable patterns:** append-only captures with raw payload + SHA-256 + parser version + separate source/fetch clocks; minute-bucket idempotency keys; immutable tables enforced with SQLite triggers; formal migration runner; fail-closed point-in-time resolvers; Decimal-as-text storage for intraday quotes; explicit `quote_basis` / `latency` / `instrument_type` labelling; strict schema/format validation of every source; CBR liquidity revision model (captures → revisions → current view).
6. **Main weaknesses:** hard-coded `/home/tako/...`, `~/.config/lima/secrets`, `~/.hermes`, `~/.cache/lima` paths; Playwright UI scraping (Andromeda) and undocumented-endpoint scraping (WorldGovernmentBonds POST, Ratescalcs HTML textarea, CBR HTML tables); spoofed User-Agent `"OpenAI File Downloader, XaiImageApiFetch/1.0"`; ingestion coupled to curve building in the same transaction path; 5-minute AlgoPack data polled every minute (~80 % duplicate rows); MOEX history loader drops multi-board rows and creates revision churn; `backfill_requests` queue has no worker; several tests depend on files outside the repo.
7. **Tests:** `uv run pytest -q` (frozen lock, extra `meeting-path`, sibling path deps present): **378 passed, 5 failed in ~24 s**. All 5 failures are environmental: 4 load wrappers from `~/.hermes/scripts/*.py`, 1 expects `.venv/bin/rub-fixed-income-mcp` inside the repo. Without `--extra meeting-path` collection aborts (matplotlib missing).

---

## 1. Data sources and loaders

Common plumbing used by most CLI commands (`src/market_data_store/cli.py`):

- One SQLite file, default `<repo>/data/market-data.sqlite3` (`cli.py:40-41`), overridable with `--database`.
- Process lock `flock` on `<db>.lock`, 30 s wait (`cli.py:49-70`); every ingest wraps the DB write in it.
- `connect_database` (`storage.py:1570-1580`): `chmod 0600`, `foreign_keys=ON`, `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=30000`, then applies all pending migrations on *every* open (`migrations.py:29-66`, one `BEGIN IMMEDIATE` per version, records `schema_migrations` + `PRAGMA user_version`).
- Secret env var names: `RATESCALC_BASE_URL`, `RATESCALC_USERNAME`, `RATESCALC_PASSWORD` (`cli.py:181-183`, `249-251`), `MOEX_APIM_KEY` (`cli.py:541`, see note under 1.9). Read-side env: `MARKET_DATA_DATABASE` (`http_api.py:233`), `MARKET_DATA_STORE_DB`, `BOND_CATALOG_DB` (`fixed_income_server.py:187`, `194`). Script-only: `CALYPSO_LOGIN`, `CALYPSO_PASSWORD` (`scripts/calculate_qr_fixed_ofz_spreads_windows.py:136-137`).
- Secret files (all expected `0600`): `~/.config/lima/secrets/ratescalc.env` (sourced by `scripts/cron_ingest_ratescalcs.sh:6-11`), `~/.config/lima/secrets/otc-monitor-token` (`cli.py:46`; checked for mode 0600 and parent dir without group/world bits, `otc_monitor.py:190-200`), `~/.config/lima/secrets/moex-api-key` (AlgoPack/APIM Bearer token, `intraday_fx.py:219`, mode check `intraday_fx.py:355-363`), `~/.config/lima/secrets/moex-realtime.json` (MOEX Passport `{"login","password"}`, `intraday_fx.py:221`; schema enforced in sibling `moex-live-bonds/moex_live_bonds/realtime.py:14,34-40`), `~/.config/lima/secrets/andromeda/storage-state.json` (Playwright cookies, `cli.py:43`; README wrongly says `~/.hermes/secrets/andromeda/...`, `README.md:251-252`).
- Network egress: per tests, the production hermes wrappers route MOEX HTTPS through a local proxy `HTTPS_PROXY=http://127.0.0.1:1080` (`tests/test_cron_scripts.py:79-88`); `httpx[socks]` is a dependency (`pyproject.toml:8`). The wrapper code itself is not in the repo.
- MOEX APIM TLS uses a bundled Russian Trusted Root CA (`src/market_data_store/certs/russian_trusted_root_ca.pem`, loaded at `intraday_fx.py:220`, `365-370`).

### Observed schedules (from `fetched_at_utc` in the DB, plus README)

| Job | Cadence observed in DB | Where defined |
|---|---|---|
| Ratescalcs curves | every 2 h at 07,09,11,13,15:00 UTC (10–18 MSK), weekdays | `scripts/cron_ingest_ratescalcs.sh` (writes prod **and** dev DB), scheduler config outside repo (README mentions 10/14/18 MSK, `README.md:266-270` — outdated) |
| OTC Monitor curves | every 15 min 07:00–16:45 UTC | `scripts/cron_ingest_otc_monitor.sh` (prod + dev DB) |
| Intraday FX/futures | every minute, `* 3-20 * * 1-5` UTC (06:00–23:59 MSK) | `~/.hermes/scripts/run_intraday_fx_ingest.py` (not in repo), `README.md:212-216` |
| MOEX G-curve | ~4×/hour 07–15 UTC (avg 34 snapshots/trade date) | `scripts/cron_ingest_gcurve.sh`; hermes wrapper `~/.hermes/scripts/run_gcurve_ingest.py` (not in repo) |
| CBR fixings + liquidity | daily ~06:00 UTC (09:00 MSK) | `scripts/run_liquidity_report.py:20-26` (via hermes job `README.md:246-249`) and separate daily fixings job (`README.md:435-436`) |
| CBR meetings | 1st of month | `README.md:435-436` |
| Andromeda archive | manual (last fetch 2026-09-10) | `cli.py:326-360` |
| Free base curves | ~daily (106 bundles 2026-08-28..10-15) | `cli.py:139-153` |
| SPFI e-mail RUSFARCNY | manual, 1 import | `cli.py:156-176` |

### 1.1 Ratescalcs — RUONIA OIS & KRS curves (intraday snapshots, no source clock)

- Code: `src/market_data_store/ratescalcs.py`, `ingest.py:26-47`, CLI `ingest-ratescalcs` (`cli.py:179-206`).
- Fetch: `GET https://ratescalcs.ru/cnyfuturesyields/key-rate-editor/ois` and `/cnyfuturesyields/key-rate-editor/krs` (`ratescalcs.py:122-125`); host pinned to `ratescalcs.ru`, https only, no redirects (`ratescalcs.py:137-144`, `181`).
- Auth: HTTP Basic from `RATESCALC_USERNAME` / `RATESCALC_PASSWORD`; `RATESCALC_BASE_URL` must equal `https://ratescalcs.ru`.
- Parsing: HTML `<textarea name="content">`, lines `TENOR BID ASK` (`ratescalcs.py:40-43`, `56-116`); validates `0 ≤ bid ≤ ask ≤ 100`, no duplicate tenors, ≥2 quotes; mid computed `(bid+ask)/2`.
- Retries: 3 attempts, exponential 0.25/0.5 s on transport errors and 5xx; content-type must be `text/html` (`ratescalcs.py:150-175`). No rate limiting (2 requests per run).
- Idempotency: capture key `ratescalcs:<UTC minute>` (`ingest.py:33-35`) → `ingestion_runs UNIQUE(source_id, capture_key)`; repeat within the same minute returns `inserted=false` (`storage.py:1724-1737`). No content dedup across minutes: identical quotes produce a new snapshot every run.
- Raw payload: textarea content stored in `curve_snapshots.raw_payload` + SHA-256 (`storage.py:1758-1774`).
- Clock: source publishes no timestamp → `source_observed_at_utc = NULL`; history axis is `fetched_at_utc` (`README.md:131-134`).
- Corrections: none (each fetch is a new snapshot).
- Coupling: after storing, the same call builds RUONIA discount curves and up to 25 key-rate projections (`ingest.py:45-46`).

### 1.2 Ratescalcs — USD/RUB cash spot (intraday, minute)

- Code: `intraday_fx.py:144-216` (`RatescalcsFxClient`), parser `intraday_fx.py:549-605`.
- Fetch: `GET https://ratescalcs.ru/cnyfuturesyields/snapshot` (JSON), same Basic auth env vars (`cli.py:248-252`).
- Parses only `timestamp`, `usd_source`, `usd_spot_settle`, `usd_results[0].spot_bid/spot_offer`; rejects sources containing `future`/`rubf` (i.e. provider-side USDRUBF fallback) as not cash spot (`intraday_fx.py:562-567`). Stored as `USD/RUB_TOM`, `instrument_type='spot'`, `latency='derived'`, `source_variant='<source>_snapshot_clock'` (currently `ntpro`).
- Retries: 2 retries with 1 s, 3 s delays on transport errors and 502/503/504; failure raises `RatescalcsSpotUnavailable` (optional source → skipped) (`intraday_fx.py:184-202`).
- Whatever else the snapshot JSON contains (endpoint name suggests CNY futures yields) is kept only in the raw payload, not parsed.
- Data quality: prod DB has 1 row with `source_variant='usdrubf_fallback_snapshot_clock'` stored as `spot` (2026-08-28, pre-guard). The as-of resolver filters only by source/instrument/type (`intraday_fx.py:1061-1079`), so that row is still selectable.

### 1.3 MOEX Passport realtime — CNY/RUB CETS spot (intraday, minute, true realtime)

- Code: `intraday_fx.py:225-336` (`MoexPassportFxClient`, `parse_passport_cny_snapshot`), transport from sibling package `moex-live-bonds` (`moex_live_bonds/realtime.py:261-282`).
- Fetch: `GET https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities/CNYRUB_TOM.json?iss.only=securities,marketdata,dataversion&iss.meta=off`.
- Auth: MOEX Passport login/password from `~/.config/lima/secrets/moex-realtime.json` → one-time HTTP Basic, then session cookie `MicexPassportCert` required (`realtime.py:105-116`, `190-216`). Host pinned to `iss.moex.com`.
- Validation: exactly one row per block, SECID/BOARD must be `CNYRUB_TOM`/`CETS`, session dates must agree, `SETTLEDATE` taken from security row (never synthesized), `SYSTIME` (MSK) must be ≤ 90 s old and not in the future (`intraday_fx.py:222`, `304-309`), bid ≤ offer. Stores bid/offer/last + `SEQNUM`.
- Retries (in `moex-live-bonds`): on 429/5xx with linear backoff (`realtime.py:187-213`).
- Optional: any failure → `PassportRealtimeUnavailable` → source skipped for that minute (`intraday_fx.py:246-251`, `1319-1320`).

### 1.4 MOEX APIM / AlgoPack — CNY spot mid + FX futures (intraday, 5-min data polled every minute)

- Code: `intraday_fx.py:339-536` (`MoexIntradayFxClient`), parsers `parse_algopack_fx_mid` (`662-706`), `parse_algopack_rfud_snapshot` (`874-951`), contract selection `_select_rfud_securities` (`616-649`).
- Base URL `https://apim.moex.com/iss`, Bearer token from file `~/.config/lima/secrets/moex-api-key`, custom CA bundle (`intraday_fx.py:340-370`, `424-435`).
- Requests per run (`intraday_fx.py:436-483`):
  1. `GET /engines/currency/markets/selt/boards/CETS/securities/CNYRUB_TOM.json?iss.meta=off&iss.only=securities,marketdata,dataversion` (only for `SETTLEDATE` and session date);
  2. `GET /datashop/algopack/fx/obstats/CNYRUB_TOM.json?latest=1&iss.meta=off` → `mid_price`;
  3. `GET /engines/futures/markets/forts/boards/RFUD/securities.json?iss.meta=off&iss.only=securities` (static spec);
  4. for each selected contract: `GET /datashop/algopack/fo/obstats/{SECID}.json?latest=1&iss.meta=off`.
- Contract universe (`intraday_fx.py:609-613`): families `USD/RUB` ASSETCODE `SI` + perpetual `USDRUBF`; `EUR/RUB` `EU` + `EURRUBF`; `CNY/RUB` `CNY`/`CR` + `CNYRUBF`. First 3 dated contracts with `LASTTRADEDATE ≥ session_date`, dynamically.
- Normalization: futures price → RUB per 1 unit FX = `raw × STEPPRICE / MINSTEP / LOTVOLUME` (`intraday_fx.py:766-771`). Only `mid_price` is stored (in `last_text`; bid/offer NULL), `latency='derived'`, `source_variant='algopack_obstats_mid'`. Interval timestamp = `tradedate tradetime` (MSK); `SYSTIME` must be ≥ interval and ≤ capture start, otherwise `AlgoPackPublicationClockRace` → whole multi-source fetch retried once (`intraday_fx.py:1311-1327`).
- Retries: 4 retries with 0.25/0.5/1/2 s on 429/5xx; transport errors retried with 0.5 s; HTML response (subscriber gate) retried then rejected (`intraday_fx.py:391-421`).
- History: on 2026-08-28 only (parser `moex-fx-v1`, 377 captures) realtime bid/offer/last from APIM `marketdata` was stored (`parse_moex_rfud_snapshot`, `intraday_fx.py:782-871`, `parse_moex_cny_spot_snapshot` `709-763`); since then AlgoPack mid only (commit `2255c06 "source FX futures from AlgoPack"`). The realtime parsers are still in code but unused by `fetch`.
- Duplication: AlgoPack intervals are 5-minute but captured every minute — 397,793 rows for 78,158 distinct (instrument, interval) pairs (~80 % duplicates). Each capture is a distinct minute key so nothing dedups.

Generic intraday storage (`intraday_fx.py:1189-1299`, `1302-1350`): one capture per (source, minute) with key `<source>:<YYYY-MM-DDTHH:MMZ>`; replay of identical payload is a no-op, a different payload for the same key raises `IntradayCaptureConflict`. Raw payload (for APIM: JSON object containing all 4+N raw responses) + SHA-256 + parser version; each observation row has `raw_row_sha256`. Tables are immutable via triggers.

Point-in-time FX reference (`intraday_fx.py:971-1132`): fixed priority per pair — USD/RUB: Ratescalcs spot → `USDRUBF` proxy; CNY/RUB: Passport realtime → AlgoPack spot mid → `CNYRUBF` proxy; EUR/RUB: `EURRUBF` proxy only. Requires source and fetch clocks ≤ decision time, max age caller-supplied, labelled `quality` + warnings.

### 1.5 OTC Monitor — RUONIA OIS & KRS (intraday, 15 min, source clock)

- Code: `otc_monitor.py`, `ingest.py:50-74`, CLI `ingest-otc-monitor` (`cli.py:209-236`).
- Fetch: `GET https://otc-monitor.ru/api/bond/` with `Authorization: Bearer <token-file>` (`otc_monitor.py:17-19`, `202-236`); redirects rejected; JSON content-type required.
- Parsing (`otc_monitor.py:78-161`): rows with `series` ending `" OIS RUONIA"` (1W..10Y, 17 tenors) or `" IRS KeyRate"` (3M..10Y, 13 tenors) (`otc_monitor.py:22-58`); exact tenor set required; bid/offer Decimal, `0 ≤ bid ≤ offer ≤ 100`; mid recomputed (provider `mid` ignored); `date_update` (MSK) per row must be within 5 s across a curve; curve older than 3 h or >5 s in future rejected. Bond/XCCY/FX rows ignored *in repo code*.
- Retries: 3 attempts, 0.25/0.5 s on transport errors and 5xx.
- Idempotency: `otc_monitor:<UTC minute>` capture key (`ingest.py:61-63`). Raw payload = whole JSON list (stored once per curve, i.e. duplicated 2×).
- Prod divergence: DB has OTC parser versions 1–5, `mid_only` OTC snapshots for both curves since 2026-09-21/22, and dataset `cnyruonia_xccy` ("XCCY CNYRUONIA", `mid_only`, 14 tenors 1W..5Y, 473 snapshots 2026-09-22..10-15). This code is not in any repo branch.

### 1.6 Andromeda Terminal archive — RUONIA OIS & KRS history (EOD, mid-only, Playwright)

- Code: `andromeda.py`, `storage.py:1583-1697`, CLI `ingest-andromeda-archive` (`cli.py:326-360`).
- Fetch: headless Chromium via Playwright, start URL `https://andromedaterminal.ru/MM?s=rates&g=ruonia-ois&p=ruonia-ois-rates&c=%2Ccol10` (`andromeda.py:200-202`), clicks UI labels "Ставки", "RUONIA OIS", "IRS на КС", dismisses cookie/onboarding/video overlays (`andromeda.py:226-235`, `244-276`), scrolls the virtualized table until row count is stable 3 rounds (max 50 loads) (`andromeda.py:63-108`).
- Auth: saved Playwright storage state (cookies) file, mode 0600 (`andromeda.py:216-219`); expiry detected by `/auth/` redirect. Session lock + "pause" file (`andromeda.py:174-193`, defaults `cli.py:44-45`).
- Parsing: header `Дата` + tenor columns; `-` = missing; `NN,NN%` rates (`andromeda.py:124-171`).
- Storage: per (curve, date) snapshot with capture key `andromeda:<curve>:<date>:<payload_sha[:16]>` (`storage.py:1637-1640`); `quote_basis='mid_only'`, bid=ask=mid; `source_observed_at_utc=<date>T00:00:00Z`. A changed value for an old date creates an additional snapshot without explicit supersede link.
- Prod coverage: RUONIA OIS 2022-07-06..2026-09-10 (1018 snapshots), KRS 2022-10-24..2026-09-10 (970). Not run since 2026-09-10.

### 1.7 CBR (Bank of Russia) — fixings, calendar, liquidity, official FX (EOD)

- Code: `cbr.py`, storage `storage.py:2331-2572`, CLI `cli.py:363-518`.
- HTTP: base `https://www.cbr.ru`, UA `curl/8.0.0`, 3 attempts with 0.5/1 s backoff on any transport/HTTP error, ≥0.2 s between requests (`cbr.py:222-277`).
- Endpoints (`cbr.py:285-356`), params `UniDbQuery.Posted=True&UniDbQuery.From=dd.mm.yyyy&UniDbQuery.To=dd.mm.yyyy`:
  - Key rate: `GET /hd_base/KeyRate/` → `index_fixings(index_code='key_rate')`.
  - RUONIA: `GET /hd_base/ruonia/dynamics/` → `index_fixings(index_code='ruonia')`.
  - Term RUONIA (index + 1M/3M/6M): `GET /Queries/UniDbQuery/DownloadExcel/125022?Posted=True&From=..&To=..&FromDate=mm/dd/yyyy&ToDate=..` (XLSX via openpyxl) → `term_ruonia_fixings`.
  - Liquidity balance: `GET /hd_base/bliquidity/` (15-column HTML table) → `cbr_liquidity_*`.
  - Meeting calendar: `GET /dkp/cal_mp/` (HTML `div.main-events_day`, Russian month names, extraordinary meetings skipped) → `policy_meetings`; effective date = next Monday (`cbr.py:176-178`).
  - Official FX: `GET /scripts/XML_daily.asp?date_req=dd/mm/yyyy`, one CharCode per call, uses `VunitRate` or `Value/Nominal` (`cbr.py:181-219`) → `price_observations` (basis `CBR_OFFICIAL_RATE_RUB_PER_UNIT`, timestamp = Moscow midnight).
- Windows: fixings re-fetch the last 5 years on every run (`cli.py:364-365`); liquidity the last 366 days (`cli.py:424-425`); CBR FX one pair × one date, fully manual (`cli.py:471-518`).
- Raw payload: stored **only** for liquidity (`cbr_liquidity_captures`, dedup on `(payload_sha256, parser_version, capture_kind)`); not stored for fixings, term RUONIA, meetings; CBR FX stores only SHA-256 of the XML.
- Corrections:
  - Liquidity: full revision model — row hash per date, new `cbr_liquidity_balance_revisions` row with `revision_number+1` and `supersedes_revision_id`, every capture linked to the revision it observed, current view `cbr_liquidity_balances` (`storage.py:2459-2572`). Prod: 15 dates have revision 2.
  - Key rate / RUONIA: current table overwritten; repo migration 19 adds `index_fixing_revisions` log (`storage.py:1497-1519`, `2358-2364`) — **not applied in prod DB**, so prod has lost correction history.
  - Term RUONIA, meetings: overwrite in place, no history (`storage.py:2369-2444`).
- Fragility: HTML table parsers silently skip unparseable rows (`cbr.py:74-87`, `103-119`), so a layout change yields "0 changed" instead of an error.

### 1.8 MOEX ISS G-curve (OFZ zero-coupon yield curve, KBD) — intraday ~15 min + EOD

- Code: `gcurve.py`, CLI `ingest-gcurve` (`cli.py:279-323`).
- Fetch: `GET https://iss.moex.com/iss/engines/stock/zcyc.json?iss.meta=off&iss.only=params,yearyields&date=YYYY-MM-DD` (`gcurve.py:342-353`), no auth (public ISS). Without `--trade-date` walks back up to 10 days to the latest published date (`gcurve.py:377-395`).
- Parsing: one `params` row (B1,B2,B3,T1,G1..G9 + tradedate/tradetime MSK) + `yearyields` rows; params/yields tradetime must match (else `GCurveClockSkewError`, retried after 5 s up to 3 attempts); published standard yields must be reproduced from parameters within <0.01 bp (`gcurve.py:108-165`); requested vs returned trade date must match.
- Storage: `gcurve_snapshots` keyed by `snapshot_uid = sha256(source, trade_date, source_observed_at, payload_sha256, parser_version)` → identical payload is idempotent (`gcurve.py:137-147`, `168-206`). Raw canonical JSON stored. Also builds `gcurve_zero_curve_builds/_nodes` (derived) in the same transaction.
- Prod: 1167 snapshots, 34 trade dates 2026-08-28..2026-10-15.

### 1.9 MOEX bond prices (designed, effectively unused)

- `backfill-moex-prices` (`cli.py:581-628`, `backfill.py`, `moex_prices.py:153-240`): `GET https://iss.moex.com/iss/history/engines/stock/markets/bonds/securities/{SECID}.json?from&till&start&iss.meta=off&history.columns=BOARDID,TRADEDATE,SECID,SHORTNAME,LEGALCLOSEPRICE,CLOSE,WAPRICE,MARKETPRICE3,CURRENCYID,TRADINGSESSION`. Max 366 days, max 100 pages; 3 attempts, 0.5 s·2^n backoff, ≥0.2 s between requests. Each of `LEGALCLOSEPRICE, CLOSE, WAPRICE, MARKETPRICE3` becomes one observation (`moex_prices.py:21`, `83-101`). One SECID per manual invocation; no universe loop, no yields, no accrued interest.
- `ingest-moex-current` (`cli.py:530-578`, `moex_prices.py:243-288`): `GET https://iss.moex.com/iss/engines/stock/markets/bonds/securities/{SECID}.json?iss.only=marketdata&marketdata.columns=SECID,BID,OFFER,LAST,SYSTIME,CURRENCYID` through MOEX Passport. Requires env `MOEX_APIM_KEY` but the token is **only checked for non-emptiness and never used** (`moex_prices.py:256-262`) — vestigial.
- Storage: `price_observations` (append-only revisions, `prices.py:41-158`). **Prod row count: 0** (also `instruments`, `instrument_identifiers`, `backfill_requests` = 0).
- Bugs in this path:
  - `BOARDID` is fetched but not part of the observation key; the history endpoint is not board-scoped, so if a bond trades on several boards the second board's row for the same date/basis carries the same page hash and is silently skipped (`prices.py:114-132`) → order-dependent board choice.
  - `raw_provenance_sha256` is the hash of the whole *page*, and revision detection compares hashes, not values (`prices.py:128-133`). Re-fetching with a different range/page split creates a new "revision" of every unchanged value.
  - Source timestamp for history = `TRADEDATE T00:00:00Z` (`moex_prices.py:95`), while CBR uses Moscow midnight (`cbr.py:209-210`) — inconsistent.
- Live bond quotes used by analytics (`fixed_income.py:383-595`, `MoexBondQuoteProvider`): Passport `board_snapshot(board)` = `GET /iss/engines/stock/markets/{bonds|ndm}/boards/{BOARD}/securities.json?iss.only=securities,marketdata`, `security_snapshot`, and unauthenticated history `GET /iss/history/engines/stock/markets/bonds/boards/{BOARD}/securities/{SECID}.json` (5000-row cap, keeps the max `TRADINGSESSION` row per date). Clean price = bid/offer mid else `LEGALCLOSEPRICE → CLOSE → WAPRICE → MARKETPRICE3 → LAST`. **Nothing is persisted.** Bond reference/cash flows come from `../moex-market-data-mcp/data/bond_catalog.sqlite3` (`fixed_income_server.py:192-197`), default board `TQCB` if missing (`fixed_income.py:661`).

### 1.10 External base curves (EOD, approximate) and imports

- `ingest-free-base-curves` (`free_base_curves.py:350-388`, `cli.py:139-153`):
  - WorldGovernmentBonds: `GET https://www.worldgovernmentbonds.com/country/{united-states|germany}/` then **`POST /wp-json/country/v1/main`** with a hand-built `jsGlobalVars` body (`free_base_curves.py:309-347`) — undocumented WordPress endpoint; US/DE govt zero curves labelled as proxies for SOFR/€STR OIS.
  - CFETS/ChinaMoney: `GET https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/Ifcc?lang=EN&cfgItemType=72` → FR007 IRS closing, approximate annual-pay bootstrap (`free_base_curves.py:156-248`).
  - Result is written as a JSON manifest under `<repo>/data/free-base-curves-latest.json` and imported via the bundle importer.
- `import-spfi-email-curve` (`free_base_curves.py:65-155`): parses a forwarded MOEX SPFI e-mail text file for `OIS RUSFARCNY` bid/ask (1W..5Y), stores **mid** only into `cny_onshore_base_v1` after an approximate bootstrap. Bid/ask survive only inside the bundle raw payload.
- `import-base-curves` (`base_curve_bundles.py:178+`): validated `normalized-base-curve-bundle.v1` JSON (template `docs/base-curve-bundle.example.json`), immutable bundles/snapshots/nodes as canonical decimal text.
- Rubik / QR (proprietary desk system on a Windows desk): `scripts/export_rubik_curves_windows.py` + `scripts/import_rubik_bundle.py` → `rubik_curve_artifacts/_nodes` (`rubik_archive.py`). Prod rows: 0. `scripts/calculate_qr_fixed_ofz_spreads_windows.py` hard-codes `C:\Users\atkachenko\...` (line 25) and Calypso env credentials.

### 1.11 Read/serve side (for context)

- FastAPI on `127.0.0.1:8765` (`http_api.py:229-238`): `/healthz`, `/v1/instruments/resolve`, `/v1/prices/{id}/current`, `/v1/prices/{id}/eod`, `/v1/fx/{date}`, `POST /v1/backfills`, `/v1/backfills/{id}`, `/v1/coverage/{code}`, `/v1/curve-worlds/{code}/resolve` (`http_api.py:81-202`). Status mapping exact=200 / missing=404 / stale=409 / pending_backfill=202 / source_failure=503 (`README.md:401-404`). `POST /v1/backfills` only inserts a `pending` row (`price_service.py:231-246`); **no worker ever processes it**.
- MCP stdio server `rub-fixed-income-mcp` with 8 tools (`fixed_income_server.py`).
- Telegram reports: `report-swaps` (`swaps_report.py`, `scripts/report_swaps.py`), `report-liquidity` (`liquidity_report.py`, `scripts/run_liquidity_report.py`).

---

## 2. Database schema walk-through

Schema version in prod: migrations 1–17 applied (2026-08-11..2026-09-23); `PRAGMA user_version` reads 0 in the export (probably reset by the export). Repo defines 19 (`storage.py:1522-1567`).

Legend — **R** = raw/normalized market data (hub candidate), **D** = derived analytics (keep outside hub), **M** = metadata/governance.

### 2.1 Generic provenance layer

| Table | Kind | Purpose / key | Written by | Prod rows |
|---|---|---|---|---|
| `sources` | M | provider registry, `code` UNIQUE, `base_url` | `store_capture`, `store_archive_curves`, `store_price_observations` (upsert) | 4: ratescalcs, andromeda, cfets_chinamoney, otc_monitor |
| `datasets` | M | `(source_id, code)` UNIQUE, `kind='curve'` | same | 9 (see §3) |
| `ingestion_runs` | M | one row per capture, `(source_id, capture_key)` UNIQUE, `status IN ('completed')` only | curve + price loaders (not intraday, gcurve, CBR) | 2871 |
| `schema_migrations` | M | migration log | `migrations.py` | 17 |

Note: only some loaders use `ingestion_runs`; intraday, G-curve, CBR and base-curve loaders have their own capture tables. There is no failed-run log at all (status only `completed`).

### 2.2 Swap/rate curve quotes

| Table | Kind | Purpose / key | Written by |
|---|---|---|---|
| `curve_snapshots` | R | one snapshot per (run, dataset); `raw_payload`, `payload_sha256`, `parser_version`, `quote_basis ('bid_ask_mid'|'mid_only')`, `fetched_at_utc`, `source_observed_at_utc`, `published_at_utc`, `snapshot_uid` (unique, canonical hash), `correction_state ('original'|'corrected'|'superseded')`, `supersedes_snapshot_uid` | Ratescalcs, OTC Monitor, Andromeda (+ prod-only CFETS) |
| `curve_quotes` | R | `(snapshot_id, tenor)` UNIQUE; `position`, `tenor_months`, `bid_pct`, `ask_pct`, `mid_pct`, per-quote `quote_basis` (REAL floats) | same |

`correction_state`/`supersedes_snapshot_uid`/`published_at_utc` are never set by any writer (schema only).

### 2.3 Derived curves (analytics)

| Table | Kind | Purpose | Written by |
|---|---|---|---|
| `derived_curve_builds` | D | stripped curves per input snapshot: `curve_code ∈ {ruonia_discount, key_rate_projection}`, `model_version`, valuation/effective dates, fit error, `build_uid`/`build_sha256`, `economic_role` | `build_missing_ruonia_curves` (`storage.py:1893`), `build_missing_key_rate_projections` (`storage.py:2050`), called from every swap ingest |
| `derived_curve_nodes` | D | DF + zero rate per node | same |
| `derived_projection_nodes` | D | key-rate forward path nodes | same |
| `derived_curve_dependencies` | D | KRS build → RUONIA discount build | same |
| `projection_curve_build_meta` | D (immutable) | rank, nullity, condition number, calendar/convention ids | same |
| `projection_curve_scenarios` / `derived_projection_scenario_nodes` | D (immutable) | all-bid / mid / all-ask scenarios | same |
| `gcurve_zero_curve_builds` / `gcurve_zero_curve_nodes` | D (immutable) | zero/DF nodes from G-curve params (text) | `store_gcurve_snapshot` |
| `curve_world_definitions` | M | named fail-closed policies (11 rows) | migrations 3/11/13/16 |
| `curve_world_instances` | M | point-in-time resolution records (0 rows) | `curve_worlds.py` |
| `curve_source_policies` | M | licensing/distribution approval per source+dataset (0 rows) | manual |
| `rubik_curve_artifacts` / `rubik_curve_nodes` | D/R (immutable) | proprietary desk curves (0 rows) | `rubik_archive.py` |
| `normalized_base_curve_bundles` / `_snapshots` / `_nodes` | D (immutable) | external base curves already bootstrapped to DF/zero (USD, EUR, CNY) | `base_curve_bundles.py` |

### 2.4 Official indices and CBR data

| Table | Kind | Key | Notes |
|---|---|---|---|
| `index_fixings` | R | `(index_code, fixing_date)` | `ruonia`, `key_rate`; current value only (overwritten) |
| `index_fixing_revisions` | R | `(index_code, fixing_date, fetched_at_utc)` | repo migration 19; **absent in prod** |
| `term_ruonia_fixings` | R | `fixing_date` | index value + 1M/3M/6M; overwritten |
| `policy_meetings` | R/M | `meeting_date` | `effective_date` (= next Monday), `is_estimated`, `source_code` |
| `cbr_liquidity_captures` | R | id; UNIQUE(payload_sha256, parser_version, capture_kind) | raw HTML; `legacy_normalized` synthetic capture for pre-raw rows |
| `cbr_liquidity_balance_revisions` | R | UNIQUE(observed_on, revision_number), (observed_on,row_sha256), (capture_id,observed_on) | 14 balance columns, `methodology_version` switch at 2023-11-01 |
| `cbr_liquidity_capture_observations` | R | (capture_id, observed_on) → revision_id | which revision each capture saw |
| `cbr_liquidity_balances` | R (view-like) | `observed_on` | current state + `revision_id`, `latest_capture_id` |

### 2.5 Instruments and prices (generic, unused in prod)

| Table | Kind | Key | Notes |
|---|---|---|---|
| `instruments` | R | `canonical_code` UNIQUE | `kind`, `display_name` overwritten on upsert |
| `instrument_identifiers` | R | `(scheme, value)` UNIQUE | schemes used: `moex_secid`, `fx_pair` |
| `price_observations` | R | UNIQUE(instrument, source, observed_on, basis, raw_provenance_sha256) | REAL `value`, `currency`, `basis` (e.g. `CLOSE`, `BID:realtime`, `CBR_OFFICIAL_RATE_RUB_PER_UNIT`); repo adds `revision_number`, `supersedes_observation_id` (migration 18; not in prod schema) |
| `backfill_requests` | M | id = sha256(code:start:end)[:36] | queue without consumer |

### 2.6 Intraday FX / futures

| Table | Kind | Key | Notes |
|---|---|---|---|
| `intraday_quote_captures` | R (immutable) | UNIQUE(source_code, capture_key) | `minute_bucket_utc`, `fetched_at_utc`, max `source_observed_at_utc`, `status ∈ {available, unavailable, gap}` (only `available` ever written), `parser_version`, raw payload + SHA |
| `intraday_quote_observations` | R (immutable) | UNIQUE(capture_id, instrument_code) | `instrument_code`, `pair_code`, `instrument_type ∈ {spot, perpetual_future, dated_future}`, `source_variant`, base/quote ccy, `engine/market/board`, `session_date`, source/fetch clocks, `value_date`, `fixing_date` (never populated), `last_trade_date`, `expiry_date`, `bid_text/offer_text/last_text` (Decimal text), `quote_unit`, `latency ∈ {realtime, delayed, derived}`, `sequence_id`, `raw_row_sha256`. Indexes for as-of by pair and by instrument. |

### 2.7 MOEX G-curve

| Table | Kind | Key | Notes |
|---|---|---|---|
| `gcurve_snapshots` | R (immutable) | `snapshot_uid` UNIQUE | trade_date, source/fetch clocks (stored with `+00:00`, unlike the `Z` used elsewhere), parser/model version, raw JSON, `parameters_json` (13 floats), `standard_yields_json`, reconciliation error < 0.01 bp (CHECK) |

### 2.8 Raw vs derived split for the hub

- **Hub-worthy (raw + normalized):** `curve_snapshots` + `curve_quotes` (as "rate quote snapshots"), `intraday_quote_*`, `gcurve_snapshots` (published parameters + standard yields are official data), `index_fixings` (+ revisions), `term_ruonia_fixings`, `policy_meetings`, `cbr_liquidity_*`, `instruments`/`identifiers`/`price_observations` (redesigned), plus source/dataset/run metadata and `curve_source_policies` (licensing).
- **Analytics (keep outside the hub):** all `derived_*`, `projection_*`, `gcurve_zero_curve_*`, `normalized_base_curve_*` (already bootstrapped — the hub should store the *input quotes* instead), `rubik_*`, `curve_world_*`.

---

## 3. Datasets and instruments present

### 3.1 In code

| Area | Instruments / datasets |
|---|---|
| Swap curves | `ruonia_ois` (RUONIA OIS), `key_rate_swap` (KRS / IRS on key rate) from `ratescalcs`, `otc_monitor`, `andromeda` |
| FX spot | `USD/RUB_TOM` (Ratescalcs/NTPro), `CNYRUB_TOM` CETS (Passport realtime; AlgoPack mid). Also mentioned but not ingested: `USD000UTSTOM` AlgoPack (`docs/currency-market-data-plan.md`) |
| FX futures | Si, Eu, CR (CNY) front-3 dated contracts on FORTS/RFUD, perpetual `USDRUBF`, `EURRUBF`, `CNYRUBF` |
| Fixings | CBR `ruonia`, `key_rate`, term RUONIA (index, 1M, 3M, 6M), CBR meeting calendar, CBR liquidity balance (14 metrics), CBR official FX (any CharCode, manual) |
| Bonds | any SECID (manual backfill / current) into `price_observations`; live board snapshots in analytics only |
| Govt curve | MOEX G-curve (KBD) parameters + standard tenors |
| External curves | US govt zero (proxy SOFR), DE govt zero (proxy €STR), CFETS FR007 IRS closing, SPFI RUSFARCNY OIS (e-mail), Rubik (desk) |

### 3.2 In the prod DB (SQL, `mode=ro`)

Curve snapshots (`curve_snapshots ⋈ datasets ⋈ sources`; dates = `coalesce(source_observed_at_utc, fetched_at_utc)`):

| Source | Dataset | Basis | Snapshots | Obs. dates (min → max) | Distinct days | Tenors |
|---|---|---|---|---|---|---|
| andromeda | ruonia_ois | mid_only | 1018 | 2022-07-06 → 2026-09-10 | 1017 | archive columns |
| andromeda | key_rate_swap | mid_only | 970 | 2022-10-24 → 2026-09-10 | 969 | archive columns |
| ratescalcs | ruonia_ois | bid_ask_mid | 257 | 2026-08-05 → 2026-10-15 | 50 | page-defined |
| ratescalcs | key_rate_swap | bid_ask_mid | 257 | 2026-08-05 → 2026-10-15 | 50 | page-defined |
| otc_monitor | ruonia_ois | bid_ask_mid (p1–5) / mid_only (p4–5) | 441 / 183 | 2026-09-04 → 2026-10-15 | 24 / 15 | 1W..10Y (17) |
| otc_monitor | key_rate_swap | bid_ask_mid / mid_only | 458 / 166 | 2026-09-04 → 2026-10-15 | 27 / 16 | 3M..10Y (13) |
| otc_monitor | cnyruonia_xccy | mid_only (p5) | 473 | 2026-09-22 → 2026-10-15 | 19 | 1W,2W,1M,2M,3M,4M,5M,6M,9M,1Y,2Y,3Y,4Y,5Y |
| cfets_chinamoney | cfets_fr007_irs_closing | bid_ask_mid | 1 | 2026-08-29 | 1 | 1M..10Y (11) |
| cfets_chinamoney | cfets_fdr007_irs_closing | bid_ask_mid | 1 | 2026-08-29 | 1 | 1M..10Y (11) |

Intraday (`intraday_quote_observations ⋈ captures`), all session dates 2026-08-28 → 2026-10-15:

| Source / variant | Instrument(s) | Type | Rows | Days | Fields |
|---|---|---|---|---|---|
| moex_passport_realtime / passport_cets_realtime | CNYRUB_TOM (CETS) | spot, realtime | 13,149 | 28 (from 2026-09-07) | bid, offer, last |
| moex_apim / algopack_obstats_mid | CNYRUB_TOM (CETS) | spot, derived | 31,037 | 34 | mid (in last) |
| ratescalcs / ntpro_snapshot_clock | USD/RUB_TOM | spot, derived | 19,528 | 34 | bid, offer |
| ratescalcs / usdrubf_fallback_snapshot_clock | USD/RUB_TOM | "spot" (mislabelled) | 1 | 1 | bid, offer |
| moex_apim / algopack_obstats_mid | USDRUBF, EURRUBF, CNYRUBF | perpetual_future | ~30,657 each | 39 | mid |
| moex_apim / algopack_obstats_mid | SiZ6, SiH7, EuZ6, EuH7, CRZ6, CRH7 | dated_future | ~30,656 each | 39 | mid |
| moex_apim / algopack_obstats_mid | SiU6, EuU6, CRU6 (exp. 2026-09-17) | dated_future | ~11.6–11.9k | 17 | mid |
| moex_apim / algopack_obstats_mid | SiM7, EuM7, CRM7 (exp. 2027-06-17) | dated_future | ~18.5k | 23 (from 2026-09-17) | mid |
| moex_apim / moex_apim (realtime marketdata) | 12 of the above | dated/perpetual | 377 each | 1 (2026-08-28) | bid, offer, last |

Captures: moex_apim 31,037 (v1 377 + v2 30,660), moex_passport_realtime 13,149, ratescalcs 19,529. Total observations 434,995; AlgoPack duplicates: 397,793 rows vs 78,158 distinct (instrument, interval).

Official / other:

| Table | Rows | Range |
|---|---|---|
| index_fixings `key_rate` | 1324 | 2021-08-02 → 2026-10-15 |
| index_fixings `ruonia` | 1283 | 2021-08-02 → 2026-10-13 |
| term_ruonia_fixings | 1900 | 2021-08-02 → 2026-10-14 |
| policy_meetings (cbr, not estimated) | 64 | 2020-02-07 → 2027-12-17 |
| cbr_liquidity_balances | 311 | 2025-08-06 → 2026-10-14 |
| cbr_liquidity_balance_revisions | 326 (15 with rev 2) | same |
| cbr_liquidity_captures | 49 (1 legacy + 48 HTML) | fetched 2026-08-07 → 2026-10-15 |
| gcurve_snapshots | 1167 | trade dates 2026-08-28 → 2026-10-15 (34 days) |
| normalized_base_curve_snapshots | 319 | free: USD/EUR/CNY-FR007 106 each, 2026-08-28 → 2026-10-15; SPFI RUSFARCNY 1 (2026-08-28) |
| derived_curve_builds | 4514 | ruonia_discount v1 1001 / v2 1718, key_rate_projection v1 126 / v2 1669 (2022-07-06 → 2026-10-15) |
| instruments / price_observations / backfill_requests / rubik_* / curve_world_instances / curve_source_policies | 0 | — |

---

## 4. Quality assessment

### 4.1 Good — reuse in the hub

| Pattern | Where | Why reuse |
|---|---|---|
| Capture = raw payload + SHA-256 + parser version + `fetched_at` + `source_observed_at` (+ `published_at`) | `curve_snapshots`, `intraday_quote_captures`, `cbr_liquidity_captures`, `gcurve_snapshots` | lets you re-parse and audit; separates "when known" from "when true" |
| Deterministic capture keys `<source>:<UTC minute>` + conflict on same key/different content | `ingest.py:33-35`, `intraday_fx.py:1215-1230`, `1332-1344` | cheap idempotent cron retries |
| Immutability via `BEFORE UPDATE/DELETE` triggers | intraday, gcurve, base curve, rubik, projection tables (see SCHEMA.sql) | append-only guarantees in SQLite |
| Revision model: capture → semantic revision (row hash, `revision_number`, `supersedes_*`) → current view | `storage.py:2459-2572` | the right model for official data with corrections (CBR, MOEX EOD) |
| Formal migration runner with per-version transaction | `migrations.py:29-66` | simple, adequate (would be replaced by Alembic/Postgres in a service) |
| Strict parsers that fail closed on format drift (tenor sets, bid ≤ ask, clock skew, reconciliation checks) | `ratescalcs.py:56-116`, `otc_monitor.py:78-161`, `gcurve.py:108-165`, `intraday_fx.py:267-336` | catches silent source changes |
| Decimal-as-text for quotes; recomputing mid from bid/ask instead of trusting provider mid | `intraday_fx.py:128-133`, `otc_monitor.py:117-121` | precision + consistency |
| Explicit quality labels: `quote_basis`, `latency`, `instrument_type`, `source_variant`, "proxy" vs "spot" | `intraday_fx.py:20-21`, `971-1023` | consumers can tell realtime vs delayed vs derived |
| Point-in-time as-of queries filtered by both source and fetch clocks + max age | `intraday_fx.py:1034-1132`, `gcurve.py:261-315`, `curve_worlds.py` | prevents look-ahead |
| Dynamic futures front-contract selection from RFUD static + price normalization formula | `intraday_fx.py:609-649`, `766-771` | directly reusable for Si/Eu/CR/... |
| Source-specific knowledge: Passport cookie auth, APIM Bearer + Russian CA, CETS `SETTLEDATE` from security row, AlgoPack clock race, G-curve parametric formula & reconciliation, CBR endpoints & params | as cited in §1 | hard-won integration details |
| Licensing governance table `curve_source_policies` + release seed that refuses `pending` | `storage.py:734+`, `release_seed.py`, `docs/release_policy.example.json` | MOEX Passport/AlgoPack/SPFI redistribution rights are real constraints |
| Secret-file permission checks | `otc_monitor.py:190-200`, `intraday_fx.py:355-363`, `andromeda.py:216-219` | good hygiene |
| Injectable transports/clocks/sleep for tests | most clients | testable without network |

### 4.2 Fragile / bad — do not copy

1. **Repo is not the production source of truth.** Prod writes OTC parser v3–v5, XCCY CNY/RUONIA and CFETS `curve_snapshots`, while repo stops at OTC parser v2; prod lacks repo migrations 18–19. The working copy on the server (`/home/tako/lima-workspace/projects/market-data-store`) evidently has uncommitted changes. Any reuse must be checked against what runs there.
2. **Hard-coded host paths:** `/home/tako/lima-workspace/projects/market-data-store` and `/home/tako/.config/lima/secrets/...` in all cron scripts (`scripts/cron_ingest_*.sh:5-6`), `Path.home()/.config/lima/secrets/*` (`cli.py:43-46`, `intraday_fx.py:219-221`, `moex-live-bonds realtime.py:14`), `~/.cache/lima/...` (`cli.py:42-45`, `scripts/report_swaps.py:11-13`), `~/.hermes/scripts/*` (README, tests), `/home/tako/lima-workspace/private-sources/...` (`scripts/search_evraz_andromeda.py:6`), `C:\Users\atkachenko\...` (`scripts/calculate_qr_fixed_ofz_spreads_windows.py:25`), default DB inside the repo (`cli.py:41`). Path deps on sibling checkouts `../moex-live-bonds`, `../rub-fixed-income-core` (`pyproject.toml:24-26`) and `../moex-market-data-mcp/data/bond_catalog.sqlite3`.
3. **Scheduling outside the repo.** The minute FX job and G-curve job wrappers live only in `~/.hermes/scripts`; repo cron scripts write to prod **and** dev DB sequentially (`cron_ingest_ratescalcs.sh:13-19`), so a dev failure fails the job after prod succeeded.
4. **Scraping:**
   - Andromeda via Playwright clicking Russian UI labels and scrolling a virtual table (`andromeda.py:244-276`) — breaks on any UI change, needs a browser, cookie-based auth that expires.
   - WorldGovernmentBonds undocumented `POST /wp-json/country/v1/main` with reverse-engineered JS globals (`free_base_curves.py:309-347`) — contradicts README's "GET only" claim.
   - Ratescalcs HTML textarea, CBR HTML tables and meeting-calendar CSS classes (`cbr.py:151-173`).
   - CBR table parsers skip bad rows silently (no fail-closed) (`cbr.py:74-87`, `103-119`).
5. **Spoofed User-Agent** `"OpenAI File Downloader, XaiImageApiFetch/1.0"` for MOEX APIM, Ratescalcs JSON, G-curve, OTC Monitor, WGB, CFETS (`intraday_fx.py:141`, `gcurve.py:334`, `352`, `otc_monitor.py:19`, `free_base_curves.py:14`, `moex_prices.py:277`); `curl/8.0.0` for CBR (`cbr.py:265`). Use an honest UA.
6. **Ingestion coupled to analytics:** every swap ingest builds RUONIA/KRS curves (`ingest.py:45-46`, `72-73`, `93-94`); G-curve ingest builds zero curves in the same transaction (`gcurve.py:200`); `storage.py` imports `rub_fixed_income` (`storage.py:13-18`) and contains curve bootstraps, a hard-coded Russian holiday list (`storage.py:1871-1886`) and an SPFI calendar projection. A bootstrap bug can break raw capture.
7. **Over-polling / duplication:** 5-minute AlgoPack OBStats captured every minute → ~80 % duplicate rows; Ratescalcs/OTC store identical snapshots repeatedly (no content-change dedup); OTC raw payload stored twice per capture; CBR fixings re-download 5 years daily, liquidity 1 year daily.
8. **Bond price layer defects** (§1.9): board not in key, page-hash-based revisions, unused `MOEX_APIM_KEY`, single-SECID manual CLI, no yields/accrued, 0 rows in prod; `backfill_requests` without worker.
9. **Data-model inconsistencies:** REAL floats for curve quotes and price observations vs Decimal text for intraday/gcurve; timestamp strings mix `Z` and `+00:00` (gcurve) and are compared as text in some queries (`gcurve.py:279-287`); EOD source timestamps use UTC midnight in some loaders and MSK midnight in others; `ingestion_runs.status` cannot record failures; `intraday_quote_captures.status` `unavailable`/`gap` never written, so outages are invisible; `correction_state` columns never used.
10. **Mislabelled data already stored:** one `usdrubf_fallback` row stored as USD/RUB spot; Andromeda mid-only quotes stored with bid=ask=mid (`storage.py:1676-1684`) — consumers must check `quote_basis`.
11. **Narrow FX policy hard-coded** for exactly 3 pairs (`intraday_fx.py:971-1023`, `609-613`); adding a pair means code changes in several places.
12. **Repo hygiene:** research one-offs (`scripts/research_evraz.py`, `search_evraz_andromeda.py`, `plot_*`, `calculate_*`, `analyze_*`) and Windows desk scripts mixed with production code; README partly Russian/partly English and partially stale (schedules, andromeda state path, XCCY).
13. **Licensing risk** acknowledged in docs: Passport realtime, AlgoPack, SPFI are separate MOEX licences; storage ≠ redistribution right (`README.md:202-205`, `docs/currency-market-data-plan.md`). The hub must carry this forward (per-source distribution policy).

### 4.3 Tests

- Command: `UV_PROJECT_ENVIRONMENT=<scratch venv> uv run --frozen --extra meeting-path pytest -q -p no:cacheprovider` (sibling repos present in `scratchpad/ref`, so path deps resolved). Repo left unmodified.
- Result: **383 tests, 378 passed, 5 failed, 1 warning, ~24 s.**
  - `tests/test_cron_scripts.py::test_gcurve_hermes_wrapper_routes_moex_https_through_local_egress` — loads `~/.hermes/scripts/run_gcurve_ingest.py` (not in repo).
  - `tests/test_intraday_fx_cron_wrapper.py` (3 tests) — load `~/.hermes/scripts/run_intraday_fx_ingest.py` (`tests/test_intraday_fx_cron_wrapper.py:10`); also read `/proc/<pid>/stat` (Linux-only).
  - `tests/test_fixed_income_server.py::test_stdio_server_lists_all_tools` — expects `<repo>/.venv/bin/rub-fixed-income-mcp`.
  - Without `--extra meeting-path`, collection aborts on `tests/test_meeting_key_rate_path.py` (imports matplotlib).
  - DeprecationWarning: `free_base_curves.py:276` parses `"%d %b"` without year (breaks in Python 3.15 / on Feb 29).
- Distribution (test functions): analytics-heavy — `test_fixed_income_service.py` 68, `test_intraday_fx.py` 36, `test_quote_freshness.py` 19, `test_storage.py` 16, `test_gcurve.py` 15, `test_otc_monitor.py` 13; loaders thin — `test_ratescalcs_parser.py` 3, `test_cbr.py` 7, `test_moex_prices.py` 6, `test_free_base_curves.py` 3, `test_price_ingestion.py` 2, `test_http_price_api.py` 2, `test_andromeda_archive.py` 6 (parser/scroll fakes only). Fixtures are tiny synthetic samples (6–33 lines; `tests/fixtures/`), no recorded real payloads for AlgoPack/Passport/G-curve. No live/contract tests; nothing covers the multi-board history issue or CBR layout drift.
- Lint/type config: ruff (E,F,I,B,UP), mypy `strict` (`pyproject.toml:550-559`) — not run here.

---

## 5. Gaps vs the target hub universe

Status legend: **Covered** = stored continuously with adequate fields; **Partial** = some instruments/fields/history or manual only; **Not covered**.

| Target | Status | What exists, source | Gap |
|---|---|---|---|
| All MOEX Russian bonds — EOD prices | **Not covered** (code partial) | `backfill-moex-prices` single SECID via ISS history (`moex_prices.py:216-240`); 0 rows in prod. Analytics fetch history on demand, not stored (`fixed_income.py:521-591`). Universe/reference in separate `moex-market-data-mcp` repo. | Need full-universe daily loader over all boards (TQOB, TQCB, TQIR, TQOD/TQOE/TQOY, TQUD, …, plus NDM/RPS/PSAU), board in key, fields: CLOSE/LEGALCLOSEPRICE/WAPRICE/MARKETPRICE2/3/ADMITTEDQUOTE, BID/OFFER close, VALUE/VOLUME/NUMTRADES, ACCINT, FACEVALUE, CURRENCYID |
| All MOEX bonds — EOD yields | **Not covered** | No YIELD/YIELDCLOSE/DURATION fields requested anywhere; yields only computed in analytics | Add `history/.../yields` or history YIELDCLOSE/YIELDATWAP/DURATION; G-curve covers only the OFZ zero curve |
| All MOEX bonds — intraday prices/yields | **Not covered** | Passport `board_snapshot` exists (`moex_live_bonds/realtime.py:284-302`) and is used live by analytics, never persisted | Need periodic board snapshots (marketdata BID/OFFER/LAST/YIELD/WAPRICE/SYSTIME/SEQNUM) into an intraday table like `intraday_quote_observations` but bond-shaped |
| OFZ zero-coupon curve (G-curve) | **Covered** | MOEX ISS `zcyc` ~15 min, params + standard yields, 2026-08-28 → | short history; backfill possible from ISS by date |
| FX vs RUB — USD/RUB | **Partial** | Ratescalcs NTPro spot bid/offer (derived, provider clock), AlgoPack `USDRUBF` proxy; CBR official rate code exists but unused | No MOEX CETS `USD000UTSTOM` (trading suspended on MOEX since 2024 — OTC/NTPro only); no CBR daily rates stored |
| FX vs RUB — CNY/RUB | **Covered** (intraday) | Passport realtime CETS `CNYRUB_TOM` bid/offer/last (from 2026-09-07), AlgoPack 5-min mid | no CNYRUB_TOD/SPT, no EOD close/WAP/volume, no CBR rate stored |
| FX vs RUB — EUR/RUB | **Partial** | only `EURRUBF` perpetual proxy | no spot (EUR_RUB__TOM unavailable on MOEX; need OTC source), no CBR rate stored |
| FX vs RUB — all other pairs (HKD, KZT, BYN, TRY, AMD, AED, INR, UZS, KGS, TJS, GLD/SLV, …) | **Not covered** | CBR XML_daily parser can read any CharCode (`cbr.py:181-219`) but is manual, 1 pair × 1 date, 0 rows | Need full CBR daily set + MOEX CETS for each traded `*RUB_TOM` |
| G7 crosses EURUSD, GBPUSD, USDJPY, USDCAD, USDCHF, AUDUSD, (NZDUSD, EURGBP…) | **Not covered** | nothing (MOEX `EURUSD000TOM` not referenced; `ED` futures not ingested) | need an external source (MOEX CETS crosses where traded, ED/other futures, or a licensed/free FX feed; CBR cross rates are derivable but not market quotes) |
| MOEX FX futures — dated Si, Eu, CR | **Partial** | AlgoPack FO OBStats 5-min `mid_price` only, first 3 contracts per family, normalized to RUB/unit; realtime bid/offer/last only 2026-08-28 | no bid/offer/last/volume/OI, no settlement/clearing prices, no EOD history, no contracts beyond front 3; other FX futures (ED, UCNY/UCNH, GBPU, UJPY, UCHF, UTRY, AUDU, Kazakh/HKD etc.) not covered |
| MOEX perpetual FX futures USDRUBF, EURRUBF, CNYRUBF | **Partial** | AlgoPack 5-min mid | no bid/offer, no funding rate (FUSR mentioned as missing input in `docs/currency-market-data-plan.md`), no settlement; other perpetuals (e.g. GLDRUBF, others) not covered |
| IRS vs RUONIA (OIS) | **Covered** | OTC Monitor bid/offer 1W–10Y every 15 min (2026-09-04 →), Ratescalcs bid/ask every 2 h (2026-08-05 →), Andromeda mid daily history (2022-07-06 → 2026-09-10, manual) | no source-of-record EOD fixing/close; three sources with different tenor sets/clocks must stay separate; Andromeda via Playwright |
| IRS vs key rate (KRS) | **Covered** | same three sources (OTC 3M–10Y; Andromeda 2022-10-24 →) | same remarks |
| XCCY vs CNY (CNY/RUB XCS vs RUONIA) | **Partial** | prod-only OTC Monitor `cnyruonia_xccy` mid-only 1W–5Y (2026-09-22 →) — code not in repo; README says XCCY ignored; MOEX SPFI `CNY-XCS-CNYRUO` / `CNY-XCS-CNRQTE` identified but not entitled (`docs/currency-market-data-plan.md`) | recover the prod parser; bid/offer and conventions undocumented; no history before 2026-09-22 |
| OIS CNY vs RUSFAR CNY | **Partial (one point)** | single manual SPFI e-mail import (2026-08-28), stored only as bootstrapped mid curve in `cny_onshore_base_v1`; bid/ask only inside raw bundle | needs SPFI Extended Curves entitlement (`CNY-OIS-RUSFCN`, `CNY-OIS-RFCQTE`) or another quote source; store raw bid/ask quotes, not just the bootstrapped curve |
| (context) RUSFAR / RUSFARCNY / MosPrime / CBR FX fixings / MOEX FX fixings (USDFIXME, CNYFIXME) | **Not covered** | only RUONIA, key rate, term RUONIA | add as index fixings with revisions |
| (context) CNY onshore FR007 / FDR007 IRS | **Partial** | CFETS FR007 closing daily (approximate bundle, 106 days), 1 prod-only raw FDR007/FR007 snapshot | store raw closing quotes daily |

---

## 6. Recommendations for the new hub (derived from the audit)

1. Treat this repo as a **source-knowledge library**, not a code base to fork: port the clients/parsers for Ratescalcs, OTC Monitor, MOEX Passport, MOEX APIM/AlgoPack, ISS (zcyc, history), CBR — each behind a uniform `fetch() -> RawCapture(payload, sha, parser_version, clocks)` + `parse(raw) -> normalized rows` contract (the intraday `FetchedIntradayQuotes` shape is the best template).
2. Keep the capture → revision → current-view model generically for every EOD dataset (CBR liquidity is the reference implementation); add `failed`/`gap` capture states so outages are visible.
3. Separate ingestion from analytics completely: no curve building in ingest paths; the hub publishes quotes/fixings, analytics services subscribe.
4. Pull the prod-only OTC Monitor parser (v3–v5, XCCY, mid-only handling) and the hermes wrappers (proxy egress, timeouts, retry-on-lock) from the server before decommissioning; they are not in GitHub.
5. Fix polling cadence to source granularity (AlgoPack every 5 min aligned to interval publication; dedup on (instrument, interval)); dedup unchanged swap snapshots or at least flag them.
6. Configuration via env/secret manager (no `~/.config/lima`, `~/.hermes`, `/home/tako`), honest User-Agent, explicit egress proxy setting, per-source licence/distribution policy carried as data.
7. Build the bond universe loader (all boards, EOD + intraday + yields) and FX universe (all CETS `*_TOM`, CBR daily set, external G7 source, all FORTS FX futures incl. bid/offer/OI/settlement) — the main gaps.
