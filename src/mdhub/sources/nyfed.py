"""Federal Reserve Bank of New York Markets API (``markets.newyorkfed.org``): SOFR, the SOFR
averages and the SOFR index - fetch + parse, no DB. The NY Fed administers SOFR.

* ``/api/rates/secured/sofr/search.json?startDate=&endDate=`` -> ``{"refRates": [{
  "effectiveDate", "type": "SOFR", "percentRate", "percentPercentile1/25/75/99",
  "volumeInBillions", "revisionIndicator"}]}`` (newest first), from 2018-04-02,
* ``/api/rates/secured/sofrai/search.json`` -> ``type = "SOFRAI"``: ``average30day``,
  ``average90day``, ``average180day`` (% p.a.) and ``index`` (SOFR index, 2018-04-02 = 1),
  from 2020-03-02.

SOFR for a U.S. government securities business day D is published at about 08:00 New York time
on D+1; ``revisionIndicator`` = ``"Y"`` marks a revised rate (revisions are published the same
day by 14:30 ET). Numbers are JSON numbers: converted through their text, never float.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.observations import parse_day

__all__ = [
    "BASE_URL",
    "PARSER_VERSION",
    "SOFRAI_FIELDS",
    "SofrRate",
    "fetch",
    "parse_sofr",
    "parse_sofrai",
]

BASE_URL: Final = "https://markets.newyorkfed.org/api/rates/secured"
PARSER_VERSION: Final = "1"
SOFR_FIELDS: Final = (
    "percentPercentile1",
    "percentPercentile25",
    "percentPercentile75",
    "percentPercentile99",
    "volumeInBillions",
)
SOFRAI_FIELDS: Final = {
    "average30day": ("SOFR30DAVG", "pct"),
    "average90day": ("SOFR90DAVG", "pct"),
    "average180day": ("SOFR180DAVG", "pct"),
    "index": ("SOFRINDEX", "index"),
}
"""``sofrai`` field -> (hub index code, unit)."""


@dataclass(frozen=True, slots=True)
class SofrRate:
    index_code: str
    day: date
    value: Decimal
    unit: str
    revised: bool
    details: dict[str, str]


def _number(text: str, where: str) -> Decimal:
    try:
        value = Decimal(text)
    except ArithmeticError:
        raise FormatDriftError(f"NY Fed {where}: not a number {text!r}") from None
    if not value.is_finite():
        raise FormatDriftError(f"NY Fed {where}: not a number {text!r}")
    return value


def _rates(payload: bytes, kind: str) -> list[dict[str, Any]]:
    try:
        # numbers as Decimal through their text: 3.88 stays 3.88
        body: Any = json.loads(payload, parse_float=str, parse_int=str)
    except ValueError as exc:
        raise FormatDriftError(f"NY Fed {kind}: not JSON ({exc})") from None
    rates = body.get("refRates") if isinstance(body, dict) else None
    if not isinstance(rates, list):
        raise FormatDriftError(f"NY Fed {kind}: no refRates list")
    seen: set[str] = set()
    for r in rates:
        if not isinstance(r, dict) or r.get("type") != kind or "effectiveDate" not in r:
            raise FormatDriftError(f"NY Fed {kind}: unexpected entry {r!r}")
        if r["effectiveDate"] in seen:
            raise FormatDriftError(f"NY Fed {kind}: duplicate {r['effectiveDate']}")
        seen.add(r["effectiveDate"])
    return rates


def parse_sofr(payload: bytes) -> list[SofrRate]:
    """SOFR rows (``index_code = SOFR``, % p.a.) sorted by date; details: percentiles, volume."""
    out: list[SofrRate] = []
    for r in _rates(payload, "SOFR"):
        day = parse_day(str(r["effectiveDate"]), "NY Fed SOFR")
        if r.get("percentRate") is None:
            raise FormatDriftError(f"NY Fed SOFR {day}: percentRate missing")
        value = _number(str(r["percentRate"]), f"SOFR {day}")
        if not Decimal("-2") <= value <= Decimal("25"):
            raise FormatDriftError(f"NY Fed SOFR {day}: value out of range {value}")
        details = {k: str(r[k]) for k in SOFR_FIELDS if r.get(k) not in (None, "")}
        revised = str(r.get("revisionIndicator") or "").upper() == "Y"
        if revised:
            details["revision_indicator"] = "Y"
        out.append(SofrRate("SOFR", day, value, "pct", revised, details))
    return sorted(out, key=lambda x: x.day)


def parse_sofrai(payload: bytes) -> list[SofrRate]:
    """SOFR averages (% p.a.) and the SOFR index (level), one row per field and date."""
    out: list[SofrRate] = []
    for r in _rates(payload, "SOFRAI"):
        day = parse_day(str(r["effectiveDate"]), "NY Fed SOFRAI")
        revised = str(r.get("revisionIndicator") or "").upper() == "Y"
        for field, (code, unit) in SOFRAI_FIELDS.items():
            if r.get(field) in (None, ""):
                raise FormatDriftError(f"NY Fed SOFRAI {day}: {field} missing")
            value = _number(str(r[field]), f"SOFRAI {field} {day}")
            lo, hi = (Decimal("0.5"), Decimal("10")) if unit == "index" else (-2, 25)
            if not Decimal(lo) <= value <= Decimal(hi):
                raise FormatDriftError(f"NY Fed SOFRAI {field} {day}: value out of range {value}")
            details = {"revision_indicator": "Y"} if revised else {}
            out.append(SofrRate(code, day, value, unit, revised, details))
    return sorted(out, key=lambda x: (x.index_code, x.day))


def fetch(http: HttpClient, rate: str, date_from: date, date_to: date) -> httpx.Response:
    """``rate`` = ``sofr`` or ``sofrai``."""
    return http.get(
        f"{BASE_URL}/{rate}/search.json",
        params={"startDate": date_from.isoformat(), "endDate": date_to.isoformat()},
        expect_json=True,
    )
