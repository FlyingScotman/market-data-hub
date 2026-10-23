# 05 — Connectivity test: market and static data sources for the market data hub

- **Run:** 2026-10-15, 19:28–19:37 MSK (Thursday). The MOEX evening session was open for shares, futures and OFZ (TQOB `TRADINGSTATUS=T`). CETS `CNYRUB_TOM` had stopped at 19:00, and CETS FX was `N` for most instruments.
- **Method:** read-only GET requests plus authentication bootstraps. The one exception is Cbonds, whose API only accepts POST for its read-only query methods (see §4). There were about 75 requests in total, with at least 0.6 s between requests to the same host and no retry loops.
- **Secrets:** credentials were taken only from env vars (`MOEX_API_KEY`, `MOEX_PASSPORT_LOGIN/PASSWORD`, `CBONDS_LOGIN/PASSWORD`, `RATESCALC_USERNAME/PASSWORD`). They were sent only in an Authorization header, through HTTP Basic auth or in a POST body. No URL carries a secret. A scan of the output files found no credential values.
- **Probe code:** `scratchpad/conn/probe.py`, with sections `moex_public | moex_passport | moex_apim | cbonds | ratescalcs | cbr | ecb | research | curve <mode> | raw <mode>`. The other scripts are `cbonds2.py` and `ratescalcs_{index,xccy,snap}.py`. Raw results without secrets are in `scratchpad/conn/results/*.json`.
- **TLS:** `iss.moex.com` and `apim.moex.com` need the **Russian Trusted Root CA**. Copy it from `market-data-store/src/market_data_store/certs/russian_trusted_root_ca.pem` and add it to the **certifi** bundle. The uv-managed Python lacks system roots, so cbr.ru and ECB failed with `CERTIFICATE_VERIFY_FAILED` until certifi was loaded.

## 1. Matrix: service × credential → status

| # | Service / endpoint family | Credential | Auth method | HTTP | Latency | Freshness | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | MOEX ISS public `iss.moex.com/iss` (bonds, FX, futures, indices, ZCYC, history, statistics) | none | — | 200 | 16–440 ms (typically 50–110) | **15-min delayed** marketdata (`SYSTIME−UPDATETIME = 900 s`); indices, ZCYC and statistics are current | **OK** |
| 2 | MOEX Passport (`passport.moex.com/authenticate` and ISS Basic → cookie) | MOEX_PASSPORT_LOGIN/PASSWORD | Basic → `MicexPassportCert` cookie | 200 | 80–170 ms | **Still 15-min delayed** for shares, futures, FX and OFZ. Order books show the HTML "subscribers only" notice. | **Auth OK, no realtime entitlement** |
| 3 | MOEX APIM / AlgoPack `apim.moex.com/iss` | MOEX_API_KEY | `Authorization: Bearer` | 200 (401 without a token) | 30–370 ms | **Realtime for shares and futures** (lag 0–1 s, SBER 20-level order book). **FX and bonds stay 15-min delayed.** Order books for FX and bonds are not entitled. | **OK (partial entitlement)** |
| 4 | MOEX SDFI swap curves `/iss/sdfi/curves/{id}` | none / Passport / APIM | — | 200 `text/html` | 30–120 ms | Catalogue `/iss/sdfi/curves.json` is free (72 curves). Every curve body returns the "subscribers only" notice under all 3 credentials. | **Catalogue only; values need a subscription** |
| 5 | Cbonds JSON API `ws2.cbonds.info/services/json` (also `ws.`) | CBONDS_LOGIN/PASSWORD | JSON body `auth{login,password}` (POST) | **401**, empty body | 20–710 ms | — | **FAIL: auth rejected** |
| 6 | Ratescalcs `ratescalcs.ru/cnyfuturesyields/...` | RATESCALC_USERNAME/PASSWORD | HTTP Basic | 200 | 20–170 ms | Dealer runs, no quote timestamp. The JSON `snapshot` has a `timestamp` and `market_closed` flag. | **OK** |
| 7 | CBR `www.cbr.ru` (hd_base HTML, XML_daily, DailyInfo WSDL) | none | — | 200 (WSDL 500 if `?WSDL=`; plain `?WSDL` gives 200) | 80–410 ms | Daily, T+1 for RUONIA | **OK** |
| 8 | ECB eurofxref daily XML and ECB Data API | none | — | 200 | ~300 ms | Daily, about 16:00 CET | **OK** |
| 9 | Andromeda Terminal | not provided | Playwright personal session | — | — | — | **Not tested** (personal browser session, no API credentials) |
| 10 | OTC Monitor `otc-monitor.ru/api/bond/` (used by market-data-store) | `OTC_MONITOR_TOKEN` (in env, 32 chars) | `Authorization: Bearer` | 200 JSON (401 JSON without token) | — | ~440 rows `{isin, series, bid, offer, mid, date_update}` (MSK wall clock); current state only — **no history** (`?date=` ignored, `/api/history` 404). Curves: OIS RUONIA (17), IRS KeyRate (13), XCCY CNYRUONIA (14), OIS RUSFARCNY (8) | **OK** (verified 2026-10-16 ~18:00 MSK, read-only GET; correction of the 2026-10-15 "not tested": the token is in env) → loader `swaps_otc_monitor` |
| — | APIM_KEY (duplicate) | not provided by design | — | — | — | — | n/a |

## 2. MOEX ISS public (no auth): details

| Probe | Endpoint (`https://iss.moex.com/iss` + …) | Result | Sample fields / latest data |
|---|---|---|---|
| OFZ list | `/engines/stock/markets/bonds/boards/TQOB/securities.json?iss.only=securities,marketdata` | 61 OFZ | `SECID, SHORTNAME, MATDATE, COUPONPERCENT, PREVPRICE, PREVDATE`; md `BID, OFFER, LAST, YIELD, UPDATETIME, SYSTIME`; PREVDATE 2026-10-14 |
| All bonds | `/engines/stock/markets/bonds/securities.json` | 3,481 rows | Rows by board: TQCB 3,024, TQOD 222, TQRD 75, TQOB 61, TQOY 57, SPOB 40 |
| FX CETS | `/engines/currency/markets/selt/boards/CETS/securities.json` | 200 instruments | TOD/TOM/SPT plus swaps (`CNYRUB_TOM1W..6M`, `CNYRUBTODTOM`, `EURRUB_TOM1Y`, …). G7 crosses: `EURUSD000TOD/TOM`, `GBPUSD_TOD/TOM`, `USDCNY_TOD/TOM`, CHF/GBP/AED…RUB. Market boards on selt: CETS, CNGD, FIXN, FIXS, LICU, SDBP, WAPN, WAPS. |
| Futures | `/engines/futures/markets/forts/boards/RFUD/securities.json` | 497 contracts | Fronts: SiZ6, CRZ6, EuZ6, EDZ6, RBZ6 (RGBI), KKV6 (KEYRATE), MMZ6. RUONIA futures asset also present. Delay 901 s. |
| Indices | `/engines/stock/markets/index/securities.json` | 904 indices | **Realtime** (RGBI/IMOEX `TIME` ≈ now). RUSFAR family: `RUSFAR, RUSFAR1W/2W/1M/3M, RUSFARRT, RUSFARCNY, RUSFARCNRT, RUSFARUSD…`, `MOEXREPO*`, `RUCBI*`, RGBI/RGBITR. RUSFAR is fixed at 12:30 and RUSFARRT at 18:00. **No RUONIA index on ISS** (take it from CBR). |
| RUSFAR stats | `/statistics/engines/stock/markets/index/rusfar.json` | 200 | Blocks `analytics, analytics.columns, analytics.dates` (trades count, volumes) |
| Index history | `/history/engines/stock/markets/index/securities/RUSFAR.json?from=…` | 8 rows | Last close 2026-10-14 = 13.83 |
| ZCYC | `/engines/stock/zcyc.json` | 200 | Blocks `params` (Nelson–Siegel B1..B3, T1, G1..G9; tradetime 18:54:59 today), `yearyields`, `securities`, `maxdates` |
| OFZ history | `/history/engines/stock/markets/bonds/boards/TQOB/securities.json` | 200 | `TRADEDATE, CLOSE, YIELDCLOSE, VOLUME, DURATION`; latest 2026-10-14 |
| FX fixing / CNY ON | `/statistics/engines/currency/markets/fixing.json` | 200 | `SRATE_CNY_ON` 0.00468 (today) |
| **Indicative FX rates** | `/statistics/engines/futures/markets/indicativerates/securities.json` | 200 | CAD, CHF, CNY, EUR, GBP, HKD, INR, JPY, KZT, TRY, USD vs RUB, at 14:00 (`tc`) and 19:00 (`mc`) clearing. **Usable for G7 crosses.** |
| CBR rates mirror | `/statistics/engines/currency/markets/selt/rates.json` | 200 | `CBRF_USD_LAST`, `CBRF_EUR_LAST` for the next day (2026-10-16), WAP rates |
| CBR repo auctions | `/statistics/engines/state/rates.json` | 200 | Blocks `auctions`, `fixed` (1D/1W repo rates) |
| SDFI catalogue | `/sdfi/curves.json` | 200 | 72 curve ids (see §7) |

## 3. Passport compared with APIM: realtime check (same minute, 19:32–19:34 MSK)

| Instrument | Public | Passport (cookie) | APIM (Bearer) |
|---|---|---|---|
| SBER TQBR marketdata | 900 s lag | 901 s lag | **0 s** (realtime) |
| SBER order book | — | HTML "subscribers only" | **JSON, 20 levels** `BUYSELL, PRICE, QUANTITY, SEQNUM, UPDATETIME` |
| SiZ6 / RFUD marketdata | 901 s | 901 s | **1 s** (realtime) |
| OFZ SU26238 TQOB marketdata | 900 s | 900 s | 901 s (delayed) |
| OFZ order book | — | HTML notice | HTML notice |
| OFZ current-session trades (`/trades.json?reversed=1`) | — | JSON, but the last trade is 15 min old | — |
| CETS USD000UTSTOM / CNYRUB_TOM | 900 s | 900–901 s | 900 s (delayed) |
| CNYRUB_TOM order book | — | HTML notice | HTML notice |
| TQCB corporate marketdata | — | — | 900 s (delayed) |
| 1-min candles SBER (`/engines/stock/markets/shares/securities/SBER/candles.json?interval=1`) | — | — | Realtime: the open 19:32 candle at 19:32:43 |

**Passport conclusion:** login works. Both `passport.moex.com/authenticate` and Basic auth on ISS set `MicexPassportCert`. However, this account currently has **no** realtime or order-book subscription. In earlier repos (moex-live-bonds, ofz-tracker V69, 2026-08/09), Passport profiles were realtime for OFZ. That subscription has expired, or this is a different login. Do not treat Passport as a realtime source until MOEX re-entitles it.

**APIM / AlgoPack datasets** (all HTTP 200 JSON with Bearer):

| Dataset | Path (`https://apim.moex.com/iss` + …) | Status / freshness | Fields (sample) |
|---|---|---|---|
| Super candles eq tradestats | `/datashop/algopack/eq/tradestats/SBER.json?date=YYYY-MM-DD` | OK. The 19:30 bucket was available at 19:33. | `tradedate, tradetime, secid, pr_open..pr_close, pr_std, vol, val, trades, pr_vwap, trades_b/s, val_b/s, vol_b/s, disb, SYSTIME` |
| eq orderstats | `/datashop/algopack/eq/orderstats/SBER.json` | OK (schema; use `date=`) | `put_orders_b/s, put_val_*, cancel_*` |
| eq obstats | `/datashop/algopack/eq/obstats/SBER.json` | OK | `spread_bbo, spread_lv10, spread_1mio, levels_b/s, imbalance_*, vwap_*_1mio` |
| fo tradestats / obstats | `/datashop/algopack/fo/{tradestats,obstats}[/SECID].json` | OK; obstats SiZ6 has 20 levels | `asset_code, oi_open..oi_close, im`; obstats `mid_price, micro_price, spread_l1..l20, vol_b_l*` |
| fx tradestats / obstats | `/datashop/algopack/fx/{tradestats,obstats}/CNYRUB_TOM.json?date=…` | OK. The last bucket was 19:00, when the CNY TOM session ended. | `mid_price, micro_price, spread_l1..l10` |
| eq hi2 | `/datashop/algopack/eq/hi2.json?date=…` | OK | `metric (hhi_buy/sell…), value` |
| FUTOI | `/analyticalproducts/futoi/securities/si.json` | OK, 298 rows today | `clgroup (YUR/FIZ), pos, pos_long, pos_short, *_num` |
| ZCYC | `/engines/stock/zcyc.json` | OK (same as public) | — |

AlgoPack notes:
- Pagination uses `start=` together with the `data.cursor` block (`INDEX, TOTAL, PAGESIZE`). Without `start`, the response is the first page (07:00 onward) and `limit=1` is ignored.
- When an entitlement is missing, the response is HTTP 200 `text/html` with "Информация доступна только подписчикам", not a 4xx. Classify any non-JSON 200 as `entitlement_missing`.

## 4. Cbonds (`CBONDS_LOGIN/PASSWORD`): FAIL

- **Contract** (from `moex-market-data-mcp/src/moex_market_data/cbonds.py`): `POST https://ws2.cbonds.info/services/json/{operation}/` with body `{"auth":{"login","password"},"filters":[{"field":"isin_code","operator":"eq","value":…}],"quantity":{"limit","offset"},"sorting":[],"fields":[{"field":…}]}`. Operations used: `get_emissions`, `get_flow_new`, `get_offert`, `get_rating_emission_maxdate`.
- **Result:** HTTP **401** with an empty `text/html` body on both `ws2.` and `ws.cbonds.info`, for `get_emissions` and `get_flow_new`.
  - The credentials have no stray whitespace.
  - A control request with a dummy login also returned 401 with an empty body. The rejection is therefore at the gateway level. Likely causes, in order: wrong or expired account, an **IP allow-list** (the API is licensed per account and often restricted by IP), or a different API host for this contract.
  - In August 2026 this API returned JSON `{"error":{"err_no":…}}` for provider errors, which is different from this behaviour.
- **Action:** ask Cbonds support to confirm the account status and allowed IPs for the hub host. Then rerun `uv run --no-project --with httpx python3 -I conn/cbonds2.py`, which sends 2 requests.

## 5. Ratescalcs (`RATESCALC_USERNAME/PASSWORD`, HTTP Basic): OK

| Page (GET only) | Status | Content |
|---|---|---|
| `/cnyfuturesyields/key-rate-editor/ois` | 200 html, 170 ms | **RUONIA OIS run** bid/ask 1W–10Y. Example: 1W 13.70/13.90, 1Y 14.32/14.51, 10Y 15.61/15.81. |
| `/cnyfuturesyields/key-rate-editor/krs` | 200 html, 19 ms | **Key Rate Swaps (IRS vs key rate)** bid/ask from 3M. Example: 1Y 14.27/14.47. |
| `/cnyfuturesyields/curve` | 200 html | Same RUONIA OIS run, inside an editor `<textarea>` |
| `/cnyfuturesyields/cny-xccy-curve` | 200 html | **CNY XCCY run (CNY fixed vs RUONIA)** bid/ask: ON 0.1/0.3, 1W 0.50/1.45, 1M 2.40/3.10, 3M 4.80/5.50, 6M 6.30/7.00, 12M 7.80/8.70 |
| `/cnyfuturesyields/snapshot` | 200 **JSON**, 62 ms, 34 KB | See the snapshot fields below |
| `/cnyfuturesyields/` (dashboard) | 200 html | Lists other GET JSON routes: `stream` (SSE), `cny-yield-history`, `key-rate-path`, `fwd-start-rate/{tenors,bonds,spread}`, `twap-history.csv`, `key-rate-path.xlsx`. Its admin routes and editor forms use POST and **must not be called**. |

- **Snapshot fields:**
  - `timestamp`, `market_closed`, `ois_loaded_at`
  - `cny_xccy_curve[] {tenor, pillar_date, days, bid, offer, mid, solved_mid, diff_bp}`, `cny_xccy_zero[]`, `cny_xccy_fwd[]`
  - `results[]` / `cny_results[]` / `usd_results[]` `{future_ticker, future_settle, days, spot_bid/offer, future_bid/offer, ois_bid/offer, yield_bid/offer}`: futures-implied CNY and USD yields
  - `ntpro_twap`, `usdrubf_vwap`, `cal_spreads[]`
- **Limits:**
  - Pages carry no quote timestamp; the snapshot has `timestamp` and `ois_loaded_at`.
  - Service hours are Mon–Fri 08:50–23:00 MSK. The snapshot reported `market_closed=true` at 19:00.
  - Stay on GET only. The README forbids POST pages.
- **Hygiene:** credentials are kept in server secrets; follow-up is tracked in the private repository.

## 6. CBR and ECB (public)

| Endpoint | Status | Latest data | Notes |
|---|---|---|---|
| `https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True&UniDbQuery.From=dd.mm.yyyy&UniDbQuery.To=…` | 200, 414 ms | 15.10.2026 key rate **14.00** | HTML table `Дата, Ставка` |
| `…/hd_base/ruonia/dynamics/?…` | 200, 350 ms | 14.10.2026 RUONIA **13.91** (published 15.10) | Also volume, trades, participants, percentiles, status, publication date |
| `…/hd_base/bliquidity/?…` | 200, 149 ms | Daily | Liquidity deficit/surplus table with multi-row header |
| `https://www.cbr.ru/scripts/XML_daily.asp?date_req=dd/mm/yyyy` | 200, 80 ms, windows-1251 XML | `ValCurs Date="15.10.2026"`, 54 currencies (USD 83.9497, EUR 93.9229, GBP 111.225) | Use `<Valute>` blocks: `CharCode, Nominal, Value, VunitRate`. Tomorrow's fixing is already available (ISS shows CBRF_USD for 2026-10-16 = 83.76). |
| `https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx?WSDL` | 200 (94 ops) | — | Operations include `KeyRate(XML)`, `Ruonia(XML)`, `RuoniaSV`, `Bliquidity(XML)`, `ROISfix(XML)` (archive), `Repo_debt*`, `SwapDynamic`, `Saldo`, `DV`, `GetCursOnDate(XML)`. **SOAP POST only**: the HTTP GET binding is disabled, and a GET to `/KeyRateXML` returns an error page. POST was not tested, per the GET-only rule. |
| Term RUONIA (used by market-data-store) | not called | — | `/Queries/UniDbQuery/DownloadExcel/125022?Posted=True&From=…&To=…&FromDate=MM/DD/YYYY&ToDate=…` (xlsx) |
| `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` | 200, 302 ms | 2026-10-15, 29 ccys: USD 1.1208, JPY 177.06, GBP 0.84610, CHF 0.9305, CAD 1.5923, AUD 1.6218, CNY 7.4993 | Daily around 16:00 CET. History is in `eurofxref-hist.zip`. |
| `https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?lastNObservations=1&format=csvdata` | 200, 298 ms | 2026-10-15 1.1208 | SDMX CSV |

## 7. Research: swaps, xccy and G7 FX — where to source each item

| Need | Best source found | Reachable now with our credentials? |
|---|---|---|
| **IRS vs RUONIA (OIS RUONIA)** | (a) Ratescalcs OIS run, 1W–10Y bid/ask. (b) MOEX SDFI `RUB-OIS-RUONIA` (curve) and `RUB-OIS-RUOQTE` (best bid/ask). | (a) **Yes.** (b) Catalogue only; values need an SDFI subscription. |
| **IRS vs key rate (KRS / IRS KEYRATE)** | (a) Ratescalcs KRS run. (b) MOEX SDFI `RUB-IRS-KEYRTE` / `RUB-IRS-KEYQTE` and `RUB-FWC-KEYRTE`. (c) MOEX KEYRATE futures (`KKV6`…). | (a) **Yes.** (b) Subscription needed. (c) **Yes** (APIM realtime, public delayed). |
| **XCCY CNY/RUB (CNY fixed vs RUONIA)** | (a) Ratescalcs `cny-xccy-curve` page and `snapshot.cny_xccy_curve`. (b) MOEX SDFI `CNY-XCS-CNYRUO` / `CNY-XCS-CNRQTE` (bid/ask), `CNY-HYB-CNYRUO`. (c) Implied from CETS FX swaps `CNYRUB_TOM1W…6M` and CR futures. | (a) **Yes.** (b) Subscription needed. (c) **Yes** (FX delayed 15 min; CR futures realtime on APIM). |
| **CNY OIS vs RUSFAR CNY** | (a) MOEX SDFI `CNY-OIS-RUSFCN` / `CNY-OIS-RFCQTE`, `CNY-FWC-RUSFCN`. (b) Fixings `RUSFARCNY`, `RUSFARCNRT`, `RUSFARC1WN`… on the ISS index market. (c) Ratescalcs futures-implied CNY yields (`cny_results`). | (a) Subscription needed. (b) **Yes**, public. (c) **Yes.** |
| **RUSFAR OIS / IRS RUSFAR 3M** | MOEX SDFI `RUB-OIS-RUSFON`, `RUB-OIS-RFOQTE`, `RUB-IRS-RUSF3M` | Subscription needed. Fixings are public on ISS. |
| USD/RUB xccy (SOFR v RUB) | MOEX SDFI `RUB-XCS-SFRRUB` | Subscription needed |
| NFEA / ROISfix | NFEA's ROISfix (RUONIA OIS fixing) is **discontinued**. The CBR keeps an archive for 2011-04-15 to 2023-03-01 at `cbr.ru/hd_base/roisfix/` and in DailyInfo `ROISfix`. | Archive only |
| **G7 FX crosses** | (a) ECB eurofxref daily: EUR vs USD, JPY, GBP, CHF, CAD, AUD and more. Cross any pair as `X/Y = (EUR/Y)/(EUR/X)`. (b) MOEX indicative rates (`indicativerates`): CAD, CHF, CNY, EUR, GBP, HKD, INR, JPY, KZT, TRY, USD vs RUB at 14:00 and 19:00 MSK; derive crosses through RUB. (c) MOEX CETS tradable crosses: `EURUSD000TOM/TOD`, `GBPUSD_TOM/TOD`, `USDCNY_TOM/TOD`. (d) CBR XML_daily: 54 currencies vs RUB. | **Yes, all public.** Use ECB as the G7 reference and MOEX or CBR for RUB legs. |

**MOEX SDFI curve catalogue (free):** `GET https://iss.moex.com/iss/sdfi/curves.json` returns `curveid, date_from, date_till, title, methodology, unit` for 72 curves (RUB, CNY, EUR, USD discount, forward, OIS, IRS, XCS and quote curves). All values were updated through 2026-10-15. Values sit behind `GET /iss/sdfi/curves/{curveid}.json`, which returned the "subscribers only" notice for public, Passport and APIM alike. MOEX describes these curves as T+0, bid/ask/mid weighted over 3-minute order-book snapshots (methodology files at fs.moex.com/files/26130 and 27002). **Recommendation:** an SDFI data subscription is the single cleanest licensed source for OIS RUONIA, IRS KEYRATE, XCS CNYRUB and OIS RUSFARCNY with history. Until then, use the Ratescalcs runs and snapshot.

## 8. Limits and operational notes

- **ISS public:**
  - There is no documented hard rate limit. Stay at or below ~2–3 req/s and reuse the connection.
  - Responses are capped at 100 rows per page for `history` endpoints (use `start=`). Market-wide `securities.json` returns the full board.
  - Use `iss.meta=off`, `iss.only=` and `<block>.columns=` to keep payloads small (3,024 TQCB rows = 279 KB).
- **Freshness rule:**
  - Realtime means `SYSTIME − UPDATETIME ≈ 0`; delayed means ≈ 900 s.
  - Indices (RGBI, IMOEX) are realtime on public ISS. RUSFAR is fixed at 12:30 MSK and RUSFARRT at 18:00.
  - ZCYC params update intraday; the last was at 18:54:59.
- **APIM:**
  - Missing or invalid tokens return 401 JSON `{"message":"Unauthorized","http_status_code":401}`. A token without entitlement returns 200 with an HTML notice.
  - The base URL `https://apim.moex.com/iss` uses the same paths as ISS.
- **Passport:**
  - `MicexPassportCert` is valid for the session. Bootstrap once with Basic auth, then use only the cookie (pattern from `moex-live-bonds/realtime.py`).
  - Earlier docs note that `passport.moex.com` was unreachable from the Linux server. Basic auth directly on `iss.moex.com` works and is the preferred route.
- **CBR:** HTML scraping with `UniDbQuery.*` params, as in `market-data-store/cbr.py`. That client rate-limits itself; keep a minimum interval of about 1 s. Use `User-Agent: curl/8.0.0`, which the reference code uses.
- **Ratescalcs:** use only GET on the pages above. Do not touch `admin/*` or editor POST forms. `stream` is an SSE feed and was not opened.
- **TLS:** pin the Russian Trusted Root CA for MOEX hosts and use certifi for everything else.

## 9. Reusable minimal clients (no secrets; env-var driven)

```python
# common.py
import os, ssl, certifi, httpx
RU_CA = "certs/russian_trusted_root_ca.pem"   # from market-data-store

def tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=RU_CA)
    return ctx

def iss_rows(payload: dict, block: str) -> list[dict]:
    b = payload.get(block) or {}
    return [dict(zip(b.get("columns", []), r)) for r in b.get("data", [])]

def require_json(r: httpx.Response) -> dict:
    r.raise_for_status()
    if "json" not in r.headers.get("content-type", ""):
        raise PermissionError("MOEX entitlement missing (HTML subscriber notice)")
    return r.json()
```

```python
# MOEX ISS public (15-min delayed marketdata; indices/ZCYC/statistics current)
with httpx.Client(base_url="https://iss.moex.com/iss", verify=tls(), timeout=30,
                  headers={"Accept": "application/json"}) as c:
    d = require_json(c.get("/engines/stock/markets/bonds/boards/TQOB/securities.json",
                           params={"iss.meta": "off", "iss.only": "marketdata",
                                   "marketdata.columns": "SECID,BID,OFFER,LAST,YIELD,UPDATETIME,SYSTIME"}))
    md = iss_rows(d, "marketdata")
```

```python
# MOEX APIM / AlgoPack (Bearer from env MOEX_API_KEY) — realtime shares/futures, super candles, FUTOI
apim = httpx.Client(base_url="https://apim.moex.com/iss", verify=tls(), timeout=30, follow_redirects=False,
                    headers={"Accept": "application/json",
                             "Authorization": f"Bearer {os.environ['MOEX_API_KEY']}"})
d = require_json(apim.get("/datashop/algopack/fo/obstats/SiZ6.json",
                          params={"iss.meta": "off", "date": "2026-10-15", "start": 0}))
rows, cursor = iss_rows(d, "data"), iss_rows(d, "data.cursor")   # paginate with start=
```

```python
# MOEX Passport (Basic once → MicexPassportCert cookie; then cookie only)
pp = httpx.Client(base_url="https://iss.moex.com/iss", verify=tls(), timeout=30, follow_redirects=False,
                  headers={"Accept": "application/json"})
r = pp.get("/index.json", params={"iss.meta": "off", "iss.only": "engines"},
           auth=httpx.BasicAuth(os.environ["MOEX_PASSPORT_LOGIN"], os.environ["MOEX_PASSPORT_PASSWORD"]))
assert "MicexPassportCert" in pp.cookies, "Passport auth failed"
# subsequent pp.get(...) calls carry only the cookie. Check SYSTIME-UPDATETIME to confirm realtime.
```

```python
# Cbonds (read-only query methods are POST with body auth) — currently 401, see §4
def cbonds(op: str, filters: list, fields: list[str], limit=10, offset=0) -> dict:
    body = {"auth": {"login": os.environ["CBONDS_LOGIN"], "password": os.environ["CBONDS_PASSWORD"]},
            "filters": filters, "quantity": {"limit": limit, "offset": offset},
            "sorting": [], "fields": [{"field": f} for f in fields]}
    r = httpx.post(f"https://ws2.cbonds.info/services/json/{op}/", json=body, timeout=30)
    r.raise_for_status(); d = r.json()
    if d.get("error"): raise RuntimeError(f"Cbonds error {d['error'].get('err_no')}")
    return d   # {count,total,limit,offset,items}
# cbonds("get_emissions", [{"field":"isin_code","operator":"eq","value":"SU26238RMFS4"}], ["id","isin_code","maturity_date"])
```

```python
# Ratescalcs (HTTP Basic, GET only)
rc = httpx.Client(base_url="https://ratescalcs.ru/cnyfuturesyields/", timeout=20, follow_redirects=False,
                  auth=httpx.BasicAuth(os.environ["RATESCALC_USERNAME"], os.environ["RATESCALC_PASSWORD"]))
snap = rc.get("snapshot").json()                 # cny_xccy_curve, cny_results, usd_results, timestamp, market_closed
import re
html = rc.get("key-rate-editor/ois").text        # also: key-rate-editor/krs, cny-xccy-curve (textarea)
text = re.sub(r"<[^>]+>", "\n", html)
quotes = re.findall(r"^\s*(\d+[DWMY])\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s*$", text, re.M | re.I)
```

```python
# CBR (public)
cbr = httpx.Client(base_url="https://www.cbr.ru", verify=certifi.where(), timeout=30,
                   headers={"User-Agent": "curl/8.0.0"})
q = {"UniDbQuery.Posted": "True", "UniDbQuery.From": "01.10.2026", "UniDbQuery.To": "15.10.2026"}
key_html = cbr.get("/hd_base/KeyRate/", params=q).text           # also /hd_base/ruonia/dynamics/, /hd_base/bliquidity/
xml = cbr.get("/scripts/XML_daily.asp", params={"date_req": "15/10/2026"}).content
fx = {m[0].decode(): float(m[1].decode().replace(",", "."))
      for m in re.findall(rb"<Valute[^>]*>.*?<CharCode>(\w+)</CharCode>.*?<VunitRate>([\d,]+)</VunitRate>", xml, re.S)}
```

```python
# ECB daily reference rates → G7 crosses
x = httpx.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", verify=certifi.where()).text
eur = {k: float(v) for k, v in re.findall(r"currency='(\w+)' rate='([\d.]+)'", x)} | {"EUR": 1.0}
cross = lambda base, quote: eur[quote] / eur[base]     # e.g. cross("USD","JPY") = USDJPY
```

## 10. Action items

1. **Cbonds:** confirm the account and IP allow-list with Cbonds; credentials are currently rejected (401).
2. **MOEX Passport:** the account authenticates but has no realtime or order-book entitlement. Re-subscribe, or confirm which login holds the realtime bond and FX subscription.
3. **MOEX APIM:** realtime covers shares and futures plus AlgoPack super candles and FUTOI. Bonds and FX are delayed, and there is no FX or bond order book. Realtime CNY spot should therefore come from APIM `fx/obstats` 5-min `mid_price`, CR futures, or a Passport FX subscription.
4. **Swap curves:** evaluate a MOEX SDFI curves subscription for OIS RUONIA, IRS KEYRATE, XCS CNYRUB and OIS RUSFARCNY with bid/ask and history. Ratescalcs covers all three in the meantime.
5. **G7 FX:** ECB eurofxref (free) for G7 crosses; MOEX `indicativerates` and CBR for RUB legs.
6. **Not tested:** Andromeda (personal Playwright session). OTC Monitor was re-tested on 2026-10-16 with `OTC_MONITOR_TOKEN` from env: 200 OK, no history endpoint (row 10); live loader `swaps_otc_monitor`.
