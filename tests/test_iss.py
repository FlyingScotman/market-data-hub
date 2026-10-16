from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from mdhub.config import Settings
from mdhub.core.decimalutil import mid, quantize, require_decimal
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import (
    ISS_BASE_URL,
    IssFormatError,
    IssPage,
    IssPagingError,
    fetch_all,
    iter_pages_until_empty,
    parse_block,
    parse_cursor,
    require_columns,
    to_date,
    to_datetime,
    to_decimal,
    to_int,
    to_str,
    to_time,
)
from mdhub.core.timeutil import MSK

HISTORY_URL = f"{ISS_BASE_URL}/history/engines/stock/markets/bonds/securities.json"
COLS = ["BOARDID", "TRADEDATE", "SECID", "CLOSE"]


def _payload(rows: list[list[Any]], index: int, total: int, pagesize: int = 100) -> dict[str, Any]:
    return {
        "history": {"columns": COLS, "data": rows},
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[index, total, pagesize]],
        },
    }


def _rows(start: int, n: int) -> list[list[Any]]:
    return [["TQOB", "2026-10-15", f"SU{i:05d}", 100.1 + i] for i in range(start, start + n)]


@pytest.fixture
def client() -> Iterator[HttpClient]:
    with HttpClient(
        settings=Settings(), limiter=RateLimiter(default_rps=None), sleep=lambda _s: None
    ) as c:
        yield c


def test_parse_block_and_columns() -> None:
    payload = _payload(_rows(0, 2), 0, 2)
    rows = parse_block(payload, "history", required=["SECID", "CLOSE"])
    assert rows[0] == {
        "BOARDID": "TQOB",
        "TRADEDATE": "2026-10-15",
        "SECID": "SU00000",
        "CLOSE": 100.1,
    }
    require_columns(payload, "history", ["BOARDID"])
    with pytest.raises(IssFormatError, match="lacks column"):
        require_columns(payload, "history", ["YIELD"])
    with pytest.raises(IssFormatError, match="missing"):
        parse_block(payload, "marketdata")
    assert parse_block(payload, "marketdata", optional=True) == []
    broken = {"history": {"columns": ["A", "B"], "data": [[1]]}}
    with pytest.raises(IssFormatError, match="row 0"):
        parse_block(broken, "history")
    with pytest.raises(IssFormatError, match="extended"):
        parse_block({"history": [{"A": 1}]}, "history")
    assert isinstance(IssFormatError("x"), FormatDriftError)
    empty = {"history": {"columns": COLS, "data": []}}
    assert parse_block(empty, "history", required=["SECID"]) == []
    cur = parse_cursor(_payload([], 200, 345), "history.cursor")
    assert cur is not None and (cur.index, cur.total, cur.pagesize) == (200, 345, 100)


@respx.mock
def test_paged_fetch_walks_cursor(client: HttpClient) -> None:
    pages = {
        0: _payload(_rows(0, 100), 0, 250),
        100: _payload(_rows(100, 100), 100, 250),
        200: _payload(_rows(200, 50), 200, 250),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["iss.meta"] == "off"
        assert request.url.params["date"] == "2026-10-15"
        return httpx.Response(200, json=pages[int(request.url.params["start"])])

    route = respx.get(HISTORY_URL).mock(side_effect=handler)
    seen: list[IssPage] = []
    rows = fetch_all(
        client, HISTORY_URL, {"date": "2026-10-15"}, required=["SECID"], on_page=seen.append
    )
    assert len(rows) == 250 and rows[-1]["SECID"] == "SU00249"
    assert route.call_count == 3
    assert [p.start for p in seen] == [0, 100, 200]
    assert all(p.response.status_code == 200 for p in seen)


@respx.mock
def test_paged_fetch_empty_result(client: HttpClient) -> None:
    respx.get(HISTORY_URL).mock(return_value=httpx.Response(200, json=_payload([], 0, 0)))
    assert fetch_all(client, HISTORY_URL, {"date": "2026-01-03"}) == []


@respx.mock
def test_paged_fetch_integrity_checks(client: HttpClient) -> None:
    # TOTAL says 150 but the second page is short -> row count mismatch.
    pages = {0: _payload(_rows(0, 100), 0, 150), 100: _payload(_rows(100, 40), 100, 150)}
    respx.get(HISTORY_URL).mock(
        side_effect=lambda r: httpx.Response(200, json=pages[int(r.url.params["start"])])
    )
    with pytest.raises(IssPagingError, match="received 140 rows, TOTAL 150"):
        fetch_all(client, HISTORY_URL)


@respx.mock
def test_paged_fetch_total_changed_and_missing_cursor(client: HttpClient) -> None:
    pages = {0: _payload(_rows(0, 100), 0, 150), 100: _payload(_rows(100, 60), 100, 160)}
    respx.get(HISTORY_URL).mock(
        side_effect=lambda r: httpx.Response(200, json=pages[int(r.url.params["start"])])
    )
    with pytest.raises(IssPagingError, match="TOTAL changed"):
        fetch_all(client, HISTORY_URL)
    url2 = f"{ISS_BASE_URL}/nocursor.json"
    respx.get(url2).mock(
        return_value=httpx.Response(200, json={"history": {"columns": COLS, "data": []}})
    )
    with pytest.raises(IssPagingError, match="cursor block"):
        fetch_all(client, url2)


@respx.mock
def test_algopack_data_block_and_until_empty(client: HttpClient) -> None:
    url = "https://apim.moex.com/iss/datashop/algopack/fo/tradestats.json"
    data = {
        "data": {"columns": ["secid"], "data": [["SiZ6"]]},
        "data.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 1, 1000]]},
    }
    respx.get(url).mock(return_value=httpx.Response(200, json=data))
    assert fetch_all(client, url, block="data") == [{"secid": "SiZ6"}]

    sec_url = f"{ISS_BASE_URL}/securities.json"
    sec = {0: [["A"], ["B"]], 2: [["C"]], 3: []}
    respx.get(sec_url).mock(
        side_effect=lambda r: httpx.Response(
            200,
            json={"securities": {"columns": ["secid"], "data": sec[int(r.url.params["start"])]}},
        )
    )
    pages = list(iter_pages_until_empty(client, sec_url, block="securities"))
    assert [len(p.rows) for p in pages] == [2, 1, 0]


def test_converters() -> None:
    assert to_date("2026-10-15") == date(2026, 10, 15)
    assert to_date("0000-00-00") is None and to_date("") is None and to_date(None) is None
    with pytest.raises(IssFormatError):
        to_date("15.10.2026")
    with pytest.raises(IssFormatError):
        to_date("2026-02-30")
    assert to_decimal(0.1) == Decimal("0.1")
    assert to_decimal(101.235) == Decimal("101.235")
    assert to_decimal("1 234,5") == Decimal("1234.5")
    assert to_decimal(7) == Decimal(7) and to_decimal("") is None and to_decimal(None) is None
    for bad in (True, "abc", float("nan"), "Infinity", [1]):
        with pytest.raises(FormatDriftError):
            to_decimal(bad)
    assert to_time("19:00:01") == time(19, 0, 1)
    assert to_time("10:00") == time(10, 0)
    assert to_time("24:00:00") == time(23, 59, 59, 999_999)
    assert to_time("") is None
    with pytest.raises(IssFormatError):
        to_time("25:00:00")
    assert to_datetime("2026-10-15 19:00:00") == datetime(2026, 10, 15, 19, tzinfo=MSK)
    assert to_datetime("0000-00-00 00:00:00") is None
    with pytest.raises(IssFormatError):
        to_datetime("2026-10-15")
    assert to_int("42") == 42 and to_int(3.0) == 3 and to_int(None) is None
    with pytest.raises(IssFormatError):
        to_int(3.5)
    assert to_str("  x ") == "x" and to_str("  ") is None and to_str(5) == "5"


def test_decimal_helpers() -> None:
    assert mid(Decimal("1"), Decimal("2")) == Decimal("1.5")
    assert mid(Decimal("1"), None) is None
    assert quantize(Decimal("1.2345"), 2) == Decimal("1.23")
    assert quantize(Decimal("1.235"), 2) == Decimal("1.24")
    with pytest.raises(FormatDriftError, match="missing"):
        require_decimal(None, field="close")


def test_parse_cursor_pagesize_zero_only_when_empty() -> None:
    from mdhub.core.iss import IssFormatError, parse_cursor

    def cur(total: int, size: int) -> dict[str, object]:
        return {"x.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, total, size]]}}

    c = parse_cursor(cur(0, 0), "x.cursor")
    assert c is not None and c.total == 0 and c.pagesize == 0
    with pytest.raises(IssFormatError):
        parse_cursor(cur(5, 0), "x.cursor")
