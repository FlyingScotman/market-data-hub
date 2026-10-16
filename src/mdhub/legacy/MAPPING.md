# Legacy → hub mapping

Phase 1 (`mdhub legacy import` / `mdhub legacy verify`) copies `market-data.sqlite3` into
`legacy_md` and `bond_catalog.sqlite3` into `legacy_bc`: all tables, all rows, same names,
verified against SQLite and `SNAPSHOT.json` (`import_sqlite.py`, `verify.py`).

Phase 2 maps those copies into the normalized schemas:

* `mdhub legacy map-md [--only …]` → `map_md.py` (targets `fixings`, `meetings`, `liquidity`,
  `zcyc`, `curves`, `intraday`);
* `mdhub legacy map-bc [--only …]` → `map_bc.py` (targets `bonds`, `curated_formulas`).

The legacy schemas stay frozen as the verifiable original. Counts below are from the live run
on the 2026-10-15 snapshots (MD sha256 `3e80be57…`, BC sha256 `85294b41…`).

## Rules shared by every mapper

- **Read only `legacy_*`.** Both mappers refuse to run unless the latest import of their
  schema is verified (`--allow-unverified` overrides).
- **One `ops.job_run` per run** (`legacy_map_md` / `legacy_map_bc`), one transaction per
  target. A failing target is reported and the run ends `partial`; the others commit.
- **Idempotent.** Every write goes through `upsert_rows()` by the table's natural key; a
  re-run reports everything `unchanged` and adds no history, rejects or captures.
- **Live rows win.** A key that already exists and was written by any other job (its
  `run_id` is not a legacy-mapping run) is never touched. The legacy value is compared with
  it and the result is reported (`live_overlap` / `live_match` / `live_mismatch`, per series and
  per column, up to 10 examples). When a live loader later overwrites a legacy-written row, the
  key becomes live-owned and the mappers skip it from then on.
- **Provenance.** `capture_id` is NULL because the export blanked the raw payloads. `run_id` is
  the mapping run. The legacy row is named in `legacy_id`/`meta` (`md.curve_snapshot`),
  `details` (`md.fixing`), `evidence` (`ref.bond_rate_formula`), or by `source = 'legacy_bc'`
  (bond tables). Tables without such a column (intraday, zcyc, liquidity, meetings) are
  traceable through `run_id` → `ops.job_run`. `ingested_at` (known-at) is the legacy
  `fetched_at_utc`, i.e. when the old collector saw the value.
- **Numbers.** `REAL` → `Decimal(repr(x))` (shortest round-trip form, no rounding).
  Decimal-text columns → `Decimal(text)` verbatim. Real data has no float noise: max scale is
  4–5 for curves and 1 for liquidity. The one exception is the NTPro text
  `85.46555000000001`, which is kept verbatim.
- **Timestamps.** `…Z`, `….ffffffZ` and `+00:00` (gcurve tables) are all parsed with
  `datetime.fromisoformat` into `timestamptz`.
- **Missing ≠ zero.** Fields that exist only in blanked raw columns become NULL / `unknown`
  and are never inferred.

## 1. `legacy_md` → `md` / `ref` (`map-md`)

| Target | Legacy source (rows) | Hub table | Rules | Live run 2026-10-16 |
|---|---|---|---|---|
| `fixings` | `index_fixings` (2,607), `term_ruonia_fixings` (1,900) | `md.fixing` | `ruonia` → `RUONIA`, `key_rate` → `KEY_RATE` (unit `pct`). Each term row → `RUONIA_INDEX` (unit `index`) and `TERM_RUONIA_1M/3M/6M`. `source = 'cbr'`, key `(index_code, fixing_date, source)` as `cbr_daily` uses it. `effective_date = fixing_date`, `published_on`/`published_at` NULL (unknown). `details` = legacy table/key/fetched_at. **RUONIA date semantics checked:** the legacy `fixing_date` is the observation date. All 31 overlapping RUONIA values equal the live value of the same date (only 1 equals the next date's value). | 9,967 inserted; overlap 240 = 240 match, 0 mismatch (KEY_RATE 33, RUONIA 31, index/term 44 each) |
| `meetings` | `policy_meetings` (64) | `ref.cbr_meeting` | `is_estimated` 0/1 → bool. `decision`/rates NULL (filled by `cbr_meetings` `resolve_meetings`). `source = 'cbr'`. | all 64 already live. Before resolution, 63 differed in `is_estimated` only. After `cbr_meetings` re-resolved with the mapped KEY_RATE history: 36 match, 28 differ (23 `is_estimated` for meetings before the KEY_RATE history starts in 2021-08; 12 `effective_date`, see §4) |
| `liquidity` | `cbr_liquidity_balance_revisions` (326) + `cbr_liquidity_captures` | `md.cbr_liquidity` (+ `ops.row_history`) | Latest revision per `observed_on` → row, with `revision_no` = legacy `revision_number` and `ingested_at` = fetch time of its capture. Superseded revisions (15) → `ops.row_history` (same JSON shape as `ops.track_changes`), with `replaced_at` = fetch time of the superseding capture, and only for legacy-owned dates. **Columns are mapped by position** (CBR table columns 2..15), see §4. `cbr_liquidity_balances` is cross-checked against the latest revisions (equal). | 279 inserted, 7 history rows; overlap 32 = 32 match |
| `zcyc` | `gcurve_snapshots` (1,167) | `md.zcyc_snapshot` / `md.zcyc_reject` | `parameters_json` (13 numbers) → `params` {B1,B2,B3,T1,G1..G9}. `standard_yields_json` → `[{tenor_years, yield_pct}]`. `trade_time` = Moscow time of `source_observed_at_utc`. Every snapshot is re-validated with the live loader's KBD check (`check_zcyc`, 0.01 bp); failures go to `md.zcyc_reject` with a synthetic capture that holds the legacy record (endpoint `legacy_gcurve_snapshot`, deterministic, so re-runs reuse it). All legacy rows are `is_eod = false`; the EOD flag is set only by the live `eod`/`final` runs. | 1,157 inserted, 0 rejected (all pass, max error < 0.005 bp); overlap 10 (legacy poll = live EOD publication) = 10 match |
| `curves` | `curve_snapshots` (4,225), `curve_quotes` (61,666) | `md.curve_snapshot` / `md.curve_quote` | Sources: `ratescalcs`, `andromeda`, `otc_monitor`, `cfets_chinamoney` → `cfets` (migration `0301_legacy_sources`). Datasets: `cnyruonia_xccy` → `cny_ruonia_xccy`, `cfets_fr007_irs_closing` → `cfets_fr007_irs`, `cfets_fdr007_irs_closing` → `cfets_fdr007_irs`, others kept. Tenors use the live convention (`rc.tenor_parts`: upper case, `tenor_months` for M/Y, `tenor_days` for W/D/ON, positions 0-based). bid/ask quotes: mid = (bid+ask)/2 recomputed like the live loader; a differing legacy mid is kept in `extra.legacy_mid_pct` (9 CFETS quotes publish their own rounded mid). mid-only quotes (andromeda, otc_monitor off-hours, XCCY): bid/ask NULL, mid kept. A per-quote basis that differs from the snapshot's goes to `extra.quote_basis`. `content_sha256` = the live Ratescalcs `content_hash` when every quote has bid/ask (so live dedup continues seamlessly); mid-only quotes add their mid. **Dedup:** a snapshot is stored only when its content differs from the previous snapshot of (source, dataset) in time order, like the live loader. `parser_version = legacy_v<n>`, `legacy_id` = legacy id, `meta.legacy` = legacy ids/hashes/clocks. **Archive batches** (andromeda: ~970 historical curves per fetch time) would collide on (source, dataset, fetched_at) and would make history invisible to as-of reads: `fetched_at` := 23:59:59 Moscow time on the curve's source date, flagged `fetched_at_synthetic`, with the real fetch time kept in `meta.legacy.fetched_at_utc`. Legacy-owned snapshots that a re-run no longer produces are pruned. The live `swaps_otc_monitor` loader uses the same tenor order, basis rules and hash (`mdhub.sources.otc_monitor.content_hash`), so its first poll dedups against the last mapped `otc_monitor` snapshot. | 2,188 snapshots / 31,737 quotes inserted; 2,037 unchanged repeats deduplicated |
| `intraday` | `intraday_quote_observations` (434,995) + captures | `md.fx_intraday` (spot) / `md.futures_intraday` | `source`: `moex_apim`, `moex_passport_realtime` → `moex_passport`, `ratescalcs`. `source_variant` kept, except the legacy APIM realtime `moex_apim` → `apim_marketdata` (the live variant of the same feed). `ts` = `source_observed_at_utc`, `source_ts` NULL (no SYSTIME in legacy). Spot: `bid/offer/last` (RUB per unit, as CETS). Futures: values are `rub_per_base_unit` → `*_rub_per_unit`; raw contract-point columns stay NULL. `trade_date` = `session_date`. `latency` kept (`derived` for AlgoPack obstats-mid and NTPro). **AlgoPack dedup:** one row per (instrument, source, variant, ts), keeping the last fetched version. 323,699 re-captures collapsed; only 1 key had differing values. Non-obstats series are then stored change-only like the live loaders (9,108 unchanged consecutive rows dropped); obstats bars are kept one per 5-minute interval. NTPro USD/RUB_TOM: `source = 'ratescalcs'`, `secid = 'USD/RUB_TOM'`, `board = 'NTPRO'`. The **mislabelled `usdrubf_fallback_snapshot_clock`** row (legacy "USD/RUB_TOM spot") → `md.futures_intraday` `secid = 'USDRUBF'`, source `ratescalcs`, its own variant (flagged in the report). | fx 23,252 (obstats 3,614, passport 12,438, NTPro 7,200) + futures 78,936 inserted; no live overlap (legacy ends 2026-10-15) |

Legacy-only (not mapped): `derived_*`, `projection_*`, `gcurve_zero_curve_*`,
`normalized_base_curve_*`, `curve_world_*`, `curve_source_policies`, `rubik_*`, `instruments`,
`instrument_identifiers`, `price_observations`, `backfill_requests`, `sources`, `datasets`,
`ingestion_runs`, `schema_migrations`, `cbr_liquidity_capture_observations`,
`cbr_liquidity_balances` (cross-check only), `intraday_quote_captures` (joined for provenance).

## 2. `legacy_bc` → `ref` (`map-bc`)

The **`bonds`** target maps only SECIDs **absent from `ref.bond` after a live `bonds_static`
run** (it refuses while `ref.bond` has no live rows). All bond tables are written in one
transaction (the FKs to `ref.bond` are deferred). Derivations reuse the live loader's
functions in `mdhub.sources.moex_static`, so mapped rows look like loaded ones.

| Legacy (rows) | Hub table (key) | Rules |
|---|---|---|
| `issues` (16,023) | `ref.bond` (`secid`) | `source = 'legacy_bc'`. `bond_class` / `coupon_type` / `coupon_type_basis` via `ms.classify_bond` / `ms.classify_coupon_type`; inputs are the MOEX labels (`type`; `bondization_only` → NULL), issuer INN/name, `coupon_benchmark`, the coupon-value flags of the legacy schedule, and `struct_flag` from the legacy `instrument_class` (`structured*`). `face_unit` / `currency_id` via `ms.norm_ccy` (SUR → RUB). `maturity_date` `0000-00-00` → NULL; `is_perpetual` uses the loader's rule (subtype `Бессрочные`, or `До оферты (call)` without maturity or redemption rows). `status` = `matured` if maturity < today, else `delisted`; `in_directory` = `is_traded` = false. `is_amortizing` = any mapped `amortization` row. `first_seen_at` = time of the legacy `first_seen_run`. Labels that exist only in raw (`coupon_details`, `facevalue_type`, `lat_name`, `start_date_moex`, `high_risk`, `is_subordinated`, …) stay NULL. Volatile market fields (`coupon_percent`, `next_coupon_date`, `accrued_interest`, `market_status`) are dropped. Duplicate ISIN `RU000A0D05B7`: the ISIN stays on SECID `RU000A0D05B7`, and `RU22TRMK2009` gets NULL. |
| `issuers` (1,670) + CBR columns of `issues` | `ref.issuer` (`issuer_id`) | Only issuers of mapped bonds that are missing from `ref.issuer`: `name` = `title`, `inn`, `okpo`, `ogrn` / `country` / `short_name` from the CBR registry columns, `is_sovereign` = Minfin INN. |
| `boards` (134,427) | `ref.bond_board` (`secid, board`) | `is_primary`, `listed = false`, `listing_source = 'description'`, `first_seen` / `last_seen` = dates of the legacy run ids. `board_json` is blank, so `lot_size`, `min_step`, `decimals`, `settle_code`, `currency_id` are NULL. |
| `events` `coupon` | `ref.bond_coupon` (`secid, coupon_no`) | `coupon_no` via `ms.number_coupons`; `status` via `ms.coupon_status` (`unknown` when no value). `amount` = `value`, `value_prc` = `rate_pct`, `value_rub` = amount for RUB face only. `start_date` / `record_date` / `face_value` are NULL (raw only). |
| `events` `amortization` | `ref.bond_amortization` (`secid, amort_date, data_source`) | `data_source` = `maturity` on the maturity date, else `amortization` (legacy has no data_source). Rows with value 0 and rate 0 are not mapped ("no principal", audit 04). |
| `events` `offer` | `ref.bond_offer` (`secid, source, offer_key`) | Via `ms.normalize_offers` with option dates from `issues.put/call_option_date` and the subtype. `0000-00-00` → `date_missing`, key `undated-n`, status `undated`. `offer_type` = `unknown` unless the option dates match (`offertype` is raw only); `value` = legacy `value`, `price` NULL. |
| `issues.coupon_benchmark(_spread)` | `ref.bond_rate_formula` (`secid, source='legacy_bc'`) | As the loader's `_formula_row`: benchmark via `ms.map_benchmark`, `spread_bp` = spread × 100, status `inferred` / `partial`, evidence = legacy fields. |
| `formula_models` | `ref.bond_rate_formula` (`secid, source='curated'`) — target **`curated_formulas`** | For **every bond in `ref.bond`** (active included): models with status `exact_curated` / `partial_public_formula` **and a benchmark**. `MOEX G-curve 5Y` → `ZCYC`, `RUONIA`, `RUONIA compounded index` → `RUONIA_INDEX`. `lag_days` from the model (`lag_calendar_days` / `fixing_calendar_days_before_start` / `index_lag_calendar_days`). `averaging = arithmetic` for `arithmetic_*`. Status `curated` / `partial`. Stepped margins, day count and missing terms go into `evidence`. The `exact_curated` **fixed** model (RU000A10A8E8, 7.5 % 30/360) is not a floater formula and is skipped (reported). |

**Live run 2026-10-16** (after `bonds_static` full, 16,055 bonds: 10,760 matured / 4,033
active / 1,262 delisted): all 16,023 legacy SECIDs are in the live load, and every legacy SECID
with coupon or amortization events has live schedule rows. So `bonds` maps **nothing** today; it
stays as a safety net for SECIDs that ISS might drop later. `curated_formulas` wrote 3 rows
(RU000A1028U9 ZCYC lag 6; RU000A10F504 RUONIA_INDEX lag 10, partial; SU29015RMFS3 RUONIA
arithmetic lag 7, spread 0). The fixed model RU000A10A8E8 was skipped.

Legacy-only: `issue_revisions`, `event_revisions` (raw blank), `runs`, `source_availability`,
`conflicts` (2, manual review), `ratings` (0), `cbonds_*` (evidence for `cbonds_enrich`).

## 2a. Andromeda CNY export → `md.curve_snapshot` (`mdhub legacy andromeda-cny`)

The owner's export of the Andromeda CNY swap archive (`andromeda_cny_swaps.xlsx`, sheets
`CNY_IRS_RUSFAR_OIS` and `XCCY`, or the same tables as CSV) is not part of the legacy SQLite
stores; `mdhub.legacy.andromeda_cny` imports it with the conventions of the legacy andromeda
mapping. The file itself is not in the repository.

| Item | Rule |
|---|---|
| Datasets | `CNY_IRS_RUSFAR_OIS` → `cny_rusfar_ois`, `XCCY` (CNY vs RUONIA) → `cny_ruonia_xccy`; source `andromeda`; label = sheet name; `parser_version = andromeda_cny_v1` |
| Raw | each input file verbatim in `ops.raw_capture` (endpoint `file:andromeda_cny.xlsx` / `file:andromeda_cny.csv:<dataset>`, `url_redacted = file:<name>`, `fetched_at` = `source_ts` = export time → a re-import reuses the capture) |
| Values | mid-only percent; bid/ask NULL, `quote_basis = mid_only`. Workbook numbers are rounded to Excel's 15 significant digits (the file carries float noise such as `8.029999999999999`; the CSV shows `8,03%`), trailing zeros dropped; the xlsx and the CSV of the same export import identically. `-`/empty = missing → no quote row; negative values are real (2025) |
| Days | one snapshot per (dataset, date). Some tenors missing → stored with the present ones (positions 0-based in column order), `meta.quality_flags` `andromeda_partial_curve` + `meta.missing_tenors`; no value at all → not stored. Weekend dates (MOEX Saturday sessions 2024-04-27, 2024-11-02, 2024-12-28, 2025-11-01) are stored like any day |
| fetched_at | dates before the export's Moscow date: 23:59:59 MSK on the date, flagged `fetched_at_synthetic` (as the legacy archive batches). The export-day row: the real export time, `meta.export = 'intraday'`, from `docProps/core.xml dcterms:modified` (UTC) or `--exported-at` (required for CSV-only input; extracted-file mtimes are not trusted) |
| Hash / key | `content_sha256` = `otc_monitor.content_hash` (= legacy `curve_content_hash`); natural key `(source, dataset, fetched_at)` as the legacy mapping, i.e. one EOD key per date. No "latest stored" dedup (that is the live-poll rule) |
| Revisions | a later export that changes a stored day updates it in place; history is enabled on `md.curve_snapshot` / `md.curve_quote` (migration 0424); a tenor the later export no longer has is deleted and its old row written to `ops.row_history` by the importer. Snapshots are never deleted. The export-day intraday snapshot stays when a later export brings the same date as EOD (two snapshots, different `fetched_at`) |
| Fail closed | bad header/date/number, duplicate date, value outside the dataset bounds (CNY −50..300 %), date after the export date, two inputs disagreeing on a day → nothing written |
| Quality flags | `jump_gt_500bp` (a tenor moved > 500 bp vs the previous date with values, also across exports; `meta.jumps_bp`, `meta.jump_prev_date`), `integer_grid_curve` (≥ 6 values, all whole numbers), `flat_curve` (≥ 6 equal values); curated `meta.quality_note` for 2026-03-18 (stylized placeholder 17/16/14…10 resp. 18/16/16…10 on both sheets) and 2024-09-18 (series maximum 43.15 %, September 2024 CNY squeeze: two days at 42–43 %, reverting on 09-20, same front end on both sheets — judged real) |
| Quality job | not registered as datasets (history only, not scheduled, nothing can backfill them) |

**Import of the 2026-10-16 export** (throwaway database; export time 2026-10-16 18:05:15 MSK from
`dcterms:modified` 15:05:15Z — the zip entries' DOS times 15:04–15:05 are the export host's UTC
wall clock, the zip reached the owner's machine at 18:05:57 MSK): `cny_rusfar_ois` 646 rows →
646 snapshots / 7,398 quotes (2023-12-12 → 2026-10-16; 87 partial days, 0 empty, 77 negative
values; flags: jump 15, integer grid 40, flat 1); `cny_ruonia_xccy` 746 rows → 746 snapshots /
8,048 quotes (2023-07-10 → 2026-10-16; 177 partial, 0 empty, 54 negative; jump 22, integer grid 8,
flat 2). Re-import of the workbook and import of the two CSVs: all unchanged. The seed DB had no
andromeda CNY snapshots (nothing to reconcile); against `otc_monitor` `cny_ruonia_xccy` (14
overlapping days) the median difference is 15 bp, max 85 bp.

## 3. Data-quality exclusions and flags (audit 04 §6 P3)

| Item | Implemented rule |
|---|---|
| ratescalcs KRS 2-tenor snapshots 2026-08-05..08-10 (parser v1) | Mapped but flagged `meta.quality_flags = ["ratescalcs_krs_partial_2_tenors"]`. The import has **18 snapshots / 36 quotes**; audit 04's "36" counts quotes. After content dedup, 2 distinct partial curves remain. |
| andromeda partial curves before 2023-07 | Mapped but flagged `andromeda_partial_curve` (fewer than 17 RUONIA / 13 KRS tenors): 148 snapshots (98 + 50), all before 2023-07. Andromeda quotes are mid-only (bid = ask in legacy) → bid/ask NULL. |
| SUR face unit | `ms.norm_ccy` → RUB. |
| `0000-00-00` | maturity → NULL (+ perpetual rule); offers → `date_missing`, `undated-n`. |
| Duplicate ISIN RU000A0D05B7 | ISIN kept on the SECID equal to it; the other SECID gets NULL (only if both reach `ref.bond` from legacy). |
| Zero amortization rows | Not mapped. |
| `+00:00` vs `Z` | Parsed to `timestamptz`. |
| Repeated intraday rows | Collapsed (see `intraday`). |
| Blank event `raw_json` | Raw-only fields NULL / `unknown`. |
| `index_fixings.fetched_at_utc` ≥ 2026-08-06 for all history | Kept only as `ingested_at` / `details.legacy_fetched_at`; `published_at` stays NULL. |

## 4. Findings from the live overlap (for loader owners)

- **CBR liquidity, columns 6/7.** The legacy schema labels CBR columns 6 and 7 "repo/FX swap
  standing" and "secured loans auctions". The hub parser (`sources/cbr.py`
  `LIQUIDITY_COLUMNS`) labels them "secured loans auctions" and "repo/FX swap standing". The
  values agree with the live CBR load only by position (32/32 dates), so the mapper maps by
  position. One of the two label sets is wrong; someone should check it against the published
  page header.
- **CBR meetings.** The live `cbr_meetings` gives Saturday effective dates for 2021-02..07
  meetings (e.g. 2021-02-12 → 2021-02-13), which looks like a calendar gap before 2021-08. The
  legacy dates differ on May-holiday weeks (legacy "next Monday" vs the live RU calendar, where
  the live value looks right) and on 2024-04-26 (live 04-27, a working Saturday with a KEY_RATE
  row; legacy 04-29). Live rows win in all cases.
