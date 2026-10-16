"""The ``maintenance`` job (DESIGN §6): nightly backup and raw-capture retention.

``mode="backup"`` (02:00, ``keep=14``)
    ``pg_dump --format=custom`` of the whole database into ``MDHUB_BACKUP_DIR`` (default
    ``/var/backups/mdhub``) as ``mdhub-<db>-<UTC timestamp>.dump`` (written to ``.partial``
    first, renamed when complete), then deletes all but the newest ``keep`` dumps. The
    password is passed to ``pg_dump`` via ``PGPASSWORD``, never on the command line. The
    ``pg_dump`` major version must match the server (16).

``mode="retention"`` (03:00)
    Deletes ``ops.raw_capture`` rows of **intraday** endpoints older than
    ``MDHUB_RAW_RETENTION_DAYS_INTRADAY`` days (default 90) in batches, then runs
    ``VACUUM (ANALYZE)`` (autocommit, outside a transaction) on ``ops.raw_capture`` and every
    table in ``ops``/``ref``/``md`` larger than ``vacuum_min_mb``. EOD/static raw is kept
    forever.

Intraday classification convention (loaders must follow it): the ``endpoint`` of an
intraday capture starts with :data:`INTRADAY_ENDPOINT_PREFIX` (``"intraday:"``), e.g.
``"intraday:bonds_marketdata"``, ``"intraday:cets_marketdata"``,
``"intraday:futures_marketdata"``. Tables that reference intraday captures must declare
``capture_id bigint REFERENCES ops.raw_capture (id) ON DELETE SET NULL`` (or no FK).
Captures referenced through any other foreign key (``NO ACTION`` or a ``NOT NULL`` column,
e.g. ``md.zcyc_reject``) are skipped by retention instead of failing it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row

from mdhub.core.capture import reap_orphaned_runs
from mdhub.core.redact import sanitize_text
from mdhub.db.pool import Database
from mdhub.jobs import JobContext, JobParamError, job

__all__ = [
    "BACKUP_DIR_ENV",
    "DEFAULT_BACKUP_DIR",
    "INTRADAY_ENDPOINT_PREFIX",
    "BackupResult",
    "backup_command",
    "backup_dir",
    "blocking_capture_refs",
    "is_intraday_endpoint",
    "maintenance",
    "purge_intraday_raw",
    "restore_command",
    "rotate_backups",
    "run_backup",
    "vacuum_tables",
]

INTRADAY_ENDPOINT_PREFIX: Final = "intraday:"
"""``ops.raw_capture.endpoint`` prefix marking intraday captures (subject to retention)."""

BACKUP_DIR_ENV: Final = "MDHUB_BACKUP_DIR"
DEFAULT_BACKUP_DIR: Final = "/var/backups/mdhub"
BACKUP_PREFIX: Final = "mdhub-"
BACKUP_SUFFIX: Final = ".dump"
PG_DUMP_TIMEOUT_S: Final = 4 * 3600
DELETE_BATCH: Final = 5000
VACUUM_SCHEMAS: Final = ("ops", "ref", "md")


def is_intraday_endpoint(endpoint: str) -> bool:
    return endpoint.startswith(INTRADAY_ENDPOINT_PREFIX)


def backup_dir(explicit: str | None = None) -> Path:
    """``explicit`` or ``$MDHUB_BACKUP_DIR`` or :data:`DEFAULT_BACKUP_DIR`."""
    return Path(explicit or os.environ.get(BACKUP_DIR_ENV) or DEFAULT_BACKUP_DIR)


def _split_password(conninfo: str) -> tuple[str, str | None]:
    params = conninfo_to_dict(conninfo)
    password = params.pop("password", None)
    clean = make_conninfo(**{k: str(v) for k, v in params.items() if v is not None})
    return clean, (str(password) if password is not None else None)


def _pg_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} not found on PATH (install postgresql-client-16)")
    return path


def backup_command(conninfo: str, target: Path) -> tuple[list[str], dict[str, str]]:
    """``(argv, extra_env)`` for ``pg_dump`` writing a custom-format dump to ``target``."""
    clean, password = _split_password(conninfo)
    argv = [
        _pg_tool("pg_dump"),
        "--format=custom",
        "--compress=6",
        "--no-password",
        f"--file={target}",
        f"--dbname={clean}",
    ]
    return argv, ({"PGPASSWORD": password} if password else {})


def restore_command(
    conninfo: str, dump: Path, *, clean: bool = False, jobs: int = 1
) -> tuple[list[str], dict[str, str]]:
    """``(argv, extra_env)`` for ``pg_restore`` of a custom-format dump.

    Ownership and grants are not restored (``--no-owner --no-privileges``): objects end up
    owned by the connecting role; re-apply read-only grants afterwards
    (``deploy/sql/grant_readonly.sql``).
    """
    if jobs < 1:
        raise ValueError("jobs must be >= 1")
    clean_conn, password = _split_password(conninfo)
    argv = [
        _pg_tool("pg_restore"),
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--no-password",
        f"--dbname={clean_conn}",
    ]
    if clean:
        argv += ["--clean", "--if-exists"]
    argv += [f"--jobs={jobs}"] if jobs > 1 else ["--single-transaction"]
    argv.append(str(dump))
    return argv, ({"PGPASSWORD": password} if password else {})


def _run_command(
    argv: list[str], extra_env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **extra_env}
    return subprocess.run(  # noqa: S603 - argv built from fixed tool paths, no shell
        argv, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def run_command(
    argv: list[str], extra_env: dict[str, str], *, timeout: float, secrets: list[str]
) -> None:
    """Run a PostgreSQL client tool; raise ``RuntimeError`` with sanitized stderr on error."""
    proc = _run_command(argv, extra_env, timeout)
    if proc.returncode != 0:
        err = sanitize_text((proc.stderr or "").strip()[-2000:], secrets)
        raise RuntimeError(f"{Path(argv[0]).name} exited with {proc.returncode}: {err}")


def rotate_backups(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` ``mdhub-*.dump`` files; returns deleted paths."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    dumps = sorted(
        (p for p in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    removed = dumps[keep:]
    for p in removed:
        p.unlink(missing_ok=True)
    return removed


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    size_bytes: int
    removed: list[Path]


def run_backup(
    db: Database,
    *,
    directory: Path,
    keep: int,
    secrets: list[str],
    now: datetime | None = None,
) -> BackupResult:
    """Dump the database to ``directory`` and rotate old dumps."""
    if keep < 1:
        raise JobParamError("keep must be >= 1")
    directory.mkdir(parents=True, exist_ok=True)
    dbname = conninfo_to_dict(db.conninfo).get("dbname") or "db"
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = directory / f"{BACKUP_PREFIX}{dbname}-{stamp}{BACKUP_SUFFIX}"
    partial = final.with_name(final.name + ".partial")
    argv, env = backup_command(db.conninfo, partial)
    try:
        run_command(argv, env, timeout=PG_DUMP_TIMEOUT_S, secrets=secrets)
        if not partial.exists() or partial.stat().st_size == 0:
            raise RuntimeError("pg_dump produced no output")
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    removed = rotate_backups(directory, keep)
    return BackupResult(path=final, size_bytes=final.stat().st_size, removed=removed)


def blocking_capture_refs(conn: psycopg.Connection[Any]) -> list[tuple[str, str, str]]:
    """``(schema, table, column)`` of every foreign key to ``ops.raw_capture`` that would block
    (or be violated by) deleting a referenced capture: not ``ON DELETE SET NULL``/``CASCADE``,
    or a ``NOT NULL`` column (e.g. ``md.zcyc_reject.capture_id``, the primary key)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT n.nspname, c.relname, a.attname
            FROM pg_constraint k
            JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = k.conkey[1]
            WHERE k.contype = 'f' AND k.confrelid = 'ops.raw_capture'::regclass
              AND cardinality(k.conkey) = 1
              AND (k.confdeltype NOT IN ('n', 'c') OR a.attnotnull)
            ORDER BY 1, 2, 3
            """
        ).fetchall()
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def purge_intraday_raw(db: Database, retention_days: int, *, batch: int = DELETE_BATCH) -> int:
    """Delete intraday raw captures older than ``retention_days``; returns rows deleted.

    Captures still referenced through a foreign key that does not null itself on delete
    (:func:`blocking_capture_refs`) are kept, so retention never fails on, e.g., a rejected
    ZCYC publication (``md.zcyc_reject``) that points at an intraday capture. Each batch
    commits separately.
    """
    if retention_days < 1:
        raise JobParamError("retention_days must be >= 1")
    with db.connection() as conn:
        refs = blocking_capture_refs(conn)
    keep = sql.SQL("").join(
        sql.SQL(" AND NOT EXISTS (SELECT 1 FROM {t} x WHERE x.{c} = r.id)").format(
            t=sql.Identifier(schema, table), c=sql.Identifier(column)
        )
        for schema, table, column in refs
    )
    query = sql.SQL(
        """
        DELETE FROM ops.raw_capture WHERE id IN (
            SELECT r.id FROM ops.raw_capture r
            WHERE r.endpoint LIKE %s
              AND r.fetched_at < now() - make_interval(days => %s){keep}
            ORDER BY r.id
            LIMIT %s
        )
        """
    ).format(keep=keep)
    total = 0
    while True:
        with db.connection() as conn:
            cur = conn.execute(query, (INTRADAY_ENDPOINT_PREFIX + "%", retention_days, batch))
            n = cur.rowcount
        total += n
        if n < batch:
            return total


def vacuum_tables(db: Database, *, min_mb: int = 64) -> list[str]:
    """``VACUUM (ANALYZE)`` ``ops.raw_capture`` and large tables of ops/ref/md (autocommit)."""
    with db.connect(autocommit=True) as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            rows = cur.execute(
                """
                SELECT n.nspname, c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r' AND n.nspname = ANY(%s)
                  AND (pg_total_relation_size(c.oid) >= %s::bigint * 1024 * 1024
                       OR (n.nspname = 'ops' AND c.relname = 'raw_capture'))
                ORDER BY 1, 2
                """,
                (list(VACUUM_SCHEMAS), min_mb),
            ).fetchall()
        done: list[str] = []
        for schema, table in rows:
            conn.execute(
                sql.SQL("VACUUM (ANALYZE) {}").format(sql.Identifier(str(schema), str(table)))
            )
            done.append(f"{schema}.{table}")
    return done


@job("maintenance", "Nightly pg_dump backup with rotation, raw retention and VACUUM ANALYZE")
def maintenance(
    ctx: JobContext,
    *,
    mode: str = "retention",
    keep: int = 14,
    backup_dir_path: str | None = None,
    retention_days: int | None = None,
    vacuum: bool = True,
    vacuum_min_mb: int = 64,
) -> dict[str, Any]:
    """Run maintenance ``mode`` (``backup`` or ``retention``).

    :param keep: dumps to keep (backup).
    :param backup_dir_path: override ``MDHUB_BACKUP_DIR``.
    :param retention_days: override ``MDHUB_RAW_RETENTION_DAYS_INTRADAY``.
    :param vacuum: run ``VACUUM (ANALYZE)`` after retention.
    """
    secrets = ctx.settings.secret_values()
    if mode == "backup":
        res = run_backup(ctx.db, directory=backup_dir(backup_dir_path), keep=keep, secrets=secrets)
        ctx.logger.info(
            "backup written: %s (%d bytes), %d old dump(s) removed",
            res.path,
            res.size_bytes,
            len(res.removed),
        )
        return {
            "mode": mode,
            "path": str(res.path),
            "size_bytes": res.size_bytes,
            "removed": [str(p) for p in res.removed],
        }
    if mode == "retention":
        days = retention_days or ctx.settings.raw_retention_days_intraday
        result: dict[str, Any] = {"mode": mode, "retention_days": days}
        result["orphaned_runs_reaped"] = reap_orphaned_runs(ctx.db)
        try:
            result["raw_deleted"] = purge_intraday_raw(ctx.db, days)
        except psycopg.errors.ForeignKeyViolation as exc:
            diag = exc.diag.table_name or "?"
            ctx.run.mark_partial(
                f"raw retention blocked by a foreign key from table {diag}: capture_id must "
                "be declared ON DELETE SET NULL for intraday tables"
            )
            result["raw_deleted"] = None
        ctx.logger.info("raw retention: deleted %s intraday capture(s)", result["raw_deleted"])
        if vacuum:
            result["vacuumed"] = vacuum_tables(ctx.db, min_mb=vacuum_min_mb)
        return result
    raise JobParamError(f"unknown maintenance mode {mode!r} (backup, retention)")
