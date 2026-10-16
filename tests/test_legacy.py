"""Verbatim legacy SQLite import (mdhub.legacy) against synthetic SQLite files."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row, tuple_row
from typer.testing import CliRunner

from mdhub import cli
from mdhub.db.pool import Database
from mdhub.legacy import import_sqlite, verify_schema
from mdhub.legacy.import_sqlite import LegacyImportError, pg_ident
from mdhub.legacy.sqlite_source import ColumnInfo, column_affinity
from mdhub.legacy.typemap import choose_pg_type, encode_value, from_pg, row_digest, to_pg

SCHEMA = "legacy_md"

BIG = 2**63 - 1
SMALL = -(2**63)
FLOATS = [
    0.1 + 0.2,
    1e-300,
    5e-324,  # smallest subnormal
    -0.0,
    math.inf,
    -math.inf,
    1.7976931348623157e308,
    123456789.12345679,
    2.0**53 + 2,
]
DECIMAL_TEXT = "123.456789012345678901234567890123"
UNICODE = "Облигация ОФЗ 26238 — ₽ 🙂 é́ \t\n\r\\ ' \" ,"

TYPED_ROWS: list[tuple[Any, ...]] = [
    (1, 0, 0.0, "", b""),
    (2, BIG, FLOATS[0], DECIMAL_TEXT, b"\x00\x01\xff"),
    (3, SMALL, FLOATS[1], UNICODE, bytes(range(256))),
    (4, None, None, None, None),
    *[(10 + i, i, f, repr(f), None) for i, f in enumerate(FLOATS[2:])],
]
MIXED_VALUES: list[Any] = [1, 2.5, "abc", b"\x00blob", None, BIG, SMALL, -0.0, "1", "", b"", "Ж"]
NUMERIC_VALUES: list[Any] = [1, 2.5, 10**15, 1e20, -3.25, None, 0.1]


def _build(path: Path) -> dict[str, int]:
    """Synthetic legacy DB; returns {table: rows}."""
    c = sqlite3.connect(path)
    c.executescript(
        '''
        CREATE TABLE typed (
            id INTEGER PRIMARY KEY, i INTEGER, r REAL, t TEXT, b BLOB
        ) STRICT;
        CREATE TABLE mixed (id INTEGER PRIMARY KEY AUTOINCREMENT, v);
        CREATE TABLE numish (id INTEGER PRIMARY KEY, n NUMERIC, z, a ANY_TYPE);
        CREATE TABLE nul_text (k TEXT PRIMARY KEY, t TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE "Odd ""Name""" ("MixedCase" TEXT, "select" INTEGER, UNIQUE ("MixedCase"));
        CREATE TABLE empty_tbl (a INTEGER NOT NULL, b REAL, c TEXT, d BLOB, e NUMERIC, f);
        CREATE TABLE nullpk (k TEXT PRIMARY KEY, v INTEGER);
        CREATE TABLE idx_tbl (id INTEGER PRIMARY KEY, a TEXT, b TEXT, n INTEGER);
        CREATE UNIQUE INDEX idx_tbl_a ON idx_tbl(a) WHERE a IS NOT NULL;
        CREATE UNIQUE INDEX idx_tbl_b_pos ON idx_tbl(b) WHERE n > 0;
        CREATE INDEX idx_tbl_expr ON idx_tbl(lower(a));
        CREATE INDEX idx_tbl_n ON idx_tbl(n);
        CREATE VIEW v_typed AS SELECT id FROM typed;
        CREATE TRIGGER typed_no_update BEFORE UPDATE ON typed
            BEGIN SELECT RAISE(ABORT, 'append-only'); END;
        '''
    )
    c.executemany("INSERT INTO typed VALUES (?, ?, ?, ?, ?)", TYPED_ROWS)
    c.executemany("INSERT INTO mixed (v) VALUES (?)", [(v,) for v in MIXED_VALUES])
    c.executemany(
        "INSERT INTO numish (n, z, a) VALUES (?, ?, ?)",
        [(v, 0.0 if i == 0 else i, "x" if i % 2 else i) for i, v in enumerate(NUMERIC_VALUES)],
    )
    c.executemany("INSERT INTO nul_text VALUES (?, ?)", [("a", "nul\x00inside"), ("b", "plain")])
    c.executemany('INSERT INTO "Odd ""Name""" VALUES (?, ?)', [("X", 1), ("y", 2), (None, 3)])
    c.executemany("INSERT INTO nullpk VALUES (?, ?)", [(None, 1), (None, 2), ("k", 3)])
    c.executemany(
        "INSERT INTO idx_tbl (a, b, n) VALUES (?, ?, ?)",
        [("p", "dup", 0), ("q", "dup", 0), (None, "dup", 1), (None, None, 2)],
    )
    c.commit()
    counts = {
        str(t): c.execute(f'SELECT count(*) FROM "{t.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
        for (t,) in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid"
        ).fetchall()
    }
    c.close()
    return counts


def _write_snapshot(path: Path, counts: dict[str, int], **override: int) -> None:
    tables = {t: {"rows": override.get(t, n), "date_ranges": {}} for t, n in counts.items()}
    tables.pop("sqlite_sequence", None)  # like the real snapshots: internal table not listed
    path.with_name("SNAPSHOT.json").write_text(
        json.dumps({"source": "synthetic", "tables": tables}), encoding="utf-8"
    )


@pytest.fixture
def lite(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    d = tmp_path / "md"
    d.mkdir()
    path = d / "market-data.sqlite3"
    counts = _build(path)
    _write_snapshot(path, counts)
    return path, counts


@pytest.fixture
def database(db_url: str) -> Iterator[Database]:
    d = Database(db_url)
    try:
        yield d
    finally:
        d.close()


def _sqlite_rows(path: Path, table: str) -> list[tuple[Any, ...]]:
    c = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        q = '"' + table.replace('"', '""') + '"'
        return [tuple(r) for r in c.execute(f"SELECT * FROM {q}")]
    finally:
        c.close()


def _pg_rows(db_url: str, table: str) -> list[tuple[Any, ...]]:
    """Rows of the copy, reconstructed into SQLite values (tagged columns folded)."""
    with psycopg.connect(db_url) as conn, conn.cursor(row_factory=tuple_row) as cur:
        cols = cur.execute(
            "SELECT pg_table, pg_column, typeof_column FROM legacy_md._sqlite_columns "
            "WHERE table_name = %s ORDER BY cid",
            (table,),
        ).fetchall()
        sel: list[sql.Composable] = []
        for _t, c, tc in cols:
            sel.append(sql.Identifier(c))
            sel.append(sql.Identifier(tc) if tc else sql.SQL("NULL"))
        rows = cur.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sel), sql.Identifier(SCHEMA, cols[0][0])
            )
        ).fetchall()
    out = []
    for r in rows:
        vals = []
        for i, (_t, _c, tc) in enumerate(cols):
            v, tag = r[2 * i], r[2 * i + 1]
            vals.append(from_pg(v, tag) if tc else v)
        out.append(tuple(vals))
    return out


def _multiset(rows: list[tuple[Any, ...]]) -> list[bytes]:
    return sorted(row_digest(r) for r in rows)


def _pg_types(db_url: str, table: str) -> dict[str, str]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT a.attname AS col, format_type(a.atttypid, a.atttypmod) AS typ
            FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (SCHEMA, table),
        ).fetchall()
    return {r["col"]: r["typ"] for r in rows}


# ------------------------------------------------------------------------------ unit tests


def _col(counts: dict[str, int], affinity: str = "TEXT", **kw: int) -> ColumnInfo:
    return ColumnInfo(
        cid=0, name="c", declared_type="", affinity=affinity, notnull=False, default=None,
        pk=0, hidden=0, counts=counts, **kw,
    )  # fmt: skip


def test_choose_pg_type() -> None:
    assert choose_pg_type(_col({"integer": 3, "null": 1})) == ("bigint", False)
    assert choose_pg_type(_col({"real": 3})) == ("double precision", False)
    assert choose_pg_type(_col({"text": 3})) == ("text", False)
    assert choose_pg_type(_col({"blob": 3})) == ("bytea", False)
    assert choose_pg_type(_col({"null": 2}, "INTEGER")) == ("bigint", False)
    assert choose_pg_type(_col({}, "NUMERIC")) == ("numeric", False)
    assert choose_pg_type(_col({}, "BLOB")) == ("bytea", False)
    assert choose_pg_type(_col({"integer": 1, "real": 1})) == ("numeric", True)
    assert choose_pg_type(_col({"integer": 1, "real": 1}, real_special=1)) == ("text", True)
    assert choose_pg_type(_col({"integer": 1, "text": 1})) == ("text", True)
    assert choose_pg_type(_col({"text": 1, "blob": 1})) == ("bytea", True)
    assert choose_pg_type(_col({"text": 2}, text_with_nul=1)) == ("bytea", True)


def test_affinity_rules() -> None:
    assert column_affinity("BIGINT") == "INTEGER"
    assert column_affinity("VARCHAR(10)") == "TEXT"
    assert column_affinity("") == "BLOB"
    assert column_affinity("DOUBLE PRECISION") == "REAL"
    assert column_affinity("DECIMAL(10,5)") == "NUMERIC"
    assert column_affinity("ANY", strict=True) == "BLOB"


def test_canonical_encoding_is_type_aware_and_injective() -> None:
    assert encode_value(1) != encode_value(1.0) != encode_value("1")
    assert encode_value("1") != encode_value(b"1")
    assert encode_value(None) != encode_value("")
    assert encode_value(-0.0) != encode_value(0.0)
    assert row_digest(("a", "bc")) != row_digest(("ab", "c"))
    with pytest.raises(TypeError):
        encode_value(object())


@pytest.mark.parametrize("pg_type", ["numeric", "text", "bytea"])
def test_tagged_conversion_roundtrip(pg_type: str) -> None:
    conv = to_pg(pg_type)
    values: list[Any] = [1, BIG, SMALL, 2.5, 1e20, 5e-324, "x", "Ж", None]
    if pg_type != "numeric":
        values += [-0.0, math.inf, "", "nul\x00"]
    if pg_type == "bytea":
        values += [b"", b"\x00\xff"]
    for v in values:
        if pg_type == "numeric" and not isinstance(v, (int, float, type(None))):
            continue
        pv, tag = conv(v)
        back = from_pg(pv, tag)
        assert type(back) is type(v) and encode_value(back) == encode_value(v), (pg_type, v)


def test_pg_ident() -> None:
    assert pg_ident("MixedCase") == "mixedcase"
    assert pg_ident("ÄBC") == "Äbc"  # ASCII-only folding, like SQLite
    with pytest.raises(LegacyImportError):
        pg_ident("x" * 64)


# ------------------------------------------------------------------------ import / verify


def test_import_is_lossless(
    lite: tuple[Path, dict[str, int]], database: Database, db_url: str
) -> None:
    path, counts = lite
    res = import_sqlite(database, path, SCHEMA)
    assert res.table_counts == counts
    assert res.snapshot_found
    assert set(res.tagged_columns) == {
        "mixed.v", "numish.n", "numish.z", "numish.a", "nul_text.t",
    }  # fmt: skip

    for table in counts:
        assert _multiset(_pg_rows(db_url, table)) == _multiset(_sqlite_rows(path, table)), table

    # exact values, not just equal hashes
    typed = {r[0]: r for r in _pg_rows(db_url, "typed")}
    assert typed[2][1] == BIG and typed[3][1] == SMALL
    assert typed[2][2] == 0.1 + 0.2 and typed[2][3] == DECIMAL_TEXT
    assert typed[3][3] == UNICODE and typed[3][4] == bytes(range(256))
    assert typed[1][4] == b"" and typed[1][3] == "" and typed[4][1:] == (None,) * 4
    assert typed[12][2] == math.inf and typed[13][2] == -math.inf
    mixed = [r[1] for r in sorted(_pg_rows(db_url, "mixed"))]
    assert [type(v) for v in mixed] == [type(v) for v in MIXED_VALUES]
    assert [encode_value(v) for v in mixed] == [encode_value(v) for v in MIXED_VALUES]
    assert math.copysign(1.0, mixed[7]) == -1.0  # -0.0 kept (no-affinity column)

    assert _pg_types(db_url, "typed") == {
        "id": "bigint", "i": "bigint", "r": "double precision", "t": "text", "b": "bytea",
    }  # fmt: skip
    assert _pg_types(db_url, "numish") == {
        "id": "bigint", "n": "numeric", "n__typeof": "text", "z": "text", "z__typeof": "text",
        "a": "text", "a__typeof": "text",
    }  # fmt: skip
    assert _pg_types(db_url, "nul_text")["t"] == "bytea"
    assert _pg_types(db_url, "empty_tbl") == {
        "a": "bigint", "b": "double precision", "c": "text", "d": "bytea", "e": "numeric",
        "f": "bytea",
    }  # fmt: skip
    assert set(_pg_types(db_url, 'odd "name"')) == {"mixedcase", "select"}

    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        master = conn.execute(
            "SELECT type, name, sql FROM legacy_md._sqlite_schema ORDER BY seq"
        ).fetchall()
        kinds = {(t, n) for t, n, _ in master}
        assert ("trigger", "typed_no_update") in kinds and ("view", "v_typed") in kinds
        trig = next(s for t, n, s in master if n == "typed_no_update")
        assert "RAISE(ABORT, 'append-only')" in trig
        # triggers / views are recorded, not re-created; the copy is writable
        assert not conn.execute(
            "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'legacy_md'"
        ).fetchall()
        assert conn.execute("SELECT to_regclass('legacy_md.v_typed')").fetchone() == (None,)
        cons = {
            (r[0], r[1], r[2]): (r[3], r[4])
            for r in conn.execute(
                "SELECT table_name, kind, coalesce(sqlite_name, ''), status, columns "
                "FROM legacy_md._sqlite_constraints"
            )
        }
        assert cons[("typed", "primary_key", "")] == ("created", ["id"])
        assert cons[("nul_text", "primary_key", "")][0] == "created"
        assert cons[("nullpk", "unique", "")][0] == "created"  # PK with NULLs -> UNIQUE
        assert cons[("idx_tbl", "unique", "idx_tbl_a")][0] == "created"
        assert cons[("idx_tbl", "unique", "idx_tbl_b_pos")][0] == "skipped"
        assert cons[("idx_tbl", "index", "idx_tbl_expr")][0] == "skipped"
        assert cons[("idx_tbl", "index", "idx_tbl_n")][0] == "created"
        assert cons[('Odd "Name"', "unique", 'sqlite_autoindex_Odd "Name"_1')][0] == "created"
        pk = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'legacy_md.typed'::regclass AND contype = 'p'"
        ).fetchone()
        assert pk == ("PRIMARY KEY (id)",)
        imp = conn.execute(
            "SELECT source_sha256, snapshot_json->>'source', table_counts, verified, "
            "sqlite_meta->'master_counts' FROM ops.legacy_import WHERE id = %s",
            (res.import_id,),
        ).fetchone()
    assert imp is not None
    assert imp[0] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert imp[1] == "synthetic" and imp[2] == counts and imp[3] is False
    assert imp[4]["trigger"] == 1 and imp[4]["view"] == 1

    v = verify_schema(database, SCHEMA)
    assert v.ok, v.failures
    assert any("sqlite_sequence" in w for w in v.warnings)
    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        row = conn.execute(
            "SELECT verified, table_checksums FROM ops.legacy_import WHERE id = %s",
            (res.import_id,),
        ).fetchone()
    assert row is not None and row[0] is True
    assert set(row[1]) == {*counts, "_sqlite_schema"}


def test_reimport_refuses_and_drop_existing_is_idempotent(
    lite: tuple[Path, dict[str, int]], database: Database, db_url: str
) -> None:
    path, _ = lite
    import_sqlite(database, path, SCHEMA)
    first = verify_schema(database, SCHEMA)
    assert first.ok, first.failures
    with pytest.raises(LegacyImportError, match="drop-existing"):
        import_sqlite(database, path, SCHEMA)
    import_sqlite(database, path, SCHEMA, drop_existing=True)
    second = verify_schema(database, SCHEMA)
    assert second.ok, second.failures
    assert second.import_id != first.import_id
    assert {t.table: t.pg_checksum for t in second.tables} == {
        t.table: t.pg_checksum for t in first.tables
    }


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE legacy_md.typed SET r = 0.30000000000000004 + 1e-16 WHERE id = 2",  # 1 ulp
        "UPDATE legacy_md.typed SET t = t || ' ' WHERE id = 3",
        "UPDATE legacy_md.mixed SET v__typeof = 'text' WHERE v__typeof = 'integer' AND v = '1'",
        "UPDATE legacy_md.typed SET b = NULL WHERE id = 1",  # b'' -> NULL
        "DELETE FROM legacy_md.nullpk WHERE k = 'k'",
        "INSERT INTO legacy_md.typed (id) VALUES (999)",
        "ALTER TABLE legacy_md.typed DROP COLUMN b",
        "CREATE TABLE legacy_md.extra (x int)",
        "DROP TABLE legacy_md.numish",
        "UPDATE legacy_md._sqlite_schema SET sql = 'x' WHERE type = 'trigger'",
    ],
)
def test_verify_detects_tampering(
    lite: tuple[Path, dict[str, int]], database: Database, db_url: str, tamper: str
) -> None:
    path, _ = lite
    import_sqlite(database, path, SCHEMA)
    with psycopg.connect(db_url) as conn:
        conn.execute(tamper)
    res = verify_schema(database, SCHEMA)
    assert not res.ok
    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        row = conn.execute(
            "SELECT verified, verify_report->'problems' FROM ops.legacy_import "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row[0] is False and row[1]


def test_verify_detects_snapshot_and_source_mismatch(
    lite: tuple[Path, dict[str, int]], database: Database
) -> None:
    path, counts = lite
    _write_snapshot(path, counts, typed=counts["typed"] + 1)
    import_sqlite(database, path, SCHEMA)
    res = verify_schema(database, SCHEMA)
    assert not res.ok
    assert any("SNAPSHOT.json" in p for p in res.failures)

    # the source file changed after the import
    c = sqlite3.connect(path)
    c.execute("INSERT INTO mixed (v) VALUES (42)")
    c.commit()
    c.close()
    res = verify_schema(database, SCHEMA)
    assert any("SHA-256" in p for p in res.failures)
    assert any(p.startswith("mixed:") for p in res.failures)


def test_import_rejects_manifest_mismatch(
    lite: tuple[Path, dict[str, int]], database: Database
) -> None:
    path, _ = lite
    path.with_name("MANIFEST.sha256").write_text(f"{'0' * 64}  {path.name}\n")
    with pytest.raises(LegacyImportError, match="MANIFEST"):
        import_sqlite(database, path, SCHEMA)
    path.with_name("MANIFEST.sha256").write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    )
    assert import_sqlite(database, path, SCHEMA).table_counts


def test_import_failure_leaves_schema_untouched(
    lite: tuple[Path, dict[str, int]],
    database: Database,
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _ = lite
    mod = importlib.import_module("mdhub.legacy.import_sqlite")

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_insert_meta", boom)
    with pytest.raises(RuntimeError):
        import_sqlite(database, path, SCHEMA)
    with psycopg.connect(db_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'legacy_md'"
        ).fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM ops.legacy_import").fetchone() == (0,)


def test_source_is_opened_read_only(lite: tuple[Path, dict[str, int]], database: Database) -> None:
    path, _ = lite
    path.chmod(0o444)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    import_sqlite(database, path, SCHEMA)
    assert verify_schema(database, SCHEMA).ok
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in path.parent.iterdir()) == sorted([path.name, "SNAPSHOT.json"])


# ---------------------------------------------------------------------------------- CLI


@pytest.fixture
def runner(db_url: str, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    return CliRunner()


def test_cli_import_and_verify(
    lite: tuple[Path, dict[str, int]], tmp_path: Path, runner: CliRunner, db_url: str
) -> None:
    md, _ = lite
    bc_dir = tmp_path / "bc"
    bc_dir.mkdir()
    bc = bc_dir / "bond_catalog.sqlite3"
    c = sqlite3.connect(bc)
    c.executescript("CREATE TABLE issues (secid TEXT PRIMARY KEY, facevalue REAL);")
    c.execute("INSERT INTO issues VALUES ('SU26238RMFS4', 1000.0)")
    c.commit()
    c.close()

    res = runner.invoke(cli.app, ["legacy", "import", "--md", str(md), "--bc", str(bc)])
    assert res.exit_code == 0, res.output
    assert "legacy_md: import #" in res.output and "legacy_bc: import #" in res.output

    res = runner.invoke(cli.app, ["legacy", "import", "--bc", str(bc)])
    assert res.exit_code == 1 and "--drop-existing" in res.output

    res = runner.invoke(cli.app, ["legacy", "import", "--bc", str(bc), "--drop-existing"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli.app, ["legacy", "verify"])
    assert res.exit_code == 0, res.output
    assert "legacy_md: import #" in res.output and "verified OK" in res.output
    assert "legacy_bc.issues" in res.output and "MISMATCH" not in res.output
    assert "snapshot=-" in res.output  # bc has no SNAPSHOT.json

    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE legacy_bc.issues SET facevalue = 1000.0000000001")
    res = runner.invoke(cli.app, ["legacy", "verify", "--bc", str(bc)])
    assert res.exit_code == 1
    assert "content checksum differs" in res.output


def test_cli_usage_errors(runner: CliRunner, tmp_path: Path) -> None:
    res = runner.invoke(cli.app, ["legacy", "import"])
    assert res.exit_code == 2
    res = runner.invoke(cli.app, ["legacy", "import", "--md", str(tmp_path / "missing.sqlite3")])
    assert res.exit_code == 2
    res = runner.invoke(cli.app, ["legacy", "verify"])
    assert res.exit_code == 1 and "no legacy import" in res.output
