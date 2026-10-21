# History read API (`/v1/history/*`)

Reference of the columnar history endpoints (WEB-15). They let a consumer such as the Trading
Desk Monitor (TDM) load the history of bond prices and yields, curves, KBD, fixings and FX for
past dates in a few calls, instead of downloading one full `eod_final` snapshot per day.
Implementation: `src/mdhub/api/routes/history.py`, envelope and caching
`src/mdhub/api/columnar.py`, tests `tests/test_history_api.py`. General API rules (tokens,
request log): [DESIGN.md](DESIGN.md) §8.

The endpoints read the stored tables (or, with `known_at`, the same as-known-at readers the
snapshot builders use). They add no values: no forward fill, no interpolation, no derived
spreads. Sources are never merged.

## Access

- `Authorization: Bearer <token>`; scope **`read`** (tokens with only `snapshots` or `export`
  get 403). Create one for the desk monitor with `mdhub tokens create tdm-history -s read`
  (the token is printed once; store it in the secret store, never in a repository).
- GET only. Every request is logged in `ops.api_request` without the token.

## Common parameters

| parameter | meaning |
|---|---|
| `from` | first date, `YYYY-MM-DD` (Moscow business / value date), **required** (not on `index`) |
| `to` | last date, inclusive; default today (Moscow) |
| `limit` | rows per page, 1..50,000, default 10,000 |
| `cursor` | opaque value of `next` from the previous page; send it with the **same** other parameters |
| `fields` | comma list of columns to return; the key columns of the endpoint are always included |
| `known_at` | ISO 8601 instant **with offset** (`2026-10-16T08:00:00+03:00`, `...Z`): read the data as the hub knew it then (revisions after it are undone). Default: the current state |

Errors: `400` (bad parameter, `from` after `to`, unknown field, too many SECIDs/codes/pairs,
`known_at` without offset, a cursor of another query), `401` (no/invalid token), `403` (scope),
`404` (unknown curve dataset), `422` (missing `from`, `limit` out of range),
`503` (database down). Body: `{"error": {"status", "message"}, "request_id"}`.

## Response envelope

```json
{"dataset": "bonds",
 "columns": ["trade_date", "secid", "board", "close_pct", ...],
 "rows": [["2026-10-15", "SU26242RMFS0", "TQOB", 97.85, ...], ...],
 "as_of": "2026-10-21T13:44:04.790654+00:00",
 "known_at": null,
 "data_version": "2026-10-15T16:00:00+00:00",
 "next": null,
 "meta": {"from": "...", "to": "...", "order": [...], "units": "...", "limit": 10000,
          "returned": 1, ...}}
```

- `rows[i][j]` is the value of `columns[j]`. Numbers are JSON numbers with exactly the stored
  digits (`97.80` stays `97.80`: parse with a decimal parser, e.g.
  `json.loads(body, parse_float=Decimal)`); dates `YYYY-MM-DD`; instants ISO 8601 with offset
  (UTC, fractional seconds of variable length); `null` = no value.
- `as_of`: start of the read transaction (all statements of the request see that state).
- `known_at`: the parameter, normalized to UTC, or `null`.
- `data_version`: the latest hub write (`ingested_at` / `updated_at`) of any row of the **whole
  selection** (all pages, not just this one); `null` for an empty selection or a `known_at`
  read. Pages of one selection share it: a different value on a later page means the data
  changed while paging.
- `next`: cursor of the next page, `null` on the last page. Rows are ordered by the endpoint's
  key (`meta.order`), pages never overlap or skip rows.

## Caching and compression

- `ETag: W/"<hash>"` on every 200, `Last-Modified` (= `data_version`) when it is known (not for
  `known_at` reads and `index`). The ETag is computed
  from the query, the page (cursor, limit) and a cheap **version query** of the selection (row
  count and latest write per table) before any row is read; for `known_at` reads and `index`
  it is the hash of the body.
- Send `If-None-Match: <etag>` (preferred) or `If-Modified-Since: <last-modified>`: when
  nothing changed the answer is `304 Not Modified` with no body. A revision of any row in the
  range (also of a past day) changes the ETag. Polling a range every few minutes is cheap.
- `Cache-Control: private, no-cache` (always revalidate), `Vary: Accept-Encoding`.
- `Accept-Encoding: gzip` gives `Content-Encoding: gzip` (bodies of 1 KB and more).

## Limits and volume

- 50,000 rows per page; at most 500 SECIDs (`bonds`), 200 codes (`fixings`), 100 pairs (`fx`)
  per call. 500 SECIDs of 12 characters make a URL of about 6.5 KB (proxies usually allow 8 KB).
- A year of 600 bonds = two SECID groups (500 + 100) x 250 days = 150,000 rows = 4 pages of
  50,000 rows. Test (`test_year_of_600_bonds_in_a_few_calls`): 125,000 rows with yields in 3
  calls, about 3 s through the test client including gzip.
- Statement timeout 120 s per request.

## `GET /v1/history/index`

What exists, so a client knows what to request. One row per dataset / series. Cached in the
API process for 5 minutes. No parameters.

Columns: `dataset`, `series`, `source`, `unit`, `date_from`, `date_to`, `rows`, `days`,
`series_count`, `endpoint`.

| `dataset` | `series` | `unit` | notes |
|---|---|---|---|
| `bonds` | null | null | `md.bond_eod`: `series_count` = number of bonds (SECIDs) |
| `bond_yields` | null | null | `md.bond_eod_yield` (MOEX yields endpoint), served by `/v1/history/bonds` |
| `curves` | curve dataset (`ruonia_ois`, ...) | `pct` | one row per (dataset, source); `rows` = stored snapshots, `days` = days with a snapshot |
| `zcyc` | null | null | `rows` = publications, `days` = days |
| `fixings` | index code | `pct` / `index` | `md.fixing` per (code, source) and the served RUSFAR series |
| `fx` | `BASE/QUOTE` | kind: `cbr_official`, `moex_eod:<secid>:<board>`, `moex_indicative_14`, `moex_indicative_19` | |

Example rows (the examples on this page are actual responses for the test data set of
`tests/test_api_seed.py`, a few columns trimmed with `...` where noted):
`["bonds", null, "moex_iss", null, "2026-10-14", "2026-10-15", 5, 2, 3, "/v1/history/bonds"]`,
`["curves", "ruonia_ois", "ratescalcs", "pct", "2026-10-15", "2026-10-15", 2, 1, null, "/v1/history/curves/ruonia_ois"]`,
`["fixings", "RUSFARIND", "moex_iss", "index", "2026-10-15", "2026-10-15", 1, 1, null, "/v1/history/fixings?code=RUSFARIND"]`,
`["fx", "CNY/RUB", "moex_iss", "moex_eod:CNYRUB_TOM:CETS", "2026-10-15", "2026-10-15", 1, 1, null, "/v1/history/fx?kind=moex_eod&pair=CNY/RUB"]`.

`meta.limits` repeats the limits above.

## `GET /v1/history/bonds`

EOD history of bonds from `md.bond_eod` (ISS history) and `md.bond_eod_yield` (ISS yields)
joined on (trade_date, secid, board). The field names are those of the snapshot BondQuote
(contract 1.2, `source_analytics` flattened). One row per (trade_date, secid, board) that has a
history or a yields row; ordered by `secid, trade_date, board`.

| parameter | meaning |
|---|---|
| `secid` | comma list, at most 500, case-sensitive; default: every bond |
| `board` | `primary` (default): only the bond's main board = `ref.bond.marketprice_board`, else `primary_board` (the board the TDM export uses); `all`; or a comma list (`TQOB,TQCB`) |
| `from`, `to`, `fields`, `limit`, `cursor`, `known_at` | common |

Key columns (always): `trade_date`, `secid`, `board`. Columns:

| column | source | unit |
|---|---|---|
| `open_pct`, `low_pct`, `high_pct`, `close_pct`, `legal_close_pct`, `waprice_pct`, `market_price2_pct`, `market_price3_pct`, `admitted_quote_pct` | history | % of face, as MOEX publishes them |
| `accrued_per_bond` | history ACCINT | per bond, in `currency_id` |
| `face_value`, `face_unit`, `currency_id` | history | face per bond in `face_unit`; `SUR` = RUB, verbatim |
| `volume_bonds`, `value_ccy`, `num_trades`, `trading_session` | history | bonds; turnover in `currency_id` |
| `yield_close_pct` | history YIELDCLOSE | % p.a.: last trade to maturity even with an offer ahead |
| `yield_at_waprice_pct` | history | % p.a. |
| `effective_yield_pct`, `effective_yield_waprice_pct` | yields EFFECTIVEYIELD(WAPRICE) | % p.a. to `yield_date` |
| `yield_to_offer_pct` | yields, else history | % p.a. |
| `yield_date`, `yield_date_type` | yields | date and MOEX type (`MATDATE`, `OFFERDATE`, ...) |
| `duration_days` | yields, else history | days |
| `duration_waprice_days` | yields DURATIONWAPRICE | days |
| `zspread_bp` | yields ZSPREADBP, else history | bp, MOEX, vs KBD |
| `gspread_bp` | yields GSPREADBP | bp, MOEX, vs KBD |
| `yield_last_coupon_pct` | yields, else history | % p.a. |
| `icpi`, `ir`, `bei`, `cbr` | yields | MOEX fields verbatim (linker analytics), not interpreted |

A day without a row on the board is absent: no substitution from another board, no forward
fill. With `board=primary` and `known_at`, the main board is read as known at `known_at` too.

Example row (`fields` omitted):
`["2026-10-15", "SU26242RMFS0", "TQOB", null, null, null, 97.85, 97.82, 97.80, null, null, null, 1.23, 1000, "SUR", "SUR", null, 1000000.50, 120, null, 15.40, null, 15.42, 15.45, null, "2029-08-29", "MATDATE", null, 1000, null, 12, 15.2, 1.2345, 7.1, 5.9, 4.0]`

Request a year for 500 bonds, close and yield only:
`GET /v1/history/bonds?from=2025-10-21&to=2026-10-20&secid=SU26238RMFS4,...&fields=close_pct,effective_yield_pct,yield_date,accrued_per_bond&limit=50000`

## `GET /v1/history/curves/{dataset}`

Curve quote sets (`md.curve_snapshot` + `md.curve_quote`) of one dataset: `ruonia_ois`,
`key_rate_swap`, `cny_ruonia_xccy`, `cny_rusfar_ois`, `cny_futures_yields`,
`usd_futures_yields`, ... (unknown dataset: 404). One row per **quote** (tenor) of each
selected snapshot; ordered by `snapshot_date, source, quote_set_id, position, tenor`.

| parameter | meaning |
|---|---|
| `rule` | `eod` (default): per (`snapshot_date`, `source`) the latest snapshot of that Moscow day - the rule of the `eod_final` cut (with `known_at`: only snapshots with `fetched_at` and `ingested_at` <= `known_at`, exactly as the cut built at `known_at`); `all`: every stored snapshot (intraday history; snapshots are stored only on change and on the first poll of each day) |
| `source` | comma list (`otc_monitor`, `ratescalcs`, `andromeda`, ...); default all. Sources are separate series; pick per day as DESIGN §8 "Curve sources" describes |
| `from`, `to` | range of `snapshot_date` (Moscow date of `fetched_at`) |

Key columns: `snapshot_date`, `source`, `quote_set_id`, `position`, `tenor`. Columns:
`snapshot_date`, `source`, `quote_set_id` (snapshot id), `fetched_at`, `source_ts` (source
clock, may be null), `quote_basis` (`bid_ask_mid` / `mid_only`), `market_closed`, `label`,
`content_sha256`, `labels` (`market_closed`, `mid_only`, `source_clock_not_on_business_date`,
as in the snapshot), `meta` (snapshot meta JSON, e.g. andromeda `quality_flags`),
`hub_ingested_at`, `tenor`, `position`, `tenor_months`, `tenor_days`, `pillar_date`, `bid_pct`,
`mid_pct`, `ask_pct` (% p.a.; mid-only quotes have null bid/ask), `extra` (quote JSON).

Example row:
`["2026-10-15", "ratescalcs", 2, "2026-10-15T12:00:00+00:00", null, "bid_ask_mid", null, null, "000...001", [], null, "2026-10-15T12:00:00+00:00", "1Y", 0, 12, null, null, 15.2, 15.3, 15.4, null]`
(`content_sha256` trimmed)

A day without a row for a source means that source stored nothing on that day; for
`ruonia_ois` / `key_rate_swap` such days are also `ops.data_gap` rows of `curves_<dataset>`
(`/v1/gaps`).

## `GET /v1/history/zcyc`

MOEX KBD (`md.zcyc_snapshot`): parameters and standard yields; ordered by `trade_date,
trade_time`.

| parameter | meaning |
|---|---|
| `rule` | `eod` (default): one publication per day - the end-of-day one, else the latest of the day with label `not_end_of_day_publication` (the rule of EOD cuts); `all`: every stored publication |

Key columns: `trade_date`, `trade_time`. Columns: `trade_date`, `trade_time` (MSK, `HH:MM:SS`),
`source_ts`, `is_eod`, `labels`, `B1`, `B2`, `B3`, `T1`, `G1`..`G9` (as MOEX publishes them, G in
bp), `standard_yields` (`[{"tenor_years", "yield_pct"}]`, effective annual %),
`check_max_error_bp`, `check_tolerance_bp` (the hub's reproduction check), `hub_ingested_at`.

Example row:
`["2026-10-15", "18:54:59", "2026-10-15T15:54:59+00:00", true, [], 1506.334188, -389.027515, 526.946929, 1.970464, 2.773419, -2.589492, -2.023503, 2.190385, -3.402547, -3.217731, 0.560305, 0.0, 0.0, [{"yield_pct": 12.4175, "tenor_years": 0.25}, {"yield_pct": 12.9366, "tenor_years": 0.5}, ...], 0.001, 0.01, "2026-10-15T16:05:01+00:00"]`
(`standard_yields` trimmed; the keys of its objects come in the order `jsonb` stores them)

## `GET /v1/history/fixings`

`md.fixing` plus the RUSFAR family from `md.index_eod`; ordered by `index_code, source,
fixing_date`.

| parameter | meaning |
|---|---|
| `code` | comma list of index codes (upper-cased), at most 200; default all: `KEY_RATE`, `RUONIA`, `RUONIA_INDEX`, `TERM_RUONIA_1M/3M/6M`, `CBR_DEPOSIT_STANDING`, `CBR_LENDING_STANDING`, `CBR_REFINANCING_RATE`, `CPI_RU_MOM`, the RUSFAR series, ... (`/v1/history/index` lists them) |
| `source` | comma list (`cbr`, `rosstat`, `moex_iss`, ...) |

Key columns: `index_code`, `source`, `fixing_date`. Columns: `index_code`, `source`,
`fixing_date`, `value`, `unit` (`pct` = % p.a.; `index` = index level), `effective_date`,
`published_on`, `published_at` (only when the source states it; else null), `hub_ingested_at`
(when the hub first stored this value: `md.fixing.ingested_at`, `md.index_eod` `updated_at`
else `ingested_at`; not a publication time), `origin` (`md.fixing` / `md.index_eod`), `labels`
(`moex_marketdata_preliminary` for a same-day RUSFAR value later replaced by the history),
`details` (source extras JSON, e.g. RUONIA volume and deals).

Semantics as `/v1/fixings`: `KEY_RATE` per day in force (`fixing_date = effective_date`),
`RUONIA` per observation date (published next business day, `published_on`), term RUONIA and
`RUONIA_INDEX` per calendar day, `CPI_RU_MOM` = CPI of the month to the previous month in %
(`unit = index`, `fixing_date` = first day of the month).

RUSFAR (contract 1.2 rule): `RUSFAR`, `RUSFAR1W`, `RUSFAR2W`, `RUSFAR1M`, `RUSFAR3M`,
`RUSFARCNY`, `RUSFARCN1W`, `RUSFARRT`, `RUSFAR1WRT`, `RUSFAR2WRT`, `RUSFAR1MRT`, `RUSFAR3MRT`,
`RUSFARCNRT`, `RUSFARC1WR`, `RUSFARN`, `RUSFAR1WN`, `RUSFAR2WN`, `RUSFAR1MN`, `RUSFAR3MN`,
`RUSFARCNN`, `RUSFARC1WN` have `unit = pct`; `RUSFARIND` (accumulated return index) has
`unit = index`. Other `RUSFAR*` indices are not served; a requested one is listed in
`meta.not_served` with reason `rusfar_series_unit_unknown`.

Example rows:
`["RUONIA", "cbr", "2026-10-14", 16.32, "pct", null, "2026-10-15", null, "2026-10-15T12:00:00+00:00", "md.fixing", [], null]`,
`["RUSFARIND", "moex_iss", "2026-10-15", 2508.17, "index", null, null, null, "2026-10-15T16:00:00+00:00", "md.index_eod", [], null]`

## `GET /v1/history/fx`

RUB and cross rates; ordered by `kind, pair, date, secid, board`.

| parameter | meaning |
|---|---|
| `pair` | comma list of `BASE/QUOTE` (`USD/RUB`, `CNY/RUB`, `EUR/USD`) or a bare currency (`USD` = `USD/RUB`), at most 100; default all |
| `kind` | comma list of `cbr_official`, `moex_eod`, `moex_indicative_14`, `moex_indicative_19`; default all |
| `tenor` | `moex_eod` only: comma list (`TOD`, `TOM`) |

Key columns: `kind`, `pair`, `date`, `secid`, `board`. Columns: `kind`, `pair`, `base_ccy`,
`quote_ccy`, `date`, `rate`, `secid`, `board`, `tenor`, `close`, `waprice`, `quote_units`,
`close_per_unit`, `waprice_per_unit`, `num_trades`, `nominal`, `set_date`, `trade_time`,
`hub_ingested_at`.

| kind | rows | `date` | values |
|---|---|---|---|
| `cbr_official` | `md.fx_official` | effective date: **every calendar day** the rate is in force (weekends included) | `rate` = RUB per 1 unit (`nominal` = CBR quotation units, `set_date` = day CBR set it) |
| `moex_eod` | `md.fx_eod`, CETS **outrights** only (`ref.fx_instrument.kind = outright`; swaps never) | trade date | `close`, `waprice` = MOEX raw price per `quote_units` units of base; `close_per_unit`, `waprice_per_unit` = exact per-unit values when `quote_units` is a power of ten (else null); `rate` null |
| `moex_indicative_14` / `_19` | `md.fx_indicative` (`tc` 14:00 / `mc` 19:00 clearing) | trade date | `rate` = quote currency per 1 unit of base; `trade_time` `HH:MM` MSK |

Example rows:
`["cbr_official", "USD/RUB", "USD", "RUB", "2026-10-15", 81.1200, null, null, null, null, null, null, null, null, null, 1, "2026-10-14", null, "2026-10-14T12:30:00+00:00"]`,
`["moex_eod", "CNY/RUB", "CNY", "RUB", "2026-10-15", null, "CNYRUB_TOM", "CETS", "TOM", 11.3400, 11.3350, 1, 11.3400, 11.3350, 812, null, null, null, "2026-10-15T21:20:00+00:00"]`,
`["moex_indicative_19", "CNY/RUB", "CNY", "RUB", "2026-10-15", 11.35, null, null, null, null, null, null, null, null, null, null, null, "19:00", "2026-10-15T16:10:00+00:00"]`

## Typical client loop

```python
import json, httpx
from decimal import Decimal


def history(client: httpx.Client, path: str, **params):
    """All rows of a history query (pages joined); returns (columns, rows, data_version)."""
    rows, cursor = [], None
    while True:
        q = {**params, **({"cursor": cursor} if cursor else {})}
        r = client.get(path, params=q)  # client sends Authorization + gzip
        r.raise_for_status()
        body = json.loads(r.content, parse_float=Decimal)
        rows += body["rows"]
        cursor = body["next"]
        if cursor is None:
            return body["columns"], rows, body["data_version"]
```

Revalidation: keep the `ETag` of each page; on the next poll send `If-None-Match` and reuse the
cached page on `304`.
