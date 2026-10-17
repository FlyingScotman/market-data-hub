"""Rosstat (rosstat.gov.ru): monthly consumer price index - fetch + parse, no DB.

The CPI workbook is linked from ``/statistics/price`` as
``/storage/mediabank/ipc_mes_MM-YYYY.xlsx`` (the name changes with every monthly release;
the newest link wins). Sheet ``01`` holds a month x year grid since 1991:

* a header row ``[None, '1991', '1992', ...]`` (consecutive years),
* the block ``к концу предыдущего месяца`` - CPI to the previous month in percent
  (``100.62`` = +0.62 %), rows ``январь`` .. ``декабрь``,
* the block ``к декабрю предыдущего года`` - its ``декабрь`` row is the year's Dec/Dec index.

Validation (fail closed, :class:`FormatDriftError`): header years consecutive from 1991,
twelve month rows in order, every value in ``[90, 400]``, no hole except the not yet published
months of the last year, and for every complete year the product of the monthly
indices reproduces the Dec/Dec index within :data:`DEC_DEC_TOLERANCE` (catches a shifted
column or row). ``rosstat.gov.ru`` omits the Russian Trusted Sub CA from its TLS chain;
:func:`mdhub.core.http.build_ssl_context` bundles it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.sources.cbr import read_xlsx_rows

__all__ = [
    "BASE_URL",
    "CPI_SHEET",
    "PARSER_VERSION",
    "PRICE_PAGE_PATH",
    "CpiMonth",
    "fetch_cpi_workbook",
    "find_cpi_workbook",
    "parse_cpi_monthly",
]

BASE_URL: Final = "https://rosstat.gov.ru"
PRICE_PAGE_PATH: Final = "/statistics/price"
CPI_SHEET: Final = "01"
PARSER_VERSION: Final = "1"
FIRST_YEAR: Final = 1991
MIN_VALUE: Final = Decimal(90)
MAX_VALUE: Final = Decimal(400)
DEC_DEC_TOLERANCE: Final = Decimal("0.001")
"""Relative tolerance of the Dec/Dec cross-check (rounding of the published monthly values
leaves at most ~0.0004, 1994)."""

_WORKBOOK_RE: Final = re.compile(r'href="(/storage/mediabank/ipc_mes_(\d{2})-(\d{4})\.xlsx)"')
_MONTHS: Final = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
_MOM_BLOCK: Final = "к концу предыдущего месяца"
_DEC_BLOCK: Final = "к декабрю предыдущего года"


@dataclass(frozen=True, slots=True)
class CpiMonth:
    period: date
    """First day of the month the index refers to."""
    index_pct: Decimal
    """CPI of the month to the previous month, percent (``100.62``)."""


def find_cpi_workbook(page: str) -> str:
    """Path of the newest ``ipc_mes_MM-YYYY.xlsx`` linked from the price page."""
    found = {(int(m.group(3)), int(m.group(2))): m.group(1) for m in _WORKBOOK_RE.finditer(page)}
    if not found:
        raise FormatDriftError("Rosstat price page: no ipc_mes_MM-YYYY.xlsx link")
    return found[max(found)]


def _cell(row: list[str | None], i: int) -> str:
    return (row[i] or "").strip() if i < len(row) else ""


def _value(text: str, where: str) -> Decimal | None:
    if not text:
        return None
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        raise FormatDriftError(f"Rosstat CPI {where}: not a number {text!r}") from None
    if not value.is_finite() or not MIN_VALUE <= value <= MAX_VALUE:
        raise FormatDriftError(f"Rosstat CPI {where}: value out of range {text!r}")
    return value


def _find_row(rows: list[list[str | None]], label: str, start: int = 0) -> int:
    for i in range(start, len(rows)):
        if _cell(rows[i], 0).casefold() == label:
            return i
    raise FormatDriftError(f"Rosstat CPI: row {label!r} not found")


def parse_cpi_monthly(payload: bytes) -> list[CpiMonth]:
    """Monthly CPI (to the previous month) from the workbook, sorted by period."""
    rows = read_xlsx_rows(payload, sheet=CPI_SHEET)
    header = next((i for i, r in enumerate(rows) if _cell(r, 1) == str(FIRST_YEAR)), None)
    if header is None:
        raise FormatDriftError(f"Rosstat CPI: no year header starting {FIRST_YEAR}")
    years: list[int] = []
    for col in range(1, len(rows[header])):
        text = _cell(rows[header], col)
        if not text:
            break
        if text != str(FIRST_YEAR + len(years)):
            raise FormatDriftError(f"Rosstat CPI: year header broken at {text!r}")
        years.append(int(text))
    block = _find_row(rows, _MOM_BLOCK, header + 1)
    month_rows = rows[block + 1 : block + 13]
    labels = tuple(_cell(r, 0).casefold() for r in month_rows)
    if labels != _MONTHS:
        raise FormatDriftError(f"Rosstat CPI: month rows {labels!r}")
    out: list[CpiMonth] = []
    for col, year in enumerate(years, start=1):
        values = [_value(_cell(r, col), f"{year}-{m:02d}") for m, r in enumerate(month_rows, 1)]
        present = [v is not None for v in values]
        n = present.index(False) if False in present else 12
        if any(present[n:]) or (n < 12 and year != years[-1]) or n == 0:
            raise FormatDriftError(f"Rosstat CPI {year}: missing months {present!r}")
        out.extend(CpiMonth(date(year, m, 1), v) for m, v in enumerate(values[:n], 1) if v)
        if n == 12:
            _check_dec_dec(rows, block, col, year, values)
    return out


def _check_dec_dec(
    rows: list[list[str | None]], block: int, col: int, year: int, values: list[Decimal | None]
) -> None:
    dec_block = _find_row(rows, _DEC_BLOCK, block + 13)
    dec_row = rows[dec_block + 1] if dec_block + 1 < len(rows) else []
    if _cell(dec_row, 0).casefold() != "декабрь":
        raise FormatDriftError("Rosstat CPI: Dec/Dec row not found")
    text = _cell(dec_row, col)
    try:
        dec_dec = Decimal(text)
    except InvalidOperation:
        raise FormatDriftError(f"Rosstat CPI {year}: Dec/Dec not a number {text!r}") from None
    chained = Decimal(100)
    for v in values:
        chained = chained * (v or Decimal(0)) / 100
    if abs(chained / dec_dec - 1) > DEC_DEC_TOLERANCE:
        raise FormatDriftError(
            f"Rosstat CPI {year}: monthly indices chain to {chained:.2f}, Dec/Dec is {dec_dec}"
        )


def fetch_cpi_workbook(http: HttpClient) -> tuple[str, httpx.Response]:
    """``(workbook path, response)``: the price page, then the newest CPI workbook."""
    page = http.get(f"{BASE_URL}{PRICE_PAGE_PATH}")
    path = find_cpi_workbook(page.text)
    return path, http.get(f"{BASE_URL}{path}")
