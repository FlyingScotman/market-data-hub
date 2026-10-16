"""Provenance: job runs (``ops.job_run``) and raw captures (``ops.raw_capture``).

``job_run`` records each execution in its **own short transactions** (separate from the
loader's data transaction), so a failed run is recorded even when the data rolls back::

    with job_run(db, "bonds_eod", {"date": "2026-10-15"}, settings=settings) as run:
        with db.connection() as conn:
            cap = record_capture(conn, run.id, "moex_iss", "bonds_history", None, resp, "1")
            run.add_counts(upsert_rows(conn, "md.bond_eod", rows, KEY))

Loaders started through :func:`mdhub.jobs.run_job` get the run as ``ctx.run`` and must not
open another ``job_run`` for the same work.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import socket
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

import httpx
import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.config import Settings
from mdhub.core.errors import JobSkipped
from mdhub.core.redact import redact_url, sanitize_text
from mdhub.core.timeutil import ensure_aware, utc_now
from mdhub.core.upsert import UpsertResult, atomic
from mdhub.db.pool import Database

__all__ = [
    "JOB_TRIGGERS",
    "SCHEDULED_TRIGGERS",
    "JobRun",
    "job_run",
    "json_default",
    "last_capture_sha256",
    "load_capture_payload",
    "reap_orphaned_runs",
    "record_capture",
]

log = logging.getLogger(__name__)

MAX_ERROR_CHARS = 4000

JOB_TRIGGERS: Final = ("schedule", "catch_up", "backfill", "cli", "api", "other")
"""Values of ``ops.job_run.trigger`` (0421): scheduler fire, quality catch-up of a fire skipped
with ``lock_busy``, backfill task run, CLI, read API, anything else."""

SCHEDULED_TRIGGERS: Final = ("schedule", "catch_up")
"""Runs that count as "the scheduled fire ran" for job health (``NULL`` = recorded before 0421
also counts)."""


def json_default(value: Any) -> Any:
    """``json.dumps`` default: dates/datetimes -> ISO, Decimal -> str, sets -> sorted list."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=json_default, ensure_ascii=False))


class JobRun:
    """Handle for an ``ops.job_run`` row; created by :func:`job_run`.

    Attributes: ``id`` (row id, use as ``run_id`` in captures and data rows), ``job``,
    ``params``, ``counts`` (accumulated :class:`UpsertResult`).
    """

    def __init__(self, run_id: int, job: str, params: Mapping[str, Any]) -> None:
        self.id = run_id
        self.job = job
        self.params = dict(params)
        self.counts = UpsertResult()
        self._partial: list[str] = []
        self._skipped: str | None = None

    def add_counts(self, result: UpsertResult) -> None:
        """Accumulate row counts of an upsert into the run totals."""
        self.counts = self.counts + result

    def mark_partial(self, reason: str) -> None:
        """Finish as ``partial`` (some items failed/missing); reasons are joined into ``error``."""
        self._partial.append(reason)

    def mark_skipped(self, reason: str) -> None:
        """Finish as ``skipped`` (nothing to do / source disabled), unless an exception occurs."""
        self._skipped = reason

    @property
    def partial_reasons(self) -> list[str]:
        return list(self._partial)

    def __repr__(self) -> str:
        return f"JobRun(id={self.id}, job={self.job!r})"


@contextmanager
def job_run(
    db: Database,
    job: str,
    params: Mapping[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    trigger: str | None = None,
) -> Iterator[JobRun]:
    """Record a job execution in ``ops.job_run``.

    ``trigger`` (:data:`JOB_TRIGGERS`) records who started the run (``ops.job_run.trigger``):
    job health counts only ``schedule``/``catch_up`` runs as "the scheduled fire ran".

    Inserts a ``running`` row on entry (committed immediately). On exit sets ``finished_at``,
    row counts and the status:

    * ``failed`` - the block raised (exception re-raised; ``error`` = sanitized
      ``"<Type>: <message>"`` with secrets and URL credentials removed),
    * ``skipped`` - the block raised :class:`~mdhub.core.errors.JobSkipped` (not re-raised) or
      called :meth:`JobRun.mark_skipped`,
    * ``partial`` - :meth:`JobRun.mark_partial` was called,
    * ``succeeded`` - otherwise.
    """
    params = dict(params or {})
    if trigger is not None and trigger not in JOB_TRIGGERS:
        raise ValueError(f"unknown job trigger {trigger!r} (one of {', '.join(JOB_TRIGGERS)})")
    secrets = settings.secret_values() if settings is not None else []
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "INSERT INTO ops.job_run (job, params, host, trigger) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (job, _jsonb(params), socket.gethostname(), trigger),
        ).fetchone()
    assert row is not None
    run = JobRun(int(row[0]), job, params)
    log.info("job %s run %d started", job, run.id)
    status = "succeeded"
    error: str | None = None
    try:
        yield run
    except JobSkipped as exc:
        status, error = "skipped", sanitize_text(str(exc) or "skipped", secrets)
    except BaseException as exc:
        status = "failed"
        error = sanitize_text(f"{type(exc).__name__}: {exc}", secrets)[:MAX_ERROR_CHARS]
        _finish(db, run, status, error)
        log.error("job %s run %d failed: %s", job, run.id, error)
        raise
    else:
        if run._skipped is not None:
            status, error = "skipped", sanitize_text(run._skipped, secrets)
        elif run._partial:
            status = "partial"
            error = sanitize_text("; ".join(run._partial), secrets)[:MAX_ERROR_CHARS]
    _finish(db, run, status, error)
    log.info(
        "job %s run %d %s (inserted=%d updated=%d unchanged=%d)%s",
        job,
        run.id,
        status,
        run.counts.inserted,
        run.counts.updated,
        run.counts.unchanged,
        f": {error}" if error else "",
    )


def _finish(db: Database, run: JobRun, status: str, error: str | None) -> None:
    try:
        with db.connection() as conn:
            conn.execute(
                """
                UPDATE ops.job_run
                SET status = %s, finished_at = clock_timestamp(), error = %s,
                    rows_inserted = %s, rows_updated = %s, rows_unchanged = %s
                WHERE id = %s
                """,
                (
                    status,
                    error,
                    run.counts.inserted,
                    run.counts.updated,
                    run.counts.unchanged,
                    run.id,
                ),
            )
    except psycopg.Error:
        log.exception("could not finalize job_run %d (status %s)", run.id, status)
        if status != "failed":
            raise


ORPHAN_RUN_HOURS: Final = 12
"""A ``running`` job run older than this is considered orphaned (its process died)."""


def reap_orphaned_runs(db: Database, *, older_than_hours: float = ORPHAN_RUN_HOURS) -> int:
    """Mark ``running`` job runs started more than ``older_than_hours`` ago as ``failed``
    (process crashed / killed: nothing will ever finish them). Runs that still hold their
    job's advisory lock are left alone. Returns the number of runs changed."""
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            """
            UPDATE ops.job_run r
            SET status = 'failed', finished_at = clock_timestamp(),
                error = format('orphaned: still running after %%s h (process ended without '
                               'finishing the run); reaped', %s::text)
            WHERE r.status = 'running'
              AND r.started_at < now() - make_interval(secs => %s * 3600)
              AND NOT EXISTS (
                  SELECT 1 FROM pg_locks l
                  WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
                    AND ((l.classid::bigint << 32) | l.objid::bigint)
                        = hashtextextended('mdhub.job:' || r.job, 0)
              )
            """,
            (f"{older_than_hours:g}", older_than_hours),
        )
        n = cur.rowcount
    if n:
        log.warning("reaped %d orphaned running job run(s)", n)
    return n


def record_capture(
    conn: psycopg.Connection[Any],
    run_id: int | None,
    source: str,
    endpoint: str,
    url: str | httpx.URL | None,
    response_or_bytes: httpx.Response | bytes | str,
    parser_version: str,
    source_ts: datetime | None = None,
    content_type: str | None = None,
    http_status: int | None = None,
    *,
    fetched_at: datetime | None = None,
) -> int:
    """Store a fetched payload in ``ops.raw_capture`` and return its ``capture_id``.

    :param run_id: ``ops.job_run.id`` (``ctx.run.id``).
    :param source: ``ops.source.code``, e.g. ``"moex_iss"``.
    :param endpoint: stable logical endpoint name (e.g. ``"bonds_history"``), used for
        de-duplication and retention; not the URL.
    :param url: request URL - redacted before storage; ``None`` takes it from the response.
    :param response_or_bytes: an ``httpx.Response`` (body, status, content type and URL are
        taken from it) or raw ``bytes``/``str`` (str is stored UTF-8 encoded).
    :param parser_version: version string of the parser that will read this payload.
    :param source_ts: the source's own clock for the payload, aware datetime.
    :param fetched_at: when the payload was received (default: now, UTC).

    The payload is gzip-compressed (deterministic, ``mtime=0``); ``sha256`` is computed over
    the uncompressed bytes. Runs in a savepoint of the caller's transaction, so the capture
    commits/rolls back together with the rows parsed from it.
    """
    if isinstance(response_or_bytes, httpx.Response):
        payload = response_or_bytes.content
        if url is None:
            url = response_or_bytes.request.url
        if content_type is None:
            content_type = response_or_bytes.headers.get("content-type")
        if http_status is None:
            http_status = response_or_bytes.status_code
    elif isinstance(response_or_bytes, str):
        payload = response_or_bytes.encode("utf-8")
    else:
        payload = bytes(response_or_bytes)
    if url is None:
        raise ValueError("url is required when passing raw bytes")
    digest = hashlib.sha256(payload).hexdigest()
    compressed = gzip.compress(payload, compresslevel=6, mtime=0)
    when = ensure_aware(fetched_at) if fetched_at is not None else utc_now()
    src_ts = ensure_aware(source_ts) if source_ts is not None else None
    with atomic(conn), conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            INSERT INTO ops.raw_capture (run_id, source, endpoint, url_redacted, http_status,
                content_type, fetched_at, source_ts, parser_version, sha256, payload_gz,
                size_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sha256, endpoint, fetched_at) DO UPDATE SET sha256 = EXCLUDED.sha256
            RETURNING id
            """,
            (
                run_id,
                source,
                endpoint,
                redact_url(url),
                http_status,
                content_type,
                when,
                src_ts,
                parser_version,
                digest,
                compressed,
                len(payload),
            ),
        ).fetchone()
    assert row is not None
    return int(row[0])


def load_capture_payload(conn: psycopg.Connection[Any], capture_id: int) -> bytes:
    """Return the decompressed payload of a capture (for re-parsing from raw)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT payload_gz, sha256 FROM ops.raw_capture WHERE id = %s", (capture_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"capture {capture_id} not found")
    payload = gzip.decompress(bytes(row[0]))
    if hashlib.sha256(payload).hexdigest() != row[1]:
        raise ValueError(f"capture {capture_id}: payload checksum mismatch")
    return payload


def last_capture_sha256(conn: psycopg.Connection[Any], source: str, endpoint: str) -> str | None:
    """SHA-256 of the most recent capture for ``(source, endpoint)`` - for "skip identical
    content" logic (e.g. Ratescalcs snapshots)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            SELECT sha256 FROM ops.raw_capture
            WHERE source = %s AND endpoint = %s
            ORDER BY fetched_at DESC, id DESC LIMIT 1
            """,
            (source, endpoint),
        ).fetchone()
    return str(row[0]) if row else None
