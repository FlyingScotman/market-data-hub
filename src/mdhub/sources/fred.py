"""FRED (Federal Reserve Bank of St. Louis) graph CSV, no API key: fetch + parse, no DB.

``GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=A,B&cosd=YYYY-MM-DD&coed=YYYY-MM-DD``
returns ``observation_date,A,B`` then one line per date; a missing value is an empty field (older
exports: header ``DATE`` and ``.``) - both are accepted. (The JSON API
``api.stlouisfed.org/fred/series/observations`` needs a free ``api_key``; the hub does not use it.)
FRED does not answer from the development environment: the format follows FRED's documented
export and committed downloads, not a response recorded by the hub - check with
``mdhub probe-sources`` on the server.

Series used: ``SOFR`` (NY Fed), ``ECBESTRVOLWGTTRMDMNRT`` (€STR, ECB), H.10 noon buying rates
``DEXUSEU`` (USD per EUR), ``DEXUSUK`` (USD per GBP), ``DEXCHUS`` (CNY per USD), ``DEXJPUS``
(JPY per USD), ``DEXSZUS`` (CHF per USD), ``DEXHKUS`` (HKD per USD), ``DEXINUS`` (INR per USD).
Values are rounded to the administrator's published decimals (:data:`DECIMALS`): some exports
carry binary float noise (``-0.54899999999999993``).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Final

import httpx

from mdhub.core.decimalutil import quantize
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.observations import Observation, parse_day, parse_value, unique

__all__ = [
    "BASE_URL",
    "DECIMALS",
    "ESTR_SERIES",
    "H10_PAIRS",
    "PARSER_VERSION",
    "SOFR_AVERAGES",
    "SOFR_SERIES",
    "check_positive",
    "fetch",
    "parse_csv",
]

BASE_URL: Final = "https://fred.stlouisfed.org/graph/fredgraph.csv"
PARSER_VERSION: Final = "1"
SOFR_SERIES: Final = "SOFR"
ESTR_SERIES: Final = "ECBESTRVOLWGTTRMDMNRT"
H10_PAIRS: Final = {
    "DEXUSEU": ("EUR", "USD"),
    "DEXUSUK": ("GBP", "USD"),
    "DEXCHUS": ("USD", "CNY"),
    "DEXJPUS": ("USD", "JPY"),
    "DEXSZUS": ("USD", "CHF"),
    "DEXHKUS": ("USD", "HKD"),
    "DEXINUS": ("USD", "INR"),
}
"""H.10 series -> (base, quote): the rate is units of quote per 1 base."""
SOFR_AVERAGES: Final = {
    "SOFR30DAYAVG": ("SOFR30DAVG", "pct"),
    "SOFR90DAYAVG": ("SOFR90DAVG", "pct"),
    "SOFR180DAYAVG": ("SOFR180DAVG", "pct"),
    "SOFRINDEX": ("SOFRINDEX", "index"),
}
"""FRED series of the NY Fed SOFR averages and index -> (hub index code, unit)."""
DECIMALS: Final = {
    SOFR_SERIES: 2,
    ESTR_SERIES: 3,
    "SOFR30DAYAVG": 5,
    "SOFR90DAYAVG": 5,
    "SOFR180DAYAVG": 5,
    "SOFRINDEX": 8,
    **dict.fromkeys(H10_PAIRS, 4),
}
_MISSING: Final = frozenset({"", "."})


def parse_csv(payload: bytes, series: tuple[str, ...]) -> list[Observation]:
    """``fredgraph.csv`` with exactly the columns ``series`` -> observations."""
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    if not rows:
        raise FormatDriftError("FRED: empty CSV")
    header = [h.strip() for h in rows[0]]
    if header[:1] not in (["observation_date"], ["DATE"]) or tuple(header[1:]) != series:
        raise FormatDriftError(f"FRED: unexpected header {header!r} (expected {series!r})")
    out: list[Observation] = []
    for row in rows[1:]:
        if not row:
            continue
        if len(row) != len(header):
            raise FormatDriftError(f"FRED: row with {len(row)} cells: {row!r}")
        day = parse_day(row[0], "FRED")
        for sid, cell in zip(series, row[1:], strict=True):
            if cell.strip() in _MISSING:
                continue
            value = parse_value(cell, f"FRED {sid} {day}")
            places = DECIMALS.get(sid)
            out.append(Observation(sid, day, quantize(value, places) if places else value))
    return unique(out, "FRED")


def check_positive(obs: list[Observation]) -> None:
    for o in obs:
        if o.value <= 0:
            raise FormatDriftError(f"FRED {o.series} {o.day}: non-positive rate {o.value}")


def fetch(
    http: HttpClient, series: tuple[str, ...], date_from: date, date_to: date
) -> httpx.Response:
    return http.get(
        BASE_URL,
        params={
            "id": ",".join(series),
            "cosd": date_from.isoformat(),
            "coed": date_to.isoformat(),
        },
    )
