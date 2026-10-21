"""DBnomics (``api.db.nomics.world``, no key): mirror of the ECB €STR series - fetch + parse.

``GET /v22/series/ECB/EST/B.EU000A2X2A25.WT?observations=1`` returns
``{"series": {"docs": [{"provider_code": "ECB", "dataset_code": "EST", "series_code": ...,
"period": [...], "period_start_day": ["YYYY-MM-DD", ...], "value": [number | "NA" | null]}]}}``
(parallel arrays, the whole history in one answer; ``"NA"`` = no value). The host does not answer
from the development environment: the shape follows the API documentation and public client
code, not a response recorded by the hub - check with ``mdhub probe-sources`` on the server.
A mirror may lag the ECB by a day or more.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.observations import Observation, check_range, parse_day, parse_value, unique

__all__ = ["BASE_URL", "ESTR_PATH", "PARSER_VERSION", "estr_from_json", "fetch_estr"]

BASE_URL: Final = "https://api.db.nomics.world/v22"
ESTR_PATH: Final = "/series/ECB/EST/B.EU000A2X2A25.WT"
ESTR_SERIES: Final = "EST.B.EU000A2X2A25.WT"
PARSER_VERSION: Final = "1"


def estr_from_json(payload: bytes) -> list[Observation]:
    try:
        body: Any = json.loads(payload)
    except ValueError as exc:
        raise FormatDriftError(f"DBnomics €STR: not JSON ({exc})") from None
    docs = body.get("series", {}).get("docs") if isinstance(body, dict) else None
    if not isinstance(docs, list) or len(docs) != 1:
        raise FormatDriftError("DBnomics €STR: expected exactly one series document")
    doc = docs[0]
    if (doc.get("provider_code"), doc.get("dataset_code")) != ("ECB", "EST") or doc.get(
        "series_code"
    ) != "B.EU000A2X2A25.WT":
        raise FormatDriftError(f"DBnomics €STR: unexpected series {doc.get('series_code')!r}")
    days, values = doc.get("period_start_day"), doc.get("value")
    if not isinstance(days, list) or not isinstance(values, list) or len(days) != len(values):
        raise FormatDriftError("DBnomics €STR: period_start_day / value missing or unequal")
    out: list[Observation] = []
    for d, v in zip(days, values, strict=True):
        if v is None or v == "NA":
            continue
        if isinstance(v, bool) or not isinstance(v, int | float | str):
            raise FormatDriftError(f"DBnomics €STR {d}: bad value {v!r}")
        where = f"DBnomics €STR {d}"
        out.append(Observation(ESTR_SERIES, parse_day(str(d), where), parse_value(str(v), where)))
    check_range(out, Decimal("-2"), Decimal("20"), "DBnomics €STR")
    return unique(out, "DBnomics €STR")


def fetch_estr(http: HttpClient) -> httpx.Response:
    return http.get(f"{BASE_URL}{ESTR_PATH}", params={"observations": "1"}, expect_json=True)
