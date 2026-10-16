"""Business-day calendars: curated file, MOEX ISS calendar, observed trading, DbCalendar."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.cbr  # noqa: F401 - registers cbr_meetings
from mdhub.config import Settings
from mdhub.core.bizday import DbCalendar
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import calendar as cal
from mdhub.sources import cbr

FIX = Path(__file__).parent / "fixtures" / "official"
Conn = psycopg.Connection[DictRow]


def _json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


# ------------------------------------------------------------------------- curated


def test_curated_file_is_valid() -> None:
    cc = cal.load_curated()
    assert cc.known_at.tzinfo is not None and cc.version
    years = {(y.calendar, y.year): y for y in cc.years}
    for cal_name in ("RU_PRODUCTION", "MOEX_STOCK", "MOEX_FX", "MOEX_FORTS"):
        assert (cal_name, 2026) in years and (cal_name, 2027) in years
    ru26 = years[("RU_PRODUCTION", 2026)].days()
    ru27 = years[("RU_PRODUCTION", 2027)].days()
    assert sum(ru26.values()) == 247 and sum(ru27.values()) == 247  # official day counts
    assert ru27[date(2027, 2, 20)] is True  # working Saturday (Decree 1187)
    assert ru27[date(2027, 11, 5)] is False and ru27[date(2027, 12, 31)] is False
    assert ru26[date(2026, 1, 9)] is False and ru26[date(2026, 12, 31)] is False
    moex26 = years[("MOEX_STOCK", 2026)].days()
    # MOEX trades on some official non-working days (audit 04)
    assert moex26[date(2026, 1, 5)] and moex26[date(2026, 3, 9)] and moex26[date(2026, 5, 11)]
    assert not moex26[date(2026, 11, 4)] and not moex26[date(2026, 12, 31)]
    assert years[("MOEX_STOCK", 2027)].source == "curated_unverified"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["calendars"]["RU_PRODUCTION"][0]["non_working"].append("2026-01-03"),
        lambda d: d["calendars"]["RU_PRODUCTION"][0]["working_weekends"].append("2026-01-05"),
        lambda d: d["calendars"].update({"TARGET": []}),
        lambda d: d.update({"known_at": "2026-10-16T12:00:00"}),
    ],
)
def test_curated_validation(mutate: Any) -> None:
    text = (Path(cal.__file__).parent.parent / "data" / cal.CURATED_RESOURCE).read_text("utf-8")
    data = json.loads(text)
    mutate(data)
    with pytest.raises(FormatDriftError):
        cal.load_curated(json.dumps(data))


# ---------------------------------------------------------------------- ISS calendar


def _iss_rows() -> list[dict[str, Any]]:
    rows = []
    for engine in ("stock", "currency", "futures"):
        block = _json(f"iss_calendar_{engine}.json")["calendar"]
        rows += [dict(zip(block["columns"], r, strict=True)) for r in block["data"]]
    return rows


def test_iss_calendar_days() -> None:
    stock = cal.iss_calendar_days(_iss_rows(), "stock")
    assert stock[date(2026, 11, 4)] is False  # holiday listed for currency only -> applied
    assert stock[date(2026, 12, 31)] is False
    assert stock[date(2026, 1, 5)] is True  # RU non-working day, MOEX trades ("N")
    assert stock[date(2026, 1, 17)] is False  # weekend session, attributed to Monday ("W")
    assert stock[date(2026, 10, 16)] is True
    assert len(stock) == 365


def test_observed_days() -> None:
    days = cal.observed_days([date(2026, 5, 4), date(2026, 4, 30)], date(2026, 4, 29))
    assert days == {
        date(2026, 4, 29): False,
        date(2026, 4, 30): True,
        date(2026, 5, 1): False,
        date(2026, 5, 2): False,
        date(2026, 5, 3): False,
        date(2026, 5, 4): True,
    }


# ------------------------------------------------------------------- DB behaviour


def test_write_calendar_days_and_db_lookup(db: Database, conn: Conn) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 10, 1, tzinfo=UTC)
    with db.connection() as c:
        r1 = cal.write_calendar_days(
            c,
            "MOEX_STOCK",
            "curated",
            {date(2026, 11, 4): True, date(2026, 11, 5): True},
            known_at=t0,
            run_id=None,
        )
        r2 = cal.write_calendar_days(
            c, "MOEX_STOCK", "curated", {date(2026, 11, 4): True}, known_at=t1, run_id=None
        )
    assert r1.inserted == 2 and r2.unchanged == 1 and r2.changed == 0
    with db.connection() as c:
        cal.write_calendar_days(
            c,
            "MOEX_STOCK",
            "moex_iss_calendar",
            {date(2026, 11, 4): False},
            known_at=t1,
            run_id=None,
        )
    dbcal = DbCalendar(db, ttl=0)
    assert dbcal(date(2026, 11, 4), "MOEX_STOCK") is False  # newer known_at wins
    assert dbcal(date(2026, 11, 5), "MOEX_STOCK") is True
    # a flag change rewrites the row with the new known_at and keeps the old image
    with db.connection() as c:
        cal.write_calendar_days(
            c, "MOEX_STOCK", "curated", {date(2026, 11, 5): False}, known_at=t1, run_id=None
        )
    assert dbcal(date(2026, 11, 5), "MOEX_STOCK") is False
    hist = conn.execute(
        "SELECT old_row FROM ops.row_history WHERE table_name = 'ref.calendar_day'"
    ).fetchall()
    assert len(hist) == 1 and hist[0]["old_row"]["is_business_day"] is True
    eff = conn.execute(
        "SELECT * FROM ref.calendar_effective WHERE calendar='MOEX_STOCK' AND day='2026-11-04'"
    ).fetchone()
    assert eff is not None and eff["source"] == "moex_iss_calendar"


# ----------------------------------------------------------------- refresh (job)


def _history_handler(request: httpx.Request) -> httpx.Response:
    cols = request.url.params.get("history.columns", "TRADEDATE").split(",")
    dates = [r[0] for r in _json("iss_hist_imoex.json")["history"]["data"]]
    data = [[d, 10][: len(cols)] for d in dates]
    body = {
        "history": {"columns": cols, "data": data},
        "history.cursor": {
            "columns": ["INDEX", "TOTAL", "PAGESIZE"],
            "data": [[0, len(data), 100]],
        },
    }
    return httpx.Response(200, json=body)


@pytest.fixture
def iss() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=ISS_BASE_URL + "/history/").mock(side_effect=_history_handler)
        for engine in ("stock", "currency", "futures"):
            router.get(f"{ISS_BASE_URL}/engines/{engine}/calendar.json").mock(
                return_value=httpx.Response(200, json=_json(f"iss_calendar_{engine}.json"))
            )
        router.get(host="www.cbr.ru", path=cbr.MEETINGS_PATH).mock(
            return_value=httpx.Response(
                200,
                content=(FIX / "cbr_cal_mp.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        )
        yield router


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"iss.moex.com": 1e6, "www.cbr.ru": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def test_cbr_meetings_calendar_mode_refreshes_calendars(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    for d in ("2026-04-29", "2026-04-30", "2026-05-04", "2026-05-12"):
        conn.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value)"
            " VALUES ('RUONIA', %s, 'cbr', 14)",
            (d,),
        )
    conn.commit()
    out = run_job(
        "cbr_meetings",
        {"mode": "calendar_full"},
        settings=settings,
        db=db,
        http_factory=_http_factory,
    )
    assert out.status == "succeeded", out.result
    n = conn.execute("SELECT count(*) AS n FROM ref.cbr_meeting").fetchone()
    assert n is not None and n["n"] > 60
    dec = conn.execute("SELECT * FROM ref.cbr_meeting WHERE meeting_date='2026-12-18'").fetchone()
    assert dec is not None and dec["is_estimated"] and dec["effective_date"] == date(2026, 12, 21)

    dbcal = DbCalendar(db, ttl=0)
    # observed MOEX trading (fixture: 2026-04-27..05-15): May 1 closed, May 11 open
    assert dbcal(date(2026, 5, 1), "MOEX_STOCK") is False
    assert dbcal(date(2026, 5, 11), "MOEX_STOCK") is True
    assert dbcal(date(2026, 5, 11), "MOEX_FORTS") is True
    # RU production calendar: observed RUONIA days; May 11 was a non-working day
    assert dbcal(date(2026, 5, 11), "RU_PRODUCTION") is False
    assert dbcal(date(2026, 5, 4), "RU_PRODUCTION") is True
    # future: ISS calendar (2026) and curated (2027)
    assert dbcal(date(2026, 11, 4), "MOEX_FX") is False
    assert dbcal(date(2026, 11, 5), "MOEX_FX") is True
    assert dbcal(date(2027, 2, 20), "RU_PRODUCTION") is True
    assert dbcal(date(2027, 1, 4), "RU_PRODUCTION") is False
    assert dbcal(date(2027, 1, 4), "MOEX_STOCK") is True  # curated_unverified rule
    src = conn.execute(
        """SELECT source FROM ref.calendar_effective
           WHERE calendar = 'MOEX_STOCK' AND day = '2026-05-01'"""
    ).fetchone()
    assert src is not None and src["source"] == "observed_moex_stock"

    # second incremental run changes nothing
    again = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=_http_factory
    )
    row = conn.execute(
        "SELECT rows_inserted, rows_updated FROM ops.job_run WHERE id = %s", (again.run_id,)
    ).fetchone()
    assert row == {"rows_inserted": 0, "rows_updated": 0}


def test_seed_calendar_function(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    summary = cal.seed_calendar(settings, db, since=date(2026, 4, 1), http_factory=_http_factory)
    assert summary["curated"] != "failed" and summary["moex_iss_calendar"] != "failed"
    run = conn.execute("SELECT job, status FROM ops.job_run").fetchone()
    assert run == {"job": "calendar_seed", "status": "succeeded"}
