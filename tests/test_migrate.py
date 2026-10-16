from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest

from mdhub.db.migrate import (
    MIGRATIONS_DIR,
    ChecksumMismatchError,
    MigrationError,
    MigrationLockTimeoutError,
    applied_migrations,
    discover_migrations,
    migrate,
    pending_migrations,
)


def _copy_core(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_core.sql", d / "0001_core.sql")
    return d


def test_packaged_migrations_are_well_formed() -> None:
    migs = discover_migrations()
    assert migs[0].version == "0001_core"
    assert [m.number for m in migs] == sorted(m.number for m in migs)


def test_migrate_is_idempotent(empty_db_url: str) -> None:
    first = migrate(empty_db_url)
    assert first.applied == ["0001_core"]
    second = migrate(empty_db_url)
    assert second.applied == []
    assert second.already_applied == ["0001_core"]
    with psycopg.connect(empty_db_url) as conn:
        applied = applied_migrations(conn)
        assert set(applied) == {"0001_core"}
        assert applied["0001_core"] == discover_migrations()[0].checksum
        schemas = {
            r[0]
            for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname IN "
                "('ops','ref','md','legacy_md','legacy_bc')"
            )
        }
        assert schemas == {"ops", "ref", "md", "legacy_md", "legacy_bc"}
        for table in (
            "ops.source",
            "ops.job_run",
            "ops.raw_capture",
            "ops.row_history",
            "ops.data_gap",
            "ops.backfill_task",
            "ops.schema_migration",
        ):
            row = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            assert row is not None and row[0] is not None, table
        assert pending_migrations(conn) == []
        sources = {r[0] for r in conn.execute("SELECT code FROM ops.source")}
        assert {"moex_iss", "moex_apim", "cbr", "ecb", "ratescalcs", "cbonds"} <= sources


def test_checksum_guard_refuses_modified_migration(empty_db_url: str, tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    (d / "0002_extra.sql").write_text("CREATE TABLE md.extra (id int);\n")
    assert migrate(empty_db_url, directory=d).applied == ["0001_core", "0002_extra"]

    (d / "0002_extra.sql").write_text("CREATE TABLE md.extra (id bigint);\n")
    (d / "0003_more.sql").write_text("CREATE TABLE md.more (id int);\n")
    with pytest.raises(ChecksumMismatchError, match="0002_extra"):
        migrate(empty_db_url, directory=d)
    with psycopg.connect(empty_db_url) as conn:
        # Nothing was applied after the guard tripped.
        assert "0003_more" not in applied_migrations(conn)
        assert conn.execute("SELECT to_regclass('md.more')").fetchone() == (None,)


def test_failed_migration_rolls_back_only_itself(empty_db_url: str, tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    (d / "0002_broken.sql").write_text(
        "CREATE TABLE md.half (id int);\nSELECT no_such_function();\n"
    )
    with pytest.raises(MigrationError, match="0002_broken"):
        migrate(empty_db_url, directory=d)
    with psycopg.connect(empty_db_url) as conn:
        assert set(applied_migrations(conn)) == {"0001_core"}
        assert conn.execute("SELECT to_regclass('md.half')").fetchone() == (None,)


def test_bad_names_and_duplicates_rejected(tmp_path: Path) -> None:
    d = _copy_core(tmp_path)
    (d / "2_bad.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="bad migration file name"):
        discover_migrations(d)
    (d / "2_bad.sql").unlink()
    (d / "0001_other.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="duplicate migration number"):
        discover_migrations(d)


def test_advisory_lock_blocks_concurrent_runner(empty_db_url: str) -> None:
    with psycopg.connect(empty_db_url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.migrate', 0))")
        with pytest.raises(MigrationLockTimeoutError):
            migrate(empty_db_url, lock_timeout=0.3)
    assert migrate(empty_db_url).applied == ["0001_core"]


def test_enable_history_validates_columns(conn: psycopg.Connection[dict[str, object]]) -> None:
    conn.execute("CREATE TABLE md.h (k int PRIMARY KEY, v int)")
    with pytest.raises(psycopg.errors.RaiseException, match="does not exist"):
        conn.execute("CALL ops.enable_history('md.h', ARRAY['nope'])")
    conn.rollback()
    conn.execute("CREATE TABLE md.h (k int PRIMARY KEY, v int)")
    conn.execute("CALL ops.enable_history('md.h', ARRAY['k'])")
    conn.execute("CALL ops.enable_history('md.h', ARRAY['k'])")  # idempotent
    row = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger WHERE tgrelid = 'md.h'::regclass "
        "AND tgname = 'mdhub_row_history'"
    ).fetchone()
    assert row == {"n": 1}
