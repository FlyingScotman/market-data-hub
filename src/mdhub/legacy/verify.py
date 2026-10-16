"""Verification of a verbatim legacy import: SQLite vs PostgreSQL vs ``SNAPSHOT.json``.

For every copied table (and for ``sqlite_master`` vs ``_sqlite_schema``):

* row counts: SQLite, PostgreSQL, ``SNAPSHOT.json`` (stored with the import), and the
  count recorded at import time must all agree;
* content checksums: every row is reconstructed into SQLite's Python values on both
  sides (tagged columns via their ``__typeof`` companion) and hashed with the same
  canonical serialization (:mod:`mdhub.legacy.typemap`); the per-table checksums
  (order independent) must be identical;
* the column lists must match, the schema must contain no unexpected tables, and the
  SQLite file must still have the SHA-256 recorded at import.

The outcome is written to the latest ``ops.legacy_import`` row of the schema
(``verified``, ``verified_at``, ``table_checksums``, ``verify_report``).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.db.pool import Database
from mdhub.legacy.import_sqlite import META_TABLES, LegacyImportError
from mdhub.legacy.sqlite_source import (
    TableInfo,
    file_sha256,
    introspect,
    iter_rows,
    open_readonly,
    read_master,
)
from mdhub.legacy.typemap import COPY_TYPE, TableHasher, from_pg

__all__ = ["TableCheck", "VerifyResult", "latest_import", "verify_schema"]

log = logging.getLogger(__name__)

_PG_TYPES = {"bigint", "double precision", "text", "bytea", "numeric", "integer"}
_COPY_TYPE = {**COPY_TYPE, "integer": "int4"}


@dataclass(slots=True)
class TableCheck:
    """Comparison of one table."""

    table: str
    sqlite_rows: int | None = None
    pg_rows: int | None = None
    snapshot_rows: int | None = None
    recorded_rows: int | None = None
    sqlite_checksum: str | None = None
    pg_checksum: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "sqlite_rows": self.sqlite_rows,
            "pg_rows": self.pg_rows,
            "snapshot_rows": self.snapshot_rows,
            "recorded_rows": self.recorded_rows,
            "sqlite_checksum": self.sqlite_checksum,
            "pg_checksum": self.pg_checksum,
            "problems": self.problems,
        }


@dataclass(slots=True)
class VerifyResult:
    """Outcome of :func:`verify_schema`."""

    schema: str
    import_id: int | None = None
    source_path: str | None = None
    tables: list[TableCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Problems not tied to one table (file hash, extra tables, no snapshot, ...)."""
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems and all(t.ok for t in self.tables)

    @property
    def failures(self) -> list[str]:
        out = list(self.problems)
        out.extend(f"{t.table}: {p}" for t in self.tables for p in t.problems)
        return out


def latest_import(conn: psycopg.Connection[Any], schema: str) -> dict[str, Any] | None:
    """Latest ``ops.legacy_import`` row of ``schema`` as a dict (``None`` if none)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            SELECT id, source_path, source_sha256, snapshot_json, table_counts
            FROM ops.legacy_import WHERE schema_name = %s ORDER BY id DESC LIMIT 1
            """,
            (schema,),
        ).fetchone()
    if row is None:
        return None
    keys = ("id", "source_path", "source_sha256", "snapshot_json", "table_counts")
    return dict(zip(keys, row, strict=True))


@dataclass(frozen=True, slots=True)
class _PgCol:
    sqlite_name: str
    pg_column: str
    pg_type: str
    typeof_column: str | None


def _pg_column_map(conn: psycopg.Connection[Any], schema: str) -> dict[str, list[_PgCol]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            sql.SQL(
                "SELECT table_name, column_name, pg_table, pg_column, typeof_column "
                "FROM {}._sqlite_columns ORDER BY table_name, cid"
            ).format(sql.Identifier(schema))
        ).fetchall()
        types = {
            (str(t), str(c)): str(ty)
            for t, c, ty in cur.execute(
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (schema,),
            ).fetchall()
        }
    out: dict[str, list[_PgCol]] = {}
    for table, col, pg_table, pg_col, typeof_col in rows:
        out.setdefault(str(table), []).append(
            _PgCol(str(col), str(pg_col), types.get((str(pg_table), str(pg_col)), "?"),
                   str(typeof_col) if typeof_col else None)
        )  # fmt: skip
    return out


def _pg_table_names(conn: psycopg.Connection[Any], schema: str) -> dict[str, str]:
    """``{sqlite table: pg table}`` from ``_sqlite_columns`` (all tables incl. empty ones)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            sql.SQL("SELECT DISTINCT table_name, pg_table FROM {}._sqlite_columns").format(
                sql.Identifier(schema)
            )
        ).fetchall()
    return {str(a): str(b) for a, b in rows}


def _actual_tables(conn: psycopg.Connection[Any], schema: str) -> set[str]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            """,
            (schema,),
        ).fetchall()
    return {str(r[0]) for r in rows}


def _pg_rows(
    conn: psycopg.Connection[Any], schema: str, pg_table: str, cols: list[_PgCol]
) -> Iterator[tuple[Any, ...]]:
    names: list[sql.Composable] = []
    types: list[str] = []
    for c in cols:
        names.append(sql.Identifier(c.pg_column))
        types.append(_COPY_TYPE[c.pg_type])
        if c.typeof_column:
            names.append(sql.Identifier(c.typeof_column))
            types.append("text")
    stmt = sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT (FORMAT BINARY)").format(
        sql.SQL(", ").join(names), sql.Identifier(schema, pg_table)
    )
    tagged = [c.typeof_column is not None for c in cols]
    with conn.cursor() as cur, cur.copy(stmt) as copy:
        copy.set_types(types)
        for raw in copy.rows():
            if not any(tagged):
                yield raw
                continue
            out: list[Any] = []
            i = 0
            for t in tagged:
                if t:
                    out.append(from_pg(raw[i], raw[i + 1]))
                    i += 2
                else:
                    out.append(raw[i])
                    i += 1
            yield tuple(out)


def _check_table(
    conn: psycopg.Connection[Any],
    lite: sqlite3.Connection,
    schema: str,
    info: TableInfo,
    pg_table: str | None,
    pg_cols: list[_PgCol] | None,
    check: TableCheck,
) -> None:
    lite_hash = TableHasher()
    for row in iter_rows(lite, info):
        lite_hash.add(row)
    check.sqlite_rows = lite_hash.rows
    check.sqlite_checksum = lite_hash.hexdigest()
    if pg_table is None or pg_cols is None:
        check.problems.append("missing in PostgreSQL")
        return
    lite_names = [c.name for c in info.columns]
    if [c.sqlite_name for c in pg_cols] != lite_names:
        check.problems.append(
            f"column list differs: sqlite {lite_names} "
            f"vs recorded {[c.sqlite_name for c in pg_cols]}"
        )
        return
    bad = [c.pg_column for c in pg_cols if c.pg_type not in _PG_TYPES]
    if bad:
        check.problems.append(f"missing or unexpected column type(s) in PostgreSQL: {bad}")
        return
    pg_hash = TableHasher()
    try:
        with conn.transaction():
            for row in _pg_rows(conn, schema, pg_table, pg_cols):
                pg_hash.add(row)
    except (psycopg.Error, ValueError, TypeError) as exc:
        check.problems.append(f"cannot read PostgreSQL copy: {type(exc).__name__}: {exc}")
        return
    check.pg_rows = pg_hash.rows
    check.pg_checksum = pg_hash.hexdigest()


def _check_master(
    conn: psycopg.Connection[Any], lite: sqlite3.Connection, schema: str
) -> TableCheck:
    check = TableCheck(table="_sqlite_schema")
    lite_hash = TableHasher()
    for m in read_master(lite):
        lite_hash.add((m.seq, m.type, m.name, m.tbl_name, m.rootpage, m.sql))
    check.sqlite_rows = lite_hash.rows
    check.sqlite_checksum = lite_hash.hexdigest()
    pg_hash = TableHasher()
    with conn.cursor(row_factory=tuple_row) as cur:
        for r in cur.execute(
            sql.SQL(
                "SELECT seq, type, name, tbl_name, rootpage, sql FROM {}._sqlite_schema"
            ).format(sql.Identifier(schema))
        ):
            pg_hash.add(tuple(r))
    check.pg_rows = pg_hash.rows
    check.pg_checksum = pg_hash.hexdigest()
    if check.sqlite_checksum != check.pg_checksum:
        check.problems.append("sqlite_master rows differ from _sqlite_schema")
    return check


def _snapshot_counts(snapshot: Any) -> dict[str, int] | None:
    if not isinstance(snapshot, dict):
        return None
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        return None
    out: dict[str, int] = {}
    for name, meta in tables.items():
        if isinstance(meta, dict) and isinstance(meta.get("rows"), int):
            out[str(name)] = int(meta["rows"])
    return out


def verify_schema(
    db: Database,
    schema: str,
    *,
    sqlite_path: Path | None = None,
    record: bool = True,
    progress: Callable[[str], None] | None = None,
) -> VerifyResult:
    """Compare ``schema`` with its SQLite source (default: path of the latest import).

    :param record: write the outcome to the latest ``ops.legacy_import`` row.
    :raises LegacyImportError: nothing has been imported into ``schema``.
    """
    say = progress or (lambda msg: log.info("%s", msg))
    started = time.monotonic()
    result = VerifyResult(schema=schema)
    with db.connect() as conn:
        imp = latest_import(conn, schema)
        if imp is None:
            raise LegacyImportError(f"no import recorded for schema {schema}")
        result.import_id = int(imp["id"])
        path = (sqlite_path or Path(str(imp["source_path"]))).resolve()
        result.source_path = str(path)
        if not path.is_file():
            raise LegacyImportError(f"SQLite source {path} not found (pass the path explicitly)")
        sha = file_sha256(path)
        if sha != imp["source_sha256"]:
            result.problems.append(
                f"source SHA-256 {sha[:12]} differs from the imported file "
                f"{str(imp['source_sha256'])[:12]}"
            )
        snap = _snapshot_counts(imp["snapshot_json"])
        if snap is None:
            result.warnings.append("no SNAPSHOT.json stored with the import: counts not compared")
        recorded: dict[str, Any] = imp["table_counts"] or {}

        names = _pg_table_names(conn, schema)
        col_map = _pg_column_map(conn, schema)
        conn.commit()

        lite = open_readonly(path)
        try:
            tables = introspect(lite)
            result.tables.append(_check_master(conn, lite, schema))
            lite_names = {t.name for t in tables}
            for info in tables:
                t0 = time.monotonic()
                check = TableCheck(table=info.name)
                check.recorded_rows = recorded.get(info.name)
                if snap is not None:
                    check.snapshot_rows = snap.get(info.name)
                _check_table(
                    conn, lite, schema, info, names.get(info.name), col_map.get(info.name), check
                )
                if check.sqlite_rows != info.row_count:
                    check.problems.append("SQLite row count unstable")
                if check.pg_rows is not None and check.pg_rows != check.sqlite_rows:
                    check.problems.append(
                        f"row count: sqlite {check.sqlite_rows} != postgres {check.pg_rows}"
                    )
                if check.recorded_rows is not None and check.recorded_rows != check.sqlite_rows:
                    check.problems.append(
                        f"row count: sqlite {check.sqlite_rows} != recorded at import "
                        f"{check.recorded_rows}"
                    )
                if snap is not None:
                    if check.snapshot_rows is None:
                        result.warnings.append(f"{info.name}: not listed in SNAPSHOT.json")
                    elif check.snapshot_rows != check.sqlite_rows:
                        check.problems.append(
                            f"row count: sqlite {check.sqlite_rows} != SNAPSHOT.json "
                            f"{check.snapshot_rows}"
                        )
                if check.pg_checksum is not None and check.pg_checksum != check.sqlite_checksum:
                    check.problems.append("content checksum differs")
                result.tables.append(check)
                say(
                    f"  {schema}.{info.name}: {check.sqlite_rows} rows "
                    f"{'OK' if check.ok else 'MISMATCH'} ({time.monotonic() - t0:.2f}s)"
                )
        finally:
            lite.close()

        if snap is not None:
            for name in sorted(set(snap) - lite_names):
                result.problems.append(f"{name}: listed in SNAPSHOT.json but not in SQLite")
        expected = {names[n] for n in lite_names if n in names} | set(META_TABLES)
        for extra in sorted(_actual_tables(conn, schema) - expected):
            result.problems.append(f"unexpected relation {schema}.{extra}")
        for gone in sorted(set(names) - lite_names):
            result.problems.append(f"{gone}: in PostgreSQL mapping but not in SQLite")

        result.duration_s = time.monotonic() - started
        if record:
            report = {
                "ok": result.ok,
                "source_path": result.source_path,
                "problems": result.failures,
                "warnings": result.warnings,
                "tables": {t.table: t.as_dict() for t in result.tables},
                "duration_s": round(result.duration_s, 3),
            }
            conn.execute(
                """
                UPDATE ops.legacy_import SET verified = %s, verified_at = now(),
                    table_checksums = %s, verify_report = %s
                WHERE id = %s
                """,
                (
                    result.ok,
                    Jsonb({t.table: t.pg_checksum for t in result.tables}),
                    Jsonb(report),
                    result.import_id,
                ),
            )
            conn.commit()
    return result
