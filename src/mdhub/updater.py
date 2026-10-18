"""Self-update agent ``mdhub updater`` for the docker compose deployment (ADMIN_DESIGN §6).

The console only inserts ``ops.update_request`` rows. This process - no listening port, the only
one with the docker socket and ``MDHUB_UPDATE_GITHUB_TOKEN`` - polls them, holds the advisory
lock :data:`LOCK_NAME` (one updater, so one update at a time) and drives each request through
the §6.3 state machine. Every state change and step writes ``ops.update_log`` and
``admin.audit_log`` (login ``updater``):

``verifying``
    install: the archive (GitHub asset, or the upload copied out of the shared volume) passes
    :func:`mdhub.release.verify_archive` against the pinned keys; it is newer than the running
    version, which is at least its ``min_previous_version``. Rollback: the release directory
    exists. Both: migration checksums equal the applied ones.
``backing_up``
    ``pg_dump`` to ``pre-update-*.dump`` in the backup directory (outside the ``mdhub-*.dump``
    rotation of the nightly backup).
``staging``
    unpack into ``releases/X.Y.Z`` and ``docker compose build`` it as ``mdhub:X.Y.Z``; the
    running services are untouched.
``draining``
    ``ops.worker_control.drain``; wait up to 10 minutes for running jobs, then stop the worker.
``migrating``
    ``docker compose run migrate`` with the new tag. A rollback across migrations first restores
    the dump taken before the running version was installed.
``starting``
    ``MDHUB_IMAGE_TAG`` in ``.env`` and the ``current`` symlink, then ``up -d`` worker, api, admin.
``checking``
    within 3 minutes: ``/health/ready`` reports the new version, a worker heartbeat reports it
    and a smoke request with a short-lived read token succeeds.
``done``
    release bookkeeping; then the updater replaces itself (a one-off container of the new image
    recreates the ``updater`` service).

A failure before ``draining`` ends as ``failed``: nothing ran on the new version. A later one
rolls back (``rolling_back`` -> ``rolled_back``): the previous tag and symlink come back, after
restoring this request's pre-update dump when the applied migrations changed. A restore drops
and re-creates the database, so :data:`KEPT_TABLES` are written to a journal file first and
loaded again afterwards; a journal found at start-up means a restore was interrupted and it is
redone. An updater that stops mid-way resumes the open request from its recorded state (every
step can run again).

Being the only process with the docker socket, the updater also reports the containers to
``ops.container_status`` every 30 s from its own thread (:class:`ContainerReporter`, the Machine
page of ADMIN_DESIGN §10), idle or mid-update; it pauses only while a restore runs.

Host layout under ``MDHUB_UPDATE_ROOT``, mounted at the same path in the updater container so
the paths compose resolves are host paths (see :class:`Layout`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from mdhub import __version__
from mdhub.admin import store
from mdhub.api.auth import create_token, revoke_token
from mdhub.config import Settings
from mdhub.core.heartbeat import Heartbeat
from mdhub.core.redact import sanitize_text
from mdhub.db.migrate import applied_migrations, discover_migrations, restrict_admin
from mdhub.db.pool import Database, DictConnection
from mdhub.loaders.maintenance import (
    PG_DUMP_TIMEOUT_S,
    _pg_tool,
    _split_password,
    backup_command,
    backup_dir,
    run_command,
)
from mdhub.release import (
    MAX_UPLOAD_BYTES,
    MIGRATIONS_PREFIX,
    ReleaseError,
    default_keys_dir,
    default_signature_path,
    load_pinned_keys,
    verify_archive,
    version_key,
)

__all__ = [
    "FINAL_STATES",
    "KEPT_TABLES",
    "LOCK_NAME",
    "STEPS",
    "ComposeHost",
    "ContainerReporter",
    "GitHub",
    "Host",
    "Layout",
    "UpdateError",
    "Updater",
    "archive_name",
    "docker",
    "parse_pct",
    "parse_size",
    "read_env_value",
    "run",
    "write_env_value",
]

log = logging.getLogger(__name__)

LOCK_NAME: Final = "mdhub.updater"
POLL_INTERVAL_S: Final = 5.0
GITHUB_INTERVAL_S: Final = 600.0
DRAIN_TIMEOUT_S: Final = 600.0
CHECK_TIMEOUT_S: Final = 180.0
BUILD_TIMEOUT_S: Final = 3600.0
COMPOSE_TIMEOUT_S: Final = 600.0
CONTAINER_INTERVAL_S: Final = 30.0
DOCKER_TIMEOUT_S: Final = 20.0
COMPOSE_PROJECT: Final = "mdhub"
"""``name:`` of deploy/docker-compose.yml."""
PREFETCH: Final = 3
"""Newer GitHub releases downloaded and verified ahead of an install (newest first)."""

STEPS: Final = (
    "verifying",
    "backing_up",
    "staging",
    "draining",
    "migrating",
    "starting",
    "checking",
)
FINAL_STATES: Final = ("done", "failed", "rolled_back", "cancelled")
SERVICES: Final = ("worker", "api", "admin")
KEPT_TABLES: Final = ("ops.update_request", "ops.update_log", "ops.release", "admin.audit_log")
"""Saved before a restore and loaded again after it: the update's own trail survives."""

_ERR_CHARS: Final = 2000
_NOTES_CHARS: Final = 20_000
_REPO_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_API: Final = "https://api.github.com"


class UpdateError(Exception):
    """A step failed; the message is shown on the Updates page (no secrets)."""


def archive_name(version: str) -> str:
    return f"market-data-hub-{version}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class Layout:
    """``releases/X.Y.Z/`` unpacked bundles, ``current`` -> the running one, ``archives/``
    verified bundles, ``shared/.env`` (linked into every release's ``deploy/``),
    ``shared/updater.env`` (the GitHub token: updater only) and the restore journal."""

    root: Path

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def archives(self) -> Path:
        return self.root / "archives"

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def env_file(self) -> Path:
        return self.shared / ".env"

    @property
    def journal(self) -> Path:
        return self.shared / "updater-journal.json"

    def release(self, version: str) -> Path:
        return self.releases / version

    @staticmethod
    def compose_file(tree: Path) -> Path:
        return tree / "deploy" / "docker-compose.yml"


def read_env_value(path: Path, name: str) -> str | None:
    """Value of ``NAME=value`` in a compose ``.env`` file (quotes stripped), else ``None``."""
    pattern = re.compile(rf"^\s*{re.escape(name)}=(.*)$")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip().strip("'\"") or None
    return None


def write_env_value(path: Path, name: str, value: str) -> None:
    """Set ``NAME=value`` in a ``.env`` file in place (atomic rename, same mode and owner)."""
    real = path.resolve()
    pattern = re.compile(rf"^\s*{re.escape(name)}=")
    out: list[str] = []
    done = False
    for line in real.read_text(encoding="utf-8").splitlines():
        if pattern.match(line):
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}={value}")
    st = real.stat()
    tmp = real.with_name(real.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, st.st_mode & 0o777)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, real)


# ----------------------------------------------------------------------------------- host


class Host(Protocol):
    """What the state machine does outside the database (faked in tests)."""

    def compose(self, compose_file: Path, args: Sequence[str], *, tag: str, timeout: float) -> None:
        """``docker compose -f compose_file args`` with ``MDHUB_IMAGE_TAG=tag``."""

    def dump(self, target: Path) -> None:
        """``pg_dump --format=custom`` of the database into ``target``."""

    def restore(self, dump: Path) -> None:
        """Drop the database and re-create it from ``dump`` (no client may stay connected)."""

    def http_json(self, url: str, token: str | None = None) -> tuple[int, Any]:
        """GET ``url``: ``(status, parsed JSON or None)``; status 0 when unreachable."""


class ComposeHost:
    """The real host: docker compose and the PostgreSQL client tools in the updater image."""

    def __init__(self, conninfo: str, secrets: Sequence[str] = ()) -> None:
        self.conninfo = conninfo
        self.secrets = list(secrets)

    def compose(self, compose_file: Path, args: Sequence[str], *, tag: str, timeout: float) -> None:
        # A minimal environment: compose reads everything else from the project's .env, and
        # the GitHub token never reaches it.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST") if k in os.environ}
        env.update(MDHUB_IMAGE_TAG=tag, MDHUB_VERSION=tag)
        argv = ["docker", "compose", "-f", str(compose_file), *args]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, env=env, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError:
            raise UpdateError("docker CLI not found in the updater image") from None
        except subprocess.TimeoutExpired:
            raise UpdateError(f"docker compose {args[0]} timed out after {timeout:.0f} s") from None
        if proc.returncode != 0:
            tail = sanitize_text((proc.stderr or proc.stdout or "").strip()[-1500:], self.secrets)
            raise UpdateError(f"docker compose {args[0]} exited with {proc.returncode}: {tail}")

    def dump(self, target: Path) -> None:
        argv, env = backup_command(self.conninfo, target)
        run_command(argv, env, timeout=PG_DUMP_TIMEOUT_S, secrets=self.secrets)

    def restore(self, dump: Path) -> None:
        maintenance, password = _split_password(make_conninfo(self.conninfo, dbname="postgres"))
        env = {"PGPASSWORD": password} if password else {}
        restore = _pg_tool("pg_restore")
        # Read the whole table of contents before dropping anything.
        run_command([restore, "--list", str(dump)], env, timeout=600, secrets=self.secrets)
        dbname = str(conninfo_to_dict(self.conninfo).get("dbname") or "")
        if not dbname:
            raise UpdateError("MDHUB_DATABASE_URL names no database")
        with psycopg.connect(self.conninfo, dbname="postgres", autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
            )
        argv = [
            restore,
            "--create",
            "--exit-on-error",
            "--no-password",
            f"--dbname={maintenance}",
            str(dump),
        ]
        run_command(argv, env, timeout=PG_DUMP_TIMEOUT_S, secrets=self.secrets)

    def http_json(self, url: str, token: str | None = None) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = httpx.get(url, headers=headers, timeout=10, trust_env=False)
        except httpx.HTTPError:
            return 0, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None


# ----------------------------------------------------------------------------- containers


def docker(args: Sequence[str]) -> str:
    """Stdout of ``docker args`` (same minimal environment as :meth:`ComposeHost.compose`)."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST") if k in os.environ}
    argv = ["docker", *args]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, env=env, capture_output=True, text=True, timeout=DOCKER_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        raise UpdateError("docker CLI not found in the updater image") from None
    except subprocess.TimeoutExpired:
        raise UpdateError(f"docker {args[0]} timed out after {DOCKER_TIMEOUT_S:.0f} s") from None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise UpdateError(f"docker {args[0]} exited with {proc.returncode}: {tail}")
    return proc.stdout


_SIZE_RE: Final = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)")
_UNITS: Final = {
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
}


def parse_size(text: str) -> int | None:
    """Bytes of a ``docker stats`` size (``12.5MiB``, ``1.2GB``, ``0B``); ``None`` for ``--``."""
    m = _SIZE_RE.fullmatch(text.strip())
    factor = _UNITS.get((m.group(2) or "b").lower()) if m else None
    return round(float(m.group(1)) * factor) if m and factor else None


def parse_pct(text: str) -> float | None:
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return None


class ContainerReporter:
    """Upserts one ``ops.container_status`` row per container of the compose project every
    ``interval_s`` (ADMIN_DESIGN §10; the console reads the rows) and deletes rows of containers
    that are gone. One-off ``compose run`` containers are left out. Runs in its own thread, so
    it reports while an update runs or the loop is idle; a failed round is logged and retried.

    :param docker: runs ``docker args`` and returns stdout (a fake in tests).
    """

    def __init__(
        self,
        db: Database,
        *,
        project: str = COMPOSE_PROJECT,
        docker: Callable[[Sequence[str]], str] = docker,
        interval_s: float = CONTAINER_INTERVAL_S,
    ) -> None:
        self.db = db
        self.project = project
        self.docker = docker
        self.interval_s = interval_s
        self.pause = threading.Lock()
        """Held during a database restore: an insert then could break ``pg_restore``."""

    def sample(self) -> list[dict[str, Any]]:
        ids = self.docker(
            [
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
                "--filter",
                "label=com.docker.compose.oneoff=False",
            ]
        ).split()
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        for c in json.loads(self.docker(["inspect", *ids])):
            state = c.get("State") or {}
            started = str(state.get("StartedAt") or "")
            rows.append(
                {
                    "name": str(c["Name"]).lstrip("/"),
                    "service": ((c.get("Config") or {}).get("Labels") or {}).get(
                        "com.docker.compose.service"
                    ),
                    "image": (c.get("Config") or {}).get("Image"),
                    "state": str(state.get("Status") or "unknown"),
                    "health": (state.get("Health") or {}).get("Status"),
                    "restart_count": int(c.get("RestartCount") or 0),
                    # docker reports 0001-01-01 for a container that never started
                    "started_at": started if started and not started.startswith("0001-") else None,
                    "cpu_pct": None,
                    "mem_bytes": None,
                    "mem_limit_bytes": int((c.get("HostConfig") or {}).get("Memory") or 0) or None,
                }
            )
        running = [r["name"] for r in rows if r["state"] == "running"]
        if running:
            by_name = {r["name"]: r for r in rows}
            out = self.docker(["stats", "--no-stream", "--format", "{{json .}}", *running])
            for line in out.splitlines():
                s = json.loads(line) if line.strip() else {}
                row = by_name.get(str(s.get("Name", "")).lstrip("/"))
                if row is not None:
                    row["cpu_pct"] = parse_pct(str(s.get("CPUPerc", "")))
                    row["mem_bytes"] = parse_size(str(s.get("MemUsage", "")).split("/")[0])
        return rows

    def write(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self.pause, self.db.connection() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO ops.container_status (name, service, image, state, health,
                        restart_count, started_at, cpu_pct, mem_bytes, mem_limit_bytes,
                        updated_at)
                    VALUES (%(name)s, %(service)s, %(image)s, %(state)s, %(health)s,
                        %(restart_count)s, %(started_at)s::timestamptz, %(cpu_pct)s,
                        %(mem_bytes)s, %(mem_limit_bytes)s, now())
                    ON CONFLICT (name) DO UPDATE SET service = EXCLUDED.service,
                        image = EXCLUDED.image, state = EXCLUDED.state, health = EXCLUDED.health,
                        restart_count = EXCLUDED.restart_count, started_at = EXCLUDED.started_at,
                        cpu_pct = EXCLUDED.cpu_pct, mem_bytes = EXCLUDED.mem_bytes,
                        mem_limit_bytes = EXCLUDED.mem_limit_bytes, updated_at = now()
                    """,
                    row,
                )
            conn.execute(
                "DELETE FROM ops.container_status WHERE name <> ALL(%s)",
                ([r["name"] for r in rows],),
            )

    def serve(self, stop: threading.Event) -> None:
        """Report now and then every ``interval_s`` until ``stop`` is set."""
        while True:
            try:
                self.write(self.sample())
            except Exception as exc:
                log.warning("container status failed: %s: %s", type(exc).__name__, exc)
            if stop.wait(self.interval_s):
                return


# --------------------------------------------------------------------------------- github


class GitHub:
    """Release listing and asset download with a fine-grained token (``contents:read``),
    through ``MDHUB_HTTP_PROXY`` when set. The token goes only to ``api.github.com``: httpx
    drops ``Authorization`` on the redirect to the asset storage host."""

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not _REPO_RE.match(repo):
            raise UpdateError(f"bad MDHUB_UPDATE_GITHUB_REPO {repo!r} (want owner/name)")
        self.repo = repo
        self.client = httpx.Client(
            base_url=_GITHUB_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"mdhub-updater/{__version__}",
            },
            proxy=proxy,
            transport=transport,
            timeout=60,
            follow_redirects=True,
            trust_env=False,
        )

    def _get_json(self, path: str, **params: Any) -> Any:
        try:
            r = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise UpdateError(f"GitHub unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise UpdateError(f"GitHub answered HTTP {r.status_code} for {path}")
        return r.json()

    def releases(self) -> list[dict[str, Any]]:
        data = self._get_json(f"/repos/{self.repo}/releases", per_page=30)
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def release(self, version: str) -> dict[str, Any]:
        data = self._get_json(f"/repos/{self.repo}/releases/tags/v{version}")
        if not isinstance(data, dict):
            raise UpdateError(f"GitHub release v{version}: unexpected answer")
        return data

    def download(self, asset_url: str, target: Path, *, limit: int = MAX_UPLOAD_BYTES) -> None:
        """Stream a release asset (API URL from the release JSON) to ``target``."""
        prefix = f"{_GITHUB_API}/repos/{self.repo}/releases/assets/"
        if not asset_url.startswith(prefix):
            raise UpdateError("asset URL outside this repository's releases; refused")
        part = target.with_name(target.name + ".part")
        size = 0
        try:
            with (
                self.client.stream(
                    "GET", asset_url, headers={"Accept": "application/octet-stream"}
                ) as r,
                part.open("wb") as f,
            ):
                if r.status_code != 200:
                    raise UpdateError(f"download of {target.name}: HTTP {r.status_code}")
                for chunk in r.iter_bytes(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise UpdateError(f"{target.name} is larger than {limit} bytes")
                    f.write(chunk)
            part.replace(target)
        except httpx.HTTPError as exc:
            raise UpdateError(f"download of {target.name} failed: {type(exc).__name__}") from None
        finally:
            part.unlink(missing_ok=True)


# -------------------------------------------------------------------------------- updater


class Updater:
    """The §6.3 state machine over ``ops.update_request``.

    :param db: a :class:`Database` without a pool (each step opens its own connection, so a
        restore only has to close the lock connection).
    :param host: compose / pg_dump / pg_restore / HTTP (a fake in tests).
    """

    def __init__(
        self,
        db: Database,
        host: Host,
        layout: Layout,
        *,
        keys_dir: Path,
        upload_dir: Path,
        backup_dir: Path,
        api_url: str,
        github: GitHub | None = None,
        version: str = __version__,
        secrets: Sequence[str] = (),
        drain_timeout_s: float = DRAIN_TIMEOUT_S,
        check_timeout_s: float = CHECK_TIMEOUT_S,
        poll_s: float = POLL_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.host = host
        self.layout = layout
        self.keys_dir = keys_dir
        self.upload_dir = upload_dir
        self.backup_dir = backup_dir
        self.api_url = api_url.rstrip("/")
        self.github = github
        self.version = version
        self.secrets = list(secrets)
        self.drain_timeout_s = drain_timeout_s
        self.check_timeout_s = check_timeout_s
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock
        self.heartbeat: Heartbeat | None = None
        self.containers: ContainerReporter | None = None
        self._lock_conn: DictConnection | None = None

    # ------------------------------------------------------------------ lock and loop

    def acquire_lock(self) -> bool:
        """Session advisory lock :data:`LOCK_NAME` on a dedicated connection."""
        conn = self.db.connect(autocommit=True)
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", (LOCK_NAME,)
        ).fetchone()
        if not (row and row["ok"]):
            conn.close()
            return False
        self._lock_conn = conn
        return True

    def release_lock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            conn.close()

    def recover(self) -> None:
        """Finish an interrupted restore (journal), then resume the open request."""
        if self.layout.journal.is_file():
            journal = json.loads(self.layout.journal.read_text(encoding="utf-8"))
            log.warning("restore journal found: redoing the restore of %s", journal["dump"])
            self._restore_journal(journal)
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id FROM ops.update_request WHERE state <> 'queued' "
                "AND state <> ALL(%s) ORDER BY id LIMIT 1",
                (list(FINAL_STATES),),
            ).fetchone()
        if row is not None:
            self.process(int(row["id"]))

    def claim(self) -> int | None:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT id FROM ops.update_request WHERE state = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
        return int(row["id"]) if row else None

    def serve(self, stop: threading.Event) -> None:
        """Poll for requests (and GitHub releases) until ``stop`` is set."""
        while not self.acquire_lock():
            log.warning("another updater holds the lock; waiting")
            if stop.wait(30):
                return
        try:
            self.recover()
            next_refresh = 0.0
            while not stop.is_set():
                if self.github is not None and self.clock() >= next_refresh:
                    try:
                        self.refresh_releases()
                    except Exception as exc:
                        log.warning("GitHub release check failed: %s", self._error(exc))
                    next_refresh = self.clock() + GITHUB_INTERVAL_S
                request_id = self.claim()
                if request_id is not None:
                    self.process(request_id)
                elif stop.wait(self.poll_s):
                    break
        finally:
            self.release_lock()

    # ----------------------------------------------------------------- bookkeeping

    def _error(self, exc: BaseException) -> str:
        text = (
            str(exc)
            if isinstance(exc, UpdateError | ReleaseError)
            else (f"{type(exc).__name__}: {exc}")
        )
        return sanitize_text(text, self.secrets)[:_ERR_CHARS]

    def _log(
        self,
        request_id: int,
        step: str,
        line: str,
        level: Literal["info", "warning", "error"] = "info",
    ) -> None:
        log.log(
            {"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO),
            "update %s [%s] %s",
            request_id,
            step,
            line,
        )
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO ops.update_log (request_id, step, level, line) "
                "VALUES (%s, %s, %s, %s)",
                (request_id, step, level, sanitize_text(line, self.secrets)[:4000]),
            )

    def _audit(
        self,
        conn: DictConnection,
        request: Mapping[str, Any],
        action: str,
        outcome: Literal["ok", "denied", "failed"] = "ok",
        **details: Any,
    ) -> None:
        store.audit(
            conn,
            f"update.{action}",
            login="updater",
            target=f"update:{request['id']}",
            details={"kind": request["kind"], "version": request["version"], **details},
            outcome=outcome,
        )

    def _load(self, request_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM ops.update_request WHERE id = %s", (request_id,)
            ).fetchone()
        if row is None:
            raise UpdateError(f"update request {request_id} not found")
        return dict(row)

    def _update(self, request_id: int, **cols: Any) -> None:
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in cols
        )
        with self.db.connection() as conn:
            conn.execute(
                sql.SQL("UPDATE ops.update_request SET {} WHERE id = {}").format(
                    assignments, sql.Placeholder("_id")
                ),
                {**cols, "_id": request_id},
            )

    def _set(self, request_id: int, state: str, **cols: Any) -> dict[str, Any]:
        final = state in FINAL_STATES
        with self.db.connection() as conn:
            row = conn.execute(
                """
                UPDATE ops.update_request SET state = %s, state_at = now(),
                    started_at = coalesce(started_at, now()),
                    finished_at = CASE WHEN %s THEN now() END
                WHERE id = %s RETURNING *
                """,
                (state, final, request_id),
            ).fetchone()
            assert row is not None
            failed = state in ("failed", "rolling_back")
            self._audit(
                conn,
                row,
                state,
                outcome="failed" if failed else "ok",
                **({"error": cols["error"]} if cols.get("error") else {}),
            )
        if cols:
            self._update(request_id, **cols)
        return self._load(request_id)

    # ---------------------------------------------------------------- state machine

    def process(self, request_id: int) -> str:
        """Run (or resume) one request to a final state; returns it."""
        request = self._load(request_id)
        state = str(request["state"])
        if state == "rolling_back":
            self._log(request_id, state, "resuming the rollback")
            return self._rollback(request_id, str(request["error"] or "interrupted"))
        if state == "queued":
            state = STEPS[0]
        elif state in STEPS:
            self._log(request_id, state, f"updater restarted: resuming at {state}")
        else:
            return state
        step = state
        try:
            for step in STEPS[STEPS.index(state) :]:
                request = self._set(request_id, step)
                getattr(self, f"_{step}")(request)
        except Exception as exc:
            error = self._error(exc)
            self._log(request_id, step, f"failed: {error}", "error")
            if STEPS.index(step) < STEPS.index("draining"):
                self._finish(request_id, "failed", f"{step}: {error}")
                return "failed"
            return self._rollback(request_id, f"{step}: {error}")
        self._done(self._load(request_id))
        return "done"

    def _active_version(self) -> str:
        with self.db.connection() as conn:
            row = conn.execute("SELECT version FROM ops.release WHERE status = 'active'").fetchone()
        return str(row["version"]) if row else self.version

    def _verifying(self, request: dict[str, Any]) -> None:
        rid, version = int(request["id"]), str(request["version"])
        version_key(version)
        from_version = self._active_version()
        if version == from_version:
            raise UpdateError(f"{version} is already running")
        if not self.layout.env_file.is_file():
            raise UpdateError(f"{self.layout.env_file} not found (README_DEPLOY §13)")
        try:
            from_link = os.readlink(self.layout.current)
        except OSError:
            raise UpdateError(
                f"{self.layout.current} is not a symlink (README_DEPLOY §13)"
            ) from None
        from_tag = read_env_value(self.layout.env_file, "MDHUB_IMAGE_TAG") or from_version
        sha = key_id = commit = None
        if request["kind"] == "install":
            archive = self._archive(request)
            verified = verify_archive(archive, load_pinned_keys(self.keys_dir))
            m = verified.manifest
            if m.version != version:
                raise UpdateError(f"the archive holds version {m.version}, not {version}")
            if version_key(version) < version_key(from_version):
                raise UpdateError(
                    f"{version} is older than the running {from_version}: only a rollback "
                    "installs an older version"
                )
            if version_key(from_version) < version_key(m.min_previous_version):
                raise UpdateError(
                    f"{version} needs at least {m.min_previous_version} installed first "
                    f"(running {from_version})"
                )
            migrations = dict(m.migrations)
            sha, commit = _sha256(archive), m.commit
            key_id = verified.key.key_id_hex if verified.key else None
            self._log(rid, "verifying", f"signature by key {key_id} and {verified.files} hashes ok")
        else:
            migrations = self._release_migrations(version)
        with self.db.connection() as conn:
            applied = applied_migrations(conn)
        differ = sorted(v for v, s in migrations.items() if v in applied and applied[v] != s)
        if differ:
            raise UpdateError(
                f"migration checksums differ from the applied ones: {', '.join(differ)}"
            )
        new = sorted(set(migrations) - set(applied))
        unknown = sorted(set(applied) - set(migrations))
        restore_path = None
        if unknown:
            if request["kind"] == "install":
                raise UpdateError(f"{version} lacks applied migrations: {', '.join(unknown)}")
            restore_path = self._pre_update_dump(from_version)
            self._log(
                rid,
                "verifying",
                f"the database has migrations {version} does not know ({', '.join(unknown)}): "
                f"the dump taken before {from_version} was installed will be restored",
                "warning",
            )
        self._log(
            rid,
            "verifying",
            f"{from_version} -> {version}; new migrations: {', '.join(new) or 'none'}",
        )
        self._update(
            rid,
            from_version=from_version,
            from_tag=from_tag,
            from_link=from_link,
            archive_sha256=sha,
            key_id=key_id,
            git_commit=commit,
            applied_before=Jsonb(applied),
            restore_path=str(restore_path) if restore_path else None,
        )

    def _release_migrations(self, version: str) -> dict[str, str]:
        release = self.layout.release(version)
        if not release.is_dir():
            raise UpdateError(f"{release} is missing: cannot roll back to {version}")
        directory = release / MIGRATIONS_PREFIX.rstrip("/")
        return {m.version: m.checksum for m in discover_migrations(directory)}

    def _pre_update_dump(self, version: str) -> Path:
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT backup_path FROM ops.release WHERE version = %s", (version,)
            ).fetchone()
        path = Path(row["backup_path"]) if row and row["backup_path"] else None
        if path is None or not path.is_file():
            raise UpdateError(
                f"rolling back across migrations needs the dump taken before {version} was "
                f"installed, and it is missing ({path or 'not recorded'})"
            )
        return path

    def _archive(self, request: Mapping[str, Any]) -> Path:
        """The bundle and its signature in ``archives/`` (copied from the upload volume, or
        downloaded); verified afterwards from this copy only."""
        version = str(request["version"])
        self.layout.archives.mkdir(parents=True, exist_ok=True)
        archive = self.layout.archives / archive_name(version)
        signature = default_signature_path(archive)
        if request["source"] == "upload":
            src = self.upload_dir / f"{request['upload_sha256']}.tar.gz"
            if not src.is_file():
                raise UpdateError("the uploaded file is gone; upload it again")
            part = archive.with_name(archive.name + ".part")
            shutil.copyfile(src, part)
            if _sha256(part) != request["upload_sha256"]:
                part.unlink()
                raise UpdateError("the uploaded file changed after it was confirmed")
            part.replace(archive)
            src_sig = default_signature_path(src)
            if src_sig.is_file():
                shutil.copyfile(src_sig, signature)
            else:
                signature.unlink(missing_ok=True)
        elif not (archive.is_file() and signature.is_file()):
            self._download(version)
        return archive

    def _download(self, version: str, release: Mapping[str, Any] | None = None) -> Path:
        if self.github is None:
            raise UpdateError("the GitHub source is not configured (MDHUB_UPDATE_GITHUB_TOKEN)")
        release = release if release is not None else self.github.release(version)
        assets = {a.get("name"): a for a in release.get("assets") or [] if isinstance(a, dict)}
        self.layout.archives.mkdir(parents=True, exist_ok=True)
        archive = self.layout.archives / archive_name(version)
        for target in (archive, default_signature_path(archive)):
            asset = assets.get(target.name)
            if asset is None:
                raise UpdateError(f"release v{version} has no asset {target.name}")
            self.github.download(str(asset.get("url") or ""), target)
        return archive

    def _backing_up(self, request: dict[str, Any]) -> None:
        dbname = conninfo_to_dict(self.db.conninfo).get("dbname") or "db"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"pre-update-{dbname}-{request['from_version']}-to-{request['version']}-{stamp}"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"{name}.dump"
        partial = target.with_name(target.name + ".partial")
        try:
            self.host.dump(partial)
            if not partial.is_file() or partial.stat().st_size == 0:
                raise UpdateError("pg_dump produced no output")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        self._update(int(request["id"]), backup_path=str(target))
        self._log(
            int(request["id"]),
            "backing_up",
            f"pre-update dump {target} ({target.stat().st_size} bytes), kept outside the rotation",
        )

    def _staging(self, request: dict[str, Any]) -> None:
        version = str(request["version"])
        release = self.layout.release(version)
        if request["kind"] == "install":
            archive = self.layout.archives / archive_name(version)
            if not archive.is_file() or _sha256(archive) != request["archive_sha256"]:
                raise UpdateError("the verified archive changed or disappeared")
            tmp = self.layout.releases / f".staging-{version}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp, filter="data")
            if release.exists():
                shutil.rmtree(release)
            (tmp / "mdhub").rename(release)
            shutil.rmtree(tmp)
        for name in (".env", "updater.env"):
            link = release / "deploy" / name
            if (self.layout.shared / name).is_file() and not link.is_symlink():
                link.unlink(missing_ok=True)
                link.symlink_to(self.layout.shared / name)
        self._log(int(request["id"]), "staging", f"building mdhub:{version} from {release}")
        self.host.compose(
            self.layout.compose_file(release), ["build"], tag=version, timeout=BUILD_TIMEOUT_S
        )

    def _drain(self, on: bool, reason: str | None = None) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE ops.worker_control SET drain = %s, reason = %s, changed_at = now()",
                (on, reason if on else None),
            )

    def _busy(self) -> list[str]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """
                SELECT job FROM ops.job_run WHERE status = 'running'
                UNION ALL SELECT job FROM ops.job_request WHERE state = 'running'
                """
            ).fetchall()
        return sorted({str(r["job"]) for r in rows})

    def _draining(self, request: dict[str, Any]) -> None:
        rid = int(request["id"])
        self._drain(True, f"update {rid} to {request['version']}")
        deadline = self.clock() + self.drain_timeout_s
        busy = self._busy()
        if busy:
            self._log(rid, "draining", f"waiting for running jobs: {', '.join(busy)}")
        while busy:
            if self.clock() >= deadline:
                self._log(
                    rid,
                    "draining",
                    f"still running after {self.drain_timeout_s:.0f} s: {', '.join(busy)}; "
                    "stopping the worker anyway (job locks and catch-up recover them)",
                    "warning",
                )
                break
            self.sleep(self.poll_s)
            busy = self._busy()
        self.host.compose(
            self.layout.compose_file(self.layout.current),
            ["stop", "worker"],
            tag=str(request["from_tag"]),
            timeout=COMPOSE_TIMEOUT_S,
        )
        self._log(rid, "draining", "worker stopped")

    def _migrating(self, request: dict[str, Any]) -> None:
        rid, version = int(request["id"]), str(request["version"])
        if request["restore_path"]:
            with self.db.connection() as conn:
                applied = applied_migrations(conn)
            if set(applied) - set(self._release_migrations(version)):
                self.host.compose(
                    self.layout.compose_file(self.layout.current),
                    ["stop", "api", "admin"],
                    tag=str(request["from_tag"]),
                    timeout=COMPOSE_TIMEOUT_S,
                )
                self._restore(rid, Path(request["restore_path"]))
        self.host.compose(
            self.layout.compose_file(self.layout.release(version)),
            ["run", "--rm", "--no-deps", "migrate"],
            tag=version,
            timeout=PG_DUMP_TIMEOUT_S,
        )
        with self.db.connection() as conn:
            applied = applied_migrations(conn)
        added = sorted(set(applied) - set(request["applied_before"] or {}))
        self._log(rid, "migrating", f"migrations applied: {', '.join(added) or 'none'}")

    def _switch(self, tag: str, link: str) -> None:
        write_env_value(self.layout.env_file, "MDHUB_IMAGE_TAG", tag)
        tmp = self.layout.root / ".current.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(link)
        os.replace(tmp, self.layout.current)

    def _starting(self, request: dict[str, Any]) -> None:
        version = str(request["version"])
        self._drain(False)
        self._switch(version, f"releases/{version}")
        self.host.compose(
            self.layout.compose_file(self.layout.current),
            ["up", "-d", "--no-deps", *SERVICES],
            tag=version,
            timeout=COMPOSE_TIMEOUT_S,
        )
        self._log(int(request["id"]), "starting", f"MDHUB_IMAGE_TAG={version}; services started")

    def _problems(self, version: str, token: str) -> list[str]:
        problems = []
        status, body = self.host.http_json(f"{self.api_url}/health/ready")
        running = body.get("version") if isinstance(body, dict) else None
        if status != 200 or running != version:
            problems.append(f"/health/ready: HTTP {status}, version {running}")
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM ops.worker_heartbeat
                WHERE process = 'worker' AND version = %s AND stopped_at IS NULL
                  AND beat_at > now() - interval '2 minutes'
                """,
                (version,),
            ).fetchone()
        if row is None:
            problems.append(f"no worker heartbeat with version {version}")
        status, _ = self.host.http_json(f"{self.api_url}/v1/coverage", token)
        if status != 200:
            problems.append(f"smoke request /v1/coverage: HTTP {status}")
        return problems

    def _checking(self, request: dict[str, Any]) -> None:
        rid, version = int(request["id"]), str(request["version"])
        name = f"updater-smoke-{rid}"
        with self.db.connection() as conn:
            revoke_token(conn, name)
            token, _ = create_token(
                conn,
                name,
                ["read"],
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_by="updater",
            )
        try:
            deadline = self.clock() + self.check_timeout_s
            while problems := self._problems(version, token):
                if self.clock() >= deadline:
                    raise UpdateError(
                        f"not healthy after {self.check_timeout_s:.0f} s: {'; '.join(problems)}"
                    )
                self.sleep(self.poll_s)
        finally:
            with self.db.connection() as conn:
                revoke_token(conn, name)
        self._log(rid, "checking", f"{version} is ready, the worker runs it, smoke request ok")

    def _done(self, request: dict[str, Any]) -> None:
        rid, version = int(request["id"]), str(request["version"])
        from_version = str(request["from_version"])
        migrations = Jsonb(self._release_migrations(version))
        with self.db.connection() as conn:
            if request["kind"] == "install":
                conn.execute(
                    "UPDATE ops.release SET status = 'superseded' WHERE status = 'previous'"
                )
                conn.execute(
                    """
                    INSERT INTO ops.release (version, source, status, installed_by)
                    VALUES (%s, 'manual', 'previous', 'manual')
                    ON CONFLICT (version) DO UPDATE SET status = 'previous'
                    """,
                    (from_version,),
                )
                conn.execute(
                    """
                    INSERT INTO ops.release (version, git_commit, source, sha256, key_id,
                        migrations, previous_version, backup_path, installed_by, status)
                    VALUES (%(v)s, %(c)s, %(s)s, %(sha)s, %(k)s, %(m)s, %(p)s, %(b)s, %(by)s,
                            'active')
                    ON CONFLICT (version) DO UPDATE SET git_commit = EXCLUDED.git_commit,
                        source = EXCLUDED.source, sha256 = EXCLUDED.sha256,
                        key_id = EXCLUDED.key_id, migrations = EXCLUDED.migrations,
                        previous_version = EXCLUDED.previous_version,
                        backup_path = EXCLUDED.backup_path, installed_by = EXCLUDED.installed_by,
                        installed_at = now(), status = 'active'
                    """,
                    {
                        "v": version,
                        "c": request["git_commit"],
                        "s": request["source"],
                        "sha": request["archive_sha256"],
                        "k": request["key_id"],
                        "m": migrations,
                        "p": from_version,
                        "b": request["backup_path"],
                        "by": request["requested_login"],
                    },
                )
            else:
                conn.execute(
                    "UPDATE ops.release SET status = 'rolled_back' WHERE version = %s",
                    (from_version,),
                )
                conn.execute(
                    """
                    INSERT INTO ops.release (version, source, status, installed_by, migrations)
                    VALUES (%s, 'manual', 'active', %s, %s)
                    ON CONFLICT (version) DO UPDATE SET status = 'active', installed_at = now()
                    """,
                    (version, request["requested_login"], migrations),
                )
        self._finish(rid, "done")
        self._replace_self(rid, version)

    def _replace_self(self, rid: int, tag: str) -> None:
        """Last step: a one-off container of the new image recreates the ``updater`` service
        (this container is stopped by it, so it cannot run that compose command itself)."""
        if tag == self.version:
            return
        compose = str(self.layout.compose_file(self.layout.current))
        try:
            self.host.compose(
                Path(compose),
                [
                    "run",
                    "-d",
                    "--rm",
                    "--no-deps",
                    "--entrypoint",
                    "docker",
                    "updater",
                    "compose",
                    "-f",
                    compose,
                    "up",
                    "-d",
                    "--no-deps",
                    "updater",
                ],
                tag=tag,
                timeout=COMPOSE_TIMEOUT_S,
            )
            self._log(rid, "done", f"the updater {tag} replaces this one ({self.version})")
        except Exception as exc:
            self._log(
                rid,
                "done",
                f"could not start the updater {tag} ({self._error(exc)}); run "
                "`docker compose up -d updater` on the server",
                "warning",
            )

    def _finish(self, request_id: int, state: str, error: str | None = None) -> None:
        request = self._set(request_id, state, **({"error": error} if error else {}))
        self._log(request_id, state, error or state, "error" if error else "info")
        if request["source"] == "upload":
            src = self.upload_dir / f"{request['upload_sha256']}.tar.gz"
            src.unlink(missing_ok=True)
            default_signature_path(src).unlink(missing_ok=True)

    # ------------------------------------------------------------------- rollback

    def _rollback(self, request_id: int, error: str) -> str:
        request = self._set(request_id, "rolling_back", error=error)
        from_tag = str(request["from_tag"] or self.version)
        current = self.layout.compose_file(self.layout.current)
        try:
            with self.db.connection() as conn:
                applied = applied_migrations(conn)
            if request["applied_before"] is not None and applied != request["applied_before"]:
                if not request["backup_path"]:
                    raise UpdateError("the migrations changed and there is no pre-update dump")
                self._log(
                    request_id,
                    "rolling_back",
                    "the applied migrations changed: restoring the pre-update dump "
                    f"{request['backup_path']}",
                    "warning",
                )
                self.host.compose(
                    current, ["stop", *SERVICES], tag=from_tag, timeout=COMPOSE_TIMEOUT_S
                )
                self._restore(request_id, Path(request["backup_path"]))
            self._drain(False)
            if request["from_link"]:
                self._switch(from_tag, str(request["from_link"]))
            self.host.compose(
                current,
                ["up", "-d", "--no-deps", *SERVICES],
                tag=from_tag,
                timeout=COMPOSE_TIMEOUT_S,
            )
            if request["kind"] == "install":
                with self.db.connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO ops.release (version, git_commit, source, sha256, key_id,
                                                 installed_by, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'failed')
                        ON CONFLICT (version) DO UPDATE SET status = 'failed'
                        """,
                        (
                            request["version"],
                            request["git_commit"],
                            request["source"],
                            request["archive_sha256"],
                            request["key_id"],
                            request["requested_login"],
                        ),
                    )
        except Exception as exc:
            message = (
                f"{error}; rollback failed: {self._error(exc)} (manual recovery: README_DEPLOY §13)"
            )
            self._finish(request_id, "failed", message[:_ERR_CHARS])
            return "failed"
        self._log(request_id, "rolling_back", f"back on {from_tag}")
        self._finish(request_id, "rolled_back")
        return "rolled_back"

    def _restore(self, request_id: int, dump: Path) -> None:
        """Restore ``dump`` keeping :data:`KEPT_TABLES` (journaled, so a crash redoes it)."""
        if not dump.is_file():
            raise UpdateError(f"dump {dump} not found")
        tables: dict[str, list[Any]] = {}
        with self.db.connection() as conn:
            for table in KEPT_TABLES:
                rows = conn.execute(
                    sql.SQL("SELECT to_jsonb(t) AS r FROM {} t").format(
                        sql.Identifier(*table.split("."))
                    )
                ).fetchall()
                tables[table] = [r["r"] for r in rows]
        journal = {"request_id": request_id, "dump": str(dump), "tables": tables}
        tmp = self.layout.journal.with_name(self.layout.journal.name + ".tmp")
        tmp.write_text(json.dumps(journal), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.layout.journal)
        self._restore_journal(journal)
        self._log(request_id, "restore", f"database restored from {dump}")

    def _restore_journal(self, journal: Mapping[str, Any]) -> None:
        if self.heartbeat is not None:
            self.heartbeat.stop()
        had_lock = self._lock_conn is not None
        self.release_lock()
        pause = self.containers.pause if self.containers is not None else contextlib.nullcontext()
        try:
            with pause:
                self.host.restore(Path(journal["dump"]))
            with self.db.connection() as conn:
                _reload(conn, journal["tables"])
                restrict_admin(conn)  # service roles created after the dump get their grants
            self.layout.journal.unlink()
        finally:
            if had_lock and not self.acquire_lock():
                raise UpdateError("another updater took the lock during the restore")
            if self.heartbeat is not None:
                self.heartbeat.start()

    # ----------------------------------------------------------------- GitHub list

    def refresh_releases(self) -> None:
        """List GitHub releases into ``ops.release_available``; download and verify the newest
        ones above the running version so the console can show their migrations."""
        assert self.github is not None
        running = self._active_version()
        newer: list[tuple[str, dict[str, Any]]] = []
        seen: list[str] = []
        with self.db.connection() as conn:
            for r in self.github.releases():
                tag = str(r.get("tag_name") or "")
                version = tag[1:] if tag.startswith("v") else ""
                try:
                    key = version_key(version)
                except ReleaseError:
                    continue
                if r.get("draft"):
                    continue
                seen.append(version)
                conn.execute(
                    """
                    INSERT INTO ops.release_available (version, tag, name, notes, html_url,
                                                       published_at, prerelease)
                    VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s)
                    ON CONFLICT (version) DO UPDATE SET tag = EXCLUDED.tag, name = EXCLUDED.name,
                        notes = EXCLUDED.notes, html_url = EXCLUDED.html_url,
                        published_at = EXCLUDED.published_at, prerelease = EXCLUDED.prerelease,
                        checked_at = now()
                    """,
                    (
                        version,
                        tag,
                        r.get("name"),
                        str(r.get("body") or "")[:_NOTES_CHARS],
                        r.get("html_url"),
                        r.get("published_at"),
                        bool(r.get("prerelease")),
                    ),
                )
                if key > version_key(running):
                    newer.append((version, r))
            conn.execute("DELETE FROM ops.release_available WHERE version <> ALL(%s)", (seen,))
            checked = {
                str(row["version"])
                for row in conn.execute(
                    "SELECT version FROM ops.release_available WHERE verified IS NOT NULL"
                ).fetchall()
            }
        newer.sort(key=lambda item: version_key(item[0]), reverse=True)
        for version, release in newer[:PREFETCH]:
            if version in checked and (self.layout.archives / archive_name(version)).is_file():
                continue
            try:
                archive = self._download(version, release)
                verified = verify_archive(archive, load_pinned_keys(self.keys_dir))
            except (UpdateError, ReleaseError) as exc:
                values: dict[str, Any] = {"verified": False, "error": self._error(exc)}
            else:
                m = verified.manifest
                values = {
                    "verified": True,
                    "error": None,
                    "archive_sha256": _sha256(archive),
                    "key_id": verified.key.key_id_hex if verified.key else None,
                    "git_commit": m.commit,
                    "min_previous_version": m.min_previous_version,
                    "migrations": Jsonb(dict(m.migrations)),
                }
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in values
            )
            with self.db.connection() as conn:
                conn.execute(
                    sql.SQL("UPDATE ops.release_available SET {} WHERE version = {}").format(
                        assignments, sql.Placeholder("_v")
                    ),
                    {**values, "_v": version},
                )


def _reload(conn: DictConnection, tables: Mapping[str, list[Any]]) -> None:
    """Load the journaled tables into the restored database: the append-only audit log gets
    the rows the dump lacks, the update tables are replaced (ids kept)."""
    for table in KEPT_TABLES:
        ident = sql.Identifier(*table.split("."))
        rows = list(tables.get(table, []))
        if table == "admin.audit_log":
            top = conn.execute(sql.SQL("SELECT coalesce(max(id), 0) AS m FROM {}").format(ident))
            row = top.fetchone()
            rows = [r for r in rows if int(r["id"]) > int(row["m"] if row else 0)]
        else:
            conn.execute(sql.SQL("DELETE FROM {}").format(ident))
        if rows:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} OVERRIDING SYSTEM VALUE "
                    "SELECT * FROM jsonb_populate_recordset(NULL::{}, %s)"
                ).format(ident, ident),
                (Jsonb(rows),),
            )
        if table != "ops.release":
            conn.execute(
                sql.SQL("SELECT setval(pg_get_serial_sequence(%s, 'id'), max(id)) FROM {}").format(
                    ident
                ),
                (table,),
            )


# ------------------------------------------------------------------------------ entry point


def run(settings: Settings, stop: threading.Event) -> None:
    """``mdhub updater``: heartbeat (process ``updater``), container status and the request
    loop."""
    db = Database.from_settings(settings)
    secrets = settings.secret_values()
    github = None
    if settings.update_github_token is not None:
        github = GitHub(
            settings.update_github_repo,
            settings.update_github_token.get_secret_value(),
            proxy=settings.proxy_url(),
        )
    else:
        log.info("MDHUB_UPDATE_GITHUB_TOKEN not set: GitHub releases are not listed")
    updater = Updater(
        db,
        ComposeHost(db.conninfo, secrets),
        Layout(Path(settings.update_root)),
        keys_dir=default_keys_dir(),
        upload_dir=Path(settings.update_upload_dir),
        backup_dir=backup_dir(settings.backup_dir),
        api_url=settings.update_api_url,
        github=github,
        secrets=secrets,
    )
    updater.heartbeat = Heartbeat(db, "updater")
    updater.heartbeat.start()
    updater.containers = ContainerReporter(db)
    threading.Thread(
        target=updater.containers.serve, args=(stop,), name="mdhub-containers", daemon=True
    ).start()
    log.info("updater %s started (root %s)", __version__, settings.update_root)
    try:
        updater.serve(stop)
    finally:
        updater.heartbeat.stop()
