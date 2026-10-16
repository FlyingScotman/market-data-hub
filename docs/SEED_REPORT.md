# market-data-hub seed report

- generated at: 2026-10-16 16:02:09 UTC
- as of (business date, MSK): 2026-10-16
- database: `mdhub` (3.2 GB)
- server: PostgreSQL 16.2 (`PostgreSQL 16.2 on aarch64-apple-darwin23.5.0, compiled by Apple clang version 15.0.0 (clang-1500.0.40.1), 64-bit`)
- migrations applied: 14 (latest `0424_curve_history`)
- read-only: one `REPEATABLE READ READ ONLY` transaction; exact `count(*)` row counts

## Summary

- datasets: 34; stale (last date < expected latest): 0
- datasets with missing days: bonds_eod_yield (1), cbr_standing_rates (1), futures_intraday (1), fx_intraday (1), zcyc (1)
- series with missing days: cbr_standing_rates:CBR_DEPOSIT_STANDING (1), cbr_standing_rates:CBR_LENDING_STANDING (1), indices_bonds:RGBI (1), indices_bonds:RGBITR (1), indices_bonds:RUCBITR1Y (6), indices_bonds:RUCBICP1Y (6), indices_bonds:RUCBITRL1 (6), indices_bonds:RUCBITRL2 (6), indices_bonds:RUCBITRL3 (6), indices_bonds:RUGBITR1Y (4), indices_bonds:RUGBITR3Y (4), indices_bonds:RUGBITR5Y (4), indices_bonds:RUGBITR10Y (4), indices_bonds:RUGBINFTR (4), indices_bonds:IMOEX (3), indices_rusfar:RUSFAR (35), indices_rusfar:RUSFAR1W (35), indices_rusfar:RUSFAR2W (36), indices_rusfar:RUSFAR1M (36), indices_rusfar:RUSFAR3M (36), indices_rusfar:RUSFARCNY (103), indices_rusfar:RUSFARCN1W (74), indices_rusfarrt:RUSFARRT (35), indices_rusfarrt:RUSFAR1WRT (35), indices_rusfarrt:RUSFAR2WRT (36), indices_rusfarrt:RUSFAR1MRT (36), indices_rusfarrt:RUSFAR3MRT (36), indices_rusfarrt:RUSFARCNRT (33), indices_rusfarrt:RUSFARC1WR (32), indices_rusfarrt:RUSFARN (9), indices_rusfarrt:RUSFAR1WN (9), indices_rusfarrt:RUSFAR2WN (9), indices_rusfarrt:RUSFAR1MN (9), indices_rusfarrt:RUSFAR3MN (9), indices_rusfarrt:RUSFARCNN (30), indices_rusfarrt:RUSFARC1WN (30)
- days below min_rows_per_day: indices_all (6)
- dataset problems: none
- open gaps (ops.data_gap): 0 (other gap rows: backfilled 106, unfillable 762, not_expected 574)
- jobs whose last run failed/partial: none

## Tables

| schema | tables | rows | size |
| --- | --: | --: | --: |
| md | 21 | 5439815 | 1.6 GB |
| ref | 15 | 385021 | 136.6 MB |
| legacy_md | 40 | 764367 | 301.1 MB |
| legacy_bc | 19 | 831248 | 203.0 MB |
| ops | 10 | 126322 | 940.9 MB |

### md

| table | rows | size |
| --- | --: | --: |
| algopack_fo_tradestats | 200194 | 113.9 MB |
| algopack_futoi | 270106 | 165.3 MB |
| bond_eod | 1976163 | 688.8 MB |
| bond_eod_yield | 1190949 | 320.6 MB |
| bond_quote_intraday | 0 | 32.0 kB |
| bond_quote_last | 0 | 24.0 kB |
| cbr_liquidity | 1980 | 480.0 kB |
| curve_quote | 47271 | 4.6 MB |
| curve_snapshot | 3588 | 2.7 MB |
| ecb_fx | 93433 | 13.4 MB |
| fixing | 37981 | 7.4 MB |
| futures_eod | 302125 | 76.8 MB |
| futures_intraday | 78936 | 15.6 MB |
| fx_eod | 517519 | 104.1 MB |
| fx_fixing | 1392 | 256.0 kB |
| fx_indicative | 14361 | 2.0 MB |
| fx_intraday | 23252 | 6.0 MB |
| fx_official | 110766 | 18.2 MB |
| index_eod | 567443 | 142.5 MB |
| zcyc_reject | 0 | 16.0 kB |
| zcyc_snapshot | 2356 | 2.9 MB |

### ref

| table | rows | size |
| --- | --: | --: |
| bond | 16055 | 17.2 MB |
| bond_amortization | 46384 | 9.2 MB |
| bond_board | 22081 | 11.4 MB |
| bond_coupon | 204543 | 48.6 MB |
| bond_offer | 6521 | 1.8 MB |
| bond_rate_formula | 709 | 352.0 kB |
| bond_rating | 0 | 24.0 kB |
| calendar_day | 35834 | 10.4 MB |
| cbr_meeting | 76 | 88.0 kB |
| futures_contract | 2218 | 568.0 kB |
| fx_instrument | 744 | 432.0 kB |
| issuer | 1673 | 1016.0 kB |
| static_conflict | 0 | 24.0 kB |
| static_snapshot | 48015 | 35.4 MB |
| weekend_session | 168 | 112.0 kB |

### legacy_md

| table | rows | size |
| --- | --: | --: |
| _sqlite_columns | 347 | 176.0 kB |
| _sqlite_constraints | 87 | 48.0 kB |
| _sqlite_schema | 131 | 96.0 kB |
| backfill_requests | 0 | 32.0 kB |
| cbr_liquidity_balance_revisions | 326 | 272.0 kB |
| cbr_liquidity_balances | 311 | 104.0 kB |
| cbr_liquidity_capture_observations | 12812 | 1.3 MB |
| cbr_liquidity_captures | 49 | 56.0 kB |
| curve_quotes | 61666 | 11.6 MB |
| curve_snapshots | 4225 | 1.9 MB |
| curve_source_policies | 0 | 24.0 kB |
| curve_world_definitions | 11 | 32.0 kB |
| curve_world_instances | 0 | 32.0 kB |
| datasets | 9 | 48.0 kB |
| derived_curve_builds | 4514 | 2.2 MB |
| derived_curve_dependencies | 1795 | 240.0 kB |
| derived_curve_nodes | 44473 | 7.8 MB |
| derived_projection_nodes | 31738 | 6.2 MB |
| derived_projection_scenario_nodes | 68751 | 16.5 MB |
| gcurve_snapshots | 1167 | 1.3 MB |
| gcurve_zero_curve_builds | 1167 | 736.0 kB |
| gcurve_zero_curve_nodes | 12837 | 2.2 MB |
| index_fixings | 2607 | 400.0 kB |
| ingestion_runs | 2871 | 776.0 kB |
| instrument_identifiers | 0 | 32.0 kB |
| instruments | 0 | 24.0 kB |
| intraday_quote_captures | 63715 | 20.9 MB |
| intraday_quote_observations | 434995 | 218.1 MB |
| normalized_base_curve_bundles | 107 | 64.0 kB |
| normalized_base_curve_nodes | 4676 | 1.8 MB |
| normalized_base_curve_snapshots | 319 | 360.0 kB |
| policy_meetings | 64 | 32.0 kB |
| price_observations | 0 | 32.0 kB |
| projection_curve_build_meta | 1669 | 4.9 MB |
| projection_curve_scenarios | 5007 | 568.0 kB |
| rubik_curve_artifacts | 0 | 32.0 kB |
| rubik_curve_nodes | 0 | 24.0 kB |
| schema_migrations | 17 | 32.0 kB |
| sources | 4 | 48.0 kB |
| term_ruonia_fixings | 1900 | 328.0 kB |

### legacy_bc

| table | rows | size |
| --- | --: | --: |
| _sqlite_columns | 195 | 96.0 kB |
| _sqlite_constraints | 20 | 16.0 kB |
| _sqlite_schema | 36 | 64.0 kB |
| boards | 134427 | 18.6 MB |
| cbonds_cashflow_observations | 4052 | 1.0 MB |
| cbonds_ingestion_runs | 21 | 32.0 kB |
| cbonds_issue_enrichment | 15 | 32.0 kB |
| cbonds_issue_observations | 80 | 40.0 kB |
| cbonds_offer_observations | 26 | 32.0 kB |
| conflicts | 2 | 32.0 kB |
| event_revisions | 265231 | 57.7 MB |
| events | 256559 | 81.1 MB |
| formula_models | 16023 | 6.3 MB |
| issue_revisions | 72753 | 12.5 MB |
| issuers | 1670 | 464.0 kB |
| issues | 16023 | 11.2 MB |
| ratings | 0 | 16.0 kB |
| runs | 23 | 56.0 kB |
| source_availability | 64092 | 13.8 MB |

### ops

| table | rows | size |
| --- | --: | --: |
| backfill_task | 178 | 184.0 kB |
| data_gap | 1442 | 640.0 kB |
| dataset_status | 34 | 64.0 kB |
| job_run | 418 | 280.0 kB |
| legacy_import | 2 | 104.0 kB |
| quality_report | 5 | 96.0 kB |
| raw_capture | 75747 | 897.4 MB |
| row_history | 48471 | 42.1 MB |
| schema_migration | 14 | 32.0 kB |
| source | 11 | 32.0 kB |

## Datasets

Window = `[history_start or first date, expected latest]`; expected = business days of the dataset calendar (`ref.calendar_effective`, weekday fallback), every day for `daily`, month starts for `monthly`, none for `event`. missing = expected day without rows; below min = rows < `min_rows_per_day` (per series for series datasets); off-cal = days with rows that the calendar does not expect.

Calendars (`ref.calendar_effective`):

| calendar | first day | last day | days | business days |
| --- | --- | --- | --: | --: |
| MOEX_FORTS | 2010-01-01 | 2027-12-31 | 6574 | 2699 |
| MOEX_FX | 2010-01-01 | 2027-12-31 | 6574 | 3723 |
| MOEX_STOCK | 2010-01-01 | 2027-12-31 | 6574 | 4528 |
| RU_PRODUCTION | 2010-01-01 | 2027-12-31 | 6574 | 4431 |
| TARGET | 2002-01-01 | 2027-12-31 | 9496 | 6659 |

| dataset | table | freq/calendar | first | last | expected latest | fresh | rows | days | window | expected | missing | below min | off-cal |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --: | --: | --: | --: |
| algopack_fo_tradestats | md.algopack_fo_tradestats | intraday/MOEX_FORTS | 2026-07-19 | 2026-10-16 | 2026-10-15 | ok | 200194 | 84 | 2026-07-19..2026-10-15 | 64 | 0 | 0 (<100) | 19 |
| algopack_futoi | md.algopack_futoi | intraday/MOEX_FORTS | 2026-07-19 | 2026-10-16 | 2026-10-15 | ok | 270106 | 84 | 2026-07-19..2026-10-15 | 64 | 0 | 0 (<100) | 19 |
| bonds_eod | md.bond_eod | business_daily/MOEX_STOCK | 2024-01-03 | 2026-10-15 | 2026-10-15 | ok | 1976163 | 710 | 2024-01-03..2026-10-15 | 710 | 0 | 0 (<1000) | 0 |
| bonds_eod_yield | md.bond_eod_yield | business_daily/MOEX_STOCK | 2024-01-03 | 2026-10-15 | 2026-10-15 | ok | 1190949 | 709 | 2024-01-03..2026-10-15 | 710 | 1 | 0 (<500) | 0 |
| cbr_fx_official | md.fx_official | daily/- | 2010-02-22 | 2026-10-16 | 2026-10-16 | ok | 110766 | 2739 | 2020-01-01..2026-10-16 | 2481 | 0 | 0 (<30) | 0 |
| cbr_key_rate | md.fixing | business_daily/RU_PRODUCTION | 2013-09-17 | 2026-10-16 | 2026-10-15 | ok | 3281 | 3281 | 2013-09-17..2026-10-15 | 3215 | 0 | 0 (<1) | 65 |
| cbr_liquidity | md.cbr_liquidity | business_daily/RU_PRODUCTION | 2014-01-01 | 2026-10-16 | 2026-10-15 | ok | 1980 | 1980 | 2019-01-01..2026-10-15 | 1911 | 0 | 0 (<1) | 59 |
| cbr_meetings | ref.cbr_meeting | event/- | 2019-02-08 | 2027-12-17 | 2025-09-11 | ok | 76 | 76 | - | - | - | - | - |
| cbr_ruonia | md.fixing | business_daily/RU_PRODUCTION | 2010-01-11 | 2026-10-14 | 2026-10-14 | ok | 4130 | 4130 | 2010-01-11..2026-10-14 | 4130 | 0 | 0 (<1) | 0 |
| cbr_ruonia_index | md.fixing | daily/- | 2010-01-11 | 2026-10-15 | 2026-10-14 | ok | 24186 | 6122 | 2010-01-11..2026-10-14 | 6121 | 0 | per series | 0 |
| cbr_standing_rates | md.fixing | business_daily/RU_PRODUCTION | 2014-02-03 | 2026-10-16 | 2026-10-15 | ok | 6384 | 3192 | 2014-02-03..2026-10-15 | 3124 | 1 | per series | 68 |
| ecb_fx | md.ecb_fx | business_daily/TARGET | 2015-01-02 | 2026-10-15 | 2026-10-15 | ok | 93433 | 3018 | 2015-01-01..2026-10-15 | 3018 | 0 | 0 (<20) | 0 |
| futures_eod | md.futures_eod | business_daily/MOEX_FORTS | 2024-01-03 | 2026-10-15 | 2026-10-15 | ok | 302125 | 710 | 2024-01-03..2026-10-15 | 710 | 0 | 0 (<200) | 0 |
| futures_intraday | md.futures_intraday | intraday/MOEX_FORTS | 2026-08-28 | 2026-10-15 | 2026-10-15 | ok | 78936 | 39 | 2026-08-28..2026-10-15 | 35 | 1 | 0 (<1) | 5 |
| fx_eod | md.fx_eod | business_daily/MOEX_FX | 2024-01-03 | 2026-10-15 | 2026-10-15 | ok | 517519 | 710 | 2024-01-03..2026-10-15 | 710 | 0 | 0 (<100) | 0 |
| fx_fixing | md.fx_fixing | event/MOEX_FX | 2024-01-03 | 2026-10-15 | 2026-10-11 | ok | 1392 | 667 | - | - | - | - | - |
| fx_indicative | md.fx_indicative | business_daily/MOEX_FORTS | 2024-01-03 | 2026-10-15 | 2026-10-15 | ok | 14361 | 710 | 2024-01-03..2026-10-15 | 710 | 0 | 0 (<10) | 0 |
| fx_intraday | md.fx_intraday | intraday/MOEX_FX | 2026-08-28 | 2026-10-15 | 2026-10-15 | ok | 23252 | 34 | 2026-08-28..2026-10-15 | 35 | 1 | 0 (<1) | 0 |
| indices_all | md.index_eod | business_daily/MOEX_STOCK | 2022-01-03 | 2026-10-15 | 2026-10-14 | ok | 567443 | 1219 | 2022-01-03..2026-10-14 | 1201 | 0 | 6 (<300) | 17 |
| indices_bonds | md.index_eod | business_daily/MOEX_STOCK | 2022-01-03 | 2026-10-15 | 2026-10-15 | ok | 567443 | 1219 | 2022-01-03..2026-10-15 | 1202 | 0 | per series | 17 |
| indices_rusfar | md.index_eod | business_daily/MOEX_STOCK | 2022-01-03 | 2026-10-15 | 2026-10-15 | ok | 567443 | 1219 | 2022-01-03..2026-10-15 | 1202 | 0 | per series | 17 |
| indices_rusfarrt | md.index_eod | business_daily/MOEX_STOCK | 2022-01-03 | 2026-10-15 | 2026-10-15 | ok | 567443 | 1219 | 2022-01-03..2026-10-15 | 1202 | 0 | per series | 17 |
| otc_monitor_cny_ruonia_xccy | md.curve_snapshot | event/MOEX_STOCK | 2026-09-23 | 2026-10-16 | 2026-10-12 | ok | 54 | 16 | - | - | - | - | - |
| otc_monitor_cny_rusfar_ois | md.curve_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-12 | ok | 1 | 1 | - | - | - | - | - |
| otc_monitor_key_rate_swap | md.curve_snapshot | event/MOEX_STOCK | 2026-09-04 | 2026-10-16 | 2026-10-12 | ok | 63 | 29 | - | - | - | - | - |
| otc_monitor_ruonia_ois | md.curve_snapshot | event/MOEX_STOCK | 2026-09-04 | 2026-10-16 | 2026-10-12 | ok | 66 | 29 | - | - | - | - | - |
| ratescalcs_cny_futures_yields | md.curve_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-12 | ok | 1 | 1 | - | - | - | - | - |
| ratescalcs_cny_ruonia_xccy | md.curve_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-12 | ok | 1 | 1 | - | - | - | - | - |
| ratescalcs_key_rate_swap | md.curve_snapshot | event/MOEX_STOCK | 2026-08-05 | 2026-10-15 | 2026-10-12 | ok | 23 | 21 | - | - | - | - | - |
| ratescalcs_ruonia_ois | md.curve_snapshot | event/MOEX_STOCK | 2026-08-05 | 2026-10-16 | 2026-10-12 | ok | 33 | 28 | - | - | - | - | - |
| ratescalcs_usd_futures_yields | md.curve_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-12 | ok | 1 | 1 | - | - | - | - | - |
| ref_bond_schedules | ref.static_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-14 | ok | 15969 | 1 | - | - | - | - | - |
| ref_bond_static | ref.static_snapshot | event/MOEX_STOCK | 2026-10-16 | 2026-10-16 | 2026-10-14 | ok | 15953 | 1 | - | - | - | - | - |
| zcyc | md.zcyc_snapshot | business_daily/MOEX_STOCK | 2022-01-03 | 2026-10-15 | 2026-10-15 | ok | 1201 | 1201 | 2022-01-03..2026-10-15 | 1202 | 1 | 0 (<1) | 0 |

### Series

Series windows start at the series' own first date.

| dataset | series | first | last | fresh | rows | days | window | expected | missing | below min | off-cal |
| --- | --- | --- | --- | --- | --- | --- | --- | --: | --: | --: | --: |
| cbr_ruonia_index | RUONIA_INDEX | 2010-01-11 | 2026-10-15 | ok | 6122 | 6122 | 2010-01-11..2026-10-14 | 6121 | 0 | 0 (<1) | 0 |
| cbr_ruonia_index | TERM_RUONIA_1M | 2010-02-11 | 2026-10-15 | ok | 6091 | 6091 | 2010-02-11..2026-10-14 | 6090 | 0 | 0 (<1) | 0 |
| cbr_ruonia_index | TERM_RUONIA_3M | 2010-04-11 | 2026-10-15 | ok | 6032 | 6032 | 2010-04-11..2026-10-14 | 6031 | 0 | 0 (<1) | 0 |
| cbr_ruonia_index | TERM_RUONIA_6M | 2010-07-11 | 2026-10-15 | ok | 5941 | 5941 | 2010-07-11..2026-10-14 | 5940 | 0 | 0 (<1) | 0 |
| cbr_standing_rates | CBR_DEPOSIT_STANDING | 2014-02-03 | 2026-10-16 | ok | 3192 | 3192 | 2014-02-03..2026-10-15 | 3124 | 1 | 0 (<1) | 68 |
| cbr_standing_rates | CBR_LENDING_STANDING | 2014-02-03 | 2026-10-16 | ok | 3192 | 3192 | 2014-02-03..2026-10-15 | 3124 | 1 | 0 (<1) | 68 |
| indices_bonds | RGBI | 2022-01-03 | 2026-10-15 | ok | 1201 | 1201 | 2022-01-03..2026-10-15 | 1202 | 1 | 0 (<1) | 0 |
| indices_bonds | RGBITR | 2022-01-03 | 2026-10-15 | ok | 1201 | 1201 | 2022-01-03..2026-10-15 | 1202 | 1 | 0 (<1) | 0 |
| indices_bonds | RUCBITR1Y | 2022-01-03 | 2026-10-15 | ok | 1196 | 1196 | 2022-01-03..2026-10-15 | 1202 | 6 | 0 (<1) | 0 |
| indices_bonds | RUCBICP1Y | 2022-01-03 | 2026-10-15 | ok | 1196 | 1196 | 2022-01-03..2026-10-15 | 1202 | 6 | 0 (<1) | 0 |
| indices_bonds | RUCBITRL1 | 2022-01-03 | 2026-10-15 | ok | 1196 | 1196 | 2022-01-03..2026-10-15 | 1202 | 6 | 0 (<1) | 0 |
| indices_bonds | RUCBITRL2 | 2022-01-03 | 2026-10-15 | ok | 1196 | 1196 | 2022-01-03..2026-10-15 | 1202 | 6 | 0 (<1) | 0 |
| indices_bonds | RUCBITRL3 | 2022-01-03 | 2026-10-15 | ok | 1196 | 1196 | 2022-01-03..2026-10-15 | 1202 | 6 | 0 (<1) | 0 |
| indices_bonds | RUGBITR1Y | 2022-01-03 | 2026-10-15 | ok | 1198 | 1198 | 2022-01-03..2026-10-15 | 1202 | 4 | 0 (<1) | 0 |
| indices_bonds | RUGBITR3Y | 2022-01-03 | 2026-10-15 | ok | 1198 | 1198 | 2022-01-03..2026-10-15 | 1202 | 4 | 0 (<1) | 0 |
| indices_bonds | RUGBITR5Y | 2022-01-03 | 2026-10-15 | ok | 1198 | 1198 | 2022-01-03..2026-10-15 | 1202 | 4 | 0 (<1) | 0 |
| indices_bonds | RUGBITR10Y | 2022-01-03 | 2026-10-15 | ok | 1198 | 1198 | 2022-01-03..2026-10-15 | 1202 | 4 | 0 (<1) | 0 |
| indices_bonds | RUGBINFTR | 2022-01-03 | 2026-10-15 | ok | 1198 | 1198 | 2022-01-03..2026-10-15 | 1202 | 4 | 0 (<1) | 0 |
| indices_bonds | IMOEX | 2022-01-03 | 2026-10-15 | ok | 1199 | 1199 | 2022-01-03..2026-10-15 | 1202 | 3 | 0 (<1) | 0 |
| indices_rusfar | RUSFAR | 2022-01-10 | 2026-10-15 | ok | 1175 | 1175 | 2022-01-10..2026-10-15 | 1197 | 35 | 0 (<1) | 13 |
| indices_rusfar | RUSFAR1W | 2022-01-10 | 2026-10-15 | ok | 1174 | 1174 | 2022-01-10..2026-10-15 | 1197 | 35 | 0 (<1) | 12 |
| indices_rusfar | RUSFAR2W | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfar | RUSFAR1M | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfar | RUSFAR3M | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfar | RUSFARCNY | 2022-09-26 | 2026-10-15 | ok | 930 | 930 | 2022-09-26..2026-10-15 | 1033 | 103 | 0 (<1) | 0 |
| indices_rusfar | RUSFARCN1W | 2023-12-04 | 2026-10-15 | ok | 656 | 656 | 2023-12-04..2026-10-15 | 730 | 74 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFARRT | 2022-01-10 | 2026-10-15 | ok | 1175 | 1175 | 2022-01-10..2026-10-15 | 1197 | 35 | 0 (<1) | 13 |
| indices_rusfarrt | RUSFAR1WRT | 2022-01-10 | 2026-10-15 | ok | 1174 | 1174 | 2022-01-10..2026-10-15 | 1197 | 35 | 0 (<1) | 12 |
| indices_rusfarrt | RUSFAR2WRT | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfarrt | RUSFAR1MRT | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfarrt | RUSFAR3MRT | 2022-01-10 | 2026-10-15 | ok | 1172 | 1172 | 2022-01-10..2026-10-15 | 1197 | 36 | 0 (<1) | 11 |
| indices_rusfarrt | RUSFARCNRT | 2023-07-03 | 2026-10-15 | ok | 807 | 807 | 2023-07-03..2026-10-15 | 840 | 33 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFARC1WR | 2023-12-04 | 2026-10-15 | ok | 698 | 698 | 2023-12-04..2026-10-15 | 730 | 32 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFARN | 2025-06-23 | 2026-10-15 | ok | 328 | 328 | 2025-06-23..2026-10-15 | 337 | 9 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFAR1WN | 2025-06-23 | 2026-10-15 | ok | 328 | 328 | 2025-06-23..2026-10-15 | 337 | 9 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFAR2WN | 2025-06-23 | 2026-10-15 | ok | 328 | 328 | 2025-06-23..2026-10-15 | 337 | 9 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFAR1MN | 2025-06-23 | 2026-10-15 | ok | 328 | 328 | 2025-06-23..2026-10-15 | 337 | 9 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFAR3MN | 2025-06-23 | 2026-10-15 | ok | 328 | 328 | 2025-06-23..2026-10-15 | 337 | 9 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFARCNN | 2025-06-23 | 2026-10-15 | ok | 307 | 307 | 2025-06-23..2026-10-15 | 337 | 30 | 0 (<1) | 0 |
| indices_rusfarrt | RUSFARC1WN | 2025-06-23 | 2026-10-15 | ok | 307 | 307 | 2025-06-23..2026-10-15 | 337 | 30 | 0 (<1) | 0 |

### Missing and thin days (first 10)

- `bonds_eod_yield` missing 1: 2024-02-08
- `cbr_standing_rates` missing 1: 2018-04-25
- `cbr_standing_rates / CBR_DEPOSIT_STANDING` missing 1: 2018-04-25
- `cbr_standing_rates / CBR_LENDING_STANDING` missing 1: 2018-04-25
- `futures_intraday` missing 1: 2026-09-28
- `fx_intraday` missing 1: 2026-09-28
- `indices_all` below 300 rows 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RGBI` missing 1: 2022-01-07
- `indices_bonds / RGBITR` missing 1: 2022-01-07
- `indices_bonds / RUCBITR1Y` missing 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RUCBICP1Y` missing 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RUCBITRL1` missing 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RUCBITRL2` missing 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RUCBITRL3` missing 6: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23, 2022-03-24, 2022-03-25
- `indices_bonds / RUGBITR1Y` missing 4: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23
- `indices_bonds / RUGBITR3Y` missing 4: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23
- `indices_bonds / RUGBITR5Y` missing 4: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23
- `indices_bonds / RUGBITR10Y` missing 4: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23
- `indices_bonds / RUGBINFTR` missing 4: 2022-01-07, 2022-03-21, 2022-03-22, 2022-03-23
- `indices_bonds / IMOEX` missing 3: 2022-03-21, 2022-03-22, 2022-03-23
- `indices_rusfar / RUSFAR` missing 35: 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, 2024-01-03, ... (+25)
- `indices_rusfar / RUSFAR1W` missing 35: 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, 2024-01-03, ... (+25)
- `indices_rusfar / RUSFAR2W` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfar / RUSFAR1M` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfar / RUSFAR3M` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfar / RUSFARCNY` missing 103: 2022-10-03, 2022-10-04, 2022-10-05, 2022-10-06, 2022-10-07, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, ... (+93)
- `indices_rusfar / RUSFARCN1W` missing 74: 2023-12-29, 2024-01-03, 2024-01-04, 2024-01-05, 2024-01-08, 2024-02-12, 2024-02-13, 2024-02-14, 2024-02-15, 2024-02-16, ... (+64)
- `indices_rusfarrt / RUSFARRT` missing 35: 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, 2024-01-03, ... (+25)
- `indices_rusfarrt / RUSFAR1WRT` missing 35: 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, 2024-01-03, ... (+25)
- `indices_rusfarrt / RUSFAR2WRT` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfarrt / RUSFAR1MRT` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfarrt / RUSFAR3MRT` missing 36: 2022-02-24, 2022-12-30, 2023-01-03, 2023-01-04, 2023-01-05, 2023-01-06, 2023-02-24, 2023-05-08, 2023-11-06, 2023-12-29, ... (+26)
- `indices_rusfarrt / RUSFARCNRT` missing 33: 2023-11-06, 2023-12-29, 2024-01-03, 2024-01-04, 2024-01-05, 2024-01-08, 2024-02-12, 2024-02-13, 2024-02-14, 2024-02-15, ... (+23)
- `indices_rusfarrt / RUSFARC1WR` missing 32: 2023-12-29, 2024-01-03, 2024-01-04, 2024-01-05, 2024-01-08, 2024-02-12, 2024-02-13, 2024-02-14, 2024-02-15, 2024-02-16, ... (+22)
- `indices_rusfarrt / RUSFARN` missing 9: 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, 2026-01-06, 2026-01-08, 2026-01-09, 2026-03-09, 2026-05-11
- `indices_rusfarrt / RUSFAR1WN` missing 9: 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, 2026-01-06, 2026-01-08, 2026-01-09, 2026-03-09, 2026-05-11
- `indices_rusfarrt / RUSFAR2WN` missing 9: 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, 2026-01-06, 2026-01-08, 2026-01-09, 2026-03-09, 2026-05-11
- `indices_rusfarrt / RUSFAR1MN` missing 9: 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, 2026-01-06, 2026-01-08, 2026-01-09, 2026-03-09, 2026-05-11
- `indices_rusfarrt / RUSFAR3MN` missing 9: 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, 2026-01-06, 2026-01-08, 2026-01-09, 2026-03-09, 2026-05-11
- `indices_rusfarrt / RUSFARCNN` missing 30: 2025-10-01, 2025-10-02, 2025-10-03, 2025-10-06, 2025-10-07, 2025-10-08, 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, ... (+20)
- `indices_rusfarrt / RUSFARC1WN` missing 30: 2025-10-01, 2025-10-02, 2025-10-03, 2025-10-06, 2025-10-07, 2025-10-08, 2025-11-01, 2025-11-03, 2025-12-30, 2026-01-05, ... (+20)
- `zcyc` missing 1: 2022-01-07

## Operations

### ops.data_gap by dataset and status

Status: `open` = missing, queued for backfill; `backfilled` = data present now; `unfillable` = the source confirmed no data after the grace period (one business day); `not_expected` = the calendar no longer expects the day and there is no data (e.g. a holiday added to the calendar).

| dataset | status | gaps | days | first | last |
| --- | --- | --- | --- | --- | --- |
| algopack_fo_obstats | not_expected | 10 | 10 | 2026-10-02 | 2026-10-15 |
| algopack_fo_tradestats | backfilled | 10 | 10 | 2026-10-02 | 2026-10-15 |
| algopack_futoi | backfilled | 10 | 10 | 2026-10-02 | 2026-10-15 |
| bonds_eod | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| bonds_eod | not_expected | 21 | 21 | 2024-02-23 | 2026-06-12 |
| bonds_eod_yield | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| bonds_eod_yield | not_expected | 21 | 21 | 2024-02-23 | 2026-06-12 |
| bonds_eod_yield | unfillable | 1 | 1 | 2024-02-08 | 2024-02-08 |
| cbr_key_rate | not_expected | 72 | 72 | 2013-11-04 | 2018-12-31 |
| cbr_ruonia | not_expected | 132 | 132 | 2010-02-22 | 2018-12-31 |
| cbr_standing_rates | not_expected | 134 | 67 | 2014-03-10 | 2018-12-31 |
| cbr_standing_rates | unfillable | 2 | 1 | 2018-04-25 | 2018-04-25 |
| ecb_fx | not_expected | 57 | 57 | 2015-04-03 | 2026-05-01 |
| futures_eod | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| fx_eod | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| fx_eod | not_expected | 21 | 21 | 2024-02-23 | 2026-06-12 |
| fx_indicative | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| fx_indicative | not_expected | 21 | 21 | 2024-02-23 | 2026-06-12 |
| indices_all | backfilled | 10 | 10 | 2022-01-07 | 2025-11-01 |
| indices_all | not_expected | 34 | 34 | 2022-03-07 | 2026-06-12 |
| indices_bonds | backfilled | 52 | 4 | 2024-04-27 | 2025-11-01 |
| indices_bonds | unfillable | 55 | 6 | 2022-01-07 | 2022-03-25 |
| indices_rusfar | unfillable | 355 | 104 | 2022-02-24 | 2026-10-07 |
| indices_rusfarrt | unfillable | 348 | 62 | 2022-02-24 | 2026-10-07 |
| zcyc | backfilled | 4 | 4 | 2024-04-27 | 2025-11-01 |
| zcyc | not_expected | 51 | 51 | 2022-02-23 | 2026-06-12 |
| zcyc | unfillable | 1 | 1 | 2022-01-07 | 2022-01-07 |

### ops.backfill_task by status

| status | dataset | tasks | attempts | first created | last created |
| --- | --- | --- | --- | --- | --- |
| cancelled | algopack_fo_obstats | 5 | 0 | 2026-10-16 13:19:50 | 2026-10-16 13:19:50 |
| done | bonds_eod | 4 | 4 | 2026-10-16 13:19:50 | 2026-10-16 13:19:50 |
| done | bonds_eod_yield | 1 | 1 | 2026-10-16 13:19:52 | 2026-10-16 13:19:52 |
| done | cbr_key_rate | 36 | 36 | 2026-10-16 13:19:52 | 2026-10-16 13:19:52 |
| done | cbr_ruonia | 31 | 31 | 2026-10-16 13:19:59 | 2026-10-16 13:19:59 |
| done | cbr_standing_rates | 1 | 1 | 2026-10-16 13:20:00 | 2026-10-16 13:20:00 |
| done | ecb_fx | 39 | 39 | 2026-10-16 13:20:00 | 2026-10-16 13:20:00 |
| done | futures_eod | 4 | 4 | 2026-10-16 13:20:00 | 2026-10-16 13:20:00 |
| done | fx_eod | 4 | 4 | 2026-10-16 13:20:02 | 2026-10-16 13:20:02 |
| done | fx_indicative | 4 | 4 | 2026-10-16 13:20:03 | 2026-10-16 13:20:03 |
| done | indices_all | 8 | 8 | 2026-10-16 13:20:03 | 2026-10-16 16:00:59 |
| done | indices_rusfar | 37 | 37 | 2026-10-16 13:20:04 | 2026-10-16 13:20:04 |
| done | zcyc | 4 | 4 | 2026-10-16 13:20:04 | 2026-10-16 13:20:04 |

### Last ops.job_run per job (UTC)

| job | run | status | started | finished | inserted | updated | runs | failed runs | last succeeded | error |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| algopack_fx_futures | 414 | succeeded | 2026-10-16 15:44:24 | 2026-10-16 16:00:13 | 470300 | 0 | 57 | 2 | 2026-10-16 15:44:24 | - |
| backfill_worker | 416 | succeeded | 2026-10-16 16:01:09 | 2026-10-16 16:01:10 | 0 | 0 | 2 | 0 | 2026-10-16 16:01:09 | - |
| bonds_eod | 214 | succeeded | 2026-10-16 13:25:05 | 2026-10-16 13:25:31 | 0 | 0 | 39 | 1 | 2026-10-16 13:25:05 | - |
| bonds_static | 134 | succeeded | 2026-10-16 11:49:38 | 2026-10-16 12:03:42 | 920 | 21368 | 3 | 0 | 2026-10-16 11:49:38 | - |
| calendar_seed | 201 | succeeded | 2026-10-16 13:20:34 | 2026-10-16 13:22:41 | 13148 | 0 | 3 | 0 | 2026-10-16 13:20:34 | - |
| cbr_daily | 293 | succeeded | 2026-10-16 13:33:27 | 2026-10-16 13:33:34 | 69 | 0 | 69 | 0 | 2026-10-16 13:33:27 | - |
| cbr_meetings | 399 | succeeded | 2026-10-16 14:04:36 | 2026-10-16 14:04:40 | 9496 | 4 | 2 | 0 | 2026-10-16 14:04:36 | - |
| ecb_fx | 333 | succeeded | 2026-10-16 13:34:36 | 2026-10-16 13:34:36 | 0 | 0 | 40 | 0 | 2026-10-16 13:34:36 | - |
| futures_eod | 337 | succeeded | 2026-10-16 13:34:43 | 2026-10-16 13:34:46 | 406 | 0 | 37 | 0 | 2026-10-16 13:34:43 | - |
| fx_indicative | 345 | succeeded | 2026-10-16 13:35:04 | 2026-10-16 13:35:05 | 20 | 0 | 21 | 0 | 2026-10-16 13:35:04 | - |
| fx_moex_eod | 341 | succeeded | 2026-10-16 13:34:54 | 2026-10-16 13:34:57 | 731 | 0 | 37 | 0 | 2026-10-16 13:34:54 | - |
| import_andromeda_cny | 411 | succeeded | 2026-10-16 15:42:44 | 2026-10-16 15:42:44 | 16838 | 0 | 1 | 0 | 2026-10-16 15:42:44 | - |
| indices_eod | 418 | succeeded | 2026-10-16 16:01:09 | 2026-10-16 16:01:10 | 0 | 0 | 74 | 0 | 2026-10-16 16:01:09 | - |
| legacy_map_bc | 25 | succeeded | 2026-10-16 09:23:25 | 2026-10-16 09:23:25 | 3 | 0 | 1 | 0 | 2026-10-16 09:23:25 | - |
| legacy_map_md | 24 | succeeded | 2026-10-16 09:23:23 | 2026-10-16 09:23:25 | 105543 | 0 | 1 | 0 | 2026-10-16 09:23:23 | - |
| maintenance | 412 | succeeded | 2026-10-16 15:43:43 | 2026-10-16 15:43:49 | 0 | 0 | 1 | 0 | 2026-10-16 15:43:43 | - |
| quality | 415 | succeeded | 2026-10-16 16:00:39 | 2026-10-16 16:01:00 | 0 | 0 | 5 | 0 | 2026-10-16 16:00:39 | - |
| swaps_otc_monitor | 410 | succeeded | 2026-10-16 15:15:06 | 2026-10-16 15:15:07 | 4 | 0 | 1 | 0 | 2026-10-16 15:15:06 | - |
| swaps_ratescalcs | 394 | succeeded | 2026-10-16 13:40:46 | 2026-10-16 13:40:49 | 4 | 0 | 1 | 0 | 2026-10-16 13:40:46 | - |
| zcyc | 392 | succeeded | 2026-10-16 13:38:12 | 2026-10-16 13:38:13 | 1 | 0 | 23 | 0 | 2026-10-16 13:38:12 | - |

## Bond static coverage

| measure | all bonds | active |
| --- | --: | --: |
| bonds | 16055 | 4033 |
| with coupon schedule | 12654 | 3557 |
| with offers | 3280 | 961 |
| with amortization/redemption rows | 15499 | 3971 |

Status: active 4033, matured 10804, delisted 1218.

### coupon_type

| coupon_type | bonds | active |
| --- | --: | --: |
| fixed | 10077 | 2418 |
| floating | 706 | 646 |
| indexed_face | 15 | 13 |
| unknown | 4134 | 573 |
| variable_known | 1121 | 381 |
| zero | 2 | 2 |

### ref.bond_coupon status (future = pay_date >= 2026-10-16)

| status | coupons | bonds | future | future, active bonds |
| --- | --: | --: | --: | --: |
| current_published | 267 | 267 | 267 | 265 |
| known | 151443 | 12430 | 40367 | 29238 |
| projected_by_moex | 6448 | 361 | 6448 | 4969 |
| unknown | 46385 | 1892 | 39424 | 34602 |

### Floaters (coupon_type = floating) and ref.bond_rate_formula_resolved

- floaters: 706 (active 646)
- with resolved benchmark: 669 (active 646), with benchmark and spread: 668

| benchmark | formula status | bonds | active |
| --- | --- | --: | --: |
| (none) | partial | 37 | 0 |
| CPI | inferred | 18 | 18 |
| KEY_RATE | inferred | 531 | 509 |
| KEY_RATE | partial | 1 | 1 |
| OTHER | inferred | 2 | 2 |
| RUONIA | curated | 1 | 1 |
| RUONIA | inferred | 84 | 83 |
| RUONIA_3M | inferred | 4 | 4 |
| RUONIA_6M | inferred | 2 | 2 |
| RUONIA_INDEX | inferred | 8 | 8 |
| ZCYC | curated | 1 | 1 |
| ZCYC | inferred | 17 | 17 |

## Legacy import (ops.legacy_import)

| id | schema | source file | sha256 | imported (UTC) | tables | rows | verified | verified at (UTC) | problems | warnings | latest |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | legacy_bc | bond_catalog.sqlite3 | 85294b41a98d | 2026-10-16 09:23:07 | 16 | 830997 | yes | 2026-10-16 09:23:19 | 0 | 0 | yes |
| 1 | legacy_md | market-data.sqlite3 | 3e80be57fbc4 | 2026-10-16 09:23:03 | 37 | 763802 | yes | 2026-10-16 09:23:11 | 0 | 0 | yes |

Latest import per schema verified: yes
