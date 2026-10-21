# STATUS — 2026-10-17 (orchestrator checkpoint)

> Historical checkpoints below. For the current deployment state and actionable open work,
> use [ROADMAP.md](../ROADMAP.md), updated 19 October 2026. Earlier "Remaining steps" and
> source-access assumptions below may have been superseded; recheck before acting.

> **2026-10-21 (WEB-16):** EOD fixes from the live check of 21.10 (eod_preliminary bonds from the
> intraday close, per-mode snapshot locks, curve EOD rule with bid/ask inside the window,
> RU_PRODUCTION from the official calendar) on branch `claude/hub-eod-fixes` (intended release
> 0.2.1): [ROADMAP.md](../ROADMAP.md) WEB-16, [deploy/UPGRADE_TO_0.2.1.md](../deploy/UPGRADE_TO_0.2.1.md).

> **2026-10-21 (WEB-15):** history read API `/v1/history/*`, contract `market_snapshot/1.2`,
> dataset-level curve gaps, `eod_final` input readiness and auto-correction, calendars to 2037 on
> branch `claude/hub-tdm-history-1-2` (intended release 0.2.0). Details and remaining work:
> [ROADMAP.md](../ROADMAP.md) WEB-15, [deploy/UPGRADE_TO_0.2.0.md](../deploy/UPGRADE_TO_0.2.0.md).

## Decisions (owner)
- GitHub: FlyingScotman/market-data-hub (public). Release defaults point to this repository;
  GitHub release artifacts contain code only. Production HTTPS is marketdatahub.cc behind host nginx.
- DB: PostgreSQL 16. Tests use pgserver (no Docker on this Mac).
- TDM export: md_fx candidate (MOEX 19:00 indicative) is opt-in (`--fx-candidate`), so a TDM
  folder load can't overwrite the internal FX mark.

## Code (local commits on main)
8c33922 foundation · d11991c bonds EOD/intraday/indices/ZCYC + legacy import + quality/backfill/maintenance + deploy ·
948a75a FX/FORTS · ba9390a CBR/ECB/Ratescalcs/calendars · 6f18a7e bond static + Cbonds ·
4e0f4bb API + TDM export · b52fc54 legacy mapping · b878fa6 review fixes (0420). 441 tests green, mypy strict clean.

## In flight
- Commits: b878fa6 review fixes (0420) · 74ed84f seed report + bundle tooling · 7354200 second-review fixes (0421).
  479 tests green. Seed DB at 0421 (applied with scratchpad/seed/migrate_locked.py: ordered table locks,
  because plain `migrate` deadlocked against a running loader).
- Chain 1 DONE (backfill1b.log): bonds_eod to 2026-10-15, zcyc 2022→, indices 2022→.
- `mdhub calendar seed --since 2010-01-01` (run 201): observed calendars 2010→ (pre-2019 CBR holidays → not_expected).
- backfill_worker run 204: all 4 Saturday sessions backfilled (bonds/yield/futures/fx/indicative/zcyc/indices).
  Unfillable (source confirms no data, verified vs ISS): RUSFAR* holiday/thin days, RUSFARCNY China holidays,
  some bond indices, zcyc 2022-01-07, bond yields 2024-02-08, CBR standing 2018-04-25, ECB TARGET holidays (57).
- c7a9d44 TARGET calendar (rule-only from 2002, ECB cross-check) → 0422 applied to seed; cbr_meetings run 399
  wrote 9496 rule_target rows; 57 ecb_fx gaps → not_expected. Pushed main to GitHub (private).
- Ratescalcs live snapshot loaded (run 394): all 5 curves present; swap history comes from legacy
  (andromeda IRS RUONIA 2022-07→, key-rate 2022-10→; otc_monitor 2026-09→; cfets 1 day).
- AlgoPack orphan stopped (owner OK); re-run 2024-01-03→2026-10-15 on 7354200 (algopack2.log).

- 0e3c7b1 OTC Monitor swap loader (swaps_otc_monitor, 15 min; ruonia_ois, key_rate_swap, cny_ruonia_xccy,
  NEW cny_rusfar_ois; CNY bounds -50..300). Live run 410 on seed: 4 snapshots stored.
- f7b1be2 AlgoPack option (b) (0423): obstats dropped; tradestats+FUTOI rolling 90 days (retention, gap scan,
  backfill clamp). 3958372: backfill captures of pre-window dates purged by till=. Seed: 0423 applied,
  >90d deleted, reloaded 2026-07-19→10-16 (run 414; 114 MB + 165 MB).
- d83e380 andromeda CNY importer (`mdhub legacy andromeda-cny`, 0424 curve row history). Seed run 411:
  cny_rusfar_ois 646 days (2023-12-12→), cny_ruonia_xccy 746 days (2023-07-10→); export time 18:05:15 MSK.
- Final quality run 415 + backfill_worker 416: 0 open gaps. docs/SEED_REPORT.md regenerated (report now
  honours rolling windows).
- 2026-10-17 fixing depth (owner: "низкая глубина фиксингов, не хватает для старых купонов"):
  19e00b5 Rosstat CPI (rosstat_cpi, 0425; Russian Trusted Sub CA bundled), CBR refinancing rate
  1992-2015 (closed series, DatasetSpec.history_end), MOEX FX fixings discovery over long ranges,
  history starts CBR FX 1998 / zcyc + indices 2014-01-06 / FX fixings 2013-03. 539d61b CBR FX ranges
  via per-currency XML_dynamic (1998-2026 reload in 10 min, 0 values changed) and the fix that a
  rate ends at the next daily-set record (12 stale LTL 2015 rows archived to row_history, deleted).
  Seed: CPI 1991-01→2026-09 (429), refi 8766 days, FX 1998→ every day, zcyc 2014→ (2016-11-25 source
  inconsistency, unfillable), indices 2014→, USDFIXME 2013-03→2024-06-11, CNYFIXME 2013-11→2025-04-07.
  Quality run: 36 datasets, 0 stale, 0 open gaps. NB seed scripts must pass DbCalendar to run_job
  (the default weekday calendar opened holiday gaps in one quality run; its 413 tasks were cancelled).
- Snapshot layer (owner-started session hub-snapshot-layer, own worktree, migrations 0500+): contract v1,
  as-known-at static readers, ops.published_snapshot, /v1/snapshots. See docs/SYSTEM_CONTEXT.md.

## Seed database (data/pgseed, pgserver PG 16.2, db `mdhub`; gitignored)
- Migrations applied 0001…0425 (0425 from src/mdhub/db/migrations).
- Loaded: bond static (16,055 bonds), CBR full history (key rate 2013+, RUONIA 2010+, refinancing rate
  1992-2015, FX 1998+, liquidity, meetings, calendars), Rosstat CPI 1991+, ECB 2015+, legacy MD/BC verbatim (verified) + mapped (curves, intraday, zcyc,
  fixings, liquidity, 3 curated formulas).
- Backfilled: bonds_eod 2024→, futures/fx EOD + fx_indicative 2024→, MOEX FX fixings 2013→, zcyc 2014→,
  indices 2014→; AlgoPack last 90 days.
- Swaps: andromeda history (RUONIA OIS, KRS, CNY OIS/xccy), otc_monitor 2026-09→ + live, ratescalcs live.

## Bundle (2026-10-16; superseded by dist/market-data-hub-20261018.tar.gz, see Code above)
- dist/market-data-hub-20261016.tar.gz (668 MB, sha256 54c17d26…) from 1c67321; seed dump 670 MB (DB 3.4 GB).
- Restore tested into a fresh UTF8 PostgreSQL 16 (restore_seed.sh --database-url): 14 migrations, checksums
  match, seed report identical to the bundled one. 1c67321: migrate/restore refuse non-UTF8 databases.
- main pushed to GitHub (1c67321).

## Remaining steps
1. First VPS deployment (deploy/README_DEPLOY.md §1-5) with the owner; after start, quality +
   backfill_worker fill the days between the seed date (2026-10-16) and the deploy date automatically.
2. Snapshot layer (session hub-snapshot-layer, worktree): review + merge when done.
3. Bond static verification: accrued interest / yield recomputed from ref schedules vs MOEX EOD.
4. Admin console, observability, self-update: design in docs/ADMIN_DESIGN.md (owner decisions 2026-10-17:
   internet + IP allow-list, roles viewer/operator/admin, passkeys + TOTP, updater v1 = docker compose).
   Phase A (acdebdb: named tokens, api_request log, heartbeat, source_probe, version from git; migrations
   0600/0610) and Phase C (47b8d47: code-only signed bundle, `mdhub release verify`, CI + release workflows,
   not yet run on GitHub) merged into main 45cb0e5. Phase B (156a5cc console + passkey/TOTP auth, migration 0620;
   a79840a "Run now" via ops.job_request run by the worker, migration 0630, no source creds in the admin container)
   merged e81c04b; 670 tests green. Not verified: real platform passkey, Docker/Caddy build. Next: Phase D (updater).
   Leftover: seed-cluster copy mdhub_admin_dev (3.4 GB) from Phase B testing; drop when not needed.
   Phase D (322f61a updater, worker drain, Updates page; 0640), E (1b397db write-only source credentials
   sealed to the worker key; 0700) and F (23581cd hostmon, PostgreSQL stats, Machine page; 0750) merged
   1bd00d8 / cd85ab8 / 1c480ca; 724 tests green. README_DEPLOY: §13 updater, §14 credentials, §15 machine.
   Not verified anywhere: real Docker/Caddy, the updater rehearsal (README §13), a real passkey.
   The updater now writes ops.container_status every 30 s (ContainerReporter thread, docker ps/inspect/stats;
   paused during a restore). Not verified against a real Docker daemon.
   c2cafc1 least-privilege DB roles mdhub_api / mdhub_admin / mdhub_hostmon (0800_service_roles,
   deploy/sql/service_roles.sql, README_DEPLOY §16; owner fallback with a warning while MDHUB_*_DB_PASSWORD
   is unset). Both merged ddfc774; 776 tests green. New tables/columns read by api/admin/hostmon need a
   grant update in the role procedure (DEVELOPING); the snapshot layer's ops reads too.
- 2026-10-18 first deploy prepared: origin/main (PR #1 bond static verification) merged 566a93c, 797 tests
  green; local tag v0.1.0 (not pushed); bundle dist/market-data-hub-20261018.tar.gz (sha256 5e2b4398…,
  seed dump without admin.* data) restore-tested into a fresh PG 16 (8 migrations 0600-0800 applied, 23/23
  checksums match). Checklist: deploy/FIRST_DEPLOY.md. No release key pinned yet: self-update starts with
  the first signed release after v0.1.0.

## Open items for owner
- Cbonds API → HTTP 401 for any login (likely IP allow-list of old server); hub auto-skips.
- MOEX Passport login has no realtime entitlement; APIM realtime only for shares/futures.
- MOEX SDFI swap curves (CNY OIS vs RUSFAR CNY, xccy) need a subscription.
- OTC Monitor: token present in env (audit 05 was wrong); live loader being added; no history endpoint.
- andromeda (EOD swap archive, stopped 2026-09-10) not connected — personal browser session on old server.
- Ratescalcs credentials are trivially guessable (username = common word, password = site URL path
  segment) — ask provider to rotate.
- Not in hub (no source): SOFR/€STR/SARON fixings & curves, CDS, issuer ratings (Cbonds).
- Two floaters with MOEX benchmark OTHER (Ситимат НН, КВС 01): reference rate not identified.
