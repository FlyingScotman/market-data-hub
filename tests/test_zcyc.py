"""md.zcyc_snapshot: KBD formula vs published yields (recorded fixtures), validation, storage."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.zcyc as zcyc_loader
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import moex_market as mm

FIX = Path(__file__).parent / "fixtures" / "moex_market"
Conn = psycopg.Connection[DictRow]


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def _run(params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job("zcyc", params, settings=settings, db=db, http_factory=_http_factory)


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=ISS_BASE_URL, assert_all_called=False) as router:
        yield router


def _serve(iss: respx.MockRouter, by_date: dict[str | None, dict[str, Any]]) -> respx.Route:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=by_date[request.url.params.get("date")])

    iss.routes.clear()
    return iss.get(mm.ZCYC_PATH).mock(side_effect=handler)


def _tamper(payload: dict[str, Any], name: str, delta: float) -> dict[str, Any]:
    cols = payload["params"]["columns"]
    payload["params"]["data"][0][cols.index(name)] += delta
    return payload


# ------------------------------------------------------------------------------ parser


@pytest.mark.parametrize(
    "name", ["zcyc_current.json", "zcyc_2026-10-15.json", "zcyc_2022-01-03.json"]
)
def test_kbd_reproduces_published_yields(name: str) -> None:
    snap = mm.parse_zcyc(_load(name))
    assert snap is not None
    assert len(snap.params) == 13 and len(snap.standard_yields) == 11
    check = mm.check_zcyc(snap)
    assert check.passed
    assert Decimal(0) <= check.max_error_bp < Decimal("0.01")
    assert set(check.errors_bp) == {repr(t) for t, _ in snap.standard_yields}


def test_parse_fields_and_errors() -> None:
    snap = mm.parse_zcyc(_load("zcyc_2026-10-15.json"), trade_date=date(2026, 10, 15))
    assert snap is not None
    assert snap.trade_time == time(18, 54, 59)
    assert snap.source_ts.isoformat() == "2026-10-15T18:54:59+03:00"
    assert snap.params["B1"] == 1506.334188
    assert snap.yields_json()[0] == {"tenor_years": 0.25, "yield_pct": 12.4175}
    assert mm.parse_zcyc(_load("zcyc_2026-10-17_empty.json")) is None
    with pytest.raises(IssFormatError, match="requested"):
        mm.parse_zcyc(_load("zcyc_2026-10-15.json"), trade_date=date(2026, 10, 14))
    skewed = _load("zcyc_2026-10-15.json")
    skewed["yearyields"]["data"][3][1] = "18:40:00"
    with pytest.raises(mm.ZcycClockSkewError):
        mm.parse_zcyc(skewed)
    missing = _load("zcyc_2026-10-15.json")
    missing["params"]["columns"][-1] = "G10"
    with pytest.raises(IssFormatError, match="G9"):
        mm.parse_zcyc(missing)


def test_check_fails_on_tampered_params() -> None:
    snap = mm.parse_zcyc(_tamper(_load("zcyc_2026-10-15.json"), "G3", 0.01))
    assert snap is not None
    check = mm.check_zcyc(snap)
    assert not check.passed and check.max_error_bp >= Decimal("0.01")


def test_dataset_registered() -> None:
    spec = get_dataset("zcyc")
    assert spec.history_start == date(2014, 1, 6) and spec.where == "is_eod"


# ---------------------------------------------------------------------------- DB tests


def _snapshots(conn: Conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT trade_date, trade_time, is_eod, check_max_error_bp, params ->> 'B1' AS b1, "
        "jsonb_array_length(standard_yields) AS n FROM md.zcyc_snapshot "
        "ORDER BY trade_date, trade_time"
    ).fetchall()
    conn.commit()
    return rows


def test_intraday_dedup_and_eod_flag(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    route = _serve(
        iss, {None: _load("zcyc_2026-10-15.json"), "2026-10-15": _load("zcyc_2026-10-15.json")}
    )
    assert _run({}, settings, db).status == "succeeded"
    assert _run({"mode": "intraday"}, settings, db).result["inserted"] == 0
    assert "date" not in route.calls.last.request.url.params
    rows = _snapshots(conn)
    assert len(rows) == 1 and rows[0]["is_eod"] is False
    assert rows[0]["b1"] == "1506.334188" and rows[0]["n"] == 11
    assert rows[0]["check_max_error_bp"] < Decimal("0.01")

    out = _run({"mode": "final", "day": "2026-10-15"}, settings, db)
    assert out.status == "succeeded"
    assert route.calls.last.request.url.params["date"] == "2026-10-15"
    assert [r["is_eod"] for r in _snapshots(conn)] == [True]
    # a later intraday poll of the same publication does not reset the flag
    _run({}, settings, db)
    assert [r["is_eod"] for r in _snapshots(conn)] == [True]
    # flipping is_eod is not a revision
    assert conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone() == {"n": 0}
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == {"n": 4}


def test_one_eod_per_day(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    early = _load("zcyc_2026-10-15.json")
    for block in ("params", "yearyields"):
        for r in early[block]["data"]:
            r[1] = "18:30:00"
    _serve(iss, {"2026-10-15": early})
    _run({"mode": "eod", "day": "2026-10-15"}, settings, db)
    _serve(iss, {"2026-10-15": _load("zcyc_2026-10-15.json")})
    _run({"mode": "final", "day": "2026-10-15"}, settings, db)
    rows = _snapshots(conn)
    assert [(r["trade_time"], r["is_eod"]) for r in rows] == [
        (time(18, 30), False),
        (time(18, 54, 59), True),
    ]


def test_failed_check_is_rejected(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    _serve(iss, {None: _tamper(_load("zcyc_current.json"), "B1", 0.5)})
    out = _run({}, settings, db)
    assert out.status == "partial"
    assert "rejected" in out.result
    assert _snapshots(conn) == []
    rej = conn.execute("SELECT trade_date, reason, max_error_bp FROM md.zcyc_reject").fetchone()
    assert rej is not None and rej["trade_date"] == date(2026, 10, 16)
    assert "not reproduced" in rej["reason"] and rej["max_error_bp"] > Decimal("0.01")
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == {"n": 1}


def test_clock_skew_retried_then_rejected(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zcyc_loader.time, "sleep", lambda _s: None)
    skewed = _load("zcyc_current.json")
    skewed["yearyields"]["data"][0][1] = "10:00:00"
    route = _serve(iss, {None: skewed})
    out = _run({}, settings, db)
    assert out.status == "partial" and route.call_count == 2
    assert conn.execute("SELECT count(*) AS n FROM md.zcyc_reject").fetchone() == {"n": 1}
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == {"n": 2}


def test_no_curve_day_recorded(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    _serve(iss, {"2026-10-14": _load("zcyc_2026-10-17_empty.json")})
    out = _run({"date_from": "2026-10-14", "date_to": "2026-10-14"}, settings, db)
    assert out.status == "succeeded"
    gap = conn.execute("SELECT dataset, status FROM ops.data_gap").fetchone()
    assert gap == {"dataset": "zcyc", "status": "unfillable"}
