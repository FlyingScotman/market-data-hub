# System context: where the hub fits

As of 16 October 2026. market-data-hub is the **market and static data service** of the Trading Desk Monitor, a RUB fixed-income desk analytics system.

The full system architecture lives in the sibling repository **trading_desk_monitor**, folder `docs/architecture/`. With the usual side-by-side checkout it is at [`../../trading_desk_monitor/docs/architecture/README.md`](../../trading_desk_monitor/docs/architecture/README.md). Read its README first: it holds the component map, the interface catalogue and the reading order. These relative links resolve only in the local checkout. This file summarises what the system expects from the hub. It does not change the hub's own design: [DESIGN.md](DESIGN.md) remains authoritative for internals.

## Components

| Component | Repository | Relation to the hub |
|---|---|---|
| Desk backend + frontend | `trading_desk_monitor` | **The only consumer.** It reads immutable snapshots, translates them into core domain objects with its own adapter, and proxies market views to the UI |
| Calculation core | `rub-fixed-income-core` | **No dependency in either direction.** Core never parses hub payloads; the hub never imports core |
| PnL tool | `pnl_tool` (local) | None |

## Topology (system ADR-001)

- The desk backend and frontend run on a **Windows server inside the work network** (Python 3.11).
- The hub may be **split**:
  - a **Linux VPS** outside runs the public collectors and publishes snapshots (the existing `deploy/`, Python 3.12);
  - an **inside instance on Windows** replicates VPS snapshots and runs inside-only collectors (bank-internal feeds, the internal FX mark, manual file drops).
- The final split per source is not decided.
- **Immutable snapshots are the only thing that crosses the boundary.** No private desk data ever reaches the VPS.
- The backend reads snapshots only from the inside endpoint.
- All market data is properly licensed. Record each source's licence scope in the source registry.

## What the system expects from the hub

Details: `trading_desk_monitor/docs/architecture/02_MARKET_DATA_AND_CORE_CONTRACT.md` (§3 contract outline, §6 work list), `adr/ADR-001`, `adr/ADR-002`, and `06_DEPLOYMENT_AND_OPERATIONS.md`.

Status in this repository (16 October 2026): items 1–6 are implemented (see the last column
and [DESIGN.md](DESIGN.md) §11); 7–10 are open.

| # | Item | Notes |
|---|---|---|
| 1 | **Market Snapshot Contract v1.** JSON Schema in `schemas/market_snapshot/v1/` plus golden fixtures from public data: about 10 OFZ and corporates covering bullet, amortizer, put, floater and perpetual | The hub **owns** this contract. Object outline in system doc 02 §3: Instrument, CouponPeriod (with status), Redemption, Offer, FloaterFormula, Issuer, Calendar (with `valid_from`/`valid_to`; working weekends from `calendar_effective`, **not** `weekend_session`), IdMap; BondQuote (all price bases labelled), CurveQuoteSet (`dataset` code, tenors, % values), ZcycParams, Fixing (with source), RUB FxRate, non-RUB cross rates (ECB), CbrMeeting |
| 2 | **Publish facts, not conventions** | Keep `dataset` codes, tenor codes, units, coupon status and price basis as they are. Curve conventions belong to core's curve-spec registry; the backend maps `dataset` → spec ID |
| 3 | **As-known-at readers for static data.** `ref.*` over `ops.row_history`; replace the hard deletes in `ref.calendar_day` and `ref.weekend_session` with versioned changes | Needed so past snapshots are reproducible |
| 4 | **Immutable published snapshots.** Static and market kinds; EOD preliminary, EOD final and intraday cuts; ID = sha256 of canonical JSON; manifest (schema version, cut, `known_at`, sources, counts, coverage, stale flags, `supersedes`, `excluded` with reasons) | Use a table name distinct from the existing `ref.static_snapshot` payload cache, for example `ops.published_snapshot` |
| 5 | **Endpoints:** `GET /v1/snapshots`, `/v1/snapshots/{id}/manifest`, `/v1/snapshots/{id}/bundle` (gzip, ETag = id) | One bearer token per consumer |
| 6 | **Coverage labelling instead of exclusion** | Include `coupon_type='unknown'` and perpetuals with labels; only quote instruments that are in the static snapshot; publish floater benchmarks verbatim with formula status |
| 7 | **Replica mode for two-host operation** | The inside instance pulls VPS snapshots through the corporate proxy (bank CA bundle; proxy auth still unknown), verifies sha256, and serves them unchanged; a file-drop fallback ingests bundles or source files manually |
| 8 | **Windows support for the inside part** | Run as a Windows service; support Python 3.11 or bundle a 3.12 runtime |
| 9 | **Alerting and backups** | Alerts for failed or missed jobs, stale data, unfillable gaps, a missed snapshot at the cut, replica lag. Backups off the database host |
| 10 | Later | Intraday bond and FX data in snapshots; internal bank sources as inside collectors; SOFR/€STR/CPI series as needed by core product packages |

The TDM CSV export (`/v1/export/tdm`) stays for compatibility until the backend reads snapshots, then it is retired.

**History (WEB-15, 21 October 2026).** For history over many dates the hub serves the columnar
read API `/v1/history/*` (scope `read`, [HISTORY_API.md](HISTORY_API.md)): bonds, curves (the
`eod_final` selection rule), KBD, fixings and FX, with `known_at` replay, cursors and ETag
revalidation. It reads the VPS database directly, so it is a VPS endpoint, not part of the
replicated snapshot path of ADR-001; immutable history packages for the inside replica (review
§7.1, `kind = history`) are not built yet. Contract `market_snapshot/1.2` adds the MOEX linker / WAP
analytics, `Fixing.hub_ingested_at` and explicit RUSFAR units; the interface catalogue in
`trading_desk_monitor/docs/architecture/README.md` still needs the 1.2 entry.

### What the hub ships for items 1–6 (M1)

| # | Where |
|---|---|
| 1 | `schemas/market_snapshot/v1/` (`snapshot`, `static`, `market`, `manifest`, `common` schemas, README) and `fixtures/` (static + EOD-final market bundles for ten public bonds of 2026-10-15: OFZ-PD bullet, OFZ-PK RUONIA floater, OFZ-IN linker, OFZ-AD amortizer, corporate key-rate floater, put offer, corporate amortizer, two perpetuals, unknown coupon type), validated and rebuilt byte for byte in hub CI |
| 2 | `dataset`/tenor codes, units, coupon `status`, every price basis published as stored; MOEX analytics under `source_analytics`; RUB FX per 1 unit, ECB crosses separate |
| 3 | `ref.*_known_at(T)` readers (bond, board, coupon, amortization, offer, formula resolved, issuer, calendar, weekend session, CBR meetings) over `ops.row_history` (migration 0500); `ref.calendar_day` / `ref.weekend_session` are soft-deleted (versioned) instead of hard-deleted, so past calendars replay |
| 4 | `ops.published_snapshot` (0510): id = sha256 of the canonical bundle, gzip payload, manifest (schema version, kind, cut type, business date, cut ts, known_at, static snapshot id, supersedes, sources, datasets with counts/sha/coverage/stale flags, excluded with reasons). Jobs `snapshot_static` (daily 07:45) and `snapshot_market` (`eod_preliminary` 19:45, `eod_final` 08:00 for D-1, `intraday` every 30 min 10:00–19:00) — **default cut times pending DEC04**, set by `MDHUB_SNAPSHOT_*`. Intraday snapshots kept 30 days, static/EOD forever |
| 5 | `GET /v1/snapshots?kind&cut_type&business_date&latest`, `/v1/snapshots/{id}/manifest`, `/v1/snapshots/{id}/bundle` (gzip, `ETag` = id), behind the bearer token |
| 6 | Unknown coupon types and perpetuals are published with labels; market snapshots quote only instruments of their `static_snapshot_id` (others listed in `excluded`); floater benchmarks verbatim with formula status; one calendar version + fingerprint per snapshot from `ref.calendar_effective` |

## Relevant system documents

All paths are under `trading_desk_monitor/docs/architecture/`:
- `02_MARKET_DATA_AND_CORE_CONTRACT.md`: the contract outline and the hub work list.
- `adr/ADR-001-hosting-topology.md`: the VPS/inside split and replication.
- `adr/ADR-002-materialized-market-snapshots.md`: snapshot rules.
- `06_DEPLOYMENT_AND_OPERATIONS.md`: Windows and VPS layouts, proxy, monitoring, backups.
- `ROADMAP.md`: milestones. M1 needs items 1–5.

When the contract changes, bump the schema version, regenerate the fixtures in a reviewed change, and update the interface catalogue in `trading_desk_monitor/docs/architecture/README.md`.
