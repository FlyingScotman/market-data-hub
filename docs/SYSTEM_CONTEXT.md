# System context: where the hub fits

As of 22 October 2026, checked against `main` @ `d71d984` (release v0.3.2). This file replaces the 16 October version, which predated the amendment of system ADR-001 on 18 October (one hub on the VPS, no inside instance, several consumers).

market-data-hub is the **market and static data service** of the Trading Desk Monitor, a RUB fixed-income desk analytics system, and of the owner's other tools. The system architecture lives in the repository **trading-desk-monitor** (GitHub `FlyingScotman/trading-desk-monitor`), folder `docs/architecture/`: start with its `README.md` (component map, interface catalogue, reading order). Monitor documents sometimes call this repository `market-data-server`. This file summarises what the system expects from the hub; [DESIGN.md](DESIGN.md) stays authoritative for the hub's internals, and [ROADMAP.md](../ROADMAP.md) for open work.

## Components

| Component | Repository | Relation to the hub |
|---|---|---|
| Desk backend + frontend | `trading-desk-monitor` | **Main consumer.** Pulls immutable snapshots into its own cache (token scope `snapshots`) and reads the history API (scope `read`); translates both into core domain objects with its own adapter |
| Owner's development machines and other tools | — | Consumers with their own named tokens (`read` or `snapshots`) |
| Calculation core | `rub-fixed-income-core` | **No dependency in either direction.** Core never parses hub payloads; the hub never imports core |
| PnL tool | `pnl_tool` (local) | None |

## Topology (system ADR-001, amended 18 October 2026)

- **One hub instance on a Linux VPS** runs every public collector, the quality and backfill jobs and the snapshot publisher. It is the single authoritative location for every series it collects. Deploy: docker compose, Python 3.12, PostgreSQL 16 ([deploy/README_DEPLOY.md](../deploy/README_DEPLOY.md)); TLS terminates at host nginx ([deploy/DOMAIN_DEPLOY.md](../deploy/DOMAIN_DEPLOY.md)).
- **Only the reverse proxy and SSH face the network** ([deploy/README_DEPLOY.md](../deploy/README_DEPLOY.md) §1). PostgreSQL, the API and the admin console listen on loopback; nginx routes `/v1/` and `/health/` to the API and the rest to the admin console.
- **No inside instance, no replica mode, no Windows port.** The 16 October plan (an inside Windows instance replicating VPS snapshots and running inside-only collectors) was dropped on 18 October. The hub stays Linux-only and needs no Python 3.11 support.
- **The desk backend runs on a Windows server inside the work network.** It pulls snapshots over HTTPS through the corporate proxy, verifies the sha256 against the snapshot ID and keeps the bundle in its cache; a run reads only from the cache, so a VPS outage delays new snapshots but never blocks a run. If the proxy blocks the VPS, an operator copies bundle files into the cache's drop folder, with the same verification.
- **Inside-only data never enters the hub.** Positions, official PnL, the internal FX mark, bank feeds and trader marks reach the backend through its private-input adapters and are applied as versioned overrides on top of a hub snapshot. Consumers only read from the hub.
- All market data is properly licensed; each source's licence scope belongs in the source registry. Tokens go only to the owner's own consumers, within that scope (agency ratings: internal desk use only, WEB-18).

## Access

Named bearer tokens per consumer ([ADMIN_DESIGN.md](ADMIN_DESIGN.md) §3.3, `src/mdhub/api/auth.py`): only the SHA-256 is stored, and a revocation takes effect within 30 seconds. Scopes: `read` (all of `/v1`), `snapshots` (`/v1/snapshots*` only), `export` (`/v1/export/*` only); `/v1/openapi.json` accepts any scope. A valid token without the scope gets 403. The desk backend uses `snapshots` (and `read` for history); a development machine `read` or `snapshots`.

## What the hub serves the system

| Interface | State in `main` | Reference |
|---|---|---|
| **Market Snapshot Contract** `market_snapshot/1.x`, owned by the hub | **1.3**. 1.1: static terms for the desk (WEB-13). 1.2: MOEX linker / WAP analytics, `Fixing.hub_ingested_at`, explicit RUSFAR units (WEB-15). 1.3: cross-rate kinds, €STR / SOFR / CPI y/y, proxy curves labelled `proxy_*`, not OIS (WEB-16); agency ratings, replacement-bond flag, `issue_size_moex`, curated-terms evidence (WEB-18). Consumers read any 1.x; a new dataset bumps the minor version | `schemas/market_snapshot/v1/` (README, schemas, golden fixtures rebuilt byte for byte in CI) |
| **Immutable snapshots** | Static and market kinds; ID = sha256 of the canonical bundle; manifest with sources, counts, coverage, stale flags, `supersedes`, `excluded` with reasons. Jobs: `snapshot_static` daily 07:45 MSK; `snapshot_market` `eod_preliminary` 19:45 for D, `eod_final` 08:00 for D-1, `intraday` every 30 min 10:00–19:00. Defaults are set by `MDHUB_SNAPSHOT_*`; system DEC04 is partly answered (FX at the MOEX 19:00 rate; bonds and curves at the hub cuts as published). Intraday snapshots are kept 30 days, static and EOD forever; a backfill that changes a day's inputs publishes a correction through `supersedes` (WEB-15) | `ops.published_snapshot`, `src/mdhub/snapshot/`, [DESIGN.md](DESIGN.md) §11 |
| **Snapshot endpoints** | `GET /v1/snapshots`, `/v1/snapshots/{id}/manifest`, `/v1/snapshots/{id}/bundle` (gzip, `ETag` = id) | `src/mdhub/api/routes/snapshots.py` |
| **History API** | `/v1/history/{index,bonds,curves/{dataset},zcyc,fixings,fx}`: columnar JSON, cursors, ETag/304, gzip, `known_at` replay. Reads the VPS database directly; not part of the snapshot path | [HISTORY_API.md](HISTORY_API.md) |
| **Published EOD history** | `eod_final` snapshots for every business date from 2026-01-05 (WEB-11), built as known at each date; instrument terms are as known now | [deploy/HISTORICAL_EOD_2026.md](../deploy/HISTORICAL_EOD_2026.md) |
| Other `/v1` reads | Bonds, FX (official, EOD, indicative, ECB), futures, fixings, curves, ZCYC, indices, calendar; operations: `/v1/coverage`, `/v1/runs`, `/v1/runs/health`, `/v1/gaps` | `src/mdhub/api/routes/` |
| TDM CSV export `/v1/export/tdm` | Legacy (scope `export`); the desk backend reads snapshots and history. Retire once no consumer uses it | `src/mdhub/export/tdm.py` |

Principles that stay: **publish facts, not conventions** (dataset and tenor codes, units, coupon status and every price basis as stored; day counts, spot lags and the curve-spec registry belong to core, and the backend maps `dataset` to a core spec ID); **as-known-at readers** for static data over `ops.row_history`, with versioned instead of hard deletes, so past snapshots replay; **coverage labelling instead of exclusion** (unknown coupon types and perpetuals are published with labels; a market snapshot quotes only the instruments of its static snapshot; floater benchmarks are published verbatim with their formula status).

**Release state.** Production was last verified at 0.2.0 (21 October, [ROADMAP.md](../ROADMAP.md)). `main` is release v0.3.2 (22 October), which contains WEB-16, WEB-17 and WEB-18; installing it is the owner's step and is not recorded here.

## The system's work list, as of 22 October

The 16 October list (items 1–10) stands as follows:

| # | Item | Status |
|---|---|---|
| 1–6 | Snapshot contract, facts not conventions, as-known-at readers, immutable snapshots, endpoints, coverage labelling | Done; in production since 0.1.3 / 0.2.0 |
| 7 | Replica mode for two-host operation | **Dropped** (ADR-001 amended 18 October): the backend's cache and drop folder replace it |
| 8 | Windows support for an inside part | **Dropped** (no inside instance) |
| 9 | Alerting and backups | Open: gaps are visible in `/v1/gaps`, but there is no alert channel (review P0-4); backups stay on the VPS until WEB-6 (off-host copy) |
| 10 | Intraday data; foreign and CPI series | Intraday market cuts done (every 30 min); €STR, SOFR and CPI y/y done in WEB-16. The remaining derivatives inputs are tracked in [ROADMAP.md](../ROADMAP.md), section "TDM H10–H15, SARON, H17" |

The desk's brief to the hub is `trading-desk-monitor/docs/architecture/briefs/HUB.md` (items H0–H26; H6 is this rewrite). Items the hub accepts are tracked in [ROADMAP.md](../ROADMAP.md). On the desk's critical path now (monitor ROADMAP revision 4, wave A package P6): H11 futures prices and H13 FX value dates, swap points and CETS FX swaps in the snapshot, then H12 RUONIA unpublished days, H10 CPI levels, SARON and H17 publication calendars, as contract `market_snapshot/1.5`.

## Relevant system documents

All paths are in `trading-desk-monitor/docs/architecture/`:
- `README.md`: the component map and the interface catalogue (it has the 1.2 contract entry; 1.3 is missing, and its hub rows still describe the snapshot layer as not deployed).
- `adr/ADR-001-hosting-topology.md`: one VPS hub for every consumer; the backend's snapshot cache.
- `adr/ADR-002-materialized-market-snapshots.md`: snapshot rules.
- `02_MARKET_DATA_AND_CORE_CONTRACT.md`: the contract outline.
- `06_DEPLOYMENT_AND_OPERATIONS.md`: Windows and VPS layouts, proxy, monitoring, backups.
- `ROADMAP.md` (revision 4) and `briefs/HUB.md`: milestones and the hub items.

When the contract changes, bump the schema version, regenerate the fixtures in a reviewed change, and update the interface catalogue in `trading-desk-monitor/docs/architecture/README.md`.
