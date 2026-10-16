"""md.bond_eod / md.bond_eod_yield: parsers on recorded ISS fixtures, paging, DB behaviour."""

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

import mdhub.loaders.bonds_eod  # noqa: F401 - registers the job
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, IssPagingError, parse_block
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import moex_market as mm

FIX = Path(__file__).parent / "fixtures" / "moex_market"
DAY = date(2026, 10, 15)
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
    return run_job("bonds_eod", params, settings=settings, db=db, http_factory=_http_factory)


def history_handler(
    payload: dict[str, Any], *, pagesize: int = 5, total: int | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    cols = payload["history"]["columns"]
    rows = payload["history"]["data"]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        body = {
            "history": {"columns": cols, "data": rows[start : start + pagesize]},
            "history.cursor": {
                "columns": CURSOR_COLS,
                "data": [[start, len(rows) if total is None else total, pagesize]],
            },
        }
        return httpx.Response(200, json=body)

    return handler


def yields_handler(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    cols = payload["history_yields"]["columns"]
    rows = payload["history_yields"]["data"]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("start", "0"))
        chunk = rows[start : start + mm.YIELDS_PAGESIZE]
        return httpx.Response(200, json={"history_yields": {"columns": cols, "data": chunk}})

    return handler


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=ISS_BASE_URL, assert_all_called=False) as router:
        yield router


def _mock(
    iss: respx.MockRouter,
    history: dict[str, Any] | None = None,
    yields: dict[str, Any] | None = None,
    **kw: Any,
) -> tuple[respx.Route, respx.Route]:
    h = iss.get(mm.BONDS_HISTORY_PATH).mock(
        side_effect=history_handler(history or _load("bonds_history_2026-10-15.json"), **kw)
    )
    y = iss.get(mm.BONDS_YIELDS_PATH).mock(
        side_effect=yields_handler(yields or _load("bonds_yields_2026-10-15.json"))
    )
    return h, y


# ----------------------------------------------------------------------------- parsers


def test_parse_history_fixture_keeps_every_board() -> None:
    payload = _load("bonds_history_2026-10-15.json")
    raw = parse_block(payload, "history", required=mm.BOND_HISTORY_REQUIRED)
    rows = mm.parse_bond_history(raw, DAY)
    assert len(rows) == 12
    by_key = {(r["secid"], r["board"]): r for r in rows}
    tqcb, tqod = by_key[("BYM000001818", "TQCB")], by_key[("BYM000001818", "TQOD")]
    assert tqcb["close"] == Decimal("100.9999") and tqcb["currency_id"] == "SUR"
    assert tqod["close"] is None and tqod["currency_id"] == "USD"
    assert tqod["legal_close"] == Decimal("100.9999")
    assert tqcb["accrued_int"] == Decimal("1894.0432")
    assert tqcb["num_trades"] == 3 and tqcb["face_unit"] == "USD"
    assert tqcb["mat_date"] == date(2027, 6, 29)
    assert isinstance(tqcb["yield_at_waprice_pct"], Decimal)


def test_parse_history_rejects_other_date_and_duplicates() -> None:
    raw = parse_block(_load("bonds_history_2026-10-15.json"), "history")
    with pytest.raises(IssFormatError, match="TRADEDATE"):
        mm.parse_bond_history(raw, date(2026, 10, 14))
    with pytest.raises(IssFormatError, match="duplicate"):
        mm.parse_bond_history([raw[0], raw[0]], DAY)


def test_parse_history_drift_fails_closed() -> None:
    payload = _load("bonds_history_2026-10-15.json")
    idx = payload["history"]["columns"].index("LEGALCLOSEPRICE")
    payload["history"]["columns"].pop(idx)
    for r in payload["history"]["data"]:
        r.pop(idx)
    with pytest.raises(IssFormatError, match="LEGALCLOSEPRICE"):
        parse_block(payload, "history", required=mm.BOND_HISTORY_REQUIRED)


def test_parse_yields_drops_empty_rows() -> None:
    raw = parse_block(_load("bonds_yields_2026-10-15.json"), "history_yields")
    rows = mm.parse_bond_yields(raw, DAY)
    assert len(raw) == 12
    assert {r["board"] for r in rows} == {"TQCB"}
    first = next(r for r in rows if r["secid"] == "BYM000001818")
    assert first["effective_yield_pct"] == Decimal("6.2201")
    assert first["yield_date_type"] == "MATDATE"
    assert first["zcyc_moment"].isoformat() == "2026-10-15T18:54:59+03:00"


def test_dataset_registered() -> None:
    spec = get_dataset("bonds_eod")
    assert spec.table == "md.bond_eod" and spec.backfill_job == "bonds_eod"
    assert spec.history_start == date(2024, 1, 3) and spec.min_rows_per_day == 1000


# --------------------------------------------------------------------------- DB tests


def _counts(conn: Conn) -> dict[str, int]:
    out = {}
    for t in ("md.bond_eod", "md.bond_eod_yield", "ops.row_history", "ops.raw_capture"):
        row = conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()
        assert row is not None
        out[t] = int(row["n"])
    conn.commit()
    return out


def test_load_paged_idempotent_and_revision(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    history = _load("bonds_history_2026-10-15.json")
    h, y = _mock(iss, history)
    out = _run({"day": "2026-10-15"}, settings, db)
    assert out.status == "succeeded"
    assert h.call_count == 3  # 12 rows, PAGESIZE 5
    assert y.call_count == 1
    c = _counts(conn)
    assert c == {
        "md.bond_eod": 12,
        "md.bond_eod_yield": 4,
        "ops.row_history": 0,
        "ops.raw_capture": 4,
    }
    boards = conn.execute(
        "SELECT board FROM md.bond_eod WHERE secid = 'BYM000001818' ORDER BY board"
    ).fetchall()
    assert [r["board"] for r in boards] == ["TQCB", "TQOD"]
    conn.commit()

    # identical reload: no data change, no history
    out2 = _run({"day": "2026-10-15"}, settings, db)
    assert out2.status == "succeeded"
    run = conn.execute(
        "SELECT rows_inserted, rows_updated, rows_unchanged FROM ops.job_run WHERE id = %s",
        (out2.run_id,),
    ).fetchone()
    assert run == {"rows_inserted": 0, "rows_updated": 0, "rows_unchanged": 16}
    assert _counts(conn)["ops.row_history"] == 0

    # post-session revision of one close -> one history row with the old value
    revised = copy.deepcopy(history)
    ci = revised["history"]["columns"].index("CLOSE")
    revised["history"]["data"][0][ci] = 101.5
    iss.routes.clear()
    _mock(iss, revised)
    out3 = _run({"day": "2026-10-15", "mode": "final"}, settings, db)
    assert out3.status == "succeeded"
    hist = conn.execute(
        "SELECT table_name, row_key, old_row ->> 'close' AS old_close, replaced_by_run "
        "FROM ops.row_history"
    ).fetchall()
    assert hist == [
        {
            "table_name": "md.bond_eod",
            "row_key": {"board": "TQCB", "secid": "BYM000001818", "trade_date": "2026-10-15"},
            "old_close": "100.9999",
            "replaced_by_run": out3.run_id,
        }
    ]
    new = conn.execute(
        "SELECT close FROM md.bond_eod WHERE secid = 'BYM000001818' AND board = 'TQCB'"
    ).fetchone()
    assert new == {"close": Decimal("101.5")}


def test_total_mismatch_fails_and_commits_nothing(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    _mock(iss, total=13)
    with pytest.raises(IssPagingError):
        _run({"day": "2026-10-15"}, settings, db)
    assert _counts(conn) == {
        "md.bond_eod": 0,
        "md.bond_eod_yield": 0,
        "ops.row_history": 0,
        "ops.raw_capture": 0,
    }
    row = conn.execute("SELECT status FROM ops.job_run").fetchone()
    assert row == {"status": "failed"}


def test_yields_count_must_match_history_total(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    yields = _load("bonds_yields_2026-10-15.json")
    yields["history_yields"]["data"].pop()
    _mock(iss, yields=yields)
    with pytest.raises(IssPagingError, match="history TOTAL"):
        _run({"day": "2026-10-15"}, settings, db)
    assert _counts(conn)["md.bond_eod"] == 0


def test_yields_multi_page(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mm, "YIELDS_PAGESIZE", 4)
    _, y = _mock(iss)
    assert _run({"day": "2026-10-15"}, settings, db).status == "succeeded"
    assert y.call_count == 4  # 4 + 4 + 4 + empty page
    assert _counts(conn)["md.bond_eod_yield"] == 4


def test_no_data_day_is_recorded_not_failed(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    empty = _load("bonds_history_2026-10-15.json")
    empty["history"]["data"] = []
    _, y = _mock(iss, empty)
    out = _run({"date_from": "2026-10-15", "date_to": "2026-10-15"}, settings, db)
    assert out.status == "succeeded"
    assert out.result == {"2026-10-15": {"history": 0, "yields": 0, "no_data": True}}
    assert y.call_count == 0
    gaps = conn.execute("SELECT dataset, status FROM ops.data_gap ORDER BY dataset").fetchall()
    assert gaps == [
        {"dataset": "bonds_eod", "status": "unfillable"},
        {"dataset": "bonds_eod_yield", "status": "unfillable"},
    ]
    conn.commit()
    # data arrives later: gap closed
    iss.routes.clear()
    _mock(iss)
    assert _run({"day": "2026-10-15"}, settings, db).status == "succeeded"
    statuses = conn.execute("SELECT DISTINCT status FROM ops.data_gap").fetchall()
    assert statuses == [{"status": "backfilled"}]


def test_prelim_no_data_records_no_gap(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    empty = _load("bonds_history_2026-10-15.json")
    empty["history"]["data"] = []
    _mock(iss, empty)
    assert _run({"day": "2026-10-15", "mode": "prelim"}, settings, db).status == "succeeded"
    assert conn.execute("SELECT count(*) AS n FROM ops.data_gap").fetchone() == {"n": 0}


def test_date_range_iterates_business_days(
    iss: respx.MockRouter, settings: Settings, db: Database
) -> None:
    seen: list[str] = []
    base = history_handler(_load("bonds_history_2026-10-15.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        d = request.url.params["date"]
        seen.append(d)
        if d == "2026-10-19":
            empty = _load("bonds_history_2026-10-15.json")
            empty["history"]["data"] = []
            return history_handler(empty)(request)
        if d != "2026-10-15":
            return httpx.Response(
                200,
                json={
                    "history": {"columns": ["BOARDID", "TRADEDATE", "SECID"], "data": []},
                    "history.cursor": {"columns": CURSOR_COLS, "data": [[0, 0, 100]]},
                },
            )
        return base(request)

    iss.get(mm.BONDS_HISTORY_PATH).mock(side_effect=handler)
    iss.get(mm.BONDS_YIELDS_PATH).mock(
        side_effect=yields_handler(_load("bonds_yields_2026-10-15.json"))
    )
    out = _run({"date_from": "2026-10-15", "date_to": "2026-10-19"}, settings, db)
    # 2026-10-16 fails (format drift: columns missing) -> partial, others fine; weekend skipped
    assert out.status == "partial"
    assert sorted(set(seen)) == ["2026-10-15", "2026-10-16", "2026-10-19"]
    assert out.result["2026-10-15"]["history"] == 12
    assert "error" in out.result["2026-10-16"]
    assert out.result["2026-10-19"]["no_data"] is True


def test_bad_mode_rejected(settings: Settings, db: Database) -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        _run({"mode": "nope", "day": "2026-10-15"}, settings, db)
