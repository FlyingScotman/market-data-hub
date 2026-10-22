"""``eod_final`` input readiness (wait for the final ``bonds_eod`` / ``zcyc`` runs, then publish
with stale reasons) and auto-correction of published ``eod_final`` cuts after a backfill
(``snapshot_rebuild``: a superseding snapshot only when the content changes)."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

import mdhub.loaders.snapshots as snapjobs
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult
from mdhub.datasets import DatasetSpec, register_dataset, unregister_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobRegistry, run_job
from mdhub.quality.backfill import process_tasks, queue_snapshot_corrections
from mdhub.snapshot.readiness import EOD_FINAL_INPUTS, eod_final_cuts, run_covers
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)  # Thursday
TODAY = date(2026, 10, 16)
BONDS, ZCYC = EOD_FINAL_INPUTS


def _wd(d: date) -> bool:
    return d.weekday() < 5


def test_run_covers_rules() -> None:
    nxt = DAY + timedelta(days=1)
    final = {"mode": "final", "days_back": [1, 2]}
    assert run_covers(BONDS, final, nxt, DAY, _wd)
    # started on Saturday: days_back 1 and 2 are Friday and Thursday
    assert run_covers(BONDS, final, nxt + timedelta(days=1), DAY, _wd)
    assert not run_covers(BONDS, {"mode": "final", "days_back": [1]}, DAY, DAY, _wd)
    assert run_covers(BONDS, {}, nxt, DAY, _wd)  # default mode final, days_back [1]
    assert not run_covers(BONDS, {"mode": "prelim", "day": DAY.isoformat()}, nxt, DAY, _wd)
    rng = {"date_from": "2026-10-01", "date_to": "2026-10-20"}
    assert run_covers(BONDS, rng, nxt, DAY, _wd)
    assert not run_covers(BONDS, {"date_from": "2026-10-16"}, nxt, DAY, _wd)
    assert run_covers(ZCYC, {"mode": "final", "days_back": [1]}, nxt, DAY, _wd)
    assert not run_covers(ZCYC, {"mode": "intraday"}, nxt, DAY, _wd)
    assert run_covers(ZCYC, {"mode": "intraday", **rng}, nxt, DAY, _wd)  # backfill run
    assert run_covers(ZCYC, {"mode": "eod", "days_back": [0]}, DAY, DAY, _wd)


@pytest.fixture
def fake_time(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    monkeypatch.setattr(snapjobs, "_sleep", sleep)
    monkeypatch.setattr(snapjobs, "_clock", lambda: now[0])
    monkeypatch.setattr("mdhub.jobs.msk_today", lambda: TODAY)
    return sleeps


def _final_runs(conn: Conn, jobs: tuple[str, ...] = ("bonds_eod", "zcyc")) -> None:
    started = datetime(2026, 10, 16, 7, 10, tzinfo=MSK)
    for job in jobs:
        conn.execute(
            "INSERT INTO ops.job_run (job, status, started_at, finished_at, params) VALUES"
            " (%s, 'succeeded', %s, %s, %s)",
            (job, started, started + timedelta(minutes=3), json.dumps({"mode": "final"})),
        )
    conn.commit()


def _market(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def _stale(doc: dict[str, Any], name: str) -> list[str]:
    entry = next(d for d in doc["datasets"] if d["name"] == name)
    out: list[str] = entry["stale_reasons"]
    return out


def test_eod_final_waits_then_publishes_with_stale_reasons(
    conn: Conn, db: Database, settings: Settings, fake_time: list[float]
) -> None:
    seed(conn)
    _final_runs(conn, ("zcyc",))  # zcyc final ran, bonds_eod final did not
    out = run_job(
        "snapshot_market",
        {"mode": "eod_final", "wait_minutes": 2},
        settings=settings,
        db=db,
        calendar=weekday_calendar,
    )
    assert out.status == "partial"
    assert out.result["inputs_missing"] == ["bonds_eod"]
    assert sum(fake_time) == 120 and len(fake_time) == 4  # polled every 30 s for 2 minutes
    doc = _market(conn, out.result["market"]["id"])
    assert doc["business_date"] == DAY.isoformat()
    assert any(
        "input not ready: no successful final bonds_eod" in r for r in _stale(doc, "bond_quotes")
    )
    assert not any("input not ready" in r for r in _stale(doc, "zcyc_params"))
    err = conn.execute("SELECT error FROM ops.job_run WHERE id = %s", (out.run_id,)).fetchone()
    assert err is not None and "bonds_eod" in err["error"]


def test_eod_final_does_not_wait_when_inputs_are_ready(
    conn: Conn, db: Database, settings: Settings, fake_time: list[float]
) -> None:
    seed(conn)
    _final_runs(conn)
    out = run_job(
        "snapshot_market",
        {"mode": "eod_final"},
        settings=settings,
        db=db,
        calendar=weekday_calendar,
    )
    assert out.status == "succeeded" and out.result["inputs_missing"] == []
    assert fake_time == []
    doc = _market(conn, out.result["market"]["id"])
    assert not any("input not ready" in r for r in _stale(doc, "bond_quotes"))


def test_older_day_is_not_checked(
    conn: Conn, db: Database, settings: Settings, fake_time: list[float]
) -> None:
    seed(conn)
    out = run_job(
        "snapshot_market",
        {"mode": "eod_final", "day": "2026-10-14"},
        settings=settings,
        db=db,
        calendar=weekday_calendar,
    )
    assert out.result["inputs_missing"] == [] and fake_time == []


# ------------------------------------------------------------------- auto-correction

FAKE_JOB = "bonds_eod"  # a snapshot input job (replaced by a fake in a private registry)


def test_backfill_change_queues_and_rebuild_supersedes(
    conn: Conn, db: Database, settings: Settings, fake_time: list[float]
) -> None:
    seed(conn)
    _final_runs(conn)
    first = run_job(
        "snapshot_market",
        {"mode": "eod_final"},
        settings=settings,
        db=db,
        calendar=weekday_calendar,
    )
    first_id = first.result["market"]["id"]
    assert [d for d, _ in eod_final_cuts(conn, DAY, DAY)] == [DAY]

    registry = JobRegistry()
    for name in ("snapshot_rebuild", "snapshot_market"):
        real = REGISTRY.get(name)
        registry.register(real.name, real.description, real.fn)

    def fake_bonds_eod(
        ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
    ) -> None:
        with ctx.connection() as c:
            c.execute(
                "UPDATE md.bond_eod SET close = 97.95, updated_at = now() "
                "WHERE secid = 'SU26242RMFS0' AND trade_date = '2026-10-15' AND board = 'TQOB'"
            )
        ctx.run.add_counts(UpsertResult(updated=1))

    registry.register(FAKE_JOB, "fake bonds_eod backfill: revises one close", fake_bonds_eod)

    spec = DatasetSpec(
        name="test_rb_bonds",
        table="md.bond_eod",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=1,
        backfill_job=FAKE_JOB,
        description="test",
    )
    register_dataset(spec)
    try:
        conn.execute(
            "INSERT INTO ops.backfill_task (dataset, params, source) VALUES"
            ' (\'test_rb_bonds\', \'{"date_from": "2026-10-14", "date_to": "2026-10-15"}\','
            " 'test')"
        )
        conn.commit()
        summary = process_tasks(
            settings=settings, db=db, calendar=weekday_calendar, registry=registry, max_tasks=1
        )
        assert summary.done == 1
        assert summary.tasks[0]["snapshot_rebuilds_queued"] == [["2026-10-15", "2026-10-15"]]
        task = conn.execute(
            "SELECT dataset, params, source FROM ops.backfill_task "
            "WHERE dataset = 'snapshot_eod_final'"
        ).fetchone()
        assert task is not None and task["source"] == "snapshot_autocorrect"
        assert task["params"] == {"date_from": "2026-10-15", "date_to": "2026-10-15"}

        summary = process_tasks(
            settings=settings, db=db, calendar=weekday_calendar, registry=registry, max_tasks=1
        )
        assert summary.done == 1
        current = conn.execute(
            "SELECT id, supersedes FROM ops.published_snapshot WHERE kind = 'market' "
            "AND cut_type = 'eod_final' AND supersedes IS NOT NULL"
        ).fetchone()
        assert current is not None and current["supersedes"] == first_id
        doc = _market(conn, current["id"])
        quote = next(
            q
            for q in doc["data"]["bond_quotes"]
            if q["secid"] == "SU26242RMFS0" and q["board"] == "TQOB"
        )
        assert quote["close_pct"] == "97.95"
        first_doc = _market(conn, first_id)
        assert doc["cut_ts"] == first_doc["cut_ts"] and doc["supersedes"] == first_id

        # rebuilding again with unchanged content publishes nothing
        again = run_job(
            "snapshot_rebuild",
            {"date_from": "2026-10-15", "date_to": "2026-10-15"},
            settings=settings,
            db=db,
            registry=registry,
            calendar=weekday_calendar,
        )
        (entry,) = again.result.values()
        assert entry["market"]["status"] == "unchanged"
        n = conn.execute(
            "SELECT count(*) AS n FROM ops.published_snapshot WHERE cut_type = 'eod_final'"
        ).fetchone()
        assert n == {"n": 2}
    finally:
        unregister_dataset("test_rb_bonds")


def test_no_rebuild_without_changes_or_when_disabled(
    conn: Conn, db: Database, settings: Settings, fake_time: list[float]
) -> None:
    seed(conn)
    _final_runs(conn)
    run_job(
        "snapshot_market",
        {"mode": "eod_final"},
        settings=settings,
        db=db,
        calendar=weekday_calendar,
    )
    spec = DatasetSpec(
        name="test_rb2",
        table="md.bond_eod",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="bonds_eod",
        description="test",
    )
    run = conn.execute(
        "INSERT INTO ops.job_run (job, status, finished_at, rows_inserted, rows_updated) VALUES"
        " ('bonds_eod', 'succeeded', now(), 0, 0), ('bonds_eod', 'succeeded', now(), 3, 0)"
        " RETURNING id"
    ).fetchall()
    conn.commit()
    unchanged_run, changed_run = run[0]["id"], run[1]["id"]
    assert queue_snapshot_corrections(db, settings, spec, DAY, DAY, unchanged_run) == []
    off = settings.model_copy(update={"snapshot_autocorrect": False})
    assert queue_snapshot_corrections(db, off, spec, DAY, DAY, changed_run) == []
    other = DatasetSpec(**{**spec.__dict__, "backfill_job": "algopack_fx_futures"})
    assert queue_snapshot_corrections(db, settings, other, DAY, DAY, changed_run) == []
    # no eod_final in the range
    assert (
        queue_snapshot_corrections(
            db, settings, spec, DAY - timedelta(days=5), DAY - timedelta(days=1), changed_run
        )
        == []
    )
    assert queue_snapshot_corrections(db, settings, spec, DAY, DAY, changed_run) == [(DAY, DAY)]
    # an identical open task is not queued twice
    assert queue_snapshot_corrections(db, settings, spec, DAY, DAY, changed_run) == []
