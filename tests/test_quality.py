"""Quality checks: expected dates, range compression, gap scan, gap bookkeeping, queueing,
job health and the ``quality`` job (fake datasets/jobs, registered per test)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate, weekday_calendar
from mdhub.core.timeutil import MSK, date_range
from mdhub.datasets import DatasetSpec, register_dataset, unregister_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobRegistry, run_job
from mdhub.quality.checks import (
    compress_ranges,
    expected_days,
    expected_latest,
    queue_backfill,
    queue_range,
    scan_dataset,
    split_range,
    sync_gaps,
)
from mdhub.quality.health import job_health, last_expected_fire
from mdhub.quality.job import quality as quality_job
from mdhub.schedule import Rule

Conn = psycopg.Connection[DictRow]

AS_OF = date(2026, 10, 16)  # Friday
START = date(2026, 9, 28)  # Monday
MISSING = {date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 12)}
FAKE_JOB = "test_q_fake_loader"
assert callable(quality_job)  # importing registers the `quality` job


class Holidays(CalendarPredicate):
    def __init__(self, holidays: set[date]) -> None:
        self.holidays = holidays

    def __call__(self, day: date, calendar: str = "MOEX_STOCK") -> bool:
        return day.weekday() < 5 and day not in self.holidays


def weekdays(lo: date, hi: date) -> list[date]:
    return [d for d in date_range(lo, hi) if d.weekday() < 5]


def spec(name: str = "test_q_daily", **kw: Any) -> DatasetSpec:
    base: dict[str, Any] = {
        "name": name,
        "table": "md.q_fake",
        "date_column": "trade_date",
        "calendar": "MOEX_STOCK",
        "frequency": "business_daily",
        "max_lag_days": 1,
        "description": "test",
        "backfill_job": FAKE_JOB,
        "history_start": START,
        "min_rows_per_day": 1,
    }
    base.update(kw)
    return DatasetSpec(**base)


@pytest.fixture
def fake_table(conn: Conn) -> Conn:
    conn.execute(
        """
        CREATE TABLE md.q_fake (
            trade_date date NOT NULL, secid text NOT NULL, source text NOT NULL DEFAULT 'x',
            PRIMARY KEY (trade_date, secid)
        )
        """
    )
    conn.commit()
    return conn


def insert_days(conn: Conn, days: list[date], secids: tuple[str, ...] = ("A",)) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.q_fake (trade_date, secid) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(d, s) for d in days for s in secids],
        )
    conn.commit()


@pytest.fixture
def fake_job() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def loader(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        calls.append({"date_from": date_from, "date_to": date_to})

    REGISTRY.register(FAKE_JOB, "fake loader", loader)
    try:
        yield calls
    finally:
        REGISTRY.unregister(FAKE_JOB)


@pytest.fixture
def registered() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    for n in names:
        unregister_dataset(n)


# ------------------------------------------------------------------------- pure logic


def test_expected_latest_per_frequency() -> None:
    cal = Holidays({date(2026, 10, 15)})
    s = spec()
    assert expected_latest(s, AS_OF, cal) == date(2026, 10, 14)  # Fri -> Thu is a holiday
    assert expected_latest(s, date(2026, 10, 18), weekday_calendar) == date(2026, 10, 15)
    assert expected_latest(spec(max_lag_days=0), date(2026, 10, 18), weekday_calendar) == date(
        2026, 10, 16
    )
    assert expected_latest(spec(frequency="daily", max_lag_days=2), AS_OF, cal) == date(
        2026, 10, 14
    )
    assert expected_latest(spec(frequency="monthly", max_lag_days=45), AS_OF, cal) == date(
        2026, 9, 1
    )
    assert expected_latest(spec(frequency="event", max_lag_days=60), AS_OF, cal) == date(
        2026, 8, 17
    )
    assert expected_latest(spec(calendar=None), AS_OF, cal) == date(2026, 10, 15)


def test_closed_series_is_never_stale_and_not_scanned_past_its_end(fake_table: Conn) -> None:
    conn = fake_table
    end = date(2026, 10, 7)
    insert_days(conn, weekdays(START, end))
    s = spec(history_end=end)
    assert expected_latest(s, AS_OF, weekday_calendar) == end
    check = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert not check.is_stale and check.problem is None
    assert check.scan_to == end and check.gaps == []
    with pytest.raises(ValueError, match="history_end before history_start"):
        spec(history_end=START - timedelta(days=1))


def test_expected_days() -> None:
    s = spec()
    assert expected_days(s, date(2026, 10, 9), date(2026, 10, 13), weekday_calendar) == [
        date(2026, 10, 9),
        date(2026, 10, 12),
        date(2026, 10, 13),
    ]
    m = spec(frequency="monthly")
    assert expected_days(m, date(2026, 1, 15), date(2026, 4, 1), weekday_calendar) == [
        date(2026, 2, 1),
        date(2026, 3, 1),
        date(2026, 4, 1),
    ]
    assert expected_days(s, date(2026, 10, 2), date(2026, 10, 1), weekday_calendar) == []


def test_compress_ranges() -> None:
    wd = weekday_calendar.__call__
    days = [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 12)]
    assert compress_ranges(days, wd) == [
        (date(2026, 10, 1), date(2026, 10, 5)),
        (date(2026, 10, 12), date(2026, 10, 12)),
    ]
    # unsorted + duplicates, and max length splits a long run
    run = weekdays(date(2026, 1, 1), date(2026, 3, 31))
    ranges = compress_ranges(list(reversed(run)) + run[:3], wd, max_days=31)
    assert ranges[0][0] == run[0] and ranges[-1][1] == run[-1]
    assert all((b - a).days + 1 <= 31 for a, b in ranges)
    assert sum(len(weekdays(a, b)) for a, b in ranges) == len(run)
    # a holiday between two gap days does not break the range
    cal = Holidays({date(2026, 10, 6)})
    assert compress_ranges([date(2026, 10, 5), date(2026, 10, 7)], cal.__call__) == [
        (date(2026, 10, 5), date(2026, 10, 7))
    ]
    assert compress_ranges([], wd) == []
    with pytest.raises(ValueError, match="max_days"):
        compress_ranges(days, wd, max_days=0)


def test_split_range() -> None:
    assert split_range(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    parts = split_range(date(2026, 1, 1), date(2026, 3, 15), 31)
    assert parts[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert parts[-1][1] == date(2026, 3, 15)
    assert all(parts[i][1] + timedelta(days=1) == parts[i + 1][0] for i in range(len(parts) - 1))
    with pytest.raises(ValueError, match="before"):
        split_range(date(2026, 1, 2), date(2026, 1, 1))


# ------------------------------------------------------------------------ scan + sync


def test_scan_sync_queue_and_close(fake_table: Conn) -> None:
    conn = fake_table
    insert_days(conn, [d for d in weekdays(START, date(2026, 10, 15)) if d not in MISSING])
    s = spec()
    check = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert not check.is_stale and check.problem is None
    assert (check.first_date, check.last_date) == (START, date(2026, 10, 15))
    assert check.scan_from == START and check.scan_to == date(2026, 10, 15)
    assert {g.day for g in check.gaps} == MISSING
    assert all(g.key == () and g.rows == 0 for g in check.gaps)

    sync = sync_gaps(conn, s, check, run_id=None)
    assert (sync.opened, sync.closed) == (4, 0)
    queued = queue_backfill(conn, s, weekday_calendar)
    conn.commit()
    assert queued == [
        (date(2026, 10, 1), date(2026, 10, 5)),
        (date(2026, 10, 12), date(2026, 10, 12)),
    ]
    tasks = conn.execute("SELECT dataset, params, status, source FROM ops.backfill_task").fetchall()
    assert {(t["params"]["date_from"], t["params"]["date_to"]) for t in tasks} == {
        ("2026-10-01", "2026-10-05"),
        ("2026-10-12", "2026-10-12"),
    }
    assert {t["status"] for t in tasks} == {"pending"} and {t["source"] for t in tasks} == {
        "quality"
    }

    # idempotent: second run opens nothing and queues nothing
    check2 = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert sync_gaps(conn, s, check2, run_id=None).opened == 0
    assert queue_backfill(conn, s, weekday_calendar) == []
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM ops.backfill_task").fetchone() == {"n": 2}

    # data arrives for 10-12 -> gap closed as backfilled; 10-01 unfillable stays put
    insert_days(conn, [date(2026, 10, 12)])
    conn.execute("UPDATE ops.data_gap SET status = 'unfillable' WHERE gap_date = '2026-10-01'")
    check3 = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    sync3 = sync_gaps(conn, s, check3, run_id=7)
    conn.commit()
    assert sync3.closed == 1
    rows = {
        r["gap_date"]: r
        for r in conn.execute("SELECT gap_date, status, note FROM ops.data_gap").fetchall()
    }
    assert rows[date(2026, 10, 12)]["status"] == "backfilled"
    assert "quality run 7" in rows[date(2026, 10, 12)]["note"]
    assert rows[date(2026, 10, 1)]["status"] == "unfillable"

    # data disappears again -> re-opened
    conn.execute("DELETE FROM md.q_fake WHERE trade_date = '2026-10-12'")
    check4 = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert sync_gaps(conn, s, check4, run_id=8).opened == 1
    conn.commit()


def test_unfillable_and_covered_days_not_queued(fake_table: Conn) -> None:
    conn = fake_table
    s = spec()
    insert_days(conn, [d for d in weekdays(START, date(2026, 10, 15)) if d not in MISSING])
    sync_gaps(conn, s, scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar), run_id=None)
    conn.execute("UPDATE ops.data_gap SET status = 'unfillable' WHERE gap_date = '2026-10-12'")
    # a manual open task already covers 10-01..10-02
    queue_range(conn, s, date(2026, 10, 1), date(2026, 10, 2))
    assert queue_backfill(conn, s, weekday_calendar) == [(date(2026, 10, 5), date(2026, 10, 5))]
    # a recently failed task blocks re-queueing its days
    conn.execute("UPDATE ops.backfill_task SET status = 'failed', done_at = now()")
    assert queue_backfill(conn, s, weekday_calendar) == []
    conn.execute("UPDATE ops.backfill_task SET done_at = now() - interval '30 days'")
    assert queue_backfill(conn, s, weekday_calendar) == [(date(2026, 10, 1), date(2026, 10, 5))]
    # no backfill_job -> nothing queued
    assert queue_backfill(conn, spec(backfill_job=None), weekday_calendar) == []
    conn.rollback()


def test_stale_empty_missing_and_event(fake_table: Conn) -> None:
    conn = fake_table
    s = spec()
    empty = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert empty.is_stale and empty.problem == "no rows" and not empty.scanned
    missing = scan_dataset(
        conn, spec(table="md.no_such_table"), today=AS_OF, calendar=weekday_calendar
    )
    assert missing.is_stale and "does not exist" in (missing.problem or "")
    insert_days(conn, weekdays(START, date(2026, 10, 14)))
    stale = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert stale.is_stale and stale.last_date == date(2026, 10, 14)
    assert [g.day for g in stale.gaps] == [date(2026, 10, 15)]
    event = scan_dataset(
        conn, spec(frequency="event", max_lag_days=30), today=AS_OF, calendar=weekday_calendar
    )
    assert not event.is_stale and not event.scanned and event.gaps == []
    with pytest.raises(ValueError, match=r"not in md\.q_fake"):
        scan_dataset(conn, spec(date_column="nope"), today=AS_OF, calendar=weekday_calendar)


def test_holiday_calendar_and_history_start(fake_table: Conn) -> None:
    conn = fake_table
    insert_days(conn, [date(2026, 9, 1), *weekdays(START, date(2026, 10, 15))])
    conn.execute("DELETE FROM md.q_fake WHERE trade_date = '2026-10-07'")
    cal = Holidays({date(2026, 10, 7)})
    check = scan_dataset(conn, spec(), today=AS_OF, calendar=cal)
    assert check.gaps == [] and check.scan_from == START  # history_start > first stored date
    check_wd = scan_dataset(conn, spec(history_start=None), today=AS_OF, calendar=weekday_calendar)
    assert check_wd.scan_from == date(2026, 9, 1)
    assert date(2026, 10, 7) in {g.day for g in check_wd.gaps}
    limited = scan_dataset(
        conn, spec(history_start=None), today=AS_OF, calendar=weekday_calendar, lookback_days=5
    )
    assert limited.scan_from == date(2026, 10, 11)
    assert {g.day for g in limited.gaps} == set()


def test_series_explicit_and_implicit(fake_table: Conn) -> None:
    conn = fake_table
    days = weekdays(START, date(2026, 10, 15))
    insert_days(conn, days, ("A",))
    insert_days(conn, [d for d in days if d != date(2026, 10, 6)], ("B",))
    # C starts later and stops early (implicit: only holes inside its span count)
    insert_days(conn, [date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 8)], ("C",))

    explicit = spec(series_column="secid", series=("A", "B", "D"))
    check = scan_dataset(conn, explicit, today=AS_OF, calendar=weekday_calendar)
    assert check.stale_series == ["D"] and check.is_stale
    assert [(g.day, g.key) for g in check.gaps] == [(date(2026, 10, 6), (("secid", "B"),))]

    implicit = spec(series_column="secid")
    check2 = scan_dataset(conn, implicit, today=AS_OF, calendar=weekday_calendar)
    assert not check2.is_stale
    assert sorted((g.day, g.key_dict["secid"]) for g in check2.gaps) == [
        (date(2026, 10, 6), "B"),
        (date(2026, 10, 6), "C"),
    ]
    sync = sync_gaps(conn, implicit, check2, run_id=None)
    assert sync.opened == 2
    keys = conn.execute("SELECT key FROM ops.data_gap ORDER BY key::text").fetchall()
    assert [k["key"] for k in keys] == [{"secid": "B"}, {"secid": "C"}]
    # one range for both series
    assert queue_backfill(conn, implicit, weekday_calendar) == [
        (date(2026, 10, 6), date(2026, 10, 6))
    ]
    conn.rollback()


def test_where_predicate_and_min_rows(fake_table: Conn) -> None:
    conn = fake_table
    days = weekdays(START, date(2026, 10, 15))
    insert_days(conn, days, ("SU1", "SU2", "XX1"))
    conn.execute("DELETE FROM md.q_fake WHERE secid = 'SU2' AND trade_date = '2026-10-09'")
    s = spec(where="secid LIKE 'SU%'", min_rows_per_day=2)
    check = scan_dataset(conn, s, today=AS_OF, calendar=weekday_calendar)
    assert [(g.day, g.rows) for g in check.gaps] == [(date(2026, 10, 9), 1)]
    assert check.row_count == 2 * len(days) - 1


def test_timestamptz_intraday_and_monthly(conn: Conn) -> None:
    conn.execute("CREATE TABLE md.q_intraday (ts timestamptz NOT NULL, secid text)")
    conn.execute("CREATE TABLE md.q_monthly (period date NOT NULL, value int)")
    # 23:30 MSK on 10-14 is 20:30 UTC: belongs to 10-14 (MSK), not to the UTC date
    rows = [datetime(2026, 10, d, 7, 0, tzinfo=UTC) for d in (5, 6, 7, 8, 9, 12, 13, 15)] + [
        datetime(2026, 10, 14, 20, 30, tzinfo=UTC)
    ]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO md.q_intraday VALUES (%s, 'X')", [(r,) for r in rows])
        cur.executemany(
            "INSERT INTO md.q_monthly VALUES (%s, 1)",
            [(date(2026, m, 15),) for m in (1, 2, 4, 5, 6, 7, 8, 9)],
        )
    intraday = DatasetSpec(
        name="test_q_intraday",
        table="md.q_intraday",
        date_column="ts",
        calendar="MOEX_STOCK",
        frequency="intraday",
        max_lag_days=1,
        description="t",
    )
    check = scan_dataset(conn, intraday, today=AS_OF, calendar=weekday_calendar)
    assert check.last_date == date(2026, 10, 15) and not check.is_stale
    assert check.scan_from == date(2026, 10, 5)  # first stored, within the 14-day lookback
    assert check.gaps == []

    monthly = DatasetSpec(
        name="test_q_monthly",
        table="md.q_monthly",
        date_column="period",
        calendar=None,
        frequency="monthly",
        max_lag_days=20,
        description="t",
    )
    check2 = scan_dataset(conn, monthly, today=AS_OF, calendar=weekday_calendar)
    assert check2.expected_latest == date(2026, 9, 1) and not check2.is_stale
    assert [g.day for g in check2.gaps] == [date(2026, 3, 1)]
    assert compress_ranges([date(2026, 3, 1), date(2026, 4, 1)], lambda d: d.day == 1, 366) == [
        (date(2026, 3, 1), date(2026, 4, 1))
    ]
    conn.rollback()


# ------------------------------------------------------------------------ quality job


def test_quality_job_stores_report_and_coverage(
    fake_table: Conn,
    db: Database,
    settings: Settings,
    fake_job: list[dict[str, Any]],
    registered: list[str],
) -> None:
    conn = fake_table
    insert_days(conn, [d for d in weekdays(START, date(2026, 10, 15)) if d not in MISSING])
    good = register_dataset(spec())
    bad = register_dataset(spec(name="test_q_bad", where="no_such_column = 1"))
    registered.extend([good.name, bad.name])
    outcome = run_job(
        "quality",
        {"as_of": "2026-10-16", "datasets": "test_q_daily,test_q_bad", "check_jobs": "false"},
        settings=settings,
        db=db,
    )
    assert outcome.status == "partial"  # the bad spec is reported, the rest is checked
    report = outcome.result
    assert report["summary"]["gaps"] == 4 and report["summary"]["tasks_queued"] == 2
    assert "test_q_bad" in report["summary"]["dataset_problems"]

    stored = conn.execute("SELECT * FROM ops.quality_report").fetchone()
    assert stored is not None and stored["run_id"] == outcome.run_id
    assert stored["gap_count"] == 4 and stored["tasks_queued"] == 2
    assert stored["report"]["datasets"][0]["dataset"] == "test_q_daily"

    cov = {r["dataset"]: r for r in conn.execute("SELECT * FROM ops.v_coverage").fetchall()}
    assert cov["test_q_daily"]["open_gaps"] == 4
    assert cov["test_q_daily"]["open_tasks"] == 2
    assert cov["test_q_daily"]["last_date"] == date(2026, 10, 15)
    assert cov["test_q_daily"]["row_count"] == len(weekdays(START, date(2026, 10, 15))) - 4
    assert cov["test_q_bad"]["problem"].startswith("check failed")
    gaps = conn.execute("SELECT * FROM ops.v_open_gaps ORDER BY gap_date").fetchall()
    assert [g["task_status"] for g in gaps] == ["pending"] * 4

    # second run: idempotent
    again = run_job(
        "quality",
        {"as_of": "2026-10-16", "datasets": ["test_q_daily"], "check_jobs": False},
        settings=settings,
        db=db,
    )
    assert again.status == "succeeded" and again.result["summary"]["tasks_queued"] == 0
    with pytest.raises(ValueError, match="unknown dataset"):
        run_job("quality", {"datasets": ["no_such_dataset"]}, settings=settings, db=db)


def test_quality_reports_unregistered_backfill_job(
    fake_table: Conn, db: Database, settings: Settings, registered: list[str]
) -> None:
    insert_days(fake_table, [d for d in weekdays(START, date(2026, 10, 15)) if d not in MISSING])
    registered.append(register_dataset(spec(name="test_q_nojob")).name)
    out = run_job(
        "quality",
        {"as_of": "2026-10-16", "datasets": ["test_q_nojob"], "check_jobs": False},
        settings=settings,
        db=db,
    )
    ds = out.result["datasets"][0]
    assert ds["tasks_queued"] == [] and "not registered" in ds["problem"]


# ------------------------------------------------------------------------- job health


def _run(conn: Conn, job: str, status: str, started: datetime, error: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO ops.job_run (job, started_at, finished_at, status, error)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            job,
            started,
            None if status == "running" else started + timedelta(seconds=5),
            status,
            error,
        ),
    )


def test_last_expected_fire() -> None:
    tz = ZoneInfo("Europe/Moscow")
    cutoff = datetime(2026, 10, 19, 8, 0, tzinfo=tz)  # Monday 08:00
    business = (Rule("30 20 * * *"),)
    assert last_expected_fire(business, cutoff, weekday_calendar, tz) == datetime(
        2026, 10, 16, 20, 30, tzinfo=tz
    )
    every = (Rule("*/10 * * * *", days="all"),)
    assert last_expected_fire(every, cutoff, weekday_calendar, tz) == cutoff
    two = (Rule("30 8 * * *"), Rule("30 20 * * *"))
    noon = datetime(2026, 10, 19, 12, 0, tzinfo=tz)
    assert last_expected_fire(two, noon, weekday_calendar, tz) == datetime(
        2026, 10, 19, 8, 30, tzinfo=tz
    )


def test_job_health(conn: Conn) -> None:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 10, 16, 12, 0, tzinfo=tz)  # Friday
    reg = JobRegistry()
    for name in ("ok_job", "missed_job", "failing_job", "degraded_job", "manual_job"):
        reg.register(name, "t", lambda ctx: None)
    rules = {
        "ok_job": (Rule("0 9 * * *"),),
        "missed_job": (Rule("0 9 * * *"),),
        "failing_job": (Rule("0 9 * * *"),),
        "degraded_job": (Rule("0 9 * * *"),),
        "absent_job": (Rule("0 9 * * *"),),
    }
    nine = datetime(2026, 10, 16, 9, 0, 5, tzinfo=tz)
    _run(conn, "ok_job", "succeeded", nine)
    _run(conn, "missed_job", "succeeded", nine - timedelta(days=1))
    _run(conn, "failing_job", "succeeded", nine - timedelta(days=4))
    for i in (3, 2, 1, 0):
        _run(conn, "failing_job", "failed", nine - timedelta(days=i), error="boom")
    _run(conn, "degraded_job", "succeeded", nine - timedelta(days=1))
    _run(conn, "degraded_job", "failed", nine, error="x")
    _run(conn, "manual_job", "succeeded", nine)
    conn.commit()

    view = {r["job"]: r for r in conn.execute("SELECT * FROM ops.v_job_health").fetchall()}
    assert view["failing_job"]["consecutive_failures"] == 4
    assert view["degraded_job"]["consecutive_failures"] == 1
    assert view["ok_job"]["consecutive_failures"] == 0

    health = {
        h.job: h
        for h in job_health(
            conn,
            now=now,
            calendar=weekday_calendar,
            tz=tz,
            rules=rules,
            known=list(rules),
            registry=reg,
        )
    }
    assert health["ok_job"].status == "ok"
    assert health["missed_job"].status == "missed" and health["missed_job"].problem
    assert health["failing_job"].status == "failing" and health["failing_job"].problem
    assert health["degraded_job"].status == "degraded" and not health["degraded_job"].problem
    assert health["manual_job"].status == "manual"
    assert health["absent_job"].status == "not_registered"
    assert health["ok_job"].last_expected_at == datetime(2026, 10, 16, 9, 0, tzinfo=tz)
    assert health["failing_job"].to_dict()["last_error"] == "boom"


def test_quality_job_includes_job_health(db: Database, settings: Settings, conn: Conn) -> None:
    _run(conn, "bonds_eod", "failed", datetime.now(MSK) - timedelta(hours=1), "e1")
    conn.commit()
    out = run_job("quality", {"datasets": [], "check_jobs": True}, settings=settings, db=db)
    jobs = {j["job"]: j for j in out.result["jobs"]}
    assert "quality" in jobs and "backfill_worker" in jobs
    assert jobs["bonds_eod"]["last_status"] == "failed"
