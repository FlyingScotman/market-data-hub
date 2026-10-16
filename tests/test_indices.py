"""md.index_eod: parsers on recorded ISS fixtures, history by date, same-day marketdata."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.jobs
import mdhub.loaders.indices
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, parse_block
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import moex_market as mm

FIX = Path(__file__).parent / "fixtures" / "moex_market"
D1 = date(2026, 10, 15)
TODAY = date(2026, 10, 16)
Conn = psycopg.Connection[DictRow]
CURSOR_COLS = ["INDEX", "TOTAL", "PAGESIZE"]


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _run(params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job("indices_eod", params, settings=settings, db=db, http_factory=_http_factory)


def _history_for(day: date) -> dict[str, Any]:
    """The 2026-10-15 fixture re-dated (only for days other than the recorded one)."""
    payload = _load("index_history_2026-10-15.json")
    if day != D1:
        cols = payload["history"]["columns"]
        for r in payload["history"]["data"]:
            r[cols.index("TRADEDATE")] = day.isoformat()
    return payload


def history_handler(
    days: dict[str, dict[str, Any]], pagesize: int = 10
) -> Callable[[httpx.Request], httpx.Response]:
    empty_cols = _load("index_history_2026-10-15.json")["history"]["columns"]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        payload = days.get(request.url.params["date"])
        cols = payload["history"]["columns"] if payload else empty_cols
        rows = payload["history"]["data"] if payload else []
        body = {
            "history": {"columns": cols, "data": rows[start : start + pagesize]},
            "history.cursor": {"columns": CURSOR_COLS, "data": [[start, len(rows), pagesize]]},
        }
        return httpx.Response(200, json=body)

    return handler


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=ISS_BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def today(monkeypatch: pytest.MonkeyPatch) -> date:
    monkeypatch.setattr(mdhub.jobs, "msk_today", lambda: TODAY)
    return TODAY


def test_parse_history_fixture() -> None:
    raw = parse_block(
        _load("index_history_2026-10-15.json"), "history", required=mm.INDEX_HISTORY_REQUIRED
    )
    rows = {r["secid"]: r for r in mm.parse_index_history(raw, D1)}
    assert rows["RUSFAR"]["close"] == Decimal("13.78") and rows["RUSFAR"]["board"] == "MMIX"
    assert rows["RUSFARCNY"]["close"] == Decimal("0.36")
    assert rows["RGBI"]["yield_pct"] == Decimal("16.03")
    assert rows["RGBI"]["duration_days"] == Decimal("1648")
    assert {r["basis"] for r in rows.values()} == {"history"}
    with pytest.raises(IssFormatError, match="TRADEDATE"):
        mm.parse_index_history(raw, TODAY)


def test_parse_marketdata_only_today_values() -> None:
    payload = _load("index_marketdata.json")
    rows = {r["secid"]: r for r in mm.parse_index_marketdata(payload, TODAY)}
    # RUSFAR (fixed 12:30) still shows yesterday at 10:25 -> skipped; RT and RGBI are today's
    assert "RUSFAR" not in rows
    assert rows["RUSFARRT"]["close"] == Decimal("13.77")
    assert rows["RUSFARRT"]["basis"] == "marketdata"
    assert rows["RUSFARRT"]["source_ts"].isoformat() == "2026-10-16T10:15:01+03:00"
    only = mm.parse_index_marketdata(payload, D1, mm.INDEX_GROUPS["rusfar"])
    assert {r["secid"] for r in only} == {
        "RUSFAR",
        "RUSFAR1W",
        "RUSFAR2W",
        "RUSFAR1M",
        "RUSFAR3M",
        "RUSFARCNY",
        "RUSFARCN1W",
    }


def test_groups_and_datasets() -> None:
    assert mm.index_group_secids("all") is None
    assert "RUSFARRT" in (mm.index_group_secids("rusfarrt") or ())
    with pytest.raises(ValueError, match="unknown index group"):
        mm.index_group_secids("nope")
    spec = get_dataset("indices_rusfar")
    assert spec.series_column == "secid" and "RUSFAR" in spec.series
    assert get_dataset("indices_all").backfill_job == "indices_eod"


def test_backfill_day_idempotent(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    route = iss.get(mm.INDEX_HISTORY_PATH).mock(
        side_effect=history_handler({D1.isoformat(): _history_for(D1)})
    )
    md = iss.get(mm.INDEX_MARKETDATA_PATH).mock(return_value=httpx.Response(500))
    out = _run({"date_from": "2026-10-15", "date_to": "2026-10-15"}, settings, db)
    assert out.status == "succeeded"
    assert route.call_count == 3 and md.call_count == 0
    n = conn.execute("SELECT count(*) AS n FROM md.index_eod WHERE basis = 'history'").fetchone()
    assert n == {"n": 26}
    conn.commit()
    again = _run({"day": "2026-10-15"}, settings, db)
    assert again.result["2026-10-15"]["inserted"] == 0
    assert again.result["2026-10-15"]["updated"] == 0
    assert conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone() == {"n": 0}


def test_scheduled_rusfarrt_marketdata_then_history(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn, today: date
) -> None:
    days = {D1.isoformat(): _history_for(D1)}
    iss.get(mm.INDEX_HISTORY_PATH).mock(side_effect=history_handler(days))
    md = iss.get(mm.INDEX_MARKETDATA_PATH).mock(
        return_value=httpx.Response(200, json=_load("index_marketdata.json"))
    )
    out = _run({"group": "rusfarrt"}, settings, db)
    assert out.status == "succeeded"
    assert out.result["2026-10-16"]["history"] == 0
    assert out.result["2026-10-16"]["marketdata"] == 2  # RUSFARRT, RUSFARCNRT
    assert out.result["2026-10-15"]["history"] == 26
    assert md.calls.last.request.url.params["securities"].startswith("RUSFARRT,")
    row = conn.execute(
        "SELECT close, basis FROM md.index_eod WHERE trade_date = %s AND secid = 'RUSFARRT'",
        (TODAY,),
    ).fetchone()
    assert row == {"close": Decimal("13.77"), "basis": "marketdata"}
    # no gap recorded for "today has no history yet"
    assert conn.execute("SELECT count(*) AS n FROM ops.data_gap").fetchone() == {"n": 0}
    conn.commit()

    # the evening history for today arrives with a different final value -> replaces it
    hist = _history_for(TODAY)
    cols = hist["history"]["columns"]
    for r in hist["history"]["data"]:
        if r[cols.index("SECID")] == "RUSFARRT":
            r[cols.index("CLOSE")] = 13.7
    days[TODAY.isoformat()] = hist
    out2 = _run({"group": "all"}, settings, db)
    assert out2.result["2026-10-16"] == {
        "history": 26,
        "marketdata": 0,
        "inserted": 24,
        "updated": 2,
    }
    row = conn.execute(
        "SELECT close, basis FROM md.index_eod WHERE trade_date = %s AND secid = 'RUSFARRT'",
        (TODAY,),
    ).fetchone()
    assert row == {"close": Decimal("13.7"), "basis": "history"}
    old = conn.execute(
        "SELECT old_row ->> 'basis' AS basis, old_row ->> 'close' AS close FROM ops.row_history "
        "WHERE row_key ->> 'secid' = 'RUSFARRT'"
    ).fetchone()
    assert old == {"basis": "marketdata", "close": "13.77"}


def test_missing_history_day_recorded(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    iss.get(mm.INDEX_HISTORY_PATH).mock(side_effect=history_handler({}))
    out = _run({"day": "2026-10-14"}, settings, db)
    assert out.status == "succeeded"
    gap = conn.execute("SELECT dataset, gap_date, status FROM ops.data_gap").fetchone()
    assert gap == {"dataset": "indices_all", "gap_date": date(2026, 10, 14), "status": "unfillable"}


def test_duplicate_secid_across_pages_fails(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    payload = _history_for(D1)
    payload["history"]["data"].append(copy.deepcopy(payload["history"]["data"][0]))
    iss.get(mm.INDEX_HISTORY_PATH).mock(side_effect=history_handler({D1.isoformat(): payload}))
    with pytest.raises(ValueError, match="duplicate"):
        _run({"day": "2026-10-15"}, settings, db)
    assert conn.execute("SELECT count(*) AS n FROM md.index_eod").fetchone() == {"n": 0}


def test_unknown_group_rejected(settings: Settings, db: Database) -> None:
    with pytest.raises(ValueError, match="unknown index group"):
        _run({"group": "x"}, settings, db)
