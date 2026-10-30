# Market Snapshot Contract v1

The hub (market-data-hub) owns this contract: JSON Schema (draft 2020-12) of the immutable
snapshots it publishes, plus golden fixtures. The system outline is
`trading_desk_monitor/docs/architecture/02_MARKET_DATA_AND_CORE_CONTRACT.md` §3; hub internals
are in [`docs/DESIGN.md`](../../../docs/DESIGN.md) §11.

| File | What |
|---|---|
| `snapshot.schema.json` | Entry point: any bundle (`oneOf` static / market) |
| `static.schema.json` | Bundle of kind `static` |
| `market.schema.json` | Bundle of kind `market` |
| `manifest.schema.json` | Manifest = envelope without `data`, plus `snapshot_id` (`GET /v1/snapshots/{id}/manifest`) |
| `common.schema.json` | Shared definitions: decimal strings, dates, UTC instants, sha256, labels |
| `fixtures/` | Golden bundles built from public data (`index.json` lists their ids) |

`schema_version` is `market_snapshot/1.6`. Every schema object has
`additionalProperties: false`: a new field is a schema version bump, so a consumer pinned to
an older version fails its conformance tests instead of silently ignoring it.

**Versions.** A minor version (`1.x`) only adds fields, datasets, enum values or labels; it
never removes, renames or changes the meaning or unit of an existing field. A consumer that
reads `1.x` and ignores unknown additive fields stays correct; a new major (`2.0`) is refused.

First hub release with each version: 1.0 = 0.1.3, 1.1 = 0.1.7, 1.2 = 0.2.0, 1.3 = 0.3.2,
1.4 = 0.3.4, 1.5 = 0.4.0 (in production since 2026-10-23); 1.6 is in `main`, planned for 0.5.0.

| Version | Change (all additive) |
|---|---|
| 1.0 | initial contract |
| 1.1 (WEB-13, TDM brief R9) | Instrument `lat_name` (HS12), `coupon_frequency_schedule` (HS7), `face_value_date` (HS11); Issuer `sector_source`, `okved`, `okved_version` (HS3), `group_id`, `group_name`, `group_relation`, `is_state_owned` (HS4); dataset `ratings` (HS5) |
| 1.2 (WEB-15, hub review for the desk monitor §6.1, H20) | BondQuote (EOD) `source_analytics`: `duration_waprice_days`, `yield_last_coupon_pct`, `icpi`, `ir`, `bei`, `cbr` (MOEX yields fields verbatim); Fixing `hub_ingested_at`; RUSFAR units: only the explicit RUSFAR rate lists are `unit = pct`, `RUSFARIND` (accumulated return index) is `unit = index`, any other `RUSFAR*` index is excluded with reason `rusfar_series_unit_unknown` (1.0/1.1 published every `RUSFAR%` index as a rate, so RUSFARIND appeared as ~2,500 % p.a.). Exclusion reason `rusfar_series_unit_unknown` |
| 1.3 (WEB-16, review P1-2 / P2-1) | CrossRate: `kind` adds `cbr_cross`, `moex_indicative_cross`, `moex_cets`, `fred_h10` (independent cross-rate series besides the ECB reference rates, which do not reach the hub VPS); `base_ccy` any currency (was `EUR` only); optional `source`, `labels`. Data only, no schema change: fixings `ESTR`, `SOFR`, `SOFR30DAVG`, `SOFR90DAVG`, `SOFR180DAVG`, `SOFRINDEX` (`unit = index`), `CPI_RU_YOY` (`unit = pct`); curve quote sets `usd_ust_par`, `eur_aaa_par`, `eur_bund_par` labelled `proxy_ust_par` / `proxy_ecb_aaa_par` / `proxy_bund_par` - government par yields, **not OIS** |
| 1.3, static part (WEB-18, review P1-1, P1-6, P1-9, P1-10; TDM brief HS3, HS5, HS8-HS10) | Rating `grade`, `rating_type`, `press_release`, `mapping_basis`; ratings of `scope = issuer` (agency issuer ratings mapped by INN; RA Expert regions via their rated bonds) and agency sources `nkr`, `nra`, `raexpert` next to `cbonds`; Instrument `replacement_bond` + `replacement_basis` (`document` / `name_rule`), `issue_size_moex` + `issue_size_moex_field`; FloaterFormula `evidence_document_url`, `evidence_page`; Offer `evidence_document_url`; Issuer `sector_source` value `agency`. Coverage: ratings `by_source`, `by_scope`, `withdrawn`, `active_non_structured_bonds*`; floater formulas `key_rate_ruonia_floaters*`, `active_perpetuals*`. `active_rub_corporate_issuers_rated` now counts current ratings only (a withdrawn one is not "rated") |
| 1.4, static part (KBD floaters) | FloaterFormula `benchmark_tenor_years`, `fixing_anchor`, `fixing_offset`, `fixing_offset_unit`, `rate_rounding_decimals`, `fixing_basis`, `spread_schedule` (SpreadStep `effective_from`, `spread_bp`, `basis`, `evidence_document_url`), `reproduction` (ZcycReproduction): the terms of floaters on the MOEX KBD (benchmark `ZCYC`) and the hub's reproduction of their published coupon rates; null for other benchmarks; `spread_bp` stays the current margin. Coverage of floater formulas: `zcyc_floaters`, `zcyc_floaters_with_tenor`, `zcyc_floaters_curated`, `zcyc_floaters_reproduced`, `zcyc_coupon_periods_checked` / `_matched` |
| 1.5 (package P6, TDM brief H10-H13; FX forwards H1-H4) | FxRate `settle_date` (MOEX SETTLEDATE of the trade date's session, `md.fx_settle_date`) and `settlement_calendars`; dataset `fx_swaps` (FxSwap: MOEX X/RUB swaps with the swap price as published, `near_settle_date` / `far_settle_date`, `settlement_calendars`; 1.0-1.3 excluded them with reason `fx_swap`, now unused); datasets `futures_prices` (FuturesPrice: FORTS EOD settlement price, OHLC, open interest and contract static) and `futures_prices_intraday` (FuturesPriceIntraday: latest FORTS FX futures quote); dataset `cpi` (Fixing objects: Rosstat `CPI_RU_MOM` and CBR `CPI_RU_YOY` from `parameters.cpi_from`, label `published_on_unknown`); `fixings` coverage `ruonia_unpublished_days`; calendars `CN_INTERBANK`, `US_FED`, `CH_ZURICH` (`calendar_code`); exclusion reasons `non_rub_pair` / `precious_metal` also for `fx_swaps`; labels `settle_date_not_observed`, `foreign_calendar_unknown`, `near_settle_date_not_observed`, `far_settle_date_not_observed`, `far_settle_date_not_published`, `contract_static_unknown`, `perpetual`, `no_settle_price`, `published_on_unknown`. Data only: fixings `SARON` (source `snb`, `unit = pct`) |
| 1.6 (TDM brief H17, market-data-hub#38) | Calendars `MOEX_SPFI`, `CBR_RUONIA`, `MOEX_RUSFAR` (`calendar_code`): the calendars the MOEX RUB swap specifications name besides MOSCOW (= `RU_PRODUCTION`; MOEX OIS RUONIA: joint SPFI + MOSCOW + RUONIA), derived by documented rules with curated exceptions; Calendar `provisional_from` also counts the derived calendars' provisional days (sources `rule_*_unverified`) |

## Identity

* The **bundle** is one JSON document: the envelope fields plus `data`.
* Canonical form: keys sorted, no whitespace (`,` `:`), UTF-8 without escaping non-ASCII,
  decimals as strings, dates `YYYY-MM-DD`, instants UTC `...Z`.
* `snapshot_id = sha256(canonical bundle)`. The bundle endpoint serves the deterministic gzip
  of exactly those bytes (`ETag: "<snapshot_id>"`). A consumer verifies
  `sha256(gunzip(body)) == snapshot_id`; replicas can serve the bytes unchanged.
* Snapshots are immutable. A correction is a new snapshot of the same cut
  (`kind`, `cut_type`, `business_date`, `cut_ts`) whose `supersedes` names the earlier one.
  For `daily`, `eod_preliminary` and `eod_final`, `cut_ts` depends only on the business date
  (the configured cut time; `eod_final`: on the next business day), so at most one snapshot
  per such cut and business date is not superseded; `intraday` and `change` can have several.
* **Static of a past business date** (1.6, market-data-hub#102). A market cut of a past date
  built later (a correction of an old `eod_final`, the first publication of a missing day)
  binds a static snapshot with `business_date` equal to the market cut's and
  `parameters.static_basis = terms_current_values_as_of_business_date`: contract terms
  (floater formulas, `spread_schedule`, conventions, schedule dates, fixed coupon rates) as
  known at `known_at`; no bond placed after the business date (unless quoted on it); status,
  face (from the redemption schedule; not for indexed face), coupon rate and amount of
  floating / `variable_known` periods starting after the date (null, status `unknown`) and
  offer status as of the business date; ratings dated after it left out. When an existing
  static already has exactly that data it is reused as is (static snapshots are shared by
  content). A static without `static_basis` was built for the day it was built. A consumer that values an old date on the
  linked static of its current (not superseded) market cut gets the reissued static
  automatically.

## Envelope

| Field | Meaning |
|---|---|
| `kind` | `static` or `market` |
| `cut_type` | market: `eod_preliminary`, `eod_final`, `intraday`; static: `daily` (scheduled) or `change` (static data changed since the last static snapshot) |
| `business_date` | Moscow business date |
| `cut_ts` | Nominal UTC instant of the cut |
| `known_at` | UTC instant: no record the hub learned after it is included (as-known-at reads) |
| `static_snapshot_id` | market only: the static snapshot it is consistent with; quotes exist only for its instruments |
| `supersedes` | earlier snapshot of the same cut that this one corrects |
| `producer` | `market-data-hub` and its version |
| `parameters` | builder parameters (universe rule, lookback windows) |
| `calendar` | one calendar `version` (latest calendar change time) and `fingerprint` per snapshot; a market snapshot repeats its static snapshot's |
| `sources[]` | per dataset and hub source: `ops.job_run` ids of the included rows, `ingested_from`/`ingested_to` (hub ingest-time range; deterministic under raw-capture retention) |
| `datasets[]` | `name`, `rows`, `sha256` (of the canonical `data.<name>` array), `coverage` (dataset-specific facts), `stale` + `stale_reasons` |
| `excluded[]` | records left out with `dataset`, `key`, `reason` |

**A record is never dropped silently**: it is in `data` (possibly with `labels`) or in
`excluded`. Exclusion reasons used by v1: `not_in_static_snapshot` (a quote of a SECID outside
the static universe), `fx_swap` (1.0-1.3 only: 1.5 publishes swaps in `fx_swaps`),
`not_an_outright`, `non_rub_pair`, `precious_metal`, `instrument_unknown` (MOEX FX rows that are
not RUB per unit of a currency; 1.5: `non_rub_pair` / `precious_metal` also for `fx_swaps`);
1.2: `rusfar_series_unit_unknown` (an `md.index_eod` RUSFAR* series that is neither in the
rate lists nor the index list, so its unit is not known).

## Units and values

* Decimals are strings with the exact stored digits (`"14.500"` and `"14.5"` are different
  facts). Never parse them as binary floats.
* Units in names: `_pct` percent (14.5 = 14.5 %), `_bp` basis points, `_per_bond` currency
  amount per bond (currency in `face_unit` or `currency_id` as documented per field),
  `_rub_per_unit` RUB per 1 unit of the foreign currency.
* Bond prices are % of face, as MOEX publishes them; every price basis is a separate field.
  MOEX analytics (yields, durations, spreads) sit under `source_analytics`.
* Quote-set (`dataset`) and tenor codes are hub facts. Conventions (day count, frequency,
  tenor -> date) belong to the consumer's curve-spec registry.
* Missing is `null` or no row, never zero; nothing is forward-filled. Dated datasets carry each
  row's own date inside an explicit window (`coverage.window_*`).

## Static objects (`data`)

| Array | Object | Notes |
|---|---|---|
| `instruments` | Instrument | `ref.bond` + boards. Universe: `active`, plus `matured` with maturity within `parameters.matured_lookback_days`, plus alive bonds MOEX no longer trades (`delisted` with `maturity_date >= business_date`, or perpetual; label `not_traded`; `parameters.universe = active_or_recently_matured_or_alive`); linked EOD static also retains known instruments with price/yield on the market date, even if now matured/delisted. All reads respect `known_at`; stored terms/status are not rewritten. `labels`: `retained_for_market_quote` (outside default universe), `not_traded` (alive but delisted: no MOEX board lists it), `coupon_type_unknown`, `perpetual`, `perpetual_no_call_date`, `day_count_not_published`, `floater_formula_missing`, `floater_formula_partial`, `coupon_schedule_empty`, `no_issuer`, `structured`, `matured`, `face_value_not_in_schedule` (initial face minus the scheduled redemptions never equals `face_value`; not for indexed face), `future_redemptions_differ_from_face` (redemptions still to be paid do not add up to `face_value`) — MOEX values are published unchanged |
| | | 1.1: `lat_name` (MOEX LATNAME); `coupon_frequency_schedule` = `round(365.25 / median period)` of the published coupon schedule (null with < 2 dated periods) next to `coupon_frequency` as MOEX states it (MOEX omits it when the first period is irregular) |
| | | 1.3: `replacement_bond` / `replacement_basis` (P1-6): `document` (an issue-document entry of `mdhub/data/bond_terms_curated.json`, true or false) or `name_rule` (MOEX NAME carries a `ЗО` / `БЗО` / `СЗО` / `СУБ-ЗО` token, or a sovereign `ОВОЗ`; true only, otherwise null = not established); `issue_size_moex` / `issue_size_moex_field` (P1-10): the number of bonds MOEX publishes (`ISSUESIZE`, else `ISSUESIZEPLACED`) - **not** the amount outstanding (buybacks, offer redemptions, partial placements are not deducted) |
| `coupon_periods` | CouponPeriod | `status`: only `known` is a fact; `current_published`, `projected_by_moex`, `unknown` are MOEX projections / gaps |
| `redemptions` | Redemption | `kind` amortization / maturity |
| `offers` | Offer | every source; `labels`: `date_missing`. 1.3: source `curated` (calls of perpetuals, dated offers read from issue documents, `type_basis = document`) with `evidence_document_url` |
| `floater_formulas` | FloaterFormula | resolved formula; `benchmark` and `benchmark_raw` verbatim, `status` curated / inferred / partial. 1.3: a formula read from an issue document (`mdhub/data/bond_terms_curated.json`, job `bond_terms_curated`) is `curated` with `evidence_document_url` / `evidence_page`; coverage `key_rate_ruonia_floaters` vs `key_rate_ruonia_floaters_curated`, `active_perpetuals` vs `active_perpetuals_with_scheduled_call`. 1.4, benchmark `ZCYC` (MOEX `ZR_YLD_CRV`): `rate % = round(Y_KBD(T; d_fix), rate_rounding_decimals) + margin_bp / 100`; `Y_KBD(T; d)` = MOEX effective annual zero yield at the **fixed** tenor `T = benchmark_tenor_years` from the end-of-day KBD parameters of day `d` (`zcyc_params`, `/v1/history/zcyc`; `(exp(G(T)/1e4) - 1) * 100`); `d_fix` = the `fixing_offset`-th day of `fixing_offset_unit` before `fixing_anchor` (`accrual_start` = coupon period start; `0` = the anchor itself or the last such day before it); the coupon is fixed in advance, amount = face x rate x days / 365. `zcyc_publication_days` = the days of `/v1/history/zcyc` rows with `is_eod = true` (rule `eod` also returns a day without an end-of-day publication, labelled `not_end_of_day_publication`: not a fixing day); since 2018 they are the `MOEX_STOCK` business days of `calendars` (one gap: 2022-01-07), which serves for future fixing days. Margin of a period: the last `spread_schedule` step with `effective_from <= period start` (a reset at an offer is a step). `fixing_basis`: `document` (curated from an issue document) or `coupon_history_fit` (job `zcyc_floater_fit` fitted `T` and `N` over 0.25..15 years x 0..15 days to the published rates; published only when `reproduction.accepted`); `reproduction` is always the hub's own check |
| `issuers` | Issuer | `short_name`, `ogrn`, `country` (ISO 3166-1 alpha-2) from the MOEX emitter card; `sector` = a code of the desk taxonomy in `mdhub/data/sector_map.json` (`banks`, `leasing`, `oil_gas`, `metals_mining`, `utilities`, `telecom`, `real_estate`, `transport`, `retail`, `agro`, `chemicals`, `it`, `financial_spv`, `securitization_spv`, `sovereign`, `subfederal`, `municipal`, `supranational`, `other`) or `null` when no versioned rule assigns one (never guessed). 1.1: `sector_source` (`okved` / `curated` / `group` / `rule`; 1.3: `agency` = industry published by a rating agency), `okved` + `okved_version` (null: OKVED not loaded, FNS registers unreachable), `group_id`, `group_name`, `group_relation` (`parent`, `subsidiary`, `spv`, `securitization_spv`, `affiliate`), `is_state_owned` (only where an evidence document states it) from the curated `mdhub/data/issuer_groups_curated.json` (evidence per row). `coverage`: `with_group`, `large_issuers_without_group` (active RUB bonds outstanding ≥ 5 bn RUB), fill rates over all issuers and over issuers of active non-structured bonds, `by_sector`, `by_sector_source`, `without_sector` |
| `ratings` | Rating | 1.1 (HS5): issue ratings from `ref.bond_rating` as known at `known_at`; 1.3 (P1-1): also issuer ratings (`scope = issuer`, `secid = null`) from `ref.issuer_rating`. Sources: Cbonds (`cbonds`; blocked by HTTP 401 for most of 2026) and the agencies' public disclosure (job `ratings_agencies`): НКР `nkr` (list exports), НРА `nra` (action export), Эксперт РА `raexpert` (press releases and rating pages); АКРА not yet (TLS chain of its site). `agency`, `scale` (`national_ru` for agency rows), `rating`, `outlook`, `watch` verbatim; `grade` = letter grade by the documented rule (`AA-.ru`, `ruAA-`, `AA–|ru|` -> `AA-`; null when not a grade); `action` only as published (NRA statuses, withdrawals; never derived); a withdrawn rating has `rating = null`, `action = withdrawn`. `mapping_basis`: `isin` / `inn` / `region_bond_isin`. Nothing is inferred; `coverage` shows the gaps: `active_rub_corporate_issuers_rated`, 1.3 `active_non_structured_bonds`, `..._issue_rated`, `..._issuer_rated`, `..._rated`, `by_source`, `by_scope`, `withdrawn`. Agency data is for internal use: the agencies' terms forbid redistribution |
| `calendars` | Calendar | from `ref.calendar_effective` only (MOEX weekend sessions are **not** business days): `valid_from`..`valid_to` (outside: unknown), `holidays`, `working_weekends`, `provisional_from` (first day decided by an unverified curated row), `version`, `fingerprint`. 1.5: the foreign settlement calendars of FX value dates `CN_INTERBANK` (China interbank market: State Council holiday notices with the adjusted working weekends; decreed years only, so `valid_to` is the last decreed year), `US_FED` (Federal Reserve Banks holidays: USD) and `CH_ZURICH` (Zurich bank holidays: CHF, SARON) - computed rules from 2002. 1.6 (H17): the calendars of RUB swap and fixing conventions, derived from the effective days of their base calendars by documented rules (`sources` `rule_moex_spfi` / `rule_cbr_ruonia` / `rule_moex_rusfar`; `rule_*_unverified` where an input day is provisional, which sets `provisional_from`) plus curated exceptions with evidence (`curated`, file `mdhub/data/calendar_derived.json`), from 2019 (the start of their base calendars): `MOEX_SPFI` = trading days of the MOEX standardised derivatives (SPFI) market = the `MOEX_FORTS` business days (MOEX sets them together in every holiday schedule; on a holiday trading day RUB obligations are not settled, so a RUB swap joins `RU_PRODUCTION`); `CBR_RUONIA` = days for which the Bank of Russia calculates and publishes RUONIA (methodology cl. 5.1): `RU_PRODUCTION` business days (working Saturdays included) except the last business day of every year (2019-2025 never published; later years provisional until the Bank of Russia decides) and the 2020 presidential non-working days 30.03-03.04 and 24.06; `MOEX_RUSFAR` = calculation days of the RUB overnight RUSFAR (MOEX methodology, Non-calculation days): `RU_PRODUCTION` and `MOEX_STOCK` business days, Monday to Friday (no trading Saturdays or trading non-working days), except the last MOEX trading day of the year (from 2021), with curated 2019 and 2022 exceptions (not `RUSFARCNY`). A RUONIA / RUSFAR value on a non-calculation day is a calendar error the hub reports (calendar refresh `partial`) |
| `id_map` | IdMap | `isin_shared_with`: other SECIDs with the same ISIN (an ISIN join is ambiguous when non-empty) |

### Face value on payment days (1.1, HS11)

`Instrument.face_value` is the face MOEX published when the static was built. MOEX deducts a
redemption from FACEVALUE during the redemption day, so on a payment day the value depends on
the build time. `face_value_date` makes it explicit: the date from which `face_value` is
outstanding according to `redemptions` (the date of the latest redemption already deducted
from `initial_face_value`; `issue_date` when none is; `null` when no step of the schedule
equals `face_value`, see label `face_value_not_in_schedule`).

Rule for consumers: the face outstanding **before** the redemptions of day `D` is
`initial_face_value − Σ redemptions.value_per_bond with date < D`. On `D = business_date`,
`face_value_date = business_date` means `face_value` is already **after** that day's
redemption; an earlier `face_value_date` means it is still **before** it. The golden static
fixture carries a payment-day amortizer (see `fixtures/index.json`).

## Market objects (`data`)

| Array | Object | Notes |
|---|---|---|
| `bond_quotes` | BondQuote (EOD) | `md.bond_eod` + `md.bond_eod_yield` of the business date; empty in intraday cuts. Coverage `basis`: `moex_history`, or in `eod_preliminary` before ISS publishes the history of D (overnight) `intraday_close` - the latest intraday marketdata of D in the same shape (`legal_close_pct` = LCLOSEPRICE, `close_pct` = LAST, `market_price3_pct` = MARKETPRICETODAY; accrued, face, `market_price2_pct` null), dataset stale (hub 0.3.1, no schema change). 1.2: `source_analytics.duration_waprice_days` (MOEX DURATIONWAPRICE), `yield_last_coupon_pct` (yields endpoint, else the history row), `icpi`, `ir`, `bei`, `cbr` (MOEX yields fields verbatim, not interpreted by the hub) |
| `bond_quotes_intraday` | BondQuote (intraday) | latest stored quote per (secid, board) on the business date; empty in EOD cuts |
| `curve_quote_sets` | CurveQuoteSet | per (source, dataset) fetched on the business date: intraday cuts the latest snapshot; EOD cuts (hub 0.3.1) the latest one with bid/ask whose source clock is on the date at or before the cutoff (`coverage.eod_cutoff_msk`, default 19:00), else the latest one; `labels`: `mid_only`, `market_closed`, `source_clock_not_on_business_date`, and (1.3 data) `proxy_ust_par` / `proxy_ecb_aaa_par` / `proxy_bund_par` for the government par-yield proxies `usd_ust_par` / `eur_aaa_par` / `eur_bund_par` (not OIS; one end-of-day set per curve date). `coverage.required_without_any_source` lists `ruonia_ois` / `key_rate_swap` when no source delivered it on the date (the dataset is then `stale`) |
| `zcyc_params` | ZcycParams | EOD cuts: the end-of-day publication (else the latest, flagged stale); intraday: the latest |
| `fixings` | Fixing | `md.fixing` and the RUSFAR family from `md.index_eod` (`origin`). 1.5: coverage `ruonia_unpublished_days` = the `RU_PRODUCTION` business days between the first and the latest CBR RUONIA fixing of the window without a fixing (CBR did not publish them, e.g. the last business day of a year; core's rate futures need them declared); `SARON` (source `snb`, % p.a., Zurich business days) is a 1.5 data addition. 1.2: RUSFAR rates (`RUSFAR`, `RUSFAR1W`, `RUSFAR2W`, `RUSFAR1M`, `RUSFAR3M`, `RUSFARCNY`, `RUSFARCN1W` and the 18:00 / compound variants `RUSFARRT`, `RUSFAR1WRT`, `RUSFAR2WRT`, `RUSFAR1MRT`, `RUSFAR3MRT`, `RUSFARCNRT`, `RUSFARC1WR`, `RUSFARN`, `RUSFAR1WN`, `RUSFAR2WN`, `RUSFAR1MN`, `RUSFAR3MN`, `RUSFARCNN`, `RUSFARC1WN`) `unit = pct`, `RUSFARIND` `unit = index`, others excluded; `hub_ingested_at` = when the hub first stored the published value (`md.fixing.ingested_at`; `md.index_eod`: `updated_at`, else `ingested_at`), UTC — not a publication time: `published_at` stays `null` unless the source states one |
| `fx_rates` | FxRate | RUB per 1 unit: `cbr_official`, `moex_eod` (raw price, `quote_units`, exact per-unit value), `moex_indicative_14` / `_19`. 1.5: `moex_eod` `settle_date` = the instrument's value date for trades of `trade_date` (MOEX SETTLEDATE of that session; null + label `settle_date_not_observed` when the hub did not read that session's listing - never computed) and `settlement_calendars` = the hub calendars whose joint business days the pair settles on (`MOEX_FX` + `CN_INTERBANK` / `US_FED` / `TARGET` / `CH_ZURICH`; other currencies `[MOEX_FX]` + label `foreign_calendar_unknown`); coverage `moex_eod_with_settle_date` / `_without_` |
| `fx_swaps` | FxSwap | 1.5 (H13), EOD cuts: MOEX X/RUB swaps of the business date on every selt board (CETS, CNGD, LICU...): `close` / `waprice` = the swap price as published = far-leg rate minus near-leg rate in RUB per `quote_units` (swap points; `*_rub_per_unit` per 1 unit), `near_leg` / `far_leg` codes, `near_settle_date` (SETTLEDATE of the swap = its near leg), `far_settle_date` (a named far leg TOD/TOM/SPT...: the SETTLEDATE of the same pair's outright of that code; a tenor far leg `1W`, `1M`...: null + label `far_settle_date_not_published` - MOEX publishes no far date; resolve the tenor from the near date on `settlement_calendars`). Rows without trades carry `no_price`. Non-RUB and precious-metal swaps are excluded |
| `futures_prices` | FuturesPrice | 1.5 (H11), EOD cuts: every outright FORTS future of `md.futures_eod` on the business date (FX, rate, index, commodity, equity) with `settle_price` (SETTLEPRICE, the price variation margin is computed on), OHLC, WAP, volume, open interest, `swap_rate` (perpetuals) and the contract static (`asset_code`, `sec_type`, `contract_type`, `is_fx`, `base_ccy`/`quote_ccy`, `last_trade_date`, `last_del_date`, `lot_volume`, `min_step`, `step_price`); `settle_rub_per_unit` / `close_rub_per_unit` only for RUB-quoted FX futures. Labels `perpetual`, `no_settle_price`, `contract_static_unknown` |
| `futures_prices_intraday` | FuturesPriceIntraday | 1.5 (H11), intraday cuts: the latest stored FX futures quote per contract on the business date (APIM realtime, else public ISS delayed: `source`, `source_variant`, `latency`), `settle_price` = MOEX's current estimate, plus the same contract static |
| `cpi` | Fixing | 1.5 (H10): monthly CPI from `parameters.cpi_from` (default 2015-01: covers every OFZ-IN's base month) to the business date, whatever the fixing window: `CPI_RU_MOM` (Rosstat, index to the previous month in %, `unit = index`) and `CPI_RU_YOY` (CBR, y/y, `unit = pct`), `fixing_date` = first day of the month. `published_on` is the release date where the source stated it (Rosstat: the newest month of each workbook the hub loaded), else null with label `published_on_unknown`. No index levels: chain the m/m values |
| `cross_rates` | CrossRate | units of `ccy` per 1 `base_ccy`, `rate_date` in [D-7, D]. `kind` `ecb` (ECB reference rates, base EUR); 1.3: `cbr_cross` (two CBR official RUB rates in force on the effective date, divided), `moex_indicative_cross` (two MOEX 19:00 indicative RUB rates), `moex_cets` (traded CETS cross, close per unit), `fred_h10` (Fed H.10 via FRED) - separate series, never merged; derived kinds have 10 significant digits and label `derived_cross`; `source` = hub source code; `coverage.latest_by_kind` |
| `cbr_meetings` | CbrMeeting | |

## Fixtures

`fixtures/static_daily.json` and `fixtures/market_eod_final.json` are built by the hub's
builders from public data of 2026-10-15 for ten bonds: OFZ-PD bullet, OFZ-PK floater (RUONIA),
OFZ-IN linker, OFZ-AD amortizer, a corporate key-rate floater, a corporate with a scheduled put
offer, a corporate amortizer, two perpetuals and a bond of unknown coupon type (`index.json`).
They are pretty-printed; their id is the sha256 of their canonical form. The market fixture uses
a 14-day fixing window (see its `parameters`) to stay reviewable. 1.5 rows: five FORTS
contracts of the hub's recorded ISS fixtures (Si, CNY, ED, RGBI, USDRUBF), CPI 2015-01..2026-09
from the recorded Rosstat workbook and CBR page, the rule calendars `US_FED` / `CH_ZURICH` and the
curated 2026 `CN_INTERBANK`; 1.6: the derived `MOEX_SPFI`, `CBR_RUONIA`, `MOEX_RUSFAR` of the calendar window
2026-2027; the 2026-10-15 value dates are **constructed** for the CNY/RUB CETS
instruments only (TOD 15.10, TOM 16.10, SPT 19.10; the seed predates the SETTLEDATE history), so
every other MOEX FX row shows the `*_not_observed` labels (`tests/snapshot_golden.py`).

Hub CI (`tests/test_snapshot_golden.py`) validates them and rebuilds them byte for byte from
`tests/fixtures/snapshot/source_rows.json.gz`. They change only in a reviewed change
(`MDHUB_REGEN_GOLDEN=1 uv run pytest tests/test_snapshot_golden.py`); a contract change bumps
`schema_version`.

## Validating

```python
import json, pathlib
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

root = pathlib.Path("schemas/market_snapshot/v1")
docs = [json.loads(p.read_text()) for p in root.glob("*.schema.json")]
registry = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)
schema = registry.contents(
    "https://schemas.market-data-hub.invalid/market_snapshot/v1/snapshot.schema.json"
)
Draft202012Validator(schema, registry=registry).validate(bundle)
```
