"""md.bond_eod / md.bond_eod_yield: parsers on recorded ISS fixtures, paging, DB behaviour."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
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
from mdhub.core.timeutil import MSK
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


def test_no_data_day_within_grace_stays_open(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    """F10: ISS history for D-1 may be late; an empty answer the next morning leaves the gap
    open (re-checked), only an older empty day becomes unfillable."""
    today_is(date(2026, 10, 16))
    empty = _load("bonds_history_2026-10-15.json")
    empty["history"]["data"] = []
    _mock(iss, empty)
    assert _run({"mode": "final", "day": "2026-10-15"}, settings, db).status == "succeeded"
    rows = conn.execute("SELECT status, note FROM ops.data_gap ORDER BY dataset").fetchall()
    assert [r["status"] for r in rows] == ["open", "open"]
    assert "grace period" in rows[0]["note"]
    conn.commit()
    today_is(date(2026, 10, 19))
    assert _run({"mode": "final", "day": "2026-10-15"}, settings, db).status == "succeeded"
    statuses = conn.execute("SELECT DISTINCT status FROM ops.data_gap").fetchall()
    assert statuses == [{"status": "unfillable"}]


def test_no_data_day_is_recorded_not_failed(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    today_is: Callable[[date], None],
) -> None:
    today_is(date(2026, 10, 20))
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


def _empty_history() -> dict[str, Any]:
    empty = _load("bonds_history_2026-10-15.json")
    empty["history"]["data"] = []
    return empty


def _clock(monkeypatch: pytest.MonkeyPatch, start: datetime) -> list[float]:
    """Fake clock and sleep of the prelim retry loop; returns the recorded sleeps."""
    import mdhub.loaders.bonds_eod as be

    now = [start]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(be, "_now", lambda: now[0])
    monkeypatch.setattr(be, "_sleep", sleep)
    return sleeps


def test_prelim_no_data_is_partial_and_records_no_gap(
    iss: respx.MockRouter, settings: Settings, db: Database, conn: Conn
) -> None:
    """Review §6.4: the 19:20 prelim run found 0 rows on a trading day and ended
    ``succeeded``; 0 rows on a business day must end ``partial`` (still no gap: the final run
    of D+1 decides)."""
    _mock(iss, _empty_history())
    out = _run({"day": "2026-10-15", "mode": "prelim"}, settings, db)
    assert out.status == "partial"
    run = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert run is not None and "0 rows (< 1000)" in run["error"]
    assert conn.execute("SELECT count(*) AS n FROM ops.data_gap").fetchone() == {"n": 0}


def test_prelim_waits_for_history_until_threshold(
    iss: respx.MockRouter, db: Database, conn: Conn, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prelim run re-reads the history cursor every N minutes until it lists at least
    MIN_ROWS rows (here: published at the third check), then loads the day."""
    settings = Settings(database_url=db_url, iss_rps=1000.0, bonds_eod_prelim_min_rows=10)
    sleeps = _clock(monkeypatch, datetime(2026, 10, 15, 19, 20, tzinfo=MSK))
    full = history_handler(_load("bonds_history_2026-10-15.json"))
    empty = history_handler(_empty_history())
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if int(request.url.params.get("start", "0")) == 0:
            calls["n"] += 1
        return (empty if calls["n"] <= 2 else full)(request)

    iss.get(mm.BONDS_HISTORY_PATH).mock(side_effect=handler)
    iss.get(mm.BONDS_YIELDS_PATH).mock(
        side_effect=yields_handler(_load("bonds_yields_2026-10-15.json"))
    )
    out = _run({"day": "2026-10-15", "mode": "prelim"}, settings, db)
    assert out.status == "succeeded"
    assert sleeps == [300.0, 300.0]
    assert out.result["2026-10-15"]["history"] == 12
    assert out.result["2026-10-15"]["checks"] == 3
    n = conn.execute("SELECT count(*) AS n FROM md.bond_eod").fetchone()
    assert n == {"n": 12}


def test_prelim_gives_up_at_deadline_and_stores_intraday_close(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
    today_is: Callable[[date], None],
) -> None:
    """History still empty at the deadline (19:40): the run ends ``partial`` after the last
    check and stores one marketdata snapshot with the official close (LCLOSEPRICE), which the
    eod_preliminary cut publishes (basis=intraday_close)."""
    today_is(date(2026, 10, 16))
    sleeps = _clock(monkeypatch, datetime(2026, 10, 16, 19, 20, 5, tzinfo=MSK))
    _mock(iss, _empty_history())
    md = json.loads((FIX / "bonds_marketdata.json").read_text("utf-8"))
    cols = md["marketdata"]["columns"]
    for row in md["marketdata"]["data"]:
        row[cols.index("LCLOSEPRICE")] = 99.5
    iss.get(mm.BONDS_MARKETDATA_PATH).mock(return_value=httpx.Response(200, json=md))
    out = _run({"day": "2026-10-16", "mode": "prelim"}, settings, db)
    assert out.status == "partial"
    assert sum(sleeps) == pytest.approx(19 * 60 + 55)  # 5-min steps, the last one shortened
    assert out.result["2026-10-16"]["intraday_close"]["changed"] > 0
    err = conn.execute("SELECT error, rows_inserted FROM ops.job_run WHERE id = %s", (out.run_id,))
    row = err.fetchone()
    assert row is not None and "basis=intraday_close" in row["error"]
    assert row["rows_inserted"] == 0  # the intraday rows are not counted as EOD rows
    lc = conn.execute(
        "SELECT count(*) AS n FROM md.bond_quote_intraday WHERE legal_close = 99.5"
    ).fetchone()
    assert lc is not None and lc["n"] > 0


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
