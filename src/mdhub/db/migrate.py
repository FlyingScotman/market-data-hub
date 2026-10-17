"""Forward-only SQL migration runner.

Migrations are plain SQL files ``NNNN_name.sql`` in ``mdhub/db/migrations/`` (number ranges
per DESIGN §5: 0001-0099 core/ops, 0100-0199 ref, 0200-0299 md, 0300-0399 legacy,
0400+ api/views). The runner:

* applies pending files sorted by filename, each in its own transaction (a failing file
  rolls back completely and stops the run; earlier files stay applied),
* records ``version`` (file stem, e.g. ``0001_core``), ``applied_at`` and the SHA-256 of the
  file bytes in ``ops.schema_migration``,
* refuses to run (before applying anything) if an already-applied file's checksum changed or
  two files share the same number,
* serialises concurrent runners with a PostgreSQL session advisory lock,
* finally calls ``admin.restrict_access()`` when it exists (also when nothing was pending), so
  the ``admin`` schema stays closed to read-only roles after a seed restore too (the owner's
  global default privileges apply to restored objects; ADMIN_DESIGN §3.1).

Files are executed as one multi-statement batch through the simple query protocol: no psql
meta-commands, no ``CREATE INDEX CONCURRENTLY`` (it cannot run inside a transaction).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import tuple_row

from mdhub.db.pool import Database

__all__ = [
    "MIGRATIONS_DIR",
    "ChecksumMismatchError",
    "Migration",
    "MigrationError",
    "MigrationLockTimeoutError",
    "MigrationReport",
    "applied_migrations",
    "discover_migrations",
    "migrate",
    "pending_migrations",
]

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
"""Directory holding the packaged migrations."""

_NAME_RE = re.compile(r"^(?P<num>\d{4})_(?P<name>[a-z0-9][a-z0-9_]*)\.sql$")
_LOCK_NAME = "mdhub.migrate"

_BOOTSTRAP = b"""
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
);
"""


class MigrationError(RuntimeError):
    """Migration set is invalid or a migration failed."""


class ChecksumMismatchError(MigrationError):
    """An applied migration file was modified after it was applied."""


class MigrationLockTimeoutError(MigrationError):
    """Another migration runner holds the advisory lock."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file."""

    version: str
    """File stem, e.g. ``"0001_core"``; primary key in ``ops.schema_migration``."""
    number: int
    path: Path
    checksum: str
    """Hex SHA-256 of the file bytes."""

    def read_sql(self) -> bytes:
        """Return the file contents (bytes, executed verbatim)."""
        return self.path.read_bytes()


@dataclass(slots=True)
class MigrationReport:
    """Outcome of :func:`migrate`."""

    applied: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    unknown_applied: list[str] = field(default_factory=list)
    """Versions recorded in the DB with no matching file (e.g. DB newer than the code)."""


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """List migration files in ``directory`` (default :data:`MIGRATIONS_DIR`), sorted.

    :raises MigrationError: on a badly named ``.sql`` file or a duplicate number.
    """
    directory = directory or MIGRATIONS_DIR
    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        m = _NAME_RE.match(path.name)
        if m is None:
            raise MigrationError(f"bad migration file name {path.name!r} (want NNNN_name.sql)")
        number = int(m.group("num"))
        if number in seen:
            raise MigrationError(
                f"duplicate migration number {number:04d}: {seen[number]} and {path.name}"
            )
        seen[number] = path.name
        data = path.read_bytes()
        migrations.append(
            Migration(
                version=path.stem,
                number=number,
                path=path,
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    return migrations


def applied_migrations(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """Return ``{version: checksum}`` from ``ops.schema_migration`` (empty if absent)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        exists = cur.execute("SELECT to_regclass('ops.schema_migration') IS NOT NULL").fetchone()
        if not exists or not exists[0]:
            return {}
        rows = cur.execute("SELECT version, checksum FROM ops.schema_migration").fetchall()
    return {str(version): str(checksum) for version, checksum in rows}


def pending_migrations(
    conn: psycopg.Connection[Any], directory: Path | None = None
) -> list[Migration]:
    """Migrations not yet applied (does not verify checksums)."""
    applied = applied_migrations(conn)
    return [m for m in discover_migrations(directory) if m.version not in applied]


def migrate(
    target: str | Database,
    *,
    directory: Path | None = None,
    lock_timeout: float = 60.0,
) -> MigrationReport:
    """Apply all pending migrations to the database.

    :param target: conninfo string or :class:`~mdhub.db.pool.Database`.
    :param directory: migration directory (default: packaged migrations).
    :param lock_timeout: seconds to wait for the advisory lock held by another runner.
    :raises ChecksumMismatchError: an applied file changed; nothing is applied.
    :raises MigrationLockTimeoutError: lock not obtained within ``lock_timeout``.
    :raises MigrationError: a migration failed (wraps the database error), or the database is
        not UTF8-encoded.
    """
    migrations = discover_migrations(directory)
    db = target if isinstance(target, Database) else Database(target)
    report = MigrationReport()
    with db.connect(autocommit=True) as conn:
        _require_utf8(conn)
        _acquire_lock(conn, lock_timeout)
        try:
            conn.execute(_BOOTSTRAP)
            applied = applied_migrations(conn)
            known = {m.version for m in migrations}
            report.unknown_applied = sorted(v for v in applied if v not in known)
            for v in report.unknown_applied:
                log.warning("migration %s is recorded in the database but has no file", v)
            for m in migrations:
                if m.version in applied and applied[m.version] != m.checksum:
                    raise ChecksumMismatchError(
                        f"migration {m.version} was modified after being applied "
                        f"(db {applied[m.version][:12]}, file {m.checksum[:12]}); "
                        "add a new migration instead of editing an applied one"
                    )
            for m in migrations:
                if m.version in applied:
                    report.already_applied.append(m.version)
                    continue
                _apply(conn, m)
                report.applied.append(m.version)
            _restrict_admin(conn)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_LOCK_NAME,))
    return report


def _require_utf8(conn: psycopg.Connection[Any]) -> None:
    """Fail closed on a non-UTF8 database: with ``SQL_ASCII`` psycopg returns text as bytes
    (migration names, every loader key) and non-ASCII source data (issuer names) is not
    validated. ``createdb -E UTF8 -T template0`` (DESIGN §9, README_DEPLOY)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database "
            "WHERE datname = current_database()"
        ).fetchone()
    encoding = row[0] if row else None
    if isinstance(encoding, bytes):
        encoding = encoding.decode("ascii", "replace")
    if encoding != "UTF8":
        raise MigrationError(
            f"database encoding is {encoding!r}, need UTF8: re-create it with "
            "`createdb -E UTF8 -T template0 ...`"
        )


def _restrict_admin(conn: psycopg.Connection[Any]) -> None:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT to_regprocedure('admin.restrict_access()') IS NOT NULL"
        ).fetchone()
        if not row or not row[0]:
            return
        try:
            cur.execute("CALL admin.restrict_access()")
        except psycopg.Error as exc:  # not the schema owner: the grant scripts still apply it
            log.warning("admin.restrict_access() failed: %s", type(exc).__name__)


def _apply(conn: psycopg.Connection[Any], m: Migration) -> None:
    log.info("applying migration %s", m.version)
    started = time.monotonic()
    try:
        with conn.transaction():
            conn.execute(m.read_sql())
            conn.execute(
                "INSERT INTO ops.schema_migration (version, checksum) VALUES (%s, %s)",
                (m.version, m.checksum),
            )
    except psycopg.Error as exc:
        raise MigrationError(f"migration {m.version} failed: {exc}") from exc
    log.info("applied migration %s in %.2fs", m.version, time.monotonic() - started)


def _acquire_lock(conn: psycopg.Connection[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with conn.cursor(row_factory=tuple_row) as cur:
            row = cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (_LOCK_NAME,)
            ).fetchone()
        if row is not None and row[0]:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeoutError(
                f"another migration runner holds the lock (waited {timeout:.0f}s)"
            )
        time.sleep(0.2)
