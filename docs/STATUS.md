# STATUS — 2026-10-16 (orchestrator checkpoint)

## Decisions (owner)
- GitHub: FiveLimaGolf/market-data-hub (private, `gh flg` alias). Nothing pushed yet — push at the end.
- DB: PostgreSQL 16. Tests use pgserver (no Docker on this Mac).
- TDM export: md_fx candidate (MOEX 19:00 indicative) is opt-in (`--fx-candidate`), so a TDM
  folder load can't overwrite the internal FX mark.

## Code (local commits on main)
8c33922 foundation · d11991c bonds EOD/intraday/indices/ZCYC + legacy import + quality/backfill/maintenance + deploy ·
948a75a FX/FORTS · ba9390a CBR/ECB/Ratescalcs/calendars · 6f18a7e bond static + Cbonds ·
4e0f4bb API + TDM export · b52fc54 legacy mapping. 412 tests green, mypy strict clean.

## In flight
- Fix agent for review findings (scratchpad/review/REVIEW.md: F1 backfill loop, F2 skipped scheduled
  runs, F3 TDM coupon status/ytm basis, F4 calendar precedence + calendars back to 2019, F5 API SECID
  case, F6 intraday history, F7 AlgoPack polling, F8 intraday raw retention/FKs, F9 floater status,
  F10–F14 lows; CBR liquidity col 6/7 labels; formula view incl. legacy_bc; DESIGN/DEVELOPING docs;
  `mdhub calendar seed`; MDHUB_API_POOL_SIZE). Schema changes only in NEW migrations 0420+.
  When it reports: review diff, run full checks, commit.

## Seed database (data/pgseed, pgserver PG 16.2, db `mdhub`; gitignored)
- Migrations applied from frozen copies in data/pgseed_migs (0001…0410). Apply 0420+ after the fix agent.
- Loaded: bond static (16,055 bonds), CBR full history (key rate 2013+, RUONIA 2010+, FX 2020+, liquidity,
  meetings, calendars), ECB 2015+, legacy MD/BC verbatim (verified) + mapped (curves, intraday, zcyc,
  fixings, liquidity, 3 curated formulas).
- Running (scratchpad/seed/*.log, runner scripts seed_backfill.py / seed_once.py):
  - chain 1 (backfill1.log): bonds_eod 2024-01-03→2026-10-15 (at ~2025-06), then zcyc 2022→, indices 2022→.
  - chain 2 (backfill_fx.log, ISS 2 rps): futures_eod → fx_moex_eod → fx_indicative 2024→ → algopack 2024→.

## Remaining steps
1. Commit fix agent's work; migrate seed DB (0420+); re-run calendar refresh / bonds_static if it says so.
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
