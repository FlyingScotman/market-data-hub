# Bond static verification

Objective check of the bond static data (`ref.bond`, `ref.bond_coupon`, `ref.bond_amortization`,
`ref.bond_offer`, `ref.bond_rate_formula_resolved`) against what MOEX publishes every day:

1. **Accrued interest**: for every `(secid, board, trade_date)` of `md.bond_eod`, accrued interest is
   recomputed from the schedule (current coupon period, coupon amount and period days, outstanding
   face after amortizations) under 6 conventions x 2 date bases (trade date, T+lag settlement date of
   the board) and compared with MOEX `ACCRUEDINT`. The best candidate per bond is its inferred
   convention.
2. **Yield** (fixed and zero-coupon bonds): MOEX effective yield recomputed from the schedule and
   the price, to MOEX's yield date (maturity or offer), compared with `md.bond_eod_yield`.
3. **Invariants** on every bond of `ref.bond`.

Regenerate (read-only, ~16 s on the seed):

```bash
uv run mdhub verify static --out docs/STATIC_VERIFICATION.md --bonds-csv data/static_verification_bonds.csv
```

The generated block below is replaced on every run; the findings after it are hand-written. The
per-bond CSV (one row per bond, mismatch reasons and examples) is not committed (1.7 MB); it lives at
`data/static_verification_bonds.csv` of the main checkout. Run of 2026-10-17 on `data/pgseed`
(EOD 2024-01-03 .. 2026-10-15).

## Key results

* **MOEX `ACCRUEDINT` is accrued to the trade date, not to the settlement date**, on every board
  (TQCB 93.4 % of rows match on the trade date vs 8.1 % on T+1; TQOB, TQOD, TQOY, TQIR, TQOE
  likewise). The settlement-date value is in `md.bond_eod_yield.accrued_int`.
* **Units**: for a foreign-currency face traded on a board that settles in another currency, MOEX
  reports `ACCRUEDINT` in the *settlement* currency: `round(AI in face currency, 2) x CBR rate of
  the trade date` (RUB boards), via the CBR cross rate on USD boards (TQOD). Without the
  conversion, sovereign_fx matched 26 %; with it, 100 %.
* **Inferred accrued-interest conventions**:
  * `coupon_pro_rata` = coupon amount x days elapsed / period days, the MOEX formula. It explains
    every OFZ-PD/AD, subfederal, municipal, IFI and structured bond, 75/76 MBS and 3,176 of 3,482
    corporates.
  * `rate_act365` on the **indexed** face (`md.bond_eod.face_value` of the day) explains all OFZ-IN
    and the corporate linkers.
  * `rate_30_360` (Eurobond basis) explains 16 of 29 `sovereign_fx`, 3 of 10 `foreign_sovereign` and
    96 corporates, mostly replacement bonds (ЗО) of Eurobonds.
  * ACT/360 is never inferred for accrued interest.
* **Coupon amounts** follow `rate x face x days / 365` for 94 % of corporates and all
  OFZ/subfederal/municipal/MBS. Most of the remaining corporates and sovereign FX bonds follow 30/360
  or `rate / frequency`.
* **Match rates** (inferred convention):
  * fixed corporates 99.1 % of rows;
  * OFZ-PD, OFZ-AD, OFZ-IN, subfederal, municipal, MBS (fixed), sovereign_fx, foreign_sovereign
    100 %.
* **Floaters** match only where the rate is fixed before the period:
  * CPI, ZCYC and RUONIA_6M linked bonds: 100 %;
  * KEY_RATE: 74.5 % (197 bonds at 100 %, 312 below 95 %);
  * RUONIA: 25 %; RUONIA_INDEX and RUONIA_3M: about 10 %.

  MOEX accrues the current period of an in-arrears floater at the fixings known so far, so a
  mismatch there is source behaviour, not a static error.
* **Yields** match MOEX within 1 bp:
  * municipal 100 %, subfederal 99.8 %, OFZ-PD 99.6 %;
  * fixed corporates 91.1 %; sovereign_fx 78 %.

  Most of the rest is the foreign-currency-face group: a consistent 6–10 bp gap that no standard
  convention reproduces, `fx_face_bond`, 96 bonds.

<!-- BEGIN GENERATED: mdhub verify static -->

Generated 2026-10-17 13:38 UTC by `mdhub verify static` in 16 s (read-only).

* EOD rows (`md.bond_eod`): 1,976,163 from 2024-01-03 to 2026-10-15, 5,423 bonds; `ref.bond`: 16,055 bonds.
* Yield rows (`md.bond_eod_yield`, fixed + zero coupon): 40,899 on 38 trade dates (sample `month_end`), 2,597 bonds.
* Tolerances: accrued interest 0.01 (face currency), yield 0.01 pp.
* Reference candidate `coupon_pro_rata@trade` = coupon amount x days elapsed / period days, accrued to the trade date; the inferred candidate is the best of 12 convention x date-basis combinations per bond.

## Summary

| check | unit | checked | matched / passed | rate |
|---|---|---|---|---|
| Accrued interest, reference `coupon_pro_rata@trade` | EOD rows | 1526277 | 1336017 | 87.53% |
| Accrued interest, inferred per-bond convention | EOD rows | 1526476 | 1424283 | 93.31% |
| Yield (fixed + zero coupon) | yield rows | 37958 | 34857 | 91.83% |
| Invariant `amortization_sum`: Redemptions (amortizations + final) sum to the initial face | bonds | 15665 | 15196 | 97.01% |
| Invariant `redemption_at_maturity`: Last redemption is dated on the maturity date | bonds | 15387 | 15376 | 99.93% |
| Invariant `coupon_dates`: Coupon dates increase, periods neither overlap nor leave gaps | bonds | 12654 | 12640 | 99.89% |
| Invariant `coupon_amount_vs_rate`: Coupon amount = rate x period face x days / 365 | bonds | 11221 | 9845 | 87.74% |
| Invariant `fixed_coupons_known`: Active fixed bonds: every future coupon has an amount or rate | bonds | 2418 | 2372 | 98.10% |
| Invariant `floater_formula`: Every floater has a resolved formula with a benchmark | bonds | 706 | 669 | 94.76% |
| Invariant `perpetual_label`: Perpetuals labelled; maturity agrees with MOEX EOD MATDATE | bonds | 15996 | 15589 | 97.46% |

## 1. Accrued interest vs MOEX ACCRUEDINT

### Date basis by board

Matches of each bond's inferred convention evaluated on the trade date vs the settlement date (trade date + board lag in MOEX business days). A row matches both bases only when the accrual does not change between the two dates (e.g. zero accrual).

| board | settle lag | rows | trade-date basis matches | settle-date basis matches | both bases | inferred basis |
|---|---|---|---|---|---|---|
| TQCB | T+1 | 1396391 | 93.37% | 8.07% | 8.03% | trade |
| TQOB | T+1 | 41177 | 76.76% | 1.35% | 1.21% | trade |
| TQOD | T+1 | 32513 | 99.98% | 4.40% | 4.39% | trade |
| TQOY | T+1 | 27651 | 100.00% | 2.37% | 2.37% | trade |
| TQIR | T+1 (default) | 26240 | 99.12% | 2.58% | 2.58% | trade |
| TQOE | T+1 (default) | 2037 | 100.00% | 0.05% | 0.05% | trade |
| PACT | T+1 (default) | 461 | 90.89% | 0.00% | 0.00% | trade |
| TQIY | T+1 (default) | 239 | 100.00% | 0.00% | 0.00% | trade |
| TQRD | T+1 | 8 | 87.50% | 0.00% | 0.00% | trade |
| TQUD | T+1 (default) | 8 | 87.50% | 0.00% | 0.00% | trade |

### Inferred convention by bond class (bonds)

| bond class | bonds | `coupon_pro_rata@trade` | `coupon_freq_365@trade` | `rate_act365@trade` | `rate_30_360@trade` | `rate_actact@trade` | `coupon_pro_rata@settle` | `coupon_freq_365@settle` | `no match` |
|---|---|---|---|---|---|---|---|---|---|
| corporate | 3482 | 3176 | 16 | 182 | 96 | 4 | 2 | 4 | 2 |
| subfederal | 132 | 132 |  |  |  |  |  |  |  |
| securitization | 82 | 75 |  | 7 |  |  |  |  |  |
| mbs | 76 | 75 |  | 1 |  |  |  |  |  |
| structured | 55 | 55 |  |  |  |  |  |  |  |
| ofz_pd | 39 | 39 |  |  |  |  |  |  |  |
| sovereign_fx | 29 | 12 |  | 1 | 16 |  |  |  |  |
| ofz_pk | 23 | 18 |  | 4 |  | 1 |  |  |  |
| foreign_sovereign | 10 | 7 |  |  | 3 |  |  |  |  |
| municipal | 8 | 8 |  |  |  |  |  |  |  |
| ifi | 4 | 4 |  |  |  |  |  |  |  |
| ofz_ad | 4 | 4 |  |  |  |  |  |  |  |
| ofz_in | 4 |  |  | 4 |  |  |  |  |  |

### Inferred convention by coupon type (bonds)

| coupon type | bonds | `coupon_pro_rata@trade` | `coupon_freq_365@trade` | `rate_act365@trade` | `rate_30_360@trade` | `rate_actact@trade` | `coupon_pro_rata@settle` | `coupon_freq_365@settle` | `no match` |
|---|---|---|---|---|---|---|---|---|---|
| fixed | 2703 | 2422 | 4 | 164 | 107 | 4 |  | 1 | 1 |
| floating | 679 | 653 | 12 | 7 |  | 1 | 2 | 3 | 1 |
| variable_known | 436 | 421 |  | 14 | 1 |  |  |  |  |
| unknown | 117 | 108 |  | 2 | 7 |  |  |  |  |
| indexed_face | 13 | 1 |  | 12 |  |  |  |  |  |

### Match rates by bond class and coupon type

| bond class | coupon type | bonds | rows checked | match (reference) | match (inferred) | bonds 100% | bonds <95% | rows skipped |
|---|---|---|---|---|---|---|---|---|
| corporate | fixed | 3048 | 816524 | 91.65% | 99.09% | 2282 | 48 | 255356 |
| corporate | floating | 642 | 258645 | 67.27% | 67.45% | 226 | 394 | 9305 |
| corporate | variable_known | 460 | 205060 | 98.36% | 99.72% | 413 | 4 | 8666 |
| structured | unknown | 407 | 1330 | 100.00% | 100.00% | 4 | 0 | 81138 |
| structured | fixed | 237 | 15971 | 100.00% | 100.00% | 48 | 0 | 56319 |
| corporate | unknown | 162 | 31085 | 94.62% | 99.59% | 86 | 1 | 17376 |
| subfederal | fixed | 117 | 50552 | 100.00% | 100.00% | 117 | 0 | 0 |
| securitization | fixed | 54 | 16689 | 98.55% | 100.00% | 53 | 0 | 858 |
| mbs | fixed | 47 | 31181 | 100.00% | 100.00% | 47 | 0 | 0 |
| mbs | variable_known | 42 | 1112 | 100.00% | 100.00% | 9 | 0 | 18191 |
| ofz_pd | fixed | 39 | 22424 | 100.00% | 100.00% | 39 | 0 | 0 |
| sovereign_fx | fixed | 28 | 20147 | 38.33% | 100.00% | 28 | 0 | 0 |
| ofz_pk | floating | 24 | 13352 | 27.76% | 28.01% | 5 | 18 | 419 |
| securitization | unknown | 20 | 807 | 100.00% | 100.00% | 18 | 0 | 292 |
| mbs | floating | 17 | 8799 | 99.94% | 99.94% | 16 | 0 | 0 |
| foreign_sovereign | fixed | 10 | 7741 | 61.88% | 100.00% | 10 | 0 | 0 |
| subfederal | floating | 10 | 2921 | 100.00% | 100.00% | 10 | 0 | 0 |
| corporate | indexed_face | 9 | 4540 | 8.28% | 100.00% | 9 | 0 | 0 |
| municipal | fixed | 8 | 4223 | 100.00% | 100.00% | 8 | 0 | 0 |
| securitization | variable_known | 7 | 623 | 91.81% | 91.81% | 5 | 1 | 222 |
| securitization | floating | 6 | 226 | 23.89% | 23.89% | 4 | 1 | 1213 |
| mbs | unknown | 5 | 770 | 100.00% | 100.00% | 3 | 0 | 40 |
| ifi | fixed | 4 | 1145 | 100.00% | 100.00% | 4 | 0 | 0 |
| ofz_ad | fixed | 4 | 2486 | 100.00% | 100.00% | 4 | 0 | 0 |
| ofz_in | indexed_face | 4 | 2846 | 5.38% | 100.00% | 4 | 0 | 0 |
| subfederal | unknown | 4 | 1973 | 100.00% | 100.00% | 4 | 0 | 0 |
| structured | variable_known | 3 | 2130 | 100.00% | 100.00% | 3 | 0 | 0 |
| corporate | zero | 2 | 0 | - | - | 0 | 0 | 202 |
| securitization | indexed_face | 1 | 0 | - | - | 0 | 0 | 90 |
| sovereign_fx | unknown | 1 | 1074 | 100.00% | 100.00% | 1 | 0 | 0 |
| subfederal | variable_known | 1 | 100 | 100.00% | 100.00% | 1 | 0 | 0 |

### Floaters by benchmark

MOEX accrues a floater's current period at the rate known so far; the schedule holds the final coupon. Benchmarks fixed before the period should match; in-arrears benchmarks (RUONIA averages) cannot until the period ends.

| benchmark | bonds | rows checked | match (inferred) | bonds 100% | bonds <95% |
|---|---|---|---|---|---|
| KEY_RATE | 514 | 192249 | 74.55% | 197 | 312 |
| RUONIA | 83 | 50130 | 24.64% | 16 | 67 |
| (no benchmark) | 31 | 11458 | 60.04% | 9 | 22 |
| CPI | 18 | 12780 | 100.00% | 18 | 0 |
| ZCYC | 18 | 12780 | 100.00% | 18 | 0 |
| RUONIA_INDEX | 7 | 1566 | 11.94% | 0 | 7 |
| RUONIA_3M | 4 | 1367 | 8.12% | 0 | 4 |
| OTHER | 2 | 332 | 79.52% | 1 | 1 |
| RUONIA_6M | 2 | 1281 | 100.00% | 2 | 0 |

### Top mismatch patterns

Rows the bond's inferred convention does not match, by the first applicable pattern (`mdhub.verify.static.classify_ai_mismatch`).

| pattern | meaning | rows | bonds | top class/coupon type (rows) | example SECIDs (rows) | example |
|---|---|---|---|---|---|---|
| `floater_accrual_vs_final_coupon` | source: floater accrued at an interim rate (in arrears) | 93474 | 417 | corporate/floating (83689), ofz_pk/floating (9612), securitization/floating (172) | RU000A105YH8 (668), RU000A105MN1 (667), RU000A107GC2 (664), RU000A107050 (663), RU000A107084 (663) | RU000A105YH8 TQCB 2024-01-03: moex=3.26 calc=3.3238 |
| `accrual_rate_differs` | label: MOEX accrued at another rate (coupon not fixed in advance) | 5710 | 58 | corporate/fixed (5115), corporate/variable_known (495), corporate/unknown (99) | RU000A1064L3 (522), RU000A107EA1 (506), RU000A106JX4 (498), RU000A10A7F7 (312), RU000A107BR1 (307) | RU000A1064L3 TQCB 2024-01-03: moex=31.45 calc=32.6461 |
| `unexplained` | not explained by the patterns below - inspect | 1423 | 47 | corporate/fixed (1330), corporate/variable_known (64), corporate/unknown (29) | RU000A0JVM32 (345), RU000A1084S6 (139), RU000A106JX4 (107), RU000A1089W7 (100), RU000A107EA1 (90) | RU000A0JVM32 TQCB 2024-01-05: moex=34.2 calc=34.2125 |
| `eod_coupon_differs` | static: schedule amount differs from MOEX COUPONVALUE of the day | 642 | 37 | corporate/floating (489), corporate/fixed (99), securitization/variable_known (50) | RU000A0ZZYS0 (252), XS2346922755 (81), RU000A0JS9X1 (67), RU000A102ZC3 (50), RU000A108QA3 (43) | RU000A0ZZYS0 TQCB 2025-06-19: moex=0.66 calc=0.0025 |
| `face_differs` | static: EOD face value differs from the schedule's outstanding face | 375 | 1 | corporate/fixed (375) | RU000A102QL3 (375) | RU000A102QL3 TQCB 2024-01-03: moex=189806.6764 calc=1913.5284 |
| `day_shift_-2` | dates: MOEX accrual is n days off the schedule's period start | 143 | 8 | corporate/fixed (143) | RU000A0JVVL4 (74), RU000A108TV3 (39), RU000A106YZ8 (21), USG76237AC37 (4), RU000A1064L3 (2) | RU000A0JVVL4 TQCB 2024-01-03: moex=34.18 calc=34.1909 |
| `day_shift_-1` | dates: MOEX accrual is n days off the schedule's period start | 141 | 8 | corporate/fixed (138), corporate/variable_known (3) | RU000A108TV3 (43), RU000A108DH6 (41), RU000A106YZ8 (40), RU000A1064L3 (8), RU000A107548 (3) | RU000A108TV3 TQCB 2024-09-02: moex=5.7700 (RUB/91.186800) calc=5.6833 |
| `day_shift_+1` | dates: MOEX accrual is n days off the schedule's period start | 77 | 9 | corporate/fixed (71), corporate/variable_known (6) | RU000A108TV3 (25), RU000A108DH6 (22), RU000A108PR9 (11), RU000A107548 (6), RU000A10A3L4 (5) | RU000A108TV3 TQCB 2024-06-26: moex=0.0900 (RUB/87.277000) calc=0.0000 |
| `day_shift_+2` | dates: MOEX accrual is n days off the schedule's period start | 37 | 8 | corporate/fixed (37) | RU000A108DH6 (15), RU000A108PR9 (9), RU000A10A3L4 (4), RU000A106JX4 (3), RU000A105KW6 (2) | RU000A108DH6 TQCB 2024-07-11: moex=6.3900 (RUB/87.855100) calc=5.8333 |
| `day_shift_+6` | dates: MOEX accrual is n days off the schedule's period start | 37 | 4 | corporate/fixed (37) | RU000A1089W7 (21), RU000A108PR9 (12), RU000A1064L3 (3), RU000A107R03 (1) | RU000A1089W7 TQCB 2025-01-31: moex=49.2900 (RUB/107.972000) calc=47.3021 |
| `day_shift_+7` | dates: MOEX accrual is n days off the schedule's period start | 35 | 3 | corporate/fixed (35) | RU000A1089W7 (22), RU000A108PR9 (12), RU000A107VW9 (1) | RU000A1089W7 TQCB 2025-01-03: moex=44.8100 (RUB/112.977400) calc=42.8208 |
| `day_shift_+3` | dates: MOEX accrual is n days off the schedule's period start | 24 | 6 | corporate/fixed (24) | RU000A105KW6 (8), RU000A108PR9 (7), RU000A106JX4 (4), RU000A107EA1 (3), RU000A107R03 (1) | RU000A105KW6 TQCB 2026-02-24: moex=40.11 calc=38.7136 |
| `moex_zero` | source: MOEX accrues nothing (default / suspended accrual) | 18 | 9 | corporate/floating (12), corporate/fixed (6) | RU000A10DFJ2 (3), RU000A10DJP1 (3), RU000A10A1N4 (2), RU000A10CM22 (2), RU000A10D4G9 (2) | RU000A10DFJ2 TQCB 2025-11-20: moex=0 calc=0.4428 |
| `day_shift_+5` | dates: MOEX accrual is n days off the schedule's period start | 16 | 4 | corporate/fixed (16) | RU000A108PR9 (10), RU000A107EA1 (3), RU000A109K16 (2), RU000A107R03 (1) | RU000A108PR9 TQCB 2024-12-23: moex=8.1000 (RUB/106.544400) calc=7.8867 |
| `full_coupon` | source: MOEX shows the whole coupon (pay-date handling) | 16 | 13 | corporate/fixed (16) | RU000A1006X9 (2), RU000A1006Y7 (2), RU000A1006Z4 (2), RU000A1078V2 (1), RU000A107B84 (1) | RU000A1006X9 TQCB 2024-03-27: moex=1002739.73 calc=0.0000 |
| `day_shift_+4` | dates: MOEX accrual is n days off the schedule's period start | 14 | 5 | corporate/fixed (14) | RU000A108PR9 (9), RU000A106JX4 (2), RU000A1064L3 (1), RU000A107R03 (1), RU000A109K16 (1) | RU000A108PR9 TQCB 2025-01-27: moex=9.4900 (RUB/103.187000) calc=9.3244 |
| `day_shift_-3` | dates: MOEX accrual is n days off the schedule's period start | 3 | 3 | corporate/fixed (3) | RU000A1064L3 (1), RU000A107A69 (1), RU000A10DJP1 (1) | RU000A1064L3 TQCB 2024-09-19: moex=74.4 calc=75.9554 |
| `day_shift_-5` | dates: MOEX accrual is n days off the schedule's period start | 3 | 2 | corporate/fixed (3) | RU000A1064L3 (2), RU000A106JX4 (1) | RU000A1064L3 TQCB 2024-07-29: moex=46.5 calc=49.0868 |
| `day_shift_-6` | dates: MOEX accrual is n days off the schedule's period start | 3 | 1 | corporate/fixed (3) | RU000A107EA1 (3) | RU000A107EA1 TQCB 2024-08-15: moex=27.73 calc=30.9893 |
| `day_shift_-4` | dates: MOEX accrual is n days off the schedule's period start | 2 | 1 | corporate/fixed (2) | RU000A107EA1 (2) | RU000A107EA1 TQCB 2024-07-23: moex=16.31 calc=18.4848 |

### Rows not evaluated

| reason | rows | bonds | examples (rows) |
|---|---|---|---|
| `moex_null` | 441387 | 1603 | RU000A0ZYX28 (1065), RU000A107W63 (748), RU000A0JXGV0 (710), RU000A0JXRM6 (710), RU000A0ZYL89 (710) |
| `coupon_unknown` | 7971 | 425 | RU000A10C1T9 (238), RU000A10C1U7 (214), RU000A10C1V5 (213), RU000A1064L3 (124), RU000A10F124 (119) |
| `no_coupon_schedule` | 329 | 2 | RU000A109MB8 (239), RU000A10DZC5 (90) |

## 2. Yield vs MOEX effective yield (fixed and zero-coupon bonds)

| bond class | coupon type | bonds | rows checked | matched | rate | bonds 100% | rows skipped |
|---|---|---|---|---|---|---|---|
| corporate | fixed | 2222 | 32977 | 30047 | 91.12% | 1787 | 1402 |
| subfederal | fixed | 113 | 2383 | 2378 | 99.79% | 108 | 4 |
| structured | fixed | 99 | 320 | 298 | 93.12% | 85 | 60 |
| securitization | fixed | 38 | 104 | 100 | 96.15% | 2 | 612 |
| ofz_pd | fixed | 37 | 1122 | 1118 | 99.64% | 33 | 0 |
| mbs | fixed | 36 | 37 | 36 | 97.30% | 0 | 861 |
| sovereign_fx | fixed | 27 | 461 | 360 | 78.09% | 20 | 0 |
| foreign_sovereign | fixed | 10 | 245 | 234 | 95.51% | 7 | 0 |
| municipal | fixed | 8 | 166 | 166 | 100.00% | 8 | 0 |
| ofz_ad | fixed | 4 | 123 | 119 | 96.75% | 3 | 2 |
| corporate | zero | 2 | 19 | 0 | 0.00% | 0 | 0 |
| ifi | fixed | 1 | 1 | 1 | 100.00% | 1 | 0 |

### Yield mismatch patterns

| pattern | meaning | rows | bonds | top class/coupon type (rows) | example SECIDs (rows) | example |
|---|---|---|---|---|---|---|
| `fx_face_bond` | foreign-currency face: MOEX yield a few bp off (convention unknown) | 1407 | 96 | corporate/fixed (1295), sovereign_fx/fixed (101), foreign_sovereign/fixed (11) | RU000A1056U0 (43), RU000A105A95 (43), XS0088543193 (43), RU000A1059P4 (38), RU000A1059Q2 (38) | RU000A1056U0 TQCB 2024-01-31: moex=5.3358 calc=5.4326 to MATDATE 2027-03-23 |
| `unexplained` | not explained by the patterns below - inspect | 869 | 125 | corporate/fixed (838), corporate/zero (19), structured/fixed (7) | RU000A102LF6 (26), RU000A108YZ4 (23), RU000A109KL1 (23), RU000A10A7A8 (20), RU000A102879 (19) | RU000A102LF6 TQCB 2024-01-31: moex=20.8323 calc=29.3218 to MATDATE 2030-12-13 |
| `accrued_differs` | MOEX's settlement accrued interest (used) differs from the schedule's | 481 | 121 | corporate/fixed (468), structured/fixed (7), ofz_pd/fixed (4) | RU000A10A133 (16), RU000A102614 (15), RU000A107BR1 (15), RU000A109K16 (12), RU000A106LZ5 (11) | RU000A10A133 TQCB 2024-11-29: moex=15.1742 calc=13.2785 to MATDATE 2029-10-16 |
| `short_horizon` | horizon <= 1 year: MOEX convention may differ | 250 | 85 | corporate/fixed (240), structured/fixed (8), subfederal/fixed (2) | KG000A3LSJ06 (12), RU000A0NRCD1 (10), RU000A1053R3 (10), RU000A1069J6 (9), RU000A0NTYB5 (8) | KG000A3LSJ06 TQCB 2024-12-30: moex=20.1306 calc=20.4370 to MATDATE 2025-12-26 |
| `maturity_differs` | static: MOEX yield date differs from ref maturity | 71 | 8 | corporate/fixed (71) | RU000A105GV6 (18), RU000A108G88 (15), RU000A106SK2 (12), RU000A0ZYC31 (8), RU000A105AS5 (8) | RU000A105GV6 TQCB 2024-01-31: moex=16.6292 calc=14.5638 to MATDATE 2025-11-18 |
| `solver_failed` | no yield solves the price (price/flows inconsistent) | 22 | 16 | corporate/fixed (17), ofz_ad/fixed (4), subfederal/fixed (1) | SU46011RMFS1 (4), RU000A103UP4 (3), RU000A103Y40 (2), RU000A0JWGV2 (1), RU000A0ZYH02 (1) | - |
| `offer_not_in_ref` | static: MOEX yields to an offer missing in ref.bond_offer | 1 | 1 | corporate/fixed (1) | RU000A106ZQ4 (1) | RU000A106ZQ4 TQCB 2026-07-31: moex=42.5839 calc=42.4750 to OFFER 2026-11-11 |

### Yield rows not evaluated

| reason | rows | bonds | examples (rows) |
|---|---|---|---|
| `price_or_yield_null` | 1156 | 178 | RU000A0JWV63 (38), RU000A0ZZ4T1 (38), RU000A0ZZ505 (38), RU000A0ZZY59 (38), RU000A105QW3 (38) |
| `future_coupon_unknown` | 1000 | 78 | RU000A102MB3 (38), RU000A105UC7 (38), RU000A1062J1 (38), RU000A1064L3 (38), RU000A106JX4 (38) |
| `mbs_yield` | 785 | 33 | RU000A0ZZV86 (38), RU000A105H23 (38), RU000A102D46 (36), RU000A102GD1 (36), RU000A102JB9 (36) |

## 3. Static invariants (all bonds in ref.bond)

| check | rule | applicable | pass | fail | pass rate | not applicable |
|---|---|---|---|---|---|---|
| `amortization_sum` | Redemptions (amortizations + final) sum to the initial face | 15665 | 15196 | 469 | 97.01% | initial_face_unknown=221; no_schedule=86; perpetual=64; indexed_face=19 |
| `redemption_at_maturity` | Last redemption is dated on the maturity date | 15387 | 15376 | 11 | 99.93% | no_maturity=530; no_redemption_rows=138 |
| `coupon_dates` | Coupon dates increase, periods neither overlap nor leave gaps | 12654 | 12640 | 14 | 99.89% | no_coupons=3401 |
| `coupon_amount_vs_rate` | Coupon amount = rate x period face x days / 365 | 11221 | 9845 | 1376 | 87.74% | no_rate_and_amount=4535; face_unknown=280; indexed_face=19 |
| `fixed_coupons_known` | Active fixed bonds: every future coupon has an amount or rate | 2418 | 2372 | 46 | 98.10% | not_active=7659; not_fixed=5978 |
| `floater_formula` | Every floater has a resolved formula with a benchmark | 706 | 669 | 37 | 94.76% | not_floating=15349 |
| `perpetual_label` | Perpetuals labelled; maturity agrees with MOEX EOD MATDATE | 15996 | 15589 | 407 | 97.46% | no_schedule=59 |

### Invariant failures by reason

| check | reason | bonds | active | top class/coupon type | example SECIDs | example detail |
|---|---|---|---|---|---|---|
| `amortization_sum` | `no_redemption_rows` | 406 | 0 | corporate/unknown (290), corporate/fixed (110), corporate/variable_known (4) | MVZ240003017, MVZ240007018, RU0001011900, RU0001231565, RU0001231870 | - |
| `amortization_sum` | `sum_below_face` | 57 | 3 | corporate/unknown (49), securitization/fixed (2), unknown/unknown (2) | RU000A1018G9, RU000A10CW87, RU000A10DF38, RU000A0JQC15, RU000A0JWT18 | RU000A0JQC15: sum=908.01 of 1000 (90.80%) |
| `amortization_sum` | `sum_above_face` | 6 | 0 | corporate/fixed (5), ofz_in/indexed_face (1) | RU000A0JVZY8, RU000A0JXY85, RU000A0ZZ141, RU000A0ZZ158, RU000A10A9K3 | RU000A0JVZY8: sum=1573.65 of 1000 (157.36%) |
| `coupon_amount_vs_rate` | `amount_not_rate_based` | 822 | 154 | corporate/fixed (584), corporate/variable_known (83), corporate/unknown (37) | BYM000001941, BYM000002469, RU000A0JVRL2, RU000A0JX2M2, RU000A0JXLU2 | AT0000A2UF10: 5/12 coupons off ACT/365, e.g. #2 |
| `coupon_amount_vs_rate` | `matches_rate_30_360` | 453 | 57 | corporate/fixed (309), unknown/fixed (55), corporate/unknown (36) | BYM000001818, CH1100259816, RU000A0JXTS9, RU000A0JXU14, RU000A0ZYYN4 | AMUNIBB2AER2: 3/12 coupons off ACT/365, e.g. #2 |
| `coupon_amount_vs_rate` | `matches_rate_actact` | 74 | 10 | corporate/fixed (52), corporate/variable_known (6), sovereign_fx/fixed (6) | BYM000001925, BYM000002410, RU000A102CL3, RU000A1034K8, RU000A105EV1 | BYM000001925: 2/12 coupons off ACT/365, e.g. #7 |
| `coupon_amount_vs_rate` | `matches_rate_act360` | 16 | 8 | corporate/fixed (10), corporate/unknown (4), cbr_bond/fixed (1) | RU000A104FK4, RU000A10B008, RU000A10B3Z3, RU000A10BS68, RU000A10C8A4 | RU000A0BCQQ0: 12/12 coupons off ACT/365, e.g. #1 |
| `coupon_amount_vs_rate` | `matches_rate_freq` | 11 | 2 | corporate/fixed (6), foreign_sovereign/fixed (4), foreign_sovereign/variable_known (1) | RU000A0ZYX28, RU000A1089W7, RU000A103GT5, RU000A1078V2, RU22TNCO3012 | RU000A0ZYX28: 1/20 coupons off ACT/365, e.g. #16 |
| `coupon_dates` | `gap` | 13 | 0 | corporate/fixed (12), corporate/unknown (1) | RU0001987620, RU0003935882, RU0008274204, RU0008288774, RU0008503032 | RU0001987620: gap@2, gap@3, gap@4 |
| `coupon_dates` | `record_after_pay` | 1 | 0 | unknown/fixed (1) | XS0503839622 | XS0503839622: record_after_pay@1 |
| `fixed_coupons_known` | `future_coupon_unknown` | 46 | 46 | corporate/fixed (34), structured/fixed (9), foreign_sovereign/fixed (3) | RU000A0JS4Z7, RU000A0JSGV0, RU000A0JT403, RU000A0JT6B2, RU000A0JV4R9 | RU000A0JS4Z7: 9/11 future coupons without amount or rate, e.g. #32 |
| `floater_formula` | `formula_without_benchmark` | 37 | 0 | corporate/floating (29), ofz_pk/floating (8) | RU000A0JXJM3, RU000A0JXJP6, RU000A1004L9, RU000A1049Y7, RU000A104KW9 | - |
| `perpetual_label` | `no_maturity_not_perpetual` | 406 | 0 | corporate/unknown (343), subfederal/unknown (35), municipal/unknown (10) | RU000A0D2LR2, RU000A0D2ZE0, RU000A0DCCZ7, RU000A0DG205, RU000A0DG213 | - |
| `perpetual_label` | `maturity_missing_eod_has_it` | 1 | 0 | corporate/unknown (1) | RU000A0JY023 | RU000A0JY023: eod MATDATE=2024-08-21 |
| `redemption_at_maturity` | `last_redemption_before_maturity` | 11 | 5 | mbs/fixed (4), securitization/unknown (3), mbs/variable_known (1) | RU000A10AAQ4, RU000A10C949, RU000A10F892, RU000A10FQV9, RU000A10FTZ4 | RU000A0JP8D8: last=2014-08-15 maturity=2039-02-15 |

### Invariant pass rate by bond class (active bonds)

| bond class | active bonds | `amortization_sum` | `redemption_at_maturity` | `coupon_dates` | `coupon_amount_vs_rate` | `fixed_coupons_known` | `floater_formula` | `perpetual_label` |
|---|---|---|---|---|---|---|---|---|
| corporate | 2368 | 2294/2295 | 2306/2306 | 2349/2349 | 1762/1948 | 1312/1346 | 592/592 | 2368/2368 |
| foreign_sovereign | 16 | 16/16 | 16/16 | 16/16 | 6/16 | 12/15 | - | 16/16 |
| mbs | 105 | 105/105 | 104/105 | 105/105 | 96/97 | 46/46 | 17/17 | 105/105 |
| municipal | 2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | - | 2/2 |
| ofz_ad | 2 | 2/2 | 2/2 | 2/2 | 2/2 | 2/2 | - | 2/2 |
| ofz_in | 4 | - | 4/4 | 4/4 | - | - | - | 4/4 |
| ofz_pd | 31 | 31/31 | 31/31 | 31/31 | 31/31 | 31/31 | - | 31/31 |
| ofz_pk | 21 | 21/21 | 21/21 | 21/21 | 18/20 | - | 21/21 | 21/21 |
| securitization | 73 | 68/70 | 69/73 | 71/71 | 54/66 | 44/44 | 6/6 | 73/73 |
| sovereign_fx | 25 | 25/25 | 25/25 | 25/25 | 6/25 | 24/24 | - | 25/25 |
| structured | 1323 | 1323/1323 | 1323/1323 | 868/868 | 819/820 | 851/860 | - | 1323/1323 |
| subfederal | 63 | 63/63 | 63/63 | 63/63 | 63/63 | 48/48 | 10/10 | 63/63 |

### Coupon amount convention by bond class (bonds, most coupons explained)

| bond class | bonds | `rate_act365` | `rate_actact` | `rate_30_360` | `rate_act360` | `rate_freq` |
|---|---|---|---|---|---|---|
| corporate | 8871 | 8322 | 92 | 399 | 10 | 48 |
| structured | 865 | 865 |  |  |  |  |
| subfederal | 409 | 409 |  |  |  |  |
| mbs | 220 | 220 |  |  |  |  |
| securitization | 114 | 114 |  |  |  |  |
| unknown | 79 | 4 | 9 | 66 |  |  |
| ofz_pd | 73 | 73 |  |  |  |  |
| municipal | 58 | 58 |  |  |  |  |
| foreign_sovereign | 42 | 16 |  | 16 |  | 10 |
| sovereign_fx | 35 | 8 | 6 | 21 |  |  |
| ofz_pk | 28 | 26 | 2 |  |  |  |
| ifi | 27 | 27 |  |  |  |  |
| ofz_ad | 13 | 13 |  |  |  |  |
| cbr_bond | 12 | 9 | 2 |  | 1 |  |
| ofz_n | 9 | 9 |  |  |  |  |
| ofz_in | 1 | 1 |  |  |  |  |
| sovereign_other | 1 |  |  | 1 |  |  |

<!-- END GENERATED: mdhub verify static -->


## Findings and proposed loader fixes

No loader was changed in this task. Each item says what was found, where, and what to change and
why. Numbers refer to the run above; SECIDs are examples.

1. **"Fixed" bonds whose coupon is not fixed in advance** (`fixed_coupons_known`: 46 active fixed
   bonds with future coupons that have neither amount nor rate; AI pattern `accrual_rate_differs`:
   58 bonds, MOEX accrues the period at one rate and the coupon is then paid at another). Examples:
   RU000A106JX4, RU000A107EA1, RU000A1064L3: MOEX labels them `Фиксированный с известными
   купонами`, yet the next coupons are unpublished and MOEX accrued 17 % p.a. while the coupon was paid
   at 14.45 %.

   *Fix*: in `moex_static.classify_coupon_type`, an **active** bond with a fixed MOEX label whose
   schedule has future coupons without amount and rate should become `variable_known` (basis
   `schedule`), with a `ref.static_conflict` row (`coupon_type`, moex label vs schedule).

   *Why*: consumers treat `fixed` as known cash flows. Here the yield check had to skip 78 bonds
   (`future_coupon_unknown`), and a pricing engine would silently assume a wrong coupon.

   *Done* (PARSER_VERSION 2): `moex_static.unknown_future_coupons` + `classify_coupon_type`; the
   loader writes the conflict (`moex_schedule` vs `moex_label`) and resolves it once every future
   coupon is published or the bond stops being active. Coupons with a rate but no amount stay
   known, so fixed-rate MBS/securitization pools stay `fixed`. The generated tables above predate
   the change.
2. **Paid coupons of foreign-currency bonds are RUB amounts converted back** (3,543 coupons of 470
   FX-face bonds carry more than 2 decimals, e.g. RU000A105A95 #8 = 43.1954342 instead of the
   contractual 43.13; RU000A1064Y6 16.8632599 vs 16.88). MOEX `bondization.value` of a paid coupon is
   `value_rub / FX`; the loader stores it verbatim. This drives most `coupon_amount_vs_rate` failures
   of FX bonds (`amount_not_rate_based`, 154 active).

   *Fix*: keep MOEX's value (it is what was paid). Document it in the
   `ref.bond_coupon.amount` comment, and add a derived contractual amount only via a migration in
   main's range when a consumer needs exact face-currency cash flows.

   *Why*: past amounts feed historical cash-flow and total-return calculations. They are off by up to
   about 0.15 %, and not visibly so.
3. **Indexed face not detected** (`amortization_sum` `sum_above_face`, 6 bonds). For example,
   RU000A0JVZY8 redeems 1,573.65 on an initial face of 1,000 (157 %), yet `is_face_indexed` is false
   and the bond is classed `fixed`.

   *Fix*: set `is_face_indexed` (and coupon type `indexed_face`) when the final redemption or the
   current face exceeds the initial face.

   *Why*: indexed-face bonds need the face of the day for accrued interest and yields; the
   verification already uses the EOD face for them.
4. **Delisted bonds without maturity**: 406 bonds (corporate/subfederal/municipal, issued in the
   2000s, e.g. RU000A0D2LR2) have coupons but no `MATDATE` and no redemption row, so they are neither
   matured nor labelled perpetual (`perpetual_label` `no_maturity_not_perpetual`;
   `amortization_sum` `no_redemption_rows`). Only RU000A0JY023 has a MATDATE in MOEX EOD
   (2024-08-21) that `ref.bond` lacks.

   *Fix*:
   * take `maturity_date` from the latest EOD `MATDATE` when the description has none (1 bond);
   * optionally, mark the others' maturity as unknown rather than leaving them indistinguishable from
     perpetuals by NULL, e.g. by keeping `is_perpetual = false` and adding a maturity basis.

   *Why*: low priority, since none of them is active. The current rule is correct (it never guesses
   perpetual); the gap is only that "maturity unknown" and "perpetual" are both NULL.
5. **Floaters without a benchmark**: 37 floaters have a resolved formula row without a benchmark
   (29 corporate, 8 OFZ-PK, e.g. RU000A0JXJM3). All of them are matured or delisted: every **active**
   floater has a resolved formula.

   *Fix*: map the old OFZ-PK series to RUONIA / key rate through the `ofz_series` rule when history
   matters.
6. **Floater fixing timing can be inferred from MOEX accruals.** A KEY_RATE floater whose accrued
   interest matches the final coupon on 100 % of days is fixed in advance (197 bonds). Consistent
   mismatches mean an averaged or in-arrears rate (312 bonds below 95 %).

   *Proposal*: feed the per-bond result (CSV `ai_match_pct`, `ai_reasons`) to the curation of
   `ref.bond_rate_formula` (`averaging`, `fixing` evidence) instead of leaving those fields NULL.

   *Why*: `current_published` vs `known` coupon status depends on exactly this.
7. **Projected amortization schedules** (`sum_below_face`, 3 active, e.g. RU000A10CW87, RU000A10DF38,
   RU000A1018G9; `last_redemption_before_maturity`, 11 MBS/securitizations). MOEX publishes a
   truncated, projected pool amortization (zeros after 2028 for RU000A10CW87). This is source
   behaviour.

   *Proposal*: no loader change. Consumers of MBS/securitization redemptions should treat the tail as
   unknown.
8. **Historical terms are not in the current schedule.** 8 bonds (e.g. RU000A105GV6) were yielded by
   MOEX in 2024 to a maturity that is earlier than today's `ref.bond.maturity_date` (restructured /
   extended). RU000A102QL3 changed its face (EOD face differs from the schedule in 2024).
   RU000A106ZQ4 was yielded to an offer on 2026-11-11 that `ref.bond_offer` does not have.

   *Proposal*: historical verification should read `ref.*` as known at the trade date
   (`ops.row_history`, the snapshot layer's as-known-at readers) once that lands. Check
   RU000A106ZQ4's offers with a single-SECID bondization reload.
9. **Coupon date defects**: 13 old delisted bonds have gaps between periods (e.g. RU0001987620), and
   XS0503839622 has a record date after the pay date. These come from MOEX data, so there is no fix.

## Not static errors (for the record)

* `floater_accrual_vs_final_coupon` (93k rows, 417 bonds): in-arrears accrual (item 6).
* `fx_face_bond` yields (96 bonds): MOEX's yield for foreign-currency bonds is 6–10 bp off any
  standard convention (ACT/365, 30/360 or ACT/ACT time, any accrued variant). This is likely an
  internal MOEX price or FX convention. The schedules of these bonds pass the accrued-interest
  check at 100 %.
* `md.bond_eod_yield.accrued_int` of foreign-currency bonds is sometimes in RUB and sometimes in
  the face currency. The yield check uses it only when it is in the face currency.
* `moex_null` (441k rows): MOEX publishes no `ACCRUEDINT` for those rows (boards without trading,
  structured notes). `TQRD` has almost none.
