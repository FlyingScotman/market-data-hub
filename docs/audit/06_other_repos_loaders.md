# 06 — Loader survey of the other FiveLimaGolf repos (excluding market-data-store and moex-market-data-mcp)

Survey date: 2026-10-15. Read-only. Clones are in `scratchpad/ref/`. All file paths are relative to each repo root.

**Scope: 26 repos**

The 16 requested repos:
- cbonds-research-connector, corporate-bond-monitor, corporate-bond-trade-monitor
- currency-bond-arbitrage-monitor, currency-bond-monitor
- implied-key-rates, market-snapshot-reporter, moex-bond-turnover, moex-live-bonds
- ofz-analytics, ofz-market-figures-reporter, ofz-rates-app, ofz-swaps-v2, ofz-tracker
- rgbi-futures-arbitrage-monitor, usdrub-proxy-research

10 more repos, cloned from `gh flg repo list FiveLimaGolf` because their names suggest data loading or a data contract:
- **rub-fixed-income-core**
- **andromeda-research-connector**
- **russian-rating-agencies-mcp**
- **russia-inflation-nowcast**
- **russian-real-money-positioning**
- **bank-form101-positioning**
- **cbr-policy-language**
- **primary-bond-pricing-reports**
- **corporate-bond-relative-value-study**
- **trading-tracker**

I skipped repos that are clearly unrelated: travelpayouts-flight-mcp, aeroflot-ticket-monitor, neovim-config, a desk-library wrapper, agent-*, workspace-hygiene, tax-optimization-model, bb-orchestration-migration.

Secrets: only environment-variable names and credential-file *paths* are listed. No values were read or reproduced.

---

## 0. Executive summary

1. **Most loader logic is already centralised in three sibling packages, and the surveyed apps consume them.**
   - `moex-live-bonds` provides the MOEX Passport-authenticated ISS transport.
   - `market-data-store` provides curves, fixings, intraday FX and curve worlds.
   - `moex-market-data-mcp` provides `bond_catalog.sqlite3` and the CBR client.

   The other repos mostly *consume* those three packages. Several of them hard-code relative paths into the sibling SQLite files (`../market-data-store/data/market-data.sqlite3`, `../moex-market-data-mcp/data/bond_catalog.sqlite3`). The hub should replace those file-level couplings with an API.
2. **No repo contains a loader for dated FX futures (Si/Eu/CR)** and none touches `/iss/engines/futures/.../forts` for FX. The only perpetual-FX-futures loader is **USDRUBF** in `usdrub-proxy-research/research.py`, which uses the AlgoPack tradestats endpoint for the `fo` market. Nobody loads EURRUBF or CNYRUBF; `trading-tracker` only *parses* CNYRUBF positions from broker reports. The FX-futures *pricing and risk model* already exists in `rub-fixed-income-core/rub_fixed_income/fx_futures.py`.
3. **CNY coverage is thin:**
   - CNYRUB_TOM spot is loaded in `market-snapshot-reporter`, `moex-live-bonds` and `usdrub-proxy-research`.
   - The RUSFARCNY index is loaded in `market-snapshot-reporter`.
   - CNY-face bond tapes and snapshots are loaded in `currency-bond-arbitrage-monitor` and `currency-bond-monitor`.
   - CNY curves (FR007 IRS and RUSFAR-CNY OIS) are only *consumed*, as market-data-store curve worlds, in `currency-bond-monitor`.
   - **Nothing loads CNY repo, CNY OIS quotes or xccy CNY.**
4. **Swap curves:**
   - The only real fetcher is the **Ratescalcs** scraper in `ofz-swaps-v2/ofz_workbench/ratescalcs.py`. It loads RUONIA OIS and KRS (IRS vs key rate).
   - `ofz-rates-app` reads OTC-Monitor and Andromeda swap snapshots out of market-data-store.
   - `rgbi-futures-arbitrage-monitor` exports desk-system RUONIA and RUB_KEY_RATE curves (desk-only, Windows).
   - SPFI `/iss/sdfi/curves.json` was probed: public access returns metadata only.
5. **G7 FX crosses** exist only via unofficial Yahoo chart API in `market-snapshot-reporter/src/market_snapshot_reporter/sources.py` (lines 296-320). USD/CNH ticks come from Dukascopy in `usdrub-proxy-research`.
6. **MOEX trading calendar: no repo fetches one.** The best *consumer-side* designs are:
   - `rgbi-futures-arbitrage-monitor/src/rgbi_monitor/calendars.py`: `OfficialCalendar` built from a hash-verified snapshot, plus `DateRole` and `ContractDateRoles`.
   - `rub-fixed-income-core/rub_fixed_income/dates.py:BusinessCalendar`: explicit holidays and working days, versioned.

   `rub-fixed-income-core/docs/BACKLOG.md` (P0 #1) explicitly assigns the official MOEX/CBR calendar to the data project. Everything else is weekday-only, a 2026-only hard-coded list, or the rule-based `implied_key_rates/moscow.py`. That last one is copied into three repos, approximates the perenos (holiday transfers), and wrongly adds Radunitsa.
7. **Strong reusable infrastructure:**
   - `moex-live-bonds/moex_live_bonds/realtime.py`: Passport cookie auth, retries, host allowlist, Russian Trusted Root CA.
   - `moex-bond-turnover/src/moex_bond_turnover/client.py`: cursor-first parallel ISS history paging plus a disk cache.
   - `russian-rating-agencies-mcp/src/rating_agencies_mcp/limits.py`: cross-process SQLite daily-quota and requests-per-second limiter.
   - `russia-inflation-nowcast/src/inflation_nowcast/download.py`: vintage-preserving conditional-GET downloader with TLS pinning.
   - `corporate-bond-monitor/src/corp_monitor/quik_session.py`: process-isolated QUIK calls.
   - `russian-real-money-positioning/scripts/research/moex_q4_cashflow_probe.py`: ISS cursor pagination with integrity gates.
8. **No repo has WebSocket/STOMP code.** ISS+ WebSocket `MXSEBB.orderbooks` is mentioned only as a need (`corporate-bond-monitor/docs/handoffs/2026-08-10-project-restart.md:56`). There is no async httpx anywhere; all clients are sync httpx, requests or urllib.

---

## 1. Per-repo findings

### 1.1 moex-live-bonds (v0.2.1): shared realtime MOEX transport

**Purpose.** Shared package used by about 10 apps.
- `MoexPassportClient` (`moex_live_bonds/realtime.py`) is authenticated `iss.moex.com`. It sends Basic auth once, then reuses the `MicexPassportCert` cookie. It covers bond marketdata, board snapshots, board-wide trade tapes, per-security trades, order books and the CETS currency snapshot.
- Legacy `MoexLiveBondsClient` (`client.py`) is `apim.moex.com` with Bearer auth and returns a normalised pandas DataFrame (`schema.py:BondSnapshot`).

**Endpoints.** Base is `https://iss.moex.com/iss`, host-restricted.

| Method | Path |
|---|---|
| `marketdata` | `/engines/stock/markets/bonds/boards/{BOARD}/securities/{SECID}.json?iss.only=marketdata` |
| `security_snapshot` | same path with `iss.only=securities,marketdata` |
| `currency_snapshot` | `/engines/currency/markets/selt/boards/{CETS}/securities/{CNYRUB_TOM}.json?iss.only=securities,marketdata,dataversion` (realtime.py:261-282) |
| `board_snapshot` | `/engines/stock/markets/{bonds\|ndm}/boards/{BOARD}/securities.json?iss.only=securities,marketdata`. Deliberately a single request: the comment at realtime.py:291-293 notes that live board endpoints ignore `start`/`limit`. |
| `orderbook` | `.../securities/{SECID}/orderbook.json?limit=1..100` |
| `recent_trades` | `.../trades.json?reversed=1` |
| `board_trades` | `/engines/stock/markets/{market}/boards/{BOARD}/trades.json?start=&limit<=5000` (returns `dataversion`) |

The legacy APIM client (`constants.py:6`, `https://apim.moex.com/iss`) has:
- `resolve_isins` via `/securities.json?engine=stock&market=bonds&q=`, in ISIN chunks of 20;
- `fetch_all_boards_snapshot` over TQOB/TQCB/TQIR/TQOD/TQOE, which skips boards returning 403/404.

**Auth.**
- Environment variable `MOEX_API_KEY`, also read from `./.env` or `../.env`.
- Credential files (JSON `{login,password}`, mode 0600 enforced): `<secrets-dir>/moex-realtime.json` and `<secrets-dir>/moex-bond-orderbook.json`.
- Bundled `certs/russian_trusted_root_ca.pem`.

**Instruments.** Bonds (OFZ, corporate, replacement and NDM boards), trades, order books, CNY/RUB spot.

**Techniques worth reusing.**
- Cookie bootstrap in `get_json` (L183-222): retries on 429/5xx with linear backoff, `max_attempts=4`, content-type check, verification that the cookie is present, error messages without bodies.
- `_SAFE_ID` regex for SECID/BOARDID.
- Automatic bonds/ndm market routing.
- `dataversion` used for freshness.
- `MoexPassportHttpClient` façade, which lets legacy httpx-style callers switch transport.

**Quality.**
- Tests are offline (respx and MockTransport).
- The legacy APIM client leaks up to 200 characters of the response body into errors (`client.py:95`) and uses pandas `iterrows`.
- The User-Agent is spoofed as "OpenAI File Downloader, XaiImageApiFetch/1.0" (`realtime.py:13`). The same string is copied into about 6 repos and should be replaced in the hub.

### 1.2 corporate-bond-trade-monitor (v0.6.22)

**Purpose.** Production minute-cron monitor of the full TQCB realtime trade tape.
- Prices each trade as a Z-spread to the versioned RUONIA curve, and floaters as a spread to the key rate.
- Drives a per-issuer spread watchlist and Telegram alerts through a SQLite outbox.
- The curve, analytics and catalogue are consumed from market-data-store and moex-market-data-mcp. It has no loaders of its own beyond the two below.

**Endpoints.**
- `production.py:MoexReferenceProvider.current_board` (L452-514): public `/engines/stock/markets/bonds/boards/{board}/securities.json?iss.only=securities` for ACCRUEDINT, FACEVALUEONSETTLEDATE and SETTLEDATE.
- `production.py:ApimBoardTradeSource.fetch_page` (L1121-1213): `board_trades` through `MoexPassportClient`. Despite the class name, the transport is Passport; `_APIM_BASE` at L30 is dead.

**Auth.**
- `CBTM_PREVIOUS_DELIVERY`, `CBTM_PREVIOUS_EXECUTION_ID`, `CBTM_CURRENT_EXECUTION_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PROXY`.
- Passport file `<secrets-dir>/moex-realtime.json`.
- `--key-path` (moex-api-key) is accepted but never used.

**Instruments.** RUB corporate bonds on TQCB, fixed and floating. The benchmark mapping for RUONIA / key rate / G-curve / CPI is at `production.py:72-94`. RUSFAR is a display label only.

**Techniques worth reusing.**
- **Offset-paged trade tape with day-rollover checkpoint** (`fetch_page` L1121):
  - resets the offset when `dataversion.trade_date` changes;
  - probes the predecessor row to detect a tape reset;
  - drops the previous day's rows during the morning rollover.

  This is the most robust trade-tape reader in the set.
- `_rows` strict-width ISS block decoder.
- Retries ×3 on 429/5xx.
- Curve freshness gate: 6 h maximum, 7 days on fallback, rejects future timestamps (`RuoniaTradePricer.current_curve_identity` L865).
- SHA-256 content-addressed pricing identity.
- Fixing-cache key that includes the SQLite inode, mtime and size (including -wal and -shm).
- Append-only ledger and outbox (`store.py`, 3.6k lines).

**Quality.**
- Strict mypy, about 26 test files, all offline.
- Monolith files: `runtime.py` is 2.8k lines, `store.py` 3.6k.
- Dead code: `_APIM_BASE`, `legacy_key_path`.
- No calendar: "previous session" means the latest snapshot date seen (`market_signals.py:597`).

### 1.3 corporate-bond-monitor (v0.2.5, Windows desk)

**Purpose.** Local FastAPI monitor of the RUB corporate universe on TQCB.
- Live bid/offer/last from **QUIK through QuikPy** on ports 34132/34133.
- Probes desk libraries (three proprietary desk packages).
- Newer `analytics/` code adds fail-closed APIM capture with offline replay, and a desk-system bond-product cache.

**Endpoints.**
- Public ISS `/securities/{isin}.json` (`adapters/moex.py:MoexAdapter.lookup`).
- `/engines/stock/markets/bonds/boards/TQCB/securities.json?iss.only=securities&securities.columns=SECID,SHORTNAME,ISIN,…,EMITTER_ID&start=&limit=` (`adapters/moex.py:iter_bond_universe_pages` L91, driven by `market.py:UniverseSyncManager`).
- APIM `https://apim.moex.com/iss/engines/stock/markets/bonds/boards/TQCB/{securities,trades}.json` (`tools/windows_acceptance/run_windows_acceptance.py:_apim_block` L184 and `live_apim_full_scan` L249). This script hits live endpoints.
- QUIK: `get_info_param(TRADEDATE/SERVERTIME)` and `get_param_ex(class,sec,BID/OFFER/LAST/VALTODAY)` (`adapters/quik.py`, `quik_session.py`). A custom Lua `get_ofz_trades_bulk` does a chunked `all_trades` scan with an `after_trade_num` cursor (`quikpy-lua-fixed/lua/ofz_bulk.lua`).
- Cbonds `https://ws.cbonds.info/services/json` (`get_emissions`, `get_emission_ratings_new`) are **placeholders only, with no live calls** (`adapters/cbonds.py`).
- The desk system's bond-product lookup: singleton calls only; comma batching is broken (`analytics/<desk>_cache.py` L418).

**Auth.**
- `MOEX_API_KEY`
- Desk-system login/password variables; `config.py` reads a differently named login variable, which is inconsistent.
- `CBONDS_LOGIN`, `CBONDS_PASSWORD`, `CBONDS_BASE_URL`
- `QUIKPY_PATH`, `QUIK_REQUESTS_PORT`, `QUIK_CALLBACKS_PORT`, `QUIK_STATUS_TIMEOUT_SECONDS`, `QUIK_SNAPSHOT_TIMEOUT_SECONDS`
- `CORP_MONITOR_DB`, `CORP_MONITOR_HOST`, `CORP_MONITOR_PORT`, `CORP_MONITOR_PYTHON`
- `HTTP_PROXY`, `HTTPS_PROXY`

**Instruments.** RUB corporate, municipal and subfederal bonds. Bond static data (coupons, amortizations, offers) comes from the catalogue DB. OFZ trades come through the Lua bulk export.

**Techniques worth reusing.**
- **QUIK isolation:** every QuikPy call runs in a spawned child process with a hard timeout and terminate/kill, a lock, and a cached last status so health checks never block (`quik_session.py:_run_isolated`). This is the pattern to copy for a hub QUIK bridge.
- ISS paging with duplicate-page fingerprinting and a max-pages limit.
- Exponential backoff; 4xx fails fast except 408 and 429.
- Fail-closed APIM payload validation with raw-byte SHA-256 (`analytics/universe.py:validate_apim_response`).
- Append-only analytics store with migrations and secret rejection (`analytics/persistence.py`).
- Desk-system positive/negative cache with 7-day revalidation.

**Quality.**
- About 20 offline test files.
- `_apim_block` has a wrong stop condition, and it paginates a board snapshot even though pagination is ignored there.
- The APIM bond `orderbook.json` returns HTML (no entitlement).
- Standard-library urllib.

### 1.4 currency-bond-arbitrage-monitor (v0.3.0)

**Purpose.** Nightly replayable collector of CNY/USD/EUR-face bond trades on the RUB boards (TQCB/TQOB) and native-currency boards (TQOY/TQOD/TQOE).
- Normalises each trade to a face-currency clean price using a point-in-time FX mark from market-data-store.
- Keeps daily history aggregates with revisions, and a WAP pricing step through moex-market-data-mcp.

**Endpoints.** Through `MoexPassportClient`:
- `board_snapshot` and `board_trades` (`moex_source.py:MoexCurrencyTradeSource.fetch` L101, `_fetch_page_rows` L79).
- `/history/engines/stock/markets/bonds/boards/{B}/securities.json?date=&iss.only=history,history.cursor&start=` (`daily_history.py:MoexDailyHistorySource._fetch_board` L67).

FX comes from `IntradayFxRepository.get_reference(pair_code="CNY/RUB", …)` in the market-data-store SQLite (`fx_source.py:32`).

**Auth.** No env vars of its own. Passport credentials file via the moex-live-bonds dependency.

**Instruments.** **CNY/USD/EUR-face bonds**, selected by FACEUNIT (`moex_source.py:14`, `daily_history.py:10`), plus CNY/USD/EUR→RUB marks and MOEX CROSSRATE.

**Techniques worth reusing.**
- The cleanest `history.cursor` loop in this group: TOTAL and PAGESIZE, plus a check that MOEX honoured the requested date (`daily_history.py:103+`).
- Repeated-page signature on the trade tape (first and last TRADENO plus length).
- SQLite unique `(BOARDID,TRADENO)` with `raw_json` and a revisions table.
- `Decimal` throughout.

**Quality.**
- Strict mypy, 8 offline tests.
- Bad rows are skipped silently.
- `<home>/...` paths in scripts.
- The backfill uses `date -d '1 day ago'`, with no holiday awareness.

### 1.5 currency-bond-monitor (v0.4.2)

**Purpose.** Intraday snapshots and reports for currency bonds across 8 boards: TQCB/TQOB/TQOD/TQOE/TQOY, plus PSOB/PTOB/OTCB on `ndm`.
- Computes effective currency WAP and yield, and Z-spreads to foreign curves:
  - USD: SOFR OIS
  - EUR: €STR OIS
  - CNY "external": FR007 IRS
  - CNY "local": RUSFAR-CNY OIS
- Also computes 3-month turnover percentiles and RUB-vs-native board dislocations.

**Endpoints.**

| Source | Call | Location |
|---|---|---|
| Passport `board_snapshot` | live board levels | `levels.py:MoexBoardLevelsClient.fetch_board` L105 |
| Public bondization | `https://iss.moex.com/iss/statistics/engines/stock/markets/bonds/bondization/{SECID}.json?iss.meta=off&limit=1000` (coupons, amortizations, offers, plus payload SHA-256) | `schedule_source.py:MoexScheduleSource.fetch` L52 |
| History via moex-bond-turnover | 92 days of turnover | `turnover_history.py:106` |
| CBR `XML_dynamic.asp` | official FX rates | via moex-bond-turnover |
| market-data-store `IntradayFxRepository` | FX marks | `fx.py:60` |
| market-data-store `CurveWorldService.resolve(world, decision_time, KnowledgePolicy.AS_KNOWN_THEN)` | worlds `usd_sofr_ois_base_v1`, `eur_estr_ois_base_v1`, `cny_fr007_irs_base_v1`, `cny_onshore_base_v1` | `curve_loader.py:12-17`, verified |
| `market-data-store ingest-free-base-curves` CLI | run before collection | `scripts/collect_currency_bond_reports.sh` |

**Auth.**
- `CURRENCY_BOND_SCHEDULE_CACHE`
- Passport credentials file via the dependency.

**Instruments.**
- CNY/USD/EUR currency bonds, sovereign and corporate.
- FX, and CBR fixings.
- Curves consumed: **CNY FR007 IRS, CNY RUSFAR-CNY OIS, USD SOFR OIS, EUR €STR OIS**.
- Bondization.

**Techniques worth reusing.**
- Bondization-to-schedule builder with put/maturity horizon and coupon-amount inference (`schedules.py`).
- Point-in-time FX "world" with age and quality checks.
- Curve-world staleness gate of 4 days.
- MOEX SYSTIME/UPDATETIME→UTC conversion (`levels.py:_moex_system_time`).
- Atomic bundle swap.

**Quality.**
- Strict typing, 16 offline tests.
- The bondization loader does **not paginate** (relies on `limit=1000`) and has no retry.
- The README names `cny_offshore_base_v1`, but the code uses `cny_fr007_irs_base_v1`.
- Sovereign issuer is hard-coded (emitent 1228).

### 1.6 market-snapshot-reporter

**Purpose.** Deterministic Telegram market snapshots:
- top-20 TQBR equities;
- a "global" block: CNY/RUB, USD/RUB, IMOEX, G-curve 10Y, RUONIA, RUSFAR, RUSFAR CNY, G7 FX, global indices and commodities.

**Endpoints** (`src/market_snapshot_reporter/sources.py`, base `https://iss.moex.com/iss`):

| Data | Path | Location |
|---|---|---|
| TQBR equities | `/engines/stock/markets/shares/boards/TQBR/securities.json` | |
| TQBR history | `/history/engines/stock/markets/shares/boards/TQBR/securities.json?date=&history.columns=SECID,VALUE,NUMTRADES`, paged by `history.cursor` | L127-145 |
| **FX spot** | `/engines/currency/markets/selt/boards/CETS/securities/{CNYRUB_TOM\|USD000UTSTOM}.json` | L155-191 |
| **RUSFAR / RUSFARCNY** | live `/engines/stock/markets/index/securities/{SECID}.json`; history `/history/engines/stock/markets/index/securities/{SECID}.json?from=&till=` | L193-247; RUSFARCNY used at `report.py:221-223` |
| IMOEX | `/engines/stock/markets/index/securities/IMOEX.json` | |
| **ZCYC** | `/engines/stock/zcyc.json?date=` (`yearyields` block, period=10) | L273-293 |
| **G7 FX and global** | Yahoo `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d` | `YAHOO_INSTRUMENTS` L296-320 |

The Yahoo list covers EURUSD=X, JPY=X, GBPUSD=X, CHF=X, CNY=X, DX-Y.NYB, indices, and Brent/WTI/gold/silver/copper/natural gas.

RUONIA comes via `moex_market_data.cbr.CbrClient.ruonia_history` from the moex-market-data-mcp dependency.

**Auth.** None.

**Techniques worth reusing.**
- `JsonClient` with 3 attempts and linear backoff; errors are body-free.
- Last-available-day fallback loop of up to 8 days.
- Stale/future timestamp guard (L359-362).
- Atomic JSON state.

**Quality.** mypy strict, offline respx tests. Yahoo is an unofficial source.

### 1.7 moex-bond-turnover

**Purpose.** Ranks MOEX FX-denominated bonds by real turnover (`VALUE`) on the main T+ boards, converted to RUB at CBR rates.

**Endpoints.**
- `https://iss.moex.com/iss/history/engines/stock/markets/{bonds|ndm}/boards/{board}/securities.json?date=` (`client.py:MoexIssClient._fetch_one` L96-130).
  - Boards: TQCB, TQOB, TQOD, TQOE, TQOY, TQRD, TQUD. PSOB, PTOB and OTCB go to `ndm`.
- `https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=&date_req2=&VAL_NM_RQ=` (`rates.py:fetch_cbr_rate_matrix`).
  - Currency codes: CNY R01375, USD R01235, EUR R01239, CHF R01775.

**Auth.** None.

**Techniques worth reusing.**
- **Cursor-first pagination:** fetches `iss.only=history.cursor` first, then all pages in parallel with `ThreadPoolExecutor(max_workers=10)` (`client.py:103-129`, verified).
- Retry on 429/5xx.
- `JsonCache` at `<cache-dir>/moex-bond-turnover/{date}/{board}.json` with atomic writes; empty days are re-fetched.
- CBR as-of lookup with a 7-day lookback.
- `HistoryProvider` and `RateLoader` protocols.

**Quality.**
- Typed, offline tests.
- Weekday-only dates.
- Spoofed User-Agent.
- Rows without a rate are silently dropped.

### 1.8 ofz-swaps-v2 ("OFZ Rates Workbench", v1.2.2)

**Purpose.** FastAPI backend with a React frontend (plus a legacy Streamlit app):
- OFZ-PD × RUONIA OIS forward matrix;
- implied key-rate path from KRS and RUONIA OIS, with manual overrides.

It vendors `implied_key_rates`, depends on moex-live-bonds and rub-fixed-income-core, and has an optional `moexalgo` extra.

**Endpoints.**

| Source | Call | Location |
|---|---|---|
| Passport TQOB `board_snapshot` | live quotes | `ofz_workbench/moex.py:MoexOfzClient.fetch` |
| Public bondization | `/iss/statistics/engines/stock/markets/bonds/bondization/{secid}.json` (`limit=1000`), with a completeness check | `moex.py:_official_cashflows` L59-108 |
| Legacy ISS | TQOB `start=`/`limit=100` paging | `ofz_analytics/static_data.py`, `market_data.py`. Risk of an infinite loop: board endpoints ignore paging. |
| AlgoPack | `moexalgo.Ticker(secid).candles(period="1d")` | `ofz_analytics/market_data.py:fetch_live_quotes_algopack` L177-241 |
| CBR key rate | `https://www.cbr.ru/hd_base/KeyRate/` (`UniDbQuery.Posted/From/To`) | `ofz_workbench/cbr.py:CbrClient.context` |
| CBR meetings | `/dkp/cal_mp/` | same; parsers `parse_key_rate_page`, `parse_meeting_calendar_page` |
| **Ratescalcs** | `https://ratescalcs.ru/cnyfuturesyields/key-rate-editor/ois` (RUONIA OIS) and `/cnyfuturesyields/key-rate-editor/krs` (KRS) | `ofz_workbench/ratescalcs.py:RatescalcsClient.fetch` L153-220, verified; HTML `<textarea name="content">` lines `TENOR bid ask`. The curves are RUB despite the "cny" path. |
| Holiday list | static 2026 production calendar from `government.ru/docs/all/161028/` | `ofz_workbench/calendar_data.py:18-37`; raises for other years |

**Auth.**
- `RATESCALC_USERNAME`, `RATESCALC_PASSWORD`, `RATESCALC_BASE_URL`, `RATESCALC_PROXY_URL`, `RATESCALC_ENV_FILE` (default file `<secrets-dir>/ratescalc.env`)
- `MOEX_API_KEY`
- Passport credentials file
- Proxy auto-set to a loopback port

**Instruments.** OFZ-PD (static, cash flows, quotes), key rate and CBR meetings, **RUONIA OIS curve, KRS curve**.

**Techniques worth reusing.**
- Ratescalcs adapter:
  - GET-only, with a base-URL allowlist;
  - strict parse (duplicate tenors, bid > ask, range checks);
  - overrides that keep provenance (`apply_quote_overrides`).
- OFZ-PD universe filter (`moex.py:_is_active_ruble_ofz_pd`).
- Price fallback LAST → WAPRICE → PREVWAPRICE, with a `quote_source` tag.
- CBR context that fails closed on implausible rates.
- Injectable `httpx.BaseTransport` everywhere.

**Quality.**
- `ofz_workbench` is typed with offline tests.
- The legacy `ofz_analytics` reads mismatched marketdata fields and swallows exceptions.
- The 2026-only calendar.

### 1.9 implied-key-rates

**Purpose.** Library that solves an implied CBR key-rate path from pasted RUONIA OIS quotes.

The same package is copied into ofz-swaps-v2 and ofz-rates-app.

**Endpoints** (`implied_key_rates/cbr.py:CBRSource`, requests):
- `https://www.cbr.ru/hd_base/KeyRate/`: HTML table, with a Highcharts regex fallback.
- `https://www.cbr.ru/eng/DKP/cal_mp/`: keyword heuristic.

**Auth.** None.

**Techniques.**
- Tolerant OIS text parser (`parser.py:parse_ois_quotes`).
- `moscow.py` business-day functions and `adjust_modified_following`.
- `DecisionCalendar.build_path_skeleton`.

**Quality.**
- Offline tests, light typing.
- The meeting heuristic can catch non-meeting dates.
- `except Exception: pass`.
- `moscow.py` is not production-grade: perenos is approximated, working Saturdays are missed, and Radunitsa is added (L93).

### 1.10 ofz-analytics

**Purpose.** Streamlit prototype for an OFZ-PD forward-forward matrix funded at RUONIA OIS. The OIS curve is pasted and saved to `~/.ofz_analytics/swap_curve.json`.

**Endpoints.**
- TQOB securities, paged with `start=`/`limit=100` (`static_data.py:44`, `:105 fetch_ofz_pd_bonds`).
- TQOB `securities,marketdata` via `MoexPassportHttpClient` (`market_data.py:46`).
- `moexalgo` candles (`market_data.py:167`).

**Auth.** `MOEX_API_KEY`, `HTTP(S)_PROXY`.

**Notes.**
- Coupons are synthetic, stepped from NEXTCOUPON; there is no bondization loader.
- `example.py` makes live calls.
- Partial typing; exceptions are swallowed.
- **Low reuse value.** This is the origin of the `ofz_analytics` package that is now copied into three repos.

### 1.11 ofz-market-figures-reporter

**Purpose.** Cron CLI producing Telegram Markdown and PNG output:
- OFZ-PD/PK/IN tables, with PK as Z-spread to RUONIA and IN with breakeven;
- hourly yield and RGBI candles;
- curves vs previous close and vs one week ago;
- turnover by board with 3-month percentiles;
- 15-minute cumulative turnover;
- TQCB top-30.

**Endpoints** (`src/ofz_market_figures/moex.py`):
- Passport `board_snapshot` for TQOB, PSOB/PTOB/OTCB (ndm) and TQCB (`moex.py:50-68`).
- `https://iss.moex.com/iss/history/engines/stock/markets/{market}/boards/{board}/securities.json?date=&iss.only=history&limit=100&start=` (`moex.py:80 history_board`).
- `/iss/engines/stock/markets/{market}/boards/{board}/securities/{secid}/candles.json?from&till&interval&start=` (`moex.py:129`). Page size 500, deduplicated on `begin`. Used for TQOB at interval 60 and for RGBI on SNDX.
- market-data-store `get_rate_curve(curve_code="ruonia_discount")`, `solve_z_spread_from_clean`, `analyze_bond_against_ruonia` (`cli.py:122-136`, `corporate.py:1071`).
- The catalogue DB.

**Auth.**
- Desk-system login/password variables (desk script only).
- Passport credentials file.
- `moex.py:30 _api_key` is dead code.

**Techniques worth reusing.**
- History retries with exponential backoff.
- `cached_history` (`moex.py:168`): a JSON file per day and board, fetched by an 8-worker pool.
- `complete_candles` drops the candle that is still forming (`moex.py:158`).
- `TurnoverStore`: SQLite WAL with 15-minute buckets (`intraday_turnover.py`).
- Live price fallback LAST → mid → PREVLEGALCLOSEPRICE.
- Empirical "latest completed session" probe (`moex.py:120`).

**Quality.** Typed, about 2.3k lines of offline tests, sibling-path coupling, weekday-only settlement.

### 1.12 ofz-rates-app

**Purpose.** FastAPI app with user auth and AES-GCM encrypted secrets. Computes OFZ forward-forwards and an implied key-rate path from RUONIA OIS and KRS.

**Endpoints.**
- Passport `board_snapshot` for TQOB/PSOB/PTOB/OTCB (`ofz_analytics/market_data.py:41,73`).
- APIM `https://apim.moex.com/iss/...TQOB/securities.json` and `https://passport.moex.com/authenticate` (`backend/moex_auth.py:57`). Used for status only.
- CBR key rate, in two versions:
  - `backend/services/cbr_rate.py:10`, falling back to `/DKP/GetCalendar`. **Bug:** it silently returns a hard-coded 20%, and its regex looks for ISO dates while the page uses DD.MM.YYYY.
  - The `implied_key_rates/cbr.py` copy.
- market-data-store SQLite, opened read-only:
  - source `otc_monitor`, datasets `ruonia_ois` (1W–10Y) and `key_rate_swap` (3M–10Y), bid/mid/ask (`backend/services/otc_monitor_sync.py:86`);
  - source `andromeda`, `ruonia_ois` (`scripts/ofz26249_ruonia_fwd_history.py:89`).
- History for SU26249 with `history.cursor`, and bondization (`scripts/ofz26249_ruonia_fwd_history.py:28,68`).

**Auth.**
- `OFZ_APP_BOOTSTRAP_ADMIN_USER`, `OFZ_APP_BOOTSTRAP_ADMIN_PASSWORD`, `MOEX_API_KEY`, `HTTP(S)_PROXY`.
- DB-stored encrypted secret names `moex_api_key`, `moex_login`, `moex_password`.
- Master key file `data/master.key`.

**Techniques worth reusing.**
- **Fail-closed swap-snapshot validation** (`otc_monitor_sync.py`): exact tenor sets, timezone-aware timestamps, bid ≤ mid ≤ ask, maximum age, same-run completeness.
- `KeyRateSwapPricer` (`backend/services/keyrate_math.py:19`).
- `SwapPricer` (`implied_key_rates/swap.py`).

### 1.13 ofz-tracker ("ofz-curve-app")

**Purpose.** FastAPI live and historical OFZ curve viewer with an intraday trade-flow monitor.
- Live TQOB via Passport, with fallback to public ISS.
- Raw trades from TQOB/PSOB/PTOB/OTCB via a Passport REST feed, with QUIK as fallback.
- SQLite trade store with 10-year retention.
- CBR USD rate for PVBP.

**Endpoints.**

| Source | Call | Location |
|---|---|---|
| Live quotes | `{base}/engines/stock/markets/{bonds\|ndm}/boards/{TQOB\|PSOB\|PTOB}/securities.json?iss.only=securities,marketdata` | `ofz_analytics/market_data.py:43,116,145`; Passport with public fallback at `:248,262,318` |
| Trade tapes | `.../TQOB/trades.json`, `.../ndm/boards/{PSOB,PTOB,OTCB}/trades.json`, `start=` cursor, `limit=5000` | `backend/algopack_feed.py:27,139,183`. `APIM_BASE` at L26 is unused. |
| Top of book | TQOB `iss.only=marketdata` (BID/OFFER/BIDDEPTH/OFFERDEPTH/SEQNUM) | `algopack_feed.py:211` |
| History | `https://iss.moex.com/iss/history/engines/stock/markets/bonds/boards/TQOB/securities.json?date=` | `backend/historical_data.py:17`. **No pagination**, so it may truncate. |
| Candles | `.../TQOB/securities/{secid}/candles.json?interval=24` | `scripts/daily_ofz_yield_report.py:75` |
| CBR | `https://www.cbr.ru/scripts/XML_daily.asp?date_req=dd/mm/YYYY` | `backend/daily_risk.py:36` |
| APIM / Passport auth | `https://apim.moex.com/iss/...` (Bearer) and `https://passport.moex.com/authenticate` | `backend/moex_auth.py:109,145,177,216` |
| QUIK | `get_info_param`, `get_param_ex(VALTODAY)`, `on_all_trade` callback, `process_request cmd=get_ofz_trades_bulk`; classes TQOB/OCBR/OCAR/PSOB/PTOB | `backend/quik_session.py:57-91`, `backend/quik_trades.py:24-196`, Lua `QUIK/lua/ofz_bulk.lua` |

**Auth.**
- MOEX: `MOEX_API_KEY`, `MOEX_LOGIN`, `MOEX_PASSWORD`, `MOEX_API_KEY_FILE`.
- Admin and reporting: `ADMIN_PASSWORD`, `OFZ_REPORT_EMAIL_TO`, `OFZ_REPORT_EMAIL_SUBJECT`, `OFZ_HISTORICAL_AVERAGE_DAYS`, `FORCE_FRESH`.
- QUIK: `QUIKPY_PATH`, `QUIK_REQUESTS_PORT`, `QUIK_CALLBACKS_PORT`.
- Trade store: `OFZ_TRADE_DB`, `OFZ_TRADE_RETENTION_DAYS`, `OFZ_FLOW_CACHE_SECONDS`.
- Proxy: `HTTP(S)_PROXY`.

**Instruments.**
- OFZ-PD/PK.
- **CNY OFZ:** SECIDs hard-coded at `scripts/daily_ofz_yield_report.py:34` (RU000A10DQB6, RU000A10DQA8, RU000A10FAK6), candles only. `static_data.py:115` excludes CNY.
- CBR USD fix.

**Techniques worth reusing.**
- Incremental cursor ingestion with a 100-row overlap, a daemon thread and a minimum 2 s refresh (`algopack_feed.py:36-37,266`).
- Trades-block schema check.
- SQLite WAL trade store: `trades` WITHOUT ROWID with PK (date, class, sec, trade_num), `orderbook_snapshots`, `ingestion_state` (`backend/trade_store.py`).
- TTL cache that serves stale data on error (`backend/cache.py:20`).
- Credential-safe error sanitising (`moex_auth.py:29,44`).
- Passport → public fallback with a trace.
- QUIK socket lock.

**Quality.**
- `main.py` is 1.35k lines.
- A release tarball is committed.
- Weekday-only calendar.

### 1.14 rgbi-futures-arbitrage-monitor (~29.5k lines)

**Purpose.** Research-grade fair-value and implied-repo monitor for RGBI index futures (RB*, FORTS RFUD) against the OFZ basket.
- The installed package (`src/rgbi_monitor`) has **no exchange loader**. Its only client is a loopback market-data-store client.
- All MOEX, APIM and CBR fetching sits in research scripts under `docs/`.
- `AGENTS.md` rule 13 says market-data-store owns source facts.

**Endpoints, package.**
- `GET {mds}/v1/curve-worlds/{recipe_id}/resolve?decision_time=&knowledge_policy=as_known_then` (`current_comparison.py:MarketDataStoreCurveWorldClient.resolve` L120-184).
  - Loopback only. HTTP 200/404/409 map to available/unavailable/stale.
  - Recipes: `ratescalcs_ruonia_mid_v2`, `<desk>_ruonia_model_v1`, `andromeda_ruonia_mid_v2`.
- **This is an existing consumer contract for the hub.** `LoopbackMarketDataCurrentSource.resolve` always raises `missing_current_inputs` for basket quotes, futures quote, repo, borrow and fixing path. The hub must supply these.

**Endpoints, scripts.**

| Data | Paths | Location |
|---|---|---|
| FORTS / bonds / index current | `/iss/engines/futures/markets/forts/securities/RBU6.json`, TQOB, `/engines/stock/markets/index/securities/RGBI.json` | `docs/implementation/run_s24_vertical_slice.py:_moex_current` L128 |
| History | `/iss/history/engines/futures/markets/forts/boards/RFUD/securities/{C}.json`, TQOB, `index/boards/SNDX/.../RGBI` | same file, `_moex_eod` L158 |
| Per-bond repo history | `history/engines/stock/markets/ccp/boards/EQRP/securities/{secid}` (WAPRICE) | `docs/analysis/historical-front-2025/historical_front.py:iss_history` L215 |
| RUSFAR ON and term | `/iss/history/engines/stock/markets/index/boards/MMIX/securities/{RUSFAR,RUSFAR1W,RUSFAR2W,RUSFAR1M,RUSFAR3M}.json` | `d003/analyze_basis.py:moex_history` L89 |
| CBR | `https://www.cbr.ru/hd_base/ruonia/dynamics/` (with **publication date**) and `/hd_base/KeyRate/`, query `UniDbQuery.Posted/From/To`, stdlib HTMLParser | `cbr_table` L120 |
| FORTS listing | `/iss/engines/futures/markets/forts/securities.json?limit=5000` | `d003/analyze_basis.py` |
| Rates-futures specs | `/iss/securities/{MFQ6,MFU6,RFU6,RRQ6,RRU6}.json` (1M RUSFAR, RUONIA-index, RUONIA-average futures) | same |
| SPFI curves | `/iss/sdfi/curves.json` (metadata only on public access) | same |
| Repo boards, GC pool | `/engines/stock/markets/ccp/boards/{EQRP,EQWP,EQMP,EQTP}/...`, `/engines/stock/markets/gcc/boards/{GCRP,GCOW,GCOM,GCTM}/securities/RU000A0JW4Z1.json`, `/history/.../ccp/securities/{29 OFZ}.json` | `d004/collect_evidence.py:fetch_raw` L44, `fetch_history` L75 (iss vs apim, schema-drift guard) |
| Index weights | `statistics/engines/stock/markets/index/analytics/RGBI.json?date=` | `d002/analyze_replication.py` |
| Fixing window | 1-minute candles 15:00-16:00 | `d011/collect_and_analyze.py` |
| Bond cash flows | bondization, `limit=1000`, no paging | `docs/analysis/<desk>-reconciliation/rgbi_<desk>_reconcile.py:moex_cashflows` L122 |
| Desk curves (Windows only) | desk curve-system loader call for the RUONIA and RUB_KEY_RATE curves | `d016/export_<desk>_curves_windows.py:178` |
| APIM order-book entitlement | probes | `d005`, `d012` |
| Interfax EFIR, QUIK Lua | documentation downloads only | `d012/collect_sources.py` |

**Auth.**
- Desk-system login/password variables, `OTEL_*`.
- APIM token from the file `<secrets-dir>/moex-api-key`, mode 0600 checked, sent as a Bearer header.

**Instruments.**
- OFZ (29 RGBI constituents), RGBI, RB futures.
- **OFZ repo (EQRP/EQWP/EQMP/EQTP) and GC repo (GCRP/GCOW/GCOM/GCTM)**.
- RUSFAR ON and term, RUONIA, key rate.
- RUONIA and key-rate curves (desk curve system).
- Rates-futures metadata (MF*, RF*, RR*).

**Techniques worth reusing.**
- **Content-addressed raw cache**: a URL-sha256 pointer to `cache/raw/<sha>.json`, verified on read, with `--refresh` (`historical_front.py:Cache` L184, `rbu6-15m-2026/build.py:get` L17). The latter retries 6 times with `min(20, 1.5**n)` backoff.
- **`calendars.py` `OfficialCalendar`** from a hash-verified snapshot, with `require_session`, `next_session`, `DateRole` and `ContractDateRoles.from_expiry`. Fails closed outside coverage. Verified.
- `tape.py:SessionSchedule` (L129) and `expected_intraday_slots` (L247) for minute slots, including halts.
- SQLite migrator: WAL, `synchronous=FULL`, up/down SQL (`tape_migrations/`).
- Online backup (`tape.py:TapeStore.backup_to` L1295).
- `quotes.py:classify_orderbook_response` (L36): an HTML response with HTTP 200 means "no entitlement", not an empty book.
- Front-contract chain (`historical_front.py:FRONT_CHAIN` L47-55).
- Stdlib xlsx parser.

**Quality.**
- Package: mypy strict, about 173 offline tests.
- `docs/` scripts are excluded from lint and heavily hard-coded.
- **Bug:** `run_s24_vertical_slice.py:519` uses `sys` without importing it.
- Three partially duplicated `historical_front` versions.

### 1.15 usdrub-proxy-research (single 377-line script)

**Purpose.** Studies how well USD/RUB proxies tracked USD000UTSTOM between 2022-04 and 2024-06. The proxies are USDRUBF, CNYRUB_TOM × Dukascopy USD/CNH, and CNYRUB_TOM × the CBR USD/CNY cross.

**Endpoints** (`research.py`).
- **AlgoPack:** `https://apim.moex.com/iss/datashop/algopack/{fx|fo}/tradestats/{secid}.json?from&till&start&limit=1000&iss.meta=off` (`moex_rows` L49-85).
  - `fx` market: USD000UTSTOM, CNYRUB_TOM.
  - `fo` market: **USDRUBF**.
- Dukascopy: `https://datafeed.dukascopy.com/datafeed/USDCNH/{YYYY}/{MM-1:02d}/{DD}/{HH}h_ticks.bi5` (`dukascopy_ticks_for_hour` L112-156).
- CBR: `https://www.cbr.ru/scripts/XML_dynamic.asp?...&VAL_NM_RQ={R01235,R01375}` (`cbr_series` L187-203).

**Auth.** Token file `<secrets-dir>/moex-api-key`; file mode not checked.

**Techniques worth reusing.**
- AlgoPack tradestats paging in month chunks.
- Right-edge bar labelling, which matters when aligning AlgoPack 5-minute bars.
- bi5 decoder: LZMA plus `>3I2f`; 8 retries with jittered backoff.
- CBR forward-fill over the calendar.

**Quality.**
- No tests, script-only.
- **Bug-prone:** CBR `Nominal` is ignored and the CNY unit is guessed with `raw > 25` (L248). The hub must read `Nominal`/`VunitRate`.

### 1.16 cbonds-research-connector (v0.1.0)

**Purpose.** ISIN point lookup of **static bond terms** from the cbonds.ru UI, through the `agent-browser` CLI (headless Chromium with a saved session). Parses the result into a pydantic `Bond`:
- issuer, nominal, coupon type, benchmark and margin;
- day count, business-day convention, frequency, maturity;
- perpetual flag, calls, put offer, placement;
- document links.

Exposed as a CLI and an MCP server.

**Endpoints.** `https://cbonds.ru/` search, then `https://cbonds.ru/bonds/<id>/` (`gateway.py:AgentBrowserGateway.fetch` L71-102). No REST API is used.

**Auth.** `CBONDS_CACHE_ROOT`. The session file is hard-coded to `<data-dir>/cbonds/session.json`.

**Techniques worth reusing.**
- `security.py:validate_isin` (Luhn checksum).
- URL allowlist.
- Content-addressed JSON cache with `manifest.jsonl` and atomic 0600 writes (`storage.py:CacheStore`).
- `fcntl` advisory lock.
- At least 5 s between live fetches.

**Quality.**
- Strict mypy, fixture-based tests.
- Brittle positional text parser; keeps only the first cash-flow row.
- An unknown coupon label becomes "fixed".

### 1.17 andromeda-research-connector (v0.3.1)

**Purpose.** Uses Playwright to discover and fetch sell-side research (HTML and PDF) from the ANDROMEDA portal. Stores Markdown, a manifest and an SQLite FTS5 catalogue. CLI plus MCP server. **No numeric market data.** Andromeda *curves* reach market-data-store by another path (recipe `andromeda_ruonia_mid_v2`, source `andromeda`).

**Endpoints.**
- `https://euler.andromedaterminal.ru/publications` (`?sid=&page=`) and `/Document/<id>`.
- `https://docs.andromedaterminal.ru` blob PDFs.
- Keycloak `https://andromedaterminal.ru/auth/realms/**`.

**Auth.**
- No env vars. Credentials are entered with getpass and never persisted.
- Storage state at `<secrets-dir>/andromeda/storage-state.json`.

**Techniques worth reusing.**
- Playwright `BrowserAdapter` with jittered retries and failure screenshots.
- Stale-PID `SessionLock` plus a pause file.
- Origin allowlist.
- FTS5 catalogue.

### 1.18 rub-fixed-income-core (v0.8.1): shared maths, no I/O

**Purpose.** Deterministic core used by about 10 apps. Covers:
- conventions, schedules, cash events, accrued interest, yields;
- OIS bootstrap (`discount.py:179`);
- KRS pricing (`key_rate.py`, `CONVENTION_ID="moex-irs-keyrate-tom-modfol-actactisda-v1"`, `build_key_rate_forward_curve` L383);
- RUONIA and key-rate floaters (`floating.py:65 FixingHistory`);
- **FX-futures risk** (`fx_futures.py:40 FxFuturesContract`, `:86 FxFuturesRiskRequest`, `:237 FxFuturesRiskEngine`, verified);
- DV01/KRD risk.

**Endpoints.** None. `tools/<desk>_bond_reconciliation_probe.py` is a desk-only probe.

**Auth.** Desk-system login/password variables, used by the probe only.

**Why it matters to the hub.**
- `dates.py:32 BusinessCalendar` (holidays and working days frozensets plus a version) and `conventions.py:118 calendar_fingerprint` are the canonical calendar type, but **they ship no data**. `docs/BACKLOG.md` P0 #1 asks the data project to own the official versioned MOEX/CBR calendar.
- `public_schedule.py:22 bind_market_data_store_schedule` is a strict validator for schedule JSON. Required fields:
  - top level: `secid`, `settlement_date`, `horizon_date`, `horizon_type`, `formula_type`, `calculation_quality=="exact_published"`, `warnings==[]`, `financing_curve_build_id`;
  - `cashflows[]` rows: `payment_date`, `coupon`, `principal`, `total`, `coupon_source`.

  **This is the consumer contract for the hub's cash-flow endpoint.**
- `local_worker.py:63-110` `SnapshotEvidence` (`snapshot_id`, `source_id`, `observed_at`, `known_at`, `valid_until`) and `PublicBondSnapshot` are good models for hub provenance.
- `docs/BACKLOG.md` items 33-34 specify a public FX-futures snapshot: exchange-spec terms, settlement price, spot and value date, both curves, and delivery date from the official calendar.

**Quality.** About 236 tests, mypy strict, frozen dataclasses. Top quality in the set.

### 1.19 russia-inflation-nowcast (v0.2.0)

**Purpose.** Weekly and monthly CPI nowcast from Rosstat and CBR. Includes a release-gate watcher and a retail price ledger.

**Endpoints.**
- Rosstat workbooks (`cli.py:33-40`): `https://rosstat.gov.ru/storage/mediabank/{Nedel_ipc.xlsx, Nedel_sred_cen.xlsx, CPR_Gr-Tov-RF_2001-2026.xlsx, CPR_tov_RF_2004-2026.xlsx, Vesa-tov-KIPC_2012-2026.xlsx}`.
- CBR seasonally adjusted CPI: `https://cbr.ru/Content/Document/File/108632/indicators_cpd.xlsx`.
- Monthly CPI discovery: `https://rosstat.gov.ru/statistics/price?print=1`, regex `ipc_mes_MM-YYYY.xlsx` (`sources.py:15-31`).
- Release gate: `https://rosstat.gov.ru/announcements?print=1` and `/central-news/rss` (`gate.py:19-22`).
- Open Prices API.

**Auth.** None.

**Data covered.** Weekly CPI components, weekly average prices, monthly CPI, weights, CBR seasonally adjusted CPI, Rosstat release announcements.

**Techniques worth reusing (strongly).**
- `download.py:155 cached_download`:
  - conditional GET with ETag and If-Modified-Since;
  - immutable vintages `vintages/<stem>/<ts>_<sha12>.<ext>` plus metadata JSON;
  - xlsx validation, backoff;
  - Rosstat Range-request and curl TLS 1.2 fallback.
- `tls.py`: pinned Russian Trusted Root bundle, scoped to Rosstat, with a TLS 1.2 adapter.
- `gate.py`: dual HTML+RSS release discovery with seen-state.
- `parsers.py`: Rosstat and CBR Excel parsers.
- robots.txt and source-policy registry.

**Quality.** About 77 mocked tests, not type-checked.

### 1.20 russian-real-money-positioning (v0.1.0)

**Purpose.** Research into real-money investor positioning and a bond cash-flow tracker for Q4 coupons, amortisations, offers and placements.

**Endpoints** (`scripts/research/moex_q4_cashflow_probe.py`):
- `/iss/securitygroups/stock_bonds/collections/stock_bonds_all/securities.json?start=&limit=1000`: the rich bond master.
- `/iss/statistics/engines/stock/markets/bonds/bondization.json?from=&till=&start=&limit=100`: **bulk bondization for all bonds over a date window**, with multi-section cursors.
- `/iss/securities.json?group_by=group&group_by_filter=stock_bonds` (`moex_perimeter_gap_probe.py`).

It also reads market-data-store tables directly (`curve_quotes` with `key_rate_swap`, `index_fixings` for ruonia, `derived_curve_nodes`).

**Auth.** None.

**Techniques worth reusing.**
- **ISS cursor pagination with integrity gates** (L38-95):
  - cursor index equals the requested start;
  - no duplicate SECIDs;
  - cursor total stays stable;
  - row total equals TOTAL;
  - SHA-256 manifest per page.
- `schema.sql`: revision-friendly `issue` and `cashflow_event` tables.
- OFZ-PK family map: SU29013 and SU29015–29025 are arithmetic RUONIA; SU29026–29029 are compounded index ratio.

### 1.21 bank-form101-positioning (v0.1.0)

**Purpose.** Monthly ingest of CBR form 0409101 (bank balance sheets) plus RGBI and TQOB context.

**Endpoints.**
- `https://www.cbr.ru/banking_sector/otchetnost-kreditnykh-organizaciy/` index page.
- `https://www.cbr.ru/vfs/credit/forms/101-YYYYMMDD.rar` (cp866 DBF inside).
- ISS `history/engines/stock/markets/index/securities/RGBI.json`.
- ISS `history/.../bonds/boards/TQOB/securities.json?date=` with `history.cursor` (`core.py:348-420`).

**Auth.** None.

**Techniques worth reusing.**
- Path-traversal-safe RAR extraction with libarchive (`core.py:122-129`).
- Lenient DBF parser.
- Idempotent ingest keyed on `(report_date, archive_sha256)` with vintages.
- "Frontier" monitor that wakes on change.
- A MOEX outage degrades the output instead of crashing.

**Quality.** Walks calendar days because there is no trading calendar.

### 1.22 cbr-policy-language (v0.1.0)

**Purpose.** Corpus of CBR policy texts for an event study.

**Endpoints.**
- `https://www.cbr.ru/dkp/mp_dec/decision_key_rate/` and `/press/pr/?file=DDMMYYYY_133000key.htm` (`scripts/collect_cbr_policy.py`).
- `https://www.cbr.ru/analytics/dkp/inflationary_expectations/`.
- Rosstat `nedel_Ipc.xlsx`, `ipc_mes_06-2026.xlsx` (the month is hard-coded), `VVP_kvartal_*`.
- CBR `mp_survey_data.xlsx`.

**Auth.** None.

**Quality.** Script-level. `rosstat_get` uses `verify=False`; avoid it and use the nowcast TLS approach instead. Useful as a seed for **key-rate decision history and the meeting calendar**.

### 1.23 russian-rating-agencies-mcp (v0.1.0)

**Purpose.** Read-only MCP for ACRA, NKR, Expert RA and NRA ratings, under agreed quotas of 1,000 requests per day and 2 per second per agency.

**Endpoints** (`service.py`).
- ACRA: `POST https://www.acra-ratings.ru/local/ajax/get_rate_emission.php`.
- NKR: `GET https://ratings.ru/ajax/ajax_filter.php` with `info_block_id` 14, 3 or 2 and header `X-Requested-With`.
- Expert RA: `https://raexpert.ru/database/securities/bonds/[<id>]` and `/ratings/debt_inst/`.
- NRA: `https://www.ra-national.ru/wp-json/wp/v2/press_release`.

**Auth.** `RATING_AGENCIES_LIMIT_DB` (path override).

**Techniques worth reusing.**
- **`limits.py:RequestLimiter`**: SQLite `BEGIN IMMEDIATE`, per-(agency, UTC day) counter, minimum interval, counting *before* dispatch, injectable clock. Reuse it for APIM, AlgoPack, Cbonds and Ratescalcs quotas.
- Redirect host re-check.
- Per-source error isolation.

**Quality.** mypy strict. `scripts/live_smoke.py` hits live endpoints and consumes quota.

### 1.24 trading-tracker (Node legacy plus Python `pa_portfolio_tracker`)

**Purpose.** Personal portfolio tracker built on broker-report parsers. ADR-0003 delegates *all* market data to a shared Market Data Service.

**Market-data code.**
- Legacy `market-data/moex-service.js` calls ISS:
  - `/iss/engines/stock/markets/{shares|bonds}/securities/{T}.json`
  - `/iss/engines/futures/markets/forts/securities/{T}.json`
  - ISIN resolution via `/iss/securities.json?q=`

  **Bug:** the futures regex at L186 has no end anchor, so GAZP and LKOH are routed to FORTS.
- FX comes from the third-party mirror `https://www.cbr-xml-daily.ru/daily_json.js`, not cbr.ru.

**Hub contract** (`src/pa_portfolio_tracker/market_data/client.py`):
- Endpoints: `GET /v1/prices/{instrument_id}/current` and `GET /v1/prices/{instrument_id}/eod?start=&end=`.
- Response: `{status: exact|missing, instrument_id, observations:[{observation_id, source, basis, value(decimal str), observed_at, observed_on}]}`.
- `basis` is one of bid, offer, mid, last, close, legalcloseprice, waprice, marketprice3, broker_mark, cost, manual.
- Sources seen: `moex_iss`, `moex_apim`.
- Base URL: `http://127.0.0.1:8765`.

**Env vars.** `PA_DATABASE_PATH`, `MARKET_DATA_REFRESH_MARKER` (the hub should touch this file after each refresh), `MARKET_DATA_STALE_AFTER_SECONDS`, `PA_CALCULATION_VERSION`.

**Instrument identity.** `instruments/resolver.py` scores ISIN > SECID > broker code > ticker, with temporal validity. This is a useful pattern for hub symbology.

**CNYRUBF.** Positions only, from broker parsing (`parsers/ronin-parser.js:435,703`, `scripts/load-real-data.js:28`).

### 1.25 primary-bond-pricing-reports and corporate-bond-relative-value-study

Both are one-off research packs.

**primary-bond-pricing-reports** (`scripts/calculate_peer_placement_metrics.py`, `scripts/build_rushydro_v3.py`):
- ZCYC `https://iss.moex.com/iss/engines/stock/zcyc.json?date=&iss.meta=off` (L35-47).
- Bondization with `limit=500` (L50).
- Per-security history with a cursor (`build_rushydro_v3.py:moex_history` L227).
- market-data-store `rank_bond_ruonia_spreads` and `analyze_bond_against_ruonia`.

**corporate-bond-relative-value-study:**
- `fetch_all_tqcb_liquidity.py`: TQCB history with a cursor and 30 ms throttle, looping over calendar days.
- Ratings via `russian-rating-agencies-mcp` with a JSON cache.
- `docs/eod-ofz-curve-archive-roadmap.md` is a useful **unimplemented spec** for immutable EOD capture: raw plus checksum, manifest, revisions, completeness gate, MOEX calendar.

No auth in either. Prototype quality: hard-coded dates and `sys.path` hacks.

---

## 2. Best implementation per data domain (ranked)

Ranks are 1 = lift first. Paths are `repo: file[:symbol]`.

| # | Data domain | Best source to lift | Runner-up / notes |
|---|---|---|---|
| 1 | **MOEX authenticated transport (Passport cookie, retries, TLS)** | moex-live-bonds: `moex_live_bonds/realtime.py:MoexPassportClient.get_json` | corporate-bond-monitor `adapters/moex.py:_request_json` (exponential backoff, 4xx fail-fast) |
| 2 | **MOEX ISS EOD history (any board), pagination** | moex-bond-turnover: `src/moex_bond_turnover/client.py:MoexIssClient` (cursor-first, parallel, disk cache) | currency-bond-arbitrage-monitor `daily_history.py` (date-honoured check); russian-real-money-positioning `moex_q4_cashflow_probe.py` (integrity gates). Combine all three. |
| 3 | **MOEX realtime board trade tape** | corporate-bond-trade-monitor: `production.py:ApimBoardTradeSource.fetch_page` (day-rollover checkpoint) | ofz-tracker `backend/algopack_feed.py` (overlap cursor, daemon) + `trade_store.py` schema |
| 4 | **MOEX live board snapshot (bonds / ndm)** | moex-live-bonds `realtime.py:board_snapshot` (single request; no paging) | ofz-tracker `ofz_analytics/market_data.py` (Passport→public fallback with trace) |
| 5 | **Bond cash flows (bondization)** | ofz-swaps-v2 `ofz_workbench/moex.py:_official_cashflows` (completeness check) + currency-bond-monitor `schedule_source.py` / `schedules.py` (offers, horizon) | Bulk window: russian-real-money-positioning `bondization.json?from=&till=` (multi-cursor). **None of them paginate the per-SECID call.** |
| 6 | **Bond master / static** | russian-real-money-positioning `stock_bonds_all` collection loader; corporate-bond-monitor `iter_bond_universe_pages` | cbonds-research-connector (terms such as day count and BDC scraped from the UI); moex-market-data-mcp (separate audit) |
| 7 | **ZCYC / G-curve** | market-snapshot-reporter `sources.py` (`/engines/stock/zcyc.json`, last-available-day loop) | primary-bond-pricing-reports `calculate_peer_placement_metrics.py:curve`. Both read `yearyields` only, not the parameters. |
| 8 | **Candles (bonds / index)** | ofz-market-figures-reporter `moex.py:candles` + `complete_candles` | rgbi `rbu6-15m-2026/build.py` |
| 9 | **Index levels (RGBI, IMOEX, RUSFAR, RUSFARCNY)** | market-snapshot-reporter `sources.py:index_rate_quote` (live and history) | rgbi `d003/analyze_basis.py:moex_history` (RUSFAR term on MMIX) |
| 10 | **Repo (OFZ CCP repo, GC repo)** | rgbi `docs/wayfinder/.../d004/collect_evidence.py` (EQRP/EQWP/EQMP/EQTP, GCRP/GCOW/GCOM/GCTM, schema-drift guard) | rgbi `historical_front.py` (EQRP WAPRICE history). Research quality; needs hardening. |
| 11 | **RUONIA / key rate (CBR)** | rgbi `d003/analyze_basis.py:cbr_table/ruonia_history/key_rate_history` (keeps the publication date) | ofz-swaps-v2 `ofz_workbench/cbr.py` (fails closed). moex-market-data-mcp `cbr.py` is the fuller client (separate audit). Avoid ofz-rates-app `cbr_rate.py` (hard-coded 20% fallback). |
| 12 | **CBR meeting calendar** | ofz-swaps-v2 `ofz_workbench/cbr.py:parse_meeting_calendar_page` (Russian page) | cbr-policy-language (decision page → decision history) |
| 13 | **CBR official FX** | moex-bond-turnover `rates.py:fetch_cbr_rate_matrix` (`XML_dynamic.asp`, cached, 7-day as-of) | Fix: read `Nominal`/`VunitRate` (usdrub-proxy-research guesses the CNY unit) |
| 14 | **FX spot (CETS)** | moex-live-bonds `realtime.py:currency_snapshot` | market-snapshot-reporter `currency_quote` (public ISS) |
| 15 | **AlgoPack (tradestats / candles)** | usdrub-proxy-research `research.py:moex_rows` (`/iss/datashop/algopack/{fx,fo}/tradestats`) | ofz-swaps-v2 / ofz-analytics `moexalgo` candles |
| 16 | **RUB swap curves (RUONIA OIS, KRS)** | ofz-swaps-v2 `ofz_workbench/ratescalcs.py:RatescalcsClient` | ofz-rates-app `otc_monitor_sync.py` (validation rules for OTC-Monitor snapshots); implied-key-rates `parser.py` (pasted quotes); rgbi `d016/export_<desk>_curves_windows.py` (desk) |
| 17 | **Point-in-time curve resolution API** | rgbi `src/rgbi_monitor/current_comparison.py` (`/v1/curve-worlds/{id}/resolve`, as_known_then) | currency-bond-monitor `curve_loader.py` (CurveWorldService, 4-day staleness) |
| 18 | **QUIK bridge** | corporate-bond-monitor `src/corp_monitor/quik_session.py:_run_isolated` | ofz-tracker `backend/quik_trades.py` + `QUIK/lua/ofz_bulk.lua` (bulk all_trades export) |
| 19 | **Trading calendar (structure)** | rgbi `src/rgbi_monitor/calendars.py:OfficialCalendar` + rub-fixed-income-core `dates.py:BusinessCalendar` | **No data source anywhere**; the hub must build one (see section 3) |
| 20 | **CPI / macro (Rosstat, CBR)** | russia-inflation-nowcast `download.py`, `tls.py`, `parsers.py`, `gate.py` | cbr-policy-language `collect_macro.py` (do not copy `verify=False`) |
| 21 | **Ratings** | russian-rating-agencies-mcp `service.py` + `limits.py` | — |
| 22 | **Quota / rate limiting** | russian-rating-agencies-mcp `limits.py:RequestLimiter` | cbonds-research-connector minimum interval |
| 23 | **Raw-payload archive / provenance** | russia-inflation-nowcast `cached_download` vintages; rgbi `historical_front.py:Cache` | corporate-bond-monitor `analytics/universe.py` (raw SHA-256 + replay) |
| 24 | **CBR bulk files (RAR/DBF)** | bank-form101-positioning `core.py:122-334` | — |
| 25 | **G7 FX crosses** | market-snapshot-reporter `sources.py:YahooClient` (unofficial) | usdrub-proxy-research Dukascopy USD/CNH bi5 |

---

## 3. Specific call-outs

### 3.1 FX futures: Si / Eu / CR dated, USDRUBF / EURRUBF / CNYRUBF perpetual

| What | Where | Status |
|---|---|---|
| USDRUBF intraday (AlgoPack tradestats, `fo` market) | usdrub-proxy-research `research.py:49-85` (`moex_rows`), call at L235 | Only working FX-futures loader. Script quality. |
| FORTS generic securities and history paths | rgbi `run_s24_vertical_slice.py:128,158`, `d003/analyze_basis.py:396` (`/iss/engines/futures/markets/forts/securities.json?limit=5000`) | RB futures only, but the path and pattern carry over directly to Si/Eu/CR/*RUBF |
| FORTS front-contract chain and roll | rgbi `historical_front.py:47-77` (`FRONT_CHAIN`, `front_contract`) | Hard-coded table. The hub needs one generated from `/iss/securities/{secid}.json` LASTTRADEDATE/LASTDELDATE. |
| FX-futures pricing and risk model | rub-fixed-income-core `rub_fixed_income/fx_futures.py:40,86,237`; spec wish list in `docs/BACKLOG.md` items 33-34 | Model ready; needs a hub snapshot (spec terms, settle, spot, value date, both curves, calendar delivery date) |
| CNYRUBF / CNY-3.26 positions | trading-tracker `parsers/ronin-parser.js:435,703`, `scripts/load-real-data.js:13,28` | Parsing only, no market data |
| FORTS endpoint in legacy JS | trading-tracker `market-data/moex-service.js:12-17,186` | Buggy regex routing |
| **Si / Eu / CR dated, EURRUBF, CNYRUBF loaders** | — | **None exist. Build new.** |

### 3.2 CNY instruments

| What | Where |
|---|---|
| CNYRUB_TOM spot (CETS) | moex-live-bonds `realtime.py:261-282`; market-snapshot-reporter `sources.py:155-191` / `report.py:266`; usdrub-proxy-research AlgoPack `fx` tradestats |
| RUSFAR CNY (`RUSFARCNY` index, live and history) | market-snapshot-reporter `sources.py:193-247`, `report.py:221-223`. **The only RUSFARCNY loader.** |
| CNY-face bonds, trades and history | currency-bond-arbitrage-monitor `moex_source.py:14`, `daily_history.py:10` |
| CNY-face bonds, snapshots, schedules, pricing | currency-bond-monitor `levels.py`, `catalog.py`, `schedule_source.py` |
| CNY OFZ (hard-coded SECIDs) | ofz-tracker `scripts/daily_ofz_yield_report.py:34` |
| CNY-bond turnover, CBR R01375 conversion | moex-bond-turnover `rates.py:13-18` |
| CNY FX mark (point in time) | currency-bond-monitor `fx.py:69`; currency-bond-arbitrage-monitor `fx_source.py:33` (both read market-data-store `IntradayFxRepository`) |
| CNY curves: FR007 IRS (`cny_fr007_irs_base_v1`) and RUSFAR-CNY OIS (`cny_onshore_base_v1`) | currency-bond-monitor `curve_loader.py:15-16` (consumed from market-data-store; ingested there by `ingest-free-base-curves`) |
| USD/CNH offshore ticks | usdrub-proxy-research `research.py:37,112` (Dukascopy) |
| **CNY repo (CCP CNY repo boards), CNY OIS quote loader, xccy CNY/RUB** | **None.** The rgbi `d004` repo-board loader is the template to extend to CNY repo boards. |

### 3.3 Swap curves

| Curve | Where |
|---|---|
| RUB RUONIA OIS, Ratescalcs | ofz-swaps-v2 `ofz_workbench/ratescalcs.py:154` (`/cnyfuturesyields/key-rate-editor/ois`) |
| RUB key-rate swap (IRS vs key rate), Ratescalcs | ofz-swaps-v2 `ofz_workbench/ratescalcs.py:155` (`/cnyfuturesyields/key-rate-editor/krs`) |
| RUONIA OIS / KRS from OTC-Monitor and Andromeda (market-data-store DB) | ofz-rates-app `backend/services/otc_monitor_sync.py:86`, `scripts/ofz26249_ruonia_fwd_history.py:89` |
| desk-system RUONIA and RUB_KEY_RATE (desk) | rgbi `docs/wayfinder/.../d016/export_<desk>_curves_windows.py:31-46,178,204` |
| SPFI `/iss/sdfi/curves.json` (RUB-OIS-RUSFON, RUB-OIS-RFOQTE, RUB-DSC-CSARUB) | rgbi `d003/analyze_basis.py:413`. Public access is metadata only; values need entitlement. |
| Pasted OIS quotes | implied-key-rates `parser.py` (and copies) |
| KRS / OIS pricing and bootstrap maths | rub-fixed-income-core `key_rate.py`, `discount.py:179`; ofz-rates-app `keyrate_math.py:19` |
| USD SOFR OIS, EUR €STR OIS, CNY FR007 IRS, CNY RUSFAR OIS | Consumed in currency-bond-monitor `curve_loader.py:12-17`; loaders are in market-data-store |
| **xccy CNY/RUB, CNY OIS quote ingestion, RUSFAR-based RUB OIS quotes** | **None in these repos** |

### 3.4 G7 FX crosses

- market-snapshot-reporter `src/market_snapshot_reporter/sources.py:296-302`, Yahoo symbols EURUSD=X, JPY=X, GBPUSD=X, CHF=X, CNY=X, DX-Y.NYB. This is an unofficial endpoint, so treat it as best-effort.
- Nothing from CBR cross rates, MOEX CETS crosses (EURUSD_TOM / USDCNY_TOM) or a licensed feed. The hub could add MOEX `CETS` crosses through the same `currency_snapshot` path, and derive CBR crosses from `XML_daily`.

### 3.5 MOEX trading calendar

| Implementation | Where | Verdict |
|---|---|---|
| `OfficialCalendar` from a hash-verified snapshot; `DateRole`, `ContractDateRoles.from_expiry`; fail-closed coverage | rgbi `src/rgbi_monitor/calendars.py:40-103` | **Best structure**; no fetcher |
| `SessionSchedule` and intraday slot grid with halts | rgbi `src/rgbi_monitor/tape.py:129-285` | Reuse for intraday completeness checks |
| `BusinessCalendar(holidays, working_days, version)` + `calendar_fingerprint` | rub-fixed-income-core `dates.py:32`, `conventions.py:118` | Canonical consumer type; **expects the hub to supply the data** (BACKLOG P0 #1) |
| 2026 production calendar (Government Resolution 1466) | ofz-swaps-v2 `ofz_workbench/calendar_data.py:18-37` | Seed data for 2026 only; raises for other years |
| Rule-based Moscow holidays | implied-key-rates `implied_key_rates/moscow.py` (copies in ofz-swaps-v2 and ofz-rates-app) | Do not reuse: approximate perenos and the spurious Radunitsa |
| Empirical "latest completed session" probe | ofz-market-figures-reporter `moex.py:120`; market-snapshot-reporter 8-day loop | Useful as a runtime cross-check |
| Weekday-only | moex-bond-turnover, currency-bond-monitor, ofz-tracker, corporate-bond-monitor, trading-tracker `settlement.py:35-50` | Replace with the hub calendar |

**Gap.** No repo fetches MOEX's official trading-day data. Candidates are ISS `/iss/calendars` or `/iss/engines/{engine}/markets/{market}/...` trading-session info, and the moex.com trading-calendar pages. The hub should ingest that data plus the government production calendar, version it, and serve a `BusinessCalendar`-compatible payload.

---

## 4. Cross-cutting quality issues the hub should fix centrally

- **Duplicated code:**
  - `ofz_analytics/` is copied three times (ofz-analytics, ofz-rates-app, ofz-tracker) and the copies have diverged in `market_data.py`.
  - `implied_key_rates/` is copied three times.
  - `moex_auth.py` is copied twice.
  - The ISS `{columns,data}`→dict helper has 5+ copies.
  - The `history.cursor` loop has 4+ copies.
  - The bondization fetch has 4 copies, none of them paginated.
  - The CBR key-rate scraper has 3 copies.
  - The proxy auto-detect block (a loopback proxy port → `HTTP(S)_PROXY` at import) is in about 12 files.
- **Spoofed User-Agent** "OpenAI File Downloader, XaiImageApiFetch/1.0" in moex-live-bonds, moex-bond-turnover, currency-bond-monitor, ofz-market-figures-reporter, rgbi and russia-inflation-nowcast. Replace it with an honest hub UA.
- **Sibling-file coupling:** at least 8 repos open `../market-data-store/data/market-data.sqlite3` or `../moex-market-data-mcp/data/bond_catalog.sqlite3` directly, often with `<workspace>` absolute paths. The hub should expose APIs instead and keep those schemas behind it.
- **Pagination pitfalls:**
  - Live board endpoints ignore `start`/`limit`, so the legacy `ofz_analytics` loops can run forever.
  - ofz-tracker `historical_data.py` does not paginate TQOB history.
  - bondization `limit=500/1000` is never paged.
- **Silent fallbacks:**
  - ofz-rates-app `cbr_rate.py` returns a hard-coded 20% key rate.
  - The CBR CNY nominal is guessed in usdrub-proxy-research.
  - Unknown Cbonds coupon labels become "fixed".
- **Live-endpoint scripts** (not in pytest):
  - corporate-bond-monitor `tools/windows_acceptance/run_windows_acceptance.py`
  - russian-rating-agencies-mcp `scripts/live_smoke.py`
  - ofz-analytics `example.py`
  - trading-tracker `test-moex.js`

  All pytest suites are offline (MockTransport, respx, monkeypatch).
- **Consumer contracts the hub must honour:**
  1. trading-tracker: `/v1/prices/{id}/current|eod` and the `MARKET_DATA_REFRESH_MARKER` file.
  2. rgbi: `/v1/curve-worlds/{recipe}/resolve?decision_time&knowledge_policy=as_known_then` on 127.0.0.1:8765.
  3. rub-fixed-income-core: `bind_market_data_store_schedule` schedule JSON and `FixingHistory` (ruonia / key_rate with `known_at`).
  4. currency-bond-monitor and currency-bond-arbitrage-monitor: `CurveWorldService.resolve` and `IntradayFxRepository.get_reference` semantics.
