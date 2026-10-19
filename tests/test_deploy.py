"""Deployment files: compose structure, env names, scripts, systemd units, grant SQL."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

import pgserver
import psycopg
import pytest
from psycopg import sql
from pydantic import AliasChoices, SecretStr

from mdhub.config import Settings
from mdhub.db.migrate import MigrationError, migrate
from mdhub.loaders.maintenance import BACKUP_DIR_ENV

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PSQL = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
SERVICES = {"postgres", "migrate", "worker", "api", "admin", "updater", "hostmon", "caddy"}


def env_names(path: Path) -> list[str]:
    names = []
    for line in path.read_text().splitlines():
        m = re.match(r"^#?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            assert m.group(2) == "", f"env.example must not contain values: {line}"
            names.append(m.group(1))
    return names


def settings_env_names() -> set[str]:
    out = set()
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        assert isinstance(alias, AliasChoices)
        out.add(str(alias.choices[0]))
    return out


def test_env_example_lists_every_setting() -> None:
    names = env_names(DEPLOY / "env.example")
    assert len(names) == len(set(names)), "duplicate names in env.example"
    required = settings_env_names() | {
        BACKUP_DIR_ENV,
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "MDHUB_RO_USER",
        "MDHUB_RO_PASSWORD",
        "MDHUB_API_DB_PASSWORD",
        "MDHUB_ADMIN_DB_PASSWORD",
        "MDHUB_HOSTMON_DB_PASSWORD",
        "MDHUB_API_DOMAIN",
        "ACME_EMAIL",
    }
    assert required <= set(names), sorted(required - set(names))
    # DESIGN §7 names
    for name in (
        "MDHUB_DATABASE_URL",
        "MDHUB_TZ",
        "MDHUB_HTTP_PROXY",
        "MDHUB_ISS_RPS",
        "MDHUB_RAW_RETENTION_DAYS_INTRADAY",
        "MDHUB_API_TOKENS",
        "MDHUB_ENABLE_SDFI",
        "MOEX_API_KEY",
        "MOEX_PASSPORT_LOGIN",
        "MOEX_PASSPORT_PASSWORD",
        "RATESCALC_USERNAME",
        "RATESCALC_PASSWORD",
        "CBONDS_LOGIN",
        "CBONDS_PASSWORD",
        "OTC_MONITOR_TOKEN",
    ):
        assert name in names


def test_env_example_uncommented_values_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copying env.example verbatim (compose env_file) must still give valid Settings."""
    raw = (DEPLOY / "env.example").read_text()
    active = re.findall(r"^([A-Z][A-Z0-9_]*)=$", raw, re.M)
    assert "MDHUB_DATABASE_URL" in active and "MDHUB_TZ" not in active
    for name in active:
        monkeypatch.setenv(name, "")
    s = Settings()
    assert s.database_url is None and s.tz == "Europe/Moscow" and s.moex_api_key is None


def _compose_text() -> str:
    return (DEPLOY / "docker-compose.yml").read_text()


def _env_files(svc: dict[str, Any]) -> list[Any]:
    """A service's ``env_file`` as a list (compose allows a string, a list or long syntax)."""
    files = svc.get("env_file", [])
    return [files] if isinstance(files, str) else list(files)


def test_compose_structure_text() -> None:
    text = _compose_text()
    top = re.findall(r"^  ([a-z][a-z0-9_-]*):\s*$", text.split("\nservices:\n", 1)[1], re.M)
    assert set(top) >= SERVICES
    assert "image: postgres:16" in text
    assert '"127.0.0.1:${MDHUB_PG_HOST_PORT:-5432}:5432"' in text
    assert "mdhub.api.app:app" in text and '["mdhub", "scheduler"]' in text
    assert '["mdhub", "migrate"]' in text
    assert "service_completed_successfully" in text and "service_healthy" in text
    assert "env_file: .env" in text and "max-size" in text


def test_compose_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    doc: dict[str, Any] = yaml.safe_load(_compose_text())
    services = doc["services"]
    assert set(services) == SERVICES
    pg = services["postgres"]
    assert pg["image"] == "postgres:16" and "healthcheck" in pg
    assert all(p.startswith("127.0.0.1:") for p in pg["ports"])
    assert any("postgresql.conf" in v for v in pg["volumes"])
    for name in ("migrate", "worker", "api"):
        svc = services[name]
        assert svc["env_file"] == ".env"
        assert svc["build"] == {
            "context": "..",
            "dockerfile": "deploy/Dockerfile",
            "args": {"MDHUB_VERSION": "${MDHUB_VERSION:-}"},
        }
        assert "MDHUB_DATABASE_URL" in svc["environment"]
        assert svc["logging"]["options"]["max-size"]
    assert services["migrate"]["restart"] == "no"
    assert services["worker"]["restart"] == "unless-stopped"
    assert services["worker"]["command"] == ["mdhub", "scheduler"]
    assert services["worker"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["command"][:2] == ["uvicorn", "mdhub.api.app:app"]
    assert all(p.startswith("127.0.0.1:") for p in services["api"]["ports"])
    assert services["caddy"]["profiles"] == ["tls"]
    admin = services["admin"]
    assert admin["command"][:2] == ["uvicorn", "mdhub.admin.app:app"]
    assert "--no-proxy-headers" in admin["command"] and "ports" not in admin
    caddy_ip = services["caddy"]["networks"]["admin_edge"]["ipv4_address"]
    assert admin["environment"]["MDHUB_ADMIN_TRUSTED_PROXIES"] == caddy_ip
    assert "MDHUB_DATABASE_URL" in admin["environment"]


def test_admin_service_gets_no_source_credentials() -> None:
    """The console runs no job (the worker does, ops.job_request): its container must not see
    source credentials, the egress proxy or the read-API tokens."""
    yaml = pytest.importorskip("yaml")
    admin = yaml.safe_load(_compose_text())["services"]["admin"]
    assert "env_file" not in admin
    secret_fields = {
        str(field.validation_alias.choices[0])  # type: ignore[union-attr]
        for name, field in Settings.model_fields.items()
        if field.annotation == (SecretStr | None)
    }
    forbidden = (secret_fields - {"MDHUB_DATABASE_URL", "MDHUB_ADMIN_SECRET_KEY"}) | {
        "MDHUB_API_TOKENS",
        "MDHUB_HTTP_PROXY",
    }
    assert {"MOEX_API_KEY", "RATESCALC_PASSWORD", "CBONDS_PASSWORD"} <= forbidden
    assert not forbidden & set(admin["environment"]), forbidden & set(admin["environment"])
    assert {n for n in admin["environment"] if not _hostmon_setting(n)} <= {
        "MDHUB_DATABASE_URL",
        "MDHUB_ADMIN_DOMAIN",
        "MDHUB_ADMIN_ORIGIN",
        "MDHUB_ADMIN_ALLOW_CIDRS",
        "MDHUB_ADMIN_TRUSTED_PROXIES",
        "MDHUB_ADMIN_SECRET_KEY",
        "MDHUB_LOG_LEVEL",
        "MDHUB_TZ",
        # snapshot cut times: the jobs page shows the schedule the worker runs
        "MDHUB_SNAPSHOT_STATIC_TIME",
        "MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME",
        "MDHUB_SNAPSHOT_EOD_FINAL_TIME",
        "MDHUB_SNAPSHOT_INTRADAY_MINUTES",
        "MDHUB_SNAPSHOT_INTRADAY_WINDOW",
    }


def test_only_the_updater_has_the_docker_socket_and_github_token() -> None:
    """ADMIN_DESIGN §2/§6: the updater alone gets the docker socket and the GitHub token
    (updater.env); it reads no .env, so no source credentials or API tokens either."""
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load(_compose_text())["services"]
    with_socket = sorted(
        name
        for name, svc in services.items()
        if any("docker.sock" in str(v) for v in svc.get("volumes", []))
    )
    assert with_socket == ["updater"]
    updater = services["updater"]
    assert updater["command"] == ["mdhub", "updater"] and "ports" not in updater
    assert updater["env_file"] == [{"path": "updater.env", "required": False}]
    secret_fields = {
        str(field.validation_alias.choices[0])  # type: ignore[union-attr]
        for field in Settings.model_fields.values()
        if field.annotation == (SecretStr | None)
    }
    forbidden = (secret_fields - {"MDHUB_DATABASE_URL", "MDHUB_HTTP_PROXY"}) | {"MDHUB_API_TOKENS"}
    assert "MDHUB_UPDATE_GITHUB_TOKEN" in forbidden  # from updater.env, never from .env
    assert not forbidden & set(updater["environment"]), forbidden & set(updater["environment"])
    root = "${MDHUB_UPDATE_ROOT:-/opt/mdhub}"
    assert f"{root}:{root}" in updater["volumes"]  # host paths = container paths for compose
    assert "uploads:/var/lib/mdhub/uploads" in updater["volumes"]
    assert "uploads:/var/lib/mdhub/uploads" in services["admin"]["volumes"]
    for name, svc in services.items():
        if name != "updater":
            assert "updater.env" not in str(svc.get("env_file", "")), name
            assert "MDHUB_UPDATE_GITHUB_TOKEN" not in str(svc.get("environment", "")), name


def test_secrets_private_key_reaches_the_worker_only() -> None:
    """MDHUB_SECRETS_PRIVATE_KEY opens the console's source credentials (ADMIN_DESIGN §9):
    blanked for every service that reads .env except the worker; admin has no .env at all."""
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load(_compose_text())["services"]
    key = "MDHUB_SECRETS_PRIVATE_KEY"
    assert services["worker"]["environment"][key] == "${MDHUB_SECRETS_PRIVATE_KEY:-}"
    assert "MDHUB_DATABASE_URL" in services["worker"]["environment"]  # merged, not replaced
    for name, svc in services.items():
        if name == "worker":
            continue
        if ".env" in [f if isinstance(f, str) else f["path"] for f in _env_files(svc)]:
            assert svc["environment"][key] == "", name
        else:
            assert key not in svc.get("environment", {}), name


def _hostmon_setting(name: str) -> bool:
    return name.startswith("MDHUB_HOSTMON_") or name == "MDHUB_NTP_SERVER"


def test_hostmon_service_has_no_docker_socket_and_no_credentials() -> None:
    """hostmon (ADMIN_DESIGN §10) reads the host read-only; the updater alone has the socket."""
    yaml = pytest.importorskip("yaml")
    svc = yaml.safe_load(_compose_text())["services"]["hostmon"]
    assert svc["command"] == ["mdhub", "hostmon"]
    assert "env_file" not in svc and "ports" not in svc
    assert not any("docker.sock" in v for v in svc["volumes"])
    assert all(v.split(":")[2].split(",")[0] == "ro" for v in svc["volumes"]), svc["volumes"]
    assert {v.split(":")[1] for v in svc["volumes"]} == {"/host/proc", "/host/sys", "/rootfs"}
    env = set(svc["environment"])
    assert {n for n in env if not _hostmon_setting(n)} == {"MDHUB_DATABASE_URL"}
    hostmon_fields = {
        str(f.validation_alias.choices[0])  # type: ignore[union-attr]
        for name, f in Settings.model_fields.items()
        if name.startswith("hostmon_") or name == "ntp_server"
    }
    assert env - {"MDHUB_DATABASE_URL"} == hostmon_fields
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]


def test_dockerfile() -> None:
    text = (DEPLOY / "Dockerfile").read_text()
    assert "python:3.12-slim" in text
    assert "uv sync --frozen --no-dev" in text
    assert "postgresql-client-16" in text
    assert "docker-ce-cli docker-compose-plugin" in text  # for the updater service
    assert "/var/lib/mdhub/uploads" in text  # upload volume owned by mdhub (console writes)
    assert re.search(r"^USER mdhub$", text, re.M)
    ignore = (DEPLOY / "Dockerfile.dockerignore").read_text()
    assert "!uv.lock" in ignore and "!src/" in ignore


def test_postgres_conf_is_parseable() -> None:
    for raw in (DEPLOY / "postgres" / "postgresql.conf").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            assert re.match(r"^[a-z_.]+ = ('[^']*'|[A-Za-z0-9.]+)$", line), raw
    text = (DEPLOY / "postgres" / "postgresql.conf").read_text()
    assert "shared_buffers = 1GB" in text


@pytest.mark.parametrize(
    "script",
    ["install.sh", "restore_seed.sh", "make_bundle.sh", "postgres/initdb/10-mdhub-roles.sh"],
)
def test_shell_scripts_syntax(script: str) -> None:
    path = DEPLOY / script
    assert os.access(path, os.X_OK), f"{script} must be executable"
    assert path.read_text().startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in path.read_text()
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(path)], check=True)


def test_systemd_units() -> None:
    units = sorted((DEPLOY / "systemd").iterdir())
    names = {u.name for u in units}
    assert {
        "mdhub-worker.service",
        "mdhub-api.service",
        "mdhub-migrate.service",
        "mdhub-backup.service",
        "mdhub-backup.timer",
    } <= names
    for unit in units:
        cp = configparser.ConfigParser(strict=True, interpolation=None)
        cp.optionxform = str  # type: ignore[assignment,method-assign]
        cp.read(unit)
        assert "Unit" in cp
        if unit.suffix == ".service":
            svc = cp["Service"]
            assert svc["User"] == "mdhub"
            assert svc["EnvironmentFile"] == "/etc/mdhub/mdhub.env"
            assert svc["ExecStart"].startswith("/opt/mdhub/.venv/bin/")
        else:
            assert "OnCalendar" in cp["Timer"]
    worker = (DEPLOY / "systemd" / "mdhub-worker.service").read_text()
    assert "mdhub scheduler" in worker and "Restart=always" in worker


def test_caddyfile_and_readme() -> None:
    caddy = (DEPLOY / "Caddyfile").read_text()
    assert "{$MDHUB_API_DOMAIN}" in caddy and "reverse_proxy" in caddy
    admin_site = caddy.split("{$MDHUB_ADMIN_DOMAIN", 1)[1]
    assert "not remote_ip {$MDHUB_ADMIN_ALLOW_CIDRS" in admin_site
    assert admin_site.index('respond @outside "Forbidden" 403') < admin_site.index("reverse_proxy")
    # the 200 MB body limit applies to the upload path only
    assert "@upload path /updates/upload" in admin_site
    assert "@not_upload not path /updates/upload" in admin_site
    upload = admin_site.split("request_body @upload", 1)[1].split("}", 1)[0]
    other = admin_site.split("request_body @not_upload", 1)[1].split("}", 1)[0]
    assert "max_size 201MB" in upload and "max_size 1MB" in other
    assert admin_site.count("request_body") == 2
    readme = (DEPLOY / "README_DEPLOY.md").read_text()
    for needle in (
        "docker compose up -d",
        "restore_seed.sh",
        "make_bundle.sh",
        "db/mdhub.dump",
        "SHA256SUMS",
        "mdhub db-info",
        "coverage",
        "443",
        "Self-update from the console",
        "Rehearsal on a throw-away VPS",
        "updater.env",
    ):
        assert needle in readme


@pytest.mark.skipif(not PSQL.exists(), reason="pgserver psql not available")
def test_readonly_grant_scripts(db_url: str, conn: psycopg.Connection[Any]) -> None:
    ro = f"ro_{uuid.uuid4().hex[:10]}"
    owner = conn.execute("SELECT current_user AS u, current_database() AS d").fetchone()
    assert owner is not None
    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(ro)))
    conn.commit()
    try:

        def psql(*args: str) -> None:
            subprocess.run(
                [str(PSQL), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db_url, *args],
                check=True,
                capture_output=True,
            )

        psql(
            "-v",
            f"owner={owner['u']}",
            "-v",
            f"ro_user={ro}",
            "-v",
            f"dbname={owner['d']}",
            "-f",
            str(DEPLOY / "sql" / "readonly_defaults.sql"),
        )
        psql("-v", f"ro_user={ro}", "-f", str(DEPLOY / "sql" / "grant_readonly.sql"))
        conn.execute("CREATE TABLE md.deploy_after (x int)")  # covered by default privileges
        conn.commit()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ro)))
        conn.execute("SELECT count(*) FROM ops.job_run").fetchone()
        conn.execute("SELECT count(*) FROM ops.v_coverage").fetchone()
        conn.execute("SELECT count(*) FROM md.deploy_after").fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO md.deploy_after VALUES (1)")
        conn.rollback()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ro)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM admin.api_token")
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(ro)))
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL ON TABLES FROM {}").format(
                sql.Identifier(owner["u"]), sql.Identifier(ro)
            )
        )
        conn.commit()
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(ro)))
        conn.commit()


@pytest.mark.skipif(not PSQL.exists(), reason="pgserver psql not available")
def test_readonly_role_cannot_read_admin(empty_db_url: str) -> None:
    """VPS order: role + default privileges at initdb, then migrations, then the grant
    script (and a re-run of the defaults, as after a seed restore). The read-only role must
    not see anything in schema admin (ADMIN_DESIGN §3.1) but keeps reading ops/md/ref."""
    ro = f"ro_{uuid.uuid4().hex[:10]}"
    conn = psycopg.connect(empty_db_url, autocommit=True)
    owner = conn.execute("SELECT current_user, current_database()").fetchone()
    assert owner is not None
    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(ro)))

    def psql(*args: str) -> None:
        subprocess.run(
            [str(PSQL), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", empty_db_url, *args],
            check=True,
            capture_output=True,
        )

    defaults = (
        "-v", f"owner={owner[0]}", "-v", f"ro_user={ro}", "-v", f"dbname={owner[1]}",
        "-f", str(DEPLOY / "sql" / "readonly_defaults.sql"),
    )  # fmt: skip
    try:
        psql(*defaults)
        migrate(empty_db_url)
        psql("-v", f"ro_user={ro}", "-f", str(DEPLOY / "sql" / "grant_readonly.sql"))
        psql(*defaults)
        privileges = conn.execute(
            """
            SELECT c.relname, has_table_privilege(%(ro)s, c.oid, 'SELECT') AS can_select
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'admin' AND c.relkind IN ('r', 'v', 'S', 'p')
            """,
            {"ro": ro},
        ).fetchall()
        assert ("api_token", False) in privileges and not any(p[1] for p in privileges)
        names = {p[0] for p in privileges}
        assert {
            "user_account",
            "session",
            "webauthn_credential",
            "totp_secret",
            "recovery_code",
            "enrollment",
            "login_attempt",
            "audit_log",
        } <= names
        usage = conn.execute("SELECT has_schema_privilege(%s, 'admin', 'USAGE')", (ro,)).fetchone()
        assert usage == (False,)
        # The owner's global default privileges do reach admin: a table created there later
        # (e.g. by a restore) is readable until the grant script / migrate locks it again.
        conn.execute("CREATE TABLE admin.later (x int)")
        can = "SELECT has_table_privilege(%s, 'admin.later', 'SELECT')"
        assert conn.execute(can, (ro,)).fetchone() == (True,)
        psql("-v", f"ro_user={ro}", "-f", str(DEPLOY / "sql" / "grant_readonly.sql"))
        assert conn.execute(can, (ro,)).fetchone() == (False,)
        conn.execute("GRANT SELECT ON admin.later TO PUBLIC")
        migrate(empty_db_url)  # nothing pending, still locks admin
        assert conn.execute(can, (ro,)).fetchone() == (False,)
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ro)))
        conn.execute("SELECT count(*) FROM ops.api_request").fetchone()
        conn.execute("SELECT count(*) FROM ops.v_source_status").fetchone()
        conn.execute("SELECT count(*) FROM md.fixing").fetchone()
        for query in (
            "SELECT count(*) FROM admin.api_token",
            "SELECT name FROM admin.api_token",
            "SELECT password_hash FROM admin.user_account",
            "SELECT id_sha256 FROM admin.session",
            "SELECT secret_enc FROM admin.totp_secret",
            "SELECT count(*) FROM admin.audit_log",
            "CALL admin.restrict_access()",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
    finally:
        conn.execute("RESET ROLE")
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(ro)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(ro)))
        conn.close()


def _table_counts(url: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with psycopg.connect(url) as c:
        rels = c.execute(
            """
            SELECT schemaname, tablename FROM pg_tables
            WHERE schemaname IN ('md', 'ref', 'ops', 'legacy_md', 'legacy_bc')
            ORDER BY 1, 2
            """
        ).fetchall()
        for schema, table in rels:
            row = c.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
            ).fetchone()
            assert row is not None
            out[f"{schema}.{table}"] = int(row[0])
    return out


FORBIDDEN = re.compile(
    r"(^|/)\.env$|^mdhub/data/|(^|/)\.handoff/|(^|/)\.venv/|(^|/)\.git/|__pycache__|\.sqlite3"
)


@pytest.mark.skipif(not PSQL.exists(), reason="pgserver binaries not available")
@pytest.mark.skipif(
    shutil.which("git") is None
    or subprocess.run(
        ["git", "-C", str(DEPLOY), "rev-parse", "--is-inside-work-tree"], capture_output=True
    ).returncode
    != 0,
    reason="not a git work tree",
)
def test_make_bundle_and_restore_roundtrip(db_url: str, empty_db_url: str, tmp_path: Path) -> None:
    with psycopg.connect(db_url) as c:
        c.execute(
            "INSERT INTO md.fixing (index_code, fixing_date, source, value, unit) "
            "VALUES ('KEY_RATE', '2026-10-15', 'cbr', 16.5, 'pct')"
        )
        c.execute(
            "INSERT INTO ref.bond (secid, bond_class, coupon_type, coupon_type_basis, status) "
            "VALUES ('B1', 'corporate', 'fixed', 'moex_label', 'active')"
        )
        # admin.* data must never reach a seed dump (ADMIN_DESIGN §9)
        c.execute(
            "INSERT INTO admin.api_token (name, token_sha256, prefix, scopes) "
            "VALUES ('seed-test', %s, 'mdh_abcd', '{read}')",
            (b"\x01" * 32,),
        )
        c.execute(
            "INSERT INTO admin.source_credential (name, ciphertext, key_id) "
            "VALUES ('MOEX_API_KEY', %s, '0123456789abcdef')",
            (b"\x01" * 80,),
        )
    env = {
        **os.environ,
        "PG_BIN": str(PSQL.parent),
        "MDHUB_CMD": f"{sys.executable} -m mdhub.cli",
        "TMPDIR": str(tmp_path),
    }
    env.pop("MDHUB_DATABASE_URL", None)
    dist = tmp_path / "dist"
    built = subprocess.run(
        [
            str(DEPLOY / "make_bundle.sh"),
            "--allow-dirty",
            "--database-url",
            db_url,
            "--out-dir",
            str(dist),
            "--date",
            "20990101",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    archive = dist / "market-data-hub-20990101.tar.gz"
    assert (dist / "market-data-hub-20990101.tar.gz.sha256").is_file()
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert not [n for n in names if FORBIDDEN.search(n)]
        for required in (
            "mdhub/db/mdhub.dump",
            "mdhub/db/mdhub.dump.sha256",
            "mdhub/SEED_REPORT.md",
            "mdhub/MANIFEST.txt",
            "mdhub/SHA256SUMS",
            "mdhub/src/mdhub/db/migrations/0001_core.sql",
        ):
            assert required in names
        assert all(m.uid == 0 and m.gid == 0 for m in tar.getmembers())
        tar.extractall(tmp_path / "x", filter="data")
    root = tmp_path / "x" / "mdhub"
    manifest = (root / "MANIFEST.txt").read_text()
    assert "git_dirty:" in manifest and "pg_dump (PostgreSQL) 16" in manifest
    assert "0001_core" in manifest and "not in the dump (applied by mdhub migrate" in manifest
    report = (root / "SEED_REPORT.md").read_text()
    assert report.startswith("# market-data-hub seed report") and "- snapshot: `" in report
    assert "| fixing | 1 |" in report
    sums = (root / "SHA256SUMS").read_text().splitlines()
    assert any(line.endswith("  db/mdhub.dump") for line in sums)
    toc = subprocess.run(
        [str(PSQL.parent / "pg_restore"), "-l", str(root / "db" / "mdhub.dump")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "TABLE DATA md fixing" in toc and "TABLE admin source_credential" in toc
    assert not re.search(r"TABLE DATA admin ", toc)

    def check_dump(path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEPLOY / "make_bundle.sh"), "--check-dump", str(path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    assert check_dump(root / "db" / "mdhub.dump").returncode == 0
    full = tmp_path / "full.dump"
    subprocess.run([str(PSQL.parent / "pg_dump"), "-Fc", "-f", str(full), "-d", db_url], check=True)
    refused = check_dump(full)
    assert refused.returncode != 0 and "TABLE DATA admin source_credential" in refused.stderr

    restore = subprocess.run(
        [
            str(DEPLOY / "restore_seed.sh"),
            "--yes",
            "--database-url",
            empty_db_url,
            str(root / "db" / "mdhub.dump"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert restore.returncode == 0, restore.stdout + restore.stderr
    assert "0 applied," in restore.stdout
    assert "checksums match" in restore.stdout
    assert _table_counts(empty_db_url) == _table_counts(db_url)

    again = subprocess.run(
        [
            str(DEPLOY / "restore_seed.sh"),
            "--yes",
            "--database-url",
            empty_db_url,
            str(root / "db" / "mdhub.dump"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert again.returncode != 0 and "not empty" in again.stderr

    (root / "db" / "mdhub.dump.sha256").write_text("0" * 64 + "  mdhub.dump\n")
    bad = subprocess.run(
        [
            str(DEPLOY / "restore_seed.sh"),
            "--yes",
            "--database-url",
            empty_db_url,
            str(root / "db" / "mdhub.dump"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert bad.returncode != 0 and "does not match" in bad.stderr


def test_migrate_refuses_non_utf8_database(pg_server: Any) -> None:
    """A SQL_ASCII database (createdb without -E UTF8 on a C-locale host) fails closed."""
    name = "mdhub_sql_ascii"
    with pg_server.admin() as conn:
        conn.execute(
            sql.SQL(
                "CREATE DATABASE {} ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C' "
                "TEMPLATE template0"
            ).format(sql.Identifier(name))
        )
    try:
        with pytest.raises(MigrationError, match="encoding is 'SQL_ASCII', need UTF8"):
            migrate(pg_server.url(name))
    finally:
        pg_server.drop_db(name)
