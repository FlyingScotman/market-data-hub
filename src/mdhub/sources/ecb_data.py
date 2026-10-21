"""ECB Data Portal (``data-api.ecb.europa.eu``, SDMX 2.1 REST): fetch + parse, no DB.

Requests use ``format=csvdata&detail=dataonly``: one CSV row per observation with the columns
``KEY, <dimensions...>, TIME_PERIOD, OBS_VALUE`` (verified 2026-10-21). Days without a value are
absent (business frequency ``B``). Series used by the hub:

* ``EST/B.EU000A2X2A25.WT`` - euro short-term rate (€STR), volume-weighted trimmed mean, % p.a.,
  published at 08:00 CET on the next TARGET day; from 2019-10-01,
* ``EXR/D..EUR.SP00.A`` - the euro foreign exchange reference rates (the same values as
  ``www.ecb.europa.eu/stats/eurofxref``; fallback host of ``ecb_fx``), units per 1 EUR,
* ``YC/B.U2.EUR.4F.G_N_A.SV_C_YM.PY_<tenor>`` - par yields of the AAA euro area government
  yield curve (Svensson, all AAA-rated issuers), % p.a. (a **proxy**, not an OIS curve).

A range without observations answers 200 with an empty body (verified); some selections answer
HTTP 404 ``No results found`` instead - :func:`fetch` turns that into an empty body too, so a
range without data is "no data", not an error.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, HttpStatusError
from mdhub.core.redact import redact_url
from mdhub.sources.observations import Observation, check_range, parse_day, parse_value, unique

__all__ = [
    "BASE_URL",
    "ESTR_FLOW_KEY",
    "ESTR_SERIES",
    "EXR_FLOW_KEY",
    "PARSER_VERSION",
    "YC_PAR_TENORS",
    "estr_from_csv",
    "exr_from_csv",
    "fetch",
    "parse_csvdata",
    "yc_flow_key",
    "yc_from_csv",
]

BASE_URL: Final = "https://data-api.ecb.europa.eu/service/data"
PARSER_VERSION: Final = "1"
ESTR_FLOW_KEY: Final = "EST/B.EU000A2X2A25.WT"
ESTR_SERIES: Final = "EST.B.EU000A2X2A25.WT"
EXR_FLOW_KEY: Final = "EXR/D..EUR.SP00.A"
YC_PAR_TENORS: Final = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y")
_YC_PREFIX: Final = "YC.B.U2.EUR.4F.G_N_A.SV_C_YM."


def yc_flow_key(tenors: tuple[str, ...] = YC_PAR_TENORS) -> str:
    return "YC/B.U2.EUR.4F.G_N_A.SV_C_YM." + "+".join(f"PY_{t}" for t in tenors)


def parse_csvdata(payload: bytes, flow: str) -> list[Observation]:
    """``csvdata`` / ``dataonly`` rows of dataflow ``flow`` -> observations (``series`` = KEY)."""
    text = payload.decode("utf-8-sig")
    if not text.strip():
        return []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0] != "KEY" or "TIME_PERIOD" not in header or "OBS_VALUE" not in header:
        raise FormatDriftError(f"ECB Data Portal {flow}: unexpected header {header!r}")
    i_day, i_val = header.index("TIME_PERIOD"), header.index("OBS_VALUE")
    out: list[Observation] = []
    for row in reader:
        if not row:
            continue
        if len(row) != len(header):
            raise FormatDriftError(f"ECB Data Portal {flow}: row with {len(row)} cells")
        key = row[0]
        if not key.startswith(f"{flow}."):
            raise FormatDriftError(f"ECB Data Portal {flow}: foreign series {key!r}")
        if not row[i_val].strip():
            continue  # an observation without a value (status M) is not a value
        where = f"ECB Data Portal {key}"
        out.append(Observation(key, parse_day(row[i_day], where), parse_value(row[i_val], where)))
    return unique(out, f"ECB Data Portal {flow}")


def estr_from_csv(payload: bytes) -> list[Observation]:
    """€STR observations (% p.a.); fails closed on any other series or an implausible value."""
    obs = parse_csvdata(payload, "EST")
    foreign = {o.series for o in obs} - {ESTR_SERIES}
    if foreign:
        raise FormatDriftError(f"ECB Data Portal EST: unexpected series {sorted(foreign)}")
    check_range(obs, Decimal("-2"), Decimal("20"), "ECB €STR")
    return obs


def exr_from_csv(payload: bytes) -> list[Observation]:
    """Reference rates; ``series`` = the currency code (units of it per 1 EUR)."""
    out: list[Observation] = []
    for o in parse_csvdata(payload, "EXR"):
        parts = o.series.split(".")
        if len(parts) != 6 or parts[1] != "D" or parts[3:] != ["EUR", "SP00", "A"]:
            raise FormatDriftError(f"ECB Data Portal EXR: unexpected series {o.series!r}")
        if o.value <= 0:
            raise FormatDriftError(f"ECB Data Portal EXR: bad rate {o.series} {o.value}")
        out.append(Observation(parts[2], o.day, o.value))
    return unique(out, "ECB Data Portal EXR")


def yc_from_csv(payload: bytes) -> list[Observation]:
    """AAA par yields; ``series`` = the tenor (``10Y``), value % p.a."""
    out: list[Observation] = []
    for o in parse_csvdata(payload, "YC"):
        if not o.series.startswith(_YC_PREFIX + "PY_"):
            raise FormatDriftError(f"ECB Data Portal YC: unexpected series {o.series!r}")
        out.append(Observation(o.series.removeprefix(_YC_PREFIX + "PY_"), o.day, o.value))
    check_range(out, Decimal("-3"), Decimal("25"), "ECB AAA yield curve")
    return unique(out, "ECB Data Portal YC")


def fetch(http: HttpClient, flow_key: str, date_from: date, date_to: date) -> httpx.Response:
    """GET ``flow_key`` for ``[date_from, date_to]``; a 404 ``No results`` -> empty body."""
    resp = http.get(
        f"{BASE_URL}/{flow_key}",
        params={
            "format": "csvdata",
            "detail": "dataonly",
            "startPeriod": date_from.isoformat(),
            "endPeriod": date_to.isoformat(),
        },
        raise_for_status=False,
    )
    if resp.status_code == 404 and "no results" in resp.text.casefold():
        return httpx.Response(200, content=b"", request=resp.request)
    if not resp.is_success:
        raise HttpStatusError(resp.status_code, url=redact_url(resp.request.url))
    return resp
