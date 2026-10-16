"""backfill_worker: claiming, nested runs, gap reconciliation, retries/backoff, attempts,
time budget, lock contention, and the `mdhub ops backfill` CLI."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from mdhub import cli
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.timeutil import date_range
from mdhub.datasets import DatasetSpec, register_dataset, unregister_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, run_job
from mdhub.quality.backfill import (
    MAX_ATTEMPTS,
    backfill_worker,
    backoff,
    claim_task,
    process_tasks,
)
from mdhub.quality.checks import queue_backfill, scan_dataset, sync_gaps

Conn = psycopg.Connection[DictRow]

JOB = "test_bf_loader"
DATASET = "test_bf_daily"
HOLIDAY = date(2026, 10, 7)  # the source has no data (holiday missing from the calendar)
assert callable(backfill_worker)  # importing registers the `backfill_worker` job


@dataclass
class Source:
    """Behaviour of the fake backfill job."""

    fail: bool = False
    calls: list[tuple[date | None, date | None]] = field(default_factory=list)
    no_data: set[date] = field(default_factory=lambda: {HOLIDAY})


def weekdays(lo: date, hi: date) -> list[date]:
    return [d for d in date_range(lo, hi) if d.weekday() < 5]


@pytest.fixture
def source(conn: Conn) -> Iterator[Source]:
    conn.execute("CREATE TABLE md.bf_fake (trade_date date PRIMARY KEY, v int)")
    conn.commit()
    src = Source()

    def loader(
        ctx: JobContext,
        *,
        mode: str = "final",
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> None:
        src.calls.append((date_from, date_to))
        if src.fail:
            raise RuntimeError("source down")
        assert date_from is not None and date_to is not None
        with ctx.connection() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO md.bf_fake VALUES (%s, 1) ON CONFLICT DO NOTHING",
                [(d,) for d in weekdays(date_from, date_to) if d not in src.no_data],
            )

    REGISTRY.register(JOB, "fake backfill loader", loader)
    spec = register_dataset(
        DatasetSpec(
            name=DATASET,
            table="md.bf_fake",
            date_column="trade_date",
            calendar="MOEX_STOCK",
            frequency="business_daily",
            max_lag_days=1,
            description="t",
            backfill_job=JOB,
            history_start=date(2026, 10, 1),
        )
    )
    try:
        yield src
    finally:
        REGISTRY.unregister(JOB)
        unregister_dataset(spec.name)


def seed_gaps(conn: Conn) -> None:
    """Data on 10-01, 10-02 and 10-13..10-15; gaps 10-05..10-12 (one task)."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO md.bf_fake VALUES (%s, 1)",
            [
                (d,)
                for d in [
                    date(2026, 10, 1),
                    date(2026, 10, 2),
                    *weekdays(date(2026, 10, 13), date(2026, 10, 15)),
                ]
            ],
        )
    from mdhub.datasets import get_dataset

    spec = get_dataset(DATASET)
    check = scan_dataset(conn, spec, today=date(2026, 10, 16), calendar=weekday_calendar)
    sync_gaps(conn, spec, check, run_id=None)
    assert queue_backfill(conn, spec, weekday_calendar) == [(date(2026, 10, 5), date(2026, 10, 12))]
    conn.commit()


def work(settings: Settings, db: Database, **kw: Any) -> Any:
    return process_tasks(settings=settings, db=db, calendar=weekday_calendar, **kw)


def test_worker_success_backfills_and_marks_unfillable(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    seed_gaps(conn)
    summary = work(settings, db)
    assert (summary.processed, summary.done) == (1, 1)
    assert summary.gaps_backfilled == 5 and summary.gaps_unfillable == 1
    assert source.calls == [(date(2026, 10, 5), date(2026, 10, 12))]

    task = conn.execute("SELECT * FROM ops.backfill_task").fetchone()
    assert task is not None
    assert task["status"] == "done" and task["attempts"] == 1 and task["done_at"] is not None
    nested = conn.execute(
        "SELECT id, status, params FROM ops.job_run WHERE job = %s", (JOB,)
    ).fetchone()
    assert nested is not None and nested["status"] == "succeeded"
    assert nested["params"] == {"date_from": "2026-10-05", "date_to": "2026-10-12"}
    assert task["last_run_id"] == nested["id"]

    gaps = {
        r["gap_date"]: r
        for r in conn.execute("SELECT gap_date, status, note FROM ops.data_gap").fetchall()
    }
    assert gaps[HOLIDAY]["status"] == "unfillable"
    assert "no data from source" in gaps[HOLIDAY]["note"]
    assert {d for d, r in gaps.items() if r["status"] == "backfilled"} == set(
        weekdays(date(2026, 10, 5), date(2026, 10, 12))
    ) - {HOLIDAY}
    # nothing left to do; unfillable day is not re-queued
    from mdhub.datasets import get_dataset

    assert queue_backfill(conn, get_dataset(DATASET), weekday_calendar) == []
    assert work(settings, db).processed == 0


def test_worker_failure_backoff_and_exhaustion(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    seed_gaps(conn)
    source.fail = True
    first = work(settings, db)
    assert (first.processed, first.retried) == (1, 1)
    task = conn.execute("SELECT * FROM ops.backfill_task").fetchone()
    assert task is not None
    assert task["status"] == "pending" and task["attempts"] == 1
    assert "source down" in task["last_error"]
    assert task["not_before"] is not None
    conn.commit()
    # backoff: not due yet
    assert work(settings, db).processed == 0
    for attempt in range(2, MAX_ATTEMPTS + 1):
        conn.execute("UPDATE ops.backfill_task SET not_before = now() - interval '1 second'")
        conn.commit()
        res = work(settings, db)
        assert res.processed == 1
        row = conn.execute("SELECT status, attempts FROM ops.backfill_task").fetchone()
        conn.commit()
        assert row is not None and row["attempts"] == attempt
        assert row["status"] == ("failed" if attempt == MAX_ATTEMPTS else "pending")
    assert len(source.calls) == MAX_ATTEMPTS
    notes = conn.execute(
        "SELECT DISTINCT status, note FROM ops.data_gap WHERE status = 'open'"
    ).fetchall()
    assert len(notes) == 1 and f"failed after {MAX_ATTEMPTS} attempts" in notes[0]["note"]
    failed_runs = conn.execute(
        "SELECT count(*) AS n FROM ops.job_run WHERE job = %s AND status = 'failed'", (JOB,)
    ).fetchone()
    assert failed_runs == {"n": MAX_ATTEMPTS}
    # quality does not re-queue days of a recently failed task
    from mdhub.datasets import get_dataset

    assert queue_backfill(conn, get_dataset(DATASET), weekday_calendar) == []


def test_backoff_grows() -> None:
    assert backoff(1) == timedelta(minutes=10)
    assert backoff(2) == timedelta(minutes=20)
    assert backoff(4) == timedelta(minutes=80)


def test_partial_run_with_open_gaps_is_retried_not_looped(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    """A partial run that leaves gaps of its range open counts as a failed attempt (backoff,
    then ``failed`` + cooldown) instead of ``done`` + re-queue by every quality run (F1)."""
    seed_gaps(conn)

    def partial_loader(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        ctx.run.mark_partial("some pages failed")

    REGISTRY.unregister(JOB)
    REGISTRY.register(JOB, "partial", partial_loader)
    summary = work(settings, db)
    assert summary.done == 0 and summary.retried == 1 and summary.gaps_unfillable == 0
    statuses = {r["status"] for r in conn.execute("SELECT status FROM ops.data_gap").fetchall()}
    assert statuses == {"open"}
    task = conn.execute("SELECT status, attempts, last_error FROM ops.backfill_task").fetchone()
    assert task["status"] == "pending" and task["attempts"] == 1
    assert "partial run left 6 gap(s) open" in task["last_error"]
    for _ in range(MAX_ATTEMPTS - 1):
        conn.execute("UPDATE ops.backfill_task SET not_before = NULL")
        conn.commit()
        work(settings, db)
    task = conn.execute("SELECT status, attempts FROM ops.backfill_task").fetchone()
    assert task == {"status": "failed", "attempts": MAX_ATTEMPTS}
    from mdhub.datasets import get_dataset

    assert queue_backfill(conn, get_dataset(DATASET), weekday_calendar) == []  # cooldown


def test_partial_run_that_filled_the_range_is_done(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    seed_gaps(conn)

    def loader(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        assert date_from is not None and date_to is not None
        with ctx.connection() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO md.bf_fake VALUES (%s, 1) ON CONFLICT DO NOTHING",
                [(d,) for d in weekdays(date_from, date_to)],
            )
        ctx.run.mark_partial("an unrelated part failed")

    REGISTRY.unregister(JOB)
    REGISTRY.register(JOB, "partial but complete", loader)
    summary = work(settings, db)
    assert summary.done == 1 and summary.gaps_backfilled == 6
    task = conn.execute("SELECT status, last_error FROM ops.backfill_task").fetchone()
    assert task == {"status": "done", "last_error": "an unrelated part failed"}


def test_no_data_day_ends_unfillable_not_requeued_forever(
    conn: Conn, settings: Settings, db: Database
) -> None:
    """Review F1 (scratch test converted): a backfill job that records a source-confirmed
    empty day with record_no_data (fx_moex_eod/futures_eod/fx_indicative now do) ends with a
    terminal unfillable gap after one cycle - no task per quality run for ever."""
    from mdhub.core.gaps import record_no_data

    conn.execute("CREATE TABLE md.rv_fake (trade_date date PRIMARY KEY, v int)")
    conn.commit()
    job, ds = "rv_nodata_loader", "rv_nodata_ds"

    def loader(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        assert date_from and date_to
        with ctx.connection() as c, c.cursor() as cur:
            for d in weekdays(date_from, date_to):
                if d == HOLIDAY:
                    record_no_data(c, ds, d, "ISS returned 0 rows", today=ctx.today())
                    continue
                cur.execute("INSERT INTO md.rv_fake VALUES (%s, 1) ON CONFLICT DO NOTHING", (d,))

    REGISTRY.register(job, "fake", loader)
    spec = register_dataset(
        DatasetSpec(
            name=ds,
            table="md.rv_fake",
            date_column="trade_date",
            calendar="MOEX_FX",
            frequency="business_daily",
            max_lag_days=1,
            description="t",
            backfill_job=job,
            history_start=date(2026, 10, 1),
        )
    )
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO md.rv_fake VALUES (%s, 1)",
                [(d,) for d in weekdays(date(2026, 10, 1), date(2026, 10, 15)) if d != HOLIDAY],
            )
        conn.commit()
        queued = []
        for _cycle in range(3):
            check = scan_dataset(conn, spec, today=date(2026, 10, 16), calendar=weekday_calendar)
            sync_gaps(conn, spec, check, run_id=None)
            queued.append(queue_backfill(conn, spec, weekday_calendar))
            conn.commit()
            work(settings, db)
        assert queued == [[(HOLIDAY, HOLIDAY)], [], []]
        gap = conn.execute(
            "SELECT status, note FROM ops.data_gap WHERE dataset = %s", (ds,)
        ).fetchone()
        assert gap["status"] == "unfillable" and "no data from source" in gap["note"]
        n = conn.execute("SELECT count(*) AS n FROM ops.backfill_task WHERE dataset = %s", (ds,))
        assert n.fetchone() == {"n": 1}
        # F10: data that appears later is still recognised (unfillable -> backfilled)
        conn.execute("INSERT INTO md.rv_fake VALUES (%s, 1)", (HOLIDAY,))
        check = scan_dataset(conn, spec, today=date(2026, 10, 16), calendar=weekday_calendar)
        assert sync_gaps(conn, spec, check, run_id=None).closed == 1
        conn.commit()
        st = conn.execute("SELECT status FROM ops.data_gap WHERE dataset = %s", (ds,)).fetchone()
        assert st == {"status": "backfilled"}
    finally:
        REGISTRY.unregister(job)
        unregister_dataset(ds)


def test_datasets_sharing_a_job_share_one_task(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    """F12: a calendar hole seen by several datasets of one backfill job queues one task, and
    that task reconciles the gaps of all of them."""
    from mdhub.datasets import get_dataset

    sibling = register_dataset(
        DatasetSpec(
            name="test_bf_sibling",
            table="md.bf_fake",
            date_column="trade_date",
            calendar="MOEX_STOCK",
            frequency="business_daily",
            max_lag_days=1,
            description="t",
            backfill_job=JOB,
            history_start=date(2026, 10, 1),
        )
    )
    try:
        seed_gaps(conn)
        check = scan_dataset(conn, sibling, today=date(2026, 10, 16), calendar=weekday_calendar)
        sync_gaps(conn, sibling, check, run_id=None)
        assert queue_backfill(conn, sibling, weekday_calendar) == []  # covered by the task
        conn.commit()
        assert work(settings, db).done == 1
        rows = conn.execute(
            "SELECT dataset, status, count(*) AS n FROM ops.data_gap GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        assert rows == [
            {"dataset": DATASET, "status": "backfilled", "n": 5},
            {"dataset": DATASET, "status": "unfillable", "n": 1},
            {"dataset": "test_bf_sibling", "status": "backfilled", "n": 5},
            {"dataset": "test_bf_sibling", "status": "unfillable", "n": 1},
        ]
        assert source.calls == [(date(2026, 10, 5), date(2026, 10, 12))]
        assert get_dataset(DATASET).backfill_job == JOB
    finally:
        unregister_dataset("test_bf_sibling")


def test_backfill_yields_to_waiting_scheduled_run(
    source: Source, conn: Conn, settings: Settings, db: Database, db_url: str
) -> None:
    """F2: while a scheduled run waits for the job lock, backfill tasks of that job defer."""
    seed_gaps(conn)
    waiter = psycopg.connect(db_url, autocommit=True)
    try:
        waiter.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"mdhub.jobwait:{JOB}",)
        )
        summary = work(settings, db)
        assert summary.deferred == 1 and source.calls == []
        task = conn.execute("SELECT status, attempts, last_error FROM ops.backfill_task").fetchone()
        assert task["status"] == "pending" and task["attempts"] == 0
        assert task["last_error"].startswith("lock_busy")
    finally:
        waiter.close()


def test_time_budget_and_max_tasks(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    for d in (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)):
        conn.execute(
            "INSERT INTO ops.backfill_task (dataset, params) VALUES (%s, %s)",
            (DATASET, Jsonb({"date_from": d.isoformat(), "date_to": d.isoformat()})),
        )
    conn.commit()
    assert work(settings, db, time_budget_s=0).processed == 0
    ticks = iter([0.0, 1.0, 100.0, 1000.0])
    res = work(settings, db, time_budget_s=50, clock=lambda: next(ticks))
    assert res.processed == 1
    assert work(settings, db, max_tasks=1).processed == 1
    assert work(settings, db).processed == 1
    assert source.calls == [(date(2026, 9, d), date(2026, 9, d)) for d in (1, 2, 3)]


def test_claim_order_skip_locked_and_stale_reset(conn: Conn, db: Database) -> None:
    for i in range(2):
        conn.execute(
            "INSERT INTO ops.backfill_task (dataset, params) VALUES ('x', %s)",
            (Jsonb({"date_from": f"2026-01-0{i + 1}", "date_to": f"2026-01-0{i + 1}"}),),
        )
    conn.commit()
    with db.connect() as other:  # another worker holds a row lock on the oldest task
        other.execute("SELECT id FROM ops.backfill_task ORDER BY id LIMIT 1 FOR UPDATE").fetchone()
        t = claim_task(db)
        assert t is not None and t.params["date_from"] == "2026-01-02" and t.attempts == 1
        assert claim_task(db) is None
    t1 = claim_task(db)
    assert t1 is not None and t1.params["date_from"] == "2026-01-01"
    conn.execute("UPDATE ops.backfill_task SET started_at = now() - interval '2 hours'")
    conn.commit()
    from mdhub.quality.backfill import reset_stale

    assert reset_stale(db, 60) == 2


def test_unknown_dataset_and_bad_params_fail_permanently(
    conn: Conn, settings: Settings, db: Database
) -> None:
    conn.execute(
        "INSERT INTO ops.backfill_task (dataset, params) VALUES ('nope', %s)",
        (Jsonb({"date_from": "2026-01-01", "date_to": "2026-01-01"}),),
    )
    conn.commit()
    res = work(settings, db)
    assert res.failed == 1
    row = conn.execute("SELECT status, last_error FROM ops.backfill_task").fetchone()
    assert row == {"status": "failed", "last_error": "unknown dataset 'nope'"}


def test_job_without_date_params_fails_permanently(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    REGISTRY.unregister(JOB)
    REGISTRY.register(JOB, "no params", lambda ctx: None)
    conn.execute(
        "INSERT INTO ops.backfill_task (dataset, params) VALUES (%s, %s)",
        (DATASET, Jsonb({"date_from": "2026-10-05", "date_to": "2026-10-05"})),
    )
    conn.commit()
    res = work(settings, db)
    assert res.failed == 1
    row = conn.execute("SELECT status, attempts, last_error FROM ops.backfill_task").fetchone()
    assert (
        row is not None and row["status"] == "failed" and "unknown parameter" in row["last_error"]
    )


def test_lock_contention_defers_without_counting(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    seed_gaps(conn)
    with db.connect(autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"mdhub.job:{JOB}",))
        res = work(settings, db)
    assert res.deferred == 1 and source.calls == []
    row = conn.execute("SELECT status, attempts, not_before FROM ops.backfill_task").fetchone()
    assert row is not None and row["status"] == "pending" and row["attempts"] == 0
    assert row["not_before"] is not None


def test_backfill_worker_job_statuses(
    source: Source, conn: Conn, settings: Settings, db: Database
) -> None:
    idle = run_job("backfill_worker", {}, settings=settings, db=db)
    assert idle.status == "skipped"
    seed_gaps(conn)
    ok = run_job("backfill_worker", {"time_budget_s": "60"}, settings=settings, db=db)
    assert ok.status == "succeeded" and ok.result["done"] == 1
    conn.execute(
        "INSERT INTO ops.backfill_task (dataset, params) VALUES (%s, %s)",
        (DATASET, Jsonb({"date_from": "2026-09-01", "date_to": "2026-09-01"})),
    )
    conn.commit()
    source.fail = True
    bad = run_job("backfill_worker", {}, settings=settings, db=db)
    assert bad.status == "partial" and bad.result["retried"] == 1


# ------------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)


def test_cli_backfill_queue_and_now(source: Source, conn: Conn, cli_env: None) -> None:
    seed_gaps(conn)
    queued = runner.invoke(
        cli.app, ["ops", "backfill", DATASET, "--from", "2026-08-01", "--to", "2026-09-15"]
    )
    assert queued.exit_code == 0, queued.output
    assert "2026-08-01..2026-08-31: task" in queued.output
    assert "2026-09-01..2026-09-15: task" in queued.output
    again = runner.invoke(
        cli.app, ["ops", "backfill", DATASET, "--from", "2026-08-01", "--to", "2026-08-31"]
    )
    assert "already queued" in again.output
    tasks = conn.execute("SELECT source FROM ops.backfill_task WHERE source = 'cli'").fetchall()
    assert len(tasks) == 2

    now = runner.invoke(
        cli.app,
        ["ops", "backfill", DATASET, "--from", "2026-10-05", "--to", "2026-10-12", "--now"],
    )
    assert now.exit_code == 0, now.output
    assert "succeeded" in now.output and "backfilled=5 unfillable=1" in now.output

    gaps = runner.invoke(cli.app, ["ops", "gaps", "--summary"])
    assert gaps.exit_code == 0 and "no open gaps" in gaps.output

    bad = runner.invoke(
        cli.app, ["ops", "backfill", "nope", "--from", "2026-01-01", "--to", "2026-01-02"]
    )
    assert bad.exit_code == 2 and "unknown dataset" in bad.output
    rev = runner.invoke(
        cli.app, ["ops", "backfill", DATASET, "--from", "2026-01-02", "--to", "2026-01-01"]
    )
    assert rev.exit_code == 2


def test_cli_coverage_and_gaps(source: Source, conn: Conn, cli_env: None) -> None:
    empty = runner.invoke(cli.app, ["ops", "coverage"])
    assert empty.exit_code == 0 and "run `mdhub run quality`" in empty.output
    seed_gaps(conn)
    conn.execute(
        """
        INSERT INTO ops.dataset_status (dataset, table_name, frequency, last_date, row_count,
            is_stale)
        VALUES (%s, 'md.bf_fake', 'business_daily', '2026-10-15', 5, false)
        """,
        (DATASET,),
    )
    conn.commit()
    cov = runner.invoke(cli.app, ["ops", "coverage"])
    assert cov.exit_code == 0, cov.output
    assert DATASET in cov.output and "2026-10-15" in cov.output
    assert runner.invoke(cli.app, ["ops", "coverage", "--stale"]).output.startswith("no coverage")
    gaps = runner.invoke(cli.app, ["ops", "gaps", "-d", DATASET])
    assert gaps.exit_code == 0
    assert gaps.output.count("2026-10-") == 6 and "pending" in gaps.output
