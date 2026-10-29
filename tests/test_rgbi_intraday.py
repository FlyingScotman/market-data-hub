"""RGBI futures monitor inputs: parsers (recorded ISS fixtures), jobs against PostgreSQL, the
intraday read API (/v1/intraday/*, /v1/index/constituents, /v1/repo/*, /v1/history/bond_close)
and the latency metric. No network: HTTP is mocked with respx."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

import mdhub.loaders.bond_close
import mdhub.loaders.futures
import mdhub.loaders.index_intraday
import mdhub.loaders.repo
import mdhub.loaders.zcyc_securities  # noqa: F401
from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import APIM_BASE_URL, ISS_BASE_URL, IssFormatError
from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, JobParamError, run_job
from mdhub.loaders.futures import refresh_futures_contracts
from mdhub.sources import moex_fx as fx
from mdhub.sources import moex_intraday as mi

FIX = Path(__file__).parent / "fixtures" / "moex_intraday"
FX_FIX = Path(__file__).parent / "fixtures" / "moex_fx"
Conn = psycopg.Connection[DictRow]
KEY = "test-apim-key"
TODAY = date(2026, 10, 29)
TOKENS = "tok-intra-1"
AUTH = {"Authorization": f"Bearer {TOKENS}"}


def load(name: str, base: Path = FIX) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((base / name).read_text("utf-8"))
    return data


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def run(name: str, params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job(name, params, settings=settings, db=db, http_factory=http_factory)


def count(conn: Conn, query: str) -> int:
    conn.commit()
    row = conn.execute(query).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return int(next(iter(row.values())))


def run_error(conn: Conn, run_id: int | None) -> str:
    row = conn.execute("SELECT error FROM ops.job_run WHERE id=%s", (run_id,)).fetchone()
    return str(row["error"]) if row else ""


@pytest.fixture(autouse=True)
def _recorded_today(today_is: Callable[[date], None]) -> None:
    today_is(TODAY)


@pytest.fixture
def iss_settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, iss_rps=1000.0)  # type: ignore[arg-type]


HTML_200 = httpx.Response(
    200,
    text="<html>Информация доступна только подписчикам</html>",
    headers={"content-type": "text/html; charset=utf-8"},
)


# ----------------------------------------------------------------------------- parsers


def test_is_ofz() -> None:
    assert mi.is_ofz("SU26238RMFS4") and mi.is_ofz("SU52005RMFS4")
    assert not mi.is_ofz("RU000A0JW4Z1") and not mi.is_ofz("AFKS") and not mi.is_ofz("su26238rmfs4")


def test_parse_zcyc_securities_realtime_and_units() -> None:
    fetched = datetime(2026, 10, 29, 15, 53, 2, tzinfo=MSK)
    rows = mi.parse_zcyc_securities(load("zcyc_securities.json"), fetched)
    assert len(rows) == 29
    by = {r["secid"]: r for r in rows}
    r = by["SU26207RMFS9"]
    assert r["ts"] == datetime(2026, 10, 29, 15, 52, 59, tzinfo=MSK)
    assert r["calc_ts"] == r["source_ts"] == datetime(2026, 10, 29, 15, 52, 58, tzinfo=MSK)
    assert (r["bid_price_pct"], r["ask_price_pct"]) == (Decimal("98.6"), Decimal("98.649"))
    assert (r["bid_yield_pct"], r["ask_yield_pct"]) == (Decimal("14.08"), Decimal("13.87"))
    assert r["benchmark"] is False and r["exp_date"] == date(2027, 2, 3)
    assert {x["latency"] for x in rows} == {"realtime"}
    late = mi.parse_zcyc_securities(load("zcyc_securities.json"), fetched + timedelta(minutes=15))
    assert {x["latency"] for x in late} == {"delayed"}


def test_parse_zcyc_securities_drift_fails_closed() -> None:
    payload = load("zcyc_securities.json")
    payload["securities"]["columns"][payload["securities"]["columns"].index("bidprice")] = "bid"
    with pytest.raises(IssFormatError):
        mi.parse_zcyc_securities(payload, datetime.now(MSK))


def test_parse_index_intraday_realtime() -> None:
    rows = mi.parse_index_intraday(load("index_marketdata.json"), ["RGBI", "RGBITR"])
    by = {r["secid"]: r for r in rows}
    assert set(by) == {"RGBI", "RGBITR"}
    assert by["RGBI"]["current_value"] == Decimal("112.36")
    assert by["RGBI"]["last_value"] == Decimal("112.51")
    assert by["RGBI"]["trade_date"] == TODAY
    assert {r["latency"] for r in rows} == {"realtime"}
    with pytest.raises(IssFormatError, match="lacks"):
        mi.parse_index_intraday(load("index_marketdata.json"), ["RGBI", "RUGBITR5Y"])


def test_parse_index_candles() -> None:
    rows = mi.parse_index_candles(
        _block_rows(load("index_candles_RGBI_p0.json"), "candles"), "RGBI", 1
    )
    assert len(rows) == 5
    first = rows[0]
    assert first["begin_ts"] == datetime(2026, 10, 28, 7, 0, tzinfo=MSK)
    assert first["end_ts"] == datetime(2026, 10, 28, 7, 0, 59, tzinfo=MSK)
    assert first["close"] == Decimal("112.47") and first["trade_date"] == date(2026, 10, 28)


def _block_rows(payload: dict[str, Any], block: str) -> list[dict[str, Any]]:
    cols = payload[block]["columns"]
    return [dict(zip(cols, r, strict=True)) for r in payload[block]["data"]]


def test_parse_and_validate_constituents() -> None:
    rows = []
    for name in ("index_analytics_RGBI_p0.json", "index_analytics_RGBI_p1.json"):
        rows += mi.parse_index_constituents(
            _block_rows(load(name), "analytics"), "RGBI", date(2026, 10, 28)
        )
    assert len(rows) == 29
    mi.validate_constituents(rows, 29, "RGBI")
    assert sum(r["weight_pct"] for r in rows) == Decimal("100.00")
    with pytest.raises(IssFormatError, match="TOTAL"):
        mi.validate_constituents(rows[:20], 29, "RGBI")
    broken = copy.deepcopy(rows)
    broken[0]["weight_pct"] += 5
    with pytest.raises(IssFormatError, match="sum"):
        mi.validate_constituents(broken, 29, "RGBI")
    with pytest.raises(IssFormatError, match="row of"):
        mi.parse_index_constituents(
            _block_rows(load("index_analytics_RGBI_p0.json"), "analytics"),
            "RGBI",
            date(2026, 10, 27),
        )


def test_parse_repo_marketdata_ccp_delayed_with_settle_dates() -> None:
    rows = mi.parse_repo_marketdata(
        load("repo_ccp_EQRP_marketdata.json"), "ccp", "EQRP", keep=mi.is_ofz
    )
    by = {r["secid"]: r for r in rows}
    assert set(by) == {"SU26212RMFS9", "SU26238RMFS4"}
    r = by["SU26238RMFS4"]
    assert r["ts"] == datetime(2026, 10, 29, 14, 4, 45, tzinfo=MSK)
    assert r["waprice_pct"] == Decimal("14.03") and r["last_pct"] == Decimal("14.15")
    assert (r["settle_date"], r["settle_date2"]) == (date(2026, 10, 29), date(2026, 10, 30))
    assert {x["latency"] for x in rows} == {"delayed"}


def test_parse_repo_marketdata_gcc_skips_rows_without_rates() -> None:
    rows = mi.parse_repo_marketdata(load("repo_gcc_GCRP_marketdata.json"), "gcc", "GCRP")
    secids = {r["secid"] for r in rows}
    assert mi.GC_BOND_BASKET in secids
    assert all(r["settle_date2"] is None for r in rows)


def test_parse_repo_history_filters_ofz() -> None:
    rows = mi.parse_repo_history(
        _block_rows(load("repo_ccp_EQRP_history.json"), "history"),
        "ccp",
        "EQRP",
        date(2026, 10, 28),
        keep=mi.is_ofz,
    )
    assert [r["secid"] for r in rows] == ["SU26207RMFS9", "SU26212RMFS9", "SU26218RMFS6"]
    assert rows[0]["waprice_pct"] == Decimal("13.59")
    with pytest.raises(IssFormatError):
        mi.parse_repo_history(
            _block_rows(load("repo_ccp_EQRP_history.json"), "history"),
            "ccp",
            "EQWP",
            date(2026, 10, 28),
        )


def test_parse_forts_trades() -> None:
    rows = mi.parse_forts_trades(load("forts_trades_RGBIF.json"), "RGBIF")
    assert len(rows) == 56
    t = rows[0]
    assert t["trade_no"] == 2050015240732542801
    assert t["trade_ts"] == datetime(2026, 10, 29, 15, 39, 57, tzinfo=MSK)
    assert t["price"] == Decimal("112.08") and t["quantity"] == Decimal("20")
    assert {r["buysell"] for r in rows} <= {"B", "S"}
    assert all(r["open_position"] is not None for r in rows)
    with pytest.raises(IssFormatError, match="row of"):
        mi.parse_forts_trades(load("forts_trades_RGBIF.json"), "RBZ6")


# -------------------------------------------------------------------- futures_intraday

APIM_FORTS = APIM_BASE_URL + fx.FORTS_SECURITIES
ISS_FORTS = ISS_BASE_URL + fx.FORTS_SECURITIES
TRADES_RE = re.compile(
    re.escape(ISS_BASE_URL)
    + r"/engines/futures/markets/forts/securities/([A-Za-z0-9]+)/trades\.json"
)
RATES = ("KKZ6", "RBZ6", "RFZ6", "RGBIF")


def seed_contracts(conn: Conn) -> None:
    rates = fx.parse_futures_securities(load("forts_md_rates_iss.json"))
    fresh = {r["secid"] for r in rates}
    rows = [
        r
        for r in fx.parse_futures_securities(load("rfud_securities.json", FX_FIX))
        if r["secid"] not in fresh
    ] + rates
    refresh_futures_contracts(
        conn,
        rows,
        capture_id=None,  # type: ignore[arg-type]
        run_id=None,  # type: ignore[arg-type]
        today=TODAY,
    )
    conn.execute("UPDATE ref.futures_contract SET last_seen = %s", (TODAY,))
    conn.commit()


def _trades(request: httpx.Request) -> httpx.Response:
    m = re.search(r"/securities/([A-Za-z0-9]+)/trades\.json$", request.url.path)
    assert m is not None
    if m.group(1) == "RGBIF":
        return httpx.Response(200, json=load("forts_trades_RGBIF.json"))
    empty = load("forts_trades_RGBIF.json")
    empty["trades"]["data"] = []
    return httpx.Response(200, json=empty)


@respx.mock
def test_futures_intraday_rates_contracts_fall_back_to_iss_with_trades(
    db_url: str, db: Database, conn: Conn
) -> None:
    seed_contracts(conn)
    settings = Settings(database_url=db_url, iss_rps=1000.0, moex_api_key=KEY)  # type: ignore[arg-type]
    apim = respx.get(APIM_FORTS).mock(
        return_value=httpx.Response(200, json=load("forts_md_apim.json", FX_FIX))
    )
    iss = respx.get(ISS_FORTS).mock(
        return_value=httpx.Response(200, json=load("forts_md_rates_iss.json"))
    )
    trades = respx.get(TRADES_RE).mock(side_effect=_trades)
    out = run("futures_intraday", {}, settings, db)
    assert out.status == "partial"
    assert "APIM answer lacks" in run_error(conn, out.run_id)
    requested = set(apim.calls.last.request.url.params["securities"].split(","))
    assert set(RATES) <= requested and "SiZ6" in requested
    # only the rates contracts APIM did not serve go to public ISS
    assert set(iss.calls.last.request.url.params["securities"].split(",")) == set(RATES)
    rows = conn.execute(
        "SELECT secid, source, source_variant, latency, bid, offer, last, last_rub_per_unit "
        "FROM md.futures_intraday WHERE secid = ANY(%s)",
        (list(RATES),),
    ).fetchall()
    by = {r["secid"]: r for r in rows}
    assert set(by) == set(RATES)
    assert {(r["source"], r["source_variant"], r["latency"]) for r in rows} == {
        ("moex_iss", "iss_public", "delayed")
    }
    assert by["RBZ6"]["bid"] == Decimal("11258") and by["RBZ6"]["last_rub_per_unit"] is None
    assert by["KKZ6"]["last"] is None  # LAST 0 without trades -> NULL
    fx_rows = conn.execute(
        "SELECT DISTINCT source, latency FROM md.futures_intraday WHERE secid = 'SiZ6'"
    ).fetchall()
    assert fx_rows == [{"source": "moex_apim", "latency": "realtime"}]
    # trades: ISS (APIM did not serve the rates contracts), delayed
    assert trades.call_count == len(RATES)
    assert count(conn, "SELECT count(*) FROM md.futures_trade") == 56
    assert (
        count(
            conn,
            "SELECT count(*) FROM md.futures_trade WHERE source='moex_iss' AND latency='delayed'",
        )
        == 56
    )
    endpoints = {
        r["endpoint"]
        for r in conn.execute(
            "SELECT DISTINCT endpoint FROM ops.raw_capture WHERE run_id=%s", (out.run_id,)
        ).fetchall()
    }
    assert endpoints == {
        "intraday:futures_apim_marketdata",
        "intraday:futures_iss_marketdata",
        "intraday:futures_iss_trades",
    }

    # second run: incremental from the last stored TRADENO, nothing new
    run("futures_intraday", {}, settings, db)
    rgbif_calls = [c for c in trades.calls if "/RGBIF/" in str(c.request.url)]
    params = rgbif_calls[-1].request.url.params
    assert params["tradeno"] == "2050015240732542856" and params["next_trade"] == "1"
    assert count(conn, "SELECT count(*) FROM md.futures_trade") == 56


def _merged_apim_answer() -> dict[str, Any]:
    """The recorded FX APIM answer plus the (15-min old) rates rows of the ISS answer."""
    out = load("forts_md_apim.json", FX_FIX)
    rates = load("forts_md_rates_iss.json")
    for block in ("securities", "marketdata"):
        cols = out[block]["columns"]
        for row in _block_rows(rates, block):
            out[block]["data"].append([row.get(c) for c in cols])
    return out


@respx.mock
def test_futures_intraday_apim_rates_rows_get_their_own_latency(
    db_url: str, db: Database, conn: Conn
) -> None:
    seed_contracts(conn)
    settings = Settings(database_url=db_url, iss_rps=1000.0, moex_api_key=KEY)  # type: ignore[arg-type]
    respx.get(APIM_FORTS).mock(return_value=httpx.Response(200, json=_merged_apim_answer()))
    apim_trades = respx.get(
        re.compile(
            re.escape(APIM_BASE_URL) + r"/engines/futures/markets/forts/securities/\w+/trades"
        )
    ).mock(side_effect=_trades)
    out = run("futures_intraday", {}, settings, db)
    assert out.status == "succeeded"
    rows = conn.execute(
        "SELECT DISTINCT secid IN ('KKZ6', 'RBZ6', 'RFZ6', 'RGBIF') AS rates, source, latency "
        "FROM md.futures_intraday ORDER BY 1"
    ).fetchall()
    assert [(r["rates"], r["source"], r["latency"]) for r in rows] == [
        (False, "moex_apim", "realtime"),
        (True, "moex_apim", "delayed"),
    ]
    # APIM served the rates marketdata: trades through APIM, labelled realtime
    assert apim_trades.call_count == len(RATES)
    assert (
        count(
            conn,
            "SELECT count(*) FROM md.futures_trade WHERE source='moex_apim' AND latency='realtime'",
        )
        == 56
    )


@respx.mock
def test_futures_intraday_trades_read_failure_is_partial_not_fatal(
    iss_settings: Settings, db: Database, conn: Conn
) -> None:
    seed_contracts(conn)
    rates = load("forts_md_rates_iss.json")
    respx.get(ISS_FORTS).mock(return_value=httpx.Response(200, json=rates))
    respx.get(TRADES_RE).mock(return_value=HTML_200)
    out = run("futures_intraday", {}, iss_settings, db)
    assert out.status == "partial"
    err = run_error(conn, out.run_id)
    assert "trades not read" in err and "EntitlementError" in err
    assert count(conn, "SELECT count(*) FROM md.futures_intraday WHERE secid='RGBIF'") == 1


# ---------------------------------------------------------------------- zcyc_securities

ZCYC_URL = ISS_BASE_URL + mi.ZCYC_SECURITIES


@respx.mock
def test_zcyc_securities_change_only(iss_settings: Settings, db: Database, conn: Conn) -> None:
    payload = load("zcyc_securities.json")
    respx.get(ZCYC_URL).mock(side_effect=lambda _r: httpx.Response(200, json=payload))
    out = run("zcyc_securities", {"polls": 1}, iss_settings, db)
    assert out.status == "succeeded"
    assert count(conn, "SELECT count(*) FROM md.zcyc_security_quote") == 29
    run("zcyc_securities", {"polls": 1}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.zcyc_security_quote") == 29
    cols = payload["securities"]["columns"]
    row = payload["securities"]["data"][0]
    row[cols.index("bidprice")] = 98.61
    row[cols.index("updatetime")] = "15:53:09"
    run("zcyc_securities", {"polls": 1}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.zcyc_security_quote") == 30
    cap = conn.execute(
        "SELECT DISTINCT endpoint FROM ops.raw_capture WHERE source='moex_iss'"
    ).fetchall()
    assert cap == [{"endpoint": "intraday:zcyc_securities"}]


@respx.mock
def test_zcyc_securities_html_answer_is_an_error(
    iss_settings: Settings, db: Database, conn: Conn
) -> None:
    respx.get(ZCYC_URL).mock(return_value=HTML_200)
    with pytest.raises(IssFormatError, match="EntitlementError"):
        run("zcyc_securities", {"polls": 1}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.zcyc_security_quote") == 0


def test_zcyc_securities_rejects_polls_beyond_a_minute(
    iss_settings: Settings, db: Database
) -> None:
    with pytest.raises(JobParamError):
        run("zcyc_securities", {"polls": 7, "interval_s": 10.0}, iss_settings, db)


# ----------------------------------------------------------------------- index_intraday

INDEX_MD_URL = ISS_BASE_URL + mi.INDEX_MARKETDATA
CANDLES_URL = re.compile(
    re.escape(ISS_BASE_URL) + r"/engines/stock/markets/index/securities/(RGBI|RGBITR)/candles\.json"
)


def _candles(request: httpx.Request) -> httpx.Response:
    if request.url.params.get("start", "0") != "0":
        return httpx.Response(200, json=load("index_candles_empty.json"))
    return httpx.Response(200, json=load("index_candles_RGBI_p0.json"))


@respx.mock
def test_index_intraday_values_and_candles(
    iss_settings: Settings, db: Database, conn: Conn
) -> None:
    respx.get(INDEX_MD_URL).mock(
        return_value=httpx.Response(200, json=load("index_marketdata.json"))
    )
    respx.get(CANDLES_URL).mock(side_effect=_candles)
    out = run("index_intraday", {}, iss_settings, db)
    assert out.status == "succeeded"
    assert count(conn, "SELECT count(*) FROM md.index_intraday") == 2
    run("index_intraday", {}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.index_intraday") == 2


@respx.mock
def test_index_intraday_candle_backfill(iss_settings: Settings, db: Database, conn: Conn) -> None:
    route = respx.get(CANDLES_URL).mock(side_effect=_candles)
    settings = iss_settings.model_copy(update={"intraday_indices": "RGBI"})
    out = run(
        "index_intraday",
        {"date_from": date(2026, 10, 28), "date_to": date(2026, 10, 28)},
        settings,
        db,
    )
    assert out.status == "succeeded"
    first = route.calls[0].request.url.params
    assert (first["from"], first["till"], first["interval"]) == ("2026-10-28", "2026-10-28", "1")
    assert count(conn, "SELECT count(*) FROM md.index_candle WHERE trade_date='2026-10-28'") == 5


# ------------------------------------------------------------------- index_constituents

ANALYTICS_URL = ISS_BASE_URL + mi.INDEX_ANALYTICS.format(index="RGBI")


def _analytics(request: httpx.Request) -> httpx.Response:
    page = "p1" if request.url.params.get("start") == "20" else "p0"
    return httpx.Response(200, json=load(f"index_analytics_RGBI_{page}.json"))


@respx.mock
def test_index_constituents_day(iss_settings: Settings, db: Database, conn: Conn) -> None:
    respx.get(ANALYTICS_URL).mock(side_effect=_analytics)
    out = run("index_constituents", {"days_back": [1]}, iss_settings, db)
    assert out.status == "succeeded"
    rows = conn.execute(
        "SELECT count(*) AS n, sum(weight_pct) AS w FROM md.index_constituent "
        "WHERE index_id='RGBI' AND trade_date='2026-10-28'"
    ).fetchone()
    assert rows is not None and rows["n"] == 29 and rows["w"] == Decimal("100.00")


@respx.mock
def test_index_constituents_total_mismatch_fails_closed(
    iss_settings: Settings, db: Database, conn: Conn
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        body = json.loads(_analytics(request).content)
        if request.url.params.get("start") == "20":
            body["analytics"]["data"] = body["analytics"]["data"][:-1]
        return httpx.Response(200, json=body)

    respx.get(ANALYTICS_URL).mock(side_effect=broken)
    with pytest.raises(IssFormatError, match="TOTAL"):
        run("index_constituents", {"days_back": [1]}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.index_constituent") == 0


# --------------------------------------------------------------------------------- repo

REPO_MD = re.compile(
    re.escape(ISS_BASE_URL) + r"/engines/stock/markets/(ccp|gcc)/boards/([A-Z]+)/securities\.json"
)
REPO_HIST = re.compile(
    re.escape(ISS_BASE_URL)
    + r"/history/engines/stock/markets/(ccp|gcc)/boards/([A-Z]+)/securities\.json"
)


def _repo_md(request: httpx.Request) -> httpx.Response:
    m = REPO_MD.match(str(request.url).split("?")[0])
    assert m is not None
    market, board = m.groups()
    name = "repo_ccp_EQRP_marketdata.json" if market == "ccp" else "repo_gcc_GCRP_marketdata.json"
    body = load(name)
    for block in ("securities", "marketdata"):
        cols = body[block]["columns"]
        for row in body[block]["data"]:
            row[cols.index("BOARDID")] = board
    return httpx.Response(200, json=body)


def _repo_hist(request: httpx.Request) -> httpx.Response:
    m = REPO_HIST.match(str(request.url).split("?")[0])
    assert m is not None
    market, board = m.groups()
    name = "repo_ccp_EQRP_history.json" if market == "ccp" else "repo_gcc_GCRP_history.json"
    body = load(name)
    cols = body["history"]["columns"]
    for row in body["history"]["data"]:
        row[cols.index("BOARDID")] = board
    return httpx.Response(200, json=body)


@respx.mock
def test_repo_ccp_intraday_uses_active_ofz_and_is_change_only(
    iss_settings: Settings, db: Database, conn: Conn
) -> None:
    route = respx.get(REPO_MD).mock(side_effect=_repo_md)
    out = run("repo_ccp_bond", {"mode": "intraday"}, iss_settings, db)
    assert out.status == "succeeded"
    assert route.call_count == len(mi.CCP_BOARDS)
    params = route.calls.last.request.url.params
    assert "securities" not in params  # ref.bond empty: the whole board, filtered here
    assert "SETTLEDATE2" in params["securities.columns"]
    assert count(conn, "SELECT count(*) FROM md.repo_intraday WHERE market='ccp'") == 8
    run("repo_ccp_bond", {"mode": "intraday"}, iss_settings, db)
    assert count(conn, "SELECT count(*) FROM md.repo_intraday") == 8


@respx.mock
def test_repo_eod_and_gc(iss_settings: Settings, db: Database, conn: Conn) -> None:
    respx.get(REPO_HIST).mock(side_effect=_repo_hist)
    respx.get(REPO_MD).mock(side_effect=_repo_md)
    out = run("repo_ccp_bond", {"mode": "final", "days_back": [1]}, iss_settings, db)
    assert out.status == "succeeded"
    assert count(
        conn, "SELECT count(*) FROM md.repo_eod WHERE market='ccp' AND trade_date='2026-10-28'"
    ) == 3 * len(mi.CCP_BOARDS)  # OFZ only
    assert run("repo_gc_term", {"mode": "final", "days_back": [1]}, iss_settings, db).status == (
        "succeeded"
    )
    assert count(conn, "SELECT count(*) FROM md.repo_eod WHERE market='gcc'") == 3 * len(
        mi.GCC_BOARDS
    )
    assert run("repo_gc_term", {"mode": "intraday"}, iss_settings, db).status == "succeeded"
    assert count(
        conn, f"SELECT count(*) FROM md.repo_intraday WHERE secid='{mi.GC_BOND_BASKET}'"
    ) == len(mi.GCC_BOARDS)


# --------------------------------------------------------------------- bond_close_quote


def _seed_quotes(conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO md.bond_quote_intraday (secid, board, ts, source_ts, latency, bid, offer)
        VALUES
          ('SU26238RMFS4', 'TQOB', '2026-10-29 18:30+03', '2026-10-29 18:45+03', 'delayed',
           52.1, 52.3),
          ('SU26238RMFS4', 'TQOB', '2026-10-29 18:44+03', '2026-10-29 18:59+03', 'delayed',
           52.2, 52.4),
          ('SU26238RMFS4', 'TQOB', '2026-10-29 18:49+03', '2026-10-29 19:04+03', 'delayed',
           52.0, 52.5),
          ('SU26212RMFS9', 'TQOB', '2026-10-28 18:40+03', '2026-10-28 18:55+03', 'delayed',
           92.0, 92.1);
        INSERT INTO md.zcyc_security_quote (secid, ts, trade_date, source_ts, latency,
            bid_price_pct, ask_price_pct, bid_yield_pct, ask_yield_pct)
        VALUES ('SU26238RMFS4', '2026-10-29 18:45+03', '2026-10-29', '2026-10-29 18:45+03',
                'realtime', 52.15, 52.35, 14.9, 14.8);
        """
    )
    conn.commit()


def test_bond_close_quote_latest_at_or_before_cut(
    settings: Settings, db: Database, conn: Conn
) -> None:
    _seed_quotes(conn)
    out = run("bond_close_quote", {"days_back": [0]}, settings, db)
    assert out.status == "succeeded"
    rows = {
        (r["secid"], r["basis"]): r
        for r in conn.execute("SELECT * FROM md.bond_close_quote").fetchall()
    }
    # SU26212RMFS9 has no quote on the day: no row (no forward fill from 10-28)
    assert set(rows) == {
        ("SU26238RMFS4", "bonds_marketdata"),
        ("SU26238RMFS4", "zcyc_securities"),
    }
    m = rows[("SU26238RMFS4", "bonds_marketdata")]
    assert (m["close_bid_pct"], m["close_ask_pct"]) == (Decimal("52.2"), Decimal("52.4"))
    assert m["quote_ts"] == datetime(2026, 10, 29, 18, 44, tzinfo=MSK)
    assert m["cut_ts"] == datetime(2026, 10, 29, 18, 45, tzinfo=MSK)
    z = rows[("SU26238RMFS4", "zcyc_securities")]
    assert z["board"] == "ZCYC" and z["bid_yield_pct"] == Decimal("14.9")


# -------------------------------------------------------------------------------- API


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens=TOKENS)
    )
    with TestClient(app) as c:
        yield c


def _get(client: TestClient, path: str, **params: Any) -> Any:
    r = client.get(path, params=params, headers=AUTH)
    assert r.status_code == 200, r.text
    return json.loads(r.content, parse_float=Decimal)


def _bad(client: TestClient, path: str, **params: Any) -> None:
    r = client.get(path, params=params, headers=AUTH)
    assert r.status_code == 400, r.text


def _records(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(body["columns"], r, strict=True)) for r in body["rows"]]


def test_intraday_bonds_paging_filters_and_known_at(client: TestClient, conn: Conn) -> None:
    _seed_quotes(conn)
    body = _get(client, "/v1/intraday/bonds", **{"from": "2026-10-29", "to": "2026-10-29"})
    recs = _records(body)
    assert [r["ts"] for r in recs] == sorted(r["ts"] for r in recs) and len(recs) == 3
    assert body["meta"]["order"] == ["ts", "secid", "board"]
    assert {"source_ts", "ingested_at", "latency"} <= set(body["columns"])
    # instant range, inclusive end
    body = _get(
        client,
        "/v1/intraday/bonds",
        **{"from": "2026-10-29T18:30:00+03:00", "to": "2026-10-29T18:44:00+03:00"},
    )
    assert len(body["rows"]) == 2
    # paging: limit 1 walks the 3 rows without overlap
    seen: list[Any] = []
    params: dict[str, Any] = {"from": "2026-10-28", "to": "2026-10-29", "limit": 1}
    while True:
        page = _get(client, "/v1/intraday/bonds", **params)
        seen += page["rows"]
        if page["next"] is None:
            break
        params["cursor"] = page["next"]
    assert len(seen) == 4 and len({json.dumps(r, default=str) for r in seen}) == 4
    # filters
    body = _get(client, "/v1/intraday/bonds", **{"from": "2026-10-28", "secid": "SU26212RMFS9"})
    assert {r["secid"] for r in _records(body)} == {"SU26212RMFS9"}
    # known_at before the rows were written: nothing; and a revision is undone
    conn.execute("UPDATE md.bond_quote_intraday SET ingested_at = '2026-10-29 10:00Z'")
    conn.commit()
    before = _get(
        client,
        "/v1/intraday/bonds",
        **{"from": "2026-10-29", "to": "2026-10-29", "known_at": "2026-10-29T09:00:00Z"},
    )
    assert _get(client, "/v1/intraday/bonds", **{"from": "2026-10-29", "to": "2026-10-29"})["rows"]
    assert before["rows"] == []
    stamp = datetime.now(MSK).isoformat()
    conn.execute(
        "UPDATE md.bond_quote_intraday SET bid = 1 WHERE secid='SU26238RMFS4' "
        "AND ts='2026-10-29 18:44+03'"
    )
    conn.commit()
    old = _records(
        _get(
            client,
            "/v1/intraday/bonds",
            **{"from": "2026-10-29", "to": "2026-10-29", "known_at": stamp},
        )
    )
    assert Decimal("52.2") in {r["bid"] for r in old}
    now = _records(_get(client, "/v1/intraday/bonds", **{"from": "2026-10-29", "to": "2026-10-29"}))
    assert Decimal("1") in {r["bid"] for r in now}


def test_intraday_etag_and_errors(client: TestClient, conn: Conn) -> None:
    _seed_quotes(conn)
    r = client.get("/v1/intraday/zcyc_securities", params={"from": "2026-10-29"}, headers=AUTH)
    assert r.status_code == 200
    again = client.get(
        "/v1/intraday/zcyc_securities",
        params={"from": "2026-10-29"},
        headers={**AUTH, "If-None-Match": r.headers["etag"]},
    )
    assert again.status_code == 304
    _bad(client, "/v1/intraday/bonds", **{"from": "2026-10-29T10:00:00"})  # no offset
    _bad(client, "/v1/intraday/bonds", **{"from": "2026-10-30", "to": "2026-10-29"})
    _bad(client, "/v1/intraday/repo", **{"from": "2026-10-29", "market": "xyz"})
    _bad(client, "/v1/intraday/bonds", **{"from": "2026-10-29", "fields": "nope"})
    assert client.get("/v1/intraday/bonds", params={"from": "2026-10-29"}).status_code == 401


@respx.mock
def test_daily_endpoints(client: TestClient, iss_settings: Settings, db: Database) -> None:
    respx.get(ANALYTICS_URL).mock(side_effect=_analytics)
    respx.get(REPO_HIST).mock(side_effect=_repo_hist)
    run("index_constituents", {"days_back": [1]}, iss_settings, db)
    run("repo_ccp_bond", {"mode": "final", "days_back": [1]}, iss_settings, db)
    run("repo_gc_term", {"mode": "final", "days_back": [1]}, iss_settings, db)
    cons = _get(client, "/v1/index/constituents", **{"from": "2026-10-28", "index": "RGBI"})
    assert len(cons["rows"]) == 29
    assert sum(r["weight_pct"] for r in _records(cons)) == Decimal("100.00")
    ccp = _get(client, "/v1/repo/ccp", **{"from": "2026-10-28", "board": "EQRP"})
    assert {r["secid"] for r in _records(ccp)} == {"SU26207RMFS9", "SU26212RMFS9", "SU26218RMFS6"}
    gc = _get(client, "/v1/repo/gc", **{"from": "2026-10-28", "secid": mi.GC_BOND_BASKET})
    assert len(gc["rows"]) == len(mi.GCC_BOARDS)
    assert {r["board"] for r in _records(gc)} == set(mi.GCC_BOARDS)


def test_bond_close_endpoint_and_latency(
    client: TestClient, settings: Settings, db: Database, conn: Conn
) -> None:
    _seed_quotes(conn)
    run("bond_close_quote", {"days_back": [0]}, settings, db)
    body = _get(
        client, "/v1/history/bond_close", **{"from": "2026-10-29", "basis": "zcyc_securities"}
    )
    assert [r["board"] for r in _records(body)] == ["ZCYC"]
    lat = _get(client, "/v1/intraday/latency")
    datasets = {(r["dataset"], r["latency"]) for r in lat["data"]}
    assert ("bond_quote_intraday", "delayed") in datasets
    assert ("zcyc_security_quote", "realtime") in datasets
    row = next(r for r in lat["data"] if r["dataset"] == "zcyc_security_quote")
    assert row["age_s"] is not None and row["rows_last_hour"] == 1
