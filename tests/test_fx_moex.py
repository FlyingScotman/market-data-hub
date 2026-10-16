"""MOEX currency market: parsers on recorded ISS fixtures, fx_* jobs against PostgreSQL."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.fx_moex  # noqa: F401 - registers the jobs
from mdhub.config import Settings
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL, IssFormatError
from mdhub.core.timeutil import MSK
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import moex_fx as fx

FIX = Path(__file__).parent / "fixtures" / "moex_fx"
Conn = psycopg.Connection[DictRow]


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def run(name: str, params: dict[str, Any], settings: Settings, db: Database) -> JobOutcome:
    return run_job(name, params, settings=settings, db=db, http_factory=http_factory)


def paged(pages: dict[int, dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[int(request.url.params.get("start", "0"))])

    return handler


def counts(conn: Conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
    assert row is not None
    return int(next(iter(row.values())))


# ---------------------------------------------------------------------- parsers


@pytest.mark.parametrize(
    ("secid", "expected"),
    [
        ("CNYRUB_TOM", ("outright", "TOM", None, None)),
        ("USD000UTSTOM", ("outright", "TOM", None, None)),
        ("CNY000000TOD", ("outright", "TOD", None, None)),
        ("EURUSD000TOM", ("outright", "TOM", None, None)),
        ("EUR_RUB__TOD", ("outright", "TOD", None, None)),
        ("CNYRUB_SPT", ("outright", "SPT", None, None)),
        ("USDRUB_TDB", ("outright", "TDB", None, None)),
        ("CNYRUBTODTOM", ("swap", "TOD/TOM", "TOD", "TOM")),
        ("CNYRUBTDSTMS", ("swap", "TDS/TMS", "TDS", "TMS")),
        ("USDRUBTMSSTS", ("swap", "TMS/STS", "TMS", "STS")),
        ("CNYRUB_TOM1W", ("swap", "TOM/1W", "TOM", "1W")),
        ("EURRUB_TOM1Y", ("swap", "TOM/1Y", "TOM", "1Y")),
        ("CNYRUB_TOM1D", ("swap", "TOM/SPT", "TOM", "SPT")),
        ("USDRUB_FIX0", ("fixing", "FIX", None, None)),
        ("CNYRUB_WAP0", ("wap", "WAP", None, None)),
        ("WEIRD", ("other", None, None, None)),
    ],
)
def test_classify_fx_secid(secid: str, expected: tuple[Any, ...]) -> None:
    assert fx.classify_fx_secid(secid) == expected


def test_parse_fx_instruments() -> None:
    rows = {
        (r["secid"], r["board"]): r for r in fx.parse_fx_instruments(load("selt_securities.json"))
    }
    cny = rows[("CNYRUB_TOM", "CETS")]
    assert (cny["base_ccy"], cny["quote_ccy"], cny["kind"], cny["tenor"]) == (
        "CNY",
        "RUB",
        "outright",
        "TOM",
    )
    assert cny["settle_date"] == date(2026, 10, 19)
    assert isinstance(cny["min_step"], Decimal)
    assert rows[("USDRUB_FIX0", "FIXN")]["kind"] == "fixing"
    assert rows[("EURRUB_TOM1Y", "CETS")]["far_leg"] == "1Y"
    assert ("CNYRUB_TOM", "LICU") in rows  # all boards kept


def test_parse_fx_marketdata_delayed_and_priced_only() -> None:
    rows, latency = fx.parse_fx_marketdata(load("cets_board.json"))
    assert latency == "delayed"  # public ISS: SYSTIME - UPDATETIME = 900 s
    by = {r["secid"]: r for r in rows}
    assert "EURUSD000TOM" not in by  # no prices at all -> nothing stored
    cny = by["CNYRUB_TOM"]
    assert cny["ts"] == datetime(2026, 10, 16, 10, 35, 14, tzinfo=MSK)
    assert cny["source_ts"] == datetime(2026, 10, 16, 10, 50, 14, tzinfo=MSK)
    assert cny["last"] == Decimal("12.5115")
    assert cny["bid"] is None and cny["latency"] == "delayed"


def test_snapshot_ts_midnight_rollover() -> None:
    sys_ts = datetime(2026, 10, 16, 0, 5, 0, tzinfo=MSK)
    assert fx.snapshot_ts("23:50:00", sys_ts) == datetime(2026, 10, 15, 23, 50, tzinfo=MSK)
    assert fx.snapshot_ts("00:04:59", sys_ts) == datetime(2026, 10, 16, 0, 4, 59, tzinfo=MSK)
    assert fx.snapshot_ts(None, sys_ts) is None


def test_classify_latency() -> None:
    t = datetime(2026, 10, 16, 10, 0, tzinfo=MSK)
    assert fx.classify_latency([(t, t.replace(second=1))]) == "realtime"
    assert fx.classify_latency([(t, t.replace(minute=15))]) == "delayed"
    assert fx.classify_latency([]) == "delayed"


def test_parse_fx_history_zero_without_trades_is_null() -> None:
    rows = fx.parse_fx_history(
        [
            dict(zip(load("selt_history_p0.json")["history"]["columns"], r, strict=True))
            for r in load("selt_history_p0.json")["history"]["data"]
        ]
    )
    zero = rows[0]
    assert zero["num_trades"] == 0
    assert zero["close"] is None and zero["open"] is None
    cets = next(r for r in rows if r["board"] == "CETS" and r["secid"] == "CNYRUB_TOM")
    assert cets["close"] == Decimal("12.4825") and cets["waprice"] == Decimal("12.5086")


def test_parse_fx_history_drift() -> None:
    with pytest.raises(IssFormatError, match="lacks"):
        fx.parse_fx_history([{"BOARDID": "CETS", "TRADEDATE": "2026-10-15", "SECID": "X"}])


def test_parse_indicative() -> None:
    rows = fx.parse_indicative(load("indicative_2026-10-15.json"))
    assert len(rows) == 22
    usd = {(r["ccy_pair"], r["clearing"]): r for r in rows}[("USD/RUB", "mc")]
    assert usd["rate"] == Decimal("83.76")
    assert (usd["base_ccy"], usd["quote_ccy"], usd["trade_time"]) == ("USD", "RUB", time(19))
    bad = copy.deepcopy(load("indicative_2026-10-15.json"))
    bad["securities"]["data"][0][2] = "USDRUB"
    with pytest.raises(IssFormatError, match="XXX/YYY"):
        fx.parse_indicative(bad)
    bad = copy.deepcopy(load("indicative_2026-10-15.json"))
    bad["securities"]["columns"][3] = "value"
    with pytest.raises(IssFormatError):
        fx.parse_indicative(bad)


# -------------------------------------------------------------------------- jobs

SELT_SEC_URL = ISS_BASE_URL + fx.SELT_SECURITIES
SELT_HIST_URL = ISS_BASE_URL + fx.SELT_HISTORY
CETS_URL = ISS_BASE_URL + fx.CETS_SECURITIES


def _mock_eod(pages: dict[int, dict[str, Any]]) -> respx.Route:
    respx.get(SELT_SEC_URL).mock(
        return_value=httpx.Response(200, json=load("selt_securities.json"))
    )
    return respx.get(SELT_HIST_URL).mock(side_effect=paged(pages))


@respx.mock
def test_fx_moex_eod_idempotent_and_revisions(settings: Settings, db: Database, conn: Conn) -> None:
    pages = {0: load("selt_history_p0.json"), 3: load("selt_history_p1.json")}
    route = _mock_eod(pages)
    params = {"date_from": "2026-10-15", "date_to": "2026-10-15"}
    out = run("fx_moex_eod", params, settings, db)
    assert out.status == "succeeded"
    assert route.call_count == 2
    assert counts(conn, "SELECT count(*) FROM md.fx_eod") == 5
    assert counts(conn, "SELECT count(*) FROM ref.fx_instrument") == 14
    row = conn.execute(
        "SELECT close, capture_id FROM md.fx_eod WHERE secid='CNYRUB_TOM' AND board='CETS'"
    ).fetchone()
    assert row is not None and row["close"] == Decimal("12.4825")
    cap = conn.execute(
        "SELECT endpoint, source FROM ops.raw_capture WHERE id=%s", (row["capture_id"],)
    ).fetchone()
    assert cap == {"endpoint": "selt_history", "source": "moex_iss"}

    # identical reload: no change, no history
    out = run("fx_moex_eod", params, settings, db)
    assert out.status == "succeeded"
    conn.commit()
    r = conn.execute(
        "SELECT rows_inserted, rows_updated FROM ops.job_run WHERE id=%s", (out.run_id,)
    ).fetchone()
    assert r == {"rows_inserted": 0, "rows_updated": 0}
    assert counts(conn, "SELECT count(*) FROM ops.row_history") == 0

    # revision: CETS close changed -> old image in row_history
    p1 = copy.deepcopy(pages[0])
    cols = p1["history"]["columns"]
    for data in p1["history"]["data"]:
        if data[cols.index("BOARDID")] == "CETS" and data[cols.index("SECID")] == "CNYRUB_TOM":
            data[cols.index("CLOSE")] = 12.5
    pages[0] = p1
    run("fx_moex_eod", params, settings, db)
    conn.commit()
    hist = conn.execute(
        "SELECT row_key, old_row FROM ops.row_history WHERE table_name='md.fx_eod'"
    ).fetchall()
    assert len(hist) == 1
    assert hist[0]["row_key"]["secid"] == "CNYRUB_TOM"
    assert Decimal(str(hist[0]["old_row"]["close"])) == Decimal("12.4825")


@respx.mock
def test_fx_moex_eod_empty_day_records_no_data(
    settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    """Review F1: an empty ISS answer is a recorded no-data day (gap), not a partial run - a
    partial run made the backfill worker leave the gap open and re-queue it for ever."""
    today_is(date(2026, 10, 19))
    empty = {
        "history": {"columns": load("selt_history_p0.json")["history"]["columns"], "data": []},
        "history.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 0, 100]]},
    }
    _mock_eod({0: empty})
    out = run("fx_moex_eod", {"date_from": "2026-10-15", "date_to": "2026-10-16"}, settings, db)
    assert out.status == "succeeded"
    assert counts(conn, "SELECT count(*) FROM md.fx_eod") == 0
    gaps = conn.execute(
        "SELECT gap_date, status FROM ops.data_gap WHERE dataset = 'fx_eod' ORDER BY 1"
    ).fetchall()
    # N5: the grace period counts business days - Friday seen on Monday is still open
    assert gaps == [
        {"gap_date": date(2026, 10, 15), "status": "unfillable"},
        {"gap_date": date(2026, 10, 16), "status": "open"},
    ]
    conn.commit()
    _mock_eod({0: empty})
    out = run("fx_moex_eod", {"mode": "prelim", "days_back": "0"}, settings, db)
    assert out.status == "succeeded"  # prelim only logs (history may not be published yet)
    n = counts(conn, "SELECT count(*) FROM ops.data_gap WHERE gap_date = '2026-10-19'")
    assert n == 0


@respx.mock
def test_fx_moex_intraday_change_only(settings: Settings, db: Database, conn: Conn) -> None:
    payload = load("cets_board.json")
    respx.get(CETS_URL).mock(side_effect=lambda _r: httpx.Response(200, json=payload))
    assert run("fx_moex_intraday", {}, settings, db).status == "succeeded"
    first = counts(conn, "SELECT count(*) FROM md.fx_intraday")
    assert first == 6  # priced CETS rows of the fixture
    conn.commit()
    row = conn.execute(
        "SELECT r.endpoint, i.latency, i.source, i.source_variant FROM md.fx_intraday i "
        "JOIN ops.raw_capture r ON r.id = i.capture_id LIMIT 1"
    ).fetchone()
    assert row == {
        "endpoint": "intraday:fx_cets_marketdata",
        "latency": "delayed",
        "source": "moex_iss",
        "source_variant": "iss_public",
    }

    run("fx_moex_intraday", {}, settings, db)  # unchanged snapshot -> nothing new
    conn.commit()
    assert counts(conn, "SELECT count(*) FROM md.fx_intraday") == first

    cols = payload["marketdata"]["columns"]
    for data in payload["marketdata"]["data"]:
        if data[cols.index("SECID")] == "CNYRUB_TOM":
            data[cols.index("LAST")] = 12.52
            data[cols.index("UPDATETIME")] = "10:36:00"
            data[cols.index("SYSTIME")] = "2026-10-16 10:51:00"
    run("fx_moex_intraday", {}, settings, db)
    conn.commit()
    assert counts(conn, "SELECT count(*) FROM md.fx_intraday") == first + 1
    assert counts(conn, "SELECT count(*) FROM md.fx_intraday WHERE secid='CNYRUB_TOM'") == 2


@respx.mock
def test_fx_indicative_and_fixings(
    settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    def indicative(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == "2026-10-15":
            return httpx.Response(200, json=load("indicative_2026-10-15.json"))
        empty = copy.deepcopy(load("indicative_2026-10-15.json"))
        empty["securities"]["data"] = []
        return httpx.Response(200, json=empty)

    respx.get(ISS_BASE_URL + fx.INDICATIVE).mock(side_effect=indicative)
    respx.get(ISS_BASE_URL + fx.FIXING_LIST).mock(
        return_value=httpx.Response(200, json=load("fixing_list.json"))
    )
    fix_route = respx.get(ISS_BASE_URL + fx.FIXING_SECID.format(secid="SRATE_CNY_ON")).mock(
        return_value=httpx.Response(200, json=load("fixing_srate.json"))
    )
    today_is(date(2026, 10, 16))
    out = run("fx_indicative", {"date_from": "2026-10-15", "date_to": "2026-10-16"}, settings, db)
    assert out.status == "succeeded"  # 2026-10-16 has no rates (yet): an open gap, not partial
    gap = conn.execute(
        "SELECT gap_date, status FROM ops.data_gap WHERE dataset = 'fx_indicative'"
    ).fetchall()
    assert gap == [{"gap_date": date(2026, 10, 16), "status": "open"}]  # within grace
    assert counts(conn, "SELECT count(*) FROM md.fx_indicative") == 22
    assert fix_route.calls.last.request.url.params["from"] == "2026-10-15"
    fixing = conn.execute("SELECT * FROM md.fx_fixing ORDER BY fixing_date").fetchall()
    assert [f["fixing_date"] for f in fixing] == [date(2026, 10, 15)]  # in range only
    assert fixing[0]["rate"] == Decimal("0.00468")
    assert fixing[0]["rate_pct"] is not None


def test_datasets_registered() -> None:
    spec = get_dataset("fx_eod")
    assert spec.backfill_job == "fx_moex_eod"
    assert spec.calendar == "MOEX_FX"
    assert spec.history_start == date(2024, 1, 3)
    assert get_dataset("fx_indicative").backfill_job == "fx_indicative"
