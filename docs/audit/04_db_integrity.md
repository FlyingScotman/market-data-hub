# 04 — Integrity and completeness of the seed snapshots (as of 2026-10-15)

**Scope.** This report covers two SQLite snapshots:

| Short name | File | Producing repo |
|---|---|---|
| **MD** | `data/incoming/md/market-data/market-data.sqlite3` | market-data-store |
| **BC** | `data/incoming/bc/bond_catalog/bond_catalog.sqlite3` | moex-market-data-mcp |

**How the checks were run.**
- The originals were never opened for writing. Their SHA-256 hashes match `MANIFEST.sha256` both before and after the work.
- All queries ran on read-only copies (`chmod 444`) in `scratchpad/dbwork/`, opened with `sqlite3 -readonly` or `python3 -I` using `mode=ro`.
- Scripts: `scratchpad/scripts/md_checks.py` and `bc_checks.py`. Raw outputs: `scratchpad/dbwork/md_out.txt` and `bc_out.txt`.

**Trading calendar.**
- Trading days were fetched from MOEX ISS: IMOEX and RGBI history from 2021-07-01 to 2026-10-14, plus today, 2026-10-15. The result is in `dbwork/calendar.json`.
- MOEX holidays in 2026: 01-01, 01-02, 01-07, 02-23, 05-01 and 06-12.
- MOEX trades on several Russian official non-working days, for example 2026-01-05..09, 03-09 and 05-11. On those days CBR does not publish RUONIA. I checked two sample windows against cbr.ru.

---

## 0. Verdict in one paragraph

**Physical state: both files are clean.**
- `PRAGMA integrity_check` returns `ok` and `foreign_key_check` returns nothing.
- The schema is byte-identical to `SCHEMA.sql`.
- Row counts and date ranges match `SNAPSHOT.json` exactly for all 37 MD tables and all 16 BC tables.
- There is no sign of page-level corruption or lost rows.

**The real risks are logical:**
1. **BC, critical.** Blanking `raw_json` / `board_json` breaks loader and analytics code that calls `json.loads` on those columns. One path, `retire_missing_events`, can permanently block the weekly catalog refresh that would otherwise fill the columns again.
2. **MD.** One collector-wide outage (2026-09-25 07:38Z → 2026-09-29 03:00Z, which loses trading day 2026-09-28), a few source-specific gaps, and several dead or stale feeds: andromeda (stopped 2026-09-10), CFETS curves (one capture), cny_onshore base (one day), cbonds (stopped 2026-09-13).
3. **MD.** Raw payloads are gone, so history cannot be re-parsed. This matters most for the partial ratescalcs KRS curves from 2026-08-05..08-10 and the partial andromeda curves from 2022–2023.

---

## 1. Physical integrity, schema, counts, migrations

| Check | MD | BC |
|---|---|---|
| `PRAGMA integrity_check` | ok | ok |
| `PRAGMA foreign_key_check` | no rows | no rows |
| journal_mode / freelist | delete / 0 | delete / 0 |
| Schema vs SCHEMA.sql (normalized diff) | identical | identical |
| Row counts vs SNAPSHOT.json | 37/37 match | 16/16 match |
| Date ranges vs SNAPSHOT.json | all match | all match |
| `PRAGMA user_version` | **0** | 0 |
| `schema_migrations` | 1..17 (last: `per_quote_basis`, 2026-09-23) | n/a (no migration table) |
| File SHA-256 vs MANIFEST | match | match |

**Notes:**
- **MD `user_version` is 0, but the migrator sets it to the latest version (17).** The export rebuilt the file (it was not a file copy), which is consistent with blanking columns in tables protected by immutability triggers. This is harmless because `apply_migrations` relies on `schema_migrations` and not on `user_version`.
- **The repo HEAD (market-data-store) defines migrations 18 and 19, which the snapshot does not have yet:**
  - **18 `price_observation_revisions`:** a no-op on data, because `price_observations` has 0 rows.
  - **19 `index_fixing_revisions`:** seeds the revision table from `index_fixings`, using `fetched_at_utc` as the as-known time. Every fixing from 2021-08-02 onward will look as if it was "known" from 2026-08-06 at the earliest. As-known replays before 2026-08-06 must therefore use the `retrospective_backfill` policy.
  - The hub will apply both migrations on first open. Run them on a copy first.
- **Tables that are empty by design in MD:** `instruments`, `instrument_identifiers`, `price_observations`, `backfill_requests`, `curve_world_instances`, `curve_source_policies`, `<desk>_curve_artifacts`, `<desk>_curve_nodes`. The repo doc `docs/critical-fixes-2026-10-14.md` also notes the empty price table.
- **Empty in BC:** `ratings`, because the MOEX CCI entitlement is unavailable.

### Blanked payload columns

Every blanked value is a 0-length TEXT. No NULLs and no non-empty values remain.

**MD:**
- `curve_snapshots`: 4,225
- `cbr_liquidity_captures`: 49
- `gcurve_snapshots`: 1,167
- `intraday_quote_captures`: 63,715
- `normalized_base_curve_bundles`: 107
- `<desk>` curves: 0 rows

**BC:**
- `issuers`: 1,670
- `issues`: 16,023
- `issue_revisions`: 72,753
- `boards`: 134,427
- `events`: 256,559
- `event_revisions`: 265,231
- `cbonds_*`: 80 / 15 / 4,052 / 26

**Hash columns:**
- All are valid 64-character hex. None equals sha256('').
- Distinct counts:
  - `curve_snapshots`: 2,585 distinct across 4,225 rows. Repeats are expected, because the ratescalcs and otc pages often do not change between polls.
  - `intraday_quote_captures`: 54,975 distinct across 63,715 rows.

---

## 2. Referential and value sanity

### 2.1 MD — structure, orphans and duplicates

**Clean (0 violations):**
- Every dataset has snapshots, every run has snapshots, and every snapshot has quotes.
- Run source matches dataset source, and run `fetched_at` matches snapshot `fetched_at`.
- Every derived build has nodes. Every v2 projection build has meta, exactly 3 scenarios, scenario nodes and a dependency.
- Every gcurve snapshot has exactly one build with 11 nodes, and build valuation_date = trade_date.
- Every intraday capture has observations.
- Every base bundle has snapshots, and every base snapshot has nodes.
- In the liquidity tables:
  - Every balance points to a revision.
  - That revision is the latest one, with matching values.
  - Every revision chain is unbroken (15 dates have revision 2).
- Position sequences have no gaps, and `build_uid` / `effective_date` are never NULL.

**Snapshots without derived builds: 838, all expected.**
- `cnyruonia_xccy`: 473. No curve model exists for the XCCY dataset.
- CFETS: 2. Base curves come from bundles instead.
- otc_monitor `mid_only` snapshots: 336. These are off-hours captures with no bid/ask, and the builder skips them.
- otc_monitor `bid_ask_mid` key-rate-swap snapshots: 27.

**Natural-key duplicates, all benign but to be handled at hub import:**
- `curve_snapshots` with the same dataset and the same `source_observed_at`: 356 groups (916 rows), 314 of them with different payload hashes.
  - otc_monitor accounts for nearly all of them. It republishes under the same source timestamp, so the hub must choose a rule: latest fetched wins, or keep all.
  - andromeda has 1 such day.
- `curve_snapshots` with the same dataset and the same `payload_sha256`: 205 groups.
  - ratescalcs: all 257 key-rate-swap snapshots fall into 23 payload groups, and all 254 RUONIA snapshots into 29.
  - The source changes about once a day, so polling stores repeats.
- Intraday observations with the same `raw_row_sha256` across captures: 77,820. Each minute re-captures an unchanged row. Deduplicate before computing statistics.
- `derived_curve_builds` with more than one build per (curve, model, valuation_date): 116. This is expected because there are several intraday builds per day.
- `normalized_base_curve_snapshots` with more than one row per world and valuation date: 92. The payloads differ (republished curves).

### 2.2 MD — values

**Curve quotes:**
- 0 rows with bid > ask, mid outside [bid, ask], a non-positive rate, or a rate above 40%.
- Ranges: RUB OIS/KRS 6.77–26.51% (2022 peak), CNY XCCY 0.95–9.0%, CFETS 1.32–1.80%.
- NULL `tenor_months` occurs only for the 1W and 2W tenors, which is by design.
- **Andromeda quotes always have bid = ask** (`quote_basis = mid_only`). Do not use andromeda spreads.
- **XCCY per-quote basis:** 2,136 quotes are `bid_ask_mid` inside `mid_only` snapshots. This is the intended mixed basis from migration 17.

**Derived nodes:**
- Discount factors are within (0.224, 0.9987], never above 1, and decrease monotonically in every build.
- Zero rates are 7.0–27.4%.
- Projection rates are 3.3–26.0%.
- Fit errors are below 1e-10 bp.

**Fixings:**
- **Key rate:** the change history matches the CBR path, including 20% on 2022-02-28, 21% on 2024-10-28 and 14.0% on 2026-07-27.
- **RUONIA:** no RUONIA–key-rate spread exceeds 2pp. Every daily jump above 1pp falls on a key-rate change date.
- **Weekend fixings:** 2022-03-05, 2024-04-27, 2024-11-02, 2024-12-28 (key rate only) and 2025-11-01. All are official Russian working Saturdays.

**Term RUONIA:** 1,900 calendar-day rows. The index increases strictly every day and every tenor is in range.

**CBR liquidity:**
- Claims and obligations equal the sums of their components (within 1bn) in every row.
- There are 7 rows on non-MOEX days: 2025-12-31, 2026-01-01, 01-18 (Sunday), 01-25 (Sunday), 04-19 (Sunday), 06-13 and 06-14. Each has distinct values, so these look like CBR-published rows rather than carry-forwards. Verify them against cbr.ru before relying on them.
- `cbr_liquidity_balances.fetched_at_utc` is overwritten on every fetch: all 311 rows show 2026-10-15T06:00:10Z. For point-in-time work use `cbr_liquidity_balance_revisions` with `cbr_liquidity_captures.fetched_at_utc`.

**G-curve:** the JSON fields parse, yields are 10.6–17.2%, discount factors are 0.042–0.975, and reconciliation error is below 0.01bp (enforced by a CHECK constraint).

**Base curves:** zero rates are 1.3–7.5% and discount factors are 0.189–0.9998.

**Intraday:**
- 0 crossed quotes, 0 non-positive values, 0 non-numeric text values.
- No outliers more than 3% from the day's median.
- `session_date` equals the Moscow date of `source_observed_at` in every row.
- No dated future appears after its expiry: U6 contracts end 2026-09-17, which is the expiry date.

**Timestamps and timezones:**
- All `*_utc` columns are UTC ISO-8601. Nothing is later than the snapshot time, and the only future dates are legitimate (node dates, expiries, policy meetings).
- Formats are mixed: `Z` with seconds, `Z` with milliseconds, and `+00:00` in the gcurve tables only. Normalize them on import. String comparison across `Z` and `+00:00` sorts correctly only by accident.
- Consistency checks:
  - **No 3-hour (MSK) offset bugs.**
  - The 19,528 ratescalcs `ntpro_snapshot_clock` USD/RUB_TOM rows captured after 16:00Z (from 2026-09-15) all have `source_observed_at` frozen at 16:00:00Z, with up to 3.99h lag. These are stale evening repeats, not a timezone error.
  - The moex_apim realtime rows have `source_observed_at` up to 37s after `fetched_at`: local clock skew against the exchange clock. passport_cets rows are skewed by up to about 1.5s.
- `algopack_obstats_mid` rows lag up to 55h over weekends. There are Sunday weekend-session rows (2026-08-30, 09-06, 09-20, 10-04, 10-11), all stamped 16:00:00Z, for 12 futures/perpetual instruments. Keep them or filter them deliberately.

### 2.3 BC — referential and values

**Clean (0 violations):**
- Every event secid, revision secid, board secid, formula secid, availability secid and conflict secid resolves to an issue.
- Every issuer referenced by issues exists.
- Every first/last run reference resolves.
- Every issue and event has a revision, and the stored payload_hash equals the latest revision's hash.
- There are no duplicate events on (secid, type, date) and no duplicate issuer INNs.

**Findings:**

| Finding | Count | Comment |
|---|---|---|
| Issues with NULL/empty `emitent_id` | 1,118 | Mostly bondization stubs and non-exchange bonds |
| Issues with NULL/empty ISIN | 569 | |
| Duplicate ISIN | 1 | `RU000A0D05B7` appears as both `RU000A0D05B7` and `RU22TRMK2009`. Pick one SECID in the hub |
| Duplicate `regnumber` | 24 | Usually tranches. Do not use regnumber as a key |
| Issues without boards | 11,666 | Boards are kept only for securities seen in the market feed |
| **Stale boards** | 111,351 of 134,427 rows have last_seen = first run (2026-08-01). For traded issues, only 4,007 secids have current-run boards; 2,978 do not | Board rows are not refreshed for many traded issues. Rebuild boards from ISS |
| Issues with more than one primary board | 58 | `issues.primary_boardid` disagrees with `boards.is_primary` for 60 |
| `maturity_date = '0000-00-00'` | 58 issues; 595 offer events | Perpetual or AT1 placeholders. A naive `< today` comparison marks them matured. These same 58 issues produce the 58 "maturity < issue_date" and 58 "next_coupon > maturity" hits |
| Truly matured (real date) but `is_traded = 1` | 11 | Matured 2026-10-12..14, after the last run (2026-10-12). Normal refresh lag |
| `next_coupon_date` < today, traded | 117 | Stale since the 2026-10-12 run |
| `facevalue` NULL | 3,476 | Issues without a security description (3,440 not collected) |
| `faceunit` 'SUR' vs currency 'RUB' | 11,979 | Legacy MOEX code. Map SUR to RUB |
| Coupon `rate_pct` out of range | 3 | RU000A102FH4 (605.9%), RU000A102FW3 (422.2%) and RU000A108C41 (98.8%): structured or odd-period coupons. Flag them |
| Amortization rows with value 0 | 13,736 (13,687 on non-matured issues; 148 traded secids) | Mostly structured notes. Treat as "no principal" and not as a schedule error |
| Sum of amortizations differs from initial face by more than 1% | 75 of 15,467 issues | Includes sums of 0 and 1,573 against a 1,000 face. Review before cash-flow use |
| Coupon events with NULL value | 58,144 future, 11,608 past | Floaters, structured notes, and past coupons never fixed in the feed |
| Events with `last_seen_run` older than the latest run | 28 | Not yet retired, or tombstones pending. This is what triggers the `retire_missing_events` crash (§5) |
| Events sharing (secid, type, ordinal) | 11,569 groups | All have `ordinal = 0`, which is unnumbered. Not a real duplicate |

---

## 3. Time-series completeness (MD)

Expected dates are MOEX trading days. Where a source follows CBR business days, missing dates that are Russian official non-working days are marked as **legit**.

### 3.1 Summary per series

| Dataset / series | First | Last | Obs (days) | Gaps on MOEX trading days | Status |
|---|---|---|---|---|---|
| CBR key_rate | 2021-08-02 | 2026-10-15 | 1,324 | 2022-01-07 only (MOEX holiday quirk). Extra rows on 2022-02-28..03-18 when MOEX was closed: legit | OK |
| CBR RUONIA | 2021-08-02 | **2026-10-13** | 1,283 | 39 missing, all Russian non-working days (legit; cbr.ru sample confirms 2021-11-05 has no fixing). **Real gap: 2026-10-14** | Reload 10-14 (published 2026-10-15 afternoon; last fetch was 04:15Z) |
| CBR term RUONIA | 2021-08-02 | 2026-10-14 | 1,900 (calendar days) | none | OK |
| CBR liquidity | 2025-08-06 | 2026-10-14 | 311 | none (10-15 is not published yet) | OK. History starts only at 2025-08-06 |
| CBR policy meetings | 2020-02-07 | 2027-12-17 | 64 | n/a | Last fetched 2026-10-01. Refresh before the 2026-10-23 meeting |
| ratescalcs ruonia_ois / KRS (polls) | 2026-08-05 | 2026-10-15 | 50 days, 257 snapshots each | **2026-09-25 (after 07:38Z), 2026-09-28** | Gap |
| ratescalcs KRS partial curves | 2026-08-05 | 2026-08-10 | 36 snapshots with only 3M/6M (2 of 13 tenors) | — | Unrecoverable without raw. Treat as bad |
| andromeda ruonia_ois (EOD archive) | 2022-07-06 | **2026-09-10** | 1,017 | 2022-07-11..09-29 (30 days; the source was thin then); 2023-01-03..06, 2023-02-24, 05-08, 2025-05-02, 05-08, 06-13, 11-03, 2026-01-05..09, 03-09, 05-11 (legit non-working days); **real: 2025-07-11, 2026-05-05, 2026-05-12**; **stopped after 2026-09-10** | Stale (feed ended; replaced by otc_monitor) |
| andromeda key_rate_swap | 2022-10-24 | **2026-09-10** | 969 | Same pattern; real gaps 2025-07-11, 2026-05-05, 2026-05-12; stopped after 2026-09-10 | Stale |
| andromeda partial curves | 2022-07-06 | 2023-06-30 | RUONIA 98 and KRS 50 snapshots with fewer than the full tenor set | — | Source-side; not fixable |
| otc_monitor ruonia_ois / KRS | 2026-09-04 | 2026-10-15 | 28 days, 624 snapshots | **2026-09-09, 2026-09-28**; 2026-09-25 thin (1 snapshot) | Gap |
| otc_monitor cnyruonia_xccy | 2026-09-22 | 2026-10-15 | 19 days, 473 snapshots | **2026-09-25** missing; thin 09-22, 09-28 (5–6 snapshots), 10-06. Weekend rows on 10-04 and 10-11 | Gap |
| CFETS FR007 / FDR007 IRS closing | 2026-08-29 | **2026-08-29** | 1 snapshot each | Everything after | Stale (one-off import) |
| Derived ruonia_discount v2 | 2022-07-06 | 2026-10-15 | 1,041 days | Mirrors andromeda gaps, plus 2026-09-28 | Rebuild after the inputs are backfilled |
| Derived key_rate_projection v2 | 2022-10-24 | 2026-10-15 | 993 days | Same as KRS, plus 2026-09-28 | Same |
| ruonia_discount v1 / key_rate_projection v1 | to 2026-08-06 / 2026-08-24 | — | — | Retired model versions | Ignore |
| G-curve (MOEX) | 2026-08-28 | 2026-10-15 | 34 days, 1,167 snapshots, about 37 per day | **2026-09-28** missing; 2026-09-25 only 2 snapshots | Gap |
| Base usd_sofr_ois | 2026-08-28 | 2026-10-15 | 34 | **2026-09-28** | Gap |
| Base eur_estr_ois | 2026-08-28 | 2026-10-15 | 34 | **2026-09-25** | Gap |
| Base cny_fr007_irs | 2026-08-28 | 2026-10-15 | 28 | 2026-09-25..28; 10-01..07 (China Golden Week holiday: legit) | Gap on 09-25 and 09-28 |
| Base cny_onshore (MOEX SPFI e-mail) | 2026-08-28 | **2026-08-28** | 1 | Everything after | Stale |
| Base cny_offshore (world defined) | — | — | 0 | No data at all | Never loaded |
| Intraday moex_apim (13 instruments) | 2026-08-28 | 2026-10-15 | about 898 captures per trading day | **2026-09-28** missing; 2026-09-25 cut at 07:38Z; 2026-09-10 gap 06:54–10:43Z (229 min); 09-02 gap 08:06–09:17Z | Gap |
| Intraday passport CNYRUB_TOM | 2026-09-07 | 2026-10-15 | 13,149 | **2026-09-28**; thin 09-10 (starts 10:56Z) and 09-25 | Gap |
| Intraday ratescalcs USD/RUB_TOM | 2026-08-28 | 2026-10-15 | 19,529 | **2026-09-28**; thin 09-10, 09-21 (ends 10:00Z), 09-25 | Gap |
| U6 futures (SiU6, EuU6, CRU6) | 2026-08-28 | 2026-09-17 | — | Ended at expiry | OK (expired) |
| M7 futures (SiM7, EuM7, CRM7) | 2026-09-17 | 2026-10-15 | — | Started on U6 expiry roll | OK |

**Stale series (last date before 2026-10-14):**
- RUONIA fixing (2026-10-13; this is publication lag)
- andromeda ruonia_ois and KRS (2026-09-10)
- CFETS FR007 and FDR007 (2026-08-29)
- base cny_onshore (2026-08-28)
- base cny_offshore (never loaded)
- policy meetings (fetched 2026-10-01; the data itself extends to 2027)
- BC cbonds (2026-09-13)

**Common-cause outage.** Every collector stops at about 07:38Z on Friday 2026-09-25 and resumes on Tuesday 2026-09-29 at around 03:00Z. Monday 2026-09-28 (a MOEX trading day) is missing in every intraday, otc, ratescalcs, G-curve and base series. The BC weekly run also slipped from Monday to Tuesday 2026-09-29 06:14Z. This points to host downtime.

### 3.2 Gaps to backfill

| # | Dataset | Instrument / series | Missing ranges | Likely backfill source | Feasible? |
|---|---|---|---|---|---|
| G1 | gcurve_snapshots (and zero builds) | MOEX G-curve | 2026-09-28 (whole day); 2026-09-25 after 07:38Z | MOEX ISS `/iss/engines/stock/zcyc` (with `date=`) or the history endpoint | Yes, EOD at least. Intraday parameter snapshots are lost |
| G2 | intraday_quote_observations | SiZ6, SiH7, SiM7, EuZ6, EuH7, EuM7, CRZ6, CRH7, CRM7, USDRUBF, EURRUBF, CNYRUBF, CNYRUB_TOM (algopack obstats) | 2026-09-28; 2026-09-25 07:38–21:00Z; 2026-09-10 06:54–10:43Z; 2026-09-02 08:06–09:17Z | MOEX AlgoPack `obstats` history (5-min bars) via the moex_apim client | Yes for obstats, as `derived` latency. Realtime minute snapshots are not reconstructible |
| G3 | intraday_quote_observations | CNYRUB_TOM (passport realtime) | 2026-09-28; 09-25 after 07:38Z; 09-10 before 10:56Z | MOEX ISS candles / tradestats for CETS CNYRUB_TOM | Partial (trades, not bid/ask) |
| G4 | intraday_quote_observations | USD/RUB_TOM (ratescalcs NTPro) | 2026-09-28; 09-25 after 07:38Z; 09-21 after 10:00Z; 09-10 before 08:50Z | Not archived by the source; use USDRUBF obstats as a proxy | No (proxy only) |
| G5 | curve_snapshots / quotes | otc_monitor ruonia_ois, key_rate_swap | 2026-09-09, 2026-09-28; 2026-09-25 (1 snapshot) | otc-monitor.ru history or archive, if it exists; otherwise andromeda | Check the source |
| G6 | curve_snapshots / quotes | otc_monitor cnyruonia_xccy | 2026-09-25, 2026-09-28 (thin) | Same | Check the source |
| G7 | curve_snapshots / quotes | ratescalcs ruonia_ois, key_rate_swap | 2026-09-25 (after the cutoff), 2026-09-28 | ratescalcs.ru keeps no history | No (use otc or andromeda) |
| G8 | curve_snapshots | andromeda ruonia_ois, key_rate_swap | 2026-09-11..2026-10-15 (feed stopped); 2025-07-11, 2026-05-05, 2026-05-12; 2022-07-11..2022-09-29 (RUONIA, 30 days) | andromedaterminal.ru archive (the collector's archive mode) | Yes if access still works. Decide whether the feed is retired |
| G9 | derived_curve_builds (ruonia_discount v2, key_rate_projection v2) | — | Every date fixed by G5/G8, plus 2026-09-28 | Rebuild locally after the inputs land | Yes |
| G10 | index_fixings | RUONIA | 2026-10-14 | cbr.ru RUONIA | Yes (scheduled job) |
| G11 | normalized_base_curve_* | usd_sofr_ois 2026-09-28; eur_estr_ois 2026-09-25; cny_fr007 2026-09-25 and 09-28 | Free approximate sources (worldgovernmentbonds, CFETS) | Partial (worldgovernmentbonds has no history) |
| G12 | normalized_base_curve_* | cny_onshore_base_v1 | 2026-08-31..2026-10-15 | MOEX SPFI e-mail feed | Depends on the mailbox archive |
| G13 | curve_snapshots | CFETS FR007 / FDR007 | 2026-08-31..2026-10-15 | chinamoney.com.cn historical closing curves | Yes (if the hub still needs CNY curves) |
| G14 | policy_meetings | — | Refresh (last fetched 2026-10-01) | cbr.ru calendar | Yes |

---

## 4. bond_catalog: coverage, maturity and refresh

**Universe: 16,023 issues and 1,670 issuers.**
- In scope: 14,192 (`ordinary_or_unclassified`).
- Out of scope: structured 1,289, securitization 542, structured securitization 176.

**Maturity status (as of 2026-10-15; `0000-00-00` counted as unknown or perpetual):**

| Status | Traded | Not traded |
|---|---|---|
| Active (maturity ≥ today) | 3,952 | 586 |
| Matured | 11 real + 58 placeholder (`0000-00-00`) | 7,967 |
| Unknown maturity | 4 | 3,445 |

- Maturing within 30 days (traded): 134.

**Active traded issues without coupons or schedules:**
- No events at all: 0.
- **No coupon events: 473.**
  - Structured notes: 451.
  - `exchange_bond` with no bond type: 15.
  - Discount bonds: 2. This is expected.
  - Others: 1 each — FX, linker, floater, fixed. These 4 are real gaps.
- No future amortization or maturity event: 4.
- At least one future coupon with NULL value (floater or structured): 1,188 issues.
- Formula status for active traded issues:
  - `preliminary_bondization`: 2,415
  - `unavailable`: 1,523
  - `partial_missing_conventions`: 11
  - `fallback_unverified`: 3
  - `exact_curated`: 2
  - `refused_exact`: 1
  - `partial_public_formula`: 1

**Last refresh per source:**

| Source | Last refresh | Cadence / note |
|---|---|---|
| MOEX ISS catalog run (issues, events, formula) | 2026-10-12T03:43Z (run `20261012T033027Z`, status ok) | Weekly on Mondays; 23 runs, none failed. Next due 2026-10-19 |
| MOEX boards | Partial: 111k rows last seen 2026-08-01 | Stale (see §2.3) |
| Bank of Russia securities registry | `cbr_registry_as_of` = 2026-06-01, 2,492 matches (13,531 not matched) | About 4.5 months old |
| cbonds (`get_emissions`, static recovery) | 2026-09-13T03:50Z | Daily until 09-13, then stopped. Over a month stale |
| MOEX CCI ratings | never (`unavailable_entitlement`) | 0 rows |
| Issuer industry | never (`unavailable_bulk_field`) | |

**Conflicts: 2, both unresolved and both still current in the latest run:**
- `RU000A10A8E8` — amortization 0.005 vs 0.01 USD (legal terms vs MOEX rounding).
- `RU000A1028U9` — issuer call 2030-10-10 is absent from MOEX offers.

**Enrichment coverage:**
- Security description (MOEX): 12,583 of 16,023. All active traded issues have one.
- CBR registry: 2,492 of 16,023 (15.6%), or 2,150 of 3,956 active traded.
- Issuer INN: 1,593 of 1,670.
- cbonds: 15 ISINs only, with cash flows for 5 and offers for 2.
- Ratings: 0.
- LLM-assisted formulas: 4.

---

## 5. What depended on the raw payloads (impact of blanking)

The full analysis is in a sub-report. The key points, verified in the code:

**BC, critical.** These are the columns that currently break code:
- `events.raw_json`
- `issue_revisions.raw_json`

**Code paths affected:**

1. **`catalog.py:559-575` `retire_missing_events`.** It runs `json.loads(raw_json)` on every event not seen in the current run.
   - **Effect:** `JSONDecodeError` → the run is marked failed → rollback. That rollback also discards the upserts that would have filled `raw_json` again.
   - **Why it is effectively permanent:** 28 events are already not current, and earlier runs report `events_disappeared` of up to 300. **The weekly refresh (and the daily static recovery that calls it) will fail on every run.**
   - **Fix options:**
     - tolerate `''` in that function, or
     - pre-fill `'{}'` before the first run, or
     - re-hydrate the column from `artifacts/bond_catalog/<run>/raw/*.jsonl.gz` if the original collector host still has them.
2. **`catalog_analytics.py`** (`:54, :176, :296, :345, :396, :626`) and **`weekly_cashflow_report.py`** (`:160, :206, :302`). Both read event fields that exist only in raw: `startdate`, `coupondate`, per-event `facevalue`, `valueprc`, `offertype`, `offerdatestart`/`end`, `price`, `recorddate`, `initialfacevalue`.
   - Single-bond calls crash.
   - The coverage and inventory functions catch `ValueError` and **silently report bonds as incomplete**.
   - At `:694`, the issue's `raw_market.SETTLEDATE` is lost, so accrued-interest convention inference falls back to ACT/365 **without warning**.
3. **market-data-store `fixed_income.py:744-756` `BondCatalogRepository.events`.** It runs `json.loads(raw_json)` on every event for a SECID. **All fixed-income MCP pricing tools crash.** The same applies to the scripts `calculate_ruonia_spreads.py` and `plot_ofz_pk_zspread_map.py`.
4. **`source_snapshots.export_bond_catalog_snapshot`.** It builds issues from `issue_revisions.raw_json`. A new revision row is written only when the issue's hash changes, so this export stays broken until every issue changes.

**Will refreshes fill the columns again?**
- `issues`, `issuers`, `boards` and `events.raw_json` are overwritten on a *successful* run. Point 1 prevents that.
- `issue_revisions`, `event_revisions` and `cbonds_*_observations` are append-only, so **historical raw is lost permanently**. Revision diffs and the "what did the record look like on run X" audit can no longer be produced for 2026-08-01..10-12.
- Events no longer present in MOEX (past or retired) never get their raw back. Analytics that need `startdate` for past coupons (carry-forward, coupon period) stay broken for them.

**MD, low impact on functionality:**
- No code re-parses stored raw or verifies stored hashes against the stored text.
- Deduplication keys on the stored hash, so there is no churn on re-ingest.
- `release_seed.py` copies `raw_payload` when raw distribution is "approved". It would publish `''` as approved raw, so set those sources to `denied`.

**What is lost in MD:**
- **No re-parse of history with a newer parser.** This matters for:
  - ratescalcs KRS parser v1, which produced 2-tenor snapshots for 2026-08-05..08-10;
  - andromeda partial curves from 2022-07..2023-06;
  - otc_monitor parser versions 1–4 (2026-09-04..09-22);
  - cbr liquidity legacy-normalized capture.
- **No re-derivation of fields that were never normalized.** Examples: intraday raw rows beyond bid/offer/last, the G-curve raw page, and bundle provenance.
- **Immutability triggers block any later fix-up.** The gcurve, intraday, desk-curve and base-bundle tables reject UPDATE, so raw cannot be repopulated in place. The export must have bypassed the triggers by rebuilding the file.
- **Desk-curve imports:** `<desk>_curve_artifacts.serialized_payload` is `STRICT BLOB NOT NULL`. The table is empty, but any future import tooling must use `X''` and not `''`.

**Not affected:** the normalized values themselves. Quotes, nodes, fixings, liquidity, intraday bid/offer/last and bond normalized columns were all checked above.

---

## 6. Needs backfill / reload — prioritized

| Prio | Item | Action |
|---|---|---|
| **P0** | BC `events.raw_json` / `issue_revisions.raw_json` blank → refresh deadlock and analytics/pricing crashes | Before seeding the hub, re-hydrate from the original collector DB or run artifacts. Otherwise fill `'{}'` **and** patch `retire_missing_events`, `BondCatalogRepository.events` and `catalog_analytics` to treat empty input as unknown. Then run a full catalog refresh. Better still, take a fresh non-blanked export from production |
| **P0** | Ask the producer for the unblanked originals, or at least `events.raw_json` / `issue_revisions.raw_json` | The README calls this snapshot "not a drop-in collector DB". Seed the hub from the operational DB, or keep this snapshot analysis-only |
| **P1** | 2026-09-25 (after 07:38Z) and 2026-09-28 outage across MD | Backfill G-curve (ISS zcyc), AlgoPack obstats for all FX futures/perpetuals and CNYRUB_TOM, base SOFR/ESTR/FR007, and otc_monitor if it has history. Then rebuild derived curves for 09-25 and 09-28 |
| **P1** | RUONIA 2026-10-14 | Run the CBR fixings job, which is routine |
| **P1** | BC boards stale (2,978 traded secids without current boards) | Force a boards reload from ISS (`/securities/{secid}.json` boards) |
| **P1** | cbonds stopped 2026-09-13 | Restore the job, or mark the cbonds enrichment (15 ISINs) as frozen |
| **P2** | andromeda feed ended 2026-09-10; real gaps 2025-07-11, 2026-05-05, 2026-05-12; RUONIA 2022-07/09 | Decide whether otc_monitor is the successor. If andromeda stays the EOD history source, run archive mode for 2026-09-11..10-15 and the listed dates, then rebuild the v2 builds |
| **P2** | otc_monitor 2026-09-09 missing; XCCY thin days | Backfill if the source has history; otherwise document |
| **P2** | Intraday partial days 2026-09-02, 09-10, 09-21 | AlgoPack obstats backfill for the gap windows |
| **P2** | Policy meetings (fetched 2026-10-01) | Refresh before the 2026-10-23 meeting |
| **P2** | BC weekly run due 2026-10-19 | 11 issues matured 10-12..10-14 and 117 traded issues have a stale `next_coupon_date`. These clear on the first successful run, after the P0 fix |
| **P3** | CFETS FR007/FDR007 (one capture), cny_onshore base (one day), cny_offshore world (never loaded) | Decide whether they are needed. Otherwise disable those curve worlds in the hub |
| **P3** | CBR registry as of 2026-06-01 | Reload the registry; coverage is 15.6% |
| **P3** | Data-quality flags to carry into the hub | Exclude ratescalcs KRS 2-tenor snapshots (2026-08-05..08-10, 36 rows) and andromeda partial curves (before 2023-07). Treat `0000-00-00` as perpetual. Map SUR to RUB. Resolve duplicate ISIN RU000A0D05B7. Treat amortization rows with value 0 as no principal. Review 75 issues whose amortization sum does not match face. Review the 3 coupon rates above 60%. Normalize `+00:00` vs `Z`. Deduplicate repeated intraday rows. Run migrations 18 and 19 on a copy |

**No physical corruption was found.** Lost data:
- historical raw payloads in both DBs (by design of the export, irreversible from this snapshot);
- the 2026-09-25/28 outage window;
- the andromeda tail after 2026-09-10.
