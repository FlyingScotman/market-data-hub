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
from pydantic import AliasChoices

from mdhub.config import Settings
from mdhub.loaders.maintenance import BACKUP_DIR_ENV

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PSQL = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
SERVICES = {"postgres", "migrate", "worker", "api", "caddy"}


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
        assert svc["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}
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


def test_dockerfile() -> None:
    text = (DEPLOY / "Dockerfile").read_text()
    assert "python:3.12-slim" in text
    assert "uv sync --frozen --no-dev" in text
    assert "postgresql-client-16" in text
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
