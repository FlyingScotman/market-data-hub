"""Admin console (ADMIN_DESIGN §4-5): allow-list, headers, enrollment, passkeys, TOTP, recovery
codes, sessions, CSRF, throttling and lockout, roles, step-up, audit trail, pages and the
`mdhub admin` CLI. Passkeys use a software authenticator (tests/admin_helpers.py)."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from psycopg.rows import DictRow
from typer.testing import CliRunner

from admin_helpers import CLIENT_IP, ORIGIN, RP_ID, Console, SoftAuthenticator
from mdhub import cli
from mdhub.admin import security, store
from mdhub.admin.app import SECURITY_HEADERS, create_app
from mdhub.admin.web import client_address, parse_networks
from mdhub.config import Settings
from mdhub.jobs import REGISTRY, JobContext

Conn = psycopg.Connection[DictRow]
PASSWORD = "correct horse battery staple"
KEY_HEX = "11" * 32


@pytest.fixture(autouse=True)
def fast_hasher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        security, "HASHER", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    security._dummy_hash.cache_clear()


def _settings(db_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": db_url,
        "admin_domain": RP_ID,
        "admin_allow_cidrs": "203.0.113.0/24, 198.51.100.7",
        "admin_trusted_proxies": "10.9.0.2",
        "admin_secret_key": KEY_HEX,
        **overrides,
    }
    return Settings(**values)


def _client(db_url: str, ip: str = CLIENT_IP, **overrides: Any) -> TestClient:
    return TestClient(
        create_app(_settings(db_url, **overrides)),
        base_url=ORIGIN,
        client=(ip, 50000),
        follow_redirects=True,
    )


@pytest.fixture
def console(db_url: str) -> Iterator[Console]:
    with _client(db_url) as c:
        yield Console(c)


def _user(conn: Conn, login: str, role: str = "admin") -> str:
    """Create a user; returns the enrollment link path."""
    user = store.create_user(conn, login, role, created_by="test")
    token = store.create_enrollment(conn, user.id, "invite", "test")
    conn.commit()
    return f"/enroll/{token}"


def _enrolled(
    console: Console, conn: Conn, login: str = "alice", role: str = "admin"
) -> SoftAuthenticator:
    auth = SoftAuthenticator()
    r = console.enroll(_user(conn, login, role), PASSWORD, auth)
    assert r.status_code == 200 and "Your account" in r.text
    return auth


def _audit(conn: Conn, action: str) -> list[dict[str, Any]]:
    conn.rollback()
    return conn.execute(
        "SELECT * FROM admin.audit_log WHERE action = %s ORDER BY id", (action,)
    ).fetchall()


def _fresh_console(db_url: str, ip: str = CLIENT_IP) -> Console:
    return Console(_client(db_url, ip).__enter__())


# --------------------------------------------------------------------------- primitives


def test_totp_rfc6238_vector_and_replay_guard() -> None:
    secret = b"12345678901234567890"
    assert security.totp_code(secret, 59 // 30) == "287082"  # RFC 6238 T=59 (8 digits 94287082)
    now = 1_111_111_109.0
    code = security.totp_code(secret, int(now // 30))
    step = security.totp_match(secret, code, last_step=None, now=now)
    assert step == int(now // 30)
    assert security.totp_match(secret, code, last_step=step, now=now) is None  # replay
    prev = security.totp_code(secret, int(now // 30) - 1)
    assert security.totp_match(secret, prev, last_step=None, now=now) is not None  # -1 step
    old = security.totp_code(secret, int(now // 30) - 2)
    assert security.totp_match(secret, old, last_step=None, now=now) is None
    assert security.totp_match(secret, "12345", last_step=None, now=now) is None


def test_secret_encryption_and_key_parsing() -> None:
    key = security.parse_secret_key(KEY_HEX)
    assert security.parse_secret_key("ERERERERERERERERERERERERERERERERERERERERERE=") == key
    blob = security.encrypt_secret(key, b"secret", aad=b"user:1")
    assert security.decrypt_secret(key, blob, aad=b"user:1") == b"secret"
    with pytest.raises(security.SecretKeyError):
        security.decrypt_secret(key, blob, aad=b"user:2")
    with pytest.raises(security.SecretKeyError):
        security.parse_secret_key("too-short")
    with pytest.raises(security.SecretKeyError):
        security.parse_secret_key(None)


def test_password_policy() -> None:
    assert security.password_problem("short") is not None
    assert security.password_problem("x" * 129) is not None
    assert security.password_problem("qwertyuiop123") is not None  # common list (12+ chars)
    assert security.password_problem("QWERTYUIOP123") is not None  # case-insensitive
    assert security.password_problem("alice-is-my-name-ok", login="alice") is not None
    assert security.password_problem(PASSWORD) is None
    assert security.password_problem("ünïcödé passphrase 1") is None  # no composition rules


def test_client_address_trusts_forwarded_for_only_from_proxy() -> None:
    proxies = parse_networks("10.9.0.2, 127.0.0.1")
    assert client_address("10.9.0.2", "203.0.113.5", proxies) == "203.0.113.5"
    # A spoofed left-most entry is ignored: the right-most non-proxy hop counts.
    assert client_address("10.9.0.2", "198.51.100.7, 203.0.113.5", proxies) == "203.0.113.5"
    assert client_address("10.9.0.2", "203.0.113.5, 127.0.0.1", proxies) == "203.0.113.5"
    assert client_address("192.0.2.9", "203.0.113.5", proxies) == "192.0.2.9"
    assert client_address("10.9.0.2", None, proxies) == "10.9.0.2"
    assert client_address("10.9.0.2", "garbage", proxies) is None


# ------------------------------------------------------------------- allow-list, headers


def test_allow_list_enforced_and_fails_closed(db_url: str) -> None:
    with _client(db_url) as c:
        assert c.get("/login").status_code == 200
    with _client(db_url, ip="192.0.2.50") as c:
        r = c.get("/login")
        assert r.status_code == 403 and r.text == "Forbidden"
        assert r.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    with _client(db_url, admin_allow_cidrs=None) as c:
        assert c.get("/login").status_code == 403
    with _client(db_url, admin_domain=None) as c:
        assert c.get("/login").status_code == 503
    # X-Forwarded-For counts only from the trusted proxy.
    with _client(db_url, ip="10.9.0.2") as c:
        assert c.get("/login", headers={"X-Forwarded-For": CLIENT_IP}).status_code == 200
        assert c.get("/login", headers={"X-Forwarded-For": "192.0.2.50"}).status_code == 403
        assert c.get("/login").status_code == 403
    with _client(db_url, ip="192.0.2.50") as c:
        assert c.get("/login", headers={"X-Forwarded-For": CLIENT_IP}).status_code == 403
    with _client(db_url, ip="127.0.0.1", admin_allow_cidrs=None) as c:
        assert c.get("/health/live").text == "ok"  # container health check only
        assert c.get("/login").status_code == 403


def test_security_headers_and_no_inline_script(console: Console) -> None:
    r = console.get("/login")
    for name, value in SECURITY_HEADERS.items():
        assert r.headers[name] == value
    assert r.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"]
    scripts = re.findall(r"<script[^>]*>", r.text)
    assert scripts and all('src="/static/' in s and "integrity=" in s for s in scripts)
    assert not re.search(r"<style|\sstyle=|\son[a-z]+=", r.text)  # no inline styles / handlers
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-mdhub_admin=")
    for flag in ("HttpOnly", "Path=/", "SameSite=strict", "Secure"):
        assert flag.lower() in cookie.lower()
    assert "Domain=" not in cookie
    src = re.search(r'src="(/static/[^"]+/htmx[^"]+)"', r.text)
    assert src is not None
    asset = console.client.get(src.group(1))
    assert asset.status_code == 200 and "immutable" in asset.headers["Cache-Control"]
    assert console.client.get("/static/0000000000000000/admin.js").status_code == 404


# ---------------------------------------------------------------- enrollment and login


def test_enroll_with_passkey_then_login(console: Console, conn: Conn) -> None:
    link = _user(conn, "alice")
    auth = SoftAuthenticator()
    r = console.get(link)
    assert "Step 1 of 2" in r.text and link not in str(r.url)  # token dropped from the URL
    r = console.post("/enroll/password", {"password": "short", "password2": "short"})
    assert r.status_code == 400 and "at least 12" in r.text
    r = console.post(
        "/enroll/password", {"password": "qwertyuiop123", "password2": "qwertyuiop123"}
    )
    assert r.status_code == 400 and "too common" in r.text
    # Registering before the password is refused.
    assert console.post_json("/webauthn/register/options").status_code == 400
    r = console.post("/enroll/password", {"password": PASSWORD, "password2": PASSWORD})
    assert "Step 2 of 2" in r.text
    options = console.post_json("/webauthn/register/options").json()
    assert options["rp"]["id"] == RP_ID
    assert options["authenticatorSelection"]["userVerification"] == "required"
    r = console.post_json(
        "/webauthn/register/verify", {"credential": auth.create(options), "name": "MacBook"}
    )
    assert r.status_code == 200 and r.json()["redirect"].startswith("/account")
    page = console.get("/account?enrolled=1")
    assert "MacBook" in page.text and "Log out" in page.text
    # The link works once.
    assert Console(console.client).get(link).status_code == 404
    assert {a["action"] for a in conn.execute("SELECT action FROM admin.audit_log").fetchall()} >= {
        "password.set",
        "passkey.add",
        "user.enrolled",
        "login.success",
    }
    # Log out, log in with password + passkey.
    console.post("/logout")
    assert console.get("/").url.path == "/login"
    console.login("alice", PASSWORD, auth)
    assert console.get("/").status_code == 200
    row = conn.execute("SELECT sign_count, last_used_at FROM admin.webauthn_credential").fetchone()
    assert row is not None and row["sign_count"] == 1 and row["last_used_at"] is not None


def test_enrollment_link_expires(console: Console, conn: Conn) -> None:
    link = _user(conn, "bob")
    conn.execute("UPDATE admin.enrollment SET expires_at = now() - interval '1 second'")
    conn.commit()
    assert console.get(link).status_code == 404
    assert console.get("/enroll/not-a-token").status_code == 404
    # A newer link invalidates the older one.
    user = store.user_by_login(conn, "bob")
    assert user is not None
    first = store.create_enrollment(conn, user.id, "invite", "t")
    second = store.create_enrollment(conn, user.id, "invite", "t")
    conn.commit()
    assert console.get(f"/enroll/{first}").status_code == 404
    assert console.get(f"/enroll/{second}").status_code == 200


def test_login_failures_are_enumeration_safe(console: Console, conn: Conn, db_url: str) -> None:
    _enrolled(console, conn, "alice")
    store.create_user(conn, "carol", "viewer")  # exists, never enrolled (no password)
    conn.execute("INSERT INTO admin.login_attempt (login, outcome) VALUES ('x', 'success')")
    conn.commit()
    disabled = _fresh_console(db_url)
    _enrolled(disabled, conn, "dave", "viewer")
    conn.execute("UPDATE admin.user_account SET disabled_at = now() WHERE login = 'dave'")
    conn.commit()

    def attempt(login: str, password: str) -> tuple[int, str]:
        c = _fresh_console(db_url, ip=f"203.0.113.{10 + len(login)}")
        r = c.password_step(login, password)
        body = re.sub(r'value="[^"]*"', "", r.text)
        return r.status_code, re.sub(r'data-csrf="[^"]*"|"X-CSRF-Token": "[^"]*"', "", body)

    results = {
        attempt("alice", "wrong password 123"),
        attempt("nobody", "wrong password 123"),
        attempt("carol", "wrong password 123"),
        attempt("dave", PASSWORD),
    }
    assert len(results) == 1
    status, body = results.pop()
    assert status == 401 and "Invalid login or password." in body


def test_lockout_after_ten_failures_and_unlock(console: Console, conn: Conn, db_url: str) -> None:
    auth = _enrolled(console, conn, "alice")
    console.post("/logout")
    # 9 failures from various IPs after the enrollment login, old enough that the exponential
    # delay has passed.
    conn.execute("UPDATE admin.login_attempt SET ts = now() - interval '10 minutes'")
    for i in range(9):
        conn.execute(
            "INSERT INTO admin.login_attempt (ts, login, ip, outcome) "
            "VALUES (now() - interval '3 minutes', 'alice', %s, 'failure')",
            (f"198.51.100.{i + 20}",),
        )
    conn.commit()
    r = console.password_step("alice", "wrong password 123")
    assert r.status_code == 401
    assert len(_audit(conn, "login.lockout")) == 1
    r = console.password_step("alice", PASSWORD)  # right password, but locked
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 800
    # The same answer for a login that does not exist after 10 failures.
    for _ in range(10):
        conn.execute(
            "INSERT INTO admin.login_attempt (ts, login, outcome) "
            "VALUES (now() - interval '3 minutes', 'ghost', 'failure')"
        )
    conn.commit()
    other = _fresh_console(db_url, ip="203.0.113.77")
    assert other.password_step("ghost", "whatever pass 1").status_code == 429
    result = CliRunner().invoke(
        cli.app, ["admin", "unlock", "alice"], env={"MDHUB_DATABASE_URL": db_url}
    )
    assert result.exit_code == 0, result.output
    console.login("alice", PASSWORD, auth)
    assert console.get("/").status_code == 200


def test_exponential_delay_per_login_and_ip(console: Console, conn: Conn) -> None:
    for _ in range(3):
        conn.execute(
            "INSERT INTO admin.login_attempt (login, ip, outcome) VALUES ('eve', NULL, 'failure')"
        )
    conn.commit()
    r = console.password_step("eve", "anything at all")
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 2
    for i in range(6):
        conn.execute(
            "INSERT INTO admin.login_attempt (login, ip, outcome) VALUES (%s, %s, 'failure')",
            (f"user{i}", CLIENT_IP),
        )
    conn.commit()
    r = console.password_step("fresh-login", "anything at all")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert conn.execute(
        "SELECT count(*) AS n FROM admin.login_attempt WHERE outcome = 'throttled'"
    ).fetchone() == {"n": 2}


# ----------------------------------------------------------------------------- sessions


def test_session_rotation_idle_and_absolute_expiry(console: Console, conn: Conn) -> None:
    console.get("/login")
    anonymous = console.cookie
    auth = SoftAuthenticator()
    console.enroll(_user(conn, "alice"), PASSWORD, auth)
    console.post("/logout")
    console.get("/login")
    before = console.cookie
    console.post("/login", {"login": "alice", "password": PASSWORD})
    after_password = console.cookie
    r = console.passkey(auth)
    full = console.cookie
    assert r.status_code == 200
    assert len({anonymous, before, after_password, full}) == 4
    revoked = conn.execute(
        "SELECT count(*) AS n FROM admin.session WHERE id_sha256 = %s AND revoked_at IS NOT NULL",
        (security.sha256(after_password or ""),),
    ).fetchone()
    assert revoked == {"n": 1}
    stored = conn.execute("SELECT id_sha256 FROM admin.session").fetchall()
    assert all(bytes(s["id_sha256"]) != (full or "").encode() for s in stored)  # only hashes
    assert console.get("/").url.path == "/"
    # Idle 30 minutes.
    conn.execute(
        "UPDATE admin.session SET last_seen_at = now() - interval '31 minutes' "
        "WHERE id_sha256 = %s",
        (security.sha256(full or ""),),
    )
    conn.commit()
    assert console.get("/").url.path == "/login"
    # Absolute 12 hours.
    console.login("alice", PASSWORD, auth)
    conn.execute(
        "UPDATE admin.session SET expires_at = now() - interval '1 second' WHERE id_sha256 = %s",
        (security.sha256(console.cookie or ""),),
    )
    conn.commit()
    assert console.get("/jobs").url.path == "/login"
    # The old full cookie no longer works after logout either.
    console.login("alice", PASSWORD, auth)
    old = console.cookie
    console.post("/logout")
    console.client.cookies.set("__Host-mdhub_admin", old or "", domain="admin.test")
    assert console.get("/").url.path == "/login"


def test_password_change_revokes_other_sessions(console: Console, conn: Conn, db_url: str) -> None:
    auth = _enrolled(console, conn, "alice")
    other = _fresh_console(db_url)
    other.login("alice", PASSWORD, auth)
    assert other.get("/").url.path == "/"
    console.get("/account")
    new = "a different long passphrase"
    r = console.post("/account/password", {"current": "wrong", "password": new, "password2": new})
    assert r.status_code == 400
    r = console.post("/account/password", {"current": PASSWORD, "password": new, "password2": new})
    assert "Password changed" in r.text
    assert other.get("/").url.path == "/login"
    assert console.get("/").url.path == "/"


# --------------------------------------------------------------------------------- CSRF


def test_csrf_token_and_origin_required(console: Console, conn: Conn) -> None:
    _enrolled(console, conn, "alice")
    console.get("/jobs")
    token = console.csrf
    ok = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    post = console.client.post
    assert post("/logout", data={}, headers=ok).status_code == 403  # no token
    assert post("/logout", data={"csrf_token": "x" * 43}, headers=ok).status_code == 403
    assert (
        post(
            "/logout", data={"csrf_token": token}, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        post(
            "/logout", data={"csrf_token": token}, headers={"Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    assert post("/logout", data={"csrf_token": token}).status_code == 403  # neither header
    assert (
        post("/logout", data={"csrf_token": token}, headers={"Origin": "null"}).status_code == 403
    )
    assert console.get("/").url.path == "/"  # still logged in
    # The login form is protected too (login CSRF).
    fresh = Console(TestClient(console.client.app, base_url=ORIGIN, client=(CLIENT_IP, 1)))
    fresh.get("/login")
    r = fresh.client.post("/login", data={"login": "alice", "password": PASSWORD}, headers=ok)
    assert r.status_code == 403
    # JSON endpoints take the header.
    r = console.client.post("/webauthn/login/options", json={}, headers=ok)
    assert r.status_code == 403
    # Form posts under Referrer-Policy: no-referrer carry "Origin: null" (Chromium).
    r = post(
        "/logout",
        data={"csrf_token": token},
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200 and r.url.path == "/login"


# ------------------------------------------------------------------- roles and step-up


@pytest.fixture
def fake_job() -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def admin_test_job(ctx: JobContext) -> dict[str, Any]:
        calls.append({"job": ctx.job})
        return {}

    REGISTRY.register("admin_test_job", "test job", admin_test_job)
    try:
        yield calls
    finally:
        REGISTRY.unregister("admin_test_job")


def test_roles_viewer_operator_admin(
    console: Console, conn: Conn, db_url: str, fake_job: list[dict[str, Any]]
) -> None:
    _enrolled(console, conn, "viv", "viewer")
    for path in ("/", "/jobs", "/data", "/sources", "/traffic", "/updates", "/account"):
        assert console.get(path).status_code == 200, path
    for path in ("/tokens", "/users", "/audit"):
        assert console.get(path).status_code == 403, path
    console.get("/jobs")
    r = console.post("/jobs/admin_test_job/run")
    assert r.status_code == 403
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM ops.job_request").fetchone() == {"n": 0}
    denied = _audit(conn, "access.denied")
    assert denied and denied[-1]["outcome"] == "denied" and denied[-1]["login"] == "viv"
    assert '/run"' not in console.get("/jobs").text  # no run buttons for viewers

    op = _fresh_console(db_url)
    _enrolled(op, conn, "otto", "operator")
    assert op.get("/tokens").status_code == 403
    op.get("/jobs")
    r = op.post("/jobs/admin_test_job/run")
    assert r.status_code == 200 and "The run is queued" in r.text
    time.sleep(0.3)
    assert fake_job == []  # the console never runs a job itself
    conn.rollback()
    req = conn.execute(
        "SELECT q.id, q.state, q.requested_login, q.requested_by, u.login FROM ops.job_request q "
        "JOIN admin.user_account u ON u.id = q.requested_by"
    ).fetchone()
    assert req is not None and req["state"] == "queued"
    assert req["requested_login"] == "otto" == req["login"]
    assert conn.execute("SELECT count(*) AS n FROM ops.job_run").fetchone() == {"n": 0}
    audit = _audit(conn, "job.request")[-1]
    assert audit["target"] == "job:admin_test_job" and audit["details"]["request_id"] == req["id"]
    assert "Requested runs" in r.text and "Cancel" in r.text
    assert op.post("/jobs/admin_test_job/run").status_code == 409  # one open request per job
    assert console.post(f"/jobs/requests/{req['id']}/cancel").status_code == 403  # viewer
    r = op.post(f"/jobs/requests/{req['id']}/cancel")
    assert "cancelled" in r.text
    conn.rollback()
    assert conn.execute("SELECT state FROM ops.job_request").fetchone() == {"state": "cancelled"}
    assert op.post(f"/jobs/requests/{req['id']}/cancel").status_code == 404
    assert op.post("/jobs/no_such_job/run").status_code == 404


def test_operator_retries_task_and_marks_gap(console: Console, conn: Conn) -> None:
    _enrolled(console, conn, "otto", "operator")
    task = conn.execute(
        """
        INSERT INTO ops.backfill_task (dataset, params, status, attempts, last_error, done_at)
        VALUES ('bonds_eod', '{"date_from": "2026-01-05", "date_to": "2026-01-05"}', 'failed',
                5, 'boom', now()) RETURNING id
        """
    ).fetchone()
    assert task is not None
    conn.execute(
        "INSERT INTO ops.data_gap (dataset, key, gap_date, status) "
        "VALUES ('bonds_eod', '{}', '2026-01-05', 'unfillable')"
    )
    conn.commit()
    page = console.get("/data")
    assert "boom" in page.text and "Retry" in page.text
    r = console.post(f"/data/tasks/{task['id']}/retry")
    assert "queued again" in r.text
    conn.rollback()
    row = conn.execute(
        "SELECT status, attempts FROM ops.backfill_task WHERE id = %s", (task["id"],)
    ).fetchone()
    assert row == {"status": "pending", "attempts": 0}
    assert console.post(f"/data/tasks/{task['id']}/retry").status_code == 404
    r = console.post(
        "/data/gaps/not-expected",
        {"dataset": "bonds_eod", "key": "{}", "gap_date": "2026-01-05", "reason": "holiday"},
    )
    assert "marked not expected" in r.text
    conn.rollback()
    gap = conn.execute("SELECT status, note FROM ops.data_gap").fetchone()
    assert gap is not None and gap["status"] == "not_expected" and "holiday" in gap["note"]
    assert _audit(conn, "gap.not_expected") and _audit(conn, "backfill.retry")


def test_tokens_need_admin_step_up_and_show_token_once(console: Console, conn: Conn) -> None:
    auth = _enrolled(console, conn, "alice")
    # The enrollment login counts as a step-up; age it.
    conn.execute("UPDATE admin.session SET step_up_at = now() - interval '6 minutes'")
    conn.commit()
    page = console.get("/tokens")
    assert "confirm your identity" in page.text
    r = console.post("/tokens", {"name": "tdm-backend", "scope": ["read", "export"]})
    assert r.url.path == "/step-up" and "Confirm your identity" in r.text
    assert conn.execute("SELECT count(*) AS n FROM admin.api_token").fetchone() == {"n": 0}
    r = console.passkey(auth)
    assert r.status_code == 200 and r.json()["redirect"] == "/"
    assert _audit(conn, "step_up")
    console.get("/tokens")  # the step-up rotated the session (and its CSRF token)
    r = console.post(
        "/tokens", {"name": "tdm-backend", "scope": ["read", "export"], "expires_days": "30"}
    )
    token = re.search(r"mdh_[a-z2-7]{52}", r.text)
    assert r.status_code == 200 and token is not None
    assert token.group(0) not in console.get("/tokens").text  # shown once only
    conn.rollback()
    row = conn.execute(
        "SELECT id, name, scopes, expires_at, created_by FROM admin.api_token"
    ).fetchone()
    assert row is not None and row["scopes"] == ["read", "export"] and row["created_by"] == "alice"
    created = _audit(conn, "token.create")[-1]
    assert token.group(0) not in str(created["details"])
    assert console.post(f"/tokens/{row['id']}/revoke").status_code == 200
    conn.rollback()
    revoked = conn.execute("SELECT revoked_at FROM admin.api_token").fetchone()
    assert revoked is not None and revoked["revoked_at"] is not None
    r = console.post("/tokens", {"name": "BAD NAME"})
    assert r.status_code == 400


def test_users_page_invite_role_disable(console: Console, conn: Conn, db_url: str) -> None:
    _enrolled(console, conn, "alice")
    console.get("/users")
    r = console.post("/users", {"login": "Bob", "role": "operator", "display_name": "Bob B"})
    link = re.search(r"https://admin\.test(/enroll/[A-Za-z0-9_-]+)", r.text)
    assert link is not None
    bob = _fresh_console(db_url)
    auth = SoftAuthenticator()
    bob.enroll(link.group(1), PASSWORD, auth)
    assert bob.get("/jobs").status_code == 200
    bob_id = conn.execute("SELECT id FROM admin.user_account WHERE login = 'bob'").fetchone()
    assert bob_id is not None
    console.get("/users")
    console.post(f"/users/{bob_id['id']}/role", {"role": "viewer"})
    assert bob.get("/jobs").url.path == "/login"  # privilege change ends sessions
    bob.login("bob", PASSWORD, auth)
    console.post(f"/users/{bob_id['id']}/disable")
    assert bob.get("/").url.path == "/login"
    assert bob.password_step("bob", PASSWORD).status_code == 401
    me = conn.execute("SELECT id FROM admin.user_account WHERE login = 'alice'").fetchone()
    assert me is not None
    assert console.post(f"/users/{me['id']}/disable").status_code == 400  # not yourself
    actions = {a["action"] for a in conn.execute("SELECT action FROM admin.audit_log").fetchall()}
    assert {"user.create", "user.role", "user.disable"} <= actions
    assert console.get("/audit").status_code == 200


# ------------------------------------------------------------------- TOTP and recovery


def _totp_now(conn: Conn, login: str) -> str:
    row = conn.execute(
        "SELECT t.secret_enc, u.id FROM admin.totp_secret t JOIN admin.user_account u "
        "ON u.id = t.user_id WHERE u.login = %s",
        (login,),
    ).fetchone()
    assert row is not None
    secret = security.decrypt_secret(
        security.parse_secret_key(KEY_HEX),
        bytes(row["secret_enc"]),
        aad=f"mdhub-totp:{row['id']}".encode(),
    )
    return security.totp_code(secret, int(time.time() // 30))


def test_totp_setup_login_and_replay(console: Console, conn: Conn, db_url: str) -> None:
    _enrolled(console, conn, "alice")
    console.get("/account")
    r = console.post("/account/totp/begin")
    assert "otpauth://totp/" in r.text
    conn.rollback()
    secret_row = conn.execute("SELECT secret_enc FROM admin.totp_secret").fetchone()
    assert secret_row is not None
    match = re.search(r"Secret: <code[^>]*>([A-Z2-7]+)<", r.text)
    assert match is not None
    assert match.group(1).encode() not in bytes(secret_row["secret_enc"])  # encrypted at rest
    assert console.post("/account/totp/confirm", {"code": "000000"}).status_code == 400
    code = _totp_now(conn, "alice")
    console.post("/account/totp/confirm", {"code": code})
    assert "An authenticator app is set up" in console.get("/account").text
    console.post("/logout")
    # The step used to confirm cannot log in again (replay guard): wait for a fresh code.
    c = _fresh_console(db_url)
    c.password_step("alice", PASSWORD)
    assert c.post("/login/totp", {"code": code}).status_code == 401
    conn.execute("UPDATE admin.totp_secret SET last_step = last_step - 1")
    conn.commit()
    r = c.post("/login/totp", {"code": code})
    assert r.status_code == 200 and r.url.path == "/"
    c.post("/logout")
    c.password_step("alice", PASSWORD)
    assert c.post("/login/totp", {"code": code}).status_code == 401  # same step again


def test_recovery_codes_work_once(console: Console, conn: Conn, db_url: str) -> None:
    _enrolled(console, conn, "alice")
    console.get("/account")
    r = console.post("/account/recovery-codes")
    codes = re.findall(r"\b[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}\b", r.text)
    assert len(codes) == 10
    conn.rollback()
    stored = conn.execute("SELECT code_hash FROM admin.recovery_code").fetchall()
    assert len(stored) == 10 and all(s["code_hash"].startswith("$argon2id$") for s in stored)
    c = _fresh_console(db_url)
    c.password_step("alice", PASSWORD)
    r = c.post("/login/recovery", {"code": codes[0].upper().replace("-", " ")})
    assert r.status_code == 200 and r.url.path == "/"
    c.post("/logout")
    c.password_step("alice", PASSWORD)
    assert c.post("/login/recovery", {"code": codes[0]}).status_code == 401
    assert _audit(conn, "recovery.used")


# ----------------------------------------------------------------------- passkey rules


def test_passkey_sign_counter_regression_marks_suspect(
    console: Console, conn: Conn, db_url: str
) -> None:
    auth = _enrolled(console, conn, "alice")
    console.post("/logout")
    console.login("alice", PASSWORD, auth)  # counter 1
    console.post("/logout")
    c = _fresh_console(db_url)
    c.password_step("alice", PASSWORD)
    r = c.passkey(auth, advance=0)  # cloned authenticator replays counter 1
    assert r.status_code == 401
    conn.rollback()
    row = conn.execute("SELECT suspect_at FROM admin.webauthn_credential").fetchone()
    assert row is not None and row["suspect_at"] is not None
    assert _audit(conn, "passkey.suspect")[-1]["outcome"] == "denied"
    c.get("/login/mfa")
    assert c.passkey(auth).status_code in (400, 401)  # blocked from now on


def test_passkey_wrong_origin_or_other_user_refused(
    console: Console, conn: Conn, db_url: str
) -> None:
    auth = _enrolled(console, conn, "alice")
    other = _fresh_console(db_url)
    mallory = SoftAuthenticator()
    other.enroll(_user(conn, "mallory", "viewer"), PASSWORD, mallory)
    other.post("/logout")
    c = _fresh_console(db_url)
    c.password_step("alice", PASSWORD)
    options = c.post_json("/webauthn/login/options").json()
    bad = auth.get(options, origin="https://admin.evil.test")
    assert c.post_json("/webauthn/login/verify", {"credential": bad}).status_code == 401
    # Mallory's valid passkey cannot complete Alice's login.
    options = c.post_json("/webauthn/login/options").json()
    assert (
        c.post_json("/webauthn/login/verify", {"credential": mallory.get(options)}).status_code
        == 401
    )
    # A challenge is used once: a replayed assertion fails even within the same session.
    conn.execute("DELETE FROM admin.login_attempt")  # stay below the throttling delay
    conn.commit()
    options = c.post_json("/webauthn/login/options").json()
    cred = auth.get(options)
    assert c.post_json("/webauthn/login/verify", {"credential": bad}).status_code == 401
    assert c.post_json("/webauthn/login/verify", {"credential": cred}).status_code == 401
    options = c.post_json("/webauthn/login/options").json()
    assert (
        c.post_json("/webauthn/login/verify", {"credential": auth.get(options)}).status_code == 200
    )


def test_passkey_only_login_needs_opt_in(console: Console, conn: Conn, db_url: str) -> None:
    auth = _enrolled(console, conn, "alice")
    c = _fresh_console(db_url)
    c.get("/login")
    assert c.passkey(auth).status_code == 401
    console.get("/account")
    console.post("/account/passkey-only", {"enabled": "1"})
    c.get("/login")
    r = c.passkey(auth)
    assert r.status_code == 200
    assert c.get("/").url.path == "/"


# ------------------------------------------------------------------------- audit, pages


def test_audit_log_is_append_only(conn: Conn) -> None:
    store.audit(conn, "test.entry", login="t", details={"a": 1})
    conn.commit()
    for statement in (
        "UPDATE admin.audit_log SET action = 'x'",
        "DELETE FROM admin.audit_log",
        "TRUNCATE admin.audit_log",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
        conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM admin.audit_log").fetchone() == {"n": 1}
    owner = conn.execute(
        "SELECT has_table_privilege(current_user, 'admin.audit_log', 'UPDATE') AS u, "
        "has_table_privilege(current_user, 'admin.audit_log', 'INSERT') AS i"
    ).fetchone()
    assert owner is not None and owner["i"]


def test_pages_render_real_data(console: Console, conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ops.worker_heartbeat (instance_id, process, host, pid, version, started_at,
                                          beat_at, running_jobs)
        VALUES (gen_random_uuid(), 'worker', 'vps1', 42, 'v-test', now(), now(),
                '[{"job": "bonds_eod", "run_id": 1, "started_at": "2026-10-17T10:00:00Z"}]')
        """
    )
    conn.execute(
        "INSERT INTO ops.job_run (job, status, finished_at, error, trigger) "
        "VALUES ('bonds_eod', 'failed', now(), 'HTTP 502 from ISS', 'schedule')"
    )
    conn.execute(
        "INSERT INTO ops.source_probe (source, endpoint, ok, http_status, latency_ms, auth) "
        "VALUES ('cbonds', 'https://ws2.cbonds.info/x', false, 401, 120, 'rejected')"
    )
    conn.execute(
        """
        INSERT INTO ops.api_request (ts, token_name, client_ip, method, path, status,
                                     duration_ms, response_bytes)
        VALUES (now(), 'tdm', '192.0.2.10', 'GET', '/v1/bonds', 200, 12.5, 1000),
               (now(), NULL, '192.0.2.66', 'GET', '<unmatched>', 401, 0.4, 20)
        """
    )
    conn.commit()
    _enrolled(console, conn, "alice")
    overview = console.get("/")
    assert "vps1" in overview.text and "credentials rejected" in overview.text
    assert 'hx-get="/overview/body"' in overview.text
    body = console.get("/overview/body")
    assert body.status_code == 200 and "<html" not in body.text
    jobs = console.get("/jobs")
    assert "HTTP 502 from ISS" in jobs.text and "Next fire" in jobs.text
    assert "cbonds" in console.get("/sources").text
    traffic = console.get("/traffic")
    assert "tdm" in traffic.text and "192.0.2.66" in traffic.text
    assert "Datasets" in console.get("/data").text
    assert "Running version" in console.get("/updates").text
    assert "alice" in console.get("/users").text
    assert "login.success" in console.get("/audit").text
    assert console.get("/jobs?job=bonds_eod&status=failed").status_code == 200


# --------------------------------------------------------------------------------- CLI


def _cli(db_url: str, *args: str, **env: str) -> Any:
    return CliRunner().invoke(
        cli.app,
        ["admin", *args],
        env={
            "MDHUB_DATABASE_URL": db_url,
            "MDHUB_ADMIN_DOMAIN": RP_ID,
            "MDHUB_ADMIN_SECRET_KEY": KEY_HEX,
            **env,
        },
    )


def test_cli_create_reset_disable_rotate(console: Console, conn: Conn, db_url: str) -> None:
    result = _cli(db_url, "create-user", "root", "--role", "admin", "--name", "Root")
    assert result.exit_code == 0, result.output
    link = re.search(r"https://admin\.test(/enroll/\S+)", result.stdout)
    assert link is not None
    assert _cli(db_url, "create-user", "root").exit_code == 2  # exists
    assert _cli(db_url, "create-user", "x", "--role", "god").exit_code == 2
    auth = SoftAuthenticator()
    console.enroll(link.group(1), PASSWORD, auth)
    console.get("/account")
    console.post("/account/totp/begin")
    console.post("/account/totp/confirm", {"code": _totp_now(conn, "root")})
    listing = _cli(db_url, "list")
    assert (
        "root" in listing.stdout and "passkeys=1" in listing.stdout and "totp=yes" in listing.stdout
    )

    rotated = _cli(db_url, "rotate-key", "--yes")
    assert rotated.exit_code == 0, rotated.output
    new_key = rotated.stdout.strip().splitlines()[-1]
    assert re.fullmatch(r"[0-9a-f]{64}", new_key) and new_key != KEY_HEX
    row = conn.execute(
        "SELECT t.secret_enc, u.id FROM admin.totp_secret t "
        "JOIN admin.user_account u ON u.id = t.user_id"
    ).fetchone()
    assert row is not None
    aad = f"mdhub-totp:{row['id']}".encode()
    assert security.decrypt_secret(bytes.fromhex(new_key), bytes(row["secret_enc"]), aad=aad)
    with pytest.raises(security.SecretKeyError):
        security.decrypt_secret(bytes.fromhex(KEY_HEX), bytes(row["secret_enc"]), aad=aad)

    reset = _cli(db_url, "reset", "root")
    assert reset.exit_code == 0
    assert console.get("/").url.path == "/login"  # reset logs the user out
    reset_link = re.search(r"https://admin\.test(/enroll/\S+)", reset.stdout)
    assert reset_link is not None
    new_auth = SoftAuthenticator()
    c = _fresh_console(db_url)
    c.enroll(reset_link.group(1), "a brand new passphrase", new_auth)
    conn.rollback()
    creds = conn.execute("SELECT count(*) AS n FROM admin.webauthn_credential").fetchone()
    assert creds == {"n": 1}  # the old passkey was replaced
    assert conn.execute("SELECT count(*) AS n FROM admin.totp_secret").fetchone() == {"n": 0}

    assert _cli(db_url, "disable", "root").exit_code == 0
    assert c.get("/").url.path == "/login"
    assert _cli(db_url, "enable", "root").exit_code == 0
    assert _cli(db_url, "unlock", "nobody").exit_code == 1
    actions = [
        a["action"]
        for a in conn.execute(
            "SELECT action FROM admin.audit_log WHERE login LIKE 'cli:%' ORDER BY id"
        ).fetchall()
    ]
    assert actions == [
        "user.create",
        "secret_key.rotate",
        "user.reset",
        "user.disable",
        "user.enable",
    ]
    assert _cli(db_url, "create-user", "y", MDHUB_ADMIN_DOMAIN="").exit_code == 2
