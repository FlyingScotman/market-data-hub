"""Least-privilege roles of api, admin and hostmon (migration 0800, README_DEPLOY §16).

The apps' own suites already run as these roles (test_api, test_tokens, test_admin,
test_credentials, test_hostmon connect with ``user=mdhub_*``): that proves the grants are
enough. This module proves they are not more, that they survive every re-lock of schema admin,
and that compose connects each service as its own role.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import pgserver
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow

from mdhub.core.heartbeat import Heartbeat
from mdhub.db.migrate import migrate
from mdhub.db.pool import Database, warn_if_owner
from mdhub.hostmon.sampler import Sample, rollup, write_samples

Conn = psycopg.Connection[DictRow]
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PSQL = Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"

FORBIDDEN = [
    # the console: no history rewrite, no key swap, no market data, no sampler rights
    ("mdhub_admin", "UPDATE ops.job_run SET status = 'failed'"),
    ("mdhub_admin", "DELETE FROM ops.job_run"),
    ("mdhub_admin", "UPDATE admin.audit_log SET action = 'x'"),
    ("mdhub_admin", "DELETE FROM admin.audit_log"),
    ("mdhub_admin", "TRUNCATE admin.audit_log"),
    ("mdhub_admin", "UPDATE admin.secret_key_public SET env_names = '{}'"),
    ("mdhub_admin", "INSERT INTO admin.secret_key_public (key_id) VALUES ('0011223344556677')"),
    ("mdhub_admin", "DELETE FROM admin.user_account"),
    ("mdhub_admin", "DELETE FROM admin.api_token"),
    ("mdhub_admin", "UPDATE ops.job_request SET job = 'x'"),
    ("mdhub_admin", "DELETE FROM ops.data_gap"),
    ("mdhub_admin", "UPDATE ops.release SET status = 'active'"),
    ("mdhub_admin", "INSERT INTO ops.host_metric (metric) VALUES ('x')"),
    ("mdhub_admin", "SELECT count(*) FROM md.fixing"),
    ("mdhub_admin", "INSERT INTO md.fixing (index_code) VALUES ('X')"),
    ("mdhub_admin", "CALL admin.restrict_access()"),
    ("mdhub_admin", "SELECT ops.host_metric_rollup()"),
    # the sampler: write its samples, nothing else
    ("mdhub_hostmon", "SELECT count(*) FROM admin.user_account"),
    ("mdhub_hostmon", "SELECT count(*) FROM admin.api_token"),
    ("mdhub_hostmon", "SELECT count(*) FROM admin.session"),
    ("mdhub_hostmon", "SELECT count(*) FROM ops.host_metric"),
    ("mdhub_hostmon", "SELECT count(*) FROM ops.job_run"),
    ("mdhub_hostmon", "SELECT count(*) FROM md.fixing"),
    ("mdhub_hostmon", "DELETE FROM ops.host_metric"),
    ("mdhub_hostmon", "UPDATE ops.host_metric_5m SET n = 0"),
    ("mdhub_hostmon", "INSERT INTO ops.job_request (job) VALUES ('x')"),
    ("mdhub_hostmon", "UPDATE ops.worker_heartbeat SET process = 'worker'"),
    # the read API: no market data writes, no token minting, no console secrets
    ("mdhub_api", "INSERT INTO md.fixing (index_code) VALUES ('X')"),
    ("mdhub_api", "UPDATE md.bond_eod SET close = 0"),
    ("mdhub_api", "DELETE FROM ref.bond"),
    ("mdhub_api", "INSERT INTO admin.api_token (name) VALUES ('x')"),
    ("mdhub_api", "UPDATE admin.api_token SET revoked_at = NULL"),
    ("mdhub_api", "UPDATE admin.api_token SET scopes = '{read}'"),
    ("mdhub_api", "SELECT count(*) FROM admin.user_account"),
    ("mdhub_api", "SELECT count(*) FROM admin.source_credential"),
    ("mdhub_api", "SELECT count(*) FROM ops.api_request"),
    ("mdhub_api", "DELETE FROM ops.api_request"),
    ("mdhub_api", "INSERT INTO ops.job_request (job) VALUES ('x')"),
    ("mdhub_api", "UPDATE ops.data_gap SET status = 'open'"),
]


@pytest.mark.parametrize(("role", "statement"), FORBIDDEN)
def test_forbidden_statements(db_url: str, role: str, statement: str) -> None:
    with (
        psycopg.connect(make_conninfo(db_url, user=role)) as c,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        c.execute(statement)


def test_hostmon_writes_samples_rollup_and_heartbeat(db_url: str, conn: Conn) -> None:
    hostmon = Database(make_conninfo(db_url, user="mdhub_hostmon"))
    write_samples(hostmon, [Sample("cpu.util_pct", {}, 12.5)])
    rollup(hostmon)
    beat = Heartbeat(hostmon, "hostmon")
    beat.beat()
    beat.beat(stopped=True)  # the upsert's UPDATE branch
    row = conn.execute(
        "SELECT process, stopped_at IS NOT NULL AS stopped FROM ops.worker_heartbeat"
    ).fetchone()
    assert row == {"process": "hostmon", "stopped": True}
    assert conn.execute("SELECT count(*) AS n FROM ops.host_metric").fetchone() == {"n": 1}


def _privileges(conn: Conn) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT r.rolname, c.oid::regclass::text AS rel, p.priv
        FROM pg_class c
        CROSS JOIN pg_roles r
        CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) p(priv)
        WHERE r.rolname LIKE 'mdhub\\_%' AND c.relkind IN ('r', 'v', 'p')
          AND c.relnamespace::regnamespace::text IN ('admin', 'ops', 'md', 'ref')
          AND has_table_privilege(r.oid, c.oid, p.priv)
        ORDER BY 1, 2, 3
        """
    ).fetchall()
    return [(r["rolname"], r["rel"], r["priv"]) for r in rows]


@pytest.mark.skipif(not PSQL.exists(), reason="pgserver psql not available")
def test_grants_survive_every_relock_and_drop_extras(db_url: str, conn: Conn) -> None:
    """migrate, grant_readonly.sql and admin migrations all call admin.restrict_access(): the
    service roles keep their exact grants, and anything granted by hand is taken back."""
    before = _privileges(conn)
    assert ("mdhub_api", "admin.api_token", "SELECT") in before
    assert ("mdhub_admin", "admin.audit_log", "INSERT") in before
    assert ("mdhub_admin", "admin.audit_log", "UPDATE") not in before
    assert [p for p in before if p[0] == "mdhub_hostmon"] == [
        ("mdhub_hostmon", "ops.host_metric", "INSERT"),
        ("mdhub_hostmon", "ops.worker_heartbeat", "INSERT"),
    ]  # column-level UPDATE on the heartbeat does not show as a table privilege
    conn.execute("GRANT INSERT ON md.fixing TO mdhub_api")
    conn.execute("GRANT ALL ON admin.user_account TO mdhub_hostmon")
    conn.execute("GRANT DELETE ON admin.audit_log TO mdhub_admin")
    conn.commit()
    migrate(db_url)  # nothing pending: re-locks and re-grants
    assert _privileges(conn) == before
    conn.execute("GRANT INSERT ON md.fixing TO mdhub_api")
    conn.commit()
    subprocess.run(
        [str(PSQL), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db_url, "-v", "ro_user=mdhub_api",
         "-f", str(DEPLOY / "sql" / "grant_readonly.sql")],
        check=True, capture_output=True,
    )  # fmt: skip
    # grant_readonly.sql given a service role still ends with exactly that role's grants
    assert _privileges(conn) == before


@pytest.mark.skipif(not PSQL.exists(), reason="pgserver psql not available")
def test_service_roles_sql_is_idempotent(db_url: str, conn: Conn) -> None:
    dbname = conn.execute("SELECT current_database() AS d").fetchone()
    assert dbname is not None

    def run(api: str, admin: str, hostmon: str) -> None:
        subprocess.run(
            [str(PSQL), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db_url,
             "-v", f"dbname={dbname['d']}", "-v", f"api_password={api}",
             "-v", f"admin_password={admin}", "-v", f"hostmon_password={hostmon}",
             "-f", str(DEPLOY / "sql" / "service_roles.sql")],
            check=True, capture_output=True,
        )  # fmt: skip

    before = _privileges(conn)
    run("api-pw-1", "admin-pw-1", "")
    run("", "", "")  # empty: leaves roles as they are
    rows = conn.execute(
        """
        SELECT rolname, rolcanlogin, rolpassword IS NOT NULL AS has_password,
               has_database_privilege(rolname, current_database(), 'CONNECT') AS can_connect
        FROM pg_authid WHERE rolname IN ('mdhub_api', 'mdhub_admin') ORDER BY rolname
        """
    ).fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("mdhub_admin", True, True, True),
        ("mdhub_api", True, True, True),
    ]
    assert _privileges(conn) == before
    conn.execute("ALTER ROLE mdhub_api PASSWORD NULL")
    conn.execute("ALTER ROLE mdhub_admin PASSWORD NULL")
    conn.commit()


def test_warn_if_owner(db_url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mdhub.db.pool"):
        assert warn_if_owner(db_url, "admin", "MDHUB_ADMIN_DB_PASSWORD")
        assert not warn_if_owner(
            make_conninfo(db_url, user="mdhub_admin"), "admin", "MDHUB_ADMIN_DB_PASSWORD"
        )
        assert not warn_if_owner(make_conninfo(db_url, dbname="missing"), "api", "X")
    assert [r.getMessage() for r in caplog.records] == [
        f"admin connects to PostgreSQL as {conninfo_to_dict(db_url)['user']}, the owner role: "
        "it can write every table. Set MDHUB_ADMIN_DB_PASSWORD in .env and create the service "
        "roles (README_DEPLOY §16)"
    ]


# ---------------------------------------------------------------------------- compose


def interpolate(text: str, env: dict[str, str]) -> str:
    """docker compose interpolation of ``${VAR}``, ``:-``, ``:+``, ``:?``, nested."""
    out, i = [], 0
    while i < len(text):
        if not text.startswith("${", i):
            out.append(text[i])
            i += 1
            continue
        depth, j = 1, i + 2
        while depth:
            if text.startswith("${", j):
                depth, j = depth + 1, j + 2
                continue
            depth -= text[j] == "}"
            j += 1
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:[-+?])(.*))?", text[i + 2 : j - 1], re.S)
        assert m, text[i:j]
        name, op, arg = m.groups()
        value = env.get(name, "")
        if op == ":-":
            value = value or interpolate(arg, env)
        elif op == ":+":
            value = interpolate(arg, env) if value else ""
        elif op == ":?":
            assert value, arg
        out.append(value)
        i = j
    return "".join(out)


def test_interpolate() -> None:
    env = {"A": "a", "E": ""}
    assert interpolate("${A}-${E:-d}-${E:+x}-${A:+y}-${E:-${A:-z}:${B:-b}}", env) == "a-d--y-a:b"


ROLE_PASSWORDS = {
    "api": "MDHUB_API_DB_PASSWORD",
    "admin": "MDHUB_ADMIN_DB_PASSWORD",
    "hostmon": "MDHUB_HOSTMON_DB_PASSWORD",
}


@pytest.mark.parametrize("roles_set", [True, False])
def test_compose_each_service_connects_as_its_own_role(roles_set: bool) -> None:
    yaml = pytest.importorskip("yaml")
    services: dict[str, Any] = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())[
        "services"
    ]
    env = {"POSTGRES_PASSWORD": "owner-pw"}
    if roles_set:
        env |= {var: f"{svc}-pw" for svc, var in ROLE_PASSWORDS.items()}
    got = {}
    for name, svc in services.items():
        url = svc.get("environment", {}).get("MDHUB_DATABASE_URL")
        if url:
            params = conninfo_to_dict(interpolate(url, env))
            got[name] = (params["user"], params["password"], params["dbname"])
    owner = ("mdhub", "owner-pw", "mdhub")
    expected = dict.fromkeys(("migrate", "worker", "updater", *ROLE_PASSWORDS), owner)
    if roles_set:
        expected |= {svc: (f"mdhub_{svc}", f"{svc}-pw", "mdhub") for svc in ROLE_PASSWORDS}
    assert got == expected


def test_compose_db_passwords_reach_only_postgres() -> None:
    """A service on its own role must not find the owner's or another role's password in its
    environment: the api reads .env (env_file), so it blanks them; admin and hostmon have none."""
    yaml = pytest.importorskip("yaml")
    services: dict[str, Any] = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())[
        "services"
    ]
    secrets = {"POSTGRES_PASSWORD", "MDHUB_RO_PASSWORD", *ROLE_PASSWORDS.values()}
    pg = services["postgres"]["environment"]
    assert all(pg[v] == f"${{{v}:-}}" for v in ROLE_PASSWORDS.values())
    api = services["api"]["environment"]
    assert services["api"]["env_file"] == ".env" and all(api[v] == "" for v in secrets)
    for name in ("admin", "hostmon"):
        assert "env_file" not in services[name]
        assert not secrets & set(services[name]["environment"]), name
