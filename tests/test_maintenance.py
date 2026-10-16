"""maintenance job: raw retention (intraday only), VACUUM, backup command/rotation, restore."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pgserver
import psycopg
import pytest
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import cli
from mdhub.config import Settings
from mdhub.db.pool import Database
from mdhub.jobs import run_job
from mdhub.loaders import maintenance as mt

Conn = psycopg.Connection[DictRow]
PG_BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"
assert callable(mt.maintenance)  # importing registers the `maintenance` job


def add_capture(conn: Conn, endpoint: str, age_days: int, tag: str) -> int:
    row = conn.execute(
        """
        INSERT INTO ops.raw_capture (source, endpoint, url_redacted, fetched_at, parser_version,
            sha256, payload_gz, size_bytes)
        VALUES ('moex_iss', %s, 'https://x', now() - make_interval(days => %s), '1',
                encode(sha256(%s::bytea), 'hex'), '\\x00', 1)
        RETURNING id
        """,
        (endpoint, age_days, tag.encode()),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_intraday_classification() -> None:
    assert mt.is_intraday_endpoint("intraday:bonds_marketdata")
    assert not mt.is_intraday_endpoint("bonds_marketdata")
    assert not mt.is_intraday_endpoint("bonds_history")


def test_retention_deletes_only_old_intraday(conn: Conn, db: Database) -> None:
    old_intraday = [add_capture(conn, "intraday:cets", 100, f"a{i}") for i in range(7)]
    new_intraday = add_capture(conn, "intraday:cets", 10, "b")
    old_eod = add_capture(conn, "bonds_history", 400, "c")
    old_other = add_capture(conn, "not_intraday:x", 400, "d")
    conn.commit()
    assert mt.purge_intraday_raw(db, 90, batch=3) == 7
    left = {r["id"] for r in conn.execute("SELECT id FROM ops.raw_capture").fetchall()}
    assert left == {new_intraday, old_eod, old_other}
    assert not set(old_intraday) & left
    assert mt.purge_intraday_raw(db, 5) == 1
    with pytest.raises(ValueError, match=">= 1"):
        mt.purge_intraday_raw(db, 0)


def test_retention_skips_captures_referenced_by_blocking_fks(
    conn: Conn, db: Database, settings: Settings
) -> None:
    cap = add_capture(conn, "intraday:futures", 200, "x")
    keep = add_capture(conn, "intraday:futures", 200, "y")
    conn.execute(
        "CREATE TABLE md.mt_ok (id int, capture_id bigint REFERENCES ops.raw_capture (id) "
        "ON DELETE SET NULL)"
    )
    conn.execute("INSERT INTO md.mt_ok VALUES (1, %s)", (cap,))
    conn.commit()
    out = run_job("maintenance", {"mode": "retention", "vacuum": "false"}, settings=settings, db=db)
    assert out.status == "succeeded" and out.result["raw_deleted"] == 2
    assert out.result["retention_days"] == settings.raw_retention_days_intraday
    assert conn.execute("SELECT capture_id FROM md.mt_ok").fetchone() == {"capture_id": None}

    blocked = add_capture(conn, "intraday:futures", 200, "z")
    conn.execute("CREATE TABLE md.mt_bad (capture_id bigint REFERENCES ops.raw_capture (id))")
    conn.execute("INSERT INTO md.mt_bad VALUES (%s)", (blocked,))
    conn.commit()
    # a NOT NULL reference (like md.zcyc_reject.capture_id, its primary key)
    rejected = add_capture(conn, "intraday:zcyc", 200, "r")
    conn.execute(
        "CREATE TABLE md.mt_reject (capture_id bigint PRIMARY KEY "
        "REFERENCES ops.raw_capture (id) ON DELETE SET NULL)"
    )
    conn.execute("INSERT INTO md.mt_reject VALUES (%s)", (rejected,))
    gone = add_capture(conn, "intraday:futures", 200, "g")
    conn.commit()
    refs = mt.blocking_capture_refs(conn)
    assert ("md", "mt_bad", "capture_id") in refs and ("md", "mt_reject", "capture_id") in refs
    assert ("md", "mt_ok", "capture_id") not in refs
    bad = run_job(
        "maintenance",
        {"mode": "retention", "retention_days": "30", "vacuum_min_mb": "0"},
        settings=settings,
        db=db,
    )
    assert bad.status == "succeeded" and bad.result["raw_deleted"] == 1  # only `gone`
    assert "ops.raw_capture" in bad.result["vacuumed"] and "md.mt_bad" in bad.result["vacuumed"]
    left = {r["id"] for r in conn.execute("SELECT id FROM ops.raw_capture").fetchall()}
    assert {blocked, rejected} <= left and gone not in left
    assert keep  # both deleted in the first run


def test_vacuum_selects_large_tables(db: Database) -> None:
    done = mt.vacuum_tables(db, min_mb=10_000)
    assert done == ["ops.raw_capture"]
    assert "ops.job_run" in mt.vacuum_tables(db, min_mb=0)


def test_unknown_mode_fails(settings: Settings, db: Database) -> None:
    with pytest.raises(ValueError, match="unknown maintenance mode"):
        run_job("maintenance", {"mode": "nope"}, settings=settings, db=db)


# ------------------------------------------------------------------------------ backup


def test_backup_command_keeps_password_out_of_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mt.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv, env = mt.backup_command(
        "postgresql://mdhub:s3cr3t-pass@db.example:5432/mdhub", Path("/b/x.dump.partial")
    )
    assert argv[0] == "/usr/bin/pg_dump"
    assert "--format=custom" in argv and "--file=/b/x.dump.partial" in argv
    assert "--no-password" in argv
    assert not any("s3cr3t-pass" in a for a in argv)
    dbname = next(a for a in argv if a.startswith("--dbname="))
    assert "host=db.example" in dbname and "dbname=mdhub" in dbname
    assert env == {"PGPASSWORD": "s3cr3t-pass"}

    argv2, env2 = mt.restore_command("dbname=mdhub host=/tmp", Path("/s/seed.dump"), clean=True)
    assert argv2[0] == "/usr/bin/pg_restore" and argv2[-1] == "/s/seed.dump"
    assert {
        "--no-owner",
        "--no-privileges",
        "--clean",
        "--if-exists",
        "--single-transaction",
    } <= set(argv2)
    assert env2 == {}
    argv3, _ = mt.restore_command("dbname=mdhub", Path("/s.dump"), jobs=4)
    assert "--jobs=4" in argv3 and "--single-transaction" not in argv3
    with pytest.raises(ValueError, match="jobs"):
        mt.restore_command("dbname=mdhub", Path("/s.dump"), jobs=0)

    monkeypatch.setattr(mt.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="postgresql-client-16"):
        mt.backup_command("dbname=x", Path("/x"))


def test_backup_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mt.BACKUP_DIR_ENV, raising=False)
    assert mt.backup_dir() == Path("/var/backups/mdhub")
    monkeypatch.setenv(mt.BACKUP_DIR_ENV, "/srv/bk")
    assert mt.backup_dir() == Path("/srv/bk")
    assert mt.backup_dir("/other") == Path("/other")


def test_backup_job_with_mocked_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings, db: Database
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], env: dict[str, str], timeout: float) -> Any:
        calls.append((argv, env))
        target = Path(next(a for a in argv if a.startswith("--file=")).split("=", 1)[1])
        target.write_bytes(b"PGDMP fake")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(mt, "_run_command", fake_run)
    monkeypatch.setenv(mt.BACKUP_DIR_ENV, str(tmp_path))
    old = []
    for i in range(4):
        p = tmp_path / f"mdhub-old-2026010{i}T000000Z.dump"
        p.write_bytes(b"x")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        old.append(p)
    (tmp_path / "unrelated.txt").write_text("keep me")

    out = run_job("maintenance", {"mode": "backup", "keep": "3"}, settings=settings, db=db)
    assert out.status == "succeeded"
    path = Path(out.result["path"])
    assert path.parent == tmp_path and path.name.startswith("mdhub-") and path.suffix == ".dump"
    assert path.read_bytes() == b"PGDMP fake"
    assert len(calls) == 1 and calls[0][0][0] == "/usr/bin/pg_dump"
    remaining = sorted(p.name for p in tmp_path.glob("mdhub-*.dump"))
    assert len(remaining) == 3 and path.name in remaining
    assert {Path(p).name for p in out.result["removed"]} == {old[0].name, old[1].name}
    assert (tmp_path / "unrelated.txt").exists()
    assert not list(tmp_path.glob("*.partial"))


def test_backup_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db: Database
) -> None:
    def failing(argv: list[str], env: dict[str, str], timeout: float) -> Any:
        target = Path(next(a for a in argv if a.startswith("--file=")).split("=", 1)[1])
        target.write_bytes(b"partial")
        return subprocess.CompletedProcess(argv, 1, "", "pg_dump: error: auth failed for TOPSECRET")

    monkeypatch.setattr(mt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(mt, "_run_command", failing)
    with pytest.raises(RuntimeError, match="exited with 1") as exc:
        mt.run_backup(db, directory=tmp_path, keep=2, secrets=["TOPSECRET"])
    assert "TOPSECRET" not in str(exc.value)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="keep"):
        mt.rotate_backups(tmp_path, 0)


@pytest.mark.skipif(not (PG_BIN / "pg_dump").exists(), reason="pgserver pg_dump not available")
def test_real_backup_and_restore_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conn: Conn,
    db: Database,
    pg_server: Any,
    settings: Settings,
) -> None:
    monkeypatch.setenv("PATH", f"{PG_BIN}{os.pathsep}{os.environ.get('PATH', '')}")
    conn.execute("CREATE TABLE md.mt_roundtrip (k int PRIMARY KEY, v text)")
    conn.execute("INSERT INTO md.mt_roundtrip VALUES (1, 'one'), (2, 'two')")
    conn.commit()
    res = mt.run_backup(
        db, directory=tmp_path, keep=5, secrets=[], now=datetime(2026, 10, 16, tzinfo=UTC)
    )
    assert res.path.name.endswith("-20261016T000000Z.dump") and res.size_bytes > 0

    target = pg_server.create_db()
    try:
        url = pg_server.url(target)
        runner = CliRunner()
        monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
        monkeypatch.setenv("MDHUB_DATABASE_URL", url)
        out = runner.invoke(cli.app, ["ops", "restore", str(res.path), "--yes"])
        assert out.exit_code == 0, out.output
        with psycopg.connect(url) as c:
            rows = c.execute("SELECT k, v FROM md.mt_roundtrip ORDER BY k").fetchall()
            assert rows == [(1, "one"), (2, "two")]
            n = c.execute("SELECT count(*) FROM ops.schema_migration").fetchone()
            assert n is not None and n[0] >= 1
        again = runner.invoke(cli.app, ["ops", "restore", str(res.path), "--yes"])
        assert again.exit_code == 1 and "restore failed" in again.output
        missing = runner.invoke(cli.app, ["ops", "restore", str(tmp_path / "nope.dump"), "-y"])
        assert missing.exit_code == 2
    finally:
        pg_server.drop_db(target)
