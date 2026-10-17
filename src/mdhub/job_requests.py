"""Job runs requested in the admin console (``ops.job_request``, migration 0630), executed by
the worker.

The console inserts a ``queued`` row. :class:`JobRequestPoller` (started by ``mdhub
scheduler``) polls every :data:`POLL_INTERVAL_S` seconds, claims the oldest queued rows with
``FOR UPDATE SKIP LOCKED`` (so two workers never take the same request), marks them
``running`` with its heartbeat instance id and runs each in a small thread pool through
:func:`mdhub.jobs.run_job` with trigger ``admin``: the run lands in ``ops.job_run`` and holds
the job's advisory lock like any other run, so a busy lock ends as ``skipped`` with
``lock_busy``. The request then becomes ``done`` (the run finished, whatever its status other
than failed) or ``failed`` (the run failed, or the job or its parameters are unknown).

Crash recovery: every poll requeues ``running`` requests of other worker instances whose
heartbeat is stopped, older than :data:`STALE_WORKER` or missing; after
:data:`MAX_ATTEMPTS` claims such a request fails instead (a job that kills the worker is not
retried forever).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from mdhub.config import Settings
from mdhub.core.bizday import CalendarPredicate
from mdhub.core.redact import sanitize_text
from mdhub.db.pool import Database
from mdhub.jobs import (
    REGISTRY,
    JobParamError,
    JobRegistry,
    UnknownJobError,
    run_job,
    worker_draining,
)

__all__ = [
    "MAX_ATTEMPTS",
    "POLL_INTERVAL_S",
    "JobRequest",
    "JobRequestPoller",
    "claim_request",
    "recover_requests",
    "run_request",
]

log = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 5.0
MAX_PARALLEL: Final = 4
MAX_ATTEMPTS: Final = 2
STALE_WORKER: Final = timedelta(minutes=3)
_ERR_CHARS: Final = 2000


@dataclass(frozen=True, slots=True)
class JobRequest:
    id: int
    job: str
    params: dict[str, Any]
    attempts: int
    picked_at: datetime


def claim_request(db: Database, worker_instance: uuid.UUID) -> JobRequest | None:
    """Atomically take the oldest queued request (``running``, ``attempts + 1``)."""
    with db.connection() as conn:
        row = conn.execute(
            """
            WITH c AS (
                SELECT id FROM ops.job_request
                WHERE state = 'queued'
                ORDER BY requested_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE ops.job_request r
            SET state = 'running', attempts = r.attempts + 1, picked_at = now(),
                worker_instance = %s
            FROM c WHERE r.id = c.id
            RETURNING r.id, r.job, r.params, r.attempts, r.picked_at
            """,
            (worker_instance,),
        ).fetchone()
    if row is None:
        return None
    return JobRequest(
        id=int(row["id"]),
        job=str(row["job"]),
        params=dict(row["params"]),
        attempts=int(row["attempts"]),
        picked_at=row["picked_at"],
    )


def recover_requests(
    db: Database,
    worker_instance: uuid.UUID | None = None,
    *,
    stale: timedelta = STALE_WORKER,
    max_attempts: int = MAX_ATTEMPTS,
) -> int:
    """Requeue (or, after ``max_attempts`` claims, fail) running requests whose worker is
    gone: heartbeat stopped, older than ``stale`` or missing. ``worker_instance`` (the caller)
    is never treated as gone. Returns the number of requests changed."""
    with db.connection() as conn:
        rows = conn.execute(
            """
            UPDATE ops.job_request r SET
                state = CASE WHEN r.attempts < %(max)s THEN 'queued' ELSE 'failed' END,
                finished_at = CASE WHEN r.attempts < %(max)s THEN NULL ELSE now() END,
                picked_at = NULL,
                worker_instance = NULL,
                error = format('worker stopped while running (attempt %%s of %%s)',
                               r.attempts, %(max)s)
            WHERE r.state = 'running'
              AND r.worker_instance IS DISTINCT FROM %(me)s
              AND NOT EXISTS (
                  SELECT 1 FROM ops.worker_heartbeat h
                  WHERE h.instance_id = r.worker_instance AND h.stopped_at IS NULL
                    AND h.beat_at > now() - %(stale)s
              )
            RETURNING r.id, r.job, r.state
            """,
            {"max": max_attempts, "me": worker_instance, "stale": stale},
        ).fetchall()
    for r in rows:
        log.warning("job request %s (%s) of a stopped worker: %s", r["id"], r["job"], r["state"])
    return len(rows)


def _finish(
    db: Database,
    request: JobRequest,
    state: Literal["done", "failed"],
    *,
    run_id: int | None,
    error: str | None,
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE ops.job_request
            SET state = %s, finished_at = now(), job_run_id = %s, error = %s
            WHERE id = %s AND state = 'running'
            """,
            (state, run_id, error[:_ERR_CHARS] if error else None, request.id),
        )


def _run_of(db: Database, request: JobRequest) -> int | None:
    """The admin run of the request's job started after the claim (run_job raised, so it
    returned no id)."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM ops.job_run
            WHERE job = %s AND trigger = 'admin' AND started_at >= %s
            ORDER BY id DESC LIMIT 1
            """,
            (request.job, request.picked_at),
        ).fetchone()
    return int(row["id"]) if row else None


def run_request(
    request: JobRequest,
    *,
    settings: Settings,
    db: Database,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
) -> str:
    """Execute a claimed request; returns its final state."""
    try:
        outcome = run_job(
            request.job,
            request.params,
            settings=settings,
            db=db,
            registry=registry,
            calendar=calendar,
            exclusive=True,
            trigger="admin",
        )
    except (UnknownJobError, JobParamError) as exc:
        _finish(db, request, "failed", run_id=None, error=str(exc))
        return "failed"
    except Exception as exc:  # recorded as a failed run by run_job
        detail = sanitize_text(f"{type(exc).__name__}: {exc}", settings.secret_values())
        _finish(db, request, "failed", run_id=_run_of(db, request), error=detail)
        return "failed"
    state: Literal["done", "failed"] = "failed" if outcome.status == "failed" else "done"
    _finish(db, request, state, run_id=outcome.run_id, error=None)
    log.info(
        "job request %s: %s run %s %s", request.id, request.job, outcome.run_id, outcome.status
    )
    return state


class JobRequestPoller:
    """Background thread of the worker that claims and runs console requests.

    :param worker_instance: the worker's heartbeat instance id (recorded on claimed rows).
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        calendar: CalendarPredicate,
        worker_instance: uuid.UUID,
        registry: JobRegistry = REGISTRY,
        interval_s: float = POLL_INTERVAL_S,
        max_parallel: int = MAX_PARALLEL,
    ) -> None:
        self.settings = settings
        self.db = db
        self.calendar = calendar
        self.worker_instance = worker_instance
        self.registry = registry
        self.interval_s = interval_s
        self.max_parallel = max_parallel
        self._executor = ThreadPoolExecutor(max_parallel, thread_name_prefix="mdhub-request")
        self._futures: set[Future[str]] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _execute(self, request: JobRequest) -> str:
        return run_request(
            request,
            settings=self.settings,
            db=self.db,
            calendar=self.calendar,
            registry=self.registry,
        )

    def poll_once(self) -> list[Future[str]]:
        """Recover abandoned requests, then claim queued ones up to the free capacity (none
        while ``ops.worker_control.drain`` is set; running requests finish)."""
        try:
            recover_requests(self.db, self.worker_instance)
        except Exception as exc:
            log.warning("job request recovery failed: %s", type(exc).__name__)
        self._futures = {f for f in self._futures if not f.done()}
        started: list[Future[str]] = []
        if worker_draining(self.db):  # an update is about to stop the worker
            return started
        while len(self._futures) < self.max_parallel and not self._stop.is_set():
            try:
                request = claim_request(self.db, self.worker_instance)
            except Exception as exc:
                log.warning("job request poll failed: %s", type(exc).__name__)
                break
            if request is None:
                break
            future = self._executor.submit(self._execute, request)
            self._futures.add(future)
            started.append(future)
        return started

    def _loop(self, wait: Callable[[float], bool]) -> None:
        while True:
            self.poll_once()
            if wait(self.interval_s):
                return

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                args=(self._stop.wait,),
                name="mdhub-job-requests",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stop polling and wait for requests already running."""
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(timeout)
        self._executor.shutdown(wait=True)
