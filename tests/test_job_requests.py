"""Console job requests executed by the worker (ops.job_request, mdhub.job_requests):
claiming without double runs, trigger 'admin', lock_busy, failures, crash recovery, poller."""

from __future__ import annotations

import uuid
from concurrent.futures import wait
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.db.pool import Database
from mdhub.job_requests import (
    JobRequestPoller,
    claim_request,
    recover_requests,
    run_request,
)
from mdhub.jobs import JobContext, JobRegistry

Conn = psycopg.Connection[DictRow]


@pytest.fixture
def registry() -> JobRegistry:
    reg = JobRegistry()

    def ok_job(ctx: JobContext) -> dict[str, Any]:
        return {"ran": ctx.job}

    def boom_job(ctx: JobContext) -> None:
        raise RuntimeError("source unreachable")

    reg.register("ok_job", "succeeds", ok_job)
    reg.register("boom_job", "fails", boom_job)
    return reg


def _queue(conn: Conn, job: str, **cols: Any) -> int:
    names = ["job", *cols]
    row = conn.execute(
        f"INSERT INTO ops.job_request ({', '.join(names)}) "
        f"VALUES ({', '.join(['%s'] * len(names))}) RETURNING id",
        (job, *cols.values()),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row["id"])


def _request(conn: Conn, request_id: int) -> dict[str, Any]:
    conn.rollback()
    row = conn.execute("SELECT * FROM ops.job_request WHERE id = %s", (request_id,)).fetchone()
    assert row is not None
    return dict(row)


def _beat(conn: Conn, instance: uuid.UUID, age: str = "0 seconds", stopped: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO ops.worker_heartbeat (instance_id, process, host, pid, version, started_at,
                                          beat_at, stopped_at)
        VALUES (%s, 'worker', 'h', 1, 'v', now(), now() - %s::interval,
                CASE WHEN %s THEN now() END)
        """,
        (instance, age, stopped),
    )
    conn.commit()


def test_claim_takes_each_request_once(db: Database, conn: Conn) -> None:
    first = _queue(conn, "ok_job")
    second = _queue(conn, "boom_job")
    a, b = uuid.uuid4(), uuid.uuid4()
    # Worker A is mid-claim on the oldest request (row locked): B skips it, takes the next.
    locker = psycopg.connect(db.conninfo)
    try:
        locker.execute("SELECT id FROM ops.job_request WHERE id = %s FOR UPDATE", (first,))
        got_b = claim_request(db, b)
        assert got_b is not None and got_b.id == second
    finally:
        locker.close()
    got_a = claim_request(db, a)
    assert got_a is not None and got_a.id == first and got_a.attempts == 1
    assert claim_request(db, a) is None and claim_request(db, b) is None
    row = _request(conn, first)
    assert row["state"] == "running" and row["worker_instance"] == a and row["picked_at"]


def test_run_request_records_admin_run(
    db: Database, conn: Conn, settings: Settings, registry: JobRegistry
) -> None:
    rid = _queue(conn, "ok_job")
    req = claim_request(db, uuid.uuid4())
    assert req is not None
    state = run_request(req, settings=settings, db=db, calendar=weekday_calendar, registry=registry)
    assert state == "done"
    row = _request(conn, rid)
    run = conn.execute(
        "SELECT status, trigger FROM ops.job_run WHERE id = %s", (row["job_run_id"],)
    ).fetchone()
    assert run == {"status": "succeeded", "trigger": "admin"}
    assert row["state"] == "done" and row["finished_at"] is not None and row["error"] is None


def test_busy_lock_ends_as_skipped_run(
    db: Database, conn: Conn, settings: Settings, registry: JobRegistry
) -> None:
    rid = _queue(conn, "ok_job")
    holder = psycopg.connect(db.conninfo, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:ok_job', 0))")
        req = claim_request(db, uuid.uuid4())
        assert req is not None
        assert (
            run_request(req, settings=settings, db=db, calendar=weekday_calendar, registry=registry)
            == "done"
        )
    finally:
        holder.close()
    row = _request(conn, rid)
    run = conn.execute(
        "SELECT status, trigger, error FROM ops.job_run WHERE id = %s", (row["job_run_id"],)
    ).fetchone()
    assert run is not None and run["status"] == "skipped" and run["trigger"] == "admin"
    assert run["error"].startswith("lock_busy")


def test_failed_and_unknown_jobs(
    db: Database, conn: Conn, settings: Settings, registry: JobRegistry
) -> None:
    boom = _queue(conn, "boom_job")
    req = claim_request(db, uuid.uuid4())
    assert req is not None
    assert (
        run_request(req, settings=settings, db=db, calendar=weekday_calendar, registry=registry)
        == "failed"
    )
    row = _request(conn, boom)
    assert row["state"] == "failed" and "source unreachable" in row["error"]
    run = conn.execute(
        "SELECT status FROM ops.job_run WHERE id = %s", (row["job_run_id"],)
    ).fetchone()
    assert run == {"status": "failed"}

    unknown = _queue(conn, "no_such_job")
    req = claim_request(db, uuid.uuid4())
    assert req is not None
    assert (
        run_request(req, settings=settings, db=db, calendar=weekday_calendar, registry=registry)
        == "failed"
    )
    row = _request(conn, unknown)
    assert row["state"] == "failed" and row["job_run_id"] is None and "no_such_job" in row["error"]

    bad = _queue(conn, "ok_job", params=psycopg.types.json.Jsonb({"nope": 1}))
    req = claim_request(db, uuid.uuid4())
    assert req is not None
    assert (
        run_request(req, settings=settings, db=db, calendar=weekday_calendar, registry=registry)
        == "failed"
    )
    assert _request(conn, bad)["job_run_id"] is None


def test_one_open_request_per_job(conn: Conn) -> None:
    _queue(conn, "ok_job")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _queue(conn, "ok_job")
    conn.rollback()


def test_crash_recovery_requeues_requests_of_dead_workers(db: Database, conn: Conn) -> None:
    me, live, stale, stopped, missing = (uuid.uuid4() for _ in range(5))
    _beat(conn, me)
    _beat(conn, live)
    _beat(conn, stale, age="10 minutes")
    _beat(conn, stopped, stopped=True)
    jobs = {}
    for name, instance, attempts in (
        ("mine", me, 1),
        ("live", live, 1),
        ("stale", stale, 1),
        ("stopped", stopped, 1),
        ("missing", missing, 1),
        ("exhausted", stale, 2),
    ):
        jobs[name] = _queue(
            conn, name, state="running", worker_instance=instance, attempts=attempts
        )
    assert recover_requests(db, me) == 4
    states = {name: _request(conn, rid)["state"] for name, rid in jobs.items()}
    assert states == {
        "mine": "running",
        "live": "running",
        "stale": "queued",
        "stopped": "queued",
        "missing": "queued",
        "exhausted": "failed",
    }
    requeued = _request(conn, jobs["stale"])
    assert requeued["worker_instance"] is None and "worker stopped" in requeued["error"]
    assert _request(conn, jobs["exhausted"])["finished_at"] is not None
    # A requeued request is claimed again (attempt 2).
    again = claim_request(db, me)
    assert again is not None and again.attempts == 2


def test_poller_runs_queued_requests(
    db: Database, conn: Conn, settings: Settings, registry: JobRegistry
) -> None:
    rid = _queue(conn, "ok_job")
    poller = JobRequestPoller(
        settings, db, calendar=weekday_calendar, worker_instance=uuid.uuid4(), registry=registry
    )
    try:
        futures = poller.poll_once()
        assert len(futures) == 1
        wait(futures, timeout=30)
        assert futures[0].result() == "done"
        assert poller.poll_once() == []
    finally:
        poller.stop()
    assert _request(conn, rid)["state"] == "done"
