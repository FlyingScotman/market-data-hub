"""ECB euro foreign exchange reference rates (eurofxref): fetch + parse, no DB.

* ``eurofxref-daily.xml``   - the latest reference day (published ~16:00 CET on TARGET days)
* ``eurofxref-hist-90d.xml`` - the last 90 days, same XML shape
* ``eurofxref-hist.zip``    - full history since 1999 as one CSV (``Date,USD,JPY,...``,
  ``N/A`` for currencies without a rate that day)

Rates are units of currency per 1 EUR, as :class:`~decimal.Decimal` parsed from the text.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.cbr import safe_xml_fromstring

__all__ = [
    "DAILY_URL",
    "HIST_90D_URL",
    "HIST_ZIP_URL",
    "PARSER_VERSION",
    "EcbRate",
    "fetch",
    "parse_hist_zip",
    "parse_xml",
]

BASE: Final = "https://www.ecb.europa.eu/stats/eurofxref/"
DAILY_URL: Final = BASE + "eurofxref-daily.xml"
HIST_90D_URL: Final = BASE + "eurofxref-hist-90d.xml"
HIST_ZIP_URL: Final = BASE + "eurofxref-hist.zip"
PARSER_VERSION: Final = "1"

_NS: Final = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
_CCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class EcbRate:
    rate_date: date
    ccy: str
    rate_per_eur: Decimal


def _rate(text: str, where: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise FormatDriftError(f"ECB {where}: bad rate {text!r}") from None
    if not value.is_finite() or value <= 0:
        raise FormatDriftError(f"ECB {where}: bad rate {text!r}")
    return value


def parse_xml(payload: bytes) -> list[EcbRate]:
    """``eurofxref-daily.xml`` / ``eurofxref-hist-90d.xml`` -> rates."""
    try:
        root = safe_xml_fromstring(payload)
    except ET.ParseError as exc:
        raise FormatDriftError(f"ECB XML: {exc}") from None
    out: list[EcbRate] = []
    days = [c for c in root.iter(f"{_NS}Cube") if "time" in c.attrib]
    if not days:
        raise FormatDriftError("ECB XML: no Cube[@time]")
    seen: set[tuple[date, str]] = set()
    for day in days:
        try:
            d = date.fromisoformat(day.attrib["time"])
        except ValueError:
            raise FormatDriftError(f"ECB XML: bad time {day.attrib['time']!r}") from None
        cubes = list(day)
        if not cubes:
            raise FormatDriftError(f"ECB XML {d}: no rates")
        for c in cubes:
            ccy = c.attrib.get("currency", "")
            if not _CCY.match(ccy) or "rate" not in c.attrib:
                raise FormatDriftError(f"ECB XML {d}: bad cube {c.attrib!r}")
            if (d, ccy) in seen:
                raise FormatDriftError(f"ECB XML: duplicate {d} {ccy}")
            seen.add((d, ccy))
            out.append(EcbRate(d, ccy, _rate(c.attrib["rate"], f"{d} {ccy}")))
    return out


def parse_hist_zip(
    payload: bytes, *, date_from: date | None = None, date_to: date | None = None
) -> list[EcbRate]:
    """``eurofxref-hist.zip`` -> rates in ``[date_from, date_to]`` (``N/A`` skipped)."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".csv")]
            if len(names) != 1:
                raise FormatDriftError(f"ECB zip: expected one CSV, got {names!r}")
            text = zf.read(names[0]).decode("utf-8-sig")
    except zipfile.BadZipFile as exc:
        raise FormatDriftError(f"ECB zip: {exc}") from None
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0] != "Date":
        raise FormatDriftError(f"ECB CSV: unexpected header {header!r}")
    ccys = [h.strip() for h in header[1:]]
    out: list[EcbRate] = []
    for row in reader:
        if not row:
            continue
        try:
            d = date.fromisoformat(row[0])
        except ValueError:
            raise FormatDriftError(f"ECB CSV: bad date {row[0]!r}") from None
        if (date_from and d < date_from) or (date_to and d > date_to):
            continue
        for ccy, cell in zip(ccys, row[1:], strict=False):
            if not ccy:
                continue
            if not _CCY.match(ccy):
                raise FormatDriftError(f"ECB CSV: bad currency column {ccy!r}")
            cell = cell.strip()
            if cell in ("", "N/A"):
                continue
            out.append(EcbRate(d, ccy, _rate(cell, f"{d} {ccy}")))
    return out


def fetch(http: HttpClient, url: str) -> httpx.Response:
    return http.get(url)
