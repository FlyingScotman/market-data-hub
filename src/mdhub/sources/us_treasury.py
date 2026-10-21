"""U.S. Treasury daily par yield curve rates (``home.treasury.gov``): fetch + parse, no DB.

``/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve``
with ``field_tdr_date_value_month=YYYYMM`` (one month) or ``field_tdr_date_value=YYYY`` (one
year): an Atom feed whose entries carry ``m:properties`` with ``d:NEW_DATE`` and
``d:BC_1MONTH`` .. ``d:BC_30YEAR`` (% p.a., bond-equivalent par yields of on-the-run securities,
read from indicative bid-side quotes at about 15:30 New York time). Tenors appear over time
(``BC_1_5MONTH`` since 2025-02, ``BC_4MONTH`` since 2022-10, 20Y since 1993-10, 2M since 2018-10)
and ``BC_30YEARDISPLAY`` repeats the 30Y: every ``BC_*`` element of :data:`TENORS` that has a
value becomes a quote; an unknown ``BC_*`` element fails closed (format drift).

This is a **proxy** of a USD rates curve (Treasury par yields, not SOFR OIS).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.cbr import safe_xml_fromstring

__all__ = [
    "PARSER_VERSION",
    "TENORS",
    "XML_URL",
    "ParCurve",
    "fetch_month",
    "fetch_year",
    "parse_xml",
]

XML_URL: Final = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
PARSER_VERSION: Final = "1"
TENORS: Final = {
    "BC_1MONTH": ("1M", 1),
    "BC_1_5MONTH": ("6W", None),
    "BC_2MONTH": ("2M", 2),
    "BC_3MONTH": ("3M", 3),
    "BC_4MONTH": ("4M", 4),
    "BC_6MONTH": ("6M", 6),
    "BC_1YEAR": ("1Y", 12),
    "BC_2YEAR": ("2Y", 24),
    "BC_3YEAR": ("3Y", 36),
    "BC_5YEAR": ("5Y", 60),
    "BC_7YEAR": ("7Y", 84),
    "BC_10YEAR": ("10Y", 120),
    "BC_20YEAR": ("20Y", 240),
    "BC_30YEAR": ("30Y", 360),
}
"""Element -> (tenor code, tenor in months; ``None`` = 6 weeks, ``tenor_days`` 42)."""
_IGNORED: Final = frozenset({"BC_30YEARDISPLAY"})
_A: Final = "{http://www.w3.org/2005/Atom}"
_M: Final = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
_D: Final = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"


@dataclass(frozen=True, slots=True)
class ParCurve:
    day: date
    quotes: tuple[tuple[str, Decimal], ...]
    """``(element, yield_pct)`` in :data:`TENORS` order."""


def parse_xml(payload: bytes) -> list[ParCurve]:
    try:
        root = safe_xml_fromstring(payload)
    except ET.ParseError as exc:
        raise FormatDriftError(f"Treasury XML: {exc}") from None
    if root.tag != f"{_A}feed":
        raise FormatDriftError(f"Treasury XML: root {root.tag!r} is not an Atom feed")
    out: dict[date, ParCurve] = {}
    order = list(TENORS)
    for entry in root.iter(f"{_A}entry"):
        props = entry.find(f"{_A}content/{_M}properties")
        if props is None:
            raise FormatDriftError("Treasury XML: entry without m:properties")
        new_date = props.find(f"{_D}NEW_DATE")
        if new_date is None or not new_date.text:
            raise FormatDriftError("Treasury XML: entry without NEW_DATE")
        try:
            day = date.fromisoformat(new_date.text[:10])
        except ValueError:
            raise FormatDriftError(f"Treasury XML: bad NEW_DATE {new_date.text!r}") from None
        quotes: list[tuple[str, Decimal]] = []
        for el in props:
            name = el.tag.removeprefix(_D)
            if not name.startswith("BC_") or name in _IGNORED:
                continue
            if name not in TENORS:
                raise FormatDriftError(f"Treasury XML {day}: unknown tenor element {name}")
            if el.attrib.get(f"{_M}null") == "true" or not (el.text or "").strip():
                continue
            try:
                value = Decimal(el.text.strip())  # type: ignore[union-attr]
            except InvalidOperation:
                raise FormatDriftError(
                    f"Treasury XML {day} {name}: bad value {el.text!r}"
                ) from None
            if not Decimal("-2") <= value <= Decimal("25"):
                raise FormatDriftError(f"Treasury XML {day} {name}: value out of range {value}")
            quotes.append((name, value))
        if not quotes:
            raise FormatDriftError(f"Treasury XML {day}: no yields")
        if day in out:
            raise FormatDriftError(f"Treasury XML: duplicate date {day}")
        quotes.sort(key=lambda q: order.index(q[0]))
        out[day] = ParCurve(day, tuple(quotes))
    return [out[d] for d in sorted(out)]


def fetch_month(http: HttpClient, year: int, month: int) -> httpx.Response:
    return http.get(
        XML_URL,
        params={
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value_month": f"{year:04d}{month:02d}",
        },
    )


def fetch_year(http: HttpClient, year: int) -> httpx.Response:
    return http.get(
        XML_URL, params={"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)}
    )
