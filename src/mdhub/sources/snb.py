"""Swiss National Bank data portal (``data.snb.ch``): SARON - fetch + parse, no DB.

``GET https://data.snb.ch/api/cube/snbgwdzid/data/csv/en?fromDate=&toDate=&dimSel=D0(SARON)``
(cube ``snbgwdzid``, "Official interest rates": the SNB policy rate and SARON, daily) returns a
semicolon-separated CSV with quoted fields: a short preamble (``"CubeId";"snbgwdzid"``,
``"PublishingDate";"YYYY-MM-DD HH:MM"``), an empty line, then the header ``"Date";"D0";"Value"``
and one line per (day, series); a day without a value has an empty ``Value``. SARON (Swiss
Average Rate Overnight, % p.a.) is administered by SIX and fixed at the close of each Zurich
business day (the SNB republishes the fixing); ``D0 = SARON`` selects it.

data.snb.ch does not answer from the development environment: the format follows the SNB data
portal's documented CSV export, not a response recorded by the hub (the test fixture is labelled
as such) - check with ``mdhub probe-sources`` on the server before relying on it. A different
layout, a missing ``SARON`` series in a non-empty answer, a duplicate day or a value outside
:data:`SARON_RANGE_PCT` fails closed (:class:`~mdhub.core.errors.FormatDriftError`).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.observations import Observation, check_range, parse_day, parse_value, unique

__all__ = [
    "BASE_URL",
    "CUBE",
    "PARSER_VERSION",
    "SARON_RANGE_PCT",
    "SARON_SERIES",
    "fetch",
    "parse_csv",
]

BASE_URL: Final = "https://data.snb.ch/api/cube"
CUBE: Final = "snbgwdzid"
SARON_SERIES: Final = "SARON"
PARSER_VERSION: Final = "1"
SARON_RANGE_PCT: Final = (Decimal(-5), Decimal(20))
"""Plausible SARON values (% p.a.); outside -> unit or column drift."""
_HEADER: Final = ("Date", "D0", "Value")


def parse_csv(payload: bytes, series: str = SARON_SERIES) -> list[Observation]:
    """SNB cube CSV -> observations of ``series`` (``D0``) with a value, sorted by day."""
    text = payload.decode("utf-8-sig")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=";") if any(c.strip() for c in r)]
    if not rows or rows[0][:1] != ["CubeId"]:
        raise FormatDriftError("SNB: not a data portal CSV (no CubeId preamble)")
    if len(rows[0]) < 2 or rows[0][1].strip() != CUBE:
        raise FormatDriftError(f"SNB: cube {rows[0][1:2]!r}, expected {CUBE!r}")
    try:
        start = next(i for i, r in enumerate(rows) if tuple(c.strip() for c in r) == _HEADER)
    except StopIteration:
        raise FormatDriftError(f"SNB: no header {_HEADER!r}") from None
    out: list[Observation] = []
    codes: set[str] = set()
    for row in rows[start + 1 :]:
        if len(row) != len(_HEADER):
            raise FormatDriftError(f"SNB: row with {len(row)} cells: {row!r}")
        day_text, code, value = (c.strip() for c in row)
        codes.add(code)
        if code != series or not value:
            continue
        day = parse_day(day_text, "SNB")
        out.append(Observation(series, day, parse_value(value, f"SNB {series} {day}")))
    if codes and series not in codes:
        raise FormatDriftError(f"SNB: series {series!r} not in the answer (D0 {sorted(codes)!r})")
    check_range(out, *SARON_RANGE_PCT, "SNB")
    return unique(out, "SNB")


def fetch(
    http: HttpClient, date_from: date, date_to: date, series: str = SARON_SERIES
) -> httpx.Response:
    return http.get(
        f"{BASE_URL}/{CUBE}/data/csv/en",
        params={
            "fromDate": date_from.isoformat(),
            "toDate": date_to.isoformat(),
            "dimSel": f"D0({series})",
        },
    )
