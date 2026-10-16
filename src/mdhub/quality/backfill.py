"""The ``backfill_worker`` job (DESIGN §6: every 10 minutes, all days).

Each invocation processes pending ``ops.backfill_task`` rows (oldest first, claimed with
``FOR UPDATE SKIP LOCKED``) until its time budget (default 8 minutes, so it does not overlap
the next 10-minute trigger) or ``max_tasks`` is used up. For a task the worker:

1. marks it ``running`` and increments ``attempts`` (committed),
2. runs ``run_job(spec.backfill_job, {"date_from": d1, "date_to": d2})`` in-process - the
   nested run gets its own ``ops.job_run`` row and advisory lock,
3. on success marks the task ``done`` and re-counts the open gaps of the range for every
   dataset sharing the job (:func:`mdhub.quality.checks.reconcile_gaps`): filled days ->
   ``backfilled``, days still empty after a *successful* run -> ``unfillable`` (the source
   has no data, e.g. a holiday missing from ``ref.calendar_day``) once they are older than the
   no-data grace period (:mod:`mdhub.core.gaps`; younger days stay open and are re-queued by
   the next quality run); days before a rolling-window dataset's window (the job clamps its
   range) -> ``not_expected``. A ``partial`` run whose range still has open gaps counts as a failed
   attempt (step 4), so a persistently failing day ends ``failed`` instead of looping; a
   ``partial`` run that filled the range marks the task done,
4. on failure retries with exponential backoff (``not_before`` = now + 10 min * 2^(n-1));
   after ``max_attempts`` (5) the task becomes ``failed`` and the open gaps of the range get a
   note; quality does not re-queue those days for
   :data:`~mdhub.quality.checks.FAILED_TASK_COOLDOWN_DAYS` days,
5. if the nested job was skipped because another run of it holds its lock - or because a
   scheduled run is waiting for that lock (backfills yield to scheduled runs) - the attempt
   is not counted and the task is retried in 5 minutes.

Tasks left ``running`` by a crashed worker for more than ``stale_minutes`` are reset to
``pending`` at the start of each invocation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate
from mdhub.core.http import HttpClient
from mdhub.core.redact import sanitize_text
from mdhub.datasets import DatasetSpec, get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobParamError, JobRegistry, job, run_job
from mdhub.quality.checks import job_datasets, reconcile_gaps

__all__ = [
    "BACKOFF_BASE",
    "MAX_ATTEMPTS",
    "TIME_BUDGET_S",
    "Task",
    "WorkerSummary",
    "backfill_worker",
    "claim_task",
    "process_tasks",
    "run_backfill_range",
]

MAX_ATTEMPTS: Final = 5
TIME_BUDGET_S: Final = 480.0
BACKOFF_BASE: Final = timedelta(minutes=10)
LOCK_RETRY: Final = timedelta(minutes=5)
_ERR_CHARS: Final = 2000


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    dataset: str
    params: dict[str, Any]
    attempts: int


@dataclass(slots=True)
class WorkerSummary:
    processed: int = 0
    done: int = 0
    retried: int = 0
    failed: int = 0
    deferred: int = 0
    gaps_backfilled: int = 0
    gaps_unfillable: int = 0
    reset_stale: int = 0
    tasks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "done": self.done,
            "retried": self.retried,
            "failed": self.failed,
            "deferred": self.deferred,
            "gaps_backfilled": self.gaps_backfilled,
            "gaps_unfillable": self.gaps_unfillable,
            "reset_stale": self.reset_stale,
            "tasks": self.tasks,
        }


def backoff(attempts: int) -> timedelta:
    """Delay before attempt ``attempts + 1`` (10, 20, 40, 80 ... minutes)."""
    factor: int = 2 ** max(attempts - 1, 0)
    return BACKOFF_BASE * factor


def reset_stale(db: Database, stale_minutes: int) -> int:
    with db.connection() as conn:
        cur = conn.execute(
            """
            UPDATE ops.backfill_task SET status = 'pending', not_before = NULL,
                   last_error = coalesce(last_error, 'worker interrupted')
            WHERE status = 'running'
              AND coalesce(started_at, created_at) < now() - make_interval(mins => %s)
            """,
            (stale_minutes,),
        )
        return cur.rowcount


def claim_task(db: Database) -> Task | None:
    """Atomically take the oldest due pending task (``running``, ``attempts + 1``)."""
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            WITH c AS (
                SELECT id FROM ops.backfill_task
                WHERE status = 'pending' AND (not_before IS NULL OR not_before <= now())
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE ops.backfill_task t
            SET status = 'running', attempts = t.attempts + 1, started_at = now(),
                not_before = NULL
            FROM c WHERE t.id = c.id
            RETURNING t.id, t.dataset, t.params, t.attempts
            """
        ).fetchone()
    if row is None:
        return None
    return Task(id=int(row[0]), dataset=str(row[1]), params=dict(row[2]), attempts=int(row[3]))


def _finish(
    db: Database,
    task: Task,
    status: str,
    *,
    error: str | None = None,
    run_id: int | None = None,
    retry_in: timedelta | None = None,
    refund: bool = False,
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE ops.backfill_task SET
                status = %s,
                last_error = %s,
                last_run_id = coalesce(%s, last_run_id),
                done_at = CASE WHEN %s IN ('done', 'failed') THEN now() END,
                not_before = CASE WHEN %s::interval IS NULL THEN NULL
                                  ELSE now() + %s::interval END,
                attempts = CASE WHEN %s THEN greatest(attempts - 1, 0) ELSE attempts END
            WHERE id = %s
            """,
            (
                status,
                error[:_ERR_CHARS] if error else None,
                run_id,
                status,
                retry_in,
                retry_in,
                refund,
                task.id,
            ),
        )


def _note_failed_gaps(
    db: Database, spec: DatasetSpec, d1: date, d2: date, task: Task, error: str
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE ops.data_gap SET note = %s
            WHERE dataset = ANY(%s) AND status = 'open' AND gap_date BETWEEN %s AND %s
            """,
            (
                f"backfill failed after {task.attempts} attempts (task {task.id}): {error[:300]}",
                job_datasets(spec),
                d1,
                d2,
            ),
        )


def _run_error(db: Database, run_id: int | None) -> str:
    if run_id is None:
        return ""
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute("SELECT error FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    return str(row[0]) if row and row[0] else ""


@dataclass(frozen=True, slots=True)
class RangeOutcome:
    status: str
    run_id: int | None
    error: str | None = None
    backfilled: int = 0
    unfillable: int = 0
    remaining_open: int = 0


def _open_gaps(db: Database, datasets: list[str], d1: date, d2: date) -> int:
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            SELECT count(*) FROM ops.data_gap
            WHERE dataset = ANY(%s) AND status = 'open' AND gap_date BETWEEN %s AND %s
            """,
            (datasets, d1, d2),
        ).fetchone()
    return int(row[0]) if row else 0


def run_backfill_range(
    spec: DatasetSpec,
    d1: date,
    d2: date,
    *,
    settings: Settings,
    db: Database,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
    http_factory: Callable[..., HttpClient] = HttpClient,
    today: date | None = None,
) -> RangeOutcome:
    """Run the dataset's backfill job for ``[d1, d2]`` and reconcile the gaps of **every**
    dataset sharing that job (one task serves them all).

    The nested run yields to a scheduled run that is waiting for the job's lock (it is then
    reported as ``skipped``/lock busy and the task is deferred). Exceptions of the job are
    caught and returned as status ``failed``; :class:`~mdhub.jobs.JobParamError` /
    :class:`~mdhub.jobs.UnknownJobError` propagate.
    """
    if not spec.backfill_job:
        raise JobParamError(f"dataset {spec.name} has no backfill_job")
    try:
        outcome = run_job(
            spec.backfill_job,
            {"date_from": d1, "date_to": d2},
            settings=settings,
            db=db,
            registry=registry,
            calendar=calendar,
            http_factory=http_factory,
            yield_to_waiters=True,
            trigger="backfill",
        )
    except JobParamError:
        raise
    except Exception as exc:
        err = sanitize_text(f"{type(exc).__name__}: {exc}", settings.secret_values())
        return RangeOutcome(status="failed", run_id=None, error=err)
    if outcome.status not in ("succeeded", "partial"):
        return RangeOutcome(
            status=outcome.status, run_id=outcome.run_id, error=_run_error(db, outcome.run_id)
        )
    names = job_datasets(spec)
    filled = unfillable = 0
    with db.connection() as conn:
        for name in names:
            try:
                sibling = spec if name == spec.name else get_dataset(name)
            except KeyError:
                continue
            f, u = reconcile_gaps(
                conn,
                sibling,
                d1,
                d2,
                run_id=outcome.run_id,
                mark_unfillable=outcome.status == "succeeded",
                today=today,
                settings=settings,
            )
            filled += f
            unfillable += u
    return RangeOutcome(
        status=outcome.status,
        run_id=outcome.run_id,
        error=_run_error(db, outcome.run_id) if outcome.status == "partial" else None,
        backfilled=filled,
        unfillable=unfillable,
        remaining_open=_open_gaps(db, names, d1, d2),
    )


def _task_range(task: Task) -> tuple[date, date]:
    try:
        d1 = date.fromisoformat(str(task.params["date_from"]))
        d2 = date.fromisoformat(str(task.params["date_to"]))
    except (KeyError, ValueError) as exc:
        raise JobParamError(f"task {task.id}: bad params {task.params!r}") from exc
    if d2 < d1:
        raise JobParamError(f"task {task.id}: date_to before date_from")
    return d1, d2


def process_task(
    task: Task,
    *,
    settings: Settings,
    db: Database,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
    http_factory: Callable[..., HttpClient] = HttpClient,
    max_attempts: int = MAX_ATTEMPTS,
    summary: WorkerSummary | None = None,
) -> str:
    """Process one claimed task; returns the task's new status."""
    summary = summary if summary is not None else WorkerSummary()
    summary.processed += 1
    entry: dict[str, Any] = {"id": task.id, "dataset": task.dataset, "attempt": task.attempts}
    summary.tasks.append(entry)
    try:
        spec = get_dataset(task.dataset)
        d1, d2 = _task_range(task)
        if not spec.backfill_job:
            raise JobParamError(f"dataset {spec.name} has no backfill_job")
    except (KeyError, JobParamError) as exc:
        msg = f"unknown dataset {task.dataset!r}" if isinstance(exc, KeyError) else str(exc)
        _finish(db, task, "failed", error=msg)
        summary.failed += 1
        entry.update(status="failed", error=msg)
        return "failed"

    try:
        res = run_backfill_range(
            spec,
            d1,
            d2,
            settings=settings,
            db=db,
            calendar=calendar,
            registry=registry,
            http_factory=http_factory,
        )
    except JobParamError as exc:  # job does not accept date_from/date_to: permanent
        _finish(db, task, "failed", error=str(exc))
        summary.failed += 1
        entry.update(status="failed", error=str(exc))
        return "failed"
    except Exception as exc:  # e.g. UnknownJobError (loader failed to import): retry
        res = RangeOutcome(status="failed", run_id=None, error=f"{type(exc).__name__}: {exc}")

    entry.update(run_id=res.run_id, job_status=res.status)
    if res.status == "partial" and res.remaining_open:
        # A partial run that left gaps of the range open failed for those days: retry with
        # backoff (attempts count) instead of "done" + re-queue by every quality run.
        summary.gaps_backfilled += res.backfilled
        summary.gaps_unfillable += res.unfillable
        res = RangeOutcome(
            status="partial",
            run_id=res.run_id,
            error=f"partial run left {res.remaining_open} gap(s) open: {res.error or ''}"[
                :_ERR_CHARS
            ],
        )
    elif res.status in ("succeeded", "partial"):
        _finish(db, task, "done", error=res.error, run_id=res.run_id)
        summary.done += 1
        summary.gaps_backfilled += res.backfilled
        summary.gaps_unfillable += res.unfillable
        entry.update(status="done", backfilled=res.backfilled, unfillable=res.unfillable)
        return "done"
    error = res.error or res.status
    lock_busy = res.status == "skipped" and ("already running" in error or "lock_busy" in error)
    if lock_busy:
        _finish(
            db, task, "pending", error=error, run_id=res.run_id, retry_in=LOCK_RETRY, refund=True
        )
        summary.deferred += 1
        entry.update(status="pending", error=error)
        return "pending"
    if task.attempts >= max_attempts:
        _finish(db, task, "failed", error=error, run_id=res.run_id)
        _note_failed_gaps(db, spec, d1, d2, task, error)
        summary.failed += 1
        entry.update(status="failed", error=error)
        return "failed"
    _finish(db, task, "pending", error=error, run_id=res.run_id, retry_in=backoff(task.attempts))
    summary.retried += 1
    entry.update(status="pending", error=error)
    return "pending"


def process_tasks(
    *,
    settings: Settings,
    db: Database,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
    http_factory: Callable[..., HttpClient] = HttpClient,
    time_budget_s: float = TIME_BUDGET_S,
    max_tasks: int = 100,
    max_attempts: int = MAX_ATTEMPTS,
    stale_minutes: int = 60,
    clock: Callable[[], float] = time.monotonic,
) -> WorkerSummary:
    """Claim and process due tasks until the time budget or ``max_tasks`` is used up."""
    summary = WorkerSummary(reset_stale=reset_stale(db, stale_minutes))
    deadline = clock() + time_budget_s
    while summary.processed < max_tasks and clock() < deadline:
        task = claim_task(db)
        if task is None:
            break
        process_task(
            task,
            settings=settings,
            db=db,
            calendar=calendar,
            registry=registry,
            http_factory=http_factory,
            max_attempts=max_attempts,
            summary=summary,
        )
    return summary


@job("backfill_worker", "Process queued ops.backfill_task rows (time-boxed)")
def backfill_worker(
    ctx: JobContext,
    *,
    time_budget_s: float = TIME_BUDGET_S,
    max_tasks: int = 100,
    max_attempts: int = MAX_ATTEMPTS,
    stale_minutes: int = 60,
) -> dict[str, Any]:
    """Process pending backfill tasks for at most ``time_budget_s`` seconds.

    The run is ``partial`` when a task attempt failed in this invocation.
    """
    summary = process_tasks(
        settings=ctx.settings,
        db=ctx.db,
        calendar=ctx.calendar,
        http_factory=ctx.http_factory,
        time_budget_s=time_budget_s,
        max_tasks=max_tasks,
        max_attempts=max_attempts,
        stale_minutes=stale_minutes,
    )
    if summary.processed == 0 and summary.reset_stale == 0:
        ctx.run.mark_skipped("no due backfill tasks")
    elif summary.retried or summary.failed:
        ctx.run.mark_partial(
            f"{summary.retried} task(s) will be retried, {summary.failed} failed permanently"
        )
    ctx.logger.info(
        "backfill_worker: processed=%d done=%d retried=%d failed=%d deferred=%d "
        "backfilled=%d unfillable=%d",
        summary.processed,
        summary.done,
        summary.retried,
        summary.failed,
        summary.deferred,
        summary.gaps_backfilled,
        summary.gaps_unfillable,
    )
    return summary.to_dict()
