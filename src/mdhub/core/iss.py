"""MOEX ISS JSON helpers: block parsing, column validation, paging and value converters.

ISS answers (with ``iss.meta=off``) look like::

    {"history": {"columns": ["BOARDID", "TRADEDATE", ...], "data": [["TQOB", "2026-10-15", ...]]},
     "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 345, 100]]}}

History endpoints return at most ``PAGESIZE`` (100) rows per request; :func:`iter_pages`
walks them with ``start=`` and verifies that the number of rows received equals ``TOTAL``.
APIM (AlgoPack) endpoints use the block ``data`` with ``data.cursor``.

All functions raise :class:`~mdhub.core.errors.FormatDriftError` (or its subclass
:class:`IssFormatError`) on unexpected shapes - never return partial data.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Final

import httpx

from mdhub.core.decimalutil import to_decimal as _to_decimal
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient
from mdhub.core.timeutil import MSK

__all__ = [
    "APIM_BASE_URL",
    "ISS_BASE_URL",
    "IssCursor",
    "IssFormatError",
    "IssPage",
    "IssPagingError",
    "block_columns",
    "fetch_all",
    "iter_pages",
    "iter_pages_until_empty",
    "parse_block",
    "parse_cursor",
    "require_columns",
    "to_date",
    "to_datetime",
    "to_decimal",
    "to_int",
    "to_str",
    "to_time",
]

ISS_BASE_URL: Final = "https://iss.moex.com/iss"
APIM_BASE_URL: Final = "https://apim.moex.com/iss"
DEFAULT_MAX_PAGES: Final = 10_000


class IssFormatError(FormatDriftError):
    """ISS payload shape differs from what the parser expects."""


class IssPagingError(IssFormatError):
    """Paged fetch is inconsistent (row count != TOTAL, TOTAL changed, stuck cursor...)."""


# ----------------------------------------------------------------------------- blocks


def _block(payload: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        raise IssFormatError(f"ISS payload is {type(payload).__name__}, expected an object")
    block = payload.get(name)
    if block is None:
        return None
    if not isinstance(block, Mapping) or "columns" not in block or "data" not in block:
        raise IssFormatError(f"ISS block {name!r} has no columns/data (iss.json=extended?)")
    if not isinstance(block["columns"], list) or not isinstance(block["data"], list):
        raise IssFormatError(f"ISS block {name!r}: columns/data must be lists")
    return block


def block_columns(payload: Mapping[str, Any], name: str) -> list[str]:
    """Column names of block ``name``.

    :raises IssFormatError: block missing or malformed.
    """
    block = _block(payload, name)
    if block is None:
        raise IssFormatError(f"ISS block {name!r} missing (have: {sorted(payload)})")
    return [str(c) for c in block["columns"]]


def require_columns(payload: Mapping[str, Any], name: str, required: Iterable[str]) -> None:
    """Raise :class:`IssFormatError` unless block ``name`` has all ``required`` columns.

    Checks the declared column list, so it also works when the block has no rows.
    """
    have = set(block_columns(payload, name))
    missing = [c for c in required if c not in have]
    if missing:
        raise IssFormatError(f"ISS block {name!r} lacks column(s): {', '.join(missing)}")


def parse_block(
    payload: Mapping[str, Any],
    name: str,
    *,
    required: Iterable[str] = (),
    optional: bool = False,
) -> list[dict[str, Any]]:
    """Return block ``name`` as a list of ``{column: value}`` dicts (values untouched).

    :param required: columns that must be declared (see :func:`require_columns`).
    :param optional: return ``[]`` instead of raising when the block is absent.
    :raises IssFormatError: block missing (unless ``optional``), malformed, or a row length
        differs from the column count.
    """
    block = _block(payload, name)
    if block is None:
        if optional:
            return []
        raise IssFormatError(f"ISS block {name!r} missing (have: {sorted(payload)})")
    columns = [str(c) for c in block["columns"]]
    required = list(required)
    if required:
        require_columns(payload, name, required)
    rows: list[dict[str, Any]] = []
    width = len(columns)
    for i, raw in enumerate(block["data"]):
        if not isinstance(raw, list) or len(raw) != width:
            raise IssFormatError(f"ISS block {name!r} row {i}: expected {width} values")
        rows.append(dict(zip(columns, raw, strict=True)))
    return rows


# ----------------------------------------------------------------------------- paging


@dataclass(frozen=True, slots=True)
class IssCursor:
    """``<block>.cursor`` values."""

    index: int
    total: int
    pagesize: int


@dataclass(frozen=True, slots=True)
class IssPage:
    """One fetched page: rows of the data block, its cursor (if any), raw response."""

    rows: list[dict[str, Any]]
    cursor: IssCursor | None
    response: httpx.Response
    payload: Mapping[str, Any]
    start: int


def parse_cursor(payload: Mapping[str, Any], name: str) -> IssCursor | None:
    """Parse cursor block ``name`` (e.g. ``"history.cursor"``); ``None`` if absent."""
    rows = parse_block(payload, name, required=("INDEX", "TOTAL", "PAGESIZE"), optional=True)
    if not rows:
        return None
    if len(rows) != 1:
        raise IssFormatError(f"ISS cursor {name!r} has {len(rows)} rows, expected 1")
    r = rows[0]
    try:
        cur = IssCursor(int(r["INDEX"]), int(r["TOTAL"]), int(r["PAGESIZE"]))
    except (TypeError, ValueError) as exc:
        raise IssFormatError(f"ISS cursor {name!r}: bad values {r}") from exc
    if cur.pagesize <= 0 or cur.total < 0 or cur.index < 0:
        raise IssFormatError(f"ISS cursor {name!r}: bad values {r}")
    return cur


def _page_params(params: Mapping[str, Any] | None, start: int) -> dict[str, Any]:
    out: dict[str, Any] = {"iss.meta": "off"}
    out.update(params or {})
    out["start"] = start
    return out


def iter_pages(
    client: HttpClient,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    block: str = "history",
    cursor_block: str | None = None,
    required: Iterable[str] = (),
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Iterator[IssPage]:
    """Yield the pages of a cursor-paged ISS endpoint (history, AlgoPack).

    Sends ``iss.meta=off`` (unless overridden) and ``start=`` = previous ``INDEX + PAGESIZE``
    until ``TOTAL`` is reached. After the last page verifies ``sum(rows) == TOTAL``.

    :param block: data block name (``"history"``; ``"data"`` for AlgoPack).
    :param cursor_block: default ``f"{block}.cursor"``. The cursor block is mandatory.
    :raises IssPagingError: missing cursor, TOTAL changed between pages, cursor not
        advancing, empty page before TOTAL, row count != TOTAL, too many pages.
    """
    cursor_name = cursor_block or f"{block}.cursor"
    required = list(required)
    start = 0
    received = 0
    total: int | None = None
    for _ in range(max_pages):
        res = client.get_json(url, params=_page_params(params, start))
        payload = res.data
        rows = parse_block(payload, block, required=required)
        cursor = parse_cursor(payload, cursor_name)
        if cursor is None:
            raise IssPagingError(f"ISS {url}: cursor block {cursor_name!r} missing")
        if cursor.index != start:
            raise IssPagingError(f"ISS {url}: cursor INDEX {cursor.index} != start {start}")
        if total is None:
            total = cursor.total
        elif cursor.total != total:
            raise IssPagingError(f"ISS {url}: TOTAL changed {total} -> {cursor.total}")
        if len(rows) > cursor.pagesize:
            raise IssPagingError(f"ISS {url}: {len(rows)} rows > PAGESIZE {cursor.pagesize}")
        received += len(rows)
        yield IssPage(rows=rows, cursor=cursor, response=res.response, payload=payload, start=start)
        next_start = cursor.index + cursor.pagesize
        if next_start >= total:
            if received != total:
                raise IssPagingError(f"ISS {url}: received {received} rows, TOTAL {total}")
            return
        if not rows:
            raise IssPagingError(f"ISS {url}: empty page at start={start} before TOTAL {total}")
        start = next_start
    raise IssPagingError(f"ISS {url}: more than {max_pages} pages")


def iter_pages_until_empty(
    client: HttpClient,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    block: str,
    required: Iterable[str] = (),
    max_pages: int = DEFAULT_MAX_PAGES,
) -> Iterator[IssPage]:
    """Yield pages of a ``start=``-paged endpoint without a cursor block (e.g.
    ``/iss/securities.json``) until a page comes back empty. No TOTAL check is possible."""
    required = list(required)
    start = 0
    for _ in range(max_pages):
        res = client.get_json(url, params=_page_params(params, start))
        rows = parse_block(res.data, block, required=required)
        yield IssPage(rows=rows, cursor=None, response=res.response, payload=res.data, start=start)
        if not rows:
            return
        start += len(rows)
    raise IssPagingError(f"ISS {url}: more than {max_pages} pages")


def fetch_all(
    client: HttpClient,
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    block: str = "history",
    cursor_block: str | None = None,
    required: Iterable[str] = (),
    on_page: Callable[[IssPage], None] | None = None,
) -> list[dict[str, Any]]:
    """All rows of a cursor-paged endpoint (see :func:`iter_pages`).

    ``on_page`` is called for every page before its rows are accepted - use it to
    :func:`~mdhub.core.capture.record_capture` each response.
    """
    rows: list[dict[str, Any]] = []
    for page in iter_pages(
        client, url, params, block=block, cursor_block=cursor_block, required=required
    ):
        if on_page is not None:
            on_page(page)
        rows.extend(page.rows)
    return rows


# ------------------------------------------------------------------------- converters

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$")


def to_str(value: Any) -> str | None:
    """Strip strings; ``None``/``""`` -> ``None``; numbers -> ``str``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_int(value: Any, *, field: str = "value") -> int | None:
    """Integer or ``None``; floats must be integral."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise IssFormatError(f"{field}: boolean is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise IssFormatError(f"{field}: {value!r} is not an integer")
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise IssFormatError(f"{field}: not an integer: {value!r}")


def to_decimal(value: Any, *, field: str = "value") -> Decimal | None:
    """Decimal via ``str()`` (no float artefacts); ``None``/``""`` -> ``None``."""
    return _to_decimal(value, field=field)


def to_date(value: Any, *, field: str = "value") -> date | None:
    """``YYYY-MM-DD`` -> date; ``None``, ``""``, ``"0000-00-00"`` -> ``None``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        raise IssFormatError(f"{field}: datetime where a date was expected")
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text == "0000-00-00":
        return None
    if not _DATE_RE.match(text):
        raise IssFormatError(f"{field}: not a date: {value!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise IssFormatError(f"{field}: invalid date {value!r}") from None


def to_time(value: Any, *, field: str = "value") -> time | None:
    """``HH:MM[:SS[.ffffff]]`` -> time; ``None``/``""`` -> ``None``.

    ISS occasionally reports ``24:00:00`` (end of day); it is mapped to ``23:59:59.999999``.
    """
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    if not text:
        return None
    m = _TIME_RE.match(text)
    if not m:
        raise IssFormatError(f"{field}: not a time: {value!r}")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    micro = int((m.group(4) or "0").ljust(6, "0"))
    if (hh, mm, ss, micro) == (24, 0, 0, 0):
        return time(23, 59, 59, 999_999)
    try:
        return time(hh, mm, ss, micro)
    except ValueError:
        raise IssFormatError(f"{field}: invalid time {value!r}") from None


def to_datetime(value: Any, *, field: str = "value") -> datetime | None:
    """``YYYY-MM-DD HH:MM:SS`` (MSK wall clock, as ISS reports) -> aware datetime (MSK).

    ``None``/``""``/``"0000-00-00 00:00:00"`` -> ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000-00-00"):
        return None
    parts = text.replace("T", " ").split(" ")
    if len(parts) != 2:
        raise IssFormatError(f"{field}: not a datetime: {value!r}")
    d = to_date(parts[0], field=field)
    t = to_time(parts[1], field=field)
    if d is None or t is None:
        raise IssFormatError(f"{field}: not a datetime: {value!r}")
    return datetime.combine(d, t, tzinfo=MSK)
