"""Business-day calendars: curated file, MOEX ISS calendar, observed trading, DbCalendar."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.cbr  # registers cbr_meetings
import mdhub.loaders.ecb  # noqa: F401 - registers the ecb_fx dataset
from mdhub.config import Settings
from mdhub.core.bizday import CALENDARS, DbCalendar, weekday_calendar
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.timeutil import utc_now
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import calendar as cal
from mdhub.quality.checks import expected_days, scan_dataset, sync_gaps
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


_LABOUR_CODE_HOLIDAYS = [(1, d) for d in range(1, 9)] + [
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
]
_MOEX_CLOSED = [(1, 1), (1, 2), (1, 7), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4), (12, 31)]


def _labour_code_rule(year: int) -> set[date]:
    """Art. 112 only: weekday holidays, plus a weekend holiday (other than Jan 1-6, 8) moved to
    the next working day (no Government decree)."""
    hol = [date(year, m, d) for m, d in _LABOUR_CODE_HOLIDAYS]
    out = {h for h in hol if h.weekday() < 5}
    for h in sorted(hol):
        if h.weekday() >= 5 and not (h.month == 1 and h.day != 7):
            x = h + timedelta(days=1)
            while x.weekday() >= 5 or x in hol or x in out:
                x += timedelta(days=1)
            out.add(x)
    return out


def test_provisional_calendars_to_2037_follow_their_rules() -> None:
    """2028-2037 are provisional (curated_unverified): RU_PRODUCTION = the Labour Code rule
    (decree transfers unknown), MOEX = the MOEX practice rule; the rules reproduce the
    verified years (2026/27 decree days come on top)."""
    years = {(y.calendar, y.year): y for y in cal.load_curated().years}
    for y in (2026, 2027):
        assert _labour_code_rule(y) <= years[("RU_PRODUCTION", y)].non_working
    assert {date(2027, m, d) for m, d in _MOEX_CLOSED if date(2027, m, d).weekday() < 5} == years[
        ("MOEX_STOCK", 2027)
    ].non_working
    for year in range(2028, 2038):
        ru = years[("RU_PRODUCTION", year)]
        assert ru.source == "curated_unverified" and ru.non_working == _labour_code_rule(year)
        assert ru.working_weekends == frozenset()
        moex = {date(year, m, d) for m, d in _MOEX_CLOSED if date(year, m, d).weekday() < 5}
        for name in ("MOEX_STOCK", "MOEX_FX", "MOEX_FORTS"):
            entry = years[(name, year)]
            assert entry.source == "curated_unverified" and entry.non_working == moex
    assert max(y.year for y in years.values()) == 2037
    assert _labour_code_rule(2029) >= {date(2029, 1, 9), date(2029, 11, 5)}  # Jan 7 / Nov 4 Sun


def _ru(data: dict[str, Any], year: int) -> dict[str, Any]:
    entry: dict[str, Any] = next(e for e in data["calendars"]["RU_PRODUCTION"] if e["year"] == year)
    return entry


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: _ru(d, 2026)["non_working"].append("2026-01-03"),
        lambda d: _ru(d, 2026)["working_weekends"].append("2026-01-05"),
        lambda d: d["calendars"].update({"NYSE": []}),
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
    """Only listed days: never a synthesized year (review F4)."""
    rows = _iss_rows()
    stock = cal.iss_calendar_days(rows, "stock")
    assert stock[date(2026, 11, 4)] is False  # holiday listed for currency only -> applied
    assert stock[date(2026, 12, 31)] is False
    assert stock[date(2026, 1, 17)] is False  # weekend session, attributed to Monday ("W")
    assert date(2026, 10, 16) not in stock  # ordinary weekday: not listed, not written
    assert date(2026, 1, 5) not in stock  # listed (N) for currency only: not for stock
    currency = cal.iss_calendar_days(rows, "currency")
    assert currency[date(2026, 1, 5)] is True  # RU non-working day, MOEX FX trades ("N")
    listed = {r["date"] for r in rows if r["engine"] in ("stock", "currency")}
    assert {d.isoformat() for d in stock} <= listed


def test_iss_partial_year_does_not_override_curated(db: Database, conn: Conn) -> None:
    """In December ISS lists a few January days of the next year: they must not turn the
    curated holidays of that year into business days (the old code synthesized the year)."""
    curated = cal.load_curated()
    y2027 = next(y for y in curated.years if y.calendar == "MOEX_STOCK" and y.year == 2027)
    rows = [
        {
            "engine": "stock",
            "date": "2027-01-01",
            "is_work_day": 0,
            "trade_session_date": None,
            "reason": "H",
        },
        {
            "engine": "stock",
            "date": "2027-01-02",
            "is_work_day": 0,
            "trade_session_date": None,
            "reason": "H",
        },
    ]
    iss_days = cal.iss_calendar_days(rows, "stock")
    assert set(iss_days) == {date(2027, 1, 1), date(2027, 1, 2)}
    t_cur = datetime(2026, 10, 16, tzinfo=UTC)
    t_iss = datetime(2026, 12, 20, tzinfo=UTC)  # newer than the curated file
    with db.connection() as c:
        cal.write_calendar_days(
            c, "MOEX_STOCK", y2027.source, y2027.days(), known_at=t_cur, run_id=None
        )
        cal.write_calendar_days(
            c, "MOEX_STOCK", "moex_iss_calendar", iss_days, known_at=t_iss, run_id=None
        )
        # an old synthesized row (pre-fix data) of the ISS source would outrank curated ...
        cal.write_calendar_days(
            c,
            "MOEX_STOCK",
            "moex_iss_calendar",
            {date(2027, 2, 23): True},
            known_at=t_iss,
            run_id=None,
        )
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2027, 2, 23), "MOEX_STOCK") is True  # bad pre-fix row still wins (rank 2)
    # ... and the repair path of the refresh removes rows ISS no longer lists
    with db.connection() as c:
        res = cal._drop_unlisted(
            c, "MOEX_STOCK", "moex_iss_calendar", set(iss_days), after=None, run_id=None
        )
    assert res.updated == 1
    assert bc(date(2027, 2, 23), "MOEX_STOCK") is (
        date(2027, 2, 23) not in y2027.non_working and date(2027, 2, 23).weekday() < 5
    )
    assert bc(date(2027, 1, 1), "MOEX_STOCK") is False
    for d, flag in y2027.days().items():
        if d not in iss_days:
            assert bc(d, "MOEX_STOCK") is flag, d
    eff = conn.execute(
        "SELECT source FROM ref.calendar_effective WHERE calendar = 'MOEX_STOCK' "
        "AND day = '2027-02-23'"
    ).fetchone()
    assert eff == {"source": y2027.source}
    # a versioned change, not a DELETE: the row stays (is_deleted), its image is in history
    gone = conn.execute(
        "SELECT is_deleted FROM ref.calendar_day WHERE calendar = 'MOEX_STOCK' "
        "AND day = '2027-02-23' AND source = 'moex_iss_calendar'"
    ).fetchone()
    assert gone == {"is_deleted": True}
    hist = conn.execute(
        "SELECT old_row ->> 'day' AS day, old_row ->> 'is_deleted' AS deleted "
        "FROM ops.row_history WHERE table_name = 'ref.calendar_day'"
    ).fetchall()
    assert hist == [{"day": "2027-02-23", "deleted": "false"}]


def test_observed_outranks_newer_curated(db: Database) -> None:
    with db.connection() as c:
        cal.write_calendar_days(
            c,
            "MOEX_FX",
            "observed_moex_fx",
            {date(2026, 6, 15): False},
            known_at=datetime(2026, 6, 16, tzinfo=UTC),
            run_id=None,
        )
        cal.write_calendar_days(
            c,
            "MOEX_FX",
            "curated",
            {date(2026, 6, 15): True},
            known_at=datetime(2026, 10, 16, tzinfo=UTC),
            run_id=None,
        )
    assert DbCalendar(db, ttl=0)(date(2026, 6, 15), "MOEX_FX") is False


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


# -------------------------------------------------------------------------- TARGET


@pytest.mark.parametrize(
    ("year", "easter", "good_friday", "easter_monday"),
    [
        (2015, date(2015, 4, 5), date(2015, 4, 3), date(2015, 4, 6)),
        (2019, date(2019, 4, 21), date(2019, 4, 19), date(2019, 4, 22)),
        (2024, date(2024, 3, 31), date(2024, 3, 29), date(2024, 4, 1)),
        (2025, date(2025, 4, 20), date(2025, 4, 18), date(2025, 4, 21)),
        (2026, date(2026, 4, 5), date(2026, 4, 3), date(2026, 4, 6)),
        (2027, date(2027, 3, 28), date(2027, 3, 26), date(2027, 3, 29)),
        (2038, date(2038, 4, 25), date(2038, 4, 23), date(2038, 4, 26)),  # latest possible
        (2285, date(2285, 3, 22), date(2285, 3, 20), date(2285, 3, 23)),  # earliest possible
    ],
)
def test_easter_computus(year: int, easter: date, good_friday: date, easter_monday: date) -> None:
    assert cal.easter_sunday(year) == easter
    assert cal.target_closing_days(year) == {
        date(year, 1, 1),
        good_friday,
        easter_monday,
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def test_target_rule_days() -> None:
    assert "TARGET" in CALENDARS
    days = cal.target_rule_days(date(2025, 12, 20), date(2026, 1, 5))
    assert len(days) == 17
    closed = sorted(d for d, v in days.items() if not v)
    assert closed == [
        date(2025, 12, 20),
        date(2025, 12, 21),
        date(2025, 12, 25),
        date(2025, 12, 26),
        date(2025, 12, 27),
        date(2025, 12, 28),
        date(2026, 1, 1),
        date(2026, 1, 3),
        date(2026, 1, 4),
    ]
    assert days[date(2025, 12, 31)] is True  # TARGET is open on Dec 31


def test_target_precedence(db: Database, conn: Conn) -> None:
    """curated > rule_target > curated_unverified (a curated entry can override the rule)."""
    t_old = datetime(2026, 1, 1, tzinfo=UTC)
    t_new = datetime(2026, 10, 16, tzinfo=UTC)
    d1, d2 = date(2026, 4, 3), date(2026, 4, 6)
    with db.connection() as c:
        rule = {d1: False, d2: False}
        cal.write_calendar_days(
            c, "TARGET", cal.TARGET_RULE_SOURCE, rule, known_at=t_new, run_id=None
        )
        cal.write_calendar_days(c, "TARGET", "curated", {d1: True}, known_at=t_old, run_id=None)
        cal.write_calendar_days(
            c, "TARGET", "curated_unverified", {d2: True}, known_at=t_new, run_id=None
        )
    bc = DbCalendar(db, ttl=0)
    assert bc(d1, "TARGET") is True  # curated beats the rule (e.g. an extraordinary change)
    assert bc(d2, "TARGET") is False  # the rule beats a newer unverified guess
    eff = conn.execute(
        "SELECT day, source FROM ref.calendar_effective WHERE calendar = 'TARGET' ORDER BY day"
    ).fetchall()
    assert eff == [
        {"day": d1, "source": "curated"},
        {"day": d2, "source": cal.TARGET_RULE_SOURCE},
    ]
    ranks = conn.execute(
        "SELECT ref.calendar_source_rank(s) AS r FROM unnest(ARRAY['observed_x', "
        "'moex_iss_calendar', 'curated', 'rule_target', 'curated_unverified', 'x']) s"
    ).fetchall()
    assert [r["r"] for r in ranks] == [1, 2, 3, 4, 5, 9]
    assert cal.SOURCE_PRIORITY == {
        "observed": 1,
        "moex_iss_calendar": 2,
        "curated": 3,
        "rule": 4,
        "curated_unverified": 5,
    }


def test_target_crosscheck() -> None:
    rule = cal.target_rule_days(date(2026, 3, 1), date(2026, 5, 31))
    bd = rule.__getitem__
    good = [d for d, v in rule.items() if v and d <= date(2026, 4, 30)]
    assert cal.target_crosscheck(good, bd, date(2026, 5, 15)) == ([], [])
    assert cal.target_crosscheck([], bd, date(2026, 5, 15)) == ([], [])
    # a failed load (business day without rates) and rates on a closing day / a weekend
    lost = [d for d in good if d != date(2026, 4, 9)]
    extra = [*good, date(2026, 4, 3), date(2026, 4, 4)]
    assert cal.target_crosscheck(lost, bd, date(2026, 5, 15)) == ([date(2026, 4, 9)], [])
    assert cal.target_crosscheck(extra, bd, date(2026, 5, 15)) == (
        [],
        [date(2026, 4, 3), date(2026, 4, 4)],
    )
    # grace: no TARGET business day between the missing day and today yet -> not reported
    # (Thu 04-09 missing, Fri 04-10 loaded, today Mon 04-13: Fri lies between -> reported;
    # with today = Fri 04-10 nothing lies between)
    recent = [d for d in good if d <= date(2026, 4, 10) and d != date(2026, 4, 9)]
    assert cal.target_crosscheck(recent, bd, date(2026, 4, 13))[0] == [date(2026, 4, 9)]
    assert cal.target_crosscheck(recent, bd, date(2026, 4, 10))[0] == []


def _insert_ecb(conn: Conn, days: list[date], n_ccy: int = 20) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.ecb_fx (rate_date, ccy, rate_per_eur) VALUES (%s, %s, 1.5)",
            [(d, "C" + chr(65 + i // 26) + chr(65 + i % 26)) for d in days for i in range(n_ccy)],
        )
    conn.commit()


def _ecb_days(start: date, end: date, closed: set[date]) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in closed:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_target_calendar_rule_only(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TARGET comes from the rule alone (2002 .. 2037, or the end of next year if later); ECB
    rates only cross-check
    it - a failed ECB load never turns a day into a holiday."""
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 4, 15))
    holidays = {date(2026, 4, 3), date(2026, 4, 6)}  # Good Friday, Easter Monday
    _insert_ecb(conn, _ecb_days(date(2026, 3, 25), date(2026, 4, 14), holidays))
    summary = cal.seed_calendar(settings, db, http_factory=_http_factory)
    assert summary["target_crosscheck"] == {"checked_days": 13, "missing": 0, "unexpected": 0}
    run = conn.execute("SELECT status FROM ops.job_run").fetchone()
    assert run == {"status": "succeeded"}
    rows = conn.execute(
        "SELECT source, min(day) AS lo, max(day) AS hi, count(*) AS n, count(*) FILTER "
        "(WHERE NOT is_business_day AND extract(isodow FROM day) < 6) AS holidays "
        "FROM ref.calendar_day WHERE calendar = 'TARGET' GROUP BY source"
    ).fetchall()
    n_days = (date(2037, 12, 31) - date(2002, 1, 1)).days + 1
    weekday_holidays = sum(
        1 for y in range(2002, 2038) for d in cal.target_closing_days(y) if d.weekday() < 5
    )
    assert rows == [
        {
            "source": cal.TARGET_RULE_SOURCE,
            "lo": date(2002, 1, 1),
            "hi": cal.TARGET_RULE_END,
            "n": n_days,
            "holidays": weekday_holidays,
        }
    ]
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2026, 4, 3), "TARGET") is False
    assert bc(date(2026, 4, 6), "TARGET") is False
    assert bc(date(2026, 4, 2), "TARGET") is True
    assert bc(date(2026, 4, 4), "TARGET") is False
    assert bc(date(2015, 12, 25), "TARGET") is False
    assert bc(date(2026, 12, 31), "TARGET") is True  # TARGET is open on Dec 31
    assert bc(date(2027, 3, 26), "TARGET") is False  # Good Friday 2027
    assert bc(date(2027, 3, 29), "TARGET") is False  # Easter Monday 2027

    # an incremental refresh writes nothing new
    out = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=_http_factory
    )
    assert out.status == "succeeded", out.result
    assert out.result["calendar"][cal.TARGET_RULE_SOURCE]["written"] == 0


@pytest.mark.parametrize("direction", ["missing", "unexpected"])
def test_target_crosscheck_marks_run_partial(
    direction: str,
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 4, 15))
    holidays = {date(2026, 4, 3), date(2026, 4, 6)}
    if direction == "missing":
        holidays.add(date(2026, 4, 8))  # a failed ECB load
    else:
        _insert_ecb(conn, [date(2026, 4, 6)])  # rates on Easter Monday
    _insert_ecb(conn, _ecb_days(date(2026, 3, 25), date(2026, 4, 14), holidays))
    out = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=_http_factory
    )
    assert out.status == "partial", out.result
    check = out.result["calendar"]["target_crosscheck"]
    if direction == "missing":
        assert (check["missing"], check["unexpected"]) == (1, 0)
    else:
        assert (check["missing"], check["unexpected"]) == (0, 1)
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert err is not None and "TARGET cross-check" in err["error"]
    assert ("2026-04-08" if direction == "missing" else "2026-04-06") in err["error"]
    # the calendar follows the rule regardless of the ECB data
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2026, 4, 8), "TARGET") is True
    assert bc(date(2026, 4, 6), "TARGET") is False


def test_target_calendar_respects_since(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 4, 15))
    sql = (
        "SELECT min(day) AS lo, max(day) AS hi FROM ref.calendar_day WHERE calendar = 'TARGET' "
        "AND source = 'rule_target'"
    )
    cal.seed_calendar(settings, db, since=date(2026, 4, 1), http_factory=_http_factory)
    assert conn.execute(sql).fetchone() == {"lo": date(2026, 4, 1), "hi": date(2037, 12, 31)}
    cal.seed_calendar(settings, db, since=date(1999, 1, 4), http_factory=_http_factory)
    assert conn.execute(sql).fetchone() == {"lo": date(2002, 1, 1), "hi": date(2037, 12, 31)}


def test_ecb_fx_gap_scan_follows_target(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ECB publishes nothing on Dec 25/26: with calendar TARGET the gap scan no longer expects
    those days, and the old unfillable gaps are closed as not_expected."""
    spec = get_dataset("ecb_fx")
    assert spec.calendar == "TARGET"
    xmas = {date(2025, 12, 25), date(2025, 12, 26), date(2026, 1, 1)}
    _insert_ecb(conn, _ecb_days(date(2025, 12, 15), date(2026, 1, 9), xmas))
    for d in sorted(xmas):  # what the Monday-Friday calendar left behind
        conn.execute(
            "INSERT INTO ops.data_gap (dataset, key, gap_date, status, note) "
            "VALUES ('ecb_fx', '{}'::jsonb, %s, 'unfillable', 'no data from source')",
            (d,),
        )
    conn.commit()
    scoped = dataclasses.replace(spec, history_start=date(2025, 12, 15))
    wd = scan_dataset(conn, scoped, today=date(2026, 1, 12), calendar=weekday_calendar)
    assert {g.day for g in wd.gaps} == xmas  # the old behaviour

    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 1, 12))
    cal.seed_calendar(settings, db, http_factory=_http_factory)
    bc = DbCalendar(db, ttl=0)
    days = expected_days(scoped, date(2025, 12, 22), date(2026, 1, 2), bc)
    assert date(2025, 12, 25) not in days and date(2025, 12, 26) not in days
    assert date(2026, 1, 1) not in days and date(2025, 12, 31) in days
    check = scan_dataset(conn, scoped, today=date(2026, 1, 12), calendar=bc)
    assert check.gaps == []
    res = sync_gaps(conn, scoped, check, run_id=None)
    conn.commit()
    assert res.closed == 3
    st = conn.execute(
        "SELECT DISTINCT status FROM ops.data_gap WHERE dataset = 'ecb_fx'"
    ).fetchall()
    assert st == [{"status": "not_expected"}]


def test_ru_production_is_the_official_calendar_and_missing_ruonia_is_a_gap(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review §2.5 item 6: RU_PRODUCTION was derived from RUONIA (``observed_cbr_ruonia``), so a
    business day without a fixing became a holiday - on production the last working day of every
    year 2019-2025 (e.g. Tue 2025-12-30) and the 2020 non-working days. The curated official
    calendar now covers past years, the observed rows are retired, RUONIA only cross-checks,
    and the missing fixing is a ``cbr_ruonia`` gap."""
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 1, 20))
    # production state of 0.2.0: RUONIA observation rows with 2025-12-30 as a "holiday"
    holidays = {date(2025, 12, 30), date(2025, 12, 31)} | {date(2026, 1, d) for d in range(1, 10)}
    observed = {
        d: d.weekday() < 5 and d not in holidays
        for d in (date(2025, 12, 1) + timedelta(days=i) for i in range(45))
    }
    cal.write_calendar_days(
        conn, "RU_PRODUCTION", "observed_cbr_ruonia", observed, known_at=utc_now(), run_id=None
    )
    for d, flag in observed.items():
        if flag:
            conn.execute(
                "INSERT INTO md.fixing (index_code, fixing_date, source, value)"
                " VALUES ('RUONIA', %s, 'cbr', 16)",
                (d,),
            )
    conn.commit()
    dbcal = DbCalendar(db, ttl=0)
    assert dbcal(date(2025, 12, 30), "RU_PRODUCTION") is False  # the defect

    out = run_job(
        "cbr_meetings",
        {"mode": "calendar_full"},
        settings=settings,
        db=db,
        http_factory=_http_factory,
    )
    assert out.status == "succeeded", out.result
    assert dbcal(date(2025, 12, 30), "RU_PRODUCTION") is True
    assert dbcal(date(2025, 12, 31), "RU_PRODUCTION") is False  # Government decree transfer
    assert dbcal(date(2025, 11, 1), "RU_PRODUCTION") is True  # working Saturday
    eff = conn.execute(
        "SELECT source FROM ref.calendar_effective"
        " WHERE calendar = 'RU_PRODUCTION' AND day = '2025-12-30'"
    ).fetchone()
    assert eff == {"source": "curated"}
    live = conn.execute(
        "SELECT count(*) AS n FROM ref.calendar_day"
        " WHERE source = 'observed_cbr_ruonia' AND NOT is_deleted"
    ).fetchone()
    assert live == {"n": 0}
    hist = conn.execute(
        "SELECT count(*) AS n FROM ops.row_history WHERE table_name = 'ref.calendar_day'"
    ).fetchone()
    assert hist is not None and hist["n"] >= len(observed)  # retired rows stay as versions
    check = out.result["calendar"]["ruonia_crosscheck"]
    assert "2025-12-30" in check["business_days_without_ruonia"]
    assert check["ruonia_on_non_business_days"] == []

    scan = scan_dataset(conn, get_dataset("cbr_ruonia"), today=date(2026, 1, 20), calendar=dbcal)
    assert date(2025, 12, 30) in {g.day for g in scan.gaps}


# ------------------------------------------- foreign settlement calendars (contract 1.5, H4)


def test_us_fed_and_zurich_rules() -> None:
    fed = {
        2026: [(1, 1), (1, 19), (2, 16), (5, 25), (6, 19), (9, 7), (10, 12), (11, 11), (11, 26),
               (12, 25)],  # Jul 4 is a Saturday: the Reserve Banks open on Friday Jul 3
        2022: [(1, 17), (2, 21), (5, 30), (6, 20), (7, 4), (9, 5), (10, 10), (11, 11), (11, 24),
               (12, 26)],  # Jan 1 Saturday: no holiday; Juneteenth / Christmas Sunday -> Monday
    }  # fmt: skip
    for year in (2026, 2022):
        assert cal.us_fed_closing_days(year) == {date(year, m, d) for m, d in fed[year]}
    assert date(2020, 7, 3) not in cal.us_fed_closing_days(2020)  # Saturday rule, not Friday
    assert date(2020, 6, 19) not in cal.us_fed_closing_days(2020)  # Juneteenth from 2021
    zurich = cal.ch_zurich_closing_days(2026)
    assert {date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 14), date(2026, 5, 25)} <= zurich
    assert {date(2026, 1, 2), date(2026, 5, 1), date(2026, 8, 1), date(2026, 12, 26)} <= zurich
    days = cal.rule_days(cal.ch_zurich_closing_days, date(2026, 7, 31), date(2026, 8, 3))
    assert days == {
        date(2026, 7, 31): True,
        date(2026, 8, 1): False,  # Saturday anyway: no substitute day
        date(2026, 8, 2): False,
        date(2026, 8, 3): True,
    }


def test_cn_interbank_curated_years() -> None:
    years = {y.year: y for y in cal.load_curated().years if y.calendar == "CN_INTERBANK"}
    assert set(years) == {2024, 2025, 2026}
    assert {y: sum(c.days().values()) for y, c in years.items()} == {
        2024: 251,
        2025: 248,
        2026: 248,
    }  # the official numbers of working days
    cn26 = years[2026].days()
    # the holidays the MOEX CETS CNY/RUB TOD data shows (core golden 2026-06..10)
    for d in (date(2026, 6, 19), date(2026, 9, 25), *(date(2026, 10, n) for n in (1, 2, 5, 6, 7))):
        assert cn26[d] is False
    assert cn26[date(2026, 10, 10)] is True and cn26[date(2026, 9, 20)] is True  # adjusted
    assert cn26[date(2026, 10, 8)] is True


def test_refresh_writes_foreign_settlement_calendars(
    iss: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 4, 15))
    summary = cal.seed_calendar(settings, db, http_factory=_http_factory)
    assert summary["rule_us_fed"]["written"] > 0 and summary["rule_ch_zurich"]["written"] > 0
    rows = conn.execute(
        "SELECT calendar, source, min(day) AS lo, max(day) AS hi FROM ref.calendar_day "
        "WHERE calendar IN ('US_FED', 'CH_ZURICH', 'CN_INTERBANK') GROUP BY 1, 2 ORDER BY 1"
    ).fetchall()
    assert rows == [
        {"calendar": "CH_ZURICH", "source": "rule_ch_zurich", "lo": date(2002, 1, 1),
         "hi": cal.RULE_END},
        {"calendar": "CN_INTERBANK", "source": "curated", "lo": date(2024, 1, 1),
         "hi": date(2026, 12, 31)},
        {"calendar": "US_FED", "source": "rule_us_fed", "lo": date(2002, 1, 1),
         "hi": cal.RULE_END},
    ]  # fmt: skip
    bc = DbCalendar(db, ttl=0)
    assert bc(date(2026, 11, 26), "US_FED") is False  # Thanksgiving
    assert bc(date(2026, 5, 14), "CH_ZURICH") is False  # Ascension
    assert bc(date(2026, 10, 1), "CN_INTERBANK") is False
    assert bc(date(2026, 10, 10), "CN_INTERBANK") is True  # adjusted working Saturday
