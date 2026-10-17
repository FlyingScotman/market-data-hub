"""FORTS FX futures: parsers, normalization math, futures_* / algopack jobs against PostgreSQL."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.futures  # noqa: F401 - registers the jobs
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import APIM_BASE_URL, ISS_BASE_URL, IssFormatError
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.loaders.futures import refresh_futures_contracts
from mdhub.sources import moex_apim as apim
from mdhub.sources import moex_fx as fx

FIX = Path(__file__).parent / "fixtures" / "moex_fx"
Conn = psycopg.Connection[DictRow]
CURSOR = ["INDEX", "TOTAL", "PAGESIZE"]
KEY = "test-apim-key"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def run(name: str, params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job(name, params, settings=settings, db=db, http_factory=http_factory)


def count(conn: Conn, sql: str) -> int:
    conn.commit()
    row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return int(next(iter(row.values())))


@pytest.fixture
def apim_settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, iss_rps=1000.0, moex_api_key=KEY)  # type: ignore[arg-type]


def rows_of(payload: dict[str, Any], block: str) -> list[dict[str, Any]]:
    cols = payload[block]["columns"]
    return [dict(zip(cols, r, strict=True)) for r in payload[block]["data"]]


def seed_ref(conn: Conn) -> None:
    refresh_futures_contracts(
        conn,
        fx.parse_futures_securities(load("rfud_securities.json")),
        capture_id=None,  # type: ignore[arg-type]
        run_id=None,  # type: ignore[arg-type]
        today=date(2026, 10, 16),
    )
    conn.execute("UPDATE ref.futures_contract SET last_seen = '2026-10-16'")
    conn.commit()


@pytest.fixture(autouse=True)
def _recorded_today(today_is: Callable[[date], None]) -> None:
    """The responses were recorded on 2026-10-16 (the daily contract bootstrap and the
    listing depend on "today")."""
    today_is(date(2026, 10, 16))


# ---------------------------------------------------------------------- parsers


def test_parse_futures_securities_classification() -> None:
    by = {r["secid"]: r for r in fx.parse_futures_securities(load("rfud_securities.json"))}
    usd = by["USDRUBF"]
    assert (usd["is_fx"], usd["contract_type"], usd["base_ccy"], usd["quote_ccy"]) == (
        True,
        "perpetual",
        "USD",
        "RUB",
    )
    assert usd["last_trade_date"] is None and usd["last_del_date"] is None
    si = by["SiZ6"]
    assert (si["contract_type"], si["last_trade_date"]) == ("dated", date(2026, 12, 17))
    assert (by["CRZ6"]["base_ccy"], by["EDZ6"]["quote_ccy"], by["UCZ6"]["base_ccy"]) == (
        "CNY",
        "USD",
        "USD",
    )
    assert by["KZZ6"]["is_fx"]
    assert not by["GLDRUBF"]["is_fx"]  # gold perpetual is not FX
    assert by["GLDRUBF"]["contract_type"] == "perpetual"
    assert not by["IMOEXF"]["is_fx"] and not by["RBZ6"]["is_fx"]


@pytest.mark.parametrize(
    ("secid", "raw", "expected"),
    [
        ("SiZ6", "85219", "85.219"),  # 1 / 1 / 1000
        ("CRZ6", "12.746", "12.746"),  # 1 / 0.001 / 1000
        ("USDRUBF", "83.76", "83.76"),  # 10 / 0.01 / 1000
        ("EDZ6", "1.1227", "94.0373520"),  # 8.376 / 0.0001 / 1000 (USD/RUB inside STEPPRICE)
        ("UCZ6", "6.682", "83.6192162"),  # 12.5141 / 0.001 / 1000
        ("KZZ6", "18.578", "0.18578"),  # quoted per 100 KZT: 1 / 0.001 / 100000
    ],
)
def test_normalization_math(secid: str, raw: str, expected: str) -> None:
    spec = {r["secid"]: r for r in fx.parse_futures_securities(load("rfud_securities.json"))}[secid]
    factor = fx.price_factor(spec["step_price"], spec["min_step"], spec["lot_volume"])
    assert fx.normalize_price(Decimal(raw), factor) == Decimal(expected)


def test_price_factor_missing() -> None:
    assert fx.price_factor(None, Decimal(1), Decimal(1)) is None
    assert fx.price_factor(Decimal(1), Decimal(0), Decimal(1)) is None
    assert fx.normalize_price(None, Decimal(1)) is None


def test_parse_forts_history() -> None:
    raw = rows_of(load("forts_history_p0.json"), "history") + rows_of(
        load("forts_history_p1.json"), "history"
    )
    rows, skipped = fx.parse_forts_history(raw)
    assert [s["SECID"] for s in skipped] == ["92V692X6"]  # calendar spread
    by = {r["secid"]: r for r in rows}
    si = by["SiZ6"]
    assert si["settle_price"] == Decimal("85219.0") and si["open_position"] == Decimal("8805546")
    assert si["num_trades"] > 0
    dead = by["SiH4"]  # no trades: zero prices are missing, not zero
    assert dead["close"] is None and dead["settle_price"] is None and dead["num_trades"] == 0
    with pytest.raises(IssFormatError, match="lacks"):
        fx.parse_forts_history([{"SECID": "SiZ6"}])


def test_parse_security_description() -> None:
    spec = fx.parse_security_description(load("security_SiH4.json"), "SiH4")
    assert spec["contract_type"] == "dated"
    assert spec["last_trade_date"] == date(2024, 3, 21)
    assert spec["lot_volume"] == Decimal(1000) and spec["is_fx"]
    with pytest.raises(IssFormatError, match="SECID"):
        fx.parse_security_description(load("security_SiH4.json"), "SiM4")


def test_parse_futures_marketdata_latency() -> None:
    factors = {"SiZ6": Decimal("0.001"), "EDZ6": None}
    rows, latency = fx.parse_futures_marketdata(load("forts_md_apim.json"), factors)
    assert latency == "realtime"
    assert {r["secid"] for r in rows} == {"SiZ6", "EDZ6"}
    si = next(r for r in rows if r["secid"] == "SiZ6")
    assert si["last_rub_per_unit"] == si["last"] * Decimal("0.001")
    _, latency = fx.parse_futures_marketdata(load("forts_md_iss.json"), factors)
    assert latency == "delayed"


def test_parse_algopack() -> None:
    ts = apim.parse_tradestats(rows_of(load("algopack_tradestats_latest.json"), "data"))
    assert ts[0]["interval_ts"].isoformat() == "2026-10-16T10:25:00+03:00"
    assert isinstance(ts[0]["trades"], int)
    oi = apim.parse_futoi(rows_of(load("futoi_latest.json"), "futoi"))
    assert {r["clgroup"] for r in oi} == {"YUR", "FIZ"}
    bad = rows_of(load("algopack_tradestats_latest.json"), "data")
    del bad[0]["pr_close"]
    with pytest.raises(IssFormatError, match="pr_close"):
        apim.parse_tradestats(bad)
    assert not hasattr(apim, "parse_obstats")  # order-book obstats dropped (0423)
    with pytest.raises(ValueError, match="empty"):
        apim.apim_headers("")


# --------------------------------------------------------------------- futures_eod


def _paged(pages: dict[int, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda r: httpx.Response(200, json=pages[int(r.url.params.get("start", "0"))])


def _mock_eod(pages: dict[int, dict[str, Any]]) -> respx.Route:
    respx.get(ISS_BASE_URL + fx.RFUD_SECURITIES).mock(
        return_value=httpx.Response(200, json=load("rfud_securities.json"))
    )
    respx.get(ISS_BASE_URL + fx.SECURITY_DESCRIPTION.format(secid="SiH4")).mock(
        return_value=httpx.Response(200, json=load("security_SiH4.json"))
    )
    return respx.get(ISS_BASE_URL + fx.FORTS_HISTORY).mock(side_effect=_paged(pages))


@respx.mock
def test_futures_eod(settings: Settings, db: Database, conn: Conn) -> None:
    pages = {0: load("forts_history_p0.json"), 4: load("forts_history_p1.json")}
    _mock_eod(pages)
    params = {"date_from": "2026-10-15", "date_to": "2026-10-15", "mode": "final"}
    assert run("futures_eod", params, settings, db).status == "succeeded"
    eod = {r["secid"]: r for r in conn.execute("SELECT * FROM md.futures_eod").fetchall()}
    assert set(eod) == {"CRZ6", "EDZ6", "RBZ6", "SiZ6", "USDRUBF", "SiH4"}
    assert eod["SiZ6"]["settle_rub_per_unit"] == Decimal("85.2190")
    assert eod["USDRUBF"]["settle_rub_per_unit"] == Decimal("83.76")
    assert eod["EDZ6"]["settle_rub_per_unit"] is None  # cross: STEPPRICE varies daily
    assert eod["RBZ6"]["settle_rub_per_unit"] is None  # not FX
    assert eod["SiH4"]["settle_price"] is None
    ref = conn.execute(
        "SELECT spec_source, contract_type, last_trade_date FROM ref.futures_contract "
        "WHERE secid='SiH4'"
    ).fetchone()
    assert ref == {
        "spec_source": "iss_description",
        "contract_type": "dated",
        "last_trade_date": date(2024, 3, 21),
    }
    conn.commit()

    out = run("futures_eod", params, settings, db)  # idempotent
    r = conn.execute(
        "SELECT rows_inserted, rows_updated FROM ops.job_run WHERE id=%s", (out.run_id,)
    ).fetchone()
    assert r == {"rows_inserted": 0, "rows_updated": 0}
    assert count(conn, "SELECT count(*) FROM ops.row_history") == 0

    revised = copy.deepcopy(pages[4])
    cols = revised["history"]["columns"]
    revised["history"]["data"][0][cols.index("SETTLEPRICE")] = 85300.0
    pages[4] = revised
    run("futures_eod", params, settings, db)
    hist = conn.execute(
        "SELECT row_key, old_row FROM ops.row_history WHERE table_name='md.futures_eod'"
    ).fetchall()
    assert len(hist) == 1 and hist[0]["row_key"] == {"trade_date": "2026-10-15", "secid": "SiZ6"}
    assert Decimal(str(hist[0]["old_row"]["settle_price"])) == Decimal("85219.0")


# ---------------------------------------------------------------- futures_intraday

APIM_FORTS = APIM_BASE_URL + fx.FORTS_SECURITIES
ISS_FORTS = ISS_BASE_URL + fx.FORTS_SECURITIES


@respx.mock
def test_futures_intraday_apim_realtime_change_only(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    seed_ref(conn)
    payload = load("forts_md_apim.json")
    route = respx.get(APIM_FORTS).mock(side_effect=lambda _r: httpx.Response(200, json=payload))
    out = run("futures_intraday", {}, apim_settings, db)
    assert out.status == "succeeded"
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(req.url)
    assert "SiZ6" in req.url.params["securities"].split(",")
    rows = conn.execute(
        "SELECT i.*, r.endpoint, r.url_redacted FROM md.futures_intraday i "
        "JOIN ops.raw_capture r ON r.id = i.capture_id"
    ).fetchall()
    assert len(rows) == 6
    assert {(r["source"], r["source_variant"], r["latency"]) for r in rows} == {
        ("moex_apim", "apim_marketdata", "realtime")
    }
    assert {r["endpoint"] for r in rows} == {"intraday:futures_apim_marketdata"}
    ed = next(r for r in rows if r["secid"] == "EDZ6")
    assert ed["last_rub_per_unit"] == ed["last"] * Decimal("8.376") / Decimal("0.0001") / 1000
    conn.commit()

    run("futures_intraday", {}, apim_settings, db)
    assert count(conn, "SELECT count(*) FROM md.futures_intraday") == 6

    cols = payload["marketdata"]["columns"]
    for data in payload["marketdata"]["data"]:
        if data[cols.index("SECID")] == "SiZ6":
            data[cols.index("BID")] = 85030
            data[cols.index("UPDATETIME")] = "10:50:15"
    run("futures_intraday", {}, apim_settings, db)
    assert count(conn, "SELECT count(*) FROM md.futures_intraday WHERE secid='SiZ6'") == 2


@respx.mock
def test_futures_intraday_entitlement_fallback(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    seed_ref(conn)
    respx.get(APIM_FORTS).mock(
        return_value=httpx.Response(
            200,
            text="<html>Информация доступна только подписчикам</html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    respx.get(ISS_FORTS).mock(return_value=httpx.Response(200, json=load("forts_md_iss.json")))
    out = run("futures_intraday", {}, apim_settings, db)
    assert out.status == "partial"
    err = conn.execute("SELECT error FROM ops.job_run WHERE id=%s", (out.run_id,)).fetchone()
    assert err is not None and "EntitlementError" in err["error"]
    rows = conn.execute(
        "SELECT DISTINCT source, source_variant, latency FROM md.futures_intraday"
    ).fetchall()
    assert rows == [{"source": "moex_iss", "source_variant": "iss_public", "latency": "delayed"}]
    endpoints = conn.execute(
        "SELECT DISTINCT endpoint FROM ops.raw_capture WHERE run_id=%s", (out.run_id,)
    ).fetchall()
    assert endpoints == [{"endpoint": "intraday:futures_iss_marketdata"}]


@respx.mock
def test_futures_intraday_without_key_bootstraps_ref(
    settings: Settings, db: Database, conn: Conn
) -> None:
    respx.get(ISS_BASE_URL + fx.RFUD_SECURITIES).mock(
        return_value=httpx.Response(200, json=load("rfud_securities.json"))
    )
    iss = respx.get(ISS_FORTS).mock(
        return_value=httpx.Response(200, json=load("forts_md_iss.json"))
    )
    out = run("futures_intraday", {}, settings, db)
    assert out.status == "partial"
    assert "MOEX_API_KEY not set" in str(
        conn.execute("SELECT error FROM ops.job_run WHERE id=%s", (out.run_id,)).fetchone()
    )
    assert "Authorization" not in iss.calls.last.request.headers
    assert count(conn, "SELECT count(*) FROM ref.futures_contract WHERE is_fx") == 10


# ------------------------------------------------------------- algopack_fx_futures


def test_algopack_requires_key(settings: Settings, db: Database) -> None:
    assert run("algopack_fx_futures", {}, settings, db).status == "skipped"


def _filtered(payload: dict[str, Any], block: str, col: str, value: str) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    cols = out[block]["columns"]
    out[block]["data"] = [r for r in out[block]["data"] if r[cols.index(col)] == value]
    if f"{block}.cursor" in out:
        out[f"{block}.cursor"]["data"] = [[0, len(out[block]["data"]), 1000]]
    return out


def _mock_day_poll(extra_ts: list[list[Any]] | None = None) -> dict[str, Any]:
    """Per-contract / per-ticker ``from=till=today`` endpoints serving the latest fixtures."""
    ts_payload = load("algopack_tradestats_latest.json")
    if extra_ts:
        ts_payload["data"]["data"] += extra_ts
        ts_payload["data.cursor"]["data"] = [[0, len(ts_payload["data"]["data"]), 1000]]
    futoi = load("futoi_latest.json")
    seen: dict[str, Any] = {"params": []}

    def bars(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"].append(dict(request.url.params))
            secid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            return httpx.Response(200, json=_filtered(payload, "data", "secid", secid))

        return handler

    def futoi_handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        cols = futoi["futoi"]["columns"]
        out = copy.deepcopy(futoi)
        out["futoi"]["data"] = [
            r for r in futoi["futoi"]["data"] if r[cols.index("ticker")].lower() == ticker
        ]
        return httpx.Response(200, json=out)

    respx.get(url__regex=r".*/datashop/algopack/fo/tradestats/\w+\.json").mock(
        side_effect=bars(ts_payload)
    )
    respx.get(url__regex=r".*/analyticalproducts/futoi/securities/\w+\.json").mock(
        side_effect=futoi_handler
    )
    return seen


@respx.mock
def test_algopack_poll_reads_whole_day_fx_only_and_dedup(
    apim_settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    """F7/N4: a re-read poll reads today per FX contract (from=till=today), so a bar published
    late or missed by an earlier poll is filled; re-polled bars are no-ops. Re-read captures go
    to the ``intraday:reread:`` prefix (short retention)."""
    today_is(date(2026, 10, 16))
    seed_ref(conn)
    seen = _mock_day_poll()
    out = run("algopack_fx_futures", {"full": True}, apim_settings, db)
    assert out.status == "succeeded" and out.result["mode"] == "reread"
    assert {(p["from"], p["till"]) for p in seen["params"]} == {("2026-10-16", "2026-10-16")}
    ts = {r["secid"]: r for r in conn.execute("SELECT * FROM md.algopack_fo_tradestats")}
    assert "ANZ6" not in ts and "RBZ6" not in ts and "CRZ6" in ts
    assert ts["CRZ6"]["pr_close_rub_per_unit"] == ts["CRZ6"]["pr_close"]
    assert ts["EDZ6"]["pr_close_rub_per_unit"] is not None  # cross, same-session STEPPRICE
    tickers = {r["ticker"] for r in conn.execute("SELECT ticker FROM md.algopack_futoi")}
    assert tickers == {"Si", "USDRUBF", "CR"}  # GL (gold) excluded
    endpoints = {
        r["endpoint"] for r in conn.execute("SELECT DISTINCT endpoint FROM ops.raw_capture")
    }
    assert endpoints == {"intraday:reread:algopack_fo_tradestats", "intraday:reread:futoi"}
    n_ts = count(conn, "SELECT count(*) FROM md.algopack_fo_tradestats")
    respx.clear()
    _mock_day_poll()
    run("algopack_fx_futures", {"full": True}, apim_settings, db)  # same day polled again
    assert count(conn, "SELECT count(*) FROM md.algopack_fo_tradestats") == n_ts
    assert count(conn, "SELECT count(*) FROM ops.row_history") == 0

    # a bar of an earlier interval shows up late (or its poll was missed): filled now
    payload = load("algopack_tradestats_latest.json")
    cols = payload["data"]["columns"]
    late = next(r for r in payload["data"]["data"] if r[cols.index("secid")] == "CRZ6")
    late = list(late)
    late[cols.index("tradetime")] = "10:10:00"
    respx.clear()
    _mock_day_poll(extra_ts=[late])
    out = run("algopack_fx_futures", {"full": True}, apim_settings, db)
    assert count(conn, "SELECT count(*) FROM md.algopack_fo_tradestats") == n_ts + 1
    # the interval check sees the holes 10:15 and 10:20 between 10:10 and 10:25
    assert out.status == "partial"
    assert out.result["missing_intervals"] == [
        "2026-10-16T10:15:00+03:00",
        "2026-10-16T10:20:00+03:00",
    ]


def test_interval_holes_ignore_clearing_and_fresh_intervals(conn: Conn) -> None:
    from datetime import datetime, timedelta

    from mdhub.core.timeutil import MSK
    from mdhub.loaders.futures import interval_holes

    seed_ref(conn)
    day = date(2026, 10, 16)

    def bar(hh: int, mm: int) -> None:
        conn.execute(
            "INSERT INTO md.algopack_fo_tradestats (secid, interval_ts, trade_date) "
            "VALUES ('SiZ6', %s, %s)",
            (datetime(2026, 10, 16, hh, mm, tzinfo=MSK), day),
        )

    for hh, mm in [(13, 50), (13, 55), (14, 10), (14, 15), (14, 25), (14, 30)]:
        bar(hh, mm)
    now = datetime(2026, 10, 16, 14, 35, tzinfo=MSK)
    assert interval_holes(conn, day, now=now) == []  # 14:20 is younger than 15 minutes
    later = now + timedelta(hours=1)
    assert interval_holes(conn, day, now=later) == [datetime(2026, 10, 16, 14, 20, tzinfo=MSK)]


@respx.mock
def test_algopack_bootstrap_uses_latest_snapshot_with_futoi_guard(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    respx.get(APIM_BASE_URL + apim.ALGOPACK_FO_TRADESTATS).mock(
        return_value=httpx.Response(200, json=load("algopack_tradestats_latest.json"))
    )
    futoi = load("futoi_latest.json")
    full = copy.deepcopy(futoi)
    full["futoi"]["data"] = futoi["futoi"]["data"] * 125  # 1000 rows: may be truncated
    route = respx.get(APIM_BASE_URL + apim.FUTOI_ALL).mock(
        return_value=httpx.Response(200, json=futoi)
    )
    out = run("algopack_fx_futures", {}, apim_settings, db)  # no FX contract known yet
    assert out.status == "succeeded" and out.result["mode"] == "latest_snapshot"
    assert route.calls.last.request.url.params["latest"] == "1"
    assert count(conn, "SELECT count(*) FROM md.algopack_futoi") > 0
    route.mock(return_value=httpx.Response(200, json=full))
    with pytest.raises(Exception, match="truncated"):
        run("algopack_fx_futures", {}, apim_settings, db)


def _empty_bars(kind_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {"columns": kind_payload["data"]["columns"], "data": []},
        "data.cursor": {"columns": CURSOR, "data": [[0, 0, 1000]]},
    }


@respx.mock
def test_algopack_backfill_per_contract(
    apim_settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    today_is(date(2026, 10, 16))  # inside the 90-day rolling window
    seed_ref(conn)
    p0, p1 = load("algopack_tradestats_SiZ6_p0.json"), load("algopack_tradestats_SiZ6_p1.json")
    futoi_day = load("futoi_si_day.json")
    futoi_empty = {"futoi": {"columns": futoi_day["futoi"]["columns"], "data": []}}

    def tradestats(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/SiZ6.json"):
            return httpx.Response(200, json=p0 if request.url.params["start"] == "0" else p1)
        return httpx.Response(200, json=_empty_bars(p0))

    def futoi(request: httpx.Request) -> httpx.Response:
        assert request.url.params["from"] == request.url.params["till"]
        hit = request.url.path.endswith("/si.json") and request.url.params["from"] == "2026-10-15"
        return httpx.Response(200, json=futoi_day if hit else futoi_empty)

    ts_route = respx.get(url__regex=r".*/datashop/algopack/fo/tradestats/\w+\.json").mock(
        side_effect=tradestats
    )
    respx.get(url__regex=r".*/analyticalproducts/futoi/securities/\w+\.json").mock(
        side_effect=futoi
    )
    params = {"date_from": "2026-10-01", "date_to": "2026-10-15"}
    out = run("algopack_fx_futures", params, apim_settings, db)
    assert out.status == "succeeded"
    si_calls = [c for c in ts_route.calls if c.request.url.path.endswith("/SiZ6.json")]
    assert [c.request.url.params["start"] for c in si_calls] == ["0", "3"]
    assert si_calls[0].request.url.params["from"] == "2026-10-01"
    assert count(conn, "SELECT count(*) FROM md.algopack_fo_tradestats WHERE secid='SiZ6'") == 5
    assert count(conn, "SELECT count(*) FROM md.algopack_futoi") == 6
    endpoints = {
        r["endpoint"]
        for r in conn.execute("SELECT DISTINCT endpoint FROM ops.raw_capture")
        if re.match("intraday:", r["endpoint"])
    }
    assert endpoints == set()  # backfill captures: *_history (rolling-window retention)


@respx.mock
def test_algopack_futoi_full_page_fails_closed(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    seed_ref(conn)
    day = load("futoi_si_day.json")
    full = copy.deepcopy(day)
    full["futoi"]["data"] = day["futoi"]["data"] * 167  # 1002 rows: page may be truncated
    p0 = load("algopack_tradestats_SiZ6_p0.json")
    respx.get(url__regex=r".*/datashop/algopack/fo/\w+/\w+\.json").mock(
        return_value=httpx.Response(200, json=_empty_bars(p0))
    )
    respx.get(url__regex=r".*/analyticalproducts/futoi/securities/\w+\.json").mock(
        return_value=httpx.Response(200, json=full)
    )
    with pytest.raises(Exception, match="truncated"):
        run(
            "algopack_fx_futures",
            {"date_from": "2026-10-15", "date_to": "2026-10-15"},
            apim_settings,
            db,
        )
