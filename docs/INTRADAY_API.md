# Intraday read API (`/v1/intraday/*`) and the RGBI monitor's daily series

Reference of the endpoints that serve the intraday inputs of the RGBI futures fair-value monitor
(and anyone else who needs MOEX intraday data from the hub). Implementation:
`src/mdhub/api/routes/intraday.py`; tables: migration `0280_md_intraday_rgbi.sql`; as-known-at
readers: `0560_intraday_known_at.sql`; tests `tests/test_rgbi_intraday.py`.

Envelope, paging, caching, gzip, errors and access (scope `read`) are those of the history API:
see [HISTORY_API.md](HISTORY_API.md) ("Access", "Response envelope", "Caching and compression").
The endpoints add no values: change-only tables hold a row when a value changed, so the state at
an instant is the latest row at or before it - the API does not forward fill.

## Common parameters

| parameter | meaning |
|---|---|
| `from` | intraday endpoints: a date `YYYY-MM-DD` (from 00:00 MSK) or an ISO 8601 instant **with offset** (`2026-10-29T10:00:00+03:00`, `...Z`); daily endpoints: a date. **Required** |
| `to` | intraday: a date (the whole day, inclusive) or an instant (inclusive); default now. Daily: a date, inclusive, default today (MSK). At most 366 days per call |
| `secid` | comma list, case-sensitive, at most 500 |
| `limit`, `cursor`, `fields`, `known_at` | as in the history API (`limit` 1..50,000, default 10,000) |

Intraday rows are ordered by the exchange time column, then the series key (`meta.order`); the
range is `[from, to)` on that column (`meta.range_column`). Poll incrementally by passing the
`next` cursor, or a `from` instant equal to the last row's time (rows at that instant repeat).
Without `to` the range ends at the request time but the query (cursor, ETag) stays the same.

Every intraday row carries `source_ts` (exchange clock: SYSTIME, or the block's own clock),
`ingested_at` (hub write) and `latency`: `realtime` (APIM, index and KBD blocks of public ISS)
or `delayed` (public ISS marketdata of bonds, repo, FORTS fallback: ~15 minutes).

`known_at`: rows written after it are absent and revised rows show their earlier image
(`ops.row_history`). `md.futures_trade` is append-only: only the `ingested_at` filter applies.

## Endpoints

| endpoint | table | key / order | filters |
|---|---|---|---|
| `GET /v1/intraday/futures` | `md.futures_intraday` | `ts, secid, source, source_variant` | `secid`, `source` (`moex_apim`, `moex_iss`) |
| `GET /v1/intraday/futures_trades` | `md.futures_trade` | `trade_ts, secid, source, trade_no` | `secid`, `source` |
| `GET /v1/intraday/bonds` | `md.bond_quote_intraday` | `ts, secid, board` | `secid`, `board` |
| `GET /v1/intraday/zcyc_securities` | `md.zcyc_security_quote` | `ts, secid` | `secid` |
| `GET /v1/intraday/index` | `md.index_intraday` | `ts, secid` | `secid` |
| `GET /v1/intraday/index_candles` | `md.index_candle` | `begin_ts, secid, interval_min` | `secid` |
| `GET /v1/intraday/repo` | `md.repo_intraday` | `ts, market, board, secid` | `market` (`ccp`, `gcc`), `board`, `secid` |
| `GET /v1/index/constituents` | `md.index_constituent` | `trade_date, index_id, secid` | `index`, `secid` |
| `GET /v1/repo/ccp` | `md.repo_eod` (`market='ccp'`) | `trade_date, board, secid` | `board`, `secid` |
| `GET /v1/repo/gc` | `md.repo_eod` (`market='gcc'`) | `trade_date, board, secid` | `board`, `secid` |
| `GET /v1/history/bond_close` | `md.bond_close_quote` | `trade_date, secid, board, basis` | `secid`, `board`, `basis` |
| `GET /v1/intraday/latency` | all intraday tables | dataset, latency | none |

### Futures (`/v1/intraday/futures`, `/v1/intraday/futures_trades`)

FX contracts and the rates contracts of `MDHUB_FUTURES_RATES_ASSETS` (default RGBI dated `RB*`,
perpetual `RGBIF`, `KEYRATE` `KK*`, `RUONIA` `RF*`), every minute 07:00-23:50 on FORTS trading
days. Columns: `bid`, `offer`, `bid_depth`, `offer_depth` (best-level volumes when the feed
publishes them; null on public ISS), `last`, `open`, `high`, `low`, `settle_price` (current
settlement price estimate), `open_position`, `num_trades`, `vol_today`, `val_today`, `swap_rate`,
`*_rub_per_unit` (FX only), `trade_date`. Prices exactly as MOEX publishes them (`RB*` in index
points x 100, `RGBIF` in index points).

Trades: `trade_no`, `trade_ts` (exchange time), `price`, `quantity` (contracts), `buysell`
(aggressor side `B`/`S`), `open_position` after the trade, `off_market`, `session_date`. A trade
read from both feeds exists once per `source`; filter one.

### Bonds and KBD quotes

`/v1/intraday/bonds`: every board, a row per change (public ISS, delayed). Units as
`/v1/history/bonds`: prices in % of face, yields % p.a., `*_bp` basis points.

`/v1/intraday/zcyc_securities`: the `securities` block of `zcyc.json` - per OFZ-PD (~29) the
order-book `bid_price_pct`/`ask_price_pct` and `bid_yield_pct`/`ask_yield_pct`, last trade
(`trd_*`), KBD price/yield (`clc_*`), corrected (`crt_*`), `correction`, durations in days. `ts` =
quote update time, `calc_ts` = `source_ts` = KBD calculation time. Polled every ~10 s
06:50-18:59.

`/v1/history/bond_close`: closing bid/ask per bond and day at `MDHUB_BOND_CLOSE_CUT` (18:45 MSK):
the latest stored quote of the day at or before the cut (`quote_ts` says when it was set);
`basis` `bonds_marketdata` (per trading board) or `zcyc_securities` (board `ZCYC`, with yields).

### Indices

`/v1/intraday/index`: `current_value`, `last_value` (previous close), `open_value`, `high`, `low`,
`last_change`, `last_change_prc` (%), `val_today` (RUB), `trading_session`; RGBI and RGBITR
(`MDHUB_INTRADAY_INDICES`). `/v1/intraday/index_candles`: 1-minute OHLC, `value` (RUB), `volume`;
the running minute's candle is revised until it ends.

`/v1/index/constituents`: `weight_pct` (% of the index; the weights of an index and day sum to
~100), `ticker`, `short_name`, `trading_session`, `session_date`; RGBI from 2016-12-13
(`MDHUB_CONSTITUENT_INDICES`; sub-indices such as `RUGBITR5Y` work the same way).

### Repo with the CCP

`/v1/intraday/repo` and `/v1/repo/{ccp,gc}`: repo rates in % p.a. `ccp` = per OFZ, boards EQRP
(1 day), EQWP (1 week), EQMP (1 month), EQTP (3 months); `gcc` = GC baskets by term (GCRP 1 day,
GCOW 1 week, GCSW 2 weeks, GCOM 1 month, GCSM 2 months, GCTM 3 months, GCUM 6 months, GCNM 9
months, GCOY 1 year; `RU000A0JW4Z1` = GC Bonds). Intraday columns: `bid_pct`, `offer_pct`,
`open_pct`, `low_pct`, `high_pct`, `last_pct`, `waprice_pct`, `num_trades`, `vol_today`,
`val_today_rub`, `trading_status`, `settle_date` / `settle_date2` (legs). Daily: `open_pct`,
`low_pct`, `high_pct`, `close_pct`, `waprice_pct`, `num_trades`, `value_rub`.

### Latency (`/v1/intraday/latency`)

One row per intraday dataset and `latency` label over the rows of the last 4 days:
`last_source_ts`, `last_ingested_at`, `age_s` (now - the freshest observation time), `rows_last_hour`,
`lag_p50_s` / `lag_p95_s` / `lag_max_s` (`ingested_at - observation time` of the rows written in the last
hour). Observation time is `ts`, `trade_ts`, or candle `begin_ts`; `age_s` also uses that clock.
`source_ts` is retained separately: a current SYSTIME does not imply a current quote.
Expect ~0-60 s for realtime feeds and ~900 s for delayed public ISS. Candle lag includes the
minute's duration. On quiet instruments, quote age also includes time without a change. Change-only tables
write nothing while the market is quiet: a large `age_s` outside trading hours is normal.

## Example

```
GET /v1/intraday/futures?from=2026-10-29T10:00:00+03:00&secid=RGBIF,RBZ6&fields=bid,offer,last,latency
GET /v1/intraday/futures?from=2026-10-29&secid=RGBIF&cursor=<next of the previous page>
GET /v1/index/constituents?from=2026-10-28&index=RGBI
```
