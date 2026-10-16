"""Idempotent bulk upsert by natural key.

:func:`upsert_rows` loads rows into a temporary table with ``COPY`` and merges them with one
``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE <data columns> IS DISTINCT FROM <new>``
statement, so:

* new keys are inserted,
* existing keys whose *data* columns differ are updated (meta columns are refreshed only
  then; ``updated_at`` is set to ``now()`` automatically if the table has it and the rows
  do not supply it) - and the ``ops.track_changes`` trigger stores the old image,
* identical re-loads touch nothing: no write, no history row, no dead tuples.

Meta columns (:data:`META_COLUMNS`) never count as a change. All identifiers are composed
with :mod:`psycopg.sql`; table and column names are validated against the catalog.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.timeutil import MSK

__all__ = ["META_COLUMNS", "UpsertResult", "atomic", "touch_last_seen", "upsert_rows"]

META_COLUMNS: Final = frozenset(
    {"ingested_at", "updated_at", "run_id", "capture_id", "last_seen_run", "last_seen"}
)
"""Columns that describe provenance, not content (also ignored by ``ops.track_changes``)."""

_tmp_counter = itertools.count(1)


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Row counts of an upsert. Results can be added: ``total = a + b``."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        """Rows submitted."""
        return self.inserted + self.updated + self.unchanged

    @property
    def changed(self) -> int:
        """Rows inserted or updated."""
        return self.inserted + self.updated

    def __add__(self, other: UpsertResult) -> UpsertResult:
        if not isinstance(other, UpsertResult):
            return NotImplemented
        return UpsertResult(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.unchanged + other.unchanged,
        )


def _split_table(table: str) -> sql.Identifier:
    parts = table.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"table must be schema-qualified 'schema.name', got {table!r}")
    return sql.Identifier(*parts)


def _table_columns(conn: psycopg.Connection[Any], table: str) -> dict[str, str]:
    """``{column: type}`` for ``table``; raises ``ValueError`` if it does not exist."""
    schema, name = table.split(".")
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (schema, name),
        ).fetchall()
    if not rows:
        raise ValueError(f"table {table} does not exist")
    return {str(col): str(typ) for col, typ in rows}


@contextmanager
def atomic(conn: psycopg.Connection[Any]) -> Iterator[None]:
    """Run a block atomically without committing the caller's transaction.

    On a non-autocommit connection the block becomes a SAVEPOINT inside the caller's
    (possibly implicit) transaction - the caller still decides when to commit. On an
    autocommit connection the block is its own transaction.
    """
    if not conn.autocommit and conn.info.transaction_status == TransactionStatus.IDLE:
        conn.execute("SELECT 1")  # open the caller's implicit transaction
    with conn.transaction():
        yield


def _adapt(value: Any) -> Any:
    if isinstance(value, dict):
        return Jsonb(value)
    return value


def _check_columns(table: str, table_cols: Mapping[str, str], cols: Iterable[str]) -> None:
    unknown = [c for c in cols if c not in table_cols]
    if unknown:
        raise ValueError(f"unknown column(s) for {table}: {', '.join(unknown)}")


def _ids(cols: Sequence[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in cols)


def _make_temp(
    conn: psycopg.Connection[Any], target: sql.Identifier, cols: Sequence[str]
) -> sql.Identifier:
    tmp = sql.Identifier(f"_mdhub_tmp_{next(_tmp_counter)}")
    conn.execute(
        sql.SQL(
            "CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {cols} FROM {t} WITH NO DATA"
        ).format(tmp=tmp, cols=_ids(cols), t=target)
    )
    return tmp


def _copy_rows(
    conn: psycopg.Connection[Any],
    tmp: sql.Identifier,
    cols: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> None:
    with (
        conn.cursor() as cur,
        cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(tmp, _ids(cols))) as copy,
    ):
        for row in rows:
            copy.write_row([_adapt(v) for v in row])


def upsert_rows(
    conn: psycopg.Connection[Any],
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    *,
    run_id: int | None = None,
) -> UpsertResult:
    """Insert or update ``rows`` into ``table`` keyed by ``key_cols``.

    :param conn: open connection. The upsert runs inside :func:`atomic` (a savepoint in the
        caller's transaction): a failure leaves the outer transaction usable and nothing is
        committed here - the caller commits (e.g. on exit of ``Database.connection()``).
    :param table: schema-qualified table name, e.g. ``"md.bond_eod"``.
    :param rows: dicts with identical key sets (missing keys are an error, use ``None`` for
        NULL). ``dict`` values are stored as ``jsonb``; wrap lists for jsonb columns in
        :class:`psycopg.types.json.Jsonb`.
    :param key_cols: natural key; must match a unique index / primary key of ``table``.
    :param update_cols: columns updated on conflict (default: all non-key columns supplied).
        Data columns among them decide whether a row changed; meta columns
        (:data:`META_COLUMNS`) among them are refreshed only when data changed.
    :param run_id: if given, set as transaction-local ``mdhub.run_id`` so history rows
        record ``replaced_by_run``.
    :raises ValueError: unknown table/columns, inconsistent rows or duplicate keys in batch.
    """
    if not rows:
        return UpsertResult()
    if not key_cols:
        raise ValueError("key_cols must not be empty")
    target = _split_table(table)
    cols = list(rows[0].keys())
    col_set = set(cols)
    for k in key_cols:
        if k not in col_set:
            raise ValueError(f"key column {k!r} missing from rows")
    if update_cols is None:
        update_cols = [c for c in cols if c not in key_cols]
    else:
        missing = [c for c in update_cols if c not in col_set]
        if missing:
            raise ValueError(f"update column(s) not in rows: {', '.join(missing)}")
        if set(update_cols) & set(key_cols):
            raise ValueError("update_cols must not contain key columns")

    values: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for i, row in enumerate(rows):
        if set(row.keys()) != col_set:
            raise ValueError(f"row {i} has different columns than row 0")
        key = tuple(row[k] for k in key_cols)
        if key in seen:
            raise ValueError(
                f"duplicate key in batch for {table}: {dict(zip(key_cols, key, strict=True))}"
            )
        seen.add(key)
        values.append(tuple(row[c] for c in cols))

    data_cols = [c for c in update_cols if c not in META_COLUMNS]
    meta_cols = [c for c in update_cols if c in META_COLUMNS]

    with atomic(conn):
        table_cols = _table_columns(conn, table)
        _check_columns(table, table_cols, cols)
        if run_id is not None:
            conn.execute("SELECT set_config('mdhub.run_id', %s, true)", (str(run_id),))
        tmp = _make_temp(conn, target, cols)
        _copy_rows(conn, tmp, cols, values)

        insert = sql.SQL("INSERT INTO {t} AS t ({cols}) SELECT {cols} FROM {tmp} ").format(
            t=target, cols=_ids(cols), tmp=tmp
        )
        if data_cols:
            assignments: list[sql.Composable] = [
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                for c in [*data_cols, *meta_cols]
            ]
            if "updated_at" in table_cols and "updated_at" not in col_set:
                assignments.append(sql.SQL("updated_at = now()"))
            conflict = sql.SQL(
                "ON CONFLICT ({keys}) DO UPDATE SET {sets} "
                "WHERE ({old}) IS DISTINCT FROM ({new}) RETURNING (xmax = 0)"
            ).format(
                keys=_ids(key_cols),
                sets=sql.SQL(", ").join(assignments),
                old=sql.SQL(", ").join(
                    sql.SQL("t.{}").format(sql.Identifier(c)) for c in data_cols
                ),
                new=sql.SQL(", ").join(
                    sql.SQL("EXCLUDED.{}").format(sql.Identifier(c)) for c in data_cols
                ),
            )
        else:
            conflict = sql.SQL("ON CONFLICT ({keys}) DO NOTHING RETURNING true").format(
                keys=_ids(key_cols)
            )
        # Row-wise IS DISTINCT FROM needs >= 2 columns to be a row constructor; with a
        # single data column "(t.c) IS DISTINCT FROM (EXCLUDED.c)" is a scalar compare -
        # both forms are valid SQL.
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute(insert + conflict)
            flags = [bool(r[0]) for r in cur.fetchall()]
        conn.execute(sql.SQL("DROP TABLE {}").format(tmp))
        if run_id is not None:
            conn.execute("SELECT set_config('mdhub.run_id', '', true)")

    inserted = sum(flags)
    updated = len(flags) - inserted
    return UpsertResult(inserted=inserted, updated=updated, unchanged=len(values) - len(flags))


def touch_last_seen(
    conn: psycopg.Connection[Any],
    table: str,
    key_cols: Sequence[str],
    keys: Iterable[Sequence[Any] | Mapping[str, Any]],
    run_id: int,
    *,
    seen_at: datetime | None = None,
) -> int:
    """Mark rows as observed by ``run_id`` without touching their data.

    Sets ``last_seen_run = run_id`` and/or ``last_seen`` (``now()``, or ``seen_at``; for a
    ``date`` column the Moscow date) - whichever of the two columns the table has - on rows
    whose key is in ``keys``. Rows already marked with ``run_id`` are skipped. Meta-only
    updates never create history rows.

    :param keys: key tuples in ``key_cols`` order, or mappings containing ``key_cols``.
    :returns: number of rows updated.
    """
    if not key_cols:
        raise ValueError("key_cols must not be empty")
    target = _split_table(table)
    key_rows: list[tuple[Any, ...]] = []
    for k in keys:
        if isinstance(k, Mapping):
            key_rows.append(tuple(k[c] for c in key_cols))
        else:
            if len(k) != len(key_cols):
                raise ValueError(f"key {k!r} does not match key_cols {list(key_cols)}")
            key_rows.append(tuple(k))
    if not key_rows:
        return 0

    with atomic(conn):
        table_cols = _table_columns(conn, table)
        _check_columns(table, table_cols, key_cols)
        sets: list[sql.Composable] = []
        params: list[Any] = []
        guards: list[sql.Composable] = []
        if "last_seen_run" in table_cols:
            sets.append(sql.SQL("last_seen_run = %s"))
            params.append(run_id)
            guards.append(sql.SQL("t.last_seen_run IS DISTINCT FROM %s"))
        if "last_seen" in table_cols:
            ts = seen_at or datetime.now(MSK)
            value: date | datetime = (
                ts.astimezone(MSK).date() if table_cols["last_seen"] == "date" else ts
            )
            sets.append(sql.SQL("last_seen = %s"))
            params.append(value)
            guards.append(sql.SQL("t.last_seen IS DISTINCT FROM %s"))
        if not sets:
            raise ValueError(f"{table} has neither last_seen_run nor last_seen")
        conn.execute("SELECT set_config('mdhub.run_id', %s, true)", (str(run_id),))
        tmp = _make_temp(conn, target, key_cols)
        _copy_rows(conn, tmp, key_cols, set(key_rows))
        query = sql.SQL(
            "UPDATE {t} AS t SET {sets} FROM {tmp} AS k WHERE {join} AND ({guard})"
        ).format(
            t=target,
            sets=sql.SQL(", ").join(sets),
            tmp=tmp,
            join=sql.SQL(" AND ").join(
                sql.SQL("t.{c} = k.{c}").format(c=sql.Identifier(c)) for c in key_cols
            ),
            guard=sql.SQL(" OR ").join(guards),
        )
        with conn.cursor() as cur:
            cur.execute(query, [*params, *params])
            count = cur.rowcount
        conn.execute(sql.SQL("DROP TABLE {}").format(tmp))
        conn.execute("SELECT set_config('mdhub.run_id', '', true)")
    return count
