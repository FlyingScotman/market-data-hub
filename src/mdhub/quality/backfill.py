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
   After a done task (not one of a history load, whose reissue is a single pass of
   ``snapshot_heal``) whose run changed rows of a job that feeds the ``eod_final`` cut
   (:data:`mdhub.snapshot.readiness.SNAPSHOT_INPUT_JOBS`), the published ``eod_final`` cuts of
   the business dates in the range are queued for a rebuild (dataset ``snapshot_eod_final``,
   job ``snapshot_rebuild``; ``MDHUB_SNAPSHOT_AUTOCORRECT``): a cut whose content changed is
   corrected by a superseding snapshot, an unchanged one publishes nothing,
4. on failure retries with exponential backoff (``not_before`` = now + 10 min * 2^(n-1));
   after ``max_attempts`` (5) the task becomes ``failed`` and the open gaps of the range get a
   note; quality does not re-queue those days for
   :data:`~mdhub.quality.checks.FAILED_TASK_COOLDOWN_DAYS` days,
5. if the nested job was skipped because another run of it holds its lock - or because a
   scheduled run is waiting for that lock (backfills yield to scheduled runs) - the attempt
   is not counted and the task is retried in 5 minutes.

Tasks of a history load (source ``history_load``, :mod:`mdhub.quality.history_load`) are taken only
while the load is switched on (``MDHUB_SNAPSHOT_HEAL_HISTORY``), inside the history-load window
(``MDHUB_HISTORY_LOAD_WINDOW``, default 00:30-06:00, after the MOEX evening session) and at most
``MDHUB_HISTORY_LOAD_MAX_TASKS_PER_NIGHT`` per window (counted by ``started_at``; a deferral
gives its slot back), so a history of years never runs in trading hours or the evening session and
cannot starve scheduled or minute jobs through the job lock; they also
come after the tasks of every other source. When such a task's job was skipped because its source
is not configured (:data:`mdhub.core.errors.NOT_CONFIGURED`), the task is not a failed attempt: it
stays ``pending`` (attempt refunded, tried again after :data:`NOT_CONFIGURED_RETRY`, its siblings
of the job with it) and the history load shows that it waits for the source.

Tasks left ``running`` by a crashed worker for more than ``stale_minutes`` are reset to
``pending`` at the start of each invocation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.config import Settings, clock_window_start, parse_clock_window
from mdhub.core.bizday import CalendarPredicate
from mdhub.core.errors import NOT_CONFIGURED
from mdhub.core.http import HttpClient
from mdhub.core.redact import sanitize_text, stored_text
from mdhub.core.timeutil import utc_now
from mdhub.datasets import DatasetSpec, get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, JobParamError, JobRegistry, job, run_job
from mdhub.quality.checks import compress_ranges, job_datasets, queue_range, reconcile_gaps
from mdhub.quality.history_load import HISTORY_SOURCE
from mdhub.snapshot.readiness import EOD_FINAL_DATASET, SNAPSHOT_INPUT_JOBS, eod_final_cuts

__all__ = [
    "BACKOFF_BASE",
    "MAX_ATTEMPTS",
    "TIME_BUDGET_S",
    "Task",
    "WorkerSummary",
    "backfill_worker",
    "claim_task",
    "history_slots",
    "process_tasks",
    "queue_snapshot_corrections",
    "run_backfill_range",
]

MAX_ATTEMPTS: Final = 5
TIME_BUDGET_S: Final = 480.0
BACKOFF_BASE: Final = timedelta(minutes=10)
LOCK_RETRY: Final = timedelta(minutes=5)
NOT_CONFIGURED_RETRY: Final = timedelta(hours=6)
"""A history-load task whose source is not configured is looked at again after this (in practice:
the next night window, the only time such tasks are taken)."""
_ERR_CHARS: Final = 2000


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    dataset: str
    params: dict[str, Any]
    attempts: int
    source: str = "quality"
    """Who queued it (``ops.backfill_task.source``)."""


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


# indirection for tests (a fixed clock)
_now: Callable[[], datetime] = utc_now


def history_slots(db: Database, settings: Settings, now: datetime) -> int:
    """How many more tasks of a history load may be taken now: 0 while the load is switched off
    (``MDHUB_SNAPSHOT_HEAL_HISTORY`` false, or ``MDHUB_SNAPSHOT_HEAL`` not ``repair``: switching it
    off stops a load that has started), 0 outside the history-load window
    (``MDHUB_HISTORY_LOAD_WINDOW``, local time of ``MDHUB_TZ``; not the window of the cuts), else
    the nightly cap (``MDHUB_HISTORY_LOAD_MAX_TASKS_PER_NIGHT``)
    minus the tasks already started in this window."""
    if not settings.snapshot_heal_history or settings.snapshot_heal != "repair":
        return 0
    start = clock_window_start(
        now.astimezone(settings.zone), parse_clock_window(settings.history_load_window)
    )
    if start is None:
        return 0
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT count(*) FROM ops.backfill_task WHERE source = %s AND started_at >= %s",
            (HISTORY_SOURCE, start),
        ).fetchone()
    return max(0, settings.history_load_max_tasks_per_night - (int(row[0]) if row else 0))


def claim_task(db: Database, *, history: bool = True) -> Task | None:
    """Atomically take the oldest due pending task (``running``, ``attempts + 1``); the tasks of a
    history load come last and only when ``history`` (:func:`history_slots`)."""
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            WITH c AS (
                SELECT id FROM ops.backfill_task
                WHERE status = 'pending' AND (not_before IS NULL OR not_before <= now())
                  AND (%s OR source <> 'history_load')
                ORDER BY (source = 'history_load'), created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE ops.backfill_task t
            SET status = 'running', attempts = t.attempts + 1, started_at = now(),
                not_before = NULL
            FROM c WHERE t.id = c.id
            RETURNING t.id, t.dataset, t.params, t.attempts, t.source
            """,
            (history,),
        ).fetchone()
    if row is None:
        return None
    return Task(
        id=int(row[0]),
        dataset=str(row[1]),
        params=dict(row[2]),
        attempts=int(row[3]),
        source=str(row[4]),
    )


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
                attempts = CASE WHEN %s THEN greatest(attempts - 1, 0) ELSE attempts END,
                started_at = CASE WHEN %s THEN NULL ELSE started_at END
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
        return RangeOutcome(status="failed", run_id=None, error=stored_text(err, settings))
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


def queue_snapshot_corrections(
    db: Database, settings: Settings, spec: DatasetSpec, d1: date, d2: date, run_id: int | None
) -> list[tuple[date, date]]:
    """Queue rebuilds of the published ``eod_final`` cuts of ``[d1, d2]`` after a backfill run
    of an input job that changed rows (module doc, step 3). Returns the queued ranges."""
    if (
        not settings.snapshot_autocorrect
        or run_id is None
        or spec.backfill_job not in SNAPSHOT_INPUT_JOBS
    ):
        return []
    try:
        target = get_dataset(EOD_FINAL_DATASET)
    except KeyError:  # mdhub.loaders.snapshots not loaded
        return []
    with db.connection() as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            row = cur.execute(
                "SELECT coalesce(rows_inserted, 0) + coalesce(rows_updated, 0) "
                "FROM ops.job_run WHERE id = %s",
                (run_id,),
            ).fetchone()
        if row is None or not row[0]:
            return []
        days = sorted({d for d, _ in eod_final_cuts(conn, d1, d2)})
        queued: list[tuple[date, date]] = []
        for lo, hi in compress_ranges(days, lambda _d: False, target.max_task_days or 5):
            for task_id, a, b in queue_range(conn, target, lo, hi, source="snapshot_autocorrect"):
                if task_id is not None:
                    queued.append((a, b))
    return queued


def _task_range(task: Task) -> tuple[date, date]:
    try:
        d1 = date.fromisoformat(str(task.params["date_from"]))
        d2 = date.fromisoformat(str(task.params["date_to"]))
    except (KeyError, ValueError) as exc:
        raise JobParamError(f"task {task.id}: bad params {task.params!r}") from exc
    if d2 < d1:
        raise JobParamError(f"task {task.id}: date_to before date_from")
    return d1, d2


def _wait_for_source(
    db: Database, task: Task, spec: DatasetSpec, error: str, run_id: int | None
) -> None:
    """Keep a history-load task whose source is not configured: ``pending``, attempt and night slot
    given back, tried again after :data:`NOT_CONFIGURED_RETRY`; the other pending history tasks of
    the same job wait with it (one skipped run per job and night instead of one per task)."""
    _finish(
        db,
        task,
        "pending",
        error=error,
        run_id=run_id,
        retry_in=NOT_CONFIGURED_RETRY,
        refund=True,
    )
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE ops.backfill_task SET not_before = now() + %s::interval, last_error = %s
            WHERE source = %s AND status = 'pending' AND dataset = ANY(%s) AND id <> %s
            """,
            (NOT_CONFIGURED_RETRY, error[:_ERR_CHARS], HISTORY_SOURCE, job_datasets(spec), task.id),
        )


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
        msg = stored_text(msg, settings)
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
        msg = stored_text(str(exc), settings)
        _finish(db, task, "failed", error=msg)
        summary.failed += 1
        entry.update(status="failed", error=msg)
        return "failed"
    except Exception as exc:  # e.g. UnknownJobError (loader failed to import): retry
        text = sanitize_text(f"{type(exc).__name__}: {exc}", settings.secret_values())
        res = RangeOutcome(status="failed", run_id=None, error=text)
    # Every sink below (ops.backfill_task.last_error, ops.data_gap.note, the summary) is readable
    # by the read-only role and the notes by the read API (/v1/gaps): the configured address of a
    # source is replaced by a placeholder in the text once, here; the log has the full text.
    if res.error is not None:
        res = replace(res, error=stored_text(res.error, settings))

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
        # the history load of a new dataset is followed by one reissue pass of the cuts
        # (snapshot_heal), not by a rebuild per task
        corrections = (
            []
            if task.source == HISTORY_SOURCE
            else queue_snapshot_corrections(db, settings, spec, d1, d2, res.run_id)
        )
        if corrections:
            entry["snapshot_rebuilds_queued"] = [
                [a.isoformat(), b.isoformat()] for a, b in corrections
            ]
        return "done"
    error = res.error or res.status
    lock_busy = res.status == "skipped" and ("already running" in error or "lock_busy" in error)
    if (
        task.source == HISTORY_SOURCE
        and res.status == "skipped"
        and error.startswith(NOT_CONFIGURED)
    ):
        # not a failed attempt: the history waits until the source is configured
        _wait_for_source(db, task, spec, error, res.run_id)
        summary.deferred += 1
        entry.update(status="pending", error=error)
        return "pending"
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
        task = claim_task(db, history=history_slots(db, settings, _now()) > 0)
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
