# 03 — Market data and bond static data requirements for the Market Data Hub

Prepared 15 October 2026 (read-only extraction). Sources:

- Backend program pack: `/Users/badtako/Code/python/desk_monitor_backend/docs/program/` (README, EXECUTION_SPEC, requirements.csv, backlog.json, DECISIONS, STATUS, BASELINE_REVIEW, AGENT_HANDOFFS).
- Trading Desk Monitor (TDM): `/Users/badtako/Code/python/trading_desk_monitor`.
- Financial core: `/Users/badtako/Code/python/rub-fixed-income-core` (package 0.6.0, main `454a78c`), including its audit, handoffs and the vendored copies of the existing collectors under `implementation/upstream/{market-data-store,moex-market-data-mcp}`.

> **Important version note.** The local TDM working tree is on `10b9b73` (contract **1.2**, `tdm/__init__.py:6 CONTRACT_VERSION = "1.2"`) and is 14 commits behind `origin/claude/clever-ramanujan-s4k3ed`. The backend pack is written against commit **`a6ba30b`, contract 1.3** (BASELINE_REVIEW.md "Reviewed version"). Contract 1.3 adds `md_fx_cbr`, `cbr_meetings`, CHF / `CHF SARON` / `SARON`, `call_date`, `is_subord` and more. All TDM citations below marked `TDM@a6ba30b:` refer to that commit (read via `git archive`, nothing in the repo was changed). Line numbers in the 1.2 checkout differ.

Citation shorthand: `PROG/` = `desk_monitor_backend/docs/program/`; `CORE/` = `rub-fixed-income-core/`; `MDS/` = `CORE/implementation/upstream/market-data-store/`; `MCP/` = `CORE/implementation/upstream/moex-market-data-mcp/`.

---

## 0. Executive summary

1. **Two consumers with different shapes.**
   - **TDM** (the monitor, its SQLite `tdm` service, historical scenarios and UI) needs **EOD, one-year, flat CSV-shaped series**: `md_curves` (built curve nodes), `md_fx` (market ~19:00 mark), `md_fx_cbr` (CBR official by effective date), `md_fixings`, `md_bond_quotes`, optional `md_cds`, plus static `instruments`, `bond_cashflows`, `issuers`, `issuer_ratings`, `curves`, `calendar`, `cbr_meetings` (TDM@a6ba30b:`tdm/datasets.py:51-344`).
   - **The core library** (run locally at work, never on the VPS for portfolios) needs **per-instrument, evidence-stamped, fully resolved inputs**: a `PublicBondSnapshot` (resolved fixed RUB cash flows, clean price % of current nominal, accrued in RUB per bond, settlement date, horizon, explicit curve or raw OIS quotes, spread convention, and offset-aware `observed_at/known_at/valid_until` evidence per market/schedule/curve record) (`CORE/rub_fixed_income/local_worker.py:50-135`), raw RUONIA OIS par quotes (`discount.py:28,157`), KRS bid/mid/ask quotes plus current key rate and next CBR effective date (`key_rate.py:96,143`), and an official versioned holiday calendar (`docs/BACKLOG.md` item 1, P0).
2. **The hub is upstream of both.** The program defines the VPS as the market/static data service: raw archive, normalized effective-dated records, curve snapshots and **immutable published bundles with manifests**, downloaded **in bulk** by the work PC (`PROG/EXECUTION_SPEC.md:20,30,48-62,90-102`). No portfolio data ever goes to the VPS.
3. **The core currently prices only fixed-coupon RUB bonds** (incl. amortization and confirmed puts). Floaters, linkers, FX bonds, swaps, XCCY and CDS are R2/R3 backlog (requirements.csv C05–C09, C24–C26). The hub must nevertheless collect their static and market inputs now, because historical fixings and terms cannot be reconstructed later without look-ahead (`PROG/backlog.json` DATA-01/HIST-01 P0 items).
4. **Non-negotiable data semantics** across all sources: no forward-fill of missing observations, separate observation / effective / publication(known) / ingestion times, append-only revisions, as-known historical selection, one authoritative provider per series with labelled fallbacks, raw curve instruments archived next to built curves, and partial snapshots that declare their omissions.

---

## 1. Requirements table

Priority key: **P0** = needed for R0/R1 (first end-to-end path and first real book); **P1** = needed for R1/R2 coverage or explicitly in the owner's baseline; **P2** = R2/R3 or nice-to-have; **P3** = only with an entitled source.

Granularity key: **EOD** = one value per business day at the accepted desk cut (~19:00 Europe/Moscow, `TDM@a6ba30b:docs/ORG_CONVENTIONS.md:146`); **Fixing** = official published value with its own effective/publication dates; **Intraday** = time-stamped observations; **Ref** = effective-dated reference record.

### 1.1 Market data

| # | Data item | Granularity | History depth needed | Fields (consumer-facing) | Consumer (file:line) | Priority |
|---|---|---|---|---|---|---|
| M1 | **Bond prices / quotes** (`md_bond_quotes`) — all bonds ever held in the last year **plus issuer benchmarks**; owner baseline extends to **all MOEX bonds** | EOD (TDM); Intraday bid/offer/last exists in current MDS PriceService | 1 year daily (TDM Level B); today + previous business day minimum (Level A) | `date, instrument_id, clean_price` (% of nominal), `ytm` (% to nearest offer while ahead, else maturity), `z_spread_bp` (Z for fixed / DM for floaters — same type as calc `spread_type`), `g_spread_bp` (to KBD), `accrued` (% of nominal), `volume_rub`, `source`. Spec adds: bid/ask/last/valuation price identity, board, quote currency, accrued convention, YTM basis | TDM@a6ba30b:`tdm/datasets.py:318-331`; `docs/DATA_REQUIREMENTS.md:566-585, 48-73`; `tdm/market.py:154-174` (issuer/rating spread moves); `docs/SERVICE_DATA_MODEL.md:183-185` (`/market/bonds`, `/bonds/history`); core `analytics.py:59` (`MarketObservationV1.clean_price_pct_of_current_nominal, settlement_date, selected_price_basis`), `local_worker.py:67` (`clean_price_pct`, `accrued_interest_rub`); `PROG/EXECUTION_SPEC.md:54`; requirements.csv D21 | **P0** |
| M2 | **Built curves on nodes** (`md_curves`): `RUB OFZ`, `RUB RUONIA`, `RUB KEY`, `USD SOFR`, `CNY`, `EUR ESTR`, `CHF SARON` (fixed UI keys) | EOD | 1 year daily, identical node set on every date, coverage 1M…20Y | `date, curve_id, tenor_years, rate` (% — zero **or** par, same for all dates), `source`. TDM interpolates linearly to 11 pillars 1M 3M 6M 1Y 2Y 3Y 5Y 7Y 10Y 15Y 20Y+ | TDM@a6ba30b:`tdm/datasets.py:279-288`; `docs/DATA_REQUIREMENTS.md:116-122, 506-521`; `tdm/market.py:41-48`; `tdm/histscen.py:97-100`; `contract/bundle.schema.json` (`curves[].points` exactly 11 buckets); requirements.csv D09/D17 | **P0** (RUB OFZ, RUB RUONIA, RUB KEY); **P1** (foreign) |
| M3 | **Raw RUONIA OIS quotes** (IRS vs RUONIA) — the instruments behind `RUB RUONIA` | EOD (cut-aligned); existing collectors also intraday | 1 year daily (risk/histscen); archive since 2022 exists | Core `OisQuote(tenor: str, maturity: date, par_rate: float /*decimal*/)`; bootstrap needs `curve_date` + `CurveConvention` (time day count, log-linear DF, flat-forward, fixed-leg schedule & day count). Spec: par/zero/DF representation, compounding, day count, maturity, projection index, collateral/discount curve, bootstrap/interp config, bid/ask/mid | core `discount.py:28-33, 157-216`; `risk.py:94` (`curve: DiscountCurve | CurveBootstrapRequest`); `local_risk.py:21` (`CurveRiskBasis = "continuous_zero" | "ois_quote"`); `docs/risk.md` "Curve risk basis"; `PROG/EXECUTION_SPEC.md:56` | **P0** |
| M4 | **Key-rate swap (KRS, IRS vs key rate) quotes** | EOD (+ intraday in current system) | 1 year daily | Core `KeyRateSwapQuote(tenor: str, bid: float, mid: float, ask: float)` (decimals); build also needs `current_rate`, `next_policy_effective_date`, `discount_curve` (RUONIA), `calendar`. Convention id `moex-irs-keyrate-tom-modfol-actactisda-v1` (TOM, MODFOL, ACT/ACT ISDA, annual fixed vs quarterly `KEYRATE-AVERAGE`) | core `key_rate.py:96-110, 143-154, 383`; `docs/CONVENTIONS.md` "KRS"; TDM `RUB KEY` curve and carry preset `implied` (TDM@a6ba30b:`docs/SCENARIOS.md:263`) | **P0** |
| M5 | **MOEX G-curve / ZCYC (KBD)** — basis of `RUB OFZ`, G-spreads, OFZ-PK constant-maturity coupons | EOD (MOEX also publishes intraday) | 1 year daily; longer for OFZ-PK historical fixings | Raw: `B1..B3, T1, G1..G9`, standard-tenor yields, source and fetch clocks; built: continuous zero, DFs (reproduce published yields within 0.01 bp) | `MDS/docs/curve-engine-design.md:18-38`; TDM `RUB OFZ` "МосБиржа КБД" (`contract/csv/curves.csv`); `md_bond_quotes.g_spread_bp` | **P0** |
| M6 | **Key rate (КС)** | Fixing — stored **by effective date**, value holds until next record | Full history ≥ 1 year; floater realized accrual needs history back to start of the current coupon period (and beyond for KS-average floaters) | `date` (= effective date), `index_name='КС'`, `value` (%) | TDM@a6ba30b:`docs/DATA_REQUIREMENTS.md:552-564`; `tdm/market.py:112-130` (`fixing_on`, `fixings_by_day` per calendar day); TRADING funding RUB = КС + spread (`docs/ORG_CONVENTIONS.md:18-33`); core `KeyRateCurveBuildRequest.current_rate`; `PROG/backlog.json` DATA-01 ("distinguish key-rate change history") | **P0** |
| M7 | **CBR board meetings** (`cbr_meetings`) | Ref (schedule) | Published schedule 1 year ahead + history | `date` (decision), `effective_date` (default next business day) | TDM@a6ba30b:`tdm/datasets.py:188-195`; `docs/DATA_REQUIREMENTS.md:385-396`; `tdm/market.py:133-141`; core `KeyRateCurveBuildRequest.next_policy_effective_date` (`key_rate.py:143`); requirements.csv D11 ("no demo schedule") | **P0** |
| M8 | **RUONIA** (+ RUONIA compounded index, term RUONIA 1M/3M/6M) | Fixing; observation date ≠ publication date (published next business day before ~15:00 MSK) | ≥ 1 year; back to start of every live RUONIA-floater coupon period; official observation calendar required | `date, index_name='RUONIA', value` (%). Spec adds observation date, publication time, accrual applicability | TDM@a6ba30b:`docs/DATA_REQUIREMENTS.md:552-564`; DERIVS RUB cash funding (`docs/ORG_CONVENTIONS.md:48-56`); `CORE/implementation/risk-worker/FIXING_AUDIT.md` (P0 gap: no silent carry over missing weekdays); `CORE/audit/FINLIB_AUDIT.md:191`; `MDS/docs/curve-engine-design.md:94-96` | **P0** |
| M9 | **RUSFAR RUB** (O/N, 1W, 1M; owner baseline: full term set) | Fixing (daily) | 1 year | `index_name ∈ {RUSFAR O/N, RUSFAR 1W, RUSFAR 1M}` | TDM@a6ba30b:`docs/DATA_REQUIREMENTS.md:563`; `funding.csv` example `index_name=RUSFAR O/N` (REPO) | **P1** |
| M10 | **RUSFAR CNY** | Fixing (daily) | 1 year | `index_name='RUSFAR CNY'` | TRADING funding of CNY bonds (`docs/ORG_CONVENTIONS.md:24`) and DERIVS CNY cash (`:53`); fixed UI index key (`docs/DATA_REQUIREMENTS.md:120`) | **P0** |
| M11 | **SOFR, €STR, SARON** | Fixing (daily) | 1 year | `index_name ∈ {SOFR, €STR, SARON}` | DERIVS cash funding USD/EUR/CHF (`docs/ORG_CONVENTIONS.md:48-56`); fixed UI index keys | **P1** |
| M12 | **Market FX mark** (`md_fx`) — USD, EUR, CNY, CHF vs RUB | EOD at ~19:00 cut | 1 year daily | `date, ccy, rate` (RUB per 1 unit), `source`. **Authoritative mark is internal** ("внешние котировки на ~19:00, подаются изнутри") until DEC04 | TDM@a6ba30b:`tdm/datasets.py:289-297`; `docs/DATA_REQUIREMENTS.md:100-101, 522-535`; `tdm/market.py:51-52`; `docs/ORG_CONVENTIONS.md:96-103`; `PROG/DECISIONS.md` DEC04, `PROG/EXECUTION_SPEC.md:132`; BASELINE_REVIEW B04 | **P0** (hub supplies a labelled *candidate* series) |
| M13 | **CBR official FX** (`md_fx_cbr`) | Fixing by **effective date**, with set (announcement) date; weekend rows allowed | Every calendar day for 1 year | `date` (effective), `ccy` (USD, EUR, CNY, CHF), `rate` (RUB per 1 unit), `set_date`. Spec adds announcement time, quote nominal (normalize to per-unit), known time | TDM@a6ba30b:`tdm/datasets.py:299-307`; `docs/DATA_REQUIREMENTS.md:537-550`; `tdm/market.py:92-109` (`fx_cbr_on`, `fx_cbr_next`, `fx_cbr_move`); `PROG/EXECUTION_SPEC.md:58`; BASELINE_REVIEW B08 | **P0** |
| M14 | **Foreign OIS curves**: USD SOFR OIS, EUR €STR OIS, CHF SARON | EOD | 1 year | Raw quotes + built nodes as M2/M3 | TDM curve keys; DERIVS/XCCY/FX forward pricing (requirements.csv C06–C08); `MDS/docs/currency-market-data-plan.md:102-119` (free proxies exist only for USD/EUR, labelled `~`; CHF absent) | **P1** |
| M15 | **CNY curves**: OIS vs RUSFAR CNY; CNY/RUB XCCY vs RUONIA; CNY/RUB FX swap points | EOD | 1 year | Quotes with conventions; SPFI IDs `CNY-OIS-RUSFCN`, `CNY-OIS-RFCQTE`, `CNY-XCS-CNYRUO`, `CNY-XCS-CNRQTE`, `SPC-FXS-CNRQTE`, discount worlds `CNY-DSC-CSACNY/CSARUB` | TDM `CNY` curve; requirements.csv C08 XCCY (R2); `MDS/docs/currency-market-data-plan.md:76-100` (SPFI values **not entitled**); `CORE/audit/FINLIB_AUDIT.md:251-257` (XCCY freshness) | **P1** (owner baseline) |
| M16 | **FX futures**: dated Si (USD/RUB), Eu (EUR/RUB), CNY (CNY/RUB) — first three live contracts; perpetual USDRUBF, EURRUBF, CNYRUBF | Intraday (5-min) + EOD settlement | From now forward (existing history since 28 Aug 2026); hub update 2026-10-16: AlgoPack 5-min bars (tradestats, FUTOI) kept as a **rolling 90-day window** only (owner decision, `MDHUB_ALGOPACK_RETENTION_DAYS`); EOD settlement (`md.futures_eod`) and realtime marketdata (`md.futures_intraday`) keep full history | Price, normalized `RUB per FX = raw × (STEPPRICE/MINSTEP)/LOTVOLUME`; last-trade, fixing/effective, delivery/expiry, spot value dates; source and clock quality | Not consumed by TDM directly. Used as labelled `perpetual_proxy` fallback for USD/EUR/CNY spot and for implied rates (`MDS/docs/currency-market-data-plan.md:40-74`) | **P1** (owner baseline) |
| M17 | **CDS quotes** (`md_cds`) | EOD | 1 year if available | `date, issuer_id, tenor_years, spread_bp`; conventions; recovery 25% only for quote→upfront | TDM@a6ba30b:`tdm/datasets.py:332-341`; `docs/ORG_CONVENTIONS.md:127-129`; requirements.csv D22 (external_licensed, R2) | **P3** |
| M18 | **CPI (ИПЦ) and OFZ-IN indexation** | Monthly fixing (+ daily indexed nominal) | Back to the base CPI of every live OFZ-IN (≥ issue date of oldest) | CPI index level by reference month, publication date; per-issue indexed nominal / indexation coefficient | Proposed factor contract `RUB REAL` + `INFL|RUB|<bucket>`, ~3-month CPI lag (`TDM@a6ba30b:docs/ORG_CONVENTIONS.md:136`); requirements.csv C24 (R3, owner-approved contract or deferral); `FINLIB_AUDIT.md:288` | **P1** to collect (owner baseline) / **P2** to price |
| M19 | **Issuer spread moves by rating bucket** (derived) | EOD | 1 year | Derived from M1 `z_spread_bp`/`g_spread_bp` + S4 ratings | TDM@a6ba30b:`tdm/market.py:154-174`; `docs/SCENARIOS.md:160` | P1 (derived by TDM, needs M1 history) |
| M20 | Intraday bond bid/offer/last with freshness | Intraday | n/a | Per side timestamp, status; mid only from compatible fresh bid+offer (max age 900 s, max skew 60 s) | `CORE/implementation/quote-money/HANDOFF.md` (MDS PriceService rules); TDM `run_type=INTRADAY` exists but no intraday market dataset | P2 |

### 1.2 Static / reference data

| # | Data item | Granularity | History depth needed | Fields | Consumer (file:line) | Priority |
|---|---|---|---|---|---|---|
| S1 | **Bond terms** (`instruments`) — all bonds held at any time in the last year incl. matured; owner baseline: all MOEX bonds | Ref, effective-dated + known-dated (spec); TDM itself stores current version only | Every instrument alive ≥ 1 day in the year; never delete matured | 22 fields, see §2.3 | TDM@a6ba30b:`tdm/datasets.py:112-136`; `docs/DATA_REQUIREMENTS.md:275-306`; `PROG/EXECUTION_SPEC.md:50-52`; requirements.csv D06 | **P0** |
| S2 | **Cash-flow schedules** (`bond_cashflows`): coupons, amortizations, redemption, offers | Ref, versioned | Future flows for all held bonds; past flows of the current coupon period (accrual) | `instrument_id, date, type ∈ {coupon, amort, redemption, offer}, rate (%), amount (per bond, ccy)`; core needs resolved `coupon`/`principal`/`total` per payment date and quality labels (§2.2) | TDM@a6ba30b:`tdm/datasets.py:138-146`; core `public_schedule.py:22-99`; requirements.csv D07 | **P0** |
| S3 | **Floater formula models** | Ref, versioned, desk-approved | Life of the issue | benchmark, margin, averaging vs compounding, observation lag / lookback / lockout / shift, fixing lag, holiday carry, day count, rounding, cap/floor, payment lag, fallback; known full coupon vs benchmark fixing | `PROG/backlog.json` CORE-03 acceptance; `FIXING_AUDIT.md` "Acceptance for the unified calculation"; `MDS/docs/curve-engine-design.md:43-55`; `PROG/EXECUTION_SPEC.md:52,120` | **P1** (R2 pricing; collect now) |
| S4 | **Issuers** | Ref | Current + all referenced in the year | `issuer_id` (INN or LEI), `name`, `issuer_group`, `sector`, `country` (ISO-2), `is_gov`, `is_quasi` | TDM@a6ba30b:`tdm/datasets.py:84-93`; requirements.csv D03 (internal grouping overrides stay local) | **P1** |
| S5 | **Issuer ratings** | Series by assignment date | All changes in 1 year | `issuer_id, date, agency, rating` (national scale, no prefix) | TDM@a6ba30b:`tdm/datasets.py:95-102`; `tdm/market.py:144-151`; requirements.csv D04 | **P1** |
| S6 | **Curve definitions** (`curves`) | Ref | Once | `curve_id, name, ccy, kind (bond/swap), index_name, source, ord` | TDM@a6ba30b:`tdm/datasets.py:171-180` | **P0** |
| S7 | **Business-day calendars**: RF production calendar (desk), MOEX bond settlement, MOEX FX (CETS, joint-currency holidays), SPFI/swap calendar | Ref, versioned (version + fingerprint) | 1 year back, 1 year forward (TDM); longer forward for swaps/bonds | TDM: `date, is_business_day`. Core: `BusinessCalendar(holidays: frozenset[date], working_days: frozenset[date], version: str)` | TDM@a6ba30b:`tdm/datasets.py:182-186`; `tdm/market.py:21-88`; core `dates.py:32`, `docs/CONVENTIONS.md` "Business days" (library ships **no** holidays); `CORE/docs/BACKLOG.md` item 1 (P0) | **P0** |
| S8 | Public event notices (offers, calls, defaults, restructurings, coupon announcements) | Ref, event-driven | Life of the issue | Event type, dates, evidence | `PROG/EXECUTION_SPEC.md:52,114` | P1 |

---

## 2. Bond static data contract (what the core and TDM expect)

### 2.1 Core library input models — exact names and types

The core is pure Python; every input is an explicit frozen dataclass. These are the shapes the hub's static and market records must be able to populate (after local private merge at work).

**`SnapshotEvidence`** — `CORE/rub_fixed_income/local_worker.py:50`

| Field | Type | Rule |
|---|---|---|
| `snapshot_id` | `str` | non-empty |
| `source_id` | `str` | non-empty |
| `observed_at` | `datetime` | **offset-aware** (explicit UTC offset, `local_worker.py:45`) |
| `known_at` | `datetime` | offset-aware |
| `valid_until` | `datetime` | offset-aware; must satisfy `observed_at <= known_at <= as_of <= valid_until` (`:61`) |

**`PublicBondSnapshot`** — `local_worker.py:67` (one per instrument; holds no positions)

| Field | Type | Notes |
|---|---|---|
| `instrument_id` | `str` | MDS bridge compares it with payload `secid` (`public_schedule.py:41`) |
| `market` / `schedule` / `curve_evidence` | `SnapshotEvidence` | one evidence record each for quote, schedule, curve |
| `calendar_version` | `str` | required non-empty |
| `schedule_complete` | `bool` | must be `True` (upstream validator assertion) |
| `redemption_confirmed` | `bool` | must be `True` |
| `cashflows` | `tuple[BondCashflow, ...]` | resolved future flows (`BondCashflow(payment_date: date, amount: float)`, `cashflows.py:23`), RUB per bond, coupon + principal combined |
| `settlement_date` | `date` | explicit; "no machine-timezone or T+1 inference" (`:121`) |
| `horizon_date` | `date` | maturity or confirmed put date |
| `horizon` | `str` | `"maturity"` or `"confirmed_investor_put"` |
| `cashflow_currency` | `str` | only `"RUB"` accepted today |
| `coupon_model` | `str` | only `"fixed"` accepted today |
| `current_nominal_rub` | `float` | outstanding nominal per bond (after amortization) |
| `clean_price_pct` | `float` | % of **current** nominal |
| `accrued_interest_rub` | `float` | RUB per bond |
| `curve` | `DiscountCurve \| CurveBootstrapRequest` | DFs (`continuous_zero` basis) or raw OIS quotes (`ois_quote` basis) |
| `spread_convention` | `ZSpreadConvention(compounding: Compounding = ANNUAL, day_count: DayCount = ACT_365_FIXED)` | desk default annual ACT/365F |
| `spread_kind` | `Literal["credit","ofz"]` | OFZ → `ofz_spr01`, others → CS01 |
| `source_payload_id` | `str \| None` | SHA-256 of provider schedule payload |

**`BondTermsV1`** — `analytics.py:29` (snapshot analytics path, `analyze_bond[_v2]`): `issue_id: str`, `current_nominal_rub: float` (> 0), `maturity_date: date`, `horizon: Literal["maturity","confirmed_investor_put"]`, `horizon_date: date` (= maturity, or < maturity for put), `accrual_period_start: date`, `accrual_period_end: date`, `coupon_amount_rub: float` (≥ 0), `terms_hash: str`.

**`MarketObservationV1`** — `analytics.py:59`: `clean_price_pct_of_current_nominal: float` (> 0), `settlement_date: date`, `selected_price_basis: str` (label of which price was used — free text today), `market_hash: str`.

**`CurveSnapshotV1`** — `analytics.py:72`: `curve_world_instance_id: str`, `discount_curve: DiscountCurve`, `curve_hash: str`.

**`DiscountCurve`** — `discount.py:35`: `curve_date: date`, `nodes: tuple[tuple[date, float], ...]` (strictly increasing dates, positive finite DFs), `model_version: str = "ois-logdf-v1"`, `convention: CurveConvention`.
**`OisQuote`** — `discount.py:28`: `tenor: str`, `maturity: date`, `par_rate: float` (decimal).
**`CurveBootstrapRequest`** — `discount.py:157`: `curve_date: date`, `quotes: tuple[OisQuote, ...]`, `convention: CurveConvention`.
**`CurveConvention`** — `conventions.py:263`: `time_day_count: DayCount = ACT_365_FIXED`, `interpolation = "log-linear-df"`, `extrapolation = "flat-forward"`, `fixed_leg_schedule: ScheduleConvention | None = None`, `fixed_leg_day_count: DayCount = ACT_365_FIXED`.
**`KeyRateSwapQuote`** — `key_rate.py:96`: `tenor: str`, `bid/mid/ask: float`. **`KeyRateCurveBuildRequest`** — `key_rate.py:143`: `trade_date`, `current_rate`, `next_policy_effective_date`, `quotes`, `discount_curve`, `calendar`, bounds.

**Conventions enums** (`conventions.py`): `DayCount ∈ {ACT_365_FIXED, ACT_360, ACT_ACT_ISDA}` (ACT/ACT-ICMA **not implemented**, BACKLOG item 8); `Compounding(periods_per_year)` with `ANNUAL, SEMI_ANNUAL, QUARTERLY, MONTHLY, CONTINUOUS`; `BusinessDayConvention ∈ {UNADJUSTED, FOLLOWING, MODIFIED_FOLLOWING, PRECEDING}`; `ScheduleConvention(frequency_months=12, roll="forward"|"backward", business_day_convention, calendar)`; `SettlementConvention(lag_business_days=1, calendar)`.

**`BusinessCalendar`** — `dates.py:32`: `holidays: frozenset[date]`, `working_days: frozenset[date]` (transferred working weekends), `version: str`. Identity = version + content fingerprint (`calendar_fingerprint`).

Private (not from the hub, for context): `LocalPosition(position_id, instrument_id, quantity, recovery_rate)` (`local_worker.py:138`).

**Hard eligibility gates today** (`local_worker.py:110-117`, `public_schedule.py:35-94`): RUB cash flows only, fixed coupons only, complete schedule, confirmed redemption, horizon maturity or confirmed investor put, positive terminal principal, strictly increasing dates, no projected coupons. Anything else is reported as a coverage failure — never silently priced.

### 2.2 Schedule payload the core already consumes (market-data-store response)

`bind_market_data_store_schedule(terms, payload, financing_curve_build_id)` (`CORE/rub_fixed_income/public_schedule.py:22`) takes the response of the existing `FixedIncomeAnalyticsService.get_projected_bond_cashflows` and requires these keys:

| Key | Requirement |
|---|---|
| `secid` | equals `instrument_id` |
| `settlement_date`, `horizon_date` | ISO strings equal to the terms |
| `horizon_type` | `"maturity"` or `"offer"` |
| `formula_type` | `"fixed"` or `"fixed_rate"` |
| `calculation_quality` | `"exact_published"` and `warnings == []` |
| `projection_curve` | `None` |
| `financing_curve_build_id` | `int > 0`, equals supplied curve provenance |
| `cashflows[]` | rows: `payment_date` (ISO), `coupon`, `principal`, `total` (≥ 0, `coupon + principal == total`), `coupon_source ∈ {None, "published_fixed"}` (positive coupon must be `published_fixed`) |

This is the one concrete machine contract already in place between collectors and core; the hub should either preserve this response shape or provide an equivalent normalized schedule with the same quality labels.

### 2.3 TDM `instruments` / `bond_cashflows` contract 1.3

`TDM@a6ba30b:contract/csv/instruments.csv`, `tdm/datasets.py:112-136`, `docs/DATA_REQUIREMENTS.md:285-306`:

| Field | Type | Req. | Meaning |
|---|---|:-:|---|
| `instrument_id` | text | ● | stable key, ISIN preferred; same key in positions, quotes, results |
| `isin` | text | | ISIN |
| `name` | text | ● | display name |
| `issuer_id` | text | ● | → issuers |
| `asset_class` | text | ● | ОФЗ-ПД, ОФЗ-ПК, Корп RUB фикс, Корп RUB флоатер, Корп валютные, Суверен валютный, Секьюритизация |
| `segment` | text | ● | Гос / Корп / Секьюритизация |
| `ccy` | text | ● | nominal currency RUB, USD, CNY, EUR, CHF |
| `face_value` | number | ● | nominal per bond, in ccy |
| `issue_date` | date | | |
| `maturity` | date | ● | |
| `coupon_type` | text | ● | `fix` / `float` |
| `coupon_rate` | number | | fixed coupon, % p.a. |
| `coupon_freq` | int | | coupons per year |
| `day_count` | text | | ACT/365, 30/360, … |
| `index_name` | text | | floater index (КС, RUONIA, …) |
| `margin` | number | | floater spread, % |
| `amortizing` | 1/0 | | |
| `offer_date` | date | | **nearest** put/call; yield, spread and term are computed to it while ahead |
| `is_mbs` | 1/0 | | ИЦБ/ABS |
| `collateral_eligible` | 1/0 | | Lombard list / CCP repo |
| `call_date` | date | | *1.3* nearest call (perpetual/subordinated) — JTD and extension risk |
| `is_subord` | 1/0 | | *1.3* subordinated |

`bond_cashflows`: `instrument_id, date, type ∈ {coupon, amort, redemption, offer}, rate (%), amount (per bond, ccy)` — key `(instrument_id, date, type)`.

### 2.4 Proposed hub static data contract (union of the above + program/audit requirements)

The hub must hold more than either consumer's flat view so that both can be derived. Minimum per-issue record (all effective-dated with `effective_from/to`, `known_from/to`, `source`, `revision`, `quality_state` — `PROG/EXECUTION_SPEC.md:50`):

- **Identity**: `secid`, `isin`, board(s) and primary board, aliases, `issuer_id` (INN/LEI/OGRN), registration number, replacement/exchange links. ISIN is **not** unique across SECIDs (1 duplicate found, `FINLIB_AUDIT.md`, "Есть один дублирующийся ISIN").
- **Classification**: TDM `asset_class`, `segment`, `is_mbs`, `is_subord`, `collateral_eligible`, sovereign/quasi flags, OFZ series (26xxx PD, 29xxx PK, 52xxx IN, 24xxx/25xxx, etc.).
- **Currencies**: `nominal_currency`, `settlement_currency`, `payment_currency` (and FX conversion rule) — required separately; 206 traded MOEX issues have `CURRENCYID=RUB` while `FACEUNIT` is CHF/CNY/EUR/USD (`FINLIB_AUDIT.md:99`).
- **Nominal**: initial face, current outstanding face (per date), issue volume / outstanding amount.
- **Dates**: issue, maturity (nullable for perpetuals — 62 traded issues have none), placement, `0000-00-00` normalized to NULL.
- **Coupon schedule rows**: period start, period end, record date if available, payment date, rate (% if known), amount per bond (if known), `coupon_source` (published / derived-from-rate / projected / unknown), known flag.
- **Amortization rows**: date, principal amount per bond or % of initial face.
- **Offers / calls**: date, type (put/call), price, exercise window, confirmed vs indicative (core needs a *confirmed* investor put as horizon), and the "nearest offer" view TDM uses.
- **Coupon formula** (floaters): see S3; benchmark code; family (`arithmetic_key_rate`, `arithmetic_ruonia`, compounded RUONIA, `ruonia_indexed_principal_fixed_coupon`, `gcurve_5y`/`constant_maturity_curve_coupon`, …); status (curated / discovery-only).
- **Linkers**: CPI base, indexation lag, indexed nominal series, floor at par.
- **Conventions**: day count (ACT/365F, ACT/360, ACT/ACT ISDA, 30/360; flag ICMA as unsupported), coupon frequency, business-day convention, settlement lag and calendar ID/version, accrued rounding (MOEX: 0.01 RUB, `analytics.py` `CalculationPolicy.money_quantum`).
- **Quality labels per issue**: schedule complete? redemption confirmed? formula valuation-ready? These map 1:1 to the core gates in §2.1.

> **Existing collector model** (bond catalog tables, formula types, MOEX/Cbonds sources) is summarised in Appendix A from the vendored MDS/MCP code.

---

## 3. Market data contract — how the backend expects to receive data

### 3.1 Transport, today and target

| Layer | Today (TDM contract 1.3) | Target (backend program) |
|---|---|---|
| Ingest into TDM | **CSV files** dropped into a folder; `python -m tdm load <dir>`; file name prefix selects dataset (`md_fx_2025.csv`, `positions_2026-10-13.csv`); `;` or `,` separator, decimal comma allowed with `;`, UTF-8 or cp1251; one transaction per file; upsert by key; refs load before series (TDM@a6ba30b:`docs/DATA_REQUIREMENTS.md` §3) | Work downloader fetches **broad immutable bundles by date, cut and universe** from the VPS, validates manifest/checksums, caches locally, merges private overrides with explicit precedence (`PROG/EXECUTION_SPEC.md:30,132`; ADR03 in `PROG/DECISIONS.md`) |
| Storage | SQLite tables `md_curve_point`, `md_fx`, `md_fx_cbr`, `md_fixing`, `md_bond_quote`, `md_cds_quote`, `ref_*` (`tdm/datasets.py`) | VPS: PostgreSQL + immutable raw archive + published snapshot bundles (ADR02) |
| Read API | `GET /api/v1/market/curves|fx|fx-cbr|fixings|bonds|issuer-spreads|cds`, `/bonds/history`, `/coverage` on the local `tdm` server (TDM@a6ba30b:`docs/SERVICE_DATA_MODEL.md:171-203`) | VPS `/v1/snapshots`, `/v1/snapshots/{id}/manifest`, `/v1/snapshots/{id}/bundle`, `/v1/instruments[/{id}]`, `/v1/quotes`, `/v1/curves`, `/v1/fx`, `/v1/fixings`, `/v1/coverage`, `/health/live`, `/health/ready`; OpenAPI, typed client, pagination, request IDs, conditional downloads; bearer tokens, never in URLs (`PROG/EXECUTION_SPEC.md:90-108`) |
| Schema location | `contract/csv/*.csv` templates + `contract/bundle.schema.json` (bundle of **calc results**, not market input) | New versioned schemas under `contract/market` and `contract/private` (`PROG/EXECUTION_SPEC.md:172`) — **not yet created** |
| Into the core | n/a | Local adapter builds `PublicBondSnapshot` / `CurveBootstrapRequest` / `KeyRateCurveBuildRequest` from bundle records (§2.1) |

There is **no JSON schema for market input** in `contract/` today; `bundle.schema.json` describes calculation-run output whose `curves[].points` must have exactly 11 buckets, `fx[]` has `rate, move_pct, rate_cbr, move_pct_cbr, rate_cbr_next`, `indices[]` has `index_name, value`. The hub's bundle format is therefore a new contract to be designed (DESIGN-01 / API-01 in backlog.json), with a TDM-CSV export view for backward compatibility.

Hard perimeter rule: the work client must not send holdings, notionals, books or missing-position diagnostics; per-instrument lookup exists for research but the work integration downloads the broad universe (`PROG/EXECUTION_SPEC.md:30`; requirements.csv S11). This is why the owner's "all MOEX bonds" universe is also a privacy requirement.

### 3.2 Time zones, cuts and dates

- Desk calendar: **RF production calendar**; desk EOD cut **~19:00** (`TDM@a6ba30b:docs/ORG_CONVENTIONS.md:145-146`). Implied time zone Europe/Moscow (the program pack is dated "Europe/Moscow", `PROG/README.md`).
- TDM dates are `YYYY-MM-DD` **market business days**; positions and market data must share dates (`docs/DATA_REQUIREMENTS.md` rule 4). `md_fx_cbr` may contain weekend rows (`docs/SERVICE_DATA_MODEL.md:203`).
- Core requires **offset-aware timestamps** on all evidence and explicit `settlement_date` (no T+1 inference) (`local_worker.py:45,121`).
- Each published snapshot manifest must carry **cut and timezone**, source and term revisions, curve model versions, checksums, counts, coverage and stale flags, superseded ID (`PROG/EXECUTION_SPEC.md:62`).
- Settlement T+1 must use an official calendar, never weekday-only or calendar+1 (`CORE/docs/CONVENTIONS.md` "Business days"; BACKLOG item 5). CETS TOM `SETTLEDATE` must be loaded, not computed (`MDS/docs/currency-market-data-plan.md:52`).

### 3.3 As-of / bitemporal semantics

- Every normalized record: instrument/series ID, source, **observation time, effective date, publication/known time, ingestion time**, units, currency, convention identity, quality state, record revision (`PROG/EXECUTION_SPEC.md:50`). Static terms: effective-from/to and known-from/to; corrections append revisions; unknown history is marked unknown, never guessed.
- **CBR FX**: distinguish announcement time, effective date, quote nominal; normalize to RUB per 1 unit; the "next" rate must be the expected effective observation **known at the run cut** (`PROG/EXECUTION_SPEC.md:58`; BASELINE_REVIEW B08 — current `fx_cbr_next` picks the first future row, `tdm/market.py:99`). Official PnL uses the rate **effective** on T (set on T−1; Monday uses Friday's) (`docs/ORG_CONVENTIONS.md:101`); OCP transfer uses the rate set today for tomorrow (DEC06).
- **Overnight fixings**: distinguish observation date, publication lag and accrual applicability; weekend applicability must not conceal a missing fixing (`PROG/EXECUTION_SPEC.md:58`). TDM accrues funding **per calendar day** using the value in force each day (`tdm/market.py:118-130`), while key rate is stored **by effective date** (`docs/DATA_REQUIREMENTS.md:555-562`).
- **Historical selection by known-at**, not fixing date; immutable fixing snapshots bound to calculations (HIST-01 P0, from `FIXING_AUDIT.md`).
- **Missing ≠ zero ≠ stale**: no forward-fill (TDM rules 6 and 10, `docs/DATA_REQUIREMENTS.md:72,124`); never publish missing factors as zero or stale as current (DATA-01 acceptance).
- **One source per series for the whole history**; switching creates false historical shocks (`docs/DATA_REQUIREMENTS.md:100-103`). Multiple providers may coexist but are never spliced without labels (`PROG/EXECUTION_SPEC.md:56`).
- Vendor observations and computed analytics (e.g. Z-spread) stored as **distinct fields** with term/curve/model versions (`PROG/EXECUTION_SPEC.md:11`).

### 3.4 Quote conventions

| Topic | Requirement | Source |
|---|---|---|
| Units | rates/yields/coupons in **%** (TDM) — core uses **decimals**; spreads **bp**; bond prices **% of nominal** (core: % of *current* nominal); FX **RUB per 1 unit**; amounts in instrument ccy unless `_rub` | TDM rule 5 (`docs/DATA_REQUIREMENTS.md:109-115`); `CORE/docs/API.md` header |
| Clean vs dirty | Quotes are **clean**; accrued separate (TDM: % of nominal; core: RUB per bond); dirty = clean×nominal/100 + AI | TDM rule 3 (`:69`); core `cashflows.py` `clean_price_pct_to_dirty_rub` |
| Accrued | Linear in period; **0 on coupon payment date** (MOEX); rounded half-up to 0.01 RUB | `CORE/docs/CONVENTIONS.md` "Accrued interest" |
| Bid/ask/mid | Keep bid / ask / last / valuation (recognised) price identities; mid only from same-source, same-currency, fresh bid and offer with bid ≤ offer and ≤ 60 s skew; stale offer is never masked by a fresh bid | `PROG/EXECUTION_SPEC.md:54`; `CORE/implementation/quote-money/HANDOFF.md` |
| YTM basis | To nearest offer while it lies ahead, else maturity — **same basis** in quotes and calculations | TDM rule 1 (`:67`); `DATA_CONTRACT.md:374,390` (`yield_date`) |
| Yield compounding | Core default nominal annual ACT/365F; `analyze_bond` V1 effective annual | `CORE/docs/CONVENTIONS.md` "Yield" |
| Spread type | `z_spread_bp` same type as calc `spread_type` (`Z` fixed, `DM` floaters; also `G, I, OAS, ASW`); RUB corporates Z/CS01 **to RUONIA**; OFZ = RUONIA + per-bond OFZ spread (`ofz_spr01`); G-spread to KBD | TDM rule 2 (`:68`); `DATA_CONTRACT.md:71,391`; `ORG_CONVENTIONS.md:121` |
| Z-spread convention | Desk default **annual, ACT/365F**; monthly and continuous differ materially (OFZ 26238: 139.1 / 124.2 / 122.9 bp) — store convention identifier (`zspread:annual:ACT/365F`) next to every spread | `CORE/docs/CONVENTIONS.md` "Z-spread" |
| Curves | One rate type (zero or par) and one method per curve for all dates; nodes stable, 1M–20Y; raw instruments archived with representation, compounding, day count, maturity, projection index, collateral, bootstrap/interp config | TDM rule 7; `PROG/EXECUTION_SPEC.md:56` |
| OIS/KRS conventions | RUONIA OIS: TOM, MODFOL, short initial stub, payment lag +1B (per MOEX methodology as read by the audit); KRS: TOM/MODFOL/ACT/ACT ISDA, annual fixed vs quarterly key-rate average; core OIS bootstrap default is legacy ACT/365F unadjusted annual | `FINLIB_AUDIT.md:169-175`; `MDS/docs/curve-engine-design.md:9-17`; `CORE/docs/CONVENTIONS.md` "OIS bootstrap" |
| FX futures | `RUB per FX = raw × (STEPPRICE/MINSTEP)/LOTVOLUME`; perpetual futures are `perpetual_proxy`, never spot | `MDS/docs/currency-market-data-plan.md:40-74` |
| FX source in TDM | Two series: market (`md_fx`, ~19:00, internal) and CBR (`md_fx_cbr`); USD and EUR have not traded on MOEX since June 2024 — "MOEX TOM close" is unusable for them | `docs/DATA_REQUIREMENTS.md:101` |

---

## 4. Owner's baseline universe merged with backend needs

The baseline below must be covered **regardless** of current TDM/core consumption. "Need" shows which consumer requires it today.

| Owner baseline item | Concrete series / instruments | Need today | Granularity & history | Candidate source (verify rights) | Known gaps |
|---|---|---|---|---|---|
| **All Russian bonds on MOEX** (static) | All issues on MOEX bond boards (TQOB, TQCB, TQIR, TQOD, TQOE, TQOY, TQUD, …) incl. OFZ-PD/PK/IN, municipal, corporate, currency (USD/CNY/EUR/CHF faceunit), structured, ИЦБ, perpetual/subordinated; terms, schedules, amortizations, offers, formulas | TDM instruments + bond_cashflows (S1/S2); core PublicBondSnapshot; perimeter rule (broad universe) | Ref, effective/known-dated; history of revisions | MOEX ISS `/securities`, bondization (coupons/amortizations/offers); Cbonds enrichment; issuer documents | Existing catalog: 16 023 issues / 4 025 traded; only 61% of traded have a buildable schedule; 42 441 of 68 348 future coupons have unknown amounts; formulas often missing lag/rounding (`FINLIB_AUDIT.md` "Полнота графиков"; `curve-engine-design.md:49-55`) |
| **All Russian bonds on MOEX** (market) | EOD clean price (close, waprice, recognised/valuation price), bid/offer/last, accrued, YTM (and its basis), MOEX duration, volume, board; optional intraday | TDM `md_bond_quotes` (M1) | EOD daily, 1y backfill | MOEX ISS history/marketdata; AlgoPack; NFA/BVAL/Cbonds as alternatives | MDS `price_observations` empty in the audited DB — no EOD archive exists yet (`FINLIB_AUDIT.md:187`) |
| **FX vs RUB spot TOD/TOM** | CETS `*_TOD`/`*_TOM` for every traded currency (CNY, HKD, BYN, KZT, …); USD/EUR/GBP/CHF are not exchange-traded since sanctions | TDM `md_fx` candidate for CNY; USD/EUR/CHF need another source | Intraday + EOD close/WAP; 1y | MOEX ISS/AlgoPack/Passport realtime (CNYRUB_TOM has realtime path); NTPro/Ratescalcs for USD cash | USD/EUR spot only as OTC or `perpetual_proxy` (`currency-market-data-plan.md:40-58`); CHF absent |
| **CBR official rates** | All currencies published by CBR (daily), with set date, effective date, nominal | TDM `md_fx_cbr` (USD, EUR, CNY, CHF required) | Fixing by effective date; every calendar day; 1y+ | CBR XML daily (existing `cbr.py` in MDS/MCP) | Need announcement time and known-at; per-unit normalization |
| **Main G7 crosses** | EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD (+ EURGBP, EURJPY, EURCHF as derived crosses); also USDCNY for CNY bonds | Not consumed by TDM today; useful for FX shocks, currency bonds, implied RUB crosses | EOD (and fixing) daily, 1y | ECB reference rates (daily, free), CBR cross-implied, or licensed feed | No source identified in existing code — open question |
| **MOEX FX futures — dated** | Si (USD/RUB), Eu (EUR/RUB), CNY (CNY/RUB); first three live expiries; optionally ED (EUR/USD) and other FX futures on FORTS | Owner baseline; implied rates and spot proxies | Intraday 5-min + EOD settlement; contract static (last trade, expiry, step price, lot) | MOEX AlgoPack FO OBStats; RFUD static (hub update 2026-10-16: AlgoPack FO **tradestats** + FUTOI, rolling 90 days; OBStats/order-book mid is **not** collected any more — dropped in migration 0423; intraday prices from APIM realtime marketdata) | Fixing/effective date contract unreconciled; implied-rate publication blocked (`currency-market-data-plan.md:72-74`) |
| **MOEX FX futures — perpetual** | USDRUBF, EURRUBF, CNYRUBF (+ funding rate series on `FUSR`) | Spot proxies (labelled) | Intraday + EOD | AlgoPack | Funding series must not be treated as OIS or zero when missing (`:58`) |
| **IRS vs RUONIA (OIS)** | RUONIA OIS par quotes 1W…10Y+ (bid/mid/ask), conventions; built `RUB RUONIA` | Core `OisQuote` / `CurveBootstrapRequest` (M3); TDM `RUB RUONIA` | EOD (cut-aligned) + intraday; 1y+ (archive since Jul 2022 exists) | Ratescalcs/Rubik (existing), MOEX SPFI `RUB-OIS-RUONIA`-type curves, NFA | Reconcile day count / schedule conventions with MOEX spec (open Q) |
| **IRS vs key rate** | KRS quotes (13-quote standard set to 10Y, bid/mid/ask); built `key_rate_projection` / `RUB KEY` | Core `KeyRateSwapQuote` (M4); TDM `RUB KEY`; carry `implied` scenario | EOD + intraday; 1y | Ratescalcs (existing) | — |
| **XCCY vs CNY** | CNY/RUB cross-currency swaps vs RUONIA (`CNY-XCS-CNYRUO`, `CNY-XCS-CNRQTE`), FX swap points `SPC-FXS-CNRQTE` | TDM `CNY` curve; requirements C08 (R2) | EOD; 1y | MOEX SPFI (entitlement needed), existing OTC monitor `cnyruonia_xccy` | SPFI values not entitled; XCCY snapshots up to 12.4 h old — need explicit freshness policy (`FINLIB_AUDIT.md:251-257`). Hub update 2026-10-16: live loader `swaps_otc_monitor` continues the legacy `otc_monitor` / `cny_ruonia_xccy` history (from 2026-09-22) every 15 min; Andromeda export history (mid-only EOD, 1W–5Y) from 2023-07-10 imported as source `andromeda` |
| **OIS CNY vs RUSFAR CNY** | `CNY-OIS-RUSFCN` (calc), `CNY-OIS-RFCQTE` (best quotes), forwards `CNY-FWC/FWA-RUSFCN` | TDM `CNY` curve; DERIVS CNY funding | EOD; 1y | MOEX SPFI; **OTC Monitor `OIS RUSFARCNY` (hub update 2026-10-16)** | SPFI not entitled; no substitute may be inserted (`currency-market-data-plan.md:88-93`); onshore FR007 world exists only as approximate (`:116-118`). **Hub update 2026-10-16:** history 2023-12-12 → 2026-10-16 (12 tenors 1W–5Y, mid-only EOD) imported from the owner's Andromeda export (`mdhub legacy andromeda-cny`, source `andromeda`), so the 1-year depth is covered; live quotes are now collected from OTC Monitor (`md.curve_snapshot` source `otc_monitor`, dataset `cny_rusfar_ois`, 8 tenors 1W–1Y, bid/offer on 6M/9M/1Y, mid-only on the rest, every 15 min) — OTC Monitor itself has no history endpoint; dealer/vendor quote sets, not the SPFI calculated curve |
| **Key rate** | CBR key rate by effective date + decision date; meeting calendar | M6, M7 | Fixing; full history since 2013 recommended | CBR | — |
| **RUONIA** | RUONIA O/N, RUONIA index, term RUONIA 1M/3M/6M, publication times | M8 | Fixing; ≥ 1y, ideally since 2014 for floaters | CBR | No silent carry across missing weekdays (P0) |
| **RUSFAR RUB** | O/N, 1W, 2W, 1M, 3M | M9 | Fixing; 1y+ | MOEX | — |
| **RUSFAR CNY** | O/N (+ terms if published) | M10 | Fixing; 1y+ | MOEX | — |
| **CPI for OFZ-IN** | Rosstat monthly CPI (index level, m/m), publication date; per OFZ-IN indexed nominal | M18 | Monthly; back to base month of oldest live OFZ-IN | Rosstat; MinFin/MOEX indexed nominal | No pricing model in core (R3); collect now to avoid look-ahead |
| **ZCYC / G-curve** | MOEX KBD parameters (B1–B3, T1, G1–G9) and standard yields | M5 (`RUB OFZ`, G-spread, OFZ-PK constant-maturity coupons) | EOD (+ intraday); 1y+ | MOEX ISS `zcyc` | — |

**Backend needs not in the owner's list (also required):** SOFR, €STR, SARON fixings (M11); USD SOFR / EUR €STR / CHF SARON curves (M14, R2 — only proxies today); CHF FX (market and CBR); CBR meeting calendar (M7); official business calendars (S7); issuers and ratings (S4, S5); CDS (M17, only if licensed).

---

## 5. Open questions and ambiguities

1. **Contract version.** Local TDM checkout is contract 1.2 (`10b9b73`), the program targets 1.3 (`a6ba30b`). Confirm the hub targets 1.3 (and later) and that the work team will update the checkout.
2. **Who owns the market FX mark?** TDM `md_fx` is an *internal* ~19:00 mark supplied from inside the organisation (DEC04 open). Is the hub's FX series only a labelled candidate/fallback, or expected to reproduce the internal cut? Which provider for USD/EUR/CHF given no MOEX cash market (OTC NTPro? perpetual futures proxy? CBR?).
3. **"All FX pairs vs RUB spot TOD/TOM"**: on MOEX only a subset of currencies trades (USD/EUR/GBP/CHF/JPY do not). Should non-traded pairs be filled from OTC, futures proxies or CBR — and labelled how?
4. **G7 crosses source** (ECB reference, CBR-implied, licensed) and whether intraday is needed.
5. **Curve representation for TDM `md_curves`**: zero vs par per curve, and which built curve the hub publishes for `RUB OFZ` (KBD zero? RUONIA + OFZ spread per ORG_CONVENTIONS §4?). TDM interpolates linearly on rates; core interpolates log-linear DFs — two "same" curves can disagree between pillars.
6. **RUONIA OIS conventions**: core default (ACT/365F, unadjusted annual fixed leg) vs MOEX methodology (TOM, MODFOL, stub, +1B payment lag; audit cites ACT/ACT ISDA) vs MDS design note (ACT/365). Needs a single documented convention per quote source (BACKLOG item 9; DEC08).
7. **Accrued interest units**: `md_bond_quotes.accrued` is % of nominal, `positions.accrued` is in currency, core wants RUB per bond. Hub should publish AI per bond in payment currency **and** % of current nominal with nominal.
8. **Which bond price is the mark** (MOEX close, WAP, last, recognised/«признаваемая», bid, NFA/BVAL)? `selected_price_basis` is free text in core; DEC04 requires quote type, timezone, stale tolerance and provider precedence per series.
9. **YTM / spread conventions in quotes**: TDM requires yield to nearest offer and the same Z type as the calc engine, but does not specify compounding (MOEX YIELD is effective annual; core defaults differ) or Z curve (RUONIA vs KBD). If the hub computes Z/G spreads on the VPS (allowed as public unit analytics, `EXECUTION_SPEC.md:11`), the convention must be fixed first.
10. **Fixing storage semantics**: TDM `fixings_by_day` carries the last value per calendar day (correct for key rate, wrong for missing RUONIA weekdays). Hub must publish observation date, publication time and an explicit "permitted carry" flag/calendar.
11. **Static history vs current version**: TDM upserts current `instruments` and expects the operator to roll `offer_date` after it passes; the program wants effective/known-dated terms. Hub should keep bitemporal history and offer a "current as of cut" projection for TDM.
12. **Perpetuals and missing maturity**: TDM makes `maturity` mandatory; 62 traded issues have none. How to export them (call_date as horizon? exclude?).
13. **Floater/linker approval**: formula interpretation needs desk-owner acceptance before affecting prices (`EXECUTION_SPEC.md:120`). Who curates S3 for the full MOEX universe, and what is "valuation-ready"?
14. **Currency bonds**: core accepts RUB only; TDM has a single `ccy`. Confirm nominal/settlement/payment currency fields and the FX rule (rate, date, source) for coupon conversion.
15. **History depth**: TDM requires 1 year; HIST-01 adds "longer stress history as optional separately entitled source" (2022 window used as manual preset). Confirm backfill depth per series (e.g. full history for fixings/key rate, 1y for quotes, since 2022 for curves).
16. **Cut alignment for intraday-sourced curves** (RUONIA OIS, KRS, XCCY): which snapshot represents EOD (last before 19:00? official MOEX close?) and maximum staleness (existing OTC parser rejects > 3 h except XCCY).
17. **Data rights**: MOEX non-display/redistribution, SPFI entitlement (CNY curves), Cbonds terms, ratings agencies — required in the Source Registry before building most collectors (`EXECUTION_SPEC.md:60`).
18. **Calendars**: RF production calendar (desk), MOEX trading/settlement, CETS joint-currency holidays, SPFI swap calendar — which IDs/versions are published and how the core's `BusinessCalendar.version` is named.
19. **Universe size and bundle format**: TDM sizing assumed ~30–300 quoted bonds; the owner universe is ~4k traded / 16k issues — confirm bundle partitioning (static vs daily market) and delivery size over the corporate proxy.
20. **Identity**: `instrument_id` "ISIN preferred" vs SECID; duplicate ISIN across SECIDs; board selection for price — define the canonical key and alias table.

---

## Appendix A — Existing collector static model (market-data-store / moex-market-data-mcp)

This appendix describes how the existing Hermes collectors store data today, read from the copies vendored into the core repo. It is the starting point for the hub's static contract, not a target design. A subagent extracted it from source; I spot-checked the key citations (issues/formula_models DDL, `index_fixings`, `calculation_quality`). Paths: `MCP/src/moex_market_data/`, `MDS/src/market_data_store/`.

**Bond catalog** (one SQLite file `bond_catalog.sqlite3`, schema at `MCP/.../catalog.py:303-505`):

| Table | Purpose |
|---|---|
| `issues` (PK `secid`) | Identity, issuer, type, face and size, currency, dates, coupon, floater, option and CBR-registry fields; scope classification; `raw_json` |
| `issue_revisions` | Append-only, written on semantic hash change |
| `boards` | Board per security, with `is_primary` |
| `events` | `event_type ∈ {coupon, amortization, offer}`, `event_date`, `ordinal`, `value`, `rate_pct`, `currency`, `raw_json` |
| `event_revisions` | Append-only revisions of `events` |
| `formula_models` | `model_type`, `status`, `annual_rate_pct`, `benchmark`, `spread_pp`, `missing_terms_json`, `model_json` (curated formula), `llm_used` |
| `ratings` | Never written |
| `source_availability` | Per-security source status by field group |
| `conflicts` | Conflicting values; also stores legal overrides such as `amortization_value` |
| `issuers` | Issuer identity: INN, OKPO, industry |

Columns of `issues`, by group:
- **Identity and issuer:** `isin`, `regnumber`, `emitent_*`, `primary_boardid`, `marketprice_boardid`.
- **Type:** `bond_type`, `bond_subtype`.
- **Face and size:** `facevalue`, `initial_facevalue`, `issue_size[_placed]`.
- **Currency:** `faceunit`, `currency`.
- **Dates:** `issue_date`, `maturity_date`.
- **Coupon:** `coupon_period_days`, `coupon_frequency`, `coupon_percent`, `coupon_value`, `next_coupon_date`, `accrued_interest`.
- **Floater:** `coupon_benchmark`, `coupon_benchmark_spread`.
- **Options:** `offer_date`, `call_option_date`, `put_option_date`.
- **CBR registry:** `cfi`, `issuer_ogrn`, `currency_cbr`, …
- **Scope classification:** `instrument_class`, `scope_status`.

Cbonds enrichment tables (`MCP/.../cbonds_enrichment.py:10-83`):
- `cbonds_issue_observations` is append-only.
- `cbonds_issue_enrichment` holds reference rate, margin, **floor, cap, day count**, coupon and principal structure, and `*_resolved` values with their `*_source`. Local values win.
- `cbonds_cashflow_observations` and `cbonds_offer_observations` hold Cbonds cash flows and offers.
- The market-data-store projector does not read any of these tables.

**Formula vocabulary**
- Inferred model types: `fixed_rate`, `known_coupon_schedule`, `benchmark_plus_spread`, `indexed_principal_unresolved`, `unclassified`.
- Curated model types: `fixed`, `arithmetic_ruonia`, `compounded_index_ratio`, `term_key_rate`, `gcurve_5y`, `constant_maturity_curve_coupon`, `arithmetic_key_rate`, `ruonia_indexed_principal_fixed_coupon`, `refuse_exact`.
- Status values: `preliminary_moex`, `preliminary_bondization`, `partial_missing_conventions`, `unavailable`, `exact_curated`, `refused_exact`, `fallback_unverified`, `partial_public_formula`.
- Benchmark codes: `RREFKEYR` (key rate); `RUONIA`, `RUREF`, `RUONIA_INDEX`; `ZR_YLD_CRV` and `MOEX_GCURVE` (G-curve).
- Formula parameters live inside `model_json`:
  - Rate and spread: `annual_rate_pct`, `spread_pp`, `margin_steps`.
  - Lags: `lag_calendar_days`, `fixing_lag_calendar_days`, `fixing_*_days_before_start`, `index_lag_calendar_days`.
  - Day count: `day_count ∈ {ACT/365, ACT/ACT-year, 30/360}`.
  - Rounding: `rate_decimals`, `amount_decimals`, `nominal_rounding_decimals`.
  - Calendar: `calendar_holidays`, `calendar_working_weekends`.
  - Overrides: `legal_overrides`.
- Cap, floor and multiplier are **not** in formula_models.
- The RUONIA and key-rate projector ignores lag and rounding. Its coupon sources say so in the name (`*_proxy_missing_lag_rounding`).

**Coverage gaps relevant to the owner's baseline**
- No CPI or OFZ-IN model or table. Linkers are only text-matched to `indexed_principal_unresolved`.
- OFZ-PK: only SU29015, SU29027 and SU29007 have curated formulas. The rest are benchmark + spread with conventions missing.
- Issuer calls are not modelled.
- No payment-currency field: the projector assumes RUB, and the catalog has only `faceunit`/`currency`.
- No calendar table.
- `index_fixings` covers only `ruonia` and `key_rate` (`MDS/.../storage.py:107-114`). `term_ruonia_fixings` stores the RUONIA index and 1M/3M/6M averages.
- RUSFAR, RUSFAR CNY, SOFR, €STR, SARON and CPI are not collected into tables.

**Sources and refresh**
- MOEX ISS:
  - `/securities` (stock_bonds) for the directory.
  - `/engines/stock/markets/bonds/securities` for board static.
  - The `stock_bonds_all` collection for descriptions (benchmark, spread, initial face).
  - `/securities/{SECID}` as a per-security fallback.
  - `bondization` for coupons, amortizations and offers.
- CBR securities registry ZIP, hard-coded to as-of 2026-06-01.
- Cbonds `ws2.cbonds.info` (get_emissions, get_flow_new, get_offert).
- Refresh: a weekly full catalog run (traded scope), plus a daily static recovery of blocked SECIDs through MOEX and then Cbonds.
- Time markers are only `first_seen_run` and `last_seen_run`, with UTC run IDs. Static terms have no effective-from or known-from dates.

**Market tables in market-data-store** (UTC `*_utc` text timestamps; migrations v1–16)
- Curve quotes:
  - `curve_snapshots`: `source_observed_at_utc`, `fetched_at_utc`, `published_at_utc`, `quote_basis`, `correction_state`, `supersedes_snapshot_uid`.
  - `curve_quotes`: `tenor`, `bid_pct`, `ask_pct`, `mid_pct` for RUONIA OIS and KRS.
- Curve builds: `derived_curve_builds`/`nodes` (`ruonia_discount`, `key_rate_projection`), and the G-curve `gcurve_snapshots`/`builds`/`nodes`.
- Point-in-time selection: `curve_world_definitions`/`instances` record `decision_time_utc` and `knowledge_policy ∈ {as_known_then, retrospective_backfill}`.
- `normalized_base_curve_*` holds USD/EUR/CNY, mostly proxies.
- `price_observations`:
  - Columns: `observed_on`, `basis`, `revision_number`, `supersedes_observation_id`.
  - `basis` is either LEGALCLOSEPRICE / CLOSE / WAPRICE / MARKETPRICE3, or `BID|OFFER|LAST:{realtime|delayed}` (MOEX SYSTIME read as Europe/Moscow, converted to UTC).
  - The table is empty in the audited DB.
- `intraday_quote_observations` stores FX spot, perpetual and dated futures with value/fixing/last-trade/expiry dates, bid/offer/last as decimal text and latency class.
- `policy_meetings` holds CBR meetings with `is_estimated`.
- There is no `known_at` column anywhere. Knowledge time is approximated by fetched, published and decision times.

`get_projected_bond_cashflows` (`MDS/.../fixed_income.py:1749-2100`):
- Input `decision_time_utc` must be tz-aware.
- It returns the keys used by the core bridge (§2.2), plus `name`, `valuation_date`, `formula_status`, `allow_approximation`, `spread_definition`, `nominal_anchor[_reference_date]` and a typed `projection_curve` object.
- Cash-flow rows can also carry `coupon_rate_pct`, `projected_nominal`, `benchmark_rate_pct`, `benchmark_tenor_years` and `fixing_date`.
- Floaters fail unless `allow_approximation=True`.
- Values of `calculation_quality`: `exact_published`, `approximate_ruonia_indexed_principal`, `approximate_forward_gcurve_no_convexity`, `approximate`.
