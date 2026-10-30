# MOEX ISS intraday fixtures (RGBI monitor inputs)

Recorded from public ISS (`https://iss.moex.com/iss`) on 2026-10-29 between 15:53 and 16:12 MSK
(Thursday, main session), `iss.meta=off`. Market data only; no credentials, no organisation data.

| file | request | edits |
|---|---|---|
| `zcyc_securities.json` | `/engines/stock/zcyc.json?iss.only=securities` | none (29 OFZ) |
| `index_marketdata.json` | `/engines/stock/markets/index/securities.json?securities=RGBI,RGBITR` | none |
| `index_candles_RGBI_p0.json` | `.../index/securities/RGBI/candles.json?interval=1&from=2026-10-28&till=2026-10-28` | first 5 of 500 candles |
| `index_candles_empty.json` | same, a page past the end | `candles.data` emptied |
| `index_analytics_RGBI_p0.json`, `_p1.json` | `/statistics/engines/stock/markets/index/analytics/RGBI.json?date=2026-10-28` (`start=0`, `start=20`) | none (TOTAL 29) |
| `repo_ccp_EQRP_marketdata.json` | `/engines/stock/markets/ccp/boards/EQRP/securities.json?securities=SU26238RMFS4,SU26212RMFS9` | none |
| `repo_ccp_EQRP_history.json` | `/history/engines/stock/markets/ccp/boards/EQRP/securities.json?date=2026-10-28` | 1 share + 3 OFZ rows of the 424, cursor TOTAL set to 4 |
| `repo_gcc_GCRP_marketdata.json` | `/engines/stock/markets/gcc/boards/GCRP/securities.json` | none |
| `repo_gcc_GCRP_history.json` | `/history/engines/stock/markets/gcc/boards/GCRP/securities.json?date=2026-10-28` | none |
| `forts_md_rates_iss.json` | `/engines/futures/markets/forts/securities.json?securities=KKZ6,RBZ6,RFZ6,RGBIF` | none (15 min delayed) |
| `forts_trades_RGBIF.json` | `.../forts/securities/RGBIF/trades.json?tradeno=2050015240732542800&next_trade=1` | none (56 trades) |

Observations used by the parsers: index marketdata and the `zcyc` securities block are realtime
(seconds behind the request); bonds/repo/FORTS marketdata on public ISS lag `SYSTIME` by ~900 s;
the FORTS `trades` endpoint answers the whole day without a cursor and honours
`tradeno=N&next_trade=1`; ISS reset the connection on about every 3rd-4th request.
