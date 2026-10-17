"""cbr_daily / cbr_meetings against a real PostgreSQL with recorded cbr.ru responses."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.cbr as loader
from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobOutcome, run_job
from mdhub.sources import cbr

FIX = Path(__file__).parent / "fixtures" / "official"
Conn = psycopg.Connection[DictRow]
HOST = "www.cbr.ru"


FAST = {"www.cbr.ru": 1e6, "iss.moex.com": 1e6, "www.ecb.europa.eu": 1e6, "ratescalcs.ru": 1e6}


def _http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits=FAST,
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def _run(
    job: str,
    params: dict[str, Any],
    settings: Settings,
    db: Database,
    calendar: CalendarPredicate | None = None,
) -> JobOutcome:
    kw: dict[str, Any] = {"calendar": calendar} if calendar is not None else {}
    return run_job(job, params, settings=settings, db=db, http_factory=_http_factory, **kw)


def _page(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _html(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})


def _xml_daily(request: httpx.Request) -> httpx.Response:
    """Rate in force on date_req: the Saturday record 10.10 up to Monday 12.10; from 13.10 a
    record per day (16.10 values, Date rewritten to the requested day)."""
    req = datetime.strptime(request.url.params["date_req"], "%d/%m/%Y").date()
    if req <= date(2026, 10, 12):
        body = _page("cbr_xml_daily_20261012.xml")
    else:
        day = min(req, date(2026, 10, 16)).strftime("%d.%m.%Y").encode()
        body = _page("cbr_xml_daily_20261016.xml").replace(
            b'ValCurs Date="16.10.2026"', b'ValCurs Date="' + day + b'"'
        )
    return httpx.Response(200, content=body, headers={"content-type": "application/xml"})


def _xml_dynamic(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=_page("cbr_xml_dynamic_usd.xml"), headers={"content-type": "application/xml"}
    )


class Pages:
    """Mutable page bodies served by the mock (tests swap them to simulate revisions)."""

    def __init__(self) -> None:
        self.body = {
            cbr.KEY_RATE_PATH: _page("cbr_keyrate.html"),
            cbr.RUONIA_PATH: _page("cbr_ruonia.html"),
            cbr.STANDING_RATES_PATH: _page("cbr_standing_rates.html"),
            cbr.LIQUIDITY_PATH: _page("cbr_bliquidity.html"),
            cbr.MEETINGS_PATH: _page("cbr_cal_mp.html"),
            cbr.REFINANCING_PATH: _page("cbr_refinancing_rates.html"),
        }

    def handler(self, path: str) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _r: _html(self.body[path])


@pytest.fixture
def pages() -> Iterator[Pages]:
    p = Pages()
    with respx.mock(assert_all_called=False) as router:
        for path in p.body:
            router.get(host=HOST, path=path).mock(side_effect=p.handler(path))
        router.get(host=HOST, path=cbr.RUONIA_INDEX_XLSX_PATH).mock(
            return_value=httpx.Response(200, content=_page("cbr_ruonia_index.xlsx"))
        )
        router.get(host=HOST, path=cbr.XML_DAILY_PATH).mock(side_effect=_xml_daily)
        router.get(host=HOST, path=cbr.XML_DYNAMIC_PATH).mock(side_effect=_xml_dynamic)
        yield p


RANGE = {"date_from": "2026-10-12", "date_to": "2026-10-16"}


@pytest.fixture(autouse=True)
def _recorded_today(today_is: Callable[[date], None]) -> None:
    """The responses were recorded on 2026-10-16 (FX tails depend on "today")."""
    today_is(date(2026, 10, 16))


def _counts(conn: Conn, run_id: int | None) -> dict[str, Any]:
    row = conn.execute(
        "SELECT status, rows_inserted, rows_updated, error FROM ops.job_run WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def test_cbr_daily_range_loads_all_parts(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    out = _run("cbr_daily", RANGE, settings, db)
    assert out.status == "succeeded", _counts(conn, out.run_id)
    fix = {
        (r["index_code"], r["fixing_date"]): r
        for r in conn.execute("SELECT * FROM md.fixing").fetchall()
    }
    kr = fix[("KEY_RATE", date(2026, 10, 16))]
    assert kr["value"] == Decimal("14.00") and kr["effective_date"] == date(2026, 10, 16)
    ru = fix[("RUONIA", date(2026, 10, 14))]
    assert ru["published_on"] == date(2026, 10, 15) and ru["value"] == Decimal("13.91")
    assert ru["details"]["volume_bn"] == "618.80" and ru["details"]["status"] == "Стандартный"
    assert fix[("RUONIA_INDEX", date(2026, 10, 11))]["unit"] == "index"  # calendar days
    assert fix[("TERM_RUONIA_6M", date(2026, 10, 15))]["value"] == Decimal("14.59403269644100")
    assert fix[("CBR_DEPOSIT_STANDING", date(2026, 10, 16))]["value"] == Decimal("13.00")
    assert all(r["capture_id"] is not None and r["ingested_at"] for r in fix.values())

    liq = conn.execute("SELECT * FROM md.cbr_liquidity ORDER BY observed_on").fetchall()
    assert liq[-1]["observed_on"] == date(2026, 10, 16) and liq[-1]["revision_no"] == 1
    assert liq[-1]["methodology_version"] == "cbr_bliquidity_v2023_11"


def test_fx_official_effective_dates_and_nominal(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    _run("cbr_daily", RANGE, settings, db)
    rows = conn.execute(
        "SELECT * FROM md.fx_official WHERE ccy = 'DZD' ORDER BY effective_date"
    ).fetchall()
    assert [r["effective_date"] for r in rows] == [date(2026, 10, d) for d in (12, 13, 14, 15, 16)]
    mon = rows[0]  # Monday 12.10: Saturday record 10.10 set on Friday 09.10
    assert mon["record_date"] == date(2026, 10, 10) and mon["set_date"] == date(2026, 10, 9)
    assert mon["nominal"] == 100
    assert mon["rate_per_unit"] == mon["value"] / 100
    assert [r["record_date"] for r in rows[1:]] == [date(2026, 10, d) for d in (13, 14, 15, 16)]
    fri = rows[-1]
    assert fri["record_date"] == date(2026, 10, 16) and fri["value"] == Decimal("62.3078")
    assert fri["rate_per_unit"] == Decimal("0.623078")
    n = conn.execute("SELECT count(DISTINCT ccy) AS n FROM md.fx_official").fetchone()
    assert n is not None and n["n"] >= 50


def test_cbr_daily_idempotent(pages: Pages, settings: Settings, db: Database, conn: Conn) -> None:
    _run("cbr_daily", RANGE, settings, db)
    before = conn.execute("SELECT count(*) AS n FROM md.fixing").fetchone()
    second = _run("cbr_daily", RANGE, settings, db)
    c = _counts(conn, second.run_id)
    assert c["status"] == "succeeded"
    assert c["rows_inserted"] == 0 and c["rows_updated"] == 0
    assert conn.execute("SELECT count(*) AS n FROM md.fixing").fetchone() == before
    hist = conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone()
    assert hist is not None and hist["n"] == 0


def test_key_rate_revision_goes_to_history(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    _run("cbr_daily", RANGE, settings, db)
    old = conn.execute(
        "SELECT * FROM md.fixing WHERE index_code='KEY_RATE' AND fixing_date='2026-10-16'"
    ).fetchone()
    assert old is not None
    body = pages.body[cbr.KEY_RATE_PATH].decode()
    pages.body[cbr.KEY_RATE_PATH] = body.replace(
        "<td>16.10.2026</td>\r\n        <td>14,00</td>", "<td>16.10.2026</td><td>13,50</td>"
    ).encode()
    assert pages.body[cbr.KEY_RATE_PATH].decode() != body, "fixture layout changed"
    out = _run("cbr_daily", RANGE, settings, db)
    assert _counts(conn, out.run_id)["rows_updated"] == 1
    new = conn.execute(
        "SELECT * FROM md.fixing WHERE index_code='KEY_RATE' AND fixing_date='2026-10-16'"
    ).fetchone()
    assert new is not None and new["value"] == Decimal("13.50")
    assert new["ingested_at"] > old["ingested_at"]  # known-at of the corrected value
    hist = conn.execute("SELECT * FROM ops.row_history WHERE table_name = 'md.fixing'").fetchall()
    assert len(hist) == 1
    assert hist[0]["old_row"]["value"] == 14.00 and hist[0]["replaced_by_run"] == out.run_id


def test_liquidity_revision_increments(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    _run("cbr_daily", RANGE, settings, db)
    body = pages.body[cbr.LIQUIDITY_PATH].decode()
    # revise the 16.10 row consistently: reserves +0.1 and deficit (2 = ... + 15) +0.1
    body = body.replace("<td>3 732,7</td>", "<td>3 732,8</td>", 1)
    body = body.replace("<td>5 463,8</td>", "<td>5 463,9</td>", 1)
    pages.body[cbr.LIQUIDITY_PATH] = body.encode()
    _run("cbr_daily", RANGE, settings, db)
    row = conn.execute("SELECT * FROM md.cbr_liquidity WHERE observed_on = '2026-10-16'").fetchone()
    assert row is not None and row["revision_no"] == 2
    assert row["deficit_bn"] == Decimal("3732.8")
    hist = conn.execute(
        "SELECT old_row FROM ops.row_history WHERE table_name = 'md.cbr_liquidity'"
    ).fetchall()
    assert len(hist) == 1 and hist[0]["old_row"]["revision_no"] == 1


def test_part_failure_marks_partial(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    pages.body[cbr.RUONIA_PATH] = b"<html><table class='data'><tr><th>x</th></tr></table></html>"
    out = _run("cbr_daily", RANGE, settings, db)
    c = _counts(conn, out.run_id)
    assert c["status"] == "partial" and "ruonia" in c["error"]
    n = conn.execute("SELECT count(*) AS n FROM md.fixing WHERE index_code='KEY_RATE'").fetchone()
    assert n is not None and n["n"] > 0  # other parts committed


def test_fx_window_mode(
    pages: Pages, settings: Settings, db: Database, conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute(
        "INSERT INTO md.fx_official (effective_date, ccy, nominal, value, rate_per_unit,"
        " record_date, set_date, cbr_id) VALUES ('2020-01-01','USD',1,1,1,'2020-01-01',"
        "'2019-12-31','R01235')"
    )
    conn.commit()
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 15))
    _run("cbr_daily", {}, settings, db)
    rows = conn.execute(
        "SELECT effective_date, record_date FROM md.fx_official WHERE ccy = 'USD'"
        " AND effective_date > '2020-01-01' ORDER BY effective_date"
    ).fetchall()
    # latest record 16.10 (set on 15.10) + the previous record 15.10
    assert [r["effective_date"] for r in rows] == [date(2026, 10, 15), date(2026, 10, 16)]
    assert rows[-1]["record_date"] == date(2026, 10, 16)


def _obs(rd: date, ccy: str = "USD", value: str = "80") -> loader.FxObservation:
    return loader.FxObservation(rd, cbr.FxRate("R01235", ccy, 1, Decimal(value), None), 1)


def test_expand_fx_tail_uses_business_calendar() -> None:
    now = datetime(2026, 1, 1)
    # Saturday record with no successor: in force Sat, Sun and Monday (next setting day)
    rows = loader.expand_fx_records(
        [_obs(date(2026, 10, 10))], lambda d: d.weekday() < 5, known_at=now, run_id=1
    )
    assert [r["effective_date"] for r in rows] == [date(2026, 10, d) for d in (10, 11, 12)]
    # New-Year record: in force through the RU holidays until the first business day
    holidays = {date(2025, 12, 31)} | {date(2026, 1, d) for d in range(1, 12)}
    rows = loader.expand_fx_records(
        [_obs(date(2025, 12, 31))],
        lambda d: d.weekday() < 5 and d not in holidays,
        known_at=now,
        run_id=1,
    )
    assert rows[-1]["effective_date"] == date(2026, 1, 12) and len(rows) == 13
    # successor known: span ends the day before it; lower bound respected
    rows = loader.expand_fx_records(
        [_obs(date(2026, 10, 3)), _obs(date(2026, 10, 6), value="81")],
        lambda d: d.weekday() < 5,
        lower=date(2026, 10, 4),
        known_at=now,
        run_id=1,
    )
    assert [(r["effective_date"].day, r["value"]) for r in rows] == [
        (4, Decimal(80)),
        (5, Decimal(80)),
        (6, Decimal(81)),
    ]


def _seed_meetings(conn: Conn) -> None:
    conn.execute(
        """INSERT INTO ref.cbr_meeting (meeting_date, effective_date, is_estimated)
           VALUES ('2022-02-28', '2022-03-01', true), ('2026-09-11', '2026-09-14', true),
                  ('2026-10-23', '2026-10-26', true)"""
    )
    rates = [("2022-02-25", "9.50"), ("2022-02-28", "20.00"), ("2022-03-01", "20.00")]
    rates += [("2026-09-10", "14.50"), ("2026-09-11", "14.50"), ("2026-09-14", "14.00")]
    for d, v in rates:
        conn.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
            "VALUES ('KEY_RATE', %s, 'cbr', %s)",
            (d, v),
        )
    conn.commit()


def test_resolve_meetings(
    settings: Settings, db: Database, conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_meetings(conn)
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    from mdhub.jobs import REGISTRY, JobContext

    assert "cbr_meetings" in REGISTRY
    with db.connection() as c:
        ctx = JobContext(
            settings=settings,
            db=db,
            run=None,  # type: ignore[arg-type]
            job="t",
            params={},
            logger=loader.log,
        )
        changed = loader.resolve_meetings(c, ctx)
    assert changed == 2
    rows = {r["meeting_date"]: r for r in conn.execute("SELECT * FROM ref.cbr_meeting").fetchall()}
    feb = rows[date(2022, 2, 28)]  # extraordinary: effective the same day
    assert feb["effective_date"] == date(2022, 2, 28) and feb["decision"] == "hike"
    assert feb["is_estimated"] is False and feb["prev_rate_pct"] == Decimal("9.50")
    sep = rows[date(2026, 9, 11)]
    assert sep["effective_date"] == date(2026, 9, 14) and sep["decision"] == "cut"
    assert sep["decision_rate_pct"] == Decimal("14.00")
    assert rows[date(2026, 10, 23)]["is_estimated"] is True  # future meeting untouched


def test_post_meeting_skips_without_meeting(
    settings: Settings, db: Database, conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    out = _run("cbr_meetings", {"mode": "post_meeting"}, settings, db)
    assert out.status == "skipped"


def test_post_meeting_resolves(
    pages: Pages, settings: Settings, db: Database, conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute(
        "INSERT INTO ref.cbr_meeting (meeting_date, effective_date, is_estimated)"
        " VALUES ('2026-10-15', '2026-10-16', true)"
    )
    conn.commit()
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    out = _run("cbr_meetings", {"mode": "post_meeting"}, settings, db)
    assert out.status == "succeeded"
    row = conn.execute("SELECT * FROM ref.cbr_meeting").fetchone()
    assert row is not None and row["decision"] == "hold" and row["is_estimated"] is False


def test_datasets_registered() -> None:
    from mdhub.datasets import get_dataset

    fx = get_dataset("cbr_fx_official")
    assert fx.frequency == "daily" and fx.backfill_job == "cbr_daily"
    assert get_dataset("cbr_ruonia").calendar == "RU_PRODUCTION"
    assert get_dataset("cbr_key_rate").history_start == date(2013, 9, 17)


def test_expand_fx_known_through_covers_unscheduled_holidays() -> None:
    # 2020-03-28 record, next one only on 2020-04-07 (non-working days by decree): a chunk
    # ending 2020-04-02 must still cover 03-31/04-01 although the calendar says weekdays
    rows = loader.expand_fx_records(
        [
            _obs(date(2020, 3, 28)),
            _obs(date(2020, 3, 28), ccy="EUR"),
            _obs(date(2020, 3, 27), "XDR"),
        ],
        lambda d: d.weekday() < 5,
        known_through=date(2020, 4, 2),
        known_at=datetime(2026, 1, 1),
        run_id=1,
    )
    usd = [r["effective_date"] for r in rows if r["ccy"] == "USD"]
    assert usd == [date(2020, 3, d) for d in range(28, 32)] + [date(2020, 4, 1), date(2020, 4, 2)]
    # a currency missing from the latest record is not extended (dropped from the daily set)
    assert [r["effective_date"] for r in rows if r["ccy"] == "XDR"] == [date(2020, 3, 27)]


def test_refinancing_rate_seeded_once_and_by_range(
    pages: Pages, settings: Settings, db: Database, conn: Conn
) -> None:
    def refi(where: str = "") -> list[DictRow]:
        return conn.execute(
            "SELECT * FROM md.fixing WHERE index_code = 'CBR_REFINANCING_RATE' "
            + where
            + " ORDER BY fixing_date"
        ).fetchall()

    def captures() -> int:
        row = conn.execute(
            "SELECT count(*) AS n FROM ops.raw_capture WHERE endpoint = 'refinancing_rate'"
        ).fetchone()
        assert row is not None
        return int(row["n"])

    _run("cbr_daily", RANGE, settings, db)  # explicit range after 2015: not read
    assert refi() == [] and captures() == 0
    out = _run("cbr_daily", {}, settings, db)  # scheduled run, series empty: seeded
    assert _counts(conn, out.run_id)["status"] in ("succeeded", "partial")
    rows = refi()
    assert len(rows) == (date(2016, 1, 1) - date(1992, 1, 1)).days
    by = {r["fixing_date"]: r for r in rows}
    assert by[date(2009, 8, 27)]["value"] == Decimal("10.75")  # ДОМ.РФ 15: coupon 1 = +2.5
    assert by[date(2013, 9, 15)]["value"] == Decimal("8.25")  # not the key rate (7.00)
    assert by[date(2015, 12, 31)]["details"]["period_from"] == "2012-09-14"
    _run("cbr_daily", {}, settings, db)  # stored: not read again
    assert captures() == 1
    _run("cbr_daily", {"date_from": "2009-08-01", "date_to": "2009-08-31"}, settings, db)
    assert captures() == 2 and len(refi()) == len(rows)


def test_refinancing_dataset_is_closed() -> None:
    from mdhub.datasets import get_dataset

    spec = get_dataset("cbr_refinancing_rate")
    assert spec.history_end == date(2015, 12, 31) and spec.backfill_job == "cbr_daily"


# ------------------------------------------------- FX ranges: per-currency XML_dynamic


class FxWorld:
    """Synthetic CBR FX history 2025-01-09..2025-03-31 (record dates = weekdays).

    USD always in the daily set; ISK leaves it after 2025-02-14; CNY joins on 2025-01-20 and
    before that only has a monthly-set rate on 2025-01-09 (XML_dynamic lists it, XML_daily
    does not); XML_dynamic of KZT also returns records of the related id R01335Z."""

    IDS: ClassVar[dict[str, tuple[str, int]]] = {
        "R01235": ("USD", 1),
        "R01350": ("ISK", 100),
        "R01375": ("CNY", 1),
        "R01335": ("KZT", 100),
    }

    def __init__(self) -> None:
        start = date(2025, 1, 9)
        self.dates = [
            start + timedelta(days=i)
            for i in range(82)
            if (start + timedelta(days=i)).weekday() < 5
        ]
        self.daily_calls: list[date] = []
        self.tamper: tuple[str, date] | None = None

    def value(self, cbr_id: str, d: date) -> Decimal:
        base = {"R01235": 90, "R01350": 65, "R01375": 12, "R01335": 18}[cbr_id]
        return Decimal(base) + Decimal(self.dates.index(d) if d in self.dates else 0) / 100

    def members(self, d: date) -> list[str]:
        out = ["R01235", "R01335"]
        if d <= date(2025, 2, 14):
            out.append("R01350")
        if d >= date(2025, 1, 20):
            out.append("R01375")
        return out

    @staticmethod
    def _num(v: Decimal) -> str:
        return f"{v:.4f}".replace(".", ",")

    def xml_daily(self, request: httpx.Request) -> httpx.Response:
        req = datetime.strptime(request.url.params["date_req"], "%d/%m/%Y").date()
        rd = max(d for d in self.dates if d <= req)
        self.daily_calls.append(rd)
        valutes = "".join(
            f'<Valute ID="{i}"><CharCode>{self.IDS[i][0]}</CharCode>'
            f"<Nominal>{self.IDS[i][1]}</Nominal><Value>{self._num(self.value(i, rd))}</Value>"
            "</Valute>"
            for i in self.members(rd)
        )
        body = f'<ValCurs Date="{rd:%d.%m.%Y}" name="Foreign Currency Market">{valutes}</ValCurs>'
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/xml"})

    def xml_dynamic(self, request: httpx.Request) -> httpx.Response:
        cbr_id = request.url.params["VAL_NM_RQ"]
        d1 = datetime.strptime(request.url.params["date_req1"], "%d/%m/%Y").date()
        d2 = datetime.strptime(request.url.params["date_req2"], "%d/%m/%Y").date()
        nominal = self.IDS[cbr_id][1]
        records = []
        for d in self.dates:
            if not d1 <= d <= d2:
                continue
            monthly_cny = cbr_id == "R01375" and d == date(2025, 1, 9)
            if cbr_id not in self.members(d) and not monthly_cny:
                continue
            v = self.value(cbr_id, d) + (1 if self.tamper == (cbr_id, d) else 0)
            records.append(
                f'<Record Date="{d:%d.%m.%Y}" Id="{cbr_id}"><Nominal>{nominal}</Nominal>'
                f"<Value>{self._num(v)}</Value></Record>"
            )
            if cbr_id == "R01335":
                records.append(
                    f'<Record Date="{d:%d.%m.%Y}" Id="R01335Z"><Nominal>1</Nominal>'
                    "<Value>1,0000</Value></Record>"
                )
        body = f'<ValCurs ID="{cbr_id}">{"".join(records)}</ValCurs>'
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/xml"})


@pytest.fixture
def fx_world(monkeypatch: pytest.MonkeyPatch) -> Iterator[FxWorld]:
    world = FxWorld()
    monkeypatch.setattr(loader, "PARTS", tuple(p for p in loader.PARTS if p[0] == "fx_official"))
    with respx.mock(assert_all_called=False) as router:
        router.get(host=HOST, path=cbr.XML_DAILY_PATH).mock(side_effect=world.xml_daily)
        router.get(host=HOST, path=cbr.XML_DYNAMIC_PATH).mock(side_effect=world.xml_dynamic)
        yield world


FX_RANGE = {"date_from": "2025-01-10", "date_to": "2025-03-28"}


def test_fx_range_per_currency_matches_daily_set(
    fx_world: FxWorld, settings: Settings, db: Database, conn: Conn
) -> None:
    out = _run("cbr_daily", FX_RANGE, settings, db)
    assert _counts(conn, out.run_id)["status"] == "succeeded"
    assert len(set(fx_world.daily_calls)) < len(fx_world.dates) // 2  # samples, not every day

    def rows(ccy: str) -> list[DictRow]:
        return conn.execute(
            "SELECT * FROM md.fx_official WHERE ccy = %s ORDER BY effective_date", (ccy,)
        ).fetchall()

    usd = rows("USD")
    assert usd[0]["effective_date"] == date(2025, 1, 10)
    assert {r["effective_date"] for r in usd} >= {date(2025, 1, 10), date(2025, 3, 28)}
    for r in usd:  # every day carries the record in force, values as published
        assert r["value"] == fx_world.value("R01235", r["record_date"])
    isk = rows("ISK")  # leaves after Friday 2025-02-14: in force through Sunday 16th only
    assert isk[-1]["record_date"] == date(2025, 2, 14)
    assert isk[-1]["effective_date"] == date(2025, 2, 16)
    assert isk[-1]["nominal"] == 100
    cny = rows("CNY")  # the monthly-set 2025-01-09 rate is not in the daily set
    assert cny[0]["effective_date"] == date(2025, 1, 20)
    kzt = rows("KZT")  # R01335Z records are not mixed in
    assert {r["cbr_id"] for r in kzt} == {"R01335"} and all(r["nominal"] == 100 for r in kzt)


def test_fx_range_value_mismatch_fails_closed(
    fx_world: FxWorld, settings: Settings, db: Database, conn: Conn
) -> None:
    fx_world.tamper = ("R01235", date(2025, 2, 3))  # first record of February: a sample
    with pytest.raises(FormatDriftError, match=r"XML_dynamic 1/91\.1700 != XML_daily 1/90\.1700"):
        _run("cbr_daily", FX_RANGE, settings, db)
    run = conn.execute("SELECT status, error FROM ops.job_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run is not None and run["status"] == "failed"
    n = conn.execute("SELECT count(*) AS n FROM md.fx_official").fetchone()
    assert n is not None and n["n"] == 0


def test_expand_fx_stops_currency_at_next_daily_record() -> None:
    usd1, usd2 = _obs(date(2026, 10, 13)), _obs(date(2026, 10, 15))
    gone = _obs(date(2026, 10, 13), ccy="LTL", value="19")
    rows = loader.expand_fx_records(
        [usd1, usd2, gone],
        lambda d: d.weekday() < 5,
        known_at=datetime(2026, 10, 15),
        run_id=1,
    )
    ltl = sorted(r["effective_date"] for r in rows if r["ccy"] == "LTL")
    assert ltl == [date(2026, 10, 13), date(2026, 10, 14)]  # not carried past the 15.10 record
