# 02 — Bond static (reference data) audit: moex-market-data-mcp and its siblings

As of 2026-10-15. Audited code: `ref/moex-market-data-mcp` at commit `326b1f5` (merge of `6a95e62`, "Publish bond catalogs atomically and retire deleted events"). Audited DB snapshot: `<workspace>/market-data-server/data/incoming/bc/bond_catalog/bond_catalog.sqlite3`, exported 2026-10-15T16:17Z; the last catalog run in it is `20261012T033027Z`. The DB was opened read-only only (`sqlite3 -readonly` / `?mode=ro`).

The live comparison used public MOEX ISS, fetched on 2026-10-15 at about 19:28 local time:
- `/iss/engines/stock/markets/bonds/securities.json?iss.only=securities`: 3,481 board rows, 3,172 SECIDs.
- `/iss/securitygroups/stock_bonds/collections/stock_bonds_all/securities.json`: 11,757 rows.

All paths below are relative to `ref/moex-market-data-mcp/` unless they start with another repo name.

The repo was not modified. `uv run pytest` rewrote `uv.lock` (lock drift, see §4), and that change was reverted with `git checkout -- uv.lock`. The tree is clean again.

---

## 0. Executive summary

- **The base is good.** Bulk MOEX ISS collection is deterministic and complete for the exchange universe. Every SECID listed on MOEX's main bond boards as of the 2026-10-12 run is in the catalog. The 15 SECIDs on today's live board list that are missing from it were all listed on or after 2026-10-13, so the gap is caused by the weekly refresh cadence. Coupon, amortization and offer schedules come from one bulk `bondization` crawl with strict cursor validation. Issuer INN coverage is about 100%.
- **Floater formulas are effectively absent, because of two string-matching bugs.**
  - `catalog.py:222` looks for `"Флоатер"`, but MOEX now labels floaters `"Облигация с плавающим купоном"`.
  - As a result, **604 of 631 active floaters are `unclassified/unavailable`**.
  - Another 24, including **OFZ 29010**, are mislabelled `known_coupon_schedule`. This happens because MOEX bondization copies the current rate into every future coupon.
  - Benchmark and spread are present in `issues` for all 631 floaters (RREFKEYR 503, RUONIA 84, CPI 18, ZR_YLD_CRV 10, RUONIA_IND 8, RUONIA3M 4, RUONIA6M 2, OTHER 2). The *conventions* (lag, averaging, compounding, floor/cap) are missing.
- **Fields that are zero or near zero:**
  - ratings: 0 rows;
  - issuer industry/sector: 0%;
  - day count: 0% as a column (15 ISINs via Cbonds);
  - settlement code / T+ / settle date: not stored;
  - lot size, first trade date (`STARTDATEMOEX`), and the subordinated / high-risk / qualified-investor-group flags: these exist only inside `raw_json`, and `raw_json` is stripped in this snapshot;
  - linker indexation conventions: not stored;
  - perpetual/AT1 call schedules: 0 of 62.
- **OGRN coverage is only 54% of active bonds (82% excluding structured notes)**, because the Bank of Russia (CBR) registry URL is hard-coded to the 2026-06-30 file (`cbr_registry.py:11-12`).
- **Cbonds is wired in, but only as a side channel.**
  - Four methods are allowed (`get_emissions`, `get_flow_new`, `get_offert`, `get_rating_emission_maxdate`). Only 15 ISINs were ever enriched.
  - Results never flow back into `issues`, `formula_models`, `events` or `ratings`.
  - As a result, the daily recovery job re-fetched the same 3–4 ISINs every day from 08-25 to 09-13.
- **The production DB has not been migrated to the 10-14 atomic/retirement fix.** `event_revisions` has no `is_deleted` column, and 28 stale events remain in `events`.
- **Tests:** 250 passed, 1 failed (a font path in an unrelated FX chart test), in 34 s.
- **Sibling repos:** none has better static data than this repo. `corporate-bond-monitor` and `currency-bond-monitor` both *consume* this catalog. `currency-bond-monitor` filters on `bond_type='Валютные облигации'`, which matches **0 traded bonds** in the current catalog (§1.7).

---

## 1. How static is sourced

### 1.1 MOEX ISS endpoints (all public; no key used for static)

Client: `src/moex_market_data/catalog_client.py`.
- `BASE_URL = https://iss.moex.com/iss` (`:9`).
- 5 attempts with exponential backoff on 429/5xx (`:45-65`).
- Concurrency semaphore; the weekly job runs with 5.
- User-Agent `Lima-Market-Data/0.9` (`:36`).

| # | Endpoint | Code | What it provides |
|---|---|---|---|
| 1 | `GET /securities.json?group_by=group&group_by_filter=stock_bonds&limit=100&start=N` | `catalog_client.py:78-96` | Full directory, traded and archived (16k SECIDs): `secid, isin, regnumber, name, shortname, is_traded, emitent_id/title/inn/okpo, type, group, primary_boardid, marketprice_boardid`. Paged by 100, about 160 calls. |
| 2 | `GET /engines/stock/markets/bonds/securities.json?iss.only=securities` | `:98-103` | One call for current board rows: `FACEVALUE, FACEUNIT, CURRENCYID, ISSUESIZE(PLACED), MATDATE, COUPONPERIOD/VALUE/PERCENT, NEXTCOUPON, ACCRUEDINT, OFFERDATE/BUYBACKDATE, CALL/PUTOPTIONDATE, LISTLEVEL, LOTSIZE, SETTLEDATE, BONDTYPE/BONDSUBTYPE, COUPON_DETAILS, FACEVALUE_TYPE`. A completeness gate requires SECID+BOARDID on every row and ≥95% `COUPONPERIOD` (`catalog_runner.py:35-50`). |
| 3 | `GET /securitygroups/stock_bonds/collections/stock_bonds_all/securities.json?limit=1000` | `:105-127` | About 12 parallel pages of rich static ("description in bulk"): `ISSUEDATE, INITIALFACEVALUE, STARTDATEMOEX, COUPON_BENCHMARK(_SPREAD), BOND_TYPE/SUBTYPE, AMORTBOND/STRUCTBOND/SUBORDBOND, ISQUALIFIEDINVESTORS, QUALINVESTORGROUP, HIGHRISK, ISSUESIZEPLANNED, PROGRAMREGISTRYNUMBER`. Used as `details` for 11.7k SECIDs (`catalog_runner.py:457-466`). |
| 4 | `GET /securities/{SECID}.json` (blocks `description`, `boards`) | `:129-151` | Per-security fallback, only for traded SECIDs missing from #3 (`--details-scope traded`, `catalog_runner.py:470-482`). |
| 5 | `GET /statistics/engines/stock/markets/bonds/bondization.json?iss.only=coupons,coupons.cursor,amortizations,amortizations.cursor,offers,offers.cursor&limit=100&start=N` | `:153-227` | Bulk coupons, amortizations and offers for the whole market. Every page validates the cursor `INDEX`, `TOTAL` and row counts, retries short pages, and fails if totals change mid-crawl (`:176-226`). An empty coupon and principal result is rejected (`catalog_runner.py:489-490`). |
| 6 | `bondization_block()` with per-block `{block}.start` | `:229-258` | Helper; not used by the runner. |
| 7 | `https://www.cbr.ru/vfs/registers/rcb/list_reestrcb_20260630.zip` (CBR securities registry XLSX) | `cbr_registry.py:11-103` | `cbr_security_id, ISIN, regnumber, CFI, face currency, issuer INN/OGRN, EGRUL legal/short names, country`. Exact ISIN join only (`catalog_runner.py:173-183`). The URL and `as_of` are **hard-coded**. |

Not used for static:
- `/iss/engines/.../boards/{board}/securities/{secid}.json`;
- `/iss/securities/{secid}/bondization.json`, the per-issue form, which gives the same data as #5;
- MOEX CCI ratings: a probe returned `X-MicexPassport-Marker: denied` (`docs/BOND_CATALOG_2026-08-01.md` §Ratings);
- the NSD API;
- AlgoPack (`algopack.py`; its key is read from the file `<secrets-dir>/moex-api-key`, `algopack.py:13,33`, not from an env var).

### 1.2 Cbonds licensed JSON API

- Transport: `src/moex_market_data/cbonds.py`.
  - Base `https://ws2.cbonds.info/services/json` (`:9`).
  - POST `/{operation}/` with body authentication (`:91-99`).
  - Allow-list (`:11-18`): `get_emissions`, `get_flow_new`, `get_offert`, `get_rating_emission_maxdate`.
- Normalization: `src/moex_market_data/cbonds_service.py`.

| Method | Filter | Fields | Normalized as |
|---|---|---|---|
| `get_emissions` | `isin_code eq` / `in` (≤50, `;`-joined) | `ISSUE_FIELDS` `cbonds_service.py:8-34`: `id, isin_code, document_rus, emitent_name_rus, currency_name, maturity_date, floating_rate, bond_type (=amortizing flag), coupon_type_name_rus, curr_coupon_rate/sum/date, cupon_period, reference_rate_name_rus, margin, floor, cup, nominal_price, outstanding_nominal_price, remaining_outstand_amount, emission_cupon_basis_title (day count), variable_rate_type_id, floating_rates_day, updating_date, update_time` | `_normalize_issue` `:175-224` |
| `get_flow_new` | `emission_isin_code eq`, paged ≤250 | `FLOW_FIELDS` `:35-58`: `start_date, date, coupon_num, cupon_rate (decimal), cupon_sum, actual_payment_date, redemtion, redemption_percentage, pool_factor, floating_rate, days_beetwen_coupons, reference_rate, margin, floor, cup, rate_change_flag` | `_normalize_flow` `:349-393`. Future floater rows are labelled `unknown_future_floater`. |
| `get_offert` | `emission_isin_code eq` | `date, date_open, date_close, until_date, price, type_rus, option_type_name_rus, status_str, notice_period, benchmark_spread, more_rus` (`:278-312`) | Offers with exercise window and type |
| `get_rating_emission_maxdate` | `emission_isin_code eq` | `agency_name_rus, scale_name_rus, scale_point_name, forecast_name_rus, rating_date` (`:253-276`) | Exposed as an MCP tool (`server.py:395`). **Never persisted**: the `ratings` table has 0 rows. |

- `docs/CBONDS_API_2026-08-13.md` notes that `get_currencies` and `get_coupon_types` are entitled but not exposed by the live schema.
- Persistence: `cbonds_enrichment.py`.
  - Side tables `cbonds_*` (§2.4) and an online backup before each write (`:86-100`).
  - `_materialize` (`:228-313`) resolves coupon value, benchmark and maturity with **"local (MOEX) always wins if non-null"** (`:301-310`).
  - Example: `RU000A0JS9X1` keeps the MOEX benchmark `OTHER` although Cbonds gives key rate + 4.0.
  - The canonical tables `issues`, `formula_models` and `events` are never updated.

### 1.3 Authentication (names only; values not read)

| Secret | Where | Format |
|---|---|---|
| `CBONDS_LOGIN`, `CBONDS_PASSWORD` | Keys inside the file `<secrets-dir>/cbonds.env` (`cbonds.py:10,25-48`). The file must have mode 0600 and is **not read from the process env**. | Sent in the request body `auth{login,password}` |
| MOEX AlgoPack / APIM key | File `<secrets-dir>/moex-api-key` (`algopack.py:13,33-47`). Siblings use the env var **`MOEX_API_KEY`** (`moex-live-bonds/moex_live_bonds/config.py:11-35`, `ofz-analytics/.../market_data.py:197`). | Bearer |
| MOEX Passport (realtime) | `<secrets-dir>/moex-realtime.json`, `moex-bond-orderbook.json` (moex-live-bonds `realtime.py:14-15`) | Not needed for static |
| Network | `ALL_PROXY` (loopback SOCKS proxy) and `SSL_CERT_FILE` (host CA bundle path) hard-coded in `scripts/run_bond_catalog_weekly.py:39-43` | Host-specific |

Other repos use these env-var names: `CBONDS_LOGIN`, `CBONDS_PASSWORD`, `CBONDS_BASE_URL`, desk-system login/password variables (corporate-bond-monitor `config.py:91-96`); `CORP_MONITOR_DB`; `CURRENCY_BOND_SCHEDULE_CACHE`.

### 1.4 Refresh logic

- **Weekly full run.** `scripts/run_bond_catalog_weekly.py` runs `moex-bond-catalog --details-scope traded --concurrency 5` (`:26-36`), which calls `catalog_runner.collect_live_catalog` (`:435-538`).
  - Steps: (1) directory, market and collection in parallel; (2) CBR registry download; (3) per-SECID fallback descriptions plus bulk bondization; (4) raw JSONL.GZ written to `artifacts/bond_catalog/{run}/raw/`; (5) `materialize_catalog` (`:153-326`) in **one transaction** (`catalog.py:525-557`); (6) CSV and SQLite exports (`:354-432`).
  - The DB shows 23 runs between 08-01 and 10-12, mostly on Mondays at 03:30 UTC.
  - The fetch takes about 12 minutes: the run id is the start time and `started_at` is when materialization starts.
- **Daily "static recovery".** `scripts/run_static_recovery_daily.py` plus `static_recovery.py`.
  - It reads `pricing_failures` from the corporate-bond-trade-monitor DBs (`:18-21`) and optionally refreshes MOEX.
  - SECIDs whose catalog still has gaps (`catalog_static_gaps`, `static_recovery.py:36-76`) are fetched from Cbonds (`:65-98`).
  - **Bugs:**
    1. The `refresh_moex` timeout is 300 s (`:40`), but a full run needs about 12 minutes.
    2. The Cbonds results never close the gap. As a result, the same ISINs were re-requested daily: 19 runs, 3–4 ISINs each, and 4,052 duplicate cash-flow observations for 5 ISINs.
    3. `incomplete_formula` only counts `None` / `partial_missing_conventions` (`static_recovery.py:71`). The 604 floaters with status `unavailable` are therefore never queued for recovery when they have published coupons.
- **Curated overrides.**
  - `src/moex_market_data/data/pilot_manifest.json` holds 12 hand-researched formula models (`_curated_formula_models`, `catalog_runner.py:66-88`).
  - `scripts/apply_formula_fallbacks.py` re-applies the `fallback_unverified` ones.
  - Two hard-coded conflicts are recorded (`catalog_runner.py:91-116`).
  - **5 of the 12 manifest SECIDs do not exist in the catalog:**
    - `SU29027RMFS7` is a typo; the real SECID is `SU29027RMFS8`, so the OFZ 29027 `compounded_index_ratio` model is silently never applied.
    - `RU000A10NOVO`, `RU000A10GTLK` and `RU000A10VEB9` are placeholder IDs and not valid ISINs.
    - `RU000A10A8D0` and `RU000A10FPC5` are absent.

### 1.5 Revisions and conflict resolution

- **`issue_revisions`.**
  - A row is appended when `_semantic_issue_hash` changes (`catalog.py:270-277`, `:674-682`). The hash excludes `raw_*`, `accrued_interest` and `details_error`.
  - It still includes volatile fields: `coupon_value`, `coupon_percent`, `next_coupon_date`, `facevalue`, `is_traded`, `market_status`, `list_level`, `offer_date`.
  - The result is about 1,000–4,000 revisions per weekly run, plus a full 15,672-row rewrite on 08-03 when the hash definition changed.
  - It is **not** a clean log of contractual changes.
- **`event_revisions`.**
  - A row is appended when the event payload hash changes. The hash excludes `facevalue`, `issuevalue` and `value_rub` (`catalog.py:280-285`, `:875-882`).
  - The new code adds `is_deleted` tombstones when an event disappears from a *complete* snapshot (`retire_missing_events`, `catalog.py:559-582`).
  - The snapshot DB predates this: no `is_deleted` column, and 28 stale events (9 amortization, 8 coupon, 11 offer) with `last_seen_run` before 10-12 still sit in `events`.
  - **Event identity is fragile.** `event_key = SECID|type|date|ordinal` (`catalog.py:841`), with the ordinal taken from JSON-sorted rows within a date (`catalog_runner.py:136-150`). A moved coupon date shows up as delete + insert, not as a revision.
- **`conflicts`.**
  - Only the 2 hard-coded rows exist: `RU000A10A8E8` amortization 0.005 vs 0.01 USD, and `RU000A1028U9` RZD call/reset absent from MOEX.
  - There is **no automatic cross-source comparison** of MOEX vs CBR vs Cbonds, even though the data to do it is present. For example, `regnumber` vs `regnumber_cbr`, `currency` vs `currency_cbr`, `emitent_inn` vs `issuer_inn_cbr`, and maturity or coupon vs Cbonds could all be compared.
- **`source_availability`.** Four fixed field groups per SECID are written on every run (`catalog_runner.py:226-258`):
  - `security_description`: available 12,583 / not_collected 3,440;
  - `ratings`: `unavailable_entitlement` for all 16,023;
  - `issuer_industry`: `unavailable_bulk_field` for all;
  - `issuer_legal_registry` (CBR): available 2,492 / not_matched 13,531.

  This is a good idea (explicit "known unknowns"), but it covers only four groups.

### 1.6 `formula_models` (floaters and linkers)

`classify_preliminary_formula` (`catalog.py:169-262`) applies these steps in order:
1. curated manifest;
2. linker, by `BOND_TYPE` containing "индексир…/линкер" → `indexed_principal_unresolved` with 6 missing principal-index terms;
3. `"Фикс" in bond_type` → `fixed_rate/preliminary_moex` (`:209`);
4. `"Флоатер" in bond_type` with a benchmark → `benchmark_plus_spread/partial_missing_conventions` with 7 missing terms (`:222`);
5. all bondization rates known → `fixed_rate` or `known_coupon_schedule`;
6. otherwise `unclassified`.

Steps 3 and 4 match **legacy labels only**: "Фикс с известным купоном" and "Флоатер" now appear only on archived issues. In the current DB, for active traded bonds:

| model_type / status | all | active traded |
|---|---|---|
| fixed_rate / preliminary_bondization | 8,072 | 2,226 |
| unclassified / unavailable | 5,721 | 1,521 (604 of them floaters) |
| known_coupon_schedule / preliminary_bondization | 2,068 | 179 (24 floaters, e.g. SU29010RMFS4, with future coupons all 82.72 = current 16.59% projected) |
| benchmark_plus_spread / partial | 16 | **0** |
| indexed_principal_unresolved | 13 | 12 |
| curated (exact / fallback / partial_public / refused) | 8 | 7 |

Linkers:
- The catalog keeps `facevalue` (indexed, e.g. SU52002 1775.97) and `initial_facevalue` (1000).
- It has no CPI series, lag (OFZ-IN uses a 3-month lag), index ratio history or floor.

Floater pricing helpers already exist in `cashflow.py`: `arithmetic_average_coupon :139`, `term_fixing_coupon :177`, `compounded_index_coupon :215`. Day count is inferred on the fly, ACT/365 vs ACT/ACT only, from raw coupon rows (`catalog_analytics.py:46-78`); the result is not persisted, and 30/360 is not supported.

### 1.7 Sibling repos (brief)

| Repo | Static fetched | Store / refresh | Better than this repo? |
|---|---|---|---|
| cbonds-research-connector | Scrapes the **cbonds.ru website** via the `agent-browser` CLI using a saved browser session (`src/cbonds_connector/gateway.py:71-102`); no JSON API. Fields: issuer, sector/industry, volumes, coupon type, benchmark, margin, **coupon formula text excerpt**, day count, **business-day convention**, perpetual flag, call dates, offer window/price, SPPI, prospectus PDFs (`models.py:34-61`, `parser.py:8-86`). | Content-addressed JSON cache; on demand per ISIN; at most 1 call per 5 s (`service.py:54-88`) | Only niche fields: business-day convention, SPPI, formula text and documents. Fragile: keeps only the first cash-flow row (`parser.py:66-86`); no amortization. |
| moex-live-bonds | Board rows from `/engines/stock/markets/bonds/boards/{board}/securities.json`, plus `ndm` routing for PSOB/PTOB/OTCB (`realtime.py:18,225-300`); ISIN→SECID via `/securities.json?q=` (`client.py:109-139`). Auth `MOEX_API_KEY` (APIM) or Passport JSON files. | None | No; it passes market-board static (LOTSIZE, SETTLEDATE, FACEVALUEONSETTLEDATE) through without storing it. |
| corporate-bond-monitor | `/securities/{isin}.json` and TQCB board securities (`src/corp_monitor/adapters/moex.py:62-110`); **reads this catalog** (`<desk>_static_comparison.py:255-300`, `tools/<desk>_static_comparison/export_server_static.py:14-32`); desk-system bond-product cache (`analytics/<desk>_cache.py`). Cbonds is only a probe. | SQLite `instruments`; Desk-system positive/negative cache with a 7-day TTL | Its useful contribution is parity evidence: Calypso vs MOEX-derived cash flows have a median 0 bp, max 0.67 bp, on 22 ISINs (`docs/decisions/2026-08-10-bond-static-and-loading-policy.md`). It also has an ACT/365 / bullet-eligibility inference (`qr_static_comparison.py:100-145`). |
| currency-bond-monitor | **Reads this catalog** `issues` (`src/currency_bond_monitor/catalog.py:96-124`); per-SECID `/statistics/.../bondization/{secid}.json` (`schedule_source.py:52-71`); live FACEVALUEONSETTLEDATE, SETTLEDATE, CURRENCYID, CROSSRATE and LOTSIZE (`levels.py:96-185`) | Schedule cache | Better offer handling: offers are verified against bondization, holder puts are identified by their exercise window, and offer principal = face × price (`schedules.py:73-111`). **Bug:** it filters on `bond_type='Валютные облигации'` (`catalog.py:57,84,123`). In the catalog that label exists only on 36 archived rows (0 traded); the active label is `'Валютная облигация'` (197 traded). It also uses `facevalue` as the amortized nominal (`catalog.py:110`). |
| ofz-analytics | TQOB board securities only (`ofz_analytics/static_data.py:28-172`) | Streamlit cache, 1 h | No. It builds a synthetic schedule from COUPONPERIOD (`models.py:61-90`), and its OFZ-AD/IN exclusion does not work as the comment says (`static_data.py:85-96`). |

**Conclusion:** none of the five is a better static source. Ideas worth borrowing:
1. Board-level LOTSIZE, SETTLEDATE, FACEVALUEONSETTLEDATE and CURRENCYID (currency-bond-monitor `levels.py:153-185`).
2. Offer validation and classification (currency-bond-monitor `schedules.py:73-111`).
3. Business-day convention and formula-text capture (cbonds-research-connector).
4. Desk-system parity as a QA oracle (corporate-bond-monitor).

---

## 2. Schema walk-through of `bond_catalog.sqlite3`

Snapshot note (`README.txt`, `SNAPSHOT.json`): all rows are kept, but **every raw payload column is blanked**. The affected columns are `issues/issuers/issue_revisions/events/event_revisions/ratings.raw_json`, `boards.board_json` and `cbonds_*.payload_json`. Fields that exist only in raw payloads therefore cannot be recovered from this export. Examples: `LOTSIZE`, `SETTLEDATE`, `STARTDATEMOEX`, `SUBORDBOND`, `HIGHRISK`, `COUPON_DETAILS`, offer `offertype`, coupon `startdate`, `faceunit` per board. The production DB still has them.

### 2.1 Run and issuer tables

| Table (rows) | Key | Fields |
|---|---|---|
| `runs` (23) | `run_id` (UTC timestamp) | `started_at`, `completed_at`, `status` (`ok`/`completed`/`failed`/`running`), `perimeter` (`all_stock_bonds_directory_and_bondization`), `counts_json` (coverage counters: issues_new/changed/disappeared, events_*, cbr matches, descriptions…) |
| `issuers` (1,670) | `emitent_id` (MOEX) | `title`, `inn` (1,593 non-null), `okpo`, `industry_code`/`industry_name` (**always null**), `industry_status` (`unavailable_source`), `first/last_seen_run`, `raw_json` |

### 2.2 `issues` (16,023): one row per MOEX SECID

Identity:
- `secid` (PK), `isin` (569 null), `regnumber` (841 null), `name`, `shortname`.
- `type`: MOEX security type: `exchange_bond` 8,614, `corporate_bond` 6,246, `subfederal_bond` 449, `non_exchange_bond` 213, `ofz_bond` 210, `bondization_only` 102 (stubs for SECIDs seen only in bondization), `cb_bond` 95, `municipal_bond` 67, `ifi_bond` 27.
- `group_name`.

Listing:
- `is_traded` (directory flag), `primary_boardid`, `marketprice_boardid`, `list_level`, `qualified_investors` (0/1), `market_status` (board STATUS).

Classification:
- `bond_type` (MOEX `BONDTYPE` / `BOND_TYPE`, Russian label; 11,195 empty, mostly archive).
- `bond_subtype`: До погашения / До оферты (put) / До оферты (call) / Бессрочные.

Nominal:
- `facevalue`: current, i.e. after amortization or indexation.
- `initial_facevalue`.
- `faceunit`: face currency as MOEX codes it (`SUR` = RUB).
- `currency`: **settlement currency** (`CURRENCYID`). 222 live board rows have face ≠ settlement currency, e.g. USD face settled in RUB.

Size:
- `issue_size`: number of bonds.
- `issue_size_placed`.

Dates:
- `issue_date`.
- `maturity_date`: `0000-00-00` or null for perpetuals.

Current coupon (volatile market fields):
- `coupon_period_days`, `coupon_frequency`, `coupon_percent`, `coupon_value`, `next_coupon_date`, `accrued_interest`.

Floater:
- `coupon_benchmark`: RREFKEYR / RUONIA / RUONIA3M / RUONIA6M / RUONIA_IND / CPI / ZR_YLD_CRV / OTHER.
- `coupon_benchmark_spread`: percentage points.

Options:
- `offer_date` (OFFERDATE or BUYBACKDATE), `call_option_date`, `put_option_date`.

Provenance:
- `description_available`, `details_error`.
- `payload_hash`: semantic hash.
- `first_seen_run`, `last_seen_run`.
- `raw_json`: directory, market and description merged.

CBR enrichment (exact ISIN join):
- `cbr_security_id`, `cfi`, `regnumber_cbr`, `currency_cbr`, `issuer_inn_cbr`, `issuer_ogrn`, `issuer_legal_name_cbr`, `issuer_short_name_cbr`, `issuer_country`, `cbr_registry_as_of`.

Scope:
- `instrument_class`: `ordinary_or_unclassified` 14,192, `structured` 1,289, `securitization_mbs` 233, `structured_securitization` 176, `securitization_other` 133.
- `scope_status`: in_scope / out_of_scope.
- `scope_reason`, `classification_source`, `classification_confidence`.
- Assigned by `classify_instrument_scope`, `catalog.py:30-82`: MOEX "Структурная облигация", CFI `DG*`, or name containing "ипотечн…агент" / "специализированное финансовое общество".

### 2.3 History, boards, events, formulas and quality tables

| Table (rows) | Key | Meaning |
|---|---|---|
| `issue_revisions` (72,753) | (`secid`, `run_id`) | Full issue payload whenever the semantic hash changed (§1.5) |
| `boards` (134,427) | (`secid`, `boardid`) | Every board a SECID was seen on. `is_primary`, `board_json`, `first/last_seen_run`. **Never retired**: 111k rows were last seen on 08-01, so readers must filter `last_seen_run`. In the latest run, 20,287 rows cover 4,007 SECIDs; the structured/EQRP notes carry 13 boards each (QBND, PSR*, OCB*, LIQR…). |
| `events` (256,559) | `event_key = SECID\|type\|date\|ordinal` | Current schedule. `event_type`: coupon 203,724 (12,628 SECIDs) / amortization 46,311 (15,467) / offer 6,524 (3,273). `event_date`, `ordinal`, `value` (coupon or amortization amount), `rate_pct` (MOEX `valueprc`: coupon % or amortization % of face), `currency` (faceunit), `payload_hash`, `raw_json` (full bondization row: `startdate`, `facevalue`, `offertype`, `price`…), `first/last_seen_run`. 595 offers have date `0000-00-00`. |
| `event_revisions` (265,231) | (`event_key`, `run_id`) | Event payload history. The new code adds `is_deleted`; it is not in this DB. |
| `formula_models` (16,023) | `secid` | `model_type`, `status`, `annual_rate_pct`, `benchmark`, `spread_pp`, `missing_terms_json` (explicit unknown conventions), `source`, `llm_used`, `model_json` (curated models carry formula params: lag, averaging, rounding), `first/last_seen_run` |
| `ratings` (0) | (`secid`, `agency`) | `rating`, `outlook`, `rating_date`, `status`, `source_url`. Designed for one row per agency, but empty. |
| `source_availability` (64,092) | (`secid`, `field_group`, `source`) | `status`, `reason`, `run_id` (§1.5) |
| `conflicts` (2) | `secid\|field\|selected_src\|conflicting_src` | `selected_value`, `conflicting_value`, `reason`, sources, `resolved` |

### 2.4 Cbonds side tables (from `cbonds_enrichment.py:10-83`)

| Table (rows) | Meaning |
|---|---|
| `cbonds_ingestion_runs` (21) | `run_id`, `observed_at`, `endpoint`, `request_mode`, `requested_count`, `found_count`, `missing_isins_json`, `status` |
| `cbonds_issue_observations` (80) | Append-only per run+ISIN: `secid`, `cbonds_issue_id`, provider update date/time, `payload_json` |
| `cbonds_issue_enrichment` (15) | Latest per ISIN: `issuer_cbonds`, `currency_cbonds`, `maturity_date_cbonds`, `coupon_structure_cbonds` (fixed/floater), `principal_structure_cbonds` (bullet/amortizing), coupon rate and amount, `reference_rate_cbonds`, `margin/floor/cap_percent_cbonds`, **`day_count_cbonds`**, `calculation_amount_cbonds`, and `*_resolved` / `*_source` for coupon value, benchmark and maturity |
| `cbonds_cashflow_observations` (4,052; 5 ISINs) | `cbonds_flow_id`, `payment_date`, `coupon_start_date`, `coupon_number`, `coupon_amount`, `coupon_status`, `principal_amount` |
| `cbonds_offer_observations` (26) | `ordinal`, `option_date`, `option_type`, `price_percent_of_face` |

---

## 3. Completeness on the DB

### 3.1 Counts by type (whole catalog vs active)

"Active" means `is_traded=1` and (`maturity_date > 2026-10-15`, or a perpetual with null/`0000-00-00` maturity). That gives **4,007 SECIDs**: 3,945 dated plus 62 perpetuals. The coverage statistics below use the 3,945 dated bonds.

| Segment (rule) | All in catalog | Active traded (dated) |
|---|---|---|
| OFZ-PD (`SU26*`, `SU25*`) | 74 | 31 |
| OFZ-PK floaters (`SU29*`, `SU24*`) | 29 | 21 (RUONIA 16, RUONIA3M 4…; 29007–29010 with spread 1.3–1.6) |
| OFZ-IN linkers (`SU52*`) | 5 | 4 (plus 9 non-OFZ linkers; 13 rows have the `Линкер` bond_type; 18 have benchmark CPI) |
| OFZ-AD (`SU46*`) | 13 | 2 |
| RF sovereign FX (RUS-xx eurobonds, РФ ЗО replacements, CNY OFZ) | — | 30 (CNY OFZ: 3, on board TQOY) |
| Foreign sovereigns on MOEX (KAZ, BLR, TUR, OMN, EGY, UZB; UST non-traded) | — | 11 |
| Sub-federal | 449 | 63 |
| Municipal | 67 | 2 |
| Corporates incl. exchange / non-exchange / CB / IFI, ordinary | 15,195 | 2,288 |
| …of which securitization / MBS | 366 | 177 |
| …of which structured notes ("Структурная облигация", mostly EQRP / qualified-investor bank notes) | 1,465 | 1,316 |
| Replacement bonds (heuristic: FX face and shortname containing ЗО or КЗ-) | — | ≈42. No reliable flag exists in MOEX or the catalog. |
| CNY-denominated (faceunit) | 107 | 69 (50 settle in RUB, 19 in CNY) |
| USD-denominated | 226 | 135 (120 settle in RUB) |
| EUR-denominated | 63 | 13 |
| CHF / GBP / other | 11 | 4 |
| FX face, RUB-settled ("ruble-linked to FX") | — | 183 |
| Perpetual (Бессрочные / no maturity) | 49+ | 62 (none has a call schedule in `events`) |

### 3.2 Catalog vs live MOEX universe

| Measure | Count |
|---|---|
| Live `engines/stock/markets/bonds/securities` (boards TQCB, TQOD, TQRD, TQOB, TQOY, SPOB, PACT) | 3,172 SECIDs |
| …missing from the catalog | **15**. All are new listings from 2026-10-13 to 10-15 (e.g. RU000A10GCW5 РУСАЛ 1Р17, RU000A10GCY1 РСетиСЗ1Р2, RU000A10G759 ВЭБ2Р-К762). The cause is weekly cadence, not a logic gap. |
| Live `stock_bonds_all` collection: alive traded | 3,181. 17 are missing from the catalog, including 2 not yet trading on the market (RU000A10GD60, RU000A10GDC5). |
| Catalog active traded but not on the live board list | 851. These are 818 EQRP-board qualified-investor structured notes, 15 RPEY, 10 OCBR and others. The directory endpoint #1 covers them, but the default market list and the collection do not, so the catalog is a **superset**. |
| Catalog SECIDs not in the collection | 4,285 (archived corporates, `bondization_only` stubs, CB bonds) |
| Field drift vs live (traded overlap) | `facevalue` 26 (indexed or capitalizing face, e.g. НКК 11 1613.87 → 1614.70), `issue_size` 3, `bond_type` 2, `coupon_benchmark` 1 (RU000A10G155 became a key-rate floater). Maturity differences are only the `0000-00-00` vs null encoding. |

### 3.3 Field completeness, active traded dated bonds (% of n)

| Segment | n | ISIN | regnum | INN | OGRN (CBR) | sector | issue size | future coupon rows | all future coupons known¹ | future amort = face | future offer rows | put/call subtype → offer row | Cbonds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OFZ-PD | 31 | 100 | 100 | 100 | 100 | 0 | 100 | 100 | 100 | 100 | 0 | 100 | 0 |
| OFZ-PK | 21 | 100 | 100 | 100 | 95 | 0 | 100 | 100 | 19² | 100 | 0 | 100 | 0 |
| OFZ-IN | 4 | 100 | 100 | 100 | 100 | 0 | 100 | 100 | 100³ | 100 | 0 | 100 | 0 |
| OFZ-AD | 2 | 100 | 100 | 100 | 100 | 0 | 100 | 100 | 100 | 100 | 0 | 100 | 0 |
| RF sovereign FX | 30 | 100 | 83 | 100 | 80 | 0 | 100 | 100 | 97 | 93 | 0 | 100 | 0 |
| Foreign sovereigns | 11 | 100 | 0 | 55 | 0 | 0 | 100 | 100 | 55 | 100 | 0 | 100 | 0 |
| Sub-federal | 63 | 100 | 100 | 100 | 92 | 0 | 100 | 100 | 83 | 98 | 0 | 100 | 0 |
| Municipal | 2 | 100 | 100 | 100 | 100 | 0 | 100 | 100 | 100 | 100 | 0 | 100 | 0 |
| Corporate ordinary | 2,288 | 100 | 98 | 100 | 82 | 0 | 100 | 99 | 59 | 100 | 23 | 94 | 0 |
| Securitization/MBS | 177 | 100 | 100 | 100 | 80 | 0 | 100 | 98 | 3 | 97 | 2 | 98 | 1 |
| Structured notes | 1,316 | 100 | 100 | 100 | **0** | 0 | 42 | 66 | 62 | 100 | 1 | 100 | 0 |
| [floaters, any] | 631 | 100 | 100 | 100 | 84 | 0 | 100 | 100 | 4 | 100 | 18 | 96 | 1 |
| [linkers CPI] | 31 | 100 | 100 | 100 | 97 | 0 | 100 | 97 | 39 | 94 | 19 | 100 | 0 |
| [FX face, RUB-settled] | 183 | 100 | 92 | 99 | 81 | 0 | 100 | 99 | 87 | 99 | 10 | 99 | 0 |
| **All active in-scope** | 2,452 | 100 | 98 | 99 | 82 | 0 | 100 | 99 | 60 | 99 | 21 | 94 | 0 |
| **All active** | 3,945 | 100 | 99 | 100 | 54 | 0 | 81 | 88 | 58 | 99 | 14 | 96 | 0 |

¹ Every future coupon row has value > 0. MOEX publishes floater coupons as null, or as the current rate copied forward.
² For OFZ-PK, a "known" value is the current rate copied into later periods. It is **not** a contractual projection.
³ OFZ-IN coupon amounts are on the current indexed face only, so later amounts are not truly known.

Fields at a constant 100% for all segments: face value + unit, initial face, issue date, maturity, primary board, board rows, and description collected.

**Fields with no column (0% usable from this snapshot):**
- day count (only in `cbonds_issue_enrichment`, 15 ISINs);
- settlement lag / settle date;
- lot size (only 2 live bonds have LOTSIZE ≠ 1: RU000A0ZYWZ2 = 100, RU000A10A8E8 = 1000);
- first trade date (`STARTDATEMOEX`);
- subordinated / high-risk / qualified-investor-group flags;
- ratings (0 rows);
- issuer sector / OKVED;
- guarantor;
- floor / cap;
- floater fixing lag / averaging;
- CPI lag / index ratio;
- offer type (put vs call is only inside `raw_json`);
- coupon start dates (only inside `raw_json`);
- business-day convention / calendar;
- replacement-bond flag and original eurobond ISIN.

### 3.4 Concrete gaps, with examples (active, in-scope)

- **Floater formula unavailable: 582.** Examples: RU000A0JQAM6 ДОМ.РФ15об, RU000A0JR4U9 ГлДорога 3, RU000A0JTYJ6 ФСК РС-23, SU29025RMFS2 (RUONIA + 0, `unclassified`). Also SU29010RMFS4 is classed `known_coupon_schedule`, although it is an OFZ-PK.
- **Future coupons unknown (null/0): 963.** Examples: CH0419041634 RZD-28 CHF, RU000A0JS4Z7 ВЭБ.РФ 21, RU000A0JRU20 СЗКК 04. Most are floaters and "fixed with unknown coupons" (`Облигация с фиксированным (неизвестным) купоном`, 267 active).
- **No future coupon rows: 17.** Examples: RU000A107W63 ЕАБР П3-10, RU000A109K81 АЛЬФАБ1Р14, RU000A109LG9 Сбер SbD2R. These are also the 15 SECIDs with no `bond_type` (index-linked deposit-style notes).
- **Put/call subtype but no future offer row: 136.** Examples: RU000A0JVP39 КАМАЗ 12 (offer row dated `0000-00-00`), RU000A0ZYBM4 АльфаБО-21 (`offer_date` 2026-10-09 has passed and there is no next offer). In addition, 595 offers are dated `0000-00-00`.
- **Future amortization ≠ current face: 14.** Examples: RU000A1021F5 НКК 11 and RU000A102RB2 НКК 12 have an indexed or capitalizing face; RU000A102A15 СПбГО35003 had face 500 in the catalog vs 300 live; RU000A106T85 CTRLлиз1Р1.
- **No OGRN: 446 in-scope and all 1,316 structured notes.** Examples: BYM000001818 РесБел 331 (foreign issuer), plus most issues placed after the 2026-06 CBR registry.
- **No regnumber: 51** (BY/XS ISINs). **No issuer INN: 14** (foreign issuers: TUR-30, LUK-26, GAZPR-34, RZD-31 GBP).
- **Perpetuals: 62 traded.** Examples: RU000A0JWMZ1 РСХБ 06Т1, RU000A0ZZFZ4 ГПБ 01-АТ1. No call dates exist; 20 coupons are projected to 2111.
- **Ratings: 0** for every bond.

---

## 4. Quality assessment

**Reuse (good, tested, production-proven):**
- `catalog_client.py`:
  - bulk directory, market and collection collection;
  - the strict bondization crawler (`:153-227`, cursor/total/row-count validation);
  - retry and backoff.
- `materialize_catalog` with a single-transaction publish and tombstoned event retirement on complete snapshots (`catalog.py:525-582`, `catalog_runner.py:153-326`). The design is right; the fixed version just needs deploying.
- The `source_availability` + `missing_terms_json` philosophy: unknowns are stored explicitly and nothing is guessed. It is worth keeping as a hub-wide principle.
- `classify_instrument_scope` (`catalog.py:30-82`).
- The exact-ISIN-only CBR join.
- The immutable raw JSONL.GZ per run.
- `cbonds.py` / `cbonds_service.py`:
  - safe transport (allow-listed operations, body auth, 0600 credentials file, ISIN validation);
  - unit normalization (`cupon_rate` decimal → %, `curr_coupon_rate` already in %).
- `cashflow.py`: coupon engines (arithmetic RUONIA, term key rate, compounded index); `catalog_analytics.py` put pricing with amortization.
- `source_snapshots.export_bond_catalog_snapshot` (`source_snapshots.py:39-141`): a sealed, hashed, run-pinned snapshot export.

**Fragile or wrong:**
1. Formula label matching uses legacy MOEX labels (`catalog.py:209,222`). The effect is no floater models, and floaters are mislabelled as known schedules.
2. The CBR registry URL and date are hard-coded (`cbr_registry.py:11-12`), so OGRN coverage decays every month.
3. Cbonds enrichment is a dead end: the side tables never update the canonical tables, "local wins" even when local is `OTHER` (`cbonds_enrichment.py:301-310`), and the recovery loop re-fetches the same ISINs daily. The `refresh_moex` 300 s timeout is shorter than the ~12-minute run.
4. Event identity by (date, ordinal) (`catalog.py:841`, `catalog_runner.py:136-150`), and `boards` are never retired.
5. The issue revision hash includes volatile market fields, which makes revisions noisy.
6. Important static fields stay in `raw_json` and are not projected into columns: LOTSIZE, SETTLEDATE, STARTDATEMOEX, SUBORDBOND, HIGHRISK, offertype, startdate.
7. The pilot manifest has a mistyped SECID and placeholder IDs (§1.4). Nothing validates that manifest SECIDs exist.
8. The production DB is on the pre-10-14 schema: 28 stale events, and no `is_deleted`.
9. `_number(a or b)` treats 0 as missing (`catalog.py:126-149`).
10. Infrastructure is hard-coded: SOCKS proxy, `<home>/...` CA path, absolute DB path in `scripts/check_formula_fallbacks.py:14`.
11. Lock drift: `uv.lock` pins the editable `../rub-fixed-income-core` at 0.2.1, but the sibling is 0.8.1, so `uv run` rewrites the lock. `source_snapshots.py:13` imports `rub_fixed_income`.

**Tests:**
- `uv run pytest -q` with an external venv: **250 passed, 1 failed** in 34 s.
- The failure is `tests/test_currency_futoi.py::test_history_chart_renders_futoi_and_cny_overlay`: a Linux-only DejaVu font path. It is pre-existing, also noted in `docs/critical-fixes-2026-10-14.md`.
- Static-related coverage: `test_catalog` 16, `test_catalog_client` 8, `test_catalog_publication` 11, `test_catalog_runner` 8, `test_catalog_analytics` 11, `test_cbonds*` 16, `test_static_recovery` 6, `test_put_amortization` 4, `test_cashflow` 10, `test_pilot*` 11.
- There is **no test that uses the current MOEX `BONDTYPE` vocabulary** for formula classification; that is how the "Флоатер" bug passes.

---

## 5. Recommendation: target static model for the hub

### 5.1 Principles

1. Keep the **observation → resolution** split:
   - store raw observations per source and run (MOEX, CBR, Cbonds, manual/legal), each hashed;
   - resolve them into a *golden* row with per-field `source`, `as_of` and `confidence`;
   - write conflicts **automatically** whenever sources disagree beyond a tolerance.
2. Use **bitemporal** history: `valid_from/valid_to` (contractual) plus `recorded_at` (knowledge). The as-of reconstruction API is still open work in the current repo.
3. Give events a stable identity: `(instrument_id, event_type, sequence_no)`, where `sequence_no` is the coupon number or offer id, not (date, ordinal). Record date changes as revisions.
4. Hash only contractual fields. Move market-volatile fields (`coupon_value`, `next_coupon`, `accrued`, `market_status`, prices) out of static into market data.
5. Promote every field anyone uses into a typed column; keep `raw` for audit only.
6. Keep the explicit "unknown" pattern (`source_availability` / `missing_terms`) for every field group.

### 5.2 Entities (minimum)

- `instrument`: `instrument_id` (internal), `secid`, `isin`, `regnumber`, `cbr_security_id`, `cfi`, `cbonds_id`, `figi`/T-Invest uid (optional).
  - `type` / `segment`: OFZ-PD/PK/IN/AD, sovereign FX, sub-federal, municipal, corporate, CB, IFI, foreign sovereign.
  - `instrument_class`: ordinary / structured / securitization / subordinated / AT1-perpetual / replacement / convertible / discount.
  - `qualified_only`, `qual_investor_group`, `high_risk`, `listing_level`, `program_regnumber`.
- `instrument_identifier_history`: SECID or ISIN changes, and the replacement bond → original eurobond ISIN link.
- `issuer`: `emitent_id`, INN, OGRN, LEI, legal/short name, country, OKVED/sector (NSD or Cbonds), parent group, sovereign / sub-sovereign flag. Add a `guarantor` link table.
- `issue_terms`:
  - face value (initial / current with date), face currency, settlement currency(ies);
  - issue / placement / first-trade (`STARTDATEMOEX`) / maturity dates; perpetual flag;
  - issue size planned / registered / placed / outstanding;
  - day count;
  - business-day convention and calendar;
  - coupon frequency;
  - ex-coupon / record-date lag;
  - rounding rules (coupon to 0.01 per bond, NKD).
- `board_listing`: `secid` × `board`, with `is_primary`, `lot_size`, `min_step`, `decimals`, `settle_code` / T+n, settlement currency, `listed_from/till`, `active`. Retire boards that disappear.
- `coupon_schedule`: per period `start`, `end`, `pay_date`, `record_date`, `rate_pct`, `amount`, `fixing_status` (known / fixed_pending / projected / unknown), `source`.
- `principal_schedule`: amortization date, % of initial face, amount, remaining face; pool factor for MBS.
- `option_schedule`: put/call/buyback/reset; announcement, exercise window and settlement dates; price %; holder vs issuer; status. Validate against bondization and Cbonds `get_offert`.
- `rate_formula`, parameterized:
  - benchmark id (`RUONIA`, `RUONIA_3M`, `KEY_RATE`, `CPI`, `ZCYC_5Y`, `SARON_5Y`, …);
  - spread, multiplier;
  - averaging (simple / compounded / point);
  - observation window and lag (days or months);
  - lookback / lockout;
  - floor / cap;
  - reset frequency;
  - rounding;
  - fallback;
  - `status` (exact / partial / fallback_unverified);
  - evidence documents.
- `indexation`, for linkers and face-indexed bonds: index (CPI RF), lag (3 months for OFZ-IN), base index value, index-ratio method, floor at par, interpolation.
- `rating`: one row per agency/scale/date/outlook, with history and source (Cbonds `get_rating_emission_maxdate` / `get_emission_ratings_new` within license; MOEX CCI if entitled).
- `fx_linkage`: face currency ≠ settlement currency, the conversion fixing (CBR rate on date X), and the replacement-bond flag.
- `observation_*`, `conflict`, `field_availability`, `run`.

### 5.3 What must be added to cover all MOEX Russian bonds

1. **Fix classification.** Match on the current MOEX vocabulary: `BOND_TYPE`, `COUPON_DETAILS`, `FACEVALUE_TYPE`, plus the `AMORTBOND` / `STRUCTBOND` / `SUBORDBOND` flags. Never copy MOEX's forward-filled floater coupons into "known" schedules.
2. **Floater conventions for 631 issues:**
   - The rules are uniform for the large homogeneous groups, so templates per issuer program apply: OFZ-PK RUONIA/RUONIA3M, key-rate corporates (RREFKEYR, 503), ZCYC.
   - Source them from Cbonds `get_emissions` (`variable_rate_type_id`, `floating_rates_day`, floor/cap) and `get_flow_new` in **bulk**: batches of 50 ISINs, plus paging over all active ISINs.
   - Store document-derived formulas as curated overrides with evidence.
3. **Linkers:** CPI series ingestion plus an OFZ-IN index-ratio model (3-month lag, interpolation, par floor). Also handle the face-indexed non-CPI bonds (НКК 11/12-type) and `RUONIA_IND` principal indexing.
4. **Day count, business-day convention and calendar** for every bond. Use Cbonds `emission_cupon_basis_title` in bulk, plus residual inference from coupon amounts (extend `catalog_analytics.py:46-78` to 30/360 and ACT/360). Persist the result.
5. **Board static:** persist LOTSIZE, MINSTEP, DECIMALS, SETTLEDATE / settle code, CURRENCYID, FACEVALUEONSETTLEDATE per board from endpoint #2 (and `ndm` boards). Retire stale boards.
6. **Ratings:** persist Cbonds ratings into `rating`, one row per agency with history, for all active ISINs (bulk). The table has been empty since August.
7. **Issuer data:**
   - Discover the CBR registry URL dynamically (monthly) instead of hard-coding it.
   - Add a Cbonds `get_emitents` or NSD source for sector/OKVED, parent group and guarantors.
   - Cover structured notes; OGRN coverage there is 0%.
8. **Options:**
   - Normalize offer type (put/call/buyback) from `offertype` and Cbonds `option_type`.
   - Drop or flag `0000-00-00` offers.
   - Add AT1/perpetual call schedules (62 bonds) and the 136 put/call-subtype bonds without a future offer row.
9. **Replacement and FX bonds:** add an explicit `replacement_of_isin` / original eurobond link (Cbonds or issuer docs) and FX conversion rules. 183 active bonds have FX face and RUB settlement.
10. **Freshness:**
    - Run the bulk MOEX run **daily**, since a run takes about 12 minutes; this closes the 15-SECID new-listing gap.
    - Add an intraday "new SECID" probe on the collection (`STARTDATEMOEX` ≥ today).
    - Add CBR monthly and Cbonds daily deltas (`updating_date`).
11. **Feed enrichment back into the golden record.** The precedence should be field-specific: legal/manual > Cbonds > MOEX for conventions; MOEX > Cbonds for exchange schedule data. Conflicts are written automatically, and recovery stops re-requesting ISINs once they are resolved.
12. **Validation suite:**
    - fixtures with current MOEX labels;
    - manifest-SECID existence checks;
    - Σ future amortization = current face;
    - coupon amount ≈ face × rate × day-count;
    - offer-row consistency with `bond_subtype`;
    - Desk-system parity samples from corporate-bond-monitor as a regression oracle.
13. **Downstream fix:** the currency-bond-monitor filter on `'Валютные облигации'` (`catalog.py:57,84,123`) should use `faceunit` / `FACEUNIT` ≠ `SUR` instead of the label.
