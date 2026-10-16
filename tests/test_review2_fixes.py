"""Regression tests for the follow-up review (REVIEW2, findings N1-N9 and the lock nit).

The scratch repro tests of the review (``test_quote_ts.py``, ``test_lock_masking.py``,
``test_gaps_bookkeeping.py``) are converted here with the corrected expectations.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.cbr
import mdhub.loaders.futures
import mdhub.loaders.fx_moex  # noqa: F401 - registers fx_eod
from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate, DbCalendar, weekday_calendar
from mdhub.core.gaps import no_data_status, record_no_data
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.iss import APIM_BASE_URL, ISS_BASE_URL
from mdhub.core.timeutil import MSK, previous_session_day
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, run_job
from mdhub.loaders import calendar as cal
from mdhub.loaders import futures as fut
from mdhub.loaders import maintenance as mt
from mdhub.loaders.bonds_static import formula_secids, key_rate_change_dates
from mdhub.quality.checks import DatasetCheck, reconcile_gaps, scan_dataset, sync_gaps
from mdhub.quality.health import job_health
from mdhub.schedule import RULES, Rule, rule_applies
from mdhub.sources import cbr
from mdhub.sources import moex_apim as apim
from mdhub.sources import moex_fx as fx
from mdhub.sources import moex_static as ms
from mdhub.sources.moex_fx import snapshot_ts
from mdhub.sources.moex_market import _quote_ts

Conn = psycopg.Connection[DictRow]
FX_FIX = Path(__file__).parent / "fixtures" / "moex_fx"
OFFICIAL_FIX = Path(__file__).parent / "fixtures" / "official"
KEY = "test-apim-key"

# 2026-10-17/18 (Sat/Sun) are FORTS weekend sessions (ISS futures calendar, reason W)
SAT, SUN, MON = date(2026, 10, 17), date(2026, 10, 18), date(2026, 10, 19)
FRI = date(2026, 10, 16)


class SessionCalendar(CalendarPredicate):
    """Monday-Friday business days plus the given weekend-session days (all calendars)."""

    def __init__(self, sessions: set[date]) -> None:
        self.sessions = sessions

    def is_trading_day(self, day: date, calendar: str = "MOEX_STOCK") -> bool:
        return self(day, calendar) or day in self.sessions


def forts_trading(d: date) -> bool:
    return d.weekday() < 5 or d in (SAT, SUN)


def load(name: str, base: Path = FX_FIX) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((base / name).read_text("utf-8"))
    return data


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None),
        rate_limits={"iss.moex.com": 1e6, "apim.moex.com": 1e6, "www.cbr.ru": 1e6},
        sleep=lambda _s: None,
        max_retries=0,
        **kw,
    )


def count(conn: Conn, query: str, params: Any = None) -> int:
    conn.commit()
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(next(iter(row.values())))


@pytest.fixture
def apim_settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, iss_rps=1000.0, moex_api_key=KEY)  # type: ignore[arg-type]


def seed_ref(conn: Conn, last_seen: date = FRI) -> None:
    fut.refresh_futures_contracts(
        conn,
        fx.parse_futures_securities(load("rfud_securities.json")),
        capture_id=None,  # type: ignore[arg-type]
        run_id=None,  # type: ignore[arg-type]
        today=last_seen,
    )
    conn.execute("UPDATE ref.futures_contract SET last_seen = %s", (last_seen,))
    conn.commit()


# =========================================================================== N1


def test_monday_preopen_after_weekend_session_is_dated_sunday() -> None:
    """N1: the Monday 08:50 FORTS poll sees the Sunday session's last trade (18:59:58): it is
    dated Sunday (trading day incl. weekend sessions), not Friday's clearing break."""
    sys_ts = datetime(2026, 10, 19, 8, 50, 5, tzinfo=MSK)
    ts = snapshot_ts("18:59:58", sys_ts, is_trading_day=forts_trading)
    assert ts == datetime(2026, 10, 18, 18, 59, 58, tzinfo=MSK)
    # no weekend session known (CETS, or a weekend without a session): Friday as before
    assert snapshot_ts("18:59:58", sys_ts) == datetime(2026, 10, 16, 18, 59, 58, tzinfo=MSK)
    # an update before SYSTIME stays on SYSTIME's date
    early = snapshot_ts("08:49:00", sys_ts, is_trading_day=forts_trading)
    assert early == datetime(2026, 10, 19, 8, 49, tzinfo=MSK)
    # bond rule shares the helper
    bond = _quote_ts(datetime(2026, 10, 19, 9, 55, tzinfo=MSK), time_(18, 39), forts_trading)
    assert bond.date() == SUN
    assert previous_session_day(MON, forts_trading) == SUN
    assert previous_session_day(MON) == FRI


def time_(h: int, m: int) -> Any:
    from datetime import time as t

    return t(h, m)


def test_db_calendar_trading_day_includes_weekend_sessions(db: Database, conn: Conn) -> None:
    conn.execute(
        "INSERT INTO ref.weekend_session (calendar, day, source, trade_session_date, known_at) "
        "VALUES ('MOEX_FORTS', %s, 'moex_iss_calendar', %s, now())",
        (SUN, MON),
    )
    conn.commit()
    dbcal = DbCalendar(db, ttl=0)
    assert dbcal(SUN, "MOEX_FORTS") is False  # settlement calendar unchanged
    assert dbcal.is_trading_day(SUN, "MOEX_FORTS") is True
    assert dbcal.is_trading_day(SUN, "MOEX_FX") is False
    assert dbcal.is_trading_day(SAT, "MOEX_FORTS") is False
    assert dbcal.is_trading_day(FRI, "MOEX_FORTS") is True
    assert weekday_calendar.is_trading_day(SUN, "MOEX_FORTS") is False


@respx.mock
def test_futures_intraday_dates_weekend_session_quote_on_sunday(
    apim_settings: Settings, db: Database, conn: Conn
) -> None:
    """N1 end to end: futures_intraday uses the engine's trading days for the dating rule."""
    seed_ref(conn, last_seen=datetime.now(MSK).date())
    payload = load("forts_md_apim.json")
    cols = payload["marketdata"]["columns"]
    for data in payload["marketdata"]["data"]:
        data[cols.index("SYSTIME")] = "2026-10-19 08:50:05"
        data[cols.index("UPDATETIME")] = "18:59:58"
    respx.get(APIM_BASE_URL + fx.FORTS_SECURITIES).mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = run_job(
        "futures_intraday",
        {},
        settings=apim_settings,
        db=db,
        http_factory=http_factory,
        calendar=SessionCalendar({SAT, SUN}),
    )
    assert out.status == "succeeded"
    stamps = {r["ts"] for r in conn.execute("SELECT ts FROM md.futures_intraday").fetchall()}
    assert stamps == {datetime(2026, 10, 18, 18, 59, 58, tzinfo=MSK)}


def test_only_changed_logs_quote_time_regression(
    conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    from mdhub.loaders.fx_moex import only_changed

    conn.execute(
        "INSERT INTO md.futures_intraday (secid, source, source_variant, ts, bid, latency) "
        "VALUES ('SiZ6', 'moex_apim', 'apim_marketdata', %s, 1, 'realtime')",
        (datetime(2026, 10, 18, 18, 59, 58, tzinfo=MSK),),
    )
    row = {
        "secid": "SiZ6",
        "source": "moex_apim",
        "source_variant": "apim_marketdata",
        "ts": datetime(2026, 10, 16, 18, 59, 58, tzinfo=MSK),
        "bid": Decimal(2),
    }
    with caplog.at_level(logging.WARNING):
        keep, _ = only_changed(
            conn,
            "md.futures_intraday",
            [row],
            ["secid", "source", "source_variant"],
            ["bid"],
            logger=logging.getLogger("t"),
        )
    assert keep == [row]
    assert "older than the last stored ts" in caplog.text


# =========================================================================== N7


def test_trading_day_rules_follow_weekend_sessions() -> None:
    sessions = SessionCalendar({SUN})
    algo = RULES["algopack_fx_futures"][0]
    intraday = RULES["futures_intraday"][1]
    assert algo.days == "trading" and intraday.days == "trading"
    assert rule_applies(algo, SUN, sessions) and rule_applies(intraday, SUN, sessions)
    assert not rule_applies(algo, SAT, sessions)
    eod = RULES["futures_eod"][0]
    assert not rule_applies(eod, SUN, sessions)  # EOD history: settlement days only
    assert rule_applies(Rule(cron="0 0 * * *", days="trading"), FRI, weekday_calendar)


def _ctx(settings: Settings, db: Database, calendar: CalendarPredicate) -> JobContext:
    return JobContext(
        settings=settings,
        db=db,
        run=None,  # type: ignore[arg-type]
        job="t",
        params={},
        logger=logging.getLogger("t"),
        calendar=calendar,
    )


def test_session_span_start_covers_preceding_weekend_session(
    settings: Settings, db: Database
) -> None:
    ctx = _ctx(settings, db, SessionCalendar({SAT, SUN}))
    assert fut.session_span_start(ctx, MON) == SAT
    assert fut.session_span_start(ctx, SUN) == SAT
    assert fut.session_span_start(ctx, FRI) == FRI
    assert fut.session_span_start(_ctx(settings, db, weekday_calendar), MON) == MON


def _mock_algopack_day(seen: list[dict[str, str]]) -> None:
    ts_payload = load("algopack_tradestats_latest.json")
    ob_payload = load("algopack_obstats_latest.json")
    futoi = load("futoi_latest.json")

    def bars(payload: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append({"path": request.url.path, **dict(request.url.params)})
            secid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
            out = copy.deepcopy(payload)
            c = out["data"]["columns"]
            out["data"]["data"] = [r for r in out["data"]["data"] if r[c.index("secid")] == secid]
            out["data.cursor"]["data"] = [[0, len(out["data"]["data"]), 1000]]
            return httpx.Response(200, json=out)

        return handler

    def futoi_handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, **dict(request.url.params)})
        return httpx.Response(200, json=futoi)

    respx.get(url__regex=r".*/datashop/algopack/fo/tradestats/\w+\.json").mock(
        side_effect=bars(ts_payload)
    )
    respx.get(url__regex=r".*/datashop/algopack/fo/obstats/\w+\.json").mock(
        side_effect=bars(ob_payload)
    )
    respx.get(url__regex=r".*/analyticalproducts/futoi/securities/\w+\.json").mock(
        side_effect=futoi_handler
    )


@respx.mock
def test_algopack_monday_reread_includes_weekend_session(
    apim_settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    """N7: the Monday re-read spans the preceding weekend-session days (from=Saturday), and
    FUTOI is read per trading day."""
    today_is(MON)
    seed_ref(conn, last_seen=MON)
    seen: list[dict[str, str]] = []
    _mock_algopack_day(seen)
    out = run_job(
        "algopack_fx_futures",
        {"full": "true"},
        settings=apim_settings,
        db=db,
        http_factory=http_factory,
        calendar=SessionCalendar({SAT, SUN}),
    )
    assert out.status in ("succeeded", "partial") and out.result["mode"] == "reread"
    bar_spans = {(p["from"], p["till"]) for p in seen if "/datashop/" in p["path"]}
    assert bar_spans == {(SAT.isoformat(), MON.isoformat())}
    futoi_days = {p["from"] for p in seen if "/futoi/" in p["path"]}
    assert futoi_days == {SAT.isoformat(), SUN.isoformat(), MON.isoformat()}


# =========================================================================== N2


def _rates(values: list[str], pays: list[date]) -> tuple[ms.CouponRate, ...]:
    out = []
    prev = pays[0] - timedelta(days=182)
    for v, p in zip(values, pays, strict=True):
        out.append(ms.CouponRate(prev, p, Decimal(v)))
        prev = p
    return tuple(out)


PAYS = [date(2025, 4, 1), date(2025, 10, 1), date(2026, 4, 1), date(2026, 10, 1), date(2027, 4, 1)]
TODAY = date(2026, 10, 16)


def test_schedule_constant_rule() -> None:
    const = _rates(["6.25"] * 5, PAYS)
    change_inside = (date(2025, 7, 28),)  # between first and last paid period start
    assert ms.schedule_constant_fixed(const, TODAY, change_inside)
    # floater on an unchanged key rate: no change during the paid periods -> not fixed
    assert not ms.schedule_constant_fixed(const, TODAY, ())
    assert not ms.schedule_constant_fixed(const, TODAY, (date(2024, 1, 1), date(2026, 10, 10)))
    # a change inside the last paid period only does not count (fixing lag)
    assert not ms.schedule_constant_fixed(const, TODAY, (date(2026, 6, 1),))
    # varying rates (forward-filled floater) -> not fixed
    varying = _rates(["16", "18", "21", "21", "21"], PAYS)
    assert not ms.schedule_constant_fixed(varying, TODAY, change_inside)
    # fewer than 2 paid coupons
    assert not ms.schedule_constant_fixed(const, date(2025, 5, 1), change_inside)
    # a missing rate anywhere
    gap = (*const[:4], ms.CouponRate(PAYS[3], PAYS[4], None))
    assert not ms.schedule_constant_fixed(gap, TODAY, change_inside)


def test_classify_unlabelled_active_bond_schedule_constant() -> None:
    base = ms.ClassifyInput(
        secid="RU000A102CT6",
        sec_type="subfederal_bond",
        has_description=True,
        coupon_values=(True,) * 5,
        coupon_rates=_rates(["6.25"] * 5, PAYS),
        key_rate_changes=(date(2025, 7, 28),),
        today=TODAY,
    )
    assert ms.classify_coupon_type(base, "subfederal") == ("fixed", "schedule_constant")
    with_formula = ms.ClassifyInput(**{**_asdict(base), "has_formula": True})
    assert ms.classify_coupon_type(with_formula, "subfederal") == ("unknown", "schedule")
    no_change = ms.ClassifyInput(**{**_asdict(base), "key_rate_changes": ()})
    assert ms.classify_coupon_type(no_change, "subfederal") == ("unknown", "schedule")
    bench = ms.ClassifyInput(**{**_asdict(base), "coupon_benchmark": "KEYRATE"})
    assert ms.classify_coupon_type(bench, "subfederal")[0] == "floating"
    assert ms.classify_coupon_type(base, "structured") == ("unknown", "schedule")
    matured = ms.ClassifyInput(**{**_asdict(base), "is_matured": True})
    assert ms.classify_coupon_type(matured, "subfederal") == ("fixed", "schedule")
    # future coupons of a schedule_constant bond are known
    row = ms.CouponRow(
        secid="X",
        isin=None,
        name=None,
        pay_date=date(2027, 4, 1),
        start_date=date(2026, 10, 1),
        record_date=None,
        value=Decimal("31.16"),
        value_prc=Decimal("6.25"),
        value_rub=None,
        face_value=None,
        initial_face_value=None,
        face_unit="RUB",
        issue_value=None,
        capture_id=None,
    )
    status = ms.coupon_status(
        row, coupon_type="fixed", benchmark=None, is_current=False, face_indexed=False, today=TODAY
    )
    assert status == "known"


def _asdict(inp: ms.ClassifyInput) -> dict[str, Any]:
    return {f: getattr(inp, f) for f in inp.__dataclass_fields__}


def test_key_rate_changes_and_formula_secids(conn: Conn) -> None:
    for d, v in (("2025-06-01", 21), ("2025-06-09", 20), ("2025-06-10", 20), ("2025-07-28", 18)):
        conn.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value) "
            "VALUES ('KEY_RATE', %s, 'cbr', %s)",
            (d, v),
        )
    conn.execute(
        "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) "
        "VALUES ('RU000AAA', 'subfederal', 'fixed', 'schedule_constant', 'active')"
    )
    conn.execute(
        "INSERT INTO ref.bond_rate_formula (secid, source, benchmark, status) "
        "VALUES ('RU000AAA', 'curated', 'KEY_RATE', 'curated')"
    )
    assert key_rate_change_dates(conn) == (date(2025, 6, 9), date(2025, 7, 28))
    assert formula_secids(conn) == frozenset({"RU000AAA"})
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) "
            "VALUES ('X2', 'corporate', 'fixed', 'bogus', 'active')"
        )


# =========================================================================== N3


@pytest.fixture
def fake_job() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def loader(
        ctx: JobContext,
        *,
        mode: str = "final",
        days_back: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        calls.append({"mode": mode, "date_from": date_from, "date_to": date_to})

    REGISTRY.register("rv2_locked", "fake", loader)
    try:
        yield calls
    finally:
        REGISTRY.unregister("rv2_locked")


def _health(conn: Conn, rule: Rule, now: datetime) -> Any:
    return {
        x.job: x
        for x in job_health(
            conn,
            now=now,
            calendar=weekday_calendar,
            tz=MSK,
            rules={"rv2_locked": (rule,)},
            known=["rv2_locked"],
        )
    }["rv2_locked"]


def test_backfill_run_after_lock_busy_skip_does_not_hide_missed_fire(
    db_url: str, db: Database, settings: Settings, conn: Conn, fake_job: list[dict[str, Any]]
) -> None:
    """N3: a skipped (lock_busy) scheduled fire followed by a backfill run of the same job is
    still missed and caught up."""
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:rv2_locked', 0))")
    try:
        skipped = run_job(
            "rv2_locked",
            {"mode": "final"},
            settings=settings,
            db=db,
            lock_wait_s=0.2,
            lock_poll_s=0.05,
            trigger="schedule",
        )
    finally:
        holder.close()
    assert skipped.status == "skipped"
    bf = run_job(
        "rv2_locked",
        {"date_from": "2025-01-06", "date_to": "2025-01-10"},
        settings=settings,
        db=db,
        yield_to_waiters=True,
        trigger="backfill",
    )
    assert bf.status == "succeeded"
    now = datetime.now(MSK)
    fire = now - timedelta(minutes=50)
    rule = Rule(
        cron=f"{fire.minute} {fire.hour} * * *",
        params={"mode": "final", "days_back": [1, 2]},
        days="all",
    )
    for run_id, minutes in ((skipped.run_id, 15), (bf.run_id, 16)):
        conn.execute(
            "UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s",
            (fire + timedelta(minutes=minutes), fire + timedelta(minutes=minutes), run_id),
        )
    conn.commit()
    h = _health(conn, rule, now)
    assert h.status == "missed" and h.missed_by_lock and h.problem

    from mdhub.quality.job import catch_up_missed

    class FakeRun:
        id = 0

        def mark_partial(self, reason: str) -> None:  # pragma: no cover
            raise AssertionError(reason)

    ctx = JobContext(
        settings=settings,
        db=db,
        run=FakeRun(),  # type: ignore[arg-type]
        job="quality",
        params={},
        logger=logging.getLogger("t"),
    )
    done = catch_up_missed(ctx, [h], lock_wait_s=1)
    assert done[0]["status"] == "succeeded"
    trig = conn.execute(
        "SELECT trigger FROM ops.job_run WHERE id = %s", (done[0]["run_id"],)
    ).fetchone()
    assert trig == {"trigger": "catch_up"}
    # the catch-up run counts as the fire: healthy again
    assert _health(conn, rule, datetime.now(MSK)).status == "ok"


def test_backfill_or_cli_run_does_not_count_as_scheduled_fire(
    db: Database, settings: Settings, conn: Conn, fake_job: list[dict[str, Any]]
) -> None:
    """N3: scheduler down at the fire time - a later backfill/CLI run of the job does not make
    it look healthy (missed, not by lock: nothing to catch up automatically)."""
    now = datetime.now(MSK)
    fire = now - timedelta(minutes=50)
    rule = Rule(cron=f"{fire.minute} {fire.hour} * * *", params={"mode": "final"}, days="all")
    for trig in ("backfill", "cli"):
        out = run_job("rv2_locked", {}, settings=settings, db=db, trigger=trig)
        conn.execute(
            "UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s",
            (fire + timedelta(minutes=5), fire + timedelta(minutes=6), out.run_id),
        )
    conn.commit()
    h = _health(conn, rule, now)
    assert h.status == "missed" and not h.missed_by_lock
    sched = run_job("rv2_locked", {}, settings=settings, db=db, trigger="schedule")
    conn.execute(
        "UPDATE ops.job_run SET started_at = %s, finished_at = %s WHERE id = %s",
        (fire, fire, sched.run_id),
    )
    conn.commit()
    assert _health(conn, rule, now).status == "ok"
    # rows of the previous release (trigger NULL) still count as before
    conn.execute("UPDATE ops.job_run SET trigger = NULL WHERE id = %s", (sched.run_id,))
    conn.commit()
    assert _health(conn, rule, now).status == "ok"
    with pytest.raises(ValueError, match="unknown job trigger"):
        run_job("rv2_locked", {}, settings=settings, db=db, trigger="cron")


def test_scheduler_records_schedule_trigger(
    db: Database, settings: Settings, conn: Conn, fake_job: list[dict[str, Any]]
) -> None:
    from mdhub.schedule import _RuleRunner

    runner = _RuleRunner(
        "rv2_locked",
        Rule(cron="* * * * *", days="all", lock_wait_s=0, catch_up=False),
        settings=settings,
        db=db,
        registry=REGISTRY,
        calendar=weekday_calendar,
        lock=threading.Lock(),
    )
    runner()
    rows = conn.execute("SELECT trigger FROM ops.job_run WHERE job = 'rv2_locked'").fetchall()
    assert rows == [{"trigger": "schedule"}]


# =========================================================================== nit


def test_lock_waiter_reannounces_after_missed_announcement(
    db_url: str, db: Database, settings: Settings, fake_job: list[dict[str, Any]]
) -> None:
    """A waiter whose first announcement collided with a backfill's check (wait key briefly
    held) announces itself on a later poll, so the next backfill yields."""
    holder = psycopg.connect(db_url, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:rv2_locked', 0))")
    holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.jobwait:rv2_locked', 0))")
    result: dict[str, Any] = {}

    def waiter() -> None:
        result["out"] = run_job(
            "rv2_locked",
            {},
            settings=settings,
            db=db,
            lock_wait_s=3,
            lock_poll_s=0.05,
            trigger="schedule",
        )

    t = threading.Thread(target=waiter)
    t.start()
    try:
        time.sleep(0.3)  # the waiter's first announcement failed
        holder.execute("SELECT pg_advisory_unlock(hashtextextended('mdhub.jobwait:rv2_locked', 0))")
        time.sleep(0.3)
        probe = psycopg.connect(db_url, autocommit=True)
        try:
            row = probe.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('mdhub.jobwait:rv2_locked', 0))"
            ).fetchone()
        finally:
            probe.close()
        assert row is not None and row[0] is False  # announced now
    finally:
        holder.close()
        t.join(10)
    assert result["out"].status == "succeeded"


# =========================================================================== N4


def test_full_reread_due_every_30_minutes() -> None:
    def at(h: int, m: int) -> datetime:
        return datetime(2026, 10, 16, h, m, tzinfo=MSK)

    due = [t for t in range(7 * 60 + 1, 24 * 60, 5) if fut.full_reread_due(at(t // 60, t % 60), 30)]
    assert len(due) == 34 and due[0] == 7 * 60 + 1 and due[1] == 7 * 60 + 31
    assert not fut.full_reread_due(at(7, 6), 30)
    assert fut.full_reread_due(at(8, 1), 60) and not fut.full_reread_due(at(8, 31), 60)


@respx.mock
def test_algopack_incremental_poll_uses_latest_snapshot(
    apim_settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    """N4: between re-reads a poll reads only the latest bars (all contracts, 1 request per
    dataset + FUTOI) under the 90-day ``intraday:`` endpoints."""
    today_is(FRI)
    seed_ref(conn)
    ts_route = respx.get(APIM_BASE_URL + apim.ALGOPACK_FO_TRADESTATS).mock(
        return_value=httpx.Response(200, json=load("algopack_tradestats_latest.json"))
    )
    respx.get(APIM_BASE_URL + apim.ALGOPACK_FO_OBSTATS).mock(
        return_value=httpx.Response(200, json=load("algopack_obstats_latest.json"))
    )
    respx.get(APIM_BASE_URL + apim.FUTOI_ALL).mock(
        return_value=httpx.Response(200, json=load("futoi_latest.json"))
    )
    per_contract = respx.get(url__regex=r".*/datashop/algopack/fo/\w+/\w+\.json").mock(
        return_value=httpx.Response(500)
    )
    out = run_job(
        "algopack_fx_futures",
        {"full": "false"},
        settings=apim_settings,
        db=db,
        http_factory=http_factory,
    )
    assert out.result["mode"] == "incremental" and out.result["holes_before"] == 0
    assert ts_route.calls.last.request.url.params["latest"] == "1"
    assert not per_contract.called
    assert count(conn, "SELECT count(*) FROM md.algopack_fo_tradestats") > 0
    endpoints = {r["endpoint"] for r in conn.execute("SELECT endpoint FROM ops.raw_capture")}
    assert endpoints == {
        "intraday:algopack_fo_tradestats",
        "intraday:algopack_fo_obstats",
        "intraday:futoi",
    }


@respx.mock
def test_algopack_holes_force_reread(
    apim_settings: Settings, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    """N4: an incremental poll that finds holes in today's bars re-reads the whole day."""
    day = date(2026, 10, 15)  # in the past: every hole is older than the grace period
    today_is(day)
    seed_ref(conn, last_seen=day)
    for hh, mm in ((10, 0), (10, 5), (10, 20)):  # holes at 10:10 and 10:15
        conn.execute(
            "INSERT INTO md.algopack_fo_tradestats (secid, interval_ts, trade_date) "
            "VALUES ('SiZ6', %s, %s)",
            (datetime(2026, 10, 15, hh, mm, tzinfo=MSK), day),
        )
    conn.commit()
    seen: list[dict[str, str]] = []
    _mock_algopack_day(seen)
    out = run_job(
        "algopack_fx_futures",
        {"full": "false"},
        settings=apim_settings,
        db=db,
        http_factory=http_factory,
    )
    assert out.result["mode"] == "reread" and out.result["holes_before"] == 2
    assert {(p["from"], p["till"]) for p in seen if "/datashop/" in p["path"]} == {
        (day.isoformat(), day.isoformat())
    }


def test_retention_of_reread_captures(conn: Conn, db: Database, settings: Settings) -> None:
    """N4: re-read captures (``intraday:reread:``) expire after 7 days, other intraday after
    90."""
    from test_maintenance import add_capture

    reread_old = add_capture(conn, "intraday:reread:algopack_fo_tradestats", 8, "r1")
    reread_new = add_capture(conn, "intraday:reread:algopack_fo_tradestats", 3, "r2")
    intraday = add_capture(conn, "intraday:algopack_fo_tradestats", 8, "i1")
    conn.commit()
    out = run_job("maintenance", {"mode": "retention", "vacuum": "false"}, settings=settings, db=db)
    assert out.result["raw_deleted"] == 1 and out.result["reread_retention_days"] == 7
    left = {r["id"] for r in conn.execute("SELECT id FROM ops.raw_capture").fetchall()}
    assert reread_old not in left and {reread_new, intraday} <= left
    assert mt.REREAD_ENDPOINT_PREFIX == fut.REREAD_ENDPOINT_PREFIX


# =========================================================================== N5


def test_no_data_grace_counts_business_days() -> None:
    """N5: Friday seen on Monday is still within the grace (no business day in between)."""
    assert no_data_status(FRI, MON) == "open"
    assert no_data_status(FRI, date(2026, 10, 20)) == "unfillable"
    assert no_data_status(date(2026, 10, 15), FRI) == "open"
    assert no_data_status(date(2026, 10, 14), FRI) == "unfillable"
    holidays = {date(2026, 12, 31), *(date(2027, 1, d) for d in range(1, 9))}

    def ny(d: date) -> bool:
        return d.weekday() < 5 and d not in holidays

    assert no_data_status(date(2026, 12, 30), date(2027, 1, 11), ny) == "open"
    assert no_data_status(date(2026, 12, 30), date(2027, 1, 12), ny) == "unfillable"


def test_record_no_data_uses_dataset_calendar(conn: Conn) -> None:
    # Monday 2026-10-19 is a holiday of MOEX_FX: Friday seen on Tuesday is still open
    conn.execute(
        "INSERT INTO ref.calendar_day (calendar, day, source, is_business_day, known_at) "
        "VALUES ('MOEX_FX', '2026-10-19', 'curated', false, now())"
    )
    st = record_no_data(conn, "fx_eod", FRI, "ISS 0 rows", today=date(2026, 10, 20))
    assert st == "open"
    note = conn.execute("SELECT note FROM ops.data_gap WHERE dataset = 'fx_eod'").fetchone()
    assert note is not None and "business-day grace" in note["note"]
    assert record_no_data(conn, "fx_eod", FRI, "again", today=date(2026, 10, 21)) == "unfillable"


def test_reconcile_on_monday_keeps_friday_open(conn: Conn) -> None:
    spec = get_dataset("fx_eod")
    record_no_data(conn, "fx_eod", FRI, "ISS 0 rows", today=MON)
    filled, unfillable = reconcile_gaps(
        conn, spec, FRI, FRI, run_id=1, mark_unfillable=True, today=MON
    )
    assert (filled, unfillable) == (0, 0)
    filled, unfillable = reconcile_gaps(
        conn, spec, FRI, FRI, run_id=2, mark_unfillable=True, today=date(2026, 10, 20)
    )
    assert (filled, unfillable) == (0, 1)


# =========================================================================== N6


def _check(spec: Any, start: date, end: date, gaps: list[Any]) -> DatasetCheck:
    return DatasetCheck(
        dataset=spec.name,
        table=spec.table,
        frequency=spec.frequency,
        calendar=spec.calendar,
        backfill_job=spec.backfill_job,
        scan_from=start,
        scan_to=end,
        gaps=gaps,
    )


def test_holiday_gap_becomes_not_expected_not_backfilled(conn: Conn) -> None:
    """N6: an unfillable holiday gap that the corrected calendar no longer expects is
    ``not_expected`` (no data), not "backfilled: data present"."""
    spec = get_dataset("fx_eod")
    st = record_no_data(conn, "fx_eod", date(2026, 6, 12), "ISS 0 rows", today=date(2026, 7, 1))
    assert st == "unfillable"
    conn.commit()
    res = sync_gaps(conn, spec, _check(spec, date(2026, 6, 1), date(2026, 6, 30), []), run_id=1)
    row = conn.execute("SELECT status, note FROM ops.data_gap WHERE dataset='fx_eod'").fetchone()
    assert res.closed == 1
    assert row is not None and row["status"] == "not_expected"
    assert row["note"].startswith("no longer expected")
    # expected and missing again (calendar flipped back): re-opened
    from mdhub.quality.checks import Gap

    again = _check(spec, date(2026, 6, 1), date(2026, 6, 30), [Gap(day=date(2026, 6, 12))])
    assert sync_gaps(conn, spec, again, run_id=2).opened == 1
    st2 = conn.execute("SELECT status FROM ops.data_gap WHERE dataset='fx_eod'").fetchone()
    assert st2 == {"status": "open"}


def test_gap_with_data_present_becomes_backfilled(conn: Conn) -> None:
    spec = get_dataset("fx_eod")
    record_no_data(conn, "fx_eod", date(2026, 6, 11), "ISS 0 rows", today=date(2026, 7, 1))
    for i in range(max(spec.min_rows_per_day, 1)):
        conn.execute(
            "INSERT INTO md.fx_eod (trade_date, secid, board) VALUES ('2026-06-11', %s, 'CETS')",
            (f"S{i}",),
        )
    sync_gaps(conn, spec, _check(spec, date(2026, 6, 1), date(2026, 6, 30), []), run_id=3)
    row = conn.execute("SELECT status, note FROM ops.data_gap WHERE dataset='fx_eod'").fetchone()
    assert row is not None and row["status"] == "backfilled"
    assert row["note"].startswith("data present")


def test_scan_and_coverage_views_report_not_expected(conn: Conn) -> None:
    spec = get_dataset("fx_eod")
    conn.execute(
        "INSERT INTO ops.data_gap (dataset, gap_date, status) "
        "VALUES ('fx_eod', '2026-06-12', 'not_expected'), ('fx_eod', '2026-06-15', 'open')"
    )
    conn.execute(
        "INSERT INTO ops.dataset_status (dataset, table_name, frequency) "
        "VALUES ('fx_eod', 'md.fx_eod', 'business_daily')"
    )
    cov = conn.execute(
        "SELECT open_gaps, unfillable_gaps, not_expected_gaps FROM ops.v_coverage "
        "WHERE dataset = 'fx_eod'"
    ).fetchone()
    assert cov == {"open_gaps": 1, "unfillable_gaps": 0, "not_expected_gaps": 1}
    open_rows = conn.execute("SELECT gap_date FROM ops.v_open_gaps").fetchall()
    assert open_rows == [{"gap_date": date(2026, 6, 15)}]
    assert scan_dataset(conn, spec, today=FRI, calendar=weekday_calendar).problem == "no rows"


# =========================================================================== N8


@pytest.fixture
def iss_empty_futures() -> Iterator[respx.MockRouter]:
    def history(request: httpx.Request) -> httpx.Response:
        cols = request.url.params.get("history.columns", "TRADEDATE").split(",")
        return httpx.Response(
            200,
            json={
                "history": {"columns": cols, "data": []},
                "history.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, 0, 100]],
                },
            },
        )

    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=ISS_BASE_URL + "/history/").mock(side_effect=history)
        for engine in ("stock", "currency", "futures"):
            body = load(f"iss_calendar_{engine}.json", OFFICIAL_FIX)
            router.get(f"{ISS_BASE_URL}/engines/{engine}/calendar.json").mock(
                return_value=httpx.Response(200, json=body)
            )
        router.get(host="www.cbr.ru", path=cbr.MEETINGS_PATH).mock(
            return_value=httpx.Response(
                200,
                content=(OFFICIAL_FIX / "cbr_cal_mp.html").read_bytes(),
                headers={"content-type": "text/html"},
            )
        )
        yield router


def test_calendar_refresh_writes_weekend_sessions_and_keeps_rows_on_empty_answer(
    iss_empty_futures: respx.MockRouter,
    settings: Settings,
    db: Database,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1/N7: ISS 'W' days are stored in ref.weekend_session (also past days of the year);
    N8: an empty engine answer neither deletes ISS calendar rows nor weekend sessions."""
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: date(2026, 10, 16))
    out = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=http_factory
    )
    assert out.status in ("succeeded", "partial")
    sessions = conn.execute(
        "SELECT day, trade_session_date FROM ref.weekend_session "
        "WHERE calendar = 'MOEX_FORTS' ORDER BY day"
    ).fetchall()
    assert {"day": date(2026, 1, 17), "trade_session_date": date(2026, 1, 19)} in sessions
    assert DbCalendar(db, ttl=0).is_trading_day(date(2026, 1, 17), "MOEX_FORTS")
    assert not DbCalendar(db, ttl=0)(date(2026, 1, 17), "MOEX_FORTS")
    n_iss = count(
        conn,
        "SELECT count(*) FROM ref.calendar_day WHERE calendar = 'MOEX_FORTS' "
        "AND source = 'moex_iss_calendar'",
    )
    n_sess = count(conn, "SELECT count(*) FROM ref.weekend_session WHERE calendar = 'MOEX_FORTS'")
    assert n_iss > 0 and n_sess > 0

    empty = load("iss_calendar_futures.json", OFFICIAL_FIX)
    empty["calendar"]["data"] = []
    iss_empty_futures.get(f"{ISS_BASE_URL}/engines/futures/calendar.json").mock(
        return_value=httpx.Response(200, json=empty)
    )
    again = run_job(
        "cbr_meetings", {"mode": "calendar"}, settings=settings, db=db, http_factory=http_factory
    )
    assert again.status == "partial"
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (again.run_id,)).fetchone()
    assert err is not None and "MOEX_FORTS" in err["error"] and "kept" in err["error"]
    assert (
        count(
            conn,
            "SELECT count(*) FROM ref.calendar_day WHERE calendar = 'MOEX_FORTS' "
            "AND source = 'moex_iss_calendar'",
        )
        == n_iss
    )
    assert (
        count(conn, "SELECT count(*) FROM ref.weekend_session WHERE calendar = 'MOEX_FORTS'")
        == n_sess
    )


def test_iss_answer_sanity() -> None:
    rows = [{"engine": "futures", "date": f"2026-0{m}-1{d}"} for m in range(1, 4) for d in range(2)]
    assert cal.iss_answer_complete(rows, "futures", date(2026, 10, 16))
    assert not cal.iss_answer_complete(rows[:4], "futures", date(2026, 10, 16))
    assert not cal.iss_answer_complete(rows, "futures", date(2027, 1, 10))
    assert not cal.iss_answer_complete(rows, "stock", date(2026, 10, 16))


# =========================================================================== N9


def test_every_raw_capture_fk_has_capture_index(conn: Conn) -> None:
    missing = conn.execute(
        """
        SELECT c.conrelid::regclass::text AS tbl
        FROM pg_constraint c
        WHERE c.contype = 'f' AND c.confrelid = 'ops.raw_capture'::regclass
          AND NOT EXISTS (
              SELECT 1 FROM pg_index i
              WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1])
        """
    ).fetchall()
    assert missing == []


def test_migration_0421_comments(conn: Conn) -> None:
    row = conn.execute(
        "SELECT col_description('ref.bond'::regclass, a.attnum) AS c FROM pg_attribute a "
        "WHERE a.attrelid = 'ref.bond'::regclass AND a.attname = 'coupon_type_basis'"
    ).fetchone()
    assert row is not None and "schedule_constant" in row["c"]
    trig = conn.execute(
        "SELECT col_description('ops.job_run'::regclass, a.attnum) AS c FROM pg_attribute a "
        "WHERE a.attrelid = 'ops.job_run'::regclass AND a.attname = 'trigger'"
    ).fetchone()
    assert trig is not None and "catch_up" in trig["c"]
    assert UTC  # imported for symmetry with other test modules
