"""Read-only introspection of a legacy SQLite file.

The files are untrusted downloads: they are opened with ``mode=ro`` (URI), ``query_only``
and inside one read transaction; table/column names read from ``sqlite_master`` are only
ever used as quoted identifiers (:func:`qi`) or bound parameters.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "STORAGE_CLASSES",
    "ColumnInfo",
    "IndexInfo",
    "MasterRow",
    "TableInfo",
    "column_affinity",
    "file_sha256",
    "introspect",
    "open_readonly",
    "qi",
    "read_master",
    "read_pragmas",
]

STORAGE_CLASSES = ("null", "integer", "real", "text", "blob")
"""SQLite ``typeof()`` results."""

_STATS_CHUNK = 40  # columns per aggregate query (7 aggregates each, SQLite column limit 2000)


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only (URI ``mode=ro``) and start a read transaction.

    Rows are plain tuples with SQLite's own Python types (``int``, ``float``, ``str``,
    ``bytes``, ``None``).
    """
    path = path.resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = 1")
    conn.execute("BEGIN")  # one consistent snapshot for introspection, copy and checksums
    return conn


def file_sha256(path: Path, chunk: int = 4 << 20) -> str:
    """Hex SHA-256 of the file bytes."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def qi(name: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + name.replace('"', '""') + '"'


def column_affinity(declared: str, *, strict: bool = False) -> str:
    """Column affinity from the declared type (SQLite docs §3.1 'Determination of affinity')."""
    t = declared.upper()
    if strict and t == "ANY":
        return "BLOB"  # no affinity: values are stored as given
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if "BLOB" in t or not t.strip():
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"


@dataclass(frozen=True, slots=True)
class MasterRow:
    """One ``sqlite_master`` row (verbatim) plus its rowid."""

    seq: int
    type: str
    name: str
    tbl_name: str
    rootpage: int | None
    sql: str | None


@dataclass(slots=True)
class ColumnInfo:
    """A table column with its declared type and observed storage classes."""

    cid: int
    name: str
    declared_type: str
    affinity: str
    notnull: bool
    default: str | None
    pk: int
    """1-based position in the primary key, 0 if not part of it."""
    hidden: int
    """``table_xinfo.hidden``: 0 normal, 2/3 generated column (copied as plain values)."""
    counts: dict[str, int] = field(default_factory=dict)
    """``typeof()`` -> number of rows."""
    text_with_nul: int = 0
    """Text values containing U+0000 (not representable in PostgreSQL ``text``)."""
    real_special: int = 0
    """Real values that are 0.0 / -0.0 or infinite (not exact in ``numeric``)."""

    @property
    def present(self) -> frozenset[str]:
        """Non-null storage classes present in the column."""
        return frozenset(k for k, v in self.counts.items() if v and k != "null")


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """An index (or UNIQUE / PRIMARY KEY auto-index) of a table."""

    name: str
    unique: bool
    origin: str
    """``c`` CREATE INDEX, ``u`` UNIQUE constraint, ``pk`` PRIMARY KEY."""
    partial: bool
    columns: tuple[str, ...]
    """Key column names; empty when the index has an expression or rowid key."""
    has_expression: bool
    sql: str | None


@dataclass(slots=True)
class TableInfo:
    """A copied table."""

    name: str
    sql: str
    strict: bool
    without_rowid: bool
    columns: list[ColumnInfo]
    row_count: int = 0
    indexes: list[IndexInfo] = field(default_factory=list)

    @property
    def pk_columns(self) -> list[str]:
        """Primary-key columns in key order (includes an INTEGER PRIMARY KEY rowid alias)."""
        return [c.name for c in sorted((c for c in self.columns if c.pk), key=lambda c: c.pk)]

    def select_sql(self) -> str:
        """``SELECT`` of all copied columns in cid order."""
        return f"SELECT {', '.join(qi(c.name) for c in self.columns)} FROM {qi(self.name)}"


def read_master(conn: sqlite3.Connection) -> list[MasterRow]:
    """All ``sqlite_master`` rows in rowid order."""
    rows = conn.execute(
        "SELECT rowid, type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY rowid"
    ).fetchall()
    return [MasterRow(*r) for r in rows]


def read_pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    """File-level pragmas worth recording."""
    out: dict[str, Any] = {"sqlite_library_version": sqlite3.sqlite_version}
    for p in ("user_version", "application_id", "encoding", "page_size", "page_count"):
        row = conn.execute(f"PRAGMA {p}").fetchone()
        out[p] = row[0] if row else None
    return out


_CREATE_TABLE_RE = re.compile(r"^\s*CREATE\s+(?:TEMP\w*\s+)?TABLE\b", re.IGNORECASE)
_WITHOUT_ROWID_RE = re.compile(
    r"\)\s*(?:STRICT\s*,\s*)?WITHOUT\s+ROWID\s*(?:,\s*STRICT\s*)?;?\s*$", re.I
)
_STRICT_RE = re.compile(
    r"\)\s*(?:WITHOUT\s+ROWID\s*,\s*)?STRICT\s*(?:,\s*WITHOUT\s+ROWID\s*)?;?\s*$", re.I
)


def _copyable(row: MasterRow) -> bool:
    # Ordinary tables only: virtual tables (CREATE VIRTUAL TABLE) cannot be read without
    # their module; their shadow tables are ordinary tables and are copied.
    return row.type == "table" and row.sql is not None and bool(_CREATE_TABLE_RE.match(row.sql))


def _columns(conn: sqlite3.Connection, table: str, strict: bool) -> list[ColumnInfo]:
    cols: list[ColumnInfo] = []
    for cid, name, typ, notnull, dflt, pk, hidden in conn.execute(
        'SELECT cid, name, type, "notnull", dflt_value, pk, hidden '
        "FROM pragma_table_xinfo(?) ORDER BY cid",
        (table,),
    ):
        if hidden == 1:  # hidden column of a virtual table
            continue
        cols.append(
            ColumnInfo(
                cid=cid,
                name=name,
                declared_type=typ or "",
                affinity=column_affinity(typ or "", strict=strict),
                notnull=bool(notnull),
                default=dflt,
                pk=pk,
                hidden=hidden,
            )
        )
    return cols


def _indexes(
    conn: sqlite3.Connection, table: str, sql_by_name: dict[str, str | None]
) -> list[IndexInfo]:
    out: list[IndexInfo] = []
    for _seq, name, unique, origin, partial in conn.execute(
        'SELECT seq, name, "unique", origin, partial FROM pragma_index_list(?) ORDER BY seq',
        (table,),
    ).fetchall():
        keys = conn.execute(
            "SELECT cid, name FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno", (name,)
        ).fetchall()
        has_expr = any(cid < 0 for cid, _ in keys)
        out.append(
            IndexInfo(
                name=name,
                unique=bool(unique),
                origin=origin,
                partial=bool(partial),
                columns=() if has_expr else tuple(n for _, n in keys),
                has_expression=has_expr,
                sql=sql_by_name.get(name),
            )
        )
    return out


def _stats(conn: sqlite3.Connection, table: TableInfo) -> None:
    """Fill row count and per-column storage-class statistics (one scan per chunk)."""
    for start in range(0, max(len(table.columns), 1), _STATS_CHUNK):
        chunk = table.columns[start : start + _STATS_CHUNK]
        parts = ["count(*)"]
        for c in chunk:
            q = qi(c.name)
            parts.extend(f"coalesce(sum(typeof({q}) = '{k}'), 0)" for k in STORAGE_CLASSES)
            parts.append(
                f"coalesce(sum(CASE WHEN typeof({q}) = 'text' "
                f"THEN instr(CAST({q} AS BLOB), x'00') > 0 ELSE 0 END), 0)"
            )
            parts.append(
                f"coalesce(sum(CASE WHEN typeof({q}) = 'real' "
                f"THEN {q} = 0.0 OR {q} >= 9e999 OR {q} <= -9e999 ELSE 0 END), 0)"
            )
        row = conn.execute(f"SELECT {', '.join(parts)} FROM {qi(table.name)}").fetchone()
        table.row_count = int(row[0])
        width = len(STORAGE_CLASSES) + 2
        for i, c in enumerate(chunk):
            vals = row[1 + i * width : 1 + (i + 1) * width]
            c.counts = {k: int(v) for k, v in zip(STORAGE_CLASSES, vals[:5], strict=True)}
            c.text_with_nul = int(vals[5])
            c.real_special = int(vals[6])


def introspect(conn: sqlite3.Connection, master: list[MasterRow] | None = None) -> list[TableInfo]:
    """Describe every copyable table (in ``sqlite_master`` order) incl. typeof statistics."""
    master = master if master is not None else read_master(conn)
    sql_by_name = {r.name: r.sql for r in master if r.type == "index"}
    tables: list[TableInfo] = []
    for r in master:
        if not _copyable(r):
            continue
        assert r.sql is not None
        strict = bool(_STRICT_RE.search(r.sql))
        t = TableInfo(
            name=r.name,
            sql=r.sql,
            strict=strict,
            without_rowid=bool(_WITHOUT_ROWID_RE.search(r.sql)),
            columns=_columns(conn, r.name, strict),
        )
        t.indexes = _indexes(conn, r.name, sql_by_name)
        _stats(conn, t)
        tables.append(t)
    return tables


def iter_rows(
    conn: sqlite3.Connection, table: TableInfo, batch: int = 5000
) -> Iterator[tuple[Any, ...]]:
    """Stream all rows of ``table`` (columns in cid order)."""
    cur = conn.execute(table.select_sql())
    while rows := cur.fetchmany(batch):
        yield from rows
