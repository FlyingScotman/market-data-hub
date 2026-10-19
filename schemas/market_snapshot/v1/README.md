# Market Snapshot Contract v1

The hub (market-data-hub) owns this contract: JSON Schema (draft 2020-12) of the immutable
snapshots it publishes, plus golden fixtures. The system outline is
`trading_desk_monitor/docs/architecture/02_MARKET_DATA_AND_CORE_CONTRACT.md` §3; hub internals
are in [`docs/DESIGN.md`](../../../docs/DESIGN.md) §11.

| File | What |
|---|---|
| `snapshot.schema.json` | Entry point: any bundle (`oneOf` static / market) |
| `static.schema.json` | Bundle of kind `static` |
| `market.schema.json` | Bundle of kind `market` |
| `manifest.schema.json` | Manifest = envelope without `data`, plus `snapshot_id` (`GET /v1/snapshots/{id}/manifest`) |
| `common.schema.json` | Shared definitions: decimal strings, dates, UTC instants, sha256, labels |
| `fixtures/` | Golden bundles built from public data (`index.json` lists their ids) |

`schema_version` is `market_snapshot/1.0`. Every schema object has
`additionalProperties: false`: a new field is a schema version bump, so a consumer pinned to
an older version fails its conformance tests instead of silently ignoring it.

## Identity

* The **bundle** is one JSON document: the envelope fields plus `data`.
* Canonical form: keys sorted, no whitespace (`,` `:`), UTF-8 without escaping non-ASCII,
  decimals as strings, dates `YYYY-MM-DD`, instants UTC `...Z`.
* `snapshot_id = sha256(canonical bundle)`. The bundle endpoint serves the deterministic gzip
  of exactly those bytes (`ETag: "<snapshot_id>"`). A consumer verifies
  `sha256(gunzip(body)) == snapshot_id`; replicas can serve the bytes unchanged.
* Snapshots are immutable. A correction is a new snapshot of the same cut
  (`kind`, `cut_type`, `business_date`, `cut_ts`) whose `supersedes` names the earlier one.
  For `daily`, `eod_preliminary` and `eod_final`, `cut_ts` depends only on the business date
  (the configured cut time; `eod_final`: on the next business day), so at most one snapshot
  per such cut and business date is not superseded; `intraday` and `change` can have several.

## Envelope

| Field | Meaning |
|---|---|
| `kind` | `static` or `market` |
| `cut_type` | market: `eod_preliminary`, `eod_final`, `intraday`; static: `daily` (scheduled) or `change` (static data changed since the last static snapshot) |
| `business_date` | Moscow business date |
| `cut_ts` | Nominal UTC instant of the cut |
| `known_at` | UTC instant: no record the hub learned after it is included (as-known-at reads) |
| `static_snapshot_id` | market only: the static snapshot it is consistent with; quotes exist only for its instruments |
| `supersedes` | earlier snapshot of the same cut that this one corrects |
| `producer` | `market-data-hub` and its version |
| `parameters` | builder parameters (universe rule, lookback windows) |
| `calendar` | one calendar `version` (latest calendar change time) and `fingerprint` per snapshot; a market snapshot repeats its static snapshot's |
| `sources[]` | per dataset and hub source: `ops.job_run` ids of the included rows, `ingested_from`/`ingested_to` (hub ingest-time range; deterministic under raw-capture retention) |
| `datasets[]` | `name`, `rows`, `sha256` (of the canonical `data.<name>` array), `coverage` (dataset-specific facts), `stale` + `stale_reasons` |
| `excluded[]` | records left out with `dataset`, `key`, `reason` |

**A record is never dropped silently**: it is in `data` (possibly with `labels`) or in
`excluded`. Exclusion reasons used by v1: `not_in_static_snapshot` (a quote of a SECID outside
the static universe), `fx_swap`, `not_an_outright`, `non_rub_pair`, `precious_metal`,
`instrument_unknown` (MOEX FX rows that are not RUB per unit of a currency).

## Units and values

* Decimals are strings with the exact stored digits (`"14.500"` and `"14.5"` are different
  facts). Never parse them as binary floats.
* Units in names: `_pct` percent (14.5 = 14.5 %), `_bp` basis points, `_per_bond` currency
  amount per bond (currency in `face_unit` or `currency_id` as documented per field),
  `_rub_per_unit` RUB per 1 unit of the foreign currency.
* Bond prices are % of face, as MOEX publishes them; every price basis is a separate field.
  MOEX analytics (yields, durations, spreads) sit under `source_analytics`.
* Quote-set (`dataset`) and tenor codes are hub facts. Conventions (day count, frequency,
  tenor -> date) belong to the consumer's curve-spec registry.
* Missing is `null` or no row, never zero; nothing is forward-filled. Dated datasets carry each
  row's own date inside an explicit window (`coverage.window_*`).

## Static objects (`data`)

| Array | Object | Notes |
|---|---|---|
| `instruments` | Instrument | `ref.bond` + boards. Universe: `active`, plus `matured` with maturity within `parameters.matured_lookback_days`. `labels`: `coupon_type_unknown`, `perpetual`, `perpetual_no_call_date`, `day_count_not_published`, `floater_formula_missing`, `floater_formula_partial`, `coupon_schedule_empty`, `no_issuer`, `structured`, `matured` |
| `coupon_periods` | CouponPeriod | `status`: only `known` is a fact; `current_published`, `projected_by_moex`, `unknown` are MOEX projections / gaps |
| `redemptions` | Redemption | `kind` amortization / maturity |
| `offers` | Offer | every source; `labels`: `date_missing` |
| `floater_formulas` | FloaterFormula | resolved formula; `benchmark` and `benchmark_raw` verbatim, `status` curated / inferred / partial |
| `issuers` | Issuer | |
| `calendars` | Calendar | from `ref.calendar_effective` only (MOEX weekend sessions are **not** business days): `valid_from`..`valid_to` (outside: unknown), `holidays`, `working_weekends`, `provisional_from` (first day decided by an unverified curated row), `version`, `fingerprint` |
| `id_map` | IdMap | `isin_shared_with`: other SECIDs with the same ISIN (an ISIN join is ambiguous when non-empty) |

## Market objects (`data`)

| Array | Object | Notes |
|---|---|---|
| `bond_quotes` | BondQuote (EOD) | `md.bond_eod` + `md.bond_eod_yield` of the business date; empty in intraday cuts |
| `bond_quotes_intraday` | BondQuote (intraday) | latest stored quote per (secid, board) on the business date; empty in EOD cuts |
| `curve_quote_sets` | CurveQuoteSet | latest snapshot per (source, dataset) fetched on the business date; `labels`: `mid_only`, `market_closed`, `source_clock_not_on_business_date` |
| `zcyc_params` | ZcycParams | EOD cuts: the end-of-day publication (else the latest, flagged stale); intraday: the latest |
| `fixings` | Fixing | `md.fixing` and the RUSFAR family from `md.index_eod` (`origin`) |
| `fx_rates` | FxRate | RUB per 1 unit: `cbr_official`, `moex_eod` (raw price, `quote_units`, exact per-unit value), `moex_indicative_14` / `_19` |
| `cross_rates` | CrossRate | ECB, units of `ccy` per 1 EUR |
| `cbr_meetings` | CbrMeeting | |

## Fixtures

`fixtures/static_daily.json` and `fixtures/market_eod_final.json` are built by the hub's
builders from public data of 2026-10-15 for ten bonds: OFZ-PD bullet, OFZ-PK floater (RUONIA),
OFZ-IN linker, OFZ-AD amortizer, a corporate key-rate floater, a corporate with a scheduled put
offer, a corporate amortizer, two perpetuals and a bond of unknown coupon type (`index.json`).
They are pretty-printed; their id is the sha256 of their canonical form. The market fixture uses
a 14-day fixing window (see its `parameters`) to stay reviewable.

Hub CI (`tests/test_snapshot_golden.py`) validates them and rebuilds them byte for byte from
`tests/fixtures/snapshot/source_rows.json.gz`. They change only in a reviewed change
(`MDHUB_REGEN_GOLDEN=1 uv run pytest tests/test_snapshot_golden.py`); a contract change bumps
`schema_version`.

## Validating

```python
import json, pathlib
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

root = pathlib.Path("schemas/market_snapshot/v1")
docs = [json.loads(p.read_text()) for p in root.glob("*.schema.json")]
registry = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)
schema = registry.contents(
    "https://schemas.market-data-hub.invalid/market_snapshot/v1/snapshot.schema.json"
)
Draft202012Validator(schema, registry=registry).validate(bundle)
```
