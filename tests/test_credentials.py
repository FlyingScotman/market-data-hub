"""Source credentials store (ADMIN_DESIGN §9): sealed boxes, worker resolution and reload,
redaction, the console page and `mdhub secrets`."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
import respx
from argon2 import PasswordHasher
from psycopg.rows import DictRow
from pydantic import SecretStr
from typer.testing import CliRunner

from admin_helpers import Console
from mdhub import cli, credentials
from mdhub.admin import security
from mdhub.config import Settings
from mdhub.core import redact
from mdhub.credentials import (
    CredentialError,
    CredentialStore,
    generate_private_key,
    key_id,
    open_sealed,
    parse_private_key,
    public_bytes,
    seal,
)
from mdhub.db.pool import Database
from mdhub.jobs import REGISTRY, JobContext, run_job
from test_admin import KEY_HEX, _audit, _client, _enrolled
from test_observability import _http_factory, _mock_sources

Conn = psycopg.Connection[DictRow]
KEY = generate_private_key()
PRIVATE = parse_private_key(KEY)
PUBLIC = public_bytes(PRIVATE)
KID = key_id(PUBLIC)


@pytest.fixture(autouse=True)
def clean_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redact, "_registered", frozenset())
    monkeypatch.setattr(credentials, "_STORE", None)
    monkeypatch.setattr(
        security, "HASHER", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    security._dummy_hash.cache_clear()


@pytest.fixture
def console(db_url: str) -> Iterator[Console]:
    with _client(db_url) as c:
        yield Console(c)


def _put(conn: Conn, name: str, value: str, public: bytes = PUBLIC) -> None:
    conn.execute(
        """
        INSERT INTO admin.source_credential (name, ciphertext, key_id, set_by)
        VALUES (%s, %s, %s, 'test')
        ON CONFLICT (name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext,
            key_id = EXCLUDED.key_id
        """,
        (name, seal(public, name, value), key_id(public)),
    )
    conn.commit()


# ------------------------------------------------------------------------------ sealed boxes


def test_seal_roundtrip_and_tampering() -> None:
    blob = seal(PUBLIC, "MOEX_API_KEY", "apim-secret-1")
    assert b"apim-secret-1" not in blob
    assert open_sealed(PRIVATE, "MOEX_API_KEY", KID, blob) == "apim-secret-1"
    assert seal(PUBLIC, "MOEX_API_KEY", "apim-secret-1") != blob  # ephemeral key, fresh nonce
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    for args in (
        ("MOEX_API_KEY", KID, tampered),
        ("CBONDS_PASSWORD", KID, blob),  # moved to another name
        ("MOEX_API_KEY", "0123456789abcdef", blob),  # another key id
        ("MOEX_API_KEY", KID, b"\x02" + blob[1:]),
        ("MOEX_API_KEY", KID, blob[:40]),
    ):
        with pytest.raises(CredentialError):
            open_sealed(PRIVATE, *args)
    other = parse_private_key(generate_private_key())
    with pytest.raises(CredentialError):
        open_sealed(other, "MOEX_API_KEY", key_id(public_bytes(other)), blob)
    assert parse_private_key(PRIVATE.private_bytes_raw().hex()).private_bytes_raw() == (
        PRIVATE.private_bytes_raw()
    )
    for bad in ("", "abc", "00" * 31):
        with pytest.raises(CredentialError):
            parse_private_key(bad)


def test_registry_matches_settings_and_migration() -> None:
    sql = (
        Path(credentials.__file__).parent / "db" / "migrations" / "0700_admin_source_credential.sql"
    ).read_text()
    for spec in credentials.REGISTRY.values():
        assert spec.field in Settings.model_fields
        alias = Settings.model_fields[spec.field].validation_alias
        assert spec.name == alias.choices[0]  # type: ignore[union-attr]
        assert f"'{spec.name}'" in sql


def test_admin_process_cannot_decrypt(conn: Conn) -> None:
    """The console holds no private key: its settings have none, compose gives it none
    (tests/test_deploy.py), its code never opens a box, and its own key opens nothing."""
    _put(conn, "MOEX_API_KEY", "apim-secret-1")
    row = conn.execute("SELECT ciphertext FROM admin.source_credential").fetchone()
    assert row is not None
    blob = bytes(row["ciphertext"])
    admin_dir = Path(credentials.__file__).parent / "admin"
    code = "".join(p.read_text() for p in admin_dir.rglob("*.py"))
    assert "open_sealed" not in code and "secrets_private_key" not in code
    as_key = parse_private_key(KEY_HEX)  # MDHUB_ADMIN_SECRET_KEY used as an X25519 key
    with pytest.raises(CredentialError):
        open_sealed(as_key, "MOEX_API_KEY", key_id(public_bytes(as_key)), blob)
    with pytest.raises(CredentialError):
        open_sealed(as_key, "MOEX_API_KEY", KID, blob)
    assert open_sealed(PRIVATE, "MOEX_API_KEY", KID, blob) == "apim-secret-1"


# ------------------------------------------------------------------------------ worker side


def test_database_value_wins_over_env_and_is_redacted(db: Database, conn: Conn) -> None:
    env = Settings(moex_api_key="env-apim-key", cbonds_login="env-cbonds-login")
    store = CredentialStore(db, PRIVATE)
    store.publish(credentials.env_names(env))
    key_row = conn.execute("SELECT key_id, env_names FROM admin.secret_key_public").fetchone()
    assert key_row == {"key_id": KID, "env_names": ["CBONDS_LOGIN", "MOEX_API_KEY"]}
    _put(conn, "MOEX_API_KEY", "db-apim-key")
    _put(conn, "OTC_MONITOR_TOKEN", "db-otc-token")
    other = public_bytes(parse_private_key(generate_private_key()))
    _put(conn, "CBONDS_LOGIN", "foreign-login", public=other)  # unknown key: never used
    store.refresh()
    resolved = store.resolve(env)
    assert resolved.moex_api_key is not None and resolved.otc_monitor_token is not None
    assert resolved.moex_api_key.get_secret_value() == "db-apim-key"
    assert resolved.otc_monitor_token.get_secret_value() == "db-otc-token"
    assert resolved.cbonds_login == SecretStr("env-cbonds-login")
    assert env.moex_api_key == SecretStr("env-apim-key")  # the base settings are untouched
    assert {"db-apim-key", "db-otc-token"} <= set(resolved.secret_values())
    # Redaction everywhere, also with filters and secret lists built before the decryption.
    assert redact.sanitize_text("auth db-apim-key / db-otc-token") == "auth *** / ***"
    flt = redact.SecretRedactingFilter([])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s", ("db-otc-token",), None)
    flt.filter(record)
    assert record.getMessage() == "token ***"
    assert "foreign-login" not in redact._registered


@pytest.fixture
def seen_key() -> Iterator[list[str | None]]:
    seen: list[str | None] = []

    def creds_test_job(ctx: JobContext) -> None:
        key = ctx.settings.moex_api_key
        seen.append(key.get_secret_value() if key else None)

    REGISTRY.register("creds_test_job", "test job", creds_test_job)
    try:
        yield seen
    finally:
        REGISTRY.unregister("creds_test_job")


def test_worker_picks_up_changes_without_restart(
    db: Database, conn: Conn, seen_key: list[str | None]
) -> None:
    settings = Settings(moex_api_key="env-apim-key", secrets_private_key=KEY)
    store = credentials.activate(settings, db, worker=True)
    assert store is not None
    try:
        run_job("creds_test_job", settings=settings, db=db)
        assert seen_key == ["env-apim-key"]  # .env fallback
        for value in ("console-v1", "console-v2", None):
            if value is None:
                conn.execute("DELETE FROM admin.source_credential")
                conn.commit()
            else:
                _put(conn, "MOEX_API_KEY", value)
            expected = value or "env-apim-key"
            deadline = time.monotonic() + 10
            while True:  # NOTIFY arrives within about a second (the poll would take 60 s)
                run_job("creds_test_job", settings=settings, db=db)
                if seen_key[-1] == expected or time.monotonic() > deadline:
                    break
                time.sleep(0.2)
            assert seen_key[-1] == expected
    finally:
        store.stop()


def test_source_probe_uses_the_store_and_records_the_verdict(db: Database, conn: Conn) -> None:
    settings = Settings(moex_api_key="env-apim-key", secrets_private_key=KEY)
    _put(conn, "MOEX_API_KEY", "db-apim-key")
    _put(conn, "RATESCALC_USERNAME", "db-rc-user")
    assert credentials.activate(settings, db, worker=False) is not None
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_sources(router)
        run_job(
            "source_probe",
            {"force": "true", "sources": "moex_apim,cbonds"},
            settings=settings,
            db=db,
            http_factory=_http_factory,
        )
    apim = routes["moex_apim"].calls.last.request
    assert apim.headers["authorization"] == "Bearer db-apim-key"  # the console value wins
    conn.rollback()
    rows = conn.execute(
        "SELECT name, verify_result, verified_at IS NOT NULL AS checked "
        "FROM admin.source_credential ORDER BY name"
    ).fetchall()
    assert rows == [
        {"name": "MOEX_API_KEY", "verify_result": "rejected", "checked": True},  # mocked 401
        {"name": "RATESCALC_USERNAME", "verify_result": None, "checked": False},  # not probed
    ]


def test_activate_without_or_with_bad_key(db: Database) -> None:
    assert credentials.activate(Settings(), db, worker=True) is None
    assert credentials.resolve(Settings(cbonds_login="x1234")).cbonds_login == SecretStr("x1234")
    with pytest.raises(credentials.ConfigError):
        credentials.activate(Settings(secrets_private_key="bad"), db, worker=False)


# ------------------------------------------------------------------------------ console


def _publish(db_url: str, names: tuple[str, ...] = ()) -> None:
    db = Database(db_url)
    try:
        CredentialStore(db, PRIVATE).publish(names)
    finally:
        db.close()


def test_console_sets_replaces_removes_write_only(
    console: Console,
    conn: Conn,
    db_url: str,
) -> None:
    _enrolled(console, conn, "alice")
    page = console.get("/credentials")
    assert page.status_code == 200 and "has not published its key" in page.text
    assert 'name="value"' not in page.text
    _publish(db_url, ("CBONDS_LOGIN", "MOEX_API_KEY"))
    page = console.get("/credentials")
    assert page.text.count('type="password" autocomplete="off"') == len(credentials.REGISTRY)
    assert ".env fallback" in page.text and "not set" in page.text
    assert not re.search(r'<input name="value"[^>]* value=', page.text)  # never pre-filled

    secret = "apim-very-secret-value"
    r = console.post("/credentials/MOEX_API_KEY", {"value": secret})
    assert r.status_code == 200 and "worker uses it" in r.text
    assert secret not in r.text and "both" in r.text and "the console value wins" in r.text
    conn.rollback()
    row = conn.execute("SELECT * FROM admin.source_credential").fetchone()
    assert row is not None and row["set_by"] == "alice" and row["key_id"] == KID
    assert open_sealed(PRIVATE, "MOEX_API_KEY", KID, bytes(row["ciphertext"])) == secret
    r = console.post("/credentials/MOEX_API_KEY", {"value": secret + "-2"})
    assert r.status_code == 200
    assert (
        console.post("/credentials/OTC_MONITOR_TOKEN", {"value": "otc-token-1"}).status_code == 200
    )
    set_, replaced = _audit(conn, "credential.set"), _audit(conn, "credential.replace")
    assert [a["target"] for a in set_] == [
        "credential:MOEX_API_KEY",
        "credential:OTC_MONITOR_TOKEN",
    ]
    assert [a["target"] for a in replaced] == ["credential:MOEX_API_KEY"]
    assert set_[0]["login"] == "alice" and set_[0]["details"] == {"key_id": KID}

    assert console.post("/credentials/NOT_A_CREDENTIAL", {"value": "x" * 8}).status_code == 404
    assert console.post("/credentials/CBONDS_LOGIN", {"value": "   "}).status_code == 400
    assert console.post("/credentials/CBONDS_LOGIN", {"value": "x" * 5000}).status_code == 422
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM admin.source_credential").fetchone() == {"n": 2}

    r = console.post("/credentials/MOEX_API_KEY/check")
    assert r.status_code == 200 and "check of the source is queued" in r.text
    conn.rollback()
    req = conn.execute("SELECT job, params, requested_login FROM ops.job_request").fetchone()
    assert req == {
        "job": "source_probe",
        "params": {"force": True, "sources": ["moex_apim"]},
        "requested_login": "alice",
    }
    assert console.post("/credentials/CBONDS_LOGIN/check").status_code == 409  # one open
    assert console.post("/credentials/MOEX_PASSPORT_LOGIN/check").status_code == 404  # no probe

    r = console.post("/credentials/MOEX_API_KEY/remove")
    assert r.status_code == 200 and "removed" in r.text
    assert console.post("/credentials/MOEX_API_KEY/remove").status_code == 404
    assert _audit(conn, "credential.remove")[0]["target"] == "credential:MOEX_API_KEY"

    # A row sealed to a key the worker no longer has is shown as "re-enter".
    _publish(db_url)  # same key, republished; now seal one row to another key
    _put(
        conn, "CBONDS_PASSWORD", "x", public=public_bytes(parse_private_key(generate_private_key()))
    )
    assert "re-enter" in console.get("/credentials").text

    trail = conn.execute("SELECT details::text AS d, target FROM admin.audit_log").fetchall()
    assert not [a for a in trail if secret in a["d"] or "otc-token-1" in a["d"]]


def test_console_credentials_need_admin_and_step_up(
    console: Console,
    conn: Conn,
    db_url: str,
) -> None:
    _publish(db_url)
    _enrolled(console, conn, "otto", "operator")
    assert console.get("/credentials").status_code == 403
    assert console.post("/credentials/MOEX_API_KEY", {"value": "op-value-1"}).status_code == 403
    conn.rollback()
    conn.execute("UPDATE admin.user_account SET role = 'admin'")
    conn.execute("UPDATE admin.session SET step_up_at = now() - interval '6 minutes'")
    conn.commit()
    page = console.get("/credentials")
    assert "confirm your identity" in page.text
    r = console.post("/credentials/MOEX_API_KEY", {"value": "admin-value-1"})
    assert r.url.path == "/step-up"
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM admin.source_credential").fetchone() == {"n": 0}


# ------------------------------------------------------------------------------ CLI


def test_cli_init_import_list_rotate(
    monkeypatch: pytest.MonkeyPatch, db_url: str, conn: Conn
) -> None:
    runner = CliRunner()
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    r = runner.invoke(cli.app, ["secrets", "init-key"])
    assert r.exit_code == 0, r.output
    line = r.stdout.strip().splitlines()[-1]
    assert line.startswith("MDHUB_SECRETS_PRIVATE_KEY=")
    key_text = line.split("=", 1)[1]
    private = parse_private_key(key_text)
    monkeypatch.setenv("MDHUB_SECRETS_PRIVATE_KEY", key_text)
    assert runner.invoke(cli.app, ["secrets", "init-key"]).exit_code == 1  # already set
    monkeypatch.setenv("MOEX_API_KEY", "env-apim-cli")
    monkeypatch.setenv("CBONDS_LOGIN", "env-cbonds-cli")
    r = runner.invoke(cli.app, ["secrets", "import-env"])
    assert r.exit_code == 0, r.output
    assert "imported MOEX_API_KEY" in r.stdout and "imported CBONDS_LOGIN" in r.stdout
    assert "env-apim-cli" not in r.output
    rows = {
        x["name"]: x
        for x in conn.execute("SELECT name, key_id, ciphertext FROM admin.source_credential")
    }
    kid = key_id(public_bytes(private))
    assert open_sealed(private, "MOEX_API_KEY", kid, bytes(rows["MOEX_API_KEY"]["ciphertext"])) == (
        "env-apim-cli"
    )
    assert runner.invoke(cli.app, ["secrets", "import-env"]).stdout == ""  # kept

    r = runner.invoke(cli.app, ["secrets", "list"])
    assert r.exit_code == 0 and "env-apim-cli" not in r.output
    listing = {ln.split()[0]: ln for ln in r.stdout.splitlines()}
    assert "both" in listing["MOEX_API_KEY"] and "not set" in listing["OTC_MONITOR_TOKEN"]

    r = runner.invoke(cli.app, ["secrets", "rotate-key", "--yes"])
    assert r.exit_code == 0, r.output
    new_text = r.stdout.strip().splitlines()[-1].split("=", 1)[1]
    new = parse_private_key(new_text)
    new_kid = key_id(public_bytes(new))
    conn.rollback()
    rows = {
        x["name"]: x
        for x in conn.execute("SELECT name, key_id, ciphertext FROM admin.source_credential")
    }
    assert {x["key_id"] for x in rows.values()} == {new_kid}
    assert open_sealed(new, "CBONDS_LOGIN", new_kid, bytes(rows["CBONDS_LOGIN"]["ciphertext"])) == (
        "env-cbonds-cli"
    )
    keys = conn.execute("SELECT key_id FROM admin.secret_key_public").fetchall()
    assert keys == [{"key_id": new_kid}]
    actions = [a["action"] for a in conn.execute("SELECT action FROM admin.audit_log ORDER BY id")]
    assert actions == ["credential.import_env", "credential.rotate_key"]
    trail = str(conn.execute("SELECT details::text FROM admin.audit_log").fetchall())
    assert "env-apim-cli" not in trail and key_text not in trail and new_text not in trail
    r = runner.invoke(cli.app, ["secrets", "list"])  # still the old key in this environment
    assert "re-enter" in r.stdout
