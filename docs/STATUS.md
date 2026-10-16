# STATUS — 2026-10-16 (orchestrator checkpoint)

## Decisions (owner)
- GitHub: FiveLimaGolf/market-data-hub (private, `gh flg` alias). Nothing pushed yet — push at the end.
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
- Agent adding TARGET calendar (observed from ECB + Easter rule) → migration 0422; ecb_fx gaps → not_expected.
- AlgoPack orphan stopped (owner OK); re-run 2024-01-03→2026-10-15 on 7354200 (algopack2.log).

## Seed database (data/pgseed, pgserver PG 16.2, db `mdhub`; gitignored)
- Migrations applied from frozen copies in data/pgseed_migs (0001…0421).
- Loaded: bond static (16,055 bonds), CBR full history (key rate 2013+, RUONIA 2010+, FX 2020+, liquidity,
  meetings, calendars), ECB 2015+, legacy MD/BC verbatim (verified) + mapped (curves, intraday, zcyc,
  fixings, liquidity, 3 curated formulas).
- Backfilled: bonds_eod 2024→, futures/fx EOD + fx_indicative 2024→, zcyc 2022→, indices 2022→; AlgoPack re-running.

## Remaining steps
1. After backfills finish: re-run `mdhub calendar seed` (observed days from full history).
2. After backfills: run quality job on seed DB, fill remaining gaps (incl. 2026-09-25/28 outage
   window via ISS history), bonds_eod/futures/fx for 2026-10-16.
3. Integrity re-check of seed DB (counts, coverage per dataset, gaps report) → docs/SEED_REPORT.md.
4. Package VPS archive: git archive of code + `pg_dump -Fc` seed + deploy/ + README_DEPLOY; checksums.
   Push repo to GitHub. Final report to owner.

## Open items for owner
- Cbonds API → HTTP 401 for any login (likely IP allow-list of old server); hub auto-skips.
- MOEX Passport login has no realtime entitlement; APIM realtime only for shares/futures.
- MOEX SDFI swap curves (CNY OIS vs RUSFAR CNY, xccy) need a subscription.
- OTC Monitor token not provided; prod parser v3–5 and ~/.hermes wrappers exist only on the old server.
- Ratescalcs credentials are trivially guessable (username = common word, password = site URL path
  segment) — ask provider to rotate.
- Not in hub (no source): SOFR/€STR/SARON fixings & curves, CDS, issuer ratings (Cbonds), CPI index.
