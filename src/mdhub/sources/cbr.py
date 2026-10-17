"""Bank of Russia (cbr.ru): fetch + parse, no DB.

Endpoints (all GET, public; ``HttpClient`` limits ``www.cbr.ru`` to 1 request/second):

* ``/hd_base/KeyRate/``            key rate per CBR business day (HTML table ``Дата, Ставка``)
* ``/hd_base/ruonia/dynamics/``    RUONIA by observation date + volume, deals, percentiles,
  calculation status and publication date (HTML)
* ``/Queries/UniDbQuery/DownloadExcel/125022``  RUONIA index and term (averaged) RUONIA
  1M/3M/6M per calendar day (XLSX; the HTML page ``/hd_base/ruonia/dynamic/`` shows only a week)
* ``/hd_base/ProcStav/IR_MPO/``    standing facility rates (HTML, multi-row header)
* ``/hd_base/bliquidity/``         liquidity balance (HTML, 15 columns, multi-row header)
* ``/dkp/cal_mp/``                 board meeting calendar (HTML)
* ``/statistics/idkp_br/refinancing_rates1/``  refinancing rate periods 1992-2015 (HTML; from
  2016 the refinancing rate equals the key rate)
* ``/scripts/XML_daily.asp``       official FX, all currencies, rate in force on ``date_req``
* ``/scripts/XML_dynamic.asp``     official FX history of one currency id

``hd_base`` pages take ``UniDbQuery.Posted=True&UniDbQuery.From=dd.mm.yyyy&UniDbQuery.To=...``.
When the range holds no data CBR returns the latest available row instead, so callers must not
assume rows lie inside the requested range.

Every parser validates headers and fails closed (:class:`FormatDriftError`); a data row that
does not parse is an error, never skipped. Numbers are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Final

import httpx

from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient

__all__ = [
    "BASE_URL",
    "PARSER_VERSION",
    "STANDING_MAIN_MECHANISM_FROM",
    "FxRate",
    "FxRecord",
    "LiquidityRow",
    "Meeting",
    "RefinancingPeriod",
    "RuoniaIndexRow",
    "RuoniaRow",
    "StandingRates",
    "Table",
    "extract_tables",
    "fetch_hd_base",
    "fetch_meetings_page",
    "fetch_refinancing_page",
    "fetch_ruonia_index_xlsx",
    "fetch_xml_daily",
    "fetch_xml_dynamic",
    "parse_cbr_number",
    "parse_key_rate_page",
    "parse_liquidity_page",
    "parse_meetings_page",
    "parse_refinancing_page",
    "parse_ruonia_index_xlsx",
    "parse_ruonia_page",
    "parse_standing_rates_page",
    "parse_xml_daily",
    "parse_xml_dynamic",
    "read_xlsx_rows",
    "safe_xml_fromstring",
]

BASE_URL: Final = "https://www.cbr.ru"
PARSER_VERSION: Final = "1"

KEY_RATE_PATH: Final = "/hd_base/KeyRate/"
RUONIA_PATH: Final = "/hd_base/ruonia/dynamics/"
RUONIA_INDEX_XLSX_PATH: Final = "/Queries/UniDbQuery/DownloadExcel/125022"
STANDING_RATES_PATH: Final = "/hd_base/ProcStav/IR_MPO/"
LIQUIDITY_PATH: Final = "/hd_base/bliquidity/"
MEETINGS_PATH: Final = "/dkp/cal_mp/"
XML_DAILY_PATH: Final = "/scripts/XML_daily.asp"
XML_DYNAMIC_PATH: Final = "/scripts/XML_dynamic.asp"
REFINANCING_PATH: Final = "/statistics/idkp_br/refinancing_rates1/"

_MISSING = {"", "—", "-", "–", "н/д"}
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


# -------------------------------------------------------------------------------- XML


def safe_xml_fromstring(payload: bytes | str) -> ET.Element:
    """``ET.fromstring`` for remote payloads without ``defusedxml``: documents with a DTD
    (``<!DOCTYPE``/``<!ENTITY`` - entity-expansion attacks) are rejected outright; expat does
    not resolve external entities by default."""
    head = payload if isinstance(payload, bytes) else payload.encode("utf-8", "replace")
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise FormatDriftError("XML with a DTD/entity declarations is not accepted")
    return ET.fromstring(payload)  # noqa: S314 - DTDs rejected above


# ----------------------------------------------------------------------------- tables


@dataclass(slots=True)
class Table:
    """An HTML table: ``rows`` of ``(cell texts, is_header_row)``; ``css_class`` of <table>."""

    css_class: str
    rows: list[tuple[list[str], bool]] = field(default_factory=list)

    def header_text(self) -> str:
        return " ".join(" ".join(cells) for cells, is_header in self.rows if is_header)

    def data_rows(self) -> list[list[str]]:
        return [cells for cells, is_header in self.rows if not is_header]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._stack: list[Table] = []
        self._row: list[str] | None = None
        self._row_header = False
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            cls = next((v or "" for k, v in attrs if k == "class"), "")
            table = Table(css_class=cls)
            self.tables.append(table)
            self._stack.append(table)
        elif not self._stack:
            return
        elif tag == "tr":
            self._row, self._row_header = [], False
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            if tag == "th":
                self._row_header = True
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_norm_space("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._stack[-1].rows.append((self._row, self._row_header))
            self._row = None
        elif tag == "table":
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _norm_space(text: str) -> str:
    return " ".join(html.unescape(text).replace("\xa0", " ").replace(" ", " ").split())


def extract_tables(page: str) -> list[Table]:
    """All ``<table>`` elements of ``page`` with their cell texts (whitespace-normalized)."""
    parser = _TableParser()
    parser.feed(page)
    parser.close()
    return parser.tables


def _find_table(page: str, *keywords: str, what: str) -> Table | None:
    """The table whose header contains ``keywords``.

    ``None`` = "no data for the range": an ``hd_base`` page (date form ``UniDbQuery`` present)
    without any ``<table>`` - CBR renders it for ranges before a series starts. Any other
    page without the expected table raises :class:`FormatDriftError`.
    """
    tables = extract_tables(page)
    for table in tables:
        header = table.header_text().casefold()
        if all(k.casefold() in header for k in keywords):
            return table
    if not tables and "UniDbQuery" in page:
        return None
    raise FormatDriftError(f"CBR {what}: table with header {keywords!r} not found")


# ----------------------------------------------------------------------------- values


def parse_cbr_number(text: str, *, field: str = "value") -> Decimal | None:
    """``"3 732,7"`` -> ``Decimal("3732.7")``; ``"—"``/empty -> ``None``."""
    cleaned = re.sub(r"[\s  ]", "", text).replace(",", ".")
    if cleaned in _MISSING:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise FormatDriftError(f"CBR {field}: not a number: {text!r}") from None
    if not value.is_finite():
        raise FormatDriftError(f"CBR {field}: non-finite {text!r}")
    return value


def _require_number(text: str, field: str) -> Decimal:
    value = parse_cbr_number(text, field=field)
    if value is None:
        raise FormatDriftError(f"CBR {field}: value missing")
    return value


def parse_ru_date(text: str, *, field: str = "date") -> date:
    """``dd.mm.yyyy`` -> date."""
    m = _DATE_RE.match(text.strip())
    if not m:
        raise FormatDriftError(f"CBR {field}: bad date {text!r}")
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        raise FormatDriftError(f"CBR {field}: bad date {text!r}") from None


def _unique_dates(rows: Sequence[tuple[date, object]], what: str) -> None:
    seen: set[date] = set()
    for d, _ in rows:
        if d in seen:
            raise FormatDriftError(f"CBR {what}: duplicate date {d}")
        seen.add(d)


# --------------------------------------------------------------------------- key rate


def parse_key_rate_page(page: str) -> list[tuple[date, Decimal]]:
    """Key rate table -> ``[(date, rate_pct)]`` sorted by date."""
    table = _find_table(page, "Дата", "Ставка", what="key rate")
    if table is None:
        return []
    out: list[tuple[date, Decimal]] = []
    for cells in table.data_rows():
        if len(cells) != 2:
            raise FormatDriftError(f"CBR key rate: row with {len(cells)} cells: {cells!r}")
        rate = _require_number(cells[1], "key rate")
        if not Decimal(0) <= rate <= Decimal(100):
            raise FormatDriftError(f"CBR key rate out of range: {rate}")
        out.append((parse_ru_date(cells[0]), rate))
    _unique_dates(out, "key rate")
    return sorted(out)


# ----------------------------------------------------------------------------- RUONIA


@dataclass(frozen=True, slots=True)
class RuoniaRow:
    fixing_date: date
    rate_pct: Decimal
    volume_bn: Decimal | None
    deals: Decimal | None
    participants: Decimal | None
    min_pct: Decimal | None
    p25_pct: Decimal | None
    p75_pct: Decimal | None
    max_pct: Decimal | None
    status: str | None
    published_on: date | None


_RUONIA_HEADERS: Final = (
    "Дата ставки",
    "Ставка RUONIA",
    "Объем сделок",
    "Количество сделок",
    "Количество участников",
    "Минимальная",
    "25-й процентиль",
    "75-й процентиль",
    "Максимальная",
    "Статус расчета",
    "Дата публикации",
)


def parse_ruonia_page(page: str) -> list[RuoniaRow]:
    """RUONIA dynamics table (11 columns, header verified column by column)."""
    table = _find_table(page, "Ставка RUONIA", "Дата публикации", what="RUONIA")
    if table is None:
        return []
    headers = [cells for cells, is_header in table.rows if is_header]
    if not headers or len(headers[0]) != len(_RUONIA_HEADERS):
        raise FormatDriftError(f"CBR RUONIA: unexpected header {headers[:1]!r}")
    for got, want in zip(headers[0], _RUONIA_HEADERS, strict=True):
        if want.casefold() not in got.casefold():
            raise FormatDriftError(f"CBR RUONIA: header {got!r} != {want!r}")
    out: list[RuoniaRow] = []
    for cells in table.data_rows():
        if len(cells) != len(_RUONIA_HEADERS):
            raise FormatDriftError(f"CBR RUONIA: row with {len(cells)} cells: {cells!r}")
        rate = _require_number(cells[1], "RUONIA")
        if not Decimal(-5) <= rate <= Decimal(100):
            raise FormatDriftError(f"CBR RUONIA out of range: {rate}")
        num = [parse_cbr_number(c, field="RUONIA stats") for c in cells[2:9]]
        out.append(
            RuoniaRow(
                fixing_date=parse_ru_date(cells[0]),
                rate_pct=rate,
                volume_bn=num[0],
                deals=num[1],
                participants=num[2],
                min_pct=num[3],
                p25_pct=num[4],
                p75_pct=num[5],
                max_pct=num[6],
                status=None if cells[9] in _MISSING else cells[9],
                published_on=None if cells[10] in _MISSING else parse_ru_date(cells[10]),
            )
        )
    _unique_dates([(r.fixing_date, r) for r in out], "RUONIA")
    return sorted(out, key=lambda r: r.fixing_date)


# -------------------------------------------------------------- RUONIA index (XLSX)

_XLSX_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
_EXCEL_EPOCH: Final = date(1899, 12, 30)


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _sheet_path(zf: zipfile.ZipFile, sheet: str) -> str:
    """Archive path of the worksheet named ``sheet`` (``xl/workbook.xml`` + its rels)."""
    wb = safe_xml_fromstring(zf.read("xl/workbook.xml"))
    rels = safe_xml_fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{_REL_NS}Relationship")}
    for sh in wb.iter(f"{_XLSX_NS}sheet"):
        if (sh.get("name") or "").strip() != sheet:
            continue
        target = targets.get(sh.get(f"{_DOC_REL_NS}id"))
        if not target:
            break
        path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        return str(PurePosixPath(path))
    raise FormatDriftError(f"XLSX: no worksheet named {sheet!r}")


def read_xlsx_rows(payload: bytes, *, sheet: str | None = None) -> list[list[str | None]]:
    """Minimal stdlib reader for one worksheet of an XLSX file (the first in archive order,
    or the one named ``sheet``).

    Returns cell texts per row (inline strings, shared strings or the raw ``<v>`` text of
    numeric cells - no float conversion). Raises :class:`FormatDriftError` on a bad file.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            if sheet is None:
                path = next((n for n in sorted(names) if n.startswith("xl/worksheets/sheet")), None)
                if path is None:
                    raise FormatDriftError("XLSX: no worksheet")
            else:
                path = _sheet_path(zf, sheet)
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sroot = safe_xml_fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter(f"{_XLSX_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")))
            root = safe_xml_fromstring(zf.read(path))
    except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise FormatDriftError(f"XLSX: unreadable workbook: {exc}") from None
    rows: list[list[str | None]] = []
    for row in root.iter(f"{_XLSX_NS}row"):
        cells: dict[int, str | None] = {}
        for c in row.iter(f"{_XLSX_NS}c"):
            m = _CELL_REF.match(c.get("r", ""))
            if not m:
                raise FormatDriftError(f"XLSX: bad cell reference {c.get('r')!r}")
            kind = c.get("t")
            text: str | None
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in c.iter(f"{_XLSX_NS}t"))
            else:
                v = c.find(f"{_XLSX_NS}v")
                text = v.text if v is not None else None
                if kind == "s" and text is not None:
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        raise FormatDriftError("XLSX: bad shared string index") from None
            cells[_col_index(m.group(1))] = text
        width = max(cells) + 1 if cells else 0
        rows.append([cells.get(i) for i in range(width)])
    return rows


@dataclass(frozen=True, slots=True)
class RuoniaIndexRow:
    fixing_date: date
    index: Decimal
    term_1m_pct: Decimal | None
    term_3m_pct: Decimal | None
    term_6m_pct: Decimal | None


def _xlsx_number(text: str | None, field: str) -> Decimal | None:
    if text is None or not text.strip():
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise FormatDriftError(f"CBR {field}: not a number {text!r}") from None
    if not value.is_finite():
        raise FormatDriftError(f"CBR {field}: non-finite {text!r}")
    return value


def parse_ruonia_index_xlsx(payload: bytes) -> list[RuoniaIndexRow]:
    """RUONIA index / term RUONIA workbook: ``Дата, Индекс, 1 месяц, 3 месяца, 6 месяцев``."""
    rows = read_xlsx_rows(payload)
    if not rows:
        raise FormatDriftError("CBR RUONIA index: empty workbook")
    header = [(c or "").strip().casefold() for c in rows[0]]
    want = ["дата", "индекс", "1 месяц", "3 месяца", "6 месяцев"]
    if header[: len(want)] != want:
        raise FormatDriftError(f"CBR RUONIA index: unexpected header {rows[0]!r}")
    out: list[RuoniaIndexRow] = []
    for row in rows[1:]:
        cells = [*row, None, None, None, None, None][:5]
        if all(c is None or not c.strip() for c in cells):
            continue
        serial = _xlsx_number(cells[0], "RUONIA index date")
        if serial is None or serial != serial.to_integral_value():
            raise FormatDriftError(f"CBR RUONIA index: bad date cell {cells[0]!r}")
        index = _xlsx_number(cells[1], "RUONIA index")
        if index is None or index <= 0:
            raise FormatDriftError(f"CBR RUONIA index: bad index {cells[1]!r}")
        out.append(
            RuoniaIndexRow(
                fixing_date=_EXCEL_EPOCH + timedelta(days=int(serial)),
                index=index,
                term_1m_pct=_xlsx_number(cells[2], "term RUONIA 1M"),
                term_3m_pct=_xlsx_number(cells[3], "term RUONIA 3M"),
                term_6m_pct=_xlsx_number(cells[4], "term RUONIA 6M"),
            )
        )
    _unique_dates([(r.fixing_date, r) for r in out], "RUONIA index")
    return sorted(out, key=lambda r: r.fixing_date)


# ------------------------------------------------------------- standing facility rates


@dataclass(frozen=True, slots=True)
class StandingRates:
    rate_date: date
    deposit_standing_pct: Decimal | None
    lending_standing_pct: Decimal | None


STANDING_MAIN_MECHANISM_FROM: Final = date(2023, 10, 16)
"""First date of column 7 ("Основной механизм") of ``/hd_base/ProcStav/IR_MPO/``."""


def parse_standing_rates_page(page: str) -> list[StandingRates]:
    """``/hd_base/ProcStav/IR_MPO/``: column 1 = standing O/N deposit rate; standing O/N
    lending rate (key rate + 1 pp) = exactly one column chosen by date: column 7 ("Ставки по
    операциям предоставления ликвидности постоянного действия / Основной механизм") from
    :data:`STANDING_MAIN_MECHANISM_FROM`, column 9 ("Кредиты, репо, валютные свопы на срок 1
    день") before. Never a fallback from one column to the other: a blank cell is no value.
    Header checked live 2026-10-16 (column 7 empty up to 2023-10-13, column 9 empty from
    2023-10-16). Rows must have 13 cells."""
    table = _find_table(
        page,
        "Ставка по депозитам постоянного действия",
        "Основной механизм",
        what="standing rates",
    )
    if table is None:
        return []
    first = [cells for cells, is_header in table.rows if is_header]
    if not first or not first[0] or first[0][1] != "Ставка по депозитам постоянного действия":
        raise FormatDriftError("CBR standing rates: unexpected first header row")
    out: list[StandingRates] = []
    for cells in table.data_rows():
        if len(cells) != 13:
            raise FormatDriftError(f"CBR standing rates: row with {len(cells)} cells")
        rate_date = parse_ru_date(cells[0])
        lending_col = 7 if rate_date >= STANDING_MAIN_MECHANISM_FROM else 9
        out.append(
            StandingRates(
                rate_date=rate_date,
                deposit_standing_pct=parse_cbr_number(cells[1], field="deposit rate"),
                lending_standing_pct=parse_cbr_number(cells[lending_col], field="lending rate"),
            )
        )
    _unique_dates([(r.rate_date, r) for r in out], "standing rates")
    return sorted(out, key=lambda r: r.rate_date)


# -------------------------------------------------------------------------- liquidity

LIQUIDITY_COLUMNS: Final = (
    "deficit_bn",
    "deficit_ex_corr_accounts_bn",
    "cbr_claims_total_bn",
    "repo_fxswap_auction_bn",
    "secured_loans_auction_bn",
    "repo_fxswap_standing_bn",
    "secured_loans_standing_bn",
    "cbr_obligations_total_bn",
    "deposits_auction_bn",
    "deposits_standing_bn",
    "cobr_bn",
    "nonstandard_net_claims_bn",
    "corr_accounts_bn",
    "required_reserves_averaged_bn",
)
"""Columns 2..15 of the published table, in order. Checked against the live header of
``/hd_base/bliquidity/`` on 2026-10-16: 4 = claims total (5+6+7+8), 5 = auctions: repo and FX
swap, 6 = auctions: secured loans, 7 = standing facilities: repo and FX swap, 8 = standing
facilities: secured loans, 9 = obligations total (10+11+12), 10 = deposit auctions, 11 =
standing deposits, 12 = КОБР, 13 = non-standard operations, 14 = correspondent accounts, 15 =
averaged required reserves. (The legacy SQLite schema labels columns 6/7 the other way round;
its values map by position - ``mdhub.legacy.map_md.LIQUIDITY_COLUMNS``.)"""

_LIQ_FORMULA_ROW: Final = [
    "1",
    "2 = 4 - 9 + 13 - 14 + 15",
    "3 = 4 - 9 + 13",
    "4 = 5 + 6 + 7 + 8",
    "5",
    "6",
    "7",
    "8",
    "9 = 10 + 11 + 12",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
]
LIQUIDITY_TOLERANCE_BN: Final = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class LiquidityRow:
    observed_on: date
    values: Mapping[str, Decimal]


def parse_liquidity_page(page: str) -> list[LiquidityRow]:
    """Liquidity balance table. Verifies the column-number row and the published identities
    (2 = 4-9+13-14+15, 3 = 4-9+13, 4 = 5+..+8, 9 = 10+11+12) within 0.5 bn."""
    table = _find_table(page, "Дефицит", "профицит", "корсчетах", what="liquidity")
    if table is None:
        return []
    if not any(cells == _LIQ_FORMULA_ROW for cells, _ in table.rows):
        raise FormatDriftError("CBR liquidity: column-number/formula header row changed")
    out: list[LiquidityRow] = []
    for cells in table.data_rows():
        if cells == _LIQ_FORMULA_ROW:
            continue
        if len(cells) != 15:
            raise FormatDriftError(f"CBR liquidity: row with {len(cells)} cells: {cells!r}")
        d = parse_ru_date(cells[0])
        nums = [_require_number(c, f"liquidity {d}") for c in cells[1:]]
        c = dict(zip(range(2, 16), nums, strict=True))
        checks = (
            (c[2], c[4] - c[9] + c[13] - c[14] + c[15]),
            (c[3], c[4] - c[9] + c[13]),
            (c[4], c[5] + c[6] + c[7] + c[8]),
            (c[9], c[10] + c[11] + c[12]),
        )
        for got, want in checks:
            if abs(got - want) > LIQUIDITY_TOLERANCE_BN:
                raise FormatDriftError(f"CBR liquidity {d}: identity broken ({got} != {want})")
        out.append(LiquidityRow(d, dict(zip(LIQUIDITY_COLUMNS, nums, strict=True))))
    _unique_dates([(r.observed_on, r) for r in out], "liquidity")
    return sorted(out, key=lambda r: r.observed_on)


# ---------------------------------------------------------------------------- meetings

_RU_MONTHS: Final = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_MEETING_DATE_RE = re.compile(r"^(\d{1,2}) ([а-яё]+)(?: (\d{4}) года)?$", re.IGNORECASE)
_EVENT_SPLIT = '<div class="main-events_day">'
_TAB_LABEL_RE = re.compile(r'href="#(t\d+)" data-text="(\d{4}) год"')
_TAB_PANEL_RE = re.compile(r'<div role="tabpanel" id="tab_content_(t\d+)"')


@dataclass(frozen=True, slots=True)
class Meeting:
    meeting_date: date
    is_extraordinary: bool
    title: str


def _strip_tags(fragment: str) -> str:
    return _norm_space(re.sub(r"<[^>]+>", " ", fragment))


def _parse_events(segment: str, year: int | None, out: dict[date, Meeting]) -> None:
    for part in segment.split(_EVENT_SPLIT)[1:]:
        m = re.search(r'<div class="date[^"]*">(.*?)</div>', part, re.S)
        if m is None:
            raise FormatDriftError("CBR meetings: event without date")
        text = _strip_tags(part[:4000])
        title_m = re.search(
            r"((?:Внеочередное )?[Зз]аседание Совета директоров[^.]*?)(?: Пресс| Предполагаемое|$)",
            text,
        )
        if title_m is None:
            continue
        title = title_m.group(1).strip()
        if "ключев" not in title and "денежно-кредитной политик" not in title:
            continue
        raw = _strip_tags(m.group(1))
        dm = _MEETING_DATE_RE.match(raw)
        if dm is None or dm.group(2).casefold() not in _RU_MONTHS:
            raise FormatDriftError(f"CBR meetings: bad date {raw!r}")
        y = int(dm.group(3)) if dm.group(3) else year
        if y is None:
            raise FormatDriftError(f"CBR meetings: no year for {raw!r}")
        d = date(y, _RU_MONTHS[dm.group(2).casefold()], int(dm.group(1)))
        out[d] = Meeting(d, title.startswith("Внеочередное"), title)


def parse_meetings_page(page: str) -> list[Meeting]:
    """Key-rate board meetings from ``/dkp/cal_mp/`` (regular and extraordinary).

    The page has one tab per year; recent years carry the year in each date, older tabs
    (``"8 февраля"``) take it from the tab label. Years rendered as plain tables (2014-2018)
    are not parsed - the key-rate series covers that history.
    """
    labels = {tab: int(year) for tab, year in _TAB_LABEL_RE.findall(page)}
    out: dict[date, Meeting] = {}
    panels = list(_TAB_PANEL_RE.finditer(page))
    if not panels:
        _parse_events(page, None, out)
    for i, panel in enumerate(panels):
        end = panels[i + 1].start() if i + 1 < len(panels) else len(page)
        _parse_events(page[panel.start() : end], labels.get(panel.group(1)), out)
    if not out:
        raise FormatDriftError("CBR meetings: no key-rate meetings found")
    return [out[d] for d in sorted(out)]


# ------------------------------------------------------------------- refinancing rate

REFINANCING_FIRST_DAY: Final = date(1992, 1, 1)
REFINANCING_MERGED_ON: Final = date(2016, 1, 1)
"""From this day the refinancing rate equals the key rate (Указание № 3894-У); the page
lists no values after it."""
_RANGE_RE = re.compile(
    r"^(\d{1,2}) ([а-яё]+) (\d{4}) г\. [–—-] (\d{1,2}) ([а-яё]+) (\d{4}) г\.$", re.IGNORECASE
)
_MERGED_RE = re.compile(r"^С (\d{1,2}) ([а-яё]+) (\d{4}) г\.$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RefinancingPeriod:
    date_from: date
    date_to: date
    rate_pct: Decimal
    document: str


def _ru_long_date(parts: Sequence[str], what: str) -> date:
    """``("14", "сентября", "2012")`` -> date."""
    day, month, year = parts
    m = _RU_MONTHS.get(month.casefold())
    try:
        if m is None:
            raise ValueError(month)
        return date(int(year), m, int(day))
    except ValueError:
        raise FormatDriftError(f"CBR {what}: bad date {day} {month} {year}") from None


def parse_refinancing_page(page: str) -> list[RefinancingPeriod]:
    """Refinancing rate periods (``/statistics/idkp_br/refinancing_rates1/``), ascending.

    Fails closed unless the periods tile 1992-01-01 .. 2015-12-31 without gaps or overlaps
    and the table opens with the "from 1 January 2016 equal to the key rate" row.
    """
    table = _find_table(page, "Период действия", "Ставка", what="refinancing rate")
    if table is None:
        raise FormatDriftError("CBR refinancing rate: table missing")
    out: list[RefinancingPeriod] = []
    merged: date | None = None
    for cells in table.data_rows():
        if len(cells) != 3:
            raise FormatDriftError(f"CBR refinancing rate: row with {len(cells)} cells: {cells!r}")
        m = _MERGED_RE.match(cells[0])
        if m is not None:
            if "ключевой ставки" not in cells[1]:
                raise FormatDriftError(f"CBR refinancing rate: unexpected row {cells!r}")
            merged = _ru_long_date(m.groups(), "refinancing rate")
            continue
        m = _RANGE_RE.match(cells[0])
        if m is None:
            raise FormatDriftError(f"CBR refinancing rate: bad period {cells[0]!r}")
        d1 = _ru_long_date(m.groups()[:3], "refinancing rate")
        d2 = _ru_long_date(m.groups()[3:], "refinancing rate")
        rate = _require_number(cells[1], "refinancing rate")
        if d2 < d1 or not Decimal(0) < rate <= Decimal(300):
            raise FormatDriftError(f"CBR refinancing rate: bad row {cells!r}")
        out.append(RefinancingPeriod(d1, d2, rate, cells[2]))
    out.sort(key=lambda p: p.date_from)
    if merged != REFINANCING_MERGED_ON:
        raise FormatDriftError(f"CBR refinancing rate: key-rate merge row says {merged}")
    expected = REFINANCING_FIRST_DAY
    for p in out:
        if p.date_from != expected:
            raise FormatDriftError(
                f"CBR refinancing rate: period starts {p.date_from}, not {expected}"
            )
        expected = p.date_to + timedelta(days=1)
    if expected != REFINANCING_MERGED_ON:
        raise FormatDriftError(f"CBR refinancing rate: periods end {expected - timedelta(days=1)}")
    return out


# -------------------------------------------------------------------------------- FX


@dataclass(frozen=True, slots=True)
class FxRate:
    cbr_id: str
    ccy: str
    nominal: int
    value: Decimal
    vunit_rate: Decimal | None

    @property
    def rate_per_unit(self) -> Decimal:
        return self.value / self.nominal


@dataclass(frozen=True, slots=True)
class FxRecord:
    """One CBR rate record: ``record_date`` = first day it is in force."""

    record_date: date
    rates: tuple[FxRate, ...]


def _xml_root(payload: bytes, what: str) -> ET.Element:
    try:
        return safe_xml_fromstring(payload)
    except ET.ParseError as exc:
        raise FormatDriftError(f"CBR {what}: invalid XML: {exc}") from None


def _fx_rate(node: ET.Element, cbr_id: str, ccy: str, what: str) -> FxRate:
    nominal_text = (node.findtext("Nominal") or "").strip()
    try:
        nominal = int(nominal_text)
    except ValueError:
        raise FormatDriftError(f"CBR {what} {ccy}: bad Nominal {nominal_text!r}") from None
    if nominal <= 0:
        raise FormatDriftError(f"CBR {what} {ccy}: Nominal {nominal}")
    value = _require_number(node.findtext("Value") or "", f"{what} {ccy} Value")
    if value <= 0:
        raise FormatDriftError(f"CBR {what} {ccy}: Value {value}")
    vunit_text = node.findtext("VunitRate")
    vunit = parse_cbr_number(vunit_text, field=f"{what} {ccy}") if vunit_text else None
    rate = FxRate(cbr_id=cbr_id, ccy=ccy, nominal=nominal, value=value, vunit_rate=vunit)
    if vunit is not None and abs(vunit - rate.rate_per_unit) > rate.rate_per_unit * Decimal(
        "0.0001"
    ):
        raise FormatDriftError(f"CBR {what} {ccy}: VunitRate {vunit} != Value/Nominal")
    return rate


def parse_xml_daily(payload: bytes) -> FxRecord:
    """``XML_daily.asp``: the record in force on the requested date (all currencies)."""
    root = _xml_root(payload, "XML_daily")
    if root.tag != "ValCurs" or "Date" not in root.attrib:
        raise FormatDriftError("CBR XML_daily: missing ValCurs/@Date")
    record_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    rates: list[FxRate] = []
    seen: set[str] = set()
    for node in root.findall("Valute"):
        ccy = (node.findtext("CharCode") or "").strip().upper()
        cbr_id = node.get("ID", "").strip()
        if not re.fullmatch(r"[A-Z]{3}", ccy) or not cbr_id:
            raise FormatDriftError(f"CBR XML_daily: bad Valute {cbr_id!r}/{ccy!r}")
        if ccy in seen:
            raise FormatDriftError(f"CBR XML_daily: duplicate currency {ccy}")
        seen.add(ccy)
        rates.append(_fx_rate(node, cbr_id, ccy, "XML_daily"))
    if not rates:
        raise FormatDriftError("CBR XML_daily: no Valute elements")
    return FxRecord(record_date=record_date, rates=tuple(rates))


def parse_xml_dynamic(payload: bytes, ccy: str) -> list[FxRecord]:
    """``XML_dynamic.asp`` for one currency id -> one single-rate record per date
    (empty list when the currency has no records in the range)."""
    root = _xml_root(payload, "XML_dynamic")
    if root.tag != "ValCurs":
        raise FormatDriftError("CBR XML_dynamic: root is not ValCurs")
    out: list[FxRecord] = []
    seen: set[date] = set()
    for node in root.findall("Record"):
        try:
            d = datetime.strptime(node.get("Date", ""), "%d.%m.%Y").date()
        except ValueError:
            raise FormatDriftError(f"CBR XML_dynamic: bad Date {node.get('Date')!r}") from None
        if d in seen:
            raise FormatDriftError(f"CBR XML_dynamic {ccy}: duplicate date {d}")
        seen.add(d)
        cbr_id = node.get("Id", "").strip()
        out.append(FxRecord(d, (_fx_rate(node, cbr_id, ccy, "XML_dynamic"),)))
    return sorted(out, key=lambda r: r.record_date)


# ------------------------------------------------------------------------------ fetch


def _dmy(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fetch_hd_base(http: HttpClient, path: str, date_from: date, date_to: date) -> httpx.Response:
    """GET an ``hd_base`` page for an inclusive date range."""
    return http.get(
        f"{BASE_URL}{path}",
        params={
            "UniDbQuery.Posted": "True",
            "UniDbQuery.From": _dmy(date_from),
            "UniDbQuery.To": _dmy(date_to),
        },
    )


def fetch_ruonia_index_xlsx(http: HttpClient, date_from: date, date_to: date) -> httpx.Response:
    """GET the RUONIA index / term RUONIA workbook for an inclusive date range."""
    return http.get(
        f"{BASE_URL}{RUONIA_INDEX_XLSX_PATH}",
        params={
            "Posted": "True",
            "From": _dmy(date_from),
            "To": _dmy(date_to),
            "FromDate": date_from.strftime("%m/%d/%Y"),
            "ToDate": date_to.strftime("%m/%d/%Y"),
        },
    )


def fetch_meetings_page(http: HttpClient) -> httpx.Response:
    return http.get(f"{BASE_URL}{MEETINGS_PATH}")


def fetch_refinancing_page(http: HttpClient) -> httpx.Response:
    return http.get(f"{BASE_URL}{REFINANCING_PATH}")


def fetch_xml_daily(http: HttpClient, day: date) -> httpx.Response:
    """Rates in force on ``day`` (CBR returns the latest record dated ``<= day``)."""
    return http.get(f"{BASE_URL}{XML_DAILY_PATH}", params={"date_req": day.strftime("%d/%m/%Y")})


def fetch_xml_dynamic(
    http: HttpClient, cbr_id: str, date_from: date, date_to: date
) -> httpx.Response:
    return http.get(
        f"{BASE_URL}{XML_DYNAMIC_PATH}",
        params={
            "date_req1": date_from.strftime("%d/%m/%Y"),
            "date_req2": date_to.strftime("%d/%m/%Y"),
            "VAL_NM_RQ": cbr_id,
        },
    )


def chunk_range(date_from: date, date_to: date, days: int) -> Iterable[tuple[date, date]]:
    """Split ``[date_from, date_to]`` into consecutive chunks of at most ``days`` days."""
    cur = date_from
    while cur <= date_to:
        end = min(date_to, cur + timedelta(days=days - 1))
        yield cur, end
        cur = end + timedelta(days=1)
