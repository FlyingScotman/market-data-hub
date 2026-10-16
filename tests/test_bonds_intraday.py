"""md.bond_quote_intraday: snapshot parser (recorded ISS fixture) and change-only storage."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.bonds_intraday  # noqa: F401 - registers the job
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError
from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import moex_market as mm

FIX = Path(__file__).parent / "fixtures" / "moex_market"
Conn = psycopg.Connection[DictRow]


def _load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / "bonds_marketdata.json").read_text("utf-8"))
    return data


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _run(settings: Settings, db: Database) -> JobOutcome:
    return run_job("bonds_intraday", {}, settings=settings, db=db, http_factory=_http_factory)


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=ISS_BASE_URL, assert_all_called=False) as router:
        yield router


def _serve(iss: respx.MockRouter, payload: dict[str, Any]) -> respx.Route:
    iss.routes.clear()
    return iss.get(mm.BONDS_MARKETDATA_PATH).mock(return_value=httpx.Response(200, json=payload))


def _set(payload: dict[str, Any], secid: str, board: str, column: str, value: Any) -> None:
    cols = payload["marketdata"]["columns"]
    for r in payload["marketdata"]["data"]:
        if r[cols.index("SECID")] == secid and r[cols.index("BOARDID")] == board:
            r[cols.index(column)] = value
            return
    raise KeyError((secid, board))


def test_parse_snapshot_fixture() -> None:
    snap = mm.parse_bond_marketdata(_load())
    assert len(snap.rows) == 30
    assert snap.latency == "delayed"
    assert snap.min_lag_seconds == 900.0
    assert snap.source_ts == datetime(2026, 10, 16, 10, 25, 18, tzinfo=MSK)
    by = {(r["secid"], r["board"]): r for r in snap.rows}
    ofz = by[("SU26207RMFS9", "TQOB")]
    assert ofz["bid"] == Decimal("98.682")
    assert ofz["ts"] == datetime(2026, 10, 16, 10, 10, 0, tzinfo=MSK)
    assert ofz["latency"] == "delayed"
    usd = by[("BYM000001818", "TQCB")]
    assert usd["gspread_bp"] is not None and usd["yield_date_type"] == "MATDATE"
    assert by[("SU26207RMFS9", "SPOB")]["gspread_bp"] is None  # no yields row


def test_quote_ts_and_latency_rules() -> None:
    sys_ = datetime(2026, 10, 16, 6, 24, 11, tzinfo=MSK)
    assert mm._quote_ts(sys_, time(18, 59, 0)) == datetime(2026, 10, 15, 18, 59, tzinfo=MSK)
    assert mm._quote_ts(sys_, time(6, 9, 11)) == datetime(2026, 10, 16, 6, 9, 11, tzinfo=MSK)
    assert mm.detect_latency([900.0, 1200.0]) == ("delayed", 900.0)
    assert mm.detect_latency([1.0, 900.0]) == ("realtime", 1.0)
    assert mm.detect_latency([]) == ("delayed", None)


def test_parse_drift_and_duplicates() -> None:
    payload = _load()
    payload["marketdata"]["data"].append(copy.deepcopy(payload["marketdata"]["data"][0]))
    with pytest.raises(IssFormatError, match="duplicate"):
        mm.parse_bond_marketdata(payload)
    payload = _load()
    idx = payload["marketdata"]["columns"].index("OFFER")
    payload["marketdata"]["columns"][idx] = "OFFER_X"
    with pytest.raises(IssFormatError, match="OFFER"):
        mm.parse_bond_marketdata(payload)


def _count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    conn.commit()
    assert row is not None
    return int(row["n"])


def test_change_only_storage(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    payload = _load()
    _serve(iss, payload)
    first = _run(settings, db)
    assert first.status == "succeeded"
    assert first.result["changed"] == 30 and first.result["latency"] == "delayed"
    assert _count(conn, "md.bond_quote_intraday") == 30
    assert _count(conn, "md.bond_quote_last") == 30

    # same snapshot again: raw captured, nothing stored
    second = _run(settings, db)
    assert second.result["changed"] == 0
    assert _count(conn, "md.bond_quote_intraday") == 30
    assert _count(conn, "ops.raw_capture") == 2

    # SYSTIME-only movement is not a change either
    moved = copy.deepcopy(payload)
    _set(moved, "SU26207RMFS9", "TQOB", "SYSTIME", "2026-10-16 10:40:01")
    _serve(iss, moved)
    assert _run(settings, db).result["changed"] == 0

    # a new bid with a new UPDATETIME -> exactly one new row, last state follows
    _set(moved, "SU26207RMFS9", "TQOB", "BID", 98.7)
    _set(moved, "SU26207RMFS9", "TQOB", "UPDATETIME", "10:12:30")
    _serve(iss, moved)
    third = _run(settings, db)
    assert third.result["changed"] == 1
    rows = conn.execute(
        "SELECT ts, bid FROM md.bond_quote_intraday "
        "WHERE secid = 'SU26207RMFS9' AND board = 'TQOB' ORDER BY ts"
    ).fetchall()
    assert [r["bid"] for r in rows] == [Decimal("98.682"), Decimal("98.7")]
    assert rows[1]["ts"] == datetime(2026, 10, 16, 10, 12, 30, tzinfo=MSK)
    last = conn.execute(
        "SELECT bid, ts FROM md.bond_quote_last WHERE secid = 'SU26207RMFS9' AND board = 'TQOB'"
    ).fetchone()
    assert last == {"bid": Decimal("98.7"), "ts": datetime(2026, 10, 16, 10, 12, 30, tzinfo=MSK)}
    assert _count(conn, "ops.row_history") == 0


def test_same_ts_change_updates_with_history(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    payload = _load()
    _serve(iss, payload)
    _run(settings, db)
    _set(payload, "SU26207RMFS9", "TQOB", "NUMBIDS", 99)  # same UPDATETIME
    _serve(iss, payload)
    out = _run(settings, db)
    assert out.result["changed"] == 1
    assert _count(conn, "md.bond_quote_intraday") == 30
    assert _count(conn, "ops.row_history") == 1
