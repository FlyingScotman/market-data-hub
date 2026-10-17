"""Named read-API tokens (admin.api_token), MDHUB_API_TOKENS names, scopes, the request log
(ops.api_request) and `mdhub tokens` (ADMIN_DESIGN §3.3, §3.2)."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from typer.testing import CliRunner

from mdhub import __version__, cli
from mdhub.api.app import create_app
from mdhub.api.auth import (
    TokenError,
    TokenStore,
    TokenStoreUnavailable,
    create_token,
    generate_token,
    list_tokens,
    required_scopes,
    revoke_token,
    token_digest,
)
from mdhub.api.requestlog import (
    UNMATCHED_PATH,
    RequestLogWriter,
    RequestRecord,
    client_ip,
    redact_query,
)
from mdhub.config import Settings
from mdhub.db.pool import Database

Conn = psycopg.Connection[DictRow]
ENV_TOKENS = "tok-alpha-123,tdm=tok-named-456"


def _app(db_url: str, tokens: str | None = ENV_TOKENS) -> TestClient:
    return TestClient(create_app(Settings(database_url=db_url, api_tokens=tokens)))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _requests(conn: Conn) -> list[dict[str, Any]]:
    conn.rollback()
    return conn.execute("SELECT * FROM ops.api_request ORDER BY id").fetchall()


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    with _app(db_url) as c:
        yield c


# ---------------------------------------------------------------------------- tokens


def test_token_format_and_digest() -> None:
    token = generate_token()
    assert re.fullmatch(r"mdh_[a-z2-7]{52}", token)
    assert generate_token() != token
    assert len(token_digest(token)) == 32


def test_env_token_entries_named_and_numbered() -> None:
    s = Settings(api_tokens=" tok-a , tdm=tok-b,,abc==,Upper=x, inside-replica=mdh_zz ")
    assert s.api_token_entries() == [
        ("env:1", "tok-a"),
        ("tdm", "tok-b"),
        ("env:3", "abc=="),  # padded base64 is never split
        ("env:4", "Upper=x"),  # names are lower case
        ("inside-replica", "mdh_zz"),
    ]
    assert s.api_token_list() == ["tok-a", "tok-b", "abc==", "Upper=x", "mdh_zz"]
    assert {"tok-a", "tok-b", "abc==", "mdh_zz"} <= set(s.secret_values())


def test_required_scopes() -> None:
    assert required_scopes("/v1/coverage") == {"read"}
    assert required_scopes("/v1/snapshots") == {"read", "snapshots"}
    assert required_scopes("/v1/snapshots/x/manifest") == {"read", "snapshots"}
    assert required_scopes("/v1/snapshotsx") == {"read"}
    assert required_scopes("/v1/export/tdm") == {"read", "export"}
    assert required_scopes("/v1/openapi.json") == {"read", "snapshots", "export"}


def test_create_list_revoke(conn: Conn) -> None:
    token, row = create_token(conn, "tdm-backend", ["export", "read"], created_by="ops")
    conn.commit()
    assert row["prefix"] == token[:8] and row["scopes"] == ["read", "export"]
    stored = conn.execute("SELECT token_sha256 FROM admin.api_token").fetchone()
    assert stored is not None and bytes(stored["token_sha256"]) == token_digest(token)
    listed = list_tokens(conn)
    assert [r["name"] for r in listed] == ["tdm-backend"]
    assert token not in repr(listed) and "token_sha256" not in listed[0]
    with pytest.raises(TokenError, match="live token"):
        create_token(conn, "tdm-backend")
    for bad_name in ("env:1", "Upper", "", "x" * 65):
        with pytest.raises(TokenError):
            create_token(conn, bad_name)
    with pytest.raises(TokenError):
        create_token(conn, "x", ["write"])
    with pytest.raises(TokenError):
        create_token(conn, "x", [])
    with pytest.raises(TokenError, match="future"):
        create_token(conn, "x", expires_at=datetime.now(UTC) - timedelta(days=1))
    revoked = revoke_token(conn, "tdm-backend")
    assert [r["name"] for r in revoked] == ["tdm-backend"]
    assert revoke_token(conn, "tdm-backend") == []
    create_token(conn, "tdm-backend")  # the name is free again
    conn.commit()
    assert len(list_tokens(conn)) == 1 and len(list_tokens(conn, include_revoked=True)) == 2


def test_store_cache_revocation_and_expiry(conn: Conn, db: Database) -> None:
    now = [1000.0]
    store = TokenStore([("env:1", "tok-env-1")], db, clock=lambda: now[0])
    token, row = create_token(conn, "replica", ["snapshots"])
    conn.commit()
    unknown = generate_token()
    assert store.authenticate(unknown) is None  # miss is cached too
    identity = store.authenticate(token)
    assert identity is not None and identity.name == "replica" and identity.token_id == row["id"]
    assert identity.source == "db" and identity.scopes == {"snapshots"}
    env = store.authenticate("tok-env-1")
    assert env is not None and env.name == "env:1" and env.scopes == {"read", "snapshots", "export"}

    revoke_token(conn, "replica")
    conn.execute(  # the unknown token becomes valid after its miss was cached
        "INSERT INTO admin.api_token (name, token_sha256, prefix, scopes) "
        "VALUES ('late', %s, %s, '{read}')",
        (token_digest(unknown), unknown[:8]),
    )
    conn.commit()
    now[0] += 29
    assert store.authenticate(token) is not None  # cached for 30 s
    assert store.authenticate(unknown) is None  # cached miss
    now[0] += 2
    assert store.authenticate(token) is None  # revocation effective after 30 s
    late = store.authenticate(unknown)
    assert late is not None and late.name == "late"

    soon = datetime.now(UTC) + timedelta(hours=1)
    expiring, _ = create_token(conn, "expiring", expires_at=soon)
    conn.commit()
    assert store.authenticate(expiring) is not None
    assert store.authenticate(expiring, now=soon + timedelta(seconds=1)) is None


def test_store_database_down_is_unavailable(pg_server: Any) -> None:
    db = Database(pg_server.url("no_such_database"))
    store = TokenStore([("env:1", "tok-env-1")], db)
    assert store.authenticate("tok-env-1") is not None  # env tokens need no database
    with pytest.raises(TokenStoreUnavailable):
        store.authenticate(generate_token())


# ------------------------------------------------------------------------------- API


def test_db_token_scopes_and_identity(db_url: str, conn: Conn) -> None:
    full, _ = create_token(conn, "full")
    snap, _ = create_token(conn, "replica", ["snapshots"])
    export, _ = create_token(conn, "tdm", ["export"])
    conn.commit()
    with _app(db_url) as c:
        assert c.get("/v1/coverage", headers=_bearer(full)).status_code == 200
        r = c.get("/v1/coverage", headers=_bearer(snap))
        assert r.status_code == 403 and r.json()["error"]["message"] == "forbidden: token scope"
        assert c.get("/v1/openapi.json", headers=_bearer(snap)).status_code == 200
        assert c.get("/v1/coverage", headers=_bearer(export)).status_code == 403
        assert c.get("/v1/export/tdm", headers=_bearer(export)).status_code == 422  # no date
        assert c.get("/v1/coverage", headers=_bearer(full + "x")).status_code == 401
    rows = _requests(conn)
    names = [(r["token_name"], r["status"]) for r in rows]
    assert names == [
        ("full", 200),
        ("replica", 403),
        ("replica", 200),
        ("tdm", 403),
        ("tdm", 422),
        (None, 401),
    ]
    assert rows[0]["token_id"] is not None and rows[-1]["token_id"] is None
    used = conn.execute("SELECT name, last_used_at FROM admin.api_token ORDER BY id").fetchall()
    assert all(u["last_used_at"] is not None for u in used)


def test_env_tokens_unchanged_and_named(db_url: str, conn: Conn) -> None:
    with _app(db_url) as c:
        assert c.get("/v1/coverage", headers=_bearer("tok-alpha-123")).status_code == 200
        assert c.get("/v1/export/tdm", headers=_bearer("tok-named-456")).status_code == 422
        assert c.get("/v1/coverage", headers=_bearer("tdm=tok-named-456")).status_code == 401
    assert [r["token_name"] for r in _requests(conn)] == ["env:1", "tdm", None]


def test_no_env_tokens_db_tokens_still_work(db_url: str, conn: Conn) -> None:
    token, _ = create_token(conn, "only-db")
    conn.commit()
    with _app(db_url, tokens=None) as c:
        assert c.get("/v1/coverage", headers=_bearer(token)).status_code == 200
        assert c.get("/v1/coverage", headers=_bearer("tok-alpha-123")).status_code == 401


def test_request_log_rows(db_url: str, conn: Conn) -> None:
    token, _ = create_token(conn, "logger")
    conn.commit()
    h = {**_bearer(token), "User-Agent": "tdm/1.0", "X-Request-ID": "rid-1"}
    with _app(db_url) as client:  # leaving the block (lifespan exit) flushes the writer
        client.get("/health/live")
        client.get("/health/ready")
        client.get("/v1/bonds/RU000A0JX0J2?access_token=SECRET1&x=1", headers=h)
        client.get("/v1/curves/ruonia_ois?as_of=2026-10-16T12:00:00%2B03:00", headers=h)
        client.get("/no/such/path?apikey=SECRET2")
    rows = _requests(conn)
    assert [r["path"] for r in rows] == [
        "/v1/bonds/{secid}",
        "/v1/curves/{dataset}",
        UNMATCHED_PATH,
    ]
    first = rows[0]
    assert first["method"] == "GET" and first["status"] == 404 and first["token_name"] == "logger"
    assert first["query"] == "access_token=***&x=1"
    assert first["user_agent"] == "tdm/1.0" and first["request_id"] == "rid-1"
    assert first["duration_ms"] >= 0 and first["client_ip"] is None  # TestClient: "testclient"
    assert rows[1]["query"] == "as_of=2026-10-16T12:00:00%2B03:00"
    assert rows[1]["response_bytes"] and rows[1]["response_bytes"] > 0
    assert rows[2]["token_name"] is None and rows[2]["query"] == "apikey=***"
    dump = repr(rows)
    for secret in (token, "SECRET1", "SECRET2", "RU000A0JX0J2"):
        assert secret not in dump


def test_health_ready_reports_version(client: TestClient) -> None:
    body = client.get("/health/ready").json()
    assert body["version"] == __version__ and body["status"] == "ok"


def test_redact_query_and_client_ip() -> None:
    assert redact_query("") is None
    assert redact_query("date=2026-10-16&token=abc&Password=x&flag") == (
        "date=2026-10-16&token=***&Password=***&flag="
    )
    assert len(redact_query("q=" + "a" * 5000) or "") == 1000
    assert client_ip("10.1.2.3") == "10.1.2.3" and client_ip("::1") == "::1"
    assert client_ip("testclient") is None and client_ip(None) is None


def _record(i: int = 0, **kw: Any) -> RequestRecord:
    return RequestRecord(
        ts=datetime.now(UTC), method="GET", path=f"/v1/x{i}", status=200, duration_ms=1.0, **kw
    )


def _wait_rows(conn: Conn, n: int, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        conn.rollback()
        row = conn.execute("SELECT count(*) AS n FROM ops.api_request").fetchone()
        count = int(row["n"]) if row else 0
        if count >= n or time.monotonic() > deadline:
            return count
        time.sleep(0.05)


def test_writer_flushes_on_batch_size_and_interval(db: Database, conn: Conn) -> None:
    writer = RequestLogWriter(db, flush_interval_s=30.0, batch_size=3)
    writer.start()
    try:
        for i in range(3):
            writer.submit(_record(i, client_ip="10.0.0.1"))
        assert _wait_rows(conn, 3) == 3  # batch full: written long before the 30 s interval
        writer.submit(_record(9))
        time.sleep(0.2)
        assert _wait_rows(conn, 4, timeout=0.2) == 3  # waits for the interval or more rows
    finally:
        writer.stop()
    assert _wait_rows(conn, 4) == 4  # stop() flushes
    assert writer.written == 4 and writer.dropped == 0

    quick = RequestLogWriter(db, flush_interval_s=0.05, batch_size=200)
    quick.start()
    try:
        quick.submit(_record(10))
        assert _wait_rows(conn, 5, timeout=2.0) == 5
    finally:
        quick.stop()


def test_writer_never_raises(pg_server: Any, db: Database) -> None:
    full = RequestLogWriter(db, max_queue=2)  # not started: the queue fills up
    assert full.submit(_record()) and full.submit(_record())
    assert not full.submit(_record()) and full.dropped == 1
    broken = RequestLogWriter(Database(pg_server.url("no_such_database")), flush_interval_s=0.01)
    broken.start()
    broken.submit(_record())
    broken.stop()
    assert broken.written == 0 and broken.dropped == 1


# ------------------------------------------------------------------------------- CLI


def test_tokens_cli(db_url: str, conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    monkeypatch.setenv("MDHUB_API_TOKENS", "tdm=tok-named-456")
    runner = CliRunner()
    created = runner.invoke(
        cli.app, ["tokens", "create", "inside-replica", "-s", "snapshots", "--expires-days", "30"]
    )
    assert created.exit_code == 0, created.output
    token = created.stdout.strip()
    assert re.fullmatch(r"mdh_[a-z2-7]{52}", token)
    assert "shown only now" in created.stderr
    listed = runner.invoke(cli.app, ["tokens", "list"])
    assert listed.exit_code == 0, listed.output
    assert "inside-replica" in listed.stdout and token[:8] in listed.stdout
    assert token not in listed.stdout and "tok-named-456" not in listed.stdout
    assert "MDHUB_API_TOKENS: tdm" in listed.stdout
    dup = runner.invoke(cli.app, ["tokens", "create", "inside-replica"])
    assert dup.exit_code == 2 and "live token" in dup.stderr
    bad = runner.invoke(cli.app, ["tokens", "create", "x", "--expires", "tomorrow"])
    assert bad.exit_code == 2
    revoked = runner.invoke(cli.app, ["tokens", "revoke", "inside-replica"])
    assert revoked.exit_code == 0 and "revoked inside-replica" in revoked.stdout
    assert runner.invoke(cli.app, ["tokens", "revoke", "inside-replica"]).exit_code == 1
    assert "inside-replica" in runner.invoke(cli.app, ["tokens", "list", "--all"]).stdout
    row = conn.execute("SELECT expires_at, created_by FROM admin.api_token").fetchone()
    assert row is not None and row["expires_at"] > datetime.now(UTC) + timedelta(days=29)
