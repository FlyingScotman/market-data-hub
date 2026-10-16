"""Verbatim import of a legacy SQLite file into a PostgreSQL schema (``legacy_md`` / ``legacy_bc``).

Every ordinary table of the SQLite file is copied with every row:

* table and column names are kept, lower-cased (ASCII only); all identifiers are composed
  with :mod:`psycopg.sql`;
* the PostgreSQL type of each column is chosen from the storage classes actually stored
  (``typeof``), see :mod:`mdhub.legacy.typemap` - lossless for any mix of values;
* rows are loaded with binary ``COPY``; NOT NULL is kept where declared; the primary key,
  UNIQUE constraints and unique indexes are re-created where they hold on the copied data
  (otherwise recorded as skipped), plain indexes are re-created for convenience;
* triggers (append-only enforcement), views and foreign keys are *not* re-created; every
  ``sqlite_master`` row is stored verbatim in ``<schema>._sqlite_schema``; the column
  mapping is in ``<schema>._sqlite_columns`` and constraint outcomes in
  ``<schema>._sqlite_constraints``;
* one row in ``ops.legacy_import`` records the file path, SHA-256, size, the
  ``SNAPSHOT.json`` shipped next to the file, pragmas and row counts.

The whole import of one file is a single transaction: it either completes or leaves the
schema untouched. Without ``drop_existing`` the import refuses to run when the schema
already contains tables.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.db.pool import Database
from mdhub.legacy.sqlite_source import (
    IndexInfo,
    MasterRow,
    TableInfo,
    file_sha256,
    introspect,
    iter_rows,
    open_readonly,
    read_master,
    read_pragmas,
)
from mdhub.legacy.typemap import COPY_TYPE, PgColumn, choose_pg_type, to_pg

__all__ = [
    "META_TABLES",
    "ImportResult",
    "LegacyImportError",
    "TablePlan",
    "import_sqlite",
    "pg_ident",
    "plan_tables",
]

log = logging.getLogger(__name__)

META_TABLES = ("_sqlite_schema", "_sqlite_columns", "_sqlite_constraints")
"""Bookkeeping tables created in every legacy schema (never copied from SQLite)."""

_PG_MAX_IDENT = 63
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_SCHEMA_RE = re.compile(r"^legacy_[a-z0-9_]+$")
_NOT_NULL_TERM = re.compile(
    r'^\(?\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\w+))\s+IS\s+NOT\s+NULL\s*\)?$', re.I
)


class LegacyImportError(RuntimeError):
    """The import (or verification) cannot proceed; nothing was changed."""


def pg_ident(name: str) -> str:
    """Lower-case (ASCII, like SQLite's case-insensitivity) and validate a PG identifier."""
    out = name.translate(_ASCII_LOWER)
    if not out or "\x00" in out or len(out.encode("utf-8")) > _PG_MAX_IDENT:
        raise LegacyImportError(f"name {name!r} is not usable as a PostgreSQL identifier")
    return out


@dataclass(slots=True)
class TablePlan:
    """How one SQLite table is created in PostgreSQL."""

    info: TableInfo
    pg_name: str
    columns: list[PgColumn]

    @property
    def pg_column_names(self) -> list[str]:
        out: list[str] = []
        for c in self.columns:
            out.append(c.name)
            if c.typeof_name:
                out.append(c.typeof_name)
        return out

    @property
    def copy_types(self) -> list[str]:
        out: list[str] = []
        for c in self.columns:
            out.append(COPY_TYPE[c.pg_type])
            if c.tagged:
                out.append("text")
        return out


@dataclass(slots=True)
class ImportResult:
    """Outcome of :func:`import_sqlite`."""

    schema: str
    import_id: int
    source_path: str
    source_sha256: str
    table_counts: dict[str, int]
    duration_s: float
    constraints: list[dict[str, Any]] = field(default_factory=list)
    tagged_columns: list[str] = field(default_factory=list)
    snapshot_found: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


def plan_tables(tables: Sequence[TableInfo]) -> list[TablePlan]:
    """Map SQLite tables to PostgreSQL names/types; reject identifier collisions."""
    plans: list[TablePlan] = []
    seen_tables: dict[str, str] = dict.fromkeys(META_TABLES, "(reserved)")
    for t in tables:
        pg_name = pg_ident(t.name)
        if pg_name in seen_tables:
            raise LegacyImportError(
                f"table {t.name!r} collides with {seen_tables[pg_name]!r} as {pg_name!r}"
            )
        seen_tables[pg_name] = t.name
        cols: list[PgColumn] = []
        seen_cols: dict[str, str] = {}
        for c in t.columns:
            name = pg_ident(c.name)
            pg_type, tagged = choose_pg_type(c)
            pc = PgColumn(
                sqlite_name=c.name,
                name=name,
                pg_type=pg_type,
                tagged=tagged,
                notnull=c.notnull and c.counts.get("null", 0) == 0,
            )
            names = [name] + ([pc.typeof_name] if pc.typeof_name else [])
            for n in names:
                pg_ident(n)  # length check of the companion column
                if n in seen_cols:
                    raise LegacyImportError(
                        f"column {t.name}.{c.name!r} collides with {seen_cols[n]!r} as {n!r}"
                    )
                seen_cols[n] = c.name
            cols.append(pc)
        plans.append(TablePlan(info=t, pg_name=pg_name, columns=cols))
    return plans


# --------------------------------------------------------------------------- DDL helpers

_META_DDL = (
    """
    CREATE TABLE {s}._sqlite_schema (
        seq      bigint PRIMARY KEY,
        type     text,
        name     text,
        tbl_name text,
        rootpage bigint,
        sql      text
    )
    """,
    """
    CREATE TABLE {s}._sqlite_columns (
        table_name    text NOT NULL,
        cid           integer NOT NULL,
        column_name   text NOT NULL,
        declared_type text NOT NULL,
        affinity      text NOT NULL,
        not_null      boolean NOT NULL,
        dflt_value    text,
        pk            integer NOT NULL,
        hidden        integer NOT NULL,
        pg_table      text NOT NULL,
        pg_column     text NOT NULL,
        pg_type       text NOT NULL,
        typeof_column text,
        typeof_counts jsonb NOT NULL,
        PRIMARY KEY (table_name, cid)
    )
    """,
    """
    CREATE TABLE {s}._sqlite_constraints (
        table_name  text NOT NULL,
        kind        text NOT NULL,
        sqlite_name text,
        columns     text[] NOT NULL,
        status      text NOT NULL,
        detail      text
    )
    """,
)


def _existing_relations(conn: psycopg.Connection[Any], schema: str) -> list[tuple[str, str]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT c.relname, c.relkind::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            ORDER BY c.relname
            """,
            (schema,),
        ).fetchall()
    return [(str(a), str(b)) for a, b in rows]


def _drop_existing(conn: psycopg.Connection[Any], schema: str, rels: list[tuple[str, str]]) -> None:
    kinds = (("v", "VIEW"), ("m", "MATERIALIZED VIEW"), ("f", "FOREIGN TABLE"), ("r", "TABLE"))
    for kind, word in kinds:
        names = [n for n, k in rels if k == kind or (kind == "r" and k == "p")]
        if not names:
            continue
        stmt = sql.SQL("DROP " + word + " {}").format(
            sql.SQL(", ").join(sql.Identifier(schema, n) for n in names)
        )
        try:
            with conn.transaction():
                conn.execute(stmt)
        except psycopg.errors.DependentObjectsStillExist as exc:
            raise LegacyImportError(
                f"cannot drop {schema} contents, other objects depend on them: {exc}"
            ) from exc


def _create_table(conn: psycopg.Connection[Any], schema: str, plan: TablePlan) -> None:
    defs: list[sql.Composable] = []
    for c in plan.columns:
        defs.append(
            sql.SQL("{} {}{}").format(
                sql.Identifier(c.name),
                sql.SQL(c.pg_type),
                sql.SQL(" NOT NULL" if c.notnull else ""),
            )
        )
        if c.typeof_name:
            defs.append(
                sql.SQL("{} text CHECK ({} IN ('integer', 'real', 'text', 'blob'))").format(
                    sql.Identifier(c.typeof_name), sql.Identifier(c.typeof_name)
                )
            )
    conn.execute(
        sql.SQL("CREATE TABLE {} ({})").format(
            sql.Identifier(schema, plan.pg_name), sql.SQL(", ").join(defs)
        )
    )
    comment = f"Verbatim copy of SQLite table {plan.info.name!r}"
    conn.execute(
        sql.SQL("COMMENT ON TABLE {} IS {}").format(
            sql.Identifier(schema, plan.pg_name), sql.Literal(comment)
        )
    )


def _copy_rows(
    conn: psycopg.Connection[Any], lite: sqlite3.Connection, schema: str, plan: TablePlan
) -> int:
    convs: list[Callable[[Any], tuple[Any, str | None]] | None] = [
        to_pg(c.pg_type) if c.tagged else None for c in plan.columns
    ]
    any_tagged = any(convs)
    stmt = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT BINARY)").format(
        sql.Identifier(schema, plan.pg_name),
        sql.SQL(", ").join(sql.Identifier(n) for n in plan.pg_column_names),
    )
    n = 0
    with conn.cursor() as cur, cur.copy(stmt) as copy:
        copy.set_types(plan.copy_types)
        for row in iter_rows(lite, plan.info):
            if any_tagged:
                out: list[Any] = []
                for v, conv in zip(row, convs, strict=True):
                    if conv is None:
                        out.append(v)
                    else:
                        out.extend(conv(v))
                copy.write_row(out)
            else:
                copy.write_row(row)
            n += 1
    return n


def _key_columns(plan: TablePlan, sqlite_cols: Sequence[str]) -> list[str]:
    by_name = {c.sqlite_name.translate(_ASCII_LOWER): c for c in plan.columns}
    out: list[str] = []
    for name in sqlite_cols:
        c = by_name[name.translate(_ASCII_LOWER)]
        out.append(c.name)
        if c.typeof_name:
            out.append(c.typeof_name)
    return out


def _trivial_partial(index: IndexInfo) -> bool:
    """``WHERE a IS NOT NULL [AND b IS NOT NULL ...]`` over key columns only.

    PostgreSQL unique indexes ignore NULL keys anyway, so such a partial unique index is
    equivalent to a plain one.
    """
    if not index.sql:
        return False
    m = re.search(r"\bWHERE\b(.*)$", index.sql, re.I | re.S)
    if not m:
        return False
    keys = {c.lower() for c in index.columns}
    for term in re.split(r"\bAND\b", m.group(1).strip().rstrip(";"), flags=re.I):
        tm = _NOT_NULL_TERM.match(term.strip())
        if not tm:
            return False
        col = next(g for g in tm.groups() if g)
        if col.lower() not in keys:
            return False
    return True


def _try(
    conn: psycopg.Connection[Any],
    stmt: sql.Composed,
    record: dict[str, Any],
    out: list[dict[str, Any]],
) -> None:
    try:
        with conn.transaction():
            conn.execute(stmt)
        record["status"] = "created"
    except (psycopg.errors.IntegrityError, psycopg.errors.DuplicateObject) as exc:
        record["status"] = "skipped"
        record["detail"] = (record.get("detail", "") + f" {type(exc).__name__}").strip()
    out.append(record)


def _constraints(
    conn: psycopg.Connection[Any], schema: str, plan: TablePlan
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    table = sql.Identifier(schema, plan.pg_name)
    info = plan.info
    nulls = {c.name: c.counts.get("null", 0) for c in info.columns}

    pk = info.pk_columns
    if pk:
        cols = _key_columns(plan, pk)
        rec: dict[str, Any] = {
            "table": info.name,
            "kind": "primary_key",
            "sqlite_name": None,
            "columns": cols,
        }
        ids = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
        if any(nulls[c] for c in pk):
            rec["kind"] = "unique"
            rec["detail"] = "primary key has NULLs in SQLite; created as UNIQUE"
            _try(conn, sql.SQL("ALTER TABLE {} ADD UNIQUE ({})").format(table, ids), rec, out)
        else:
            _try(conn, sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(table, ids), rec, out)

    for idx in info.indexes:
        if idx.origin == "pk":
            continue  # handled above (table_info.pk covers it)
        rec = {
            "table": info.name,
            "kind": "unique" if idx.unique else "index",
            "sqlite_name": idx.name,
            "columns": [],
        }
        if idx.has_expression or not idx.columns:
            rec["status"] = "skipped"
            rec["detail"] = "expression index"
            out.append(rec)
            continue
        cols = _key_columns(plan, idx.columns)
        rec["columns"] = cols
        ids = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
        if idx.unique and idx.partial and not _trivial_partial(idx):
            rec["status"] = "skipped"
            rec["detail"] = "partial unique index with a non-trivial predicate"
            out.append(rec)
            continue
        if idx.partial:
            rec["detail"] = "predicate dropped (equivalent for unique / harmless for plain)"
        if idx.origin == "u":
            stmt: sql.Composed = sql.SQL("ALTER TABLE {} ADD UNIQUE ({})").format(table, ids)
        else:
            name = idx.name.translate(_ASCII_LOWER)
            index_name = (
                sql.Identifier(name)
                if name and len(name.encode()) <= _PG_MAX_IDENT and not name.startswith("sqlite_")
                else sql.SQL("")
            )
            stmt = sql.SQL("CREATE {}INDEX {} ON {} ({})").format(
                sql.SQL("UNIQUE " if idx.unique else ""), index_name, table, ids
            )
        _try(conn, stmt, rec, out)
    return out


def _insert_meta(
    conn: psycopg.Connection[Any],
    schema: str,
    master: list[MasterRow],
    plans: list[TablePlan],
    constraints: list[dict[str, Any]],
) -> None:
    s = sql.Identifier(schema)
    with conn.cursor() as cur:
        cur.executemany(
            sql.SQL(
                "INSERT INTO {}._sqlite_schema (seq, type, name, tbl_name, rootpage, sql) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(s),
            [(m.seq, m.type, m.name, m.tbl_name, m.rootpage, m.sql) for m in master],
        )
        rows = []
        for p in plans:
            by_name = {c.name: c for c in p.info.columns}
            for pc in p.columns:
                c = by_name[pc.sqlite_name]
                rows.append(
                    (
                        p.info.name, c.cid, c.name, c.declared_type, c.affinity, c.notnull,
                        c.default, c.pk, c.hidden, p.pg_name, pc.name, pc.pg_type,
                        pc.typeof_name, Jsonb(c.counts),
                    )
                )  # fmt: skip
        cur.executemany(
            sql.SQL(
                "INSERT INTO {}._sqlite_columns (table_name, cid, column_name, declared_type, "
                "affinity, not_null, dflt_value, pk, hidden, pg_table, pg_column, pg_type, "
                "typeof_column, typeof_counts) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(s),
            rows,
        )
        cur.executemany(
            sql.SQL(
                "INSERT INTO {}._sqlite_constraints "
                "(table_name, kind, sqlite_name, columns, status, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s)"
            ).format(s),
            [
                (
                    r["table"],
                    r["kind"],
                    r["sqlite_name"],
                    r["columns"],
                    r["status"],
                    r.get("detail"),
                )
                for r in constraints
            ],
        )


def _read_snapshot(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot read snapshot {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LegacyImportError(f"snapshot {path} is not a JSON object")
    return data


def _manifest_entry(sqlite_path: Path) -> str | None:
    manifest = sqlite_path.with_name("MANIFEST.sha256")
    if not manifest.is_file():
        return None
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == sqlite_path.name:
            return parts[0].lower()
    return None


def import_sqlite(
    db: Database,
    sqlite_path: Path,
    schema: str,
    *,
    drop_existing: bool = False,
    snapshot_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> ImportResult:
    """Copy every table and row of ``sqlite_path`` into ``schema`` (one transaction).

    :param snapshot_path: ``SNAPSHOT.json`` to store with the import (default: the file of
        that name next to ``sqlite_path``, if present).
    :param drop_existing: drop the schema's existing tables/views first; otherwise refuse
        when it contains any.
    :raises LegacyImportError: schema not empty, identifier collisions, file changed during
        the import, invalid snapshot; nothing is committed.
    """
    say = progress or (lambda msg: log.info("%s", msg))
    if not _SCHEMA_RE.match(schema):
        raise LegacyImportError(f"target schema must match legacy_*, got {schema!r}")
    started = time.monotonic()
    sqlite_path = sqlite_path.resolve(strict=True)
    if snapshot_path is None:
        snapshot_path = sqlite_path.with_name("SNAPSHOT.json")
    snapshot = _read_snapshot(snapshot_path)
    sha = file_sha256(sqlite_path)
    size = sqlite_path.stat().st_size
    manifest = _manifest_entry(sqlite_path)
    if manifest is not None and manifest != sha:
        raise LegacyImportError(
            f"{sqlite_path.name}: SHA-256 {sha[:12]} does not match MANIFEST.sha256 {manifest[:12]}"
        )

    lite = open_readonly(sqlite_path)
    try:
        master = read_master(lite)
        pragmas = read_pragmas(lite)
        tables = introspect(lite, master)
        plans = plan_tables(tables)
        say(f"{schema}: {len(plans)} tables, {sum(t.row_count for t in tables)} rows in "
            f"{sqlite_path.name} ({size / 1e6:.1f} MB)")  # fmt: skip

        with db.connect() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"mdhub.legacy.{schema}",),
            )
            rels = _existing_relations(conn, schema)
            if rels and not drop_existing:
                raise LegacyImportError(
                    f"schema {schema} already contains {len(rels)} relation(s) "
                    f"(e.g. {rels[0][0]}); re-run with --drop-existing to replace them"
                )
            if rels:
                _drop_existing(conn, schema, rels)
                say(f"{schema}: dropped {len(rels)} existing relation(s)")
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            for ddl in _META_DDL:
                conn.execute(sql.SQL(ddl).format(s=sql.Identifier(schema)))

            counts: dict[str, int] = {}
            constraints: list[dict[str, Any]] = []
            for plan in plans:
                t0 = time.monotonic()
                _create_table(conn, schema, plan)
                n = _copy_rows(conn, lite, schema, plan)
                if n != plan.info.row_count:
                    raise LegacyImportError(
                        f"{plan.info.name}: copied {n} rows but SQLite reported "
                        f"{plan.info.row_count}"
                    )
                counts[plan.info.name] = n
                constraints.extend(_constraints(conn, schema, plan))
                conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(schema, plan.pg_name)))
                say(f"  {schema}.{plan.pg_name}: {n} rows in {time.monotonic() - t0:.2f}s")

            _insert_meta(conn, schema, master, plans, constraints)
            if file_sha256(sqlite_path) != sha:
                raise LegacyImportError(f"{sqlite_path} changed during the import")
            tagged = [
                f"{p.info.name}.{c.sqlite_name}" for p in plans for c in p.columns if c.tagged
            ]
            skipped = [c for c in constraints if c["status"] == "skipped"]
            meta = {
                "pragmas": pragmas,
                "manifest_sha256": manifest,
                "tables": {
                    p.info.name: {
                        "pg_table": p.pg_name,
                        "strict": p.info.strict,
                        "without_rowid": p.info.without_rowid,
                    }
                    for p in plans
                },
                "master_counts": _master_counts(master),
                "tagged_columns": tagged,
                "constraints_created": sum(1 for c in constraints if c["status"] == "created"),
                "constraints_skipped": skipped,
            }
            duration = time.monotonic() - started
            with conn.cursor(row_factory=tuple_row) as cur:
                row = cur.execute(
                    """
                        INSERT INTO ops.legacy_import (schema_name, source_path, source_sha256,
                            source_size_bytes, snapshot_json, sqlite_meta, duration_s, table_counts)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                        """,
                    (schema, str(sqlite_path), sha, size,
                     Jsonb(snapshot) if snapshot is not None else None,
                     Jsonb(meta), duration, Jsonb(counts)),
                ).fetchone()  # fmt: skip
            assert row is not None
            import_id = int(row[0])
    finally:
        lite.close()

    return ImportResult(
        schema=schema,
        import_id=import_id,
        source_path=str(sqlite_path),
        source_sha256=sha,
        table_counts=counts,
        duration_s=time.monotonic() - started,
        constraints=constraints,
        tagged_columns=tagged,
        snapshot_found=snapshot is not None,
    )


def _master_counts(master: list[MasterRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in master:
        out[m.type] = out.get(m.type, 0) + 1
    return out
