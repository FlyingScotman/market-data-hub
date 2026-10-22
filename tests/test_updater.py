"""Self-update (ADMIN_DESIGN §6, mdhub.updater): the state machine with a fake docker/compose
host - success, failure at each step, both automatic rollback paths, an admin rollback, crash
and resume (also mid-restore), refused archives - the GitHub source, the advisory lock and the
worker drain flag."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.admin import store
from mdhub.api.auth import token_digest
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.db.pool import Database
from mdhub.job_requests import JobRequestPoller
from mdhub.jobs import DRAINING, JobContext, JobRegistry
from mdhub.release import build_manifest, default_signature_path
from mdhub.schedule import Rule, _RuleRunner
from mdhub.updater import (
    ComposeHost,
    ContainerReporter,
    GitHub,
    Layout,
    UpdateError,
    Updater,
    _reload,
    parse_pct,
    parse_size,
    read_env_value,
)
from test_maintenance import PG_BIN
from test_release import COMMIT, SigningKey, _sums

Conn = psycopg.Connection[DictRow]
MIGRATIONS = Path(__file__).resolve().parents[1] / "src" / "mdhub" / "db" / "migrations"
EXTRA = "0699_updater_test"
EXTRA_SQL = b"CREATE TABLE ops.updater_test (x int);\n"


class Crash(BaseException):
    """The updater process dies (not an ``Exception``: nothing catches it)."""


class FakeHost:
    """Records compose / dump / restore / HTTP calls and simulates their effect on the DB."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.calls: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self.unhealthy: set[str] = set()
        self.running = "1.0.0"
        self.drain_at_stop: list[bool] = []

    def _fail(self, key: str) -> None:
        for pattern, exc in self.fail.items():
            if pattern in key:
                raise exc

    def _sql(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else None

    def compose(self, compose_file: Path, args: list[str], *, tag: str, timeout: float) -> None:
        key = f"{' '.join(args)} @{tag}"
        self.calls.append(key)
        assert compose_file.name == "docker-compose.yml"
        if args[:2] == ["stop", "worker"]:
            self.drain_at_stop.append(self._sql("SELECT drain FROM ops.worker_control")[0][0])
        self._fail(key)
        release = compose_file.parents[1]
        if "migrate" in args and (release / "src/mdhub/db/migrations" / f"{EXTRA}.sql").exists():
            self._sql(EXTRA_SQL.decode())
            self._sql(
                "INSERT INTO ops.schema_migration (version, checksum) VALUES (%s, %s)",
                (EXTRA, hashlib.sha256(EXTRA_SQL).hexdigest()),
            )
        if args[0] == "up" and "worker" in args:
            self.running = tag
            self._sql(
                "INSERT INTO ops.worker_heartbeat (instance_id, process, host, pid, version, "
                "started_at, beat_at) VALUES (%s, 'worker', 'h', 1, %s, now(), now())",
                (uuid.uuid4(), tag),
            )

    def dump(self, target: Path) -> None:
        self.calls.append("dump")
        self._fail("dump")
        target.write_bytes(b"PGDMP fake")

    def restore(self, dump: Path) -> None:
        self.calls.append(f"restore {dump.name}")
        # The pre-update dump predates the migration and the update's own rows.
        self._sql(f"DROP TABLE IF EXISTS ops.{EXTRA[5:]}")
        self._sql("DELETE FROM ops.schema_migration WHERE version = %s", (EXTRA,))
        self._sql("DELETE FROM ops.update_request")
        self._sql("DELETE FROM ops.release")
        self._fail("restore")

    def http_json(self, url: str, token: str | None = None) -> tuple[int, Any]:
        path = url.removeprefix("http://api:8080")
        self.calls.append(f"GET {path}")
        if self.running in self.unhealthy:
            return 503, {"status": "not_ready", "version": self.running}
        if path == "/health/ready":
            return 200, {"status": "ok", "version": self.running}
        rows = self._sql(
            "SELECT scopes FROM admin.api_token WHERE token_sha256 = %s AND revoked_at IS NULL",
            (token_digest(token or ""),),
        )
        return (200, {}) if rows and "read" in rows[0][0] else (401, None)


# --------------------------------------------------------------------------------- fixtures


def _tree(root: Path, version: str, *, extra: bool = False, min_previous: str = "0.1.0") -> None:
    """A release tree: the real migrations (plus EXTRA), a compose file, a pyproject."""
    shutil.copytree(MIGRATIONS, root / "src/mdhub/db/migrations")
    if extra:
        (root / "src/mdhub/db/migrations" / f"{EXTRA}.sql").write_bytes(EXTRA_SQL)
    (root / "deploy").mkdir(parents=True)
    (root / "deploy/docker-compose.yml").write_text(f"name: mdhub # {version}\n")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "market-data-hub"\n\n[tool.mdhub.release]\n'
        f'min_previous_version = "{min_previous}"\n'
    )


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def keys_dir(tmp_path: Path, key: SigningKey) -> Path:
    d = tmp_path / "release-keys"
    d.mkdir()
    (d / f"{key.key_id_hex}.pub").write_bytes(key.public_file())
    return d


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    lay = Layout(tmp_path / "opt")
    _tree(lay.release("1.0.0"), "1.0.0")
    lay.shared.mkdir(parents=True)
    lay.env_file.write_text("POSTGRES_PASSWORD=x\nMDHUB_IMAGE_TAG=1.0.0\n")
    lay.current.symlink_to("releases/1.0.0")
    return lay


@pytest.fixture
def host(db_url: str) -> FakeHost:
    return FakeHost(db_url)


def bundle(
    tmp_path: Path,
    key: SigningKey | None,
    version: str = "1.1.0",
    *,
    extra: bool = False,
    min_previous: str = "0.1.0",
    tamper: bool = False,
    change_migration: bool = False,
) -> Path:
    """A signed code-only bundle (``mdhub release`` format) in ``tmp_path/dist``."""
    stage = tmp_path / f"stage-{uuid.uuid4().hex[:8]}"
    root = stage / "mdhub"
    _tree(root, version, extra=extra, min_previous=min_previous)
    if change_migration:
        (root / "src/mdhub/db/migrations/0001_core.sql").write_text("-- changed\n")
    manifest = build_manifest(root, version=version, commit=COMMIT)
    (root / "MANIFEST.json").write_text(manifest.dumps())
    sums = _sums(root)
    (root / "SHA256SUMS").write_bytes(sums)
    if tamper:
        (root / "deploy/docker-compose.yml").write_text("services: {evil: {}}\n")
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / f"market-data-hub-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname="mdhub")
    sig = default_signature_path(archive)
    sig.unlink(missing_ok=True)
    if key is not None:
        sig.write_bytes(key.sign(sums))
    return archive


def upload(tmp_path: Path, archive: Path) -> str:
    """Put a bundle into the upload volume as the console does; returns its sha256."""
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)
    shutil.copyfile(archive, uploads / f"{sha}.tar.gz")
    if default_signature_path(archive).exists():
        shutil.copyfile(
            default_signature_path(archive), default_signature_path(uploads / f"{sha}.tar.gz")
        )
    return sha


def queue(conn: Conn, version: str, sha: str | None = None, kind: str = "install") -> int:
    source = "upload" if sha else ("release" if kind == "rollback" else "github")
    row = conn.execute(
        "INSERT INTO ops.update_request (kind, source, version, upload_sha256, requested_login) "
        "VALUES (%s, %s, %s, %s, 'alice') RETURNING id",
        (kind, source, version, sha),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row["id"])


def make(
    db_url: str, host: FakeHost, layout: Layout, tmp_path: Path, keys_dir: Path, **kw: Any
) -> Updater:
    return Updater(
        Database(db_url),
        host,
        layout,
        keys_dir=keys_dir,
        upload_dir=tmp_path / "uploads",
        backup_dir=tmp_path / "backups",
        api_url="http://api:8080",
        version="1.0.0",
        drain_timeout_s=0.3,
        check_timeout_s=0.2,
        poll_s=0.01,
        **kw,
    )


def req(conn: Conn, request_id: int) -> dict[str, Any]:
    conn.rollback()
    row = conn.execute("SELECT * FROM ops.update_request WHERE id = %s", (request_id,)).fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.parametrize("mask", [0o022, 0o777])
def test_updater_backup_permissions_during_write_and_after_rename(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mask: int,
) -> None:
    up = make(db_url, host, layout, tmp_path, keys_dir)
    up.backup_dir.mkdir()
    request_id = queue(conn, "1.1.0")
    request = {**req(conn, request_id), "from_version": "1.0.0"}
    written_inodes = []

    def dump(partial: Path) -> None:
        with partial.open("wb") as stream:
            st = os.fstat(stream.fileno())
            assert stat.S_IMODE(st.st_mode) == 0o600
            written_inodes.append(st.st_ino)
            stream.write(b"PGDMP fake")

    monkeypatch.setattr(host, "dump", dump)
    previous = os.umask(mask)
    try:
        up._backing_up(request)
    finally:
        os.umask(previous)
    final = Path(req(conn, request_id)["backup_path"])
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert final.stat().st_ino == written_inodes[0]
    assert not list(up.backup_dir.glob("*.partial"))


def test_updater_failed_dump_cleans_partial(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    up = make(db_url, host, layout, tmp_path, keys_dir)
    request_id = queue(conn, "1.1.0")

    def failing_dump(partial: Path) -> None:
        partial.write_bytes(b"incomplete")
        raise RuntimeError("dump failed")

    monkeypatch.setattr(host, "dump", failing_dump)
    with pytest.raises(RuntimeError, match="dump failed"):
        up._backing_up({**req(conn, request_id), "from_version": "1.0.0"})
    assert list(up.backup_dir.iterdir()) == []
    assert req(conn, request_id)["backup_path"] is None


@pytest.mark.skipif(not (PG_BIN / "pg_dump").exists(), reason="pgserver pg_dump not available")
def test_updater_real_dump_is_readable_and_private(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", f"{PG_BIN}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(host, "dump", ComposeHost(db_url).dump)
    up = make(db_url, host, layout, tmp_path, keys_dir)
    request_id = queue(conn, "1.1.0")
    up._backing_up({**req(conn, request_id), "from_version": "1.0.0"})
    final = Path(req(conn, request_id)["backup_path"])
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    listing = subprocess.run(
        [str(PG_BIN / "pg_restore"), "--list", str(final)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "schema_migration" in listing.stdout


def log_lines(conn: Conn, request_id: int) -> str:
    conn.rollback()
    rows = conn.execute(
        "SELECT step, level, line FROM ops.update_log WHERE request_id = %s ORDER BY id",
        (request_id,),
    ).fetchall()
    return "\n".join(f"[{r['step']}] {r['level']} {r['line']}" for r in rows)


def drain(conn: Conn) -> bool:
    conn.rollback()
    row = conn.execute("SELECT drain FROM ops.worker_control").fetchone()
    assert row is not None
    return bool(row["drain"])


def applied(conn: Conn) -> set[str]:
    conn.rollback()
    return {r["version"] for r in conn.execute("SELECT version FROM ops.schema_migration")}


UP_NEW = "up -d --no-deps worker api admin hostmon @1.1.0"
UP_OLD = "up -d --no-deps worker api admin hostmon @1.0.0"


# ------------------------------------------------------------------------------ success


def test_install_success_path(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    sha = upload(tmp_path, bundle(tmp_path, key, extra=True))
    rid = queue(conn, "1.1.0", sha)
    up = make(db_url, host, layout, tmp_path, keys_dir)
    assert up.claim() == rid
    assert up.process(rid) == "done", log_lines(conn, rid)

    assert host.calls == [
        "dump",
        "build @1.1.0",
        "stop worker @1.0.0",
        "run --rm --no-deps migrate @1.1.0",
        UP_NEW,
        "GET /health/ready",
        "GET /v1/coverage",
        "run -d --rm --no-deps --entrypoint docker updater compose -f "
        f"{layout.current}/deploy/docker-compose.yml up -d --no-deps updater @1.1.0",
    ]
    assert host.drain_at_stop == [True] and not drain(conn)
    r = req(conn, rid)
    assert r["state"] == "done" and r["error"] is None and r["finished_at"] is not None
    assert r["from_version"] == "1.0.0" and r["from_tag"] == "1.0.0"
    assert r["from_link"] == "releases/1.0.0" and r["archive_sha256"] == sha
    assert EXTRA in applied(conn) and EXTRA not in r["applied_before"]
    # switched: image tag in .env, current symlink, .env linked into the new release
    assert read_env_value(layout.env_file, "MDHUB_IMAGE_TAG") == "1.1.0"
    assert "POSTGRES_PASSWORD=x" in layout.env_file.read_text()
    assert str(layout.current.readlink()) == "releases/1.1.0"
    assert (layout.release("1.1.0") / "deploy/.env").resolve() == layout.env_file.resolve()
    backup = Path(r["backup_path"])
    assert backup.name.startswith("pre-update-") and "-1.0.0-to-1.1.0-" in backup.name
    assert backup.is_file()
    rows = {x["version"]: x for x in conn.execute("SELECT * FROM ops.release").fetchall()}
    assert rows["1.1.0"]["status"] == "active" and rows["1.1.0"]["previous_version"] == "1.0.0"
    assert rows["1.1.0"]["key_id"] == key.key_id_hex and rows["1.1.0"]["backup_path"] == str(backup)
    assert EXTRA in rows["1.1.0"]["migrations"] and rows["1.1.0"]["installed_by"] == "alice"
    assert rows["1.0.0"]["status"] == "previous" and rows["1.0.0"]["source"] == "manual"
    text = log_lines(conn, rid)
    for step in (
        "verifying",
        "backing_up",
        "staging",
        "draining",
        "migrating",
        "starting",
        "checking",
        "done",
    ):
        assert f"[{step}]" in text, (step, text)
    assert f"new migrations: {EXTRA}" in text and f"migrations applied: {EXTRA}" in text
    audit = conn.execute(
        "SELECT action, login, target FROM admin.audit_log WHERE target = %s ORDER BY id",
        (f"update:{rid}",),
    ).fetchall()
    assert [a["action"] for a in audit] == [
        f"update.{s}"
        for s in (
            "verifying",
            "backing_up",
            "staging",
            "draining",
            "migrating",
            "starting",
            "checking",
            "done",
        )
    ]
    assert {a["login"] for a in audit} == {"updater"}
    tok = conn.execute("SELECT revoked_at FROM admin.api_token WHERE name LIKE 'updater-smoke%'")
    assert all(t["revoked_at"] is not None for t in tok.fetchall())
    assert not (tmp_path / "uploads" / f"{sha}.tar.gz").exists()  # upload consumed


# ------------------------------------------------------------------- failure at each step


@pytest.mark.parametrize(
    ("pattern", "step", "final"),
    [
        ("dump", "backing_up", "failed"),
        ("build", "staging", "failed"),
        ("stop worker", "draining", "rolled_back"),
        ("migrate", "migrating", "rolled_back"),
        (UP_NEW, "starting", "rolled_back"),
        ("unhealthy", "checking", "rolled_back"),
    ],
)
def test_failure_at_each_step(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
    pattern: str,
    step: str,
    final: str,
) -> None:
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key)))
    if pattern == "unhealthy":
        host.unhealthy.add("1.1.0")
    else:
        host.fail[pattern] = RuntimeError("boom")
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == final
    r = req(conn, rid)
    assert r["state"] == final and r["error"].startswith(f"{step}: ")
    assert not drain(conn)
    assert read_env_value(layout.env_file, "MDHUB_IMAGE_TAG") == "1.0.0"
    assert str(layout.current.readlink()) == "releases/1.0.0"
    assert not any(c.startswith("restore") for c in host.calls)  # no migration: tag switch only
    if final == "failed":
        assert not any(c.startswith(("stop", "up")) for c in host.calls)
    else:
        assert host.calls[-1] == UP_OLD
        status = conn.execute("SELECT status FROM ops.release WHERE version = '1.1.0'").fetchone()
        assert status == {"status": "failed"}
    assert f"[{step}] error failed:" in log_lines(conn, rid)
    outcome = conn.execute(
        "SELECT outcome FROM admin.audit_log WHERE action = %s AND target = %s",
        (f"update.{final}", f"update:{rid}"),
    ).fetchone()
    assert outcome is not None


def test_rollback_restores_dump_when_migrations_ran(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key, extra=True)))
    host.unhealthy.add("1.1.0")
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == "rolled_back"
    r = req(conn, rid)
    i = host.calls.index("stop worker api admin hostmon @1.0.0")
    assert host.calls[i + 1] == f"restore {Path(r['backup_path']).name}"
    assert host.calls[-1] == UP_OLD
    assert EXTRA not in applied(conn)
    # the update's own trail survived the restore (journal reload), ids kept
    text = log_lines(conn, rid)
    assert "[verifying]" in text and "database restored" in text and "back on 1.0.0" in text
    assert r["state"] == "rolled_back" and r["error"].startswith("checking: not healthy")
    assert not layout.journal.exists()
    assert conn.execute("SELECT status FROM ops.release WHERE version = '1.1.0'").fetchone() == {
        "status": "failed"
    }
    nxt = conn.execute(
        "INSERT INTO ops.update_log (request_id, step, line) VALUES (%s, 'x', 'y') RETURNING id",
        (rid,),
    ).fetchone()
    assert nxt is not None and nxt["id"] > max(  # identity sequence moved past reloaded ids
        x["id"] for x in conn.execute("SELECT id FROM ops.update_log WHERE step <> 'x'")
    )


def test_rollback_that_fails_ends_failed(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key)))
    host.fail[UP_NEW] = RuntimeError("new image broken")
    host.fail[UP_OLD] = RuntimeError("old image gone")
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == "failed"
    error = req(conn, rid)["error"]
    assert "new image broken" in error and "rollback failed" in error and "old image gone" in error


def test_admin_rollback_to_previous_restores_pre_update_dump(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    up = make(db_url, host, layout, tmp_path, keys_dir)
    first = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key, extra=True)))
    assert up.process(first) == "done"
    install_dump = Path(req(conn, first)["backup_path"])
    host.calls.clear()
    rid = queue(conn, "1.0.0", kind="rollback")
    assert up.process(rid) == "done"
    r = req(conn, rid)
    assert r["restore_path"] == str(install_dump) and r["from_version"] == "1.1.0"
    assert f"restore {install_dump.name}" in host.calls
    assert "build @1.0.0" in host.calls and "run --rm --no-deps migrate @1.0.0" in host.calls
    assert host.calls.index("stop api admin hostmon @1.1.0") < host.calls.index(
        f"restore {install_dump.name}"
    )
    assert EXTRA not in applied(conn)
    assert str(layout.current.readlink()) == "releases/1.0.0"
    assert read_env_value(layout.env_file, "MDHUB_IMAGE_TAG") == "1.0.0"
    rows = {x["version"]: x["status"] for x in conn.execute("SELECT * FROM ops.release")}
    assert rows == {"1.0.0": "active", "1.1.0": "rolled_back"}
    assert req(conn, first)["state"] == "done"  # the earlier request survived the restore


# --------------------------------------------------------------------------- crash/resume


def test_crash_mid_update_resumes_from_recorded_state(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key)))
    host.fail["migrate"] = Crash()
    with pytest.raises(Crash):
        make(db_url, host, layout, tmp_path, keys_dir).process(rid)
    assert req(conn, rid)["state"] == "migrating" and drain(conn)
    host.fail.clear()
    host.calls.clear()
    fresh = make(db_url, host, layout, tmp_path, keys_dir)
    assert fresh.acquire_lock()
    fresh.recover()
    fresh.release_lock()
    assert req(conn, rid)["state"] == "done"
    assert host.calls[0] == "run --rm --no-deps migrate @1.1.0"  # earlier steps not repeated
    assert "updater restarted: resuming at migrating" in log_lines(conn, rid)


def test_crash_during_restore_is_redone_from_the_journal(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key, extra=True)))
    host.unhealthy.add("1.1.0")
    host.fail["restore"] = Crash()
    with pytest.raises(Crash):
        make(db_url, host, layout, tmp_path, keys_dir).process(rid)
    assert layout.journal.is_file()
    assert json.loads(layout.journal.read_text())["request_id"] == rid
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM ops.update_request").fetchone() == {"n": 0}
    host.fail.clear()
    fresh = make(db_url, host, layout, tmp_path, keys_dir)
    assert fresh.acquire_lock()
    fresh.recover()
    fresh.release_lock()
    assert sum(c.startswith("restore") for c in host.calls) == 2
    assert not layout.journal.exists()
    r = req(conn, rid)
    assert r["state"] == "rolled_back" and EXTRA not in applied(conn)
    assert host.calls[-1] == UP_OLD and "[verifying]" in log_lines(conn, rid)


# ------------------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unsigned", "unsigned"),
        ("unknown_key", "unknown key"),
        ("bad_signature", "does not verify"),
        ("tampered", "hash mismatch"),
        ("downgrade", "older than the running 1.0.0"),
        ("same", "already running"),
        ("checksum", "checksums differ"),
        ("min_previous", "needs at least 1.0.5"),
        ("gone", "upload it again"),
    ],
)
def test_refused_archives_change_nothing(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
    case: str,
    message: str,
) -> None:
    version = {"downgrade": "0.9.0", "same": "1.0.0"}.get(case, "1.1.0")
    archive = bundle(
        tmp_path,
        None if case == "unsigned" else SigningKey.generate() if case == "unknown_key" else key,
        version,
        tamper=case == "tampered",
        change_migration=case == "checksum",
        min_previous="1.0.5" if case == "min_previous" else "0.1.0",
    )
    if case == "bad_signature":
        default_signature_path(archive).write_bytes(key.sign(b"other sums"))
    sha = upload(tmp_path, archive)
    if case == "gone":
        (tmp_path / "uploads" / f"{sha}.tar.gz").unlink()
    rid = queue(conn, version, sha)
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == "failed"
    r = req(conn, rid)
    assert r["error"].startswith("verifying: ") and message in r["error"], r["error"]
    assert host.calls == [] and not drain(conn)
    assert str(layout.current.readlink()) == "releases/1.0.0"


def test_one_updater_holds_the_lock(
    db_url: str, host: FakeHost, layout: Layout, tmp_path: Path, keys_dir: Path
) -> None:
    a = make(db_url, host, layout, tmp_path, keys_dir)
    b = make(db_url, host, layout, tmp_path, keys_dir)
    assert a.acquire_lock() and not b.acquire_lock()
    a.release_lock()
    assert b.acquire_lock()
    b.release_lock()


def test_drain_waits_for_running_jobs_then_stops_the_worker(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
) -> None:
    conn.execute(
        "INSERT INTO ops.job_run (job, params, status, trigger) "
        "VALUES ('slow_job', '{}', 'running', 'schedule')"
    )
    conn.commit()
    rid = queue(conn, "1.1.0", upload(tmp_path, bundle(tmp_path, key)))
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == "done"
    text = log_lines(conn, rid)
    assert "waiting for running jobs: slow_job" in text
    assert "stopping the worker anyway" in text and host.drain_at_stop == [True]


# -------------------------------------------------------------------------------- github


def test_compose_preserves_site_override_without_forwarding_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "compose.local.yaml"
    override.write_text("services: {}\n")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("MDHUB_UPDATE_GITHUB_TOKEN", "test-only-placeholder")
    monkeypatch.setattr(subprocess, "run", run)
    host = ComposeHost("dbname=test", compose_override=override)
    compose = tmp_path / "deploy/docker-compose.yml"
    host.compose(compose, ["up", "-d", "admin"], tag="1.1.0", timeout=5)
    argv, options = calls[0]
    assert argv == [
        "docker",
        "compose",
        "-f",
        str(compose),
        "-f",
        str(override),
        "up",
        "-d",
        "admin",
    ]
    assert "MDHUB_UPDATE_GITHUB_TOKEN" not in options["env"]
    override.unlink()
    with pytest.raises(UpdateError, match=r"override.*not found"):
        host.compose(compose, ["up", "-d", "admin"], tag="1.1.0", timeout=5)
    assert len(calls) == 1


def test_updater_replacement_keeps_site_override(
    db_url: str, conn: Conn, host: FakeHost, layout: Layout, tmp_path: Path, keys_dir: Path
) -> None:
    override = tmp_path / "compose.local.yaml"
    override.write_text("services: {}\n")
    up = make(db_url, host, layout, tmp_path, keys_dir, compose_override=override)
    up._replace_self(queue(conn, "1.1.0"), "1.1.0")
    assert f"-f {override} up -d --no-deps updater" in host.calls[-1]


@pytest.mark.parametrize("token", ["github_pat_secret", None])
def test_github_listing_download_and_install(
    db_url: str,
    conn: Conn,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    key: SigningKey,
    token: str | None,
) -> None:
    archive = bundle(tmp_path, key, extra=True)
    files = {
        "1": archive.read_bytes(),
        "2": default_signature_path(archive).read_bytes(),
    }
    api = "https://api.github.com/repos/o/r"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url.startswith(f"{api}/releases?"):
            return httpx.Response(
                200,
                json=[
                    {
                        "tag_name": "v1.1.0",
                        "name": "1.1.0",
                        "body": "Adds the updater test table.",
                        "html_url": "https://github.com/o/r/releases/tag/v1.1.0",
                        "published_at": "2026-10-17T10:00:00Z",
                        "assets": [
                            {"name": archive.name, "url": f"{api}/releases/assets/1"},
                            {
                                "name": default_signature_path(archive).name,
                                "url": f"{api}/releases/assets/2",
                            },
                        ],
                    },
                    {"tag_name": "v0.9.0", "name": "old", "assets": []},
                    {"tag_name": "v2.0.0", "draft": True, "assets": []},
                    {"tag_name": "nightly", "assets": []},
                ],
            )
        if url.startswith(f"{api}/releases/assets/"):
            n = url.rsplit("/", 1)[1]
            return httpx.Response(302, headers={"Location": f"https://objects.example/{n}"})
        if url.startswith("https://objects.example/"):
            return httpx.Response(200, content=files[url.rsplit("/", 1)[1]])
        return httpx.Response(404)

    gh = GitHub("o/r", token, transport=httpx.MockTransport(handler))
    up = make(db_url, host, layout, tmp_path, keys_dir, github=gh)
    up.refresh_releases()
    rows = {r["version"]: r for r in conn.execute("SELECT * FROM ops.release_available").fetchall()}
    assert set(rows) == {"1.1.0", "0.9.0"}
    new = rows["1.1.0"]
    assert new["verified"] is True and new["key_id"] == key.key_id_hex
    assert EXTRA in new["migrations"] and new["notes"] == "Adds the updater test table."
    assert rows["0.9.0"]["verified"] is None  # older than the running version: not fetched
    for r in seen:
        on_api = r.url.host == "api.github.com"
        assert ("authorization" in r.headers) == (on_api and token is not None), r.url
    if token is not None:
        assert seen[0].headers["authorization"] == f"Bearer {token}"
    n_requests = len(seen)
    up.refresh_releases()  # verified and present: not downloaded again
    assert len(seen) == n_requests + 1

    rid = queue(conn, "1.1.0")
    assert up.process(rid) == "done"
    assert req(conn, rid)["key_id"] == key.key_id_hex
    with pytest.raises(Exception, match="outside this repository"):
        gh.download("https://evil.example/x", tmp_path / "x")


def test_github_not_configured_fails_cleanly(
    db_url: str, conn: Conn, host: FakeHost, layout: Layout, tmp_path: Path, keys_dir: Path
) -> None:
    rid = queue(conn, "1.1.0")
    assert make(db_url, host, layout, tmp_path, keys_dir).process(rid) == "failed"
    assert "MDHUB_UPDATE_GITHUB_TOKEN" in req(conn, rid)["error"]


# ------------------------------------------------------------------------- worker drain


@pytest.fixture
def registry() -> JobRegistry:
    reg = JobRegistry()
    reg.register("drain_job", "test", lambda ctx: {"ran": ctx.job})
    return reg


def _set_drain(conn: Conn, on: bool) -> None:
    conn.execute("UPDATE ops.worker_control SET drain = %s", (on,))
    conn.commit()


def test_job_requests_not_claimed_while_draining(
    db: Database, conn: Conn, settings: Settings, registry: JobRegistry
) -> None:
    conn.execute("INSERT INTO ops.job_request (job) VALUES ('drain_job')")
    conn.commit()
    poller = JobRequestPoller(
        settings, db, calendar=weekday_calendar, worker_instance=uuid.uuid4(), registry=registry
    )
    try:
        _set_drain(conn, True)
        assert poller.poll_once() == []
        conn.rollback()
        assert conn.execute("SELECT state FROM ops.job_request").fetchone() == {"state": "queued"}
        _set_drain(conn, False)
        futures = poller.poll_once()
        assert len(futures) == 1 and futures[0].result(timeout=10) == "done"
    finally:
        poller.stop()


def test_scheduled_fires_skipped_while_draining(
    db: Database, conn: Conn, settings: Settings
) -> None:
    calls: list[str] = []
    reg = JobRegistry()

    def drain_job(ctx: JobContext) -> None:
        calls.append(ctx.job)

    reg.register("drain_job", "test", drain_job)

    def runner(wait: int) -> _RuleRunner:
        return _RuleRunner(
            "drain_job",
            Rule(cron="* * * * *", days="all", lock_wait_s=wait),
            settings=settings,
            db=db,
            registry=reg,
            calendar=weekday_calendar,
            lock=threading.Lock(),
        )

    _set_drain(conn, True)
    runner(60)()
    runner(0)()  # a polling rule: not recorded, the next fire supersedes it
    assert calls == []
    rows = conn.execute("SELECT status, error FROM ops.job_run WHERE job = 'drain_job'").fetchall()
    assert rows == [{"status": "skipped", "error": DRAINING}]
    _set_drain(conn, False)
    runner(60)()
    assert calls == ["drain_job"]


def test_reload_appends_missing_audit_rows_with_their_ids(conn: Conn) -> None:
    """After a restore the audit log gets back the rows the dump lacks (ids, IPs, details
    kept) and its identity sequence moves past them; nothing is updated or deleted."""
    store.audit(conn, "update.test", login="updater", ip="192.0.2.1", details={"a": 1})
    conn.commit()
    rows = [r["r"] for r in conn.execute("SELECT to_jsonb(t) AS r FROM admin.audit_log t")]
    top = max(int(r["id"]) for r in rows)
    lost = {**rows[-1], "id": top + 5, "action": "update.lost", "ip": "198.51.100.9"}
    _reload(conn, {"admin.audit_log": [*rows, lost]})
    conn.commit()
    got = conn.execute(
        "SELECT id, ip::text AS ip, details FROM admin.audit_log WHERE action = 'update.lost'"
    ).fetchone()
    assert got == {"id": top + 5, "ip": "198.51.100.9/32", "details": {"a": 1}}
    store.audit(conn, "update.next", login="updater")
    nxt = conn.execute("SELECT id FROM admin.audit_log WHERE action = 'update.next'").fetchone()
    assert nxt is not None and nxt["id"] > top + 5


# ----------------------------------------------------------------------- container status


def _docker_time(ago: timedelta) -> str:
    return f"{datetime.now(UTC) - ago:%Y-%m-%dT%H:%M:%S}.123456789Z"


class FakeDocker:
    """``docker ps / inspect / stats`` of a compose project with four containers."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False
        self.inspect: list[dict[str, Any]] = [
            self._container("worker", "running", None, 0, timedelta(days=3), 0),
            self._container("api", "running", "healthy", 2, timedelta(minutes=10), 512 << 20),
            self._container("admin", "exited", None, 0, None, 0),
            self._container("postgres", "running", "starting", 0, timedelta(seconds=30), 0),
        ]
        self.stats = {
            "mdhub-worker-1": ("2.50%", "300MiB / 1.94GiB"),
            "mdhub-api-1": ("1.00%", "120.5MiB / 512MiB"),
            "mdhub-postgres-1": ("--", "-- / --"),
        }

    @staticmethod
    def _container(
        service: str,
        state: str,
        health: str | None,
        restarts: int,
        up: timedelta | None,
        memory: int,
    ) -> dict[str, Any]:
        return {
            "Id": f"id-{service}",
            "Name": f"/mdhub-{service}-1",
            "RestartCount": restarts,
            "State": {
                "Status": state,
                "StartedAt": _docker_time(up) if up else "0001-01-01T00:00:00Z",
                **({"Health": {"Status": health}} if health else {}),
            },
            "Config": {
                "Image": "mdhub:1.2.0" if service != "postgres" else "postgres:16",
                "Labels": {"com.docker.compose.service": service},
            },
            "HostConfig": {"Memory": memory},
        }

    def __call__(self, args: Any) -> str:
        args = list(args)
        self.calls.append(args)
        if self.fail:
            raise UpdateError("docker ps exited with 1: Cannot connect to the Docker daemon")
        if args[0] == "ps":
            return "".join(f"{c['Id']}\n" for c in self.inspect)
        if args[0] == "inspect":
            return json.dumps([c for c in self.inspect if c["Id"] in args[1:]])
        assert args[:4] == ["stats", "--no-stream", "--format", "{{json .}}"]
        return "".join(
            json.dumps({"Name": n, "CPUPerc": cpu, "MemUsage": mem}) + "\n"
            for n, (cpu, mem) in self.stats.items()
            if n in args[4:]
        )


def test_parse_docker_sizes_and_percentages() -> None:
    assert parse_size("0B") == 0
    assert parse_size("120.5MiB ") == round(120.5 * 2**20)
    assert parse_size("1.2GB") == 1_200_000_000
    assert parse_size("--") is None and parse_size("3 parsecs") is None and not parse_size("1.2.3B")
    assert parse_pct("2.50%") == 2.5 and parse_pct("--") is None


def test_container_reporter_upserts_and_drops_gone_containers(db_url: str, conn: Conn) -> None:
    conn.execute(
        "INSERT INTO ops.container_status (name, state, updated_at) "
        "VALUES ('mdhub-old-1', 'running', now() - interval '1 day')"
    )
    conn.commit()
    docker = FakeDocker()
    reporter = ContainerReporter(Database(db_url), docker=docker)
    reporter.write(reporter.sample())
    ps = docker.calls[0]
    assert "label=com.docker.compose.project=mdhub" in ps
    assert "label=com.docker.compose.oneoff=False" in ps  # no `compose run` one-offs
    assert docker.calls[2][4:] == ["mdhub-worker-1", "mdhub-api-1", "mdhub-postgres-1"]
    rows = {
        r["name"]: r
        for r in conn.execute(
            "SELECT *, updated_at > now() - interval '1 minute' AS fresh FROM ops.container_status"
        )
    }
    assert set(rows) == {"mdhub-worker-1", "mdhub-api-1", "mdhub-admin-1", "mdhub-postgres-1"}
    api = rows["mdhub-api-1"]
    assert (api["service"], api["image"], api["state"], api["health"]) == (
        "api",
        "mdhub:1.2.0",
        "running",
        "healthy",
    )
    assert api["restart_count"] == 2 and api["cpu_pct"] == 1.0
    assert api["mem_bytes"] == round(120.5 * 2**20) and api["mem_limit_bytes"] == 512 << 20
    assert datetime.now(UTC) - api["started_at"] < timedelta(minutes=11)
    worker, admin = rows["mdhub-worker-1"], rows["mdhub-admin-1"]
    assert worker["mem_limit_bytes"] is None and worker["health"] is None
    assert admin["started_at"] is None and admin["cpu_pct"] is None
    assert rows["mdhub-postgres-1"]["mem_bytes"] is None
    assert all(r["fresh"] for r in rows.values())

    # a container that stopped existing is dropped; the rest are updated in place
    docker.inspect = docker.inspect[:1]
    reporter.write(reporter.sample())
    conn.rollback()
    names = [r["name"] for r in conn.execute("SELECT name FROM ops.container_status")]
    assert names == ["mdhub-worker-1"]


def test_container_reporter_failure_is_logged_not_raised(
    db_url: str, conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    docker = FakeDocker()
    reporter = ContainerReporter(Database(db_url), docker=docker)
    reporter.write(reporter.sample())
    docker.fail = True
    stop = threading.Event()
    stop.set()
    with caplog.at_level(logging.WARNING, logger="mdhub.updater"):
        reporter.serve(stop)  # one round, then stop
    assert "container status failed: UpdateError: docker ps exited with 1" in caplog.text
    conn.rollback()
    row = conn.execute("SELECT count(*) AS n FROM ops.container_status").fetchone()
    assert row == {"n": 4}  # the last good report stays (and ages into "not reporting")


def test_container_reporter_paused_during_a_restore(
    db_url: str,
    host: FakeHost,
    layout: Layout,
    tmp_path: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An insert while pg_restore re-creates the table would break its primary key."""
    u = make(db_url, host, layout, tmp_path, keys_dir)
    reporter = u.containers = ContainerReporter(Database(db_url), docker=FakeDocker())
    held: list[bool] = []

    def restore(dump: Path) -> None:
        held.append(reporter.pause.locked())
        FakeHost.restore(host, dump)

    monkeypatch.setattr(host, "restore", restore)
    layout.journal.write_text("{}", encoding="utf-8")
    u._restore_journal({"dump": str(tmp_path / "x.dump"), "tables": {}})
    assert held == [True] and not reporter.pause.locked()
