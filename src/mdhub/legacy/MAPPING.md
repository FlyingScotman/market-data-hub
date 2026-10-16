# Legacy → hub mapping (phase 2 plan)

Phase 1 (done, `mdhub legacy import` / `mdhub legacy verify`) copies `market-data.sqlite3`
into `legacy_md` and `bond_catalog.sqlite3` into `legacy_bc`: all tables, all rows, same
names, verified byte-exact against SQLite and `SNAPSHOT.json` (see `import_sqlite.py`,
`verify.py`). This document is the plan for phase 2: moving that history into the
normalized `md` / `ref` schemas (DESIGN §4). The legacy schemas stay as the frozen,
verifiable original. No mapping code exists yet.

Row counts are from the verified import of the 2026-10-15 snapshots
(MD sha256 `3e80be57…`, BC sha256 `85294b41…`).

## General rules for every mapper

- **Read only from `legacy_*`; never modify it.** Mapping is repeatable: identical
  re-runs must be no-ops (`upsert_rows()` by natural key).
- **Fresh loaders win.** Where a live loader covers the same key (fixings, zcyc, intraday,
  bond static), legacy rows are inserted only for keys that are absent
  (`ON CONFLICT DO NOTHING` semantics, or upsert restricted to absent keys). Differences
  for keys that both have are reported (`ops.quality_report`), not silently overwritten.
- **Provenance.** One `ops.job_run` per mapper run (job `legacy_map_<target>`); mapped
  rows carry that `run_id`, `capture_id = NULL` (raw payloads were blanked in the export),
  and `legacy_id` where the target has it. The source is the *original provider*
  (`ratescalcs`, `andromeda`, `otc_monitor`, `cbr`, `moex_apim`, …), never `legacy`.
- **Numbers.** `REAL` → `numeric` via `Decimal(repr(x))` (shortest round-trip string, no
  float noise added, no rounding). Text decimals (`bid_text`, `*_text`) → `Decimal(text)`.
- **Timestamps.** Legacy UTC strings come as `…Z`, `….ffffffZ` and `+00:00` (gcurve tables
  only). Parse with `datetime.fromisoformat()` and store `timestamptz`; never compare the
  strings. Business dates stay `date` (Moscow).
- **Missing ≠ zero.** Blank payload columns (`raw_payload`, `raw_json`, `board_json`,
  `payload_json` = `''`) mean *unknown*: fields that exist only in raw become NULL /
  `unknown`, never guessed or defaulted.
- **Quality exclusions** (audit 04 §6 P3) are applied by the mappers and listed in
  §4 below; excluded rows stay in `legacy_*` and are counted in the mapper's report.

## 1. `legacy_md` (market-data.sqlite3)

| Legacy table (rows) | Target | Notes |
|---|---|---|
| `curve_snapshots` (4,225) | `md.curve_snapshot` | `source` = `sources.code` via `datasets`; `dataset` from `datasets.code`: `ruonia_ois`, `key_rate_swap`, `cnyruonia_xccy` → `cny_ruonia_xccy`, `cfets_fr007_irs_closing` → `cfets_fr007_irs`, `cfets_fdr007_irs_closing` → `cfets_fdr007_irs`. `fetched_at` = `fetched_at_utc`, `source_ts` = `source_observed_at_utc` (NULL for 516 rows: ratescalcs has no source clock), `quote_basis`, `parser_version`, `legacy_id` = `id`. `content_sha256` is computed from the quotes (canonical tenor/bid/ask/mid), **not** `payload_sha256` (that hashes the omitted raw page; keep it only as evidence in a note column if the target has one). Consecutive snapshots with equal content per (source, dataset) are not duplicated (DESIGN): ratescalcs collapses 257+254 polls into ~52 distinct curves. otc_monitor republishes under the same `source_observed_at` (356 groups): keep every distinct content, latest `fetched_at` wins for as-of reads. Needs `ops.source` rows for `andromeda`, `otc_monitor` (seeded) and `cfets_chinamoney` (**not seeded**). |
| `curve_quotes` (61,666) | `md.curve_quote` | `snapshot_id` → new snapshot id; `tenor`, `tenor_months` (NULL for 1W/2W by design), `bid_pct`/`ask_pct`/`mid_pct`. Per-quote `quote_basis` wins over the snapshot's (2,136 XCCY quotes are `bid_ask_mid` inside `mid_only` snapshots). For `mid_only` quotes (all andromeda: bid = ask) store `bid_pct`/`ask_pct` NULL, only `mid_pct`. |
| `gcurve_snapshots` (1,167) | `md.zcyc_snapshot` | `trade_date`; `trade_time` = Moscow wall time of `source_observed_at_utc`; `source_ts` = `source_observed_at_utc` (`+00:00` form). `params` from `parameters_json` (13 numbers in ISS order B1, B2, B3, T1, G1..G9; **verify units of G against one fresh ISS zcyc load before mapping**), `standard_yields` from `standard_yields_json` (`[[tenor_years, yield_pct]…]` → `[{tenor_years, yield_pct}]`). `check_max_error_bp` = `max_reconciliation_error_bp`, `check_tolerance_bp` = 0.01 (legacy CHECK). `is_eod` only where it matches the ISS `?date=` EOD publication (fresh `zcyc` final run), otherwise false. Fresh loads win on (trade_date, trade_time). |
| `intraday_quote_captures` (63,715) | — (provenance only) | Used to join `source_code`, `fetched_at_utc`, `minute_bucket_utc`. Raw payload blank. |
| `intraday_quote_observations` (434,995) | `md.fx_intraday` (spot) / `md.futures_intraday` (dated + perpetual futures) | See §3 (dedup and target-constraint gaps). `ts` = `source_observed_at_utc`, `source_ts` = same, values from `bid_text`/`offer_text`/`last_text` (Decimal text, exact). Spot CNYRUB_TOM (CETS): `secid` = `instrument_code`, `board` = `CETS`, prices are RUB per unit. Futures: legacy prices are `quote_unit = rub_per_base_unit` → `bid/offer/last_rub_per_unit`; contract-point columns (`bid`, `offer`, `last`) stay NULL (not derived). `latency`/`source_variant` from the legacy columns. ratescalcs `USD/RUB_TOM` (19,529 NTPro dealer quotes, `board` NULL) is **not** a MOEX series: legacy-only unless an `md` series with an explicit `source` is added. |
| `index_fixings` (2,607) | `md.fixing` | `index_code`: `key_rate` → `KEY_RATE` (1,324; by effective date, matches CBR change history), `ruonia` → `RUONIA` (1,283). `value` = `rate_pct`, `source` = `cbr`, `published_at` NULL (unknown; `fetched_at_utc` is when the collector saw it, ≥ 2026-08-06 for all history — do not use it as as-known time before that date). Fresh `cbr_daily` wins. |
| `term_ruonia_fixings` (1,900) | `md.fixing` | One legacy row → 4 fixings on `fixing_date` (calendar days, incl. weekends): `RUONIA_INDEX` (`index_value`), `TERM_RUONIA_1M/3M/6M` (`one/three/six_month_pct`). `source` = `cbr`. |
| `policy_meetings` (64) | `ref.cbr_meeting` | `meeting_date`, `effective_date`, `is_estimated` (0/1 → bool), `source` = `cbr`, `decision` NULL (not in legacy). Refresh from CBR before relying on it (last fetched 2026-10-01). |
| `cbr_liquidity_balance_revisions` (326) | `md.cbr_liquidity` (+ `ops.row_history`) | Latest revision per `observed_on` (= `cbr_liquidity_balances`, 311 rows) becomes the row: 14 `*_bn` metrics (billions RUB; keep the unit in the column names), `methodology_version` (switch 2023-11-01). The 15 superseded revision-1 rows go to `ops.row_history` with `replaced_at` = fetch time of the superseding capture (`cbr_liquidity_captures.fetched_at_utc`), not the mapping time. |
| `cbr_liquidity_balances` (311) | — (cross-check) | Current-state view of the revisions; its `fetched_at_utc` is overwritten on every fetch (all 2026-10-15T06:00:10Z) — never use it as an observation time. Mapper asserts it equals the latest revisions. |
| `cbr_liquidity_captures` (49), `cbr_liquidity_capture_observations` (12,812) | — (provenance only) | Which capture saw which revision; payload blank; one `legacy_normalized` synthetic capture. |
| `sources` (4), `datasets` (9), `ingestion_runs` (2,871), `schema_migrations` (17) | legacy only | Lookups for the mappers. |
| `derived_curve_builds` (4,514), `derived_curve_nodes` (44,473), `derived_projection_nodes` (31,738), `derived_curve_dependencies` (1,795), `projection_curve_build_meta` (1,669), `projection_curve_scenarios` (5,007), `derived_projection_scenario_nodes` (68,751) | legacy only | Analytics (stripped curves), out of hub scope; rebuildable from `md.curve_quote`. |
| `gcurve_zero_curve_builds` (1,167), `gcurve_zero_curve_nodes` (12,837) | legacy only | Derived from G-curve params. |
| `normalized_base_curve_bundles` (107), `_snapshots` (319), `_nodes` (4,676) | legacy only | Already bootstrapped USD/EUR/CNY curves; the hub stores input quotes, not bootstraps. |
| `curve_world_definitions` (11), `curve_world_instances` (0), `curve_source_policies` (0), `rubik_curve_artifacts` (0), `rubik_curve_nodes` (0) | legacy only | Curve-selection policy / proprietary curves. |
| `instruments` (0), `instrument_identifiers` (0), `price_observations` (0), `backfill_requests` (0) | legacy only | Empty. |

## 2. `legacy_bc` (bond_catalog.sqlite3)

Scope: **only issues absent from the fresh MOEX static load** (`ref.bond` filled by
`bonds_static`) are mapped — i.e. matured / delisted history that ISS no longer lists.
Issues present in the fresh load are not touched (the live data is authoritative and
richer: the legacy raw columns are blank). Everything below applies to that subset;
`source` = `legacy_bc`, `status` ∈ {`matured`, `delisted`} (by `maturity_date` < run date
else `delisted`).

| Legacy table (rows) | Target | Notes |
|---|---|---|
| `issues` (16,023) | `ref.bond` | `secid`, `isin`, `regnumber`, `issuer_id` = `emitent_id`, `name`, `short_name` = `shortname`, `bond_type` = `bond_type` (MOEX label) and `bond_class` derived from `type` / `bond_type` / `instrument_class`, `face_value` = `facevalue`, `initial_face_value`, `face_unit` = `faceunit` (SUR → RUB), `currency_id` = `currency` (settlement; SUR → RUB), `issue_date`, `maturity_date` (`0000-00-00` → NULL, `is_perpetual` only if `bond_subtype` = Бессрочные, else maturity unknown), `coupon_frequency`, `qualified_only` = `qualified_investors`, `listing_level` = `list_level`, `issue_size`, `primary_board` = `primary_boardid`, `is_structured` from `instrument_class`. `coupon_type` from `formula_models.model_type` / `coupon_benchmark` (unknown when unclassified). Volatile market fields (`coupon_percent`, `coupon_value`, `next_coupon_date`, `accrued_interest`, `market_status`) are not static: dropped. CBR registry columns → `ref.issuer` (ogrn, legal name) where useful. |
| `issuers` (1,670) | `ref.issuer` | Only issuers referenced by mapped bonds and absent from the fresh load: `issuer_id` = `emitent_id`, `inn`, `okpo`, `name` = `title`. `industry_*` always NULL in legacy. |
| `boards` (134,427) | `ref.bond_board` | For mapped bonds: `board` = `boardid`, `is_primary`, `first_seen`/`last_seen` = dates of `first/last_seen_run` (run ids are UTC timestamps `YYYYMMDDTHHMMSSZ`). `board_json` blank → `lot_size`, `min_step`, `decimals`, `settle_code`, `currency_id` NULL. 111k rows are stale (last seen 2026-08-01): `listed` = false unless seen in the latest run. |
| `events` (256,559) type `coupon` (203,724) | `ref.bond_coupon` | `pay_date` = `event_date`, `coupon_no` = `ordinal` (0 = unnumbered → ordinal by date, as DESIGN), `amount` = `value`, `value_prc` = `rate_pct`, `start_date`/`record_date`/`face_value` NULL (raw blank: **unknown**, not inferred). `status` = `known` if `value` not NULL else `unknown`. |
| `events` type `amortization` (46,311) | `ref.bond_amortization` | `amort_date` = `event_date`, `value` = `value`, `value_prc` = `rate_pct`, `face_value` NULL. Zero-value rows: see §4. |
| `events` type `offer` (6,524) | `ref.bond_offer` | `offer_date` = `event_date` (`0000-00-00` → NULL + flagged, 595 rows), `offer_type` = `unknown` (`offertype` only in the blank raw), `price`/`value` from `value`/`rate_pct` only when present, `status` = `unknown`. |
| `formula_models` (16,023) | `ref.bond_rate_formula` | For mapped bonds with `model_type` other than `unclassified`/`fixed_rate`: `benchmark` (RREFKEYR → KEY_RATE, RUONIA, RUONIA3M → RUONIA_3M, RUONIA6M → RUONIA_6M, RUONIA_IND, CPI, ZR_YLD_CRV → ZCYC), `spread_bp` = `spread_pp` × 100, conventions from `model_json`; `status`: `exact_curated` → `curated`, `partial_*` → `partial`, `preliminary_*` / `fallback_unverified` → `inferred`, `unavailable` / `refused_exact` → no row; `evidence` = legacy `source` + `missing_terms_json`. (Decision pending: the 2 `exact_curated` + 1 `partial_public_formula` models may also seed active bonds as curated evidence.) |
| `issue_revisions` (72,753), `event_revisions` (265,231) | legacy only | Raw blank; only hashes and run ids remain. |
| `runs` (23), `source_availability` (64,092), `conflicts` (2), `ratings` (0) | legacy only | `conflicts` (RU000A10A8E8 amortization rounding, RU000A1028U9 missing call) → manual review. |
| `cbonds_*` (21 / 80 / 15 / 4,052 / 26) | legacy only | Evidence for `cbonds_enrich` (day count of 15 ISINs); payloads blank. |

## 3. Intraday dedup and target gaps (`intraday_quote_observations`)

| source_code / source_variant | instrument_type | latency | rows | distinct (instrument, ts) | target |
|---|---|---|---|---|---|
| moex_apim / algopack_obstats_mid | dated_future | derived | 274,785 | 55,853 | `md.futures_intraday` |
| moex_apim / algopack_obstats_mid | perpetual_future | derived | 91,971 | 18,691 | `md.futures_intraday` |
| moex_apim / algopack_obstats_mid | spot (CNYRUB_TOM) | derived | 31,037 | 3,614 | `md.fx_intraday` |
| moex_apim / moex_apim | dated + perpetual | realtime | 4,524 | 4,524 | `md.futures_intraday` (`source_variant` = `apim`) |
| moex_passport_realtime / passport_cets_realtime | spot (CNYRUB_TOM) | realtime | 13,149 | 13,149 | `md.fx_intraday` |
| ratescalcs / ntpro_snapshot_clock (+1 usdrubf_fallback) | spot (USD/RUB_TOM) | derived | 19,529 | 15,465 | legacy only (non-MOEX) |

- **AlgoPack duplicates.** Each minute re-captured the open 5-minute obstats bar: collapse
  to one row per (instrument, `source_observed_at_utc`, source_variant), taking the
  **last fetched** version (a bar can change until it closes). `raw_row_sha256` differs
  within ~1,270 keys, but bid/offer/last differ for only 1 key, so the choice is almost
  never visible. Earlier versions remain in `legacy_md` only.
- **Change-only storage.** After dedup, drop rows whose values equal the previous stored
  row of the same series (the live loaders store changed rows only).
- **Weekend rows** (`algopack_obstats_mid` Sundays 2026-08-30, 09-06, 09-20, 10-04, 10-11,
  all stamped 16:00:00Z) are kept (they are real weekend-session bars).
- **ratescalcs evening repeats**: USD/RUB_TOM rows captured after 16:00Z (from 2026-09-15)
  have `source_observed_at` frozen at 16:00:00Z (stale repeats) — moot while the series
  stays legacy-only.
- **Target constraint gaps (shared change needed before mapping):** `md.fx_intraday.latency`
  allows only `realtime`/`delayed` and `md.futures_intraday.source_variant` only
  `apim`/`iss_public`. The legacy obstats-derived rows need `latency = 'derived'` and a
  variant such as `algopack_obstats_mid` (or a separate target); passport rows need a
  variant/`source` distinguishing them from ISS CETS marketdata.

## 4. Data-quality exclusions and normalizations (audit 04 §6 P3)

| Item | Rule in phase 2 |
|---|---|
| ratescalcs KRS partial curves 2026-08-05..08-10 (only 3M/6M of 13 tenors, parser v1) | Not mapped: ratescalcs/`key_rate_swap` snapshots with 2 quotes, fetched 2026-08-05T09:17Z..08-10T07:18Z. The import shows **18 snapshots / 36 quotes** (audit 04 says "36 rows": quotes, not snapshots). Unrecoverable without raw. |
| andromeda partial curves before 2023-07 (RUONIA 98, KRS 50 snapshots) | Not mapped: andromeda snapshots with fewer quotes than the full set (17 RUONIA / 13 KRS tenors), all with `source_observed_at` < 2023-07-01. Select by quote count and *source* date: the whole archive has `fetched_at_utc` = 2026-08-06T09:19:48Z. Counts confirmed on the import (98 / 50). |
| `SUR` face unit | Mapped to `RUB` (`faceunit`, `currency`); 11,979 issues. |
| `0000-00-00` dates (58 issue maturities, 595 offer dates) | NULL + flag; perpetual only when `bond_subtype` says so. Never compared as dates. |
| Duplicate ISIN `RU000A0D05B7` (SECIDs `RU000A0D05B7` and `RU22TRMK2009`, both not traded) | If it reaches `ref.bond`: keep the ISIN on the SECID present in the fresh load, else on `RU000A0D05B7` (SECID = ISIN); the other gets `isin` NULL and a note. |
| Amortization rows with value 0 (13,736; mostly structured notes) | Not mapped as principal flows ("no principal", not a schedule error); counted in the report. |
| 75 issues whose amortization sum ≠ initial face (> 1 %) and 3 coupon rates > 60 % (RU000A102FH4, RU000A102FW3, RU000A108C41) | Mapped as-is, listed in the quality report for review. |
| `+00:00` vs `Z` (gcurve tables vs the rest) | Parsed to `timestamptz` (see general rules). |
| Repeated intraday rows (77,820 identical `raw_row_sha256` across captures) | Deduplicated as in §3. |
| Blank `raw_json` on events / issue revisions | Event fields that exist only in raw (`startdate`, `recorddate`, `offertype`, `price`, per-event `facevalue`) are `unknown`/NULL; nothing is inferred from neighbours. |
| `index_fixings.fetched_at_utc` ≥ 2026-08-06 for all history | Not used as as-known time; `published_at` stays NULL for legacy fixings. |
| CBR liquidity rows on non-MOEX days (7) | Mapped (CBR publishes them); flagged for a cbr.ru spot check. |
