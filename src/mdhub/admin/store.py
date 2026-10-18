"""Database side of console authentication (``admin.*``, migration 0620; ADMIN_DESIGN §5).

Users, enrollment links, server-side sessions, throttling and the audit log. All times are
the database's ``now()``, so tests can age a session or an attempt by updating its row.

Sessions: the cookie holds 256 random bits; the table holds only their SHA-256. A session is
valid while not revoked, before ``expires_at`` (absolute: 12 h for ``full``, 15 min for the
login, second-factor and enrollment stages) and seen within the idle limit (30 min).
:func:`rotate_session` issues a new id (login, step-up, privilege change) and revokes the old.

Throttling (:func:`retry_after`) counts ``failure`` rows of ``admin.login_attempt`` per typed
login (since its last success or unlock) and per IP over 15 minutes: from the 3rd failure of a
login the next try waits 2^(n-3) s (max 5 min), from the 5th failure of an IP 2^(n-5) s (max
15 min), and 10 failures of a login lock it for 15 minutes. The login does not have to exist,
so the answers do not reveal which logins do.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from psycopg.types.json import Jsonb

from mdhub.admin.security import new_token, sha256
from mdhub.db.pool import DictConnection

__all__ = [
    "ABSOLUTE_LIFETIME",
    "ENROLLMENT_TTL",
    "IDLE_TIMEOUT",
    "LOCK_THRESHOLD",
    "LOCK_WINDOW",
    "PRE_AUTH_LIFETIME",
    "ROLES",
    "STEP_UP_WINDOW",
    "Session",
    "User",
    "audit",
    "create_enrollment",
    "create_session",
    "create_user",
    "load_session",
    "lookup_enrollment",
    "record_attempt",
    "retry_after",
    "revoke_user_sessions",
    "rotate_session",
    "user_by_id",
    "user_by_login",
]

Role = Literal["viewer", "operator", "admin"]
Stage = Literal["anonymous", "password", "enroll", "full"]
ROLES: Final[tuple[Role, ...]] = ("viewer", "operator", "admin")
IDLE_TIMEOUT: Final = timedelta(minutes=30)
ABSOLUTE_LIFETIME: Final = timedelta(hours=12)
PRE_AUTH_LIFETIME: Final = timedelta(minutes=15)
STEP_UP_WINDOW: Final = timedelta(minutes=5)
ENROLLMENT_TTL: Final = timedelta(minutes=15)
CHALLENGE_TTL: Final = timedelta(minutes=5)
LOCK_THRESHOLD: Final = 10
LOCK_WINDOW: Final = timedelta(minutes=15)
ATTEMPT_RETENTION: Final = timedelta(days=30)


class UserError(ValueError):
    """Invalid login, role or display name."""


@dataclass(frozen=True, slots=True)
class User:
    id: int
    login: str
    display_name: str
    role: Role
    password_hash: str | None
    passkey_only: bool
    webauthn_user_id: bytes
    disabled_at: datetime | None

    def at_least(self, role: Role) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


@dataclass(frozen=True, slots=True)
class Session:
    id_sha256: bytes
    user: User | None
    stage: Stage
    csrf_token: str
    created_at: datetime
    expires_at: datetime
    mfa_at: datetime | None
    step_up_at: datetime | None
    enrollment_id: int | None
    now: datetime
    """Database time of the request that loaded the session."""

    @property
    def ref(self) -> str:
        """Short reference for the audit log (not usable as a session id)."""
        return self.id_sha256.hex()[:12]

    @property
    def stepped_up(self) -> bool:
        return self.step_up_at is not None and self.now - self.step_up_at < STEP_UP_WINDOW


_USER_COLS: Final = (
    "id, login, display_name, role, password_hash, passkey_only, webauthn_user_id, disabled_at"
)


def _user(row: dict[str, Any] | None) -> User | None:
    if row is None:
        return None
    return User(
        id=int(row["id"]),
        login=row["login"],
        display_name=row["display_name"],
        role=row["role"],
        password_hash=row["password_hash"],
        passkey_only=bool(row["passkey_only"]),
        webauthn_user_id=bytes(row["webauthn_user_id"]),
        disabled_at=row["disabled_at"],
    )


def normalize_login(login: str) -> str:
    return login.strip().lower()[:128]


# ----------------------------------------------------------------------------------- users


def create_user(
    conn: DictConnection,
    login: str,
    role: str,
    *,
    display_name: str = "",
    created_by: str | None = None,
) -> User:
    login = normalize_login(login)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._@-]{0,63}", login):
        raise UserError("login: 1-64 of a-z 0-9 . _ @ -, starting with a letter or digit")
    if role not in ROLES:
        raise UserError(f"role must be one of {', '.join(ROLES)}")
    if len(display_name) > 128:
        raise UserError("display name: at most 128 characters")
    if user_by_login(conn, login) is not None:
        raise UserError(f"user {login!r} exists")
    row = conn.execute(
        f"""
        INSERT INTO admin.user_account (login, display_name, role, webauthn_user_id, created_by)
        VALUES (%s, %s, %s, %s, %s) RETURNING {_USER_COLS}
        """,
        (login, display_name.strip(), role, secrets.token_bytes(32), created_by),
    ).fetchone()
    user = _user(row)
    assert user is not None
    return user


def user_by_login(conn: DictConnection, login: str) -> User | None:
    return _user(
        conn.execute(
            f"SELECT {_USER_COLS} FROM admin.user_account WHERE login = %s",
            (normalize_login(login),),
        ).fetchone()
    )


def user_by_id(conn: DictConnection, user_id: int) -> User | None:
    return _user(
        conn.execute(
            f"SELECT {_USER_COLS} FROM admin.user_account WHERE id = %s", (user_id,)
        ).fetchone()
    )


# ------------------------------------------------------------------------------ enrollment


def create_enrollment(
    conn: DictConnection, user_id: int, purpose: Literal["invite", "reset"], created_by: str | None
) -> str:
    """New one-time link token (valid :data:`ENROLLMENT_TTL`); earlier unused links of the
    user stop working."""
    conn.execute(
        "UPDATE admin.enrollment SET expires_at = least(expires_at, now()) "
        "WHERE user_id = %s AND used_at IS NULL",
        (user_id,),
    )
    token = new_token()
    conn.execute(
        """
        INSERT INTO admin.enrollment (token_sha256, user_id, purpose, created_by, expires_at)
        VALUES (%s, %s, %s, %s, now() + %s)
        """,
        (sha256(token), user_id, purpose, created_by, ENROLLMENT_TTL),
    )
    return token


def lookup_enrollment(conn: DictConnection, token: str) -> dict[str, Any] | None:
    """The unused, unexpired enrollment of ``token`` (with its user), else ``None``."""
    row = conn.execute(
        """
        SELECT e.id, e.user_id, e.purpose FROM admin.enrollment e
        JOIN admin.user_account u ON u.id = e.user_id
        WHERE e.token_sha256 = %s AND e.used_at IS NULL AND e.expires_at > now()
          AND u.disabled_at IS NULL
        """,
        (sha256(token),),
    ).fetchone()
    return dict(row) if row else None


# -------------------------------------------------------------------------------- sessions


def create_session(
    conn: DictConnection,
    *,
    user_id: int | None,
    stage: Stage,
    ip: str | None,
    user_agent: str | None,
    enrollment_id: int | None = None,
    mfa: bool = False,
) -> str:
    """Insert a session; returns the cookie value (never stored)."""
    sid = new_token()
    lifetime = ABSOLUTE_LIFETIME if stage == "full" else PRE_AUTH_LIFETIME
    conn.execute(
        """
        INSERT INTO admin.session
            (id_sha256, user_id, stage, enrollment_id, csrf_token, expires_at, ip, user_agent,
             mfa_at, step_up_at)
        VALUES (%s, %s, %s, %s, %s, now() + %s, %s, %s,
                CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END)
        """,
        (
            sha256(sid),
            user_id,
            stage,
            enrollment_id,
            new_token(),
            lifetime,
            ip,
            (user_agent or "")[:256] or None,
            mfa,
            mfa,
        ),
    )
    return sid


def load_session(conn: DictConnection, cookie: str | None) -> Session | None:
    """The live session of ``cookie`` (seen-time updated), else ``None``."""
    if not cookie or len(cookie) > 128:
        return None
    row = conn.execute(
        """
        UPDATE admin.session s SET last_seen_at = now()
        WHERE s.id_sha256 = %s AND s.revoked_at IS NULL AND s.expires_at > now()
          AND s.last_seen_at > now() - %s
        RETURNING s.id_sha256, s.user_id, s.stage, s.csrf_token, s.created_at, s.expires_at,
                  s.mfa_at, s.step_up_at, s.enrollment_id, now() AS now
        """,
        (sha256(cookie), IDLE_TIMEOUT),
    ).fetchone()
    if row is None:
        return None
    user = None
    if row["user_id"] is not None:
        user = user_by_id(conn, int(row["user_id"]))
        if user is None or user.disabled_at is not None:
            return None
    return Session(
        id_sha256=bytes(row["id_sha256"]),
        user=user,
        stage=row["stage"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        mfa_at=row["mfa_at"],
        step_up_at=row["step_up_at"],
        enrollment_id=row["enrollment_id"],
        now=row["now"],
    )


def rotate_session(
    conn: DictConnection,
    old: Session | None,
    *,
    user_id: int,
    stage: Stage,
    ip: str | None,
    user_agent: str | None,
    enrollment_id: int | None = None,
    mfa: bool = False,
) -> str:
    """Revoke ``old`` and issue a new session id (login, step-up, stage change). A step-up
    keeps the absolute expiry of the session it replaces."""
    if old is not None:
        revoke_session(conn, old.id_sha256)
    sid = create_session(
        conn,
        user_id=user_id,
        stage=stage,
        ip=ip,
        user_agent=user_agent,
        enrollment_id=enrollment_id,
        mfa=mfa,
    )
    if old is not None and old.stage == "full" and stage == "full":
        conn.execute(
            "UPDATE admin.session SET expires_at = %s, mfa_at = %s, created_at = %s "
            "WHERE id_sha256 = %s",
            (old.expires_at, old.mfa_at, old.created_at, sha256(sid)),
        )
    return sid


def revoke_session(conn: DictConnection, id_sha256: bytes) -> None:
    conn.execute(
        "UPDATE admin.session SET revoked_at = now(), webauthn_challenge = NULL "
        "WHERE id_sha256 = %s AND revoked_at IS NULL",
        (id_sha256,),
    )


def revoke_user_sessions(conn: DictConnection, user_id: int, *, keep: bytes | None = None) -> int:
    """Revoke every live session of the user except ``keep``; returns how many."""
    cur = conn.execute(
        """
        UPDATE admin.session SET revoked_at = now(), webauthn_challenge = NULL
        WHERE user_id = %s AND revoked_at IS NULL AND id_sha256 IS DISTINCT FROM %s
        """,
        (user_id, keep),
    )
    return cur.rowcount


def set_challenge(conn: DictConnection, session: Session, challenge: bytes) -> None:
    conn.execute(
        "UPDATE admin.session SET webauthn_challenge = %s, challenge_expires_at = now() + %s "
        "WHERE id_sha256 = %s",
        (challenge, CHALLENGE_TTL, session.id_sha256),
    )


def pop_challenge(conn: DictConnection, session: Session) -> bytes | None:
    """The pending WebAuthn challenge of the session (one use), if not expired."""
    row = conn.execute(
        "SELECT webauthn_challenge AS challenge, challenge_expires_at > now() AS fresh "
        "FROM admin.session WHERE id_sha256 = %s FOR UPDATE",
        (session.id_sha256,),
    ).fetchone()
    conn.execute(
        "UPDATE admin.session SET webauthn_challenge = NULL, challenge_expires_at = NULL "
        "WHERE id_sha256 = %s",
        (session.id_sha256,),
    )
    if row is None or row["challenge"] is None or not row["fresh"]:
        return None
    return bytes(row["challenge"])


def purge_expired(conn: DictConnection) -> None:
    """Drop sessions dead for a day and login attempts older than 30 days."""
    conn.execute(
        "DELETE FROM admin.session WHERE greatest(expires_at, coalesce(revoked_at, expires_at)) "
        "< now() - interval '1 day' OR last_seen_at < now() - interval '1 day'"
    )
    conn.execute("DELETE FROM admin.login_attempt WHERE ts < now() - %s", (ATTEMPT_RETENTION,))


# ------------------------------------------------------------------------------ throttling


def record_attempt(
    conn: DictConnection,
    login: str | None,
    ip: str | None,
    outcome: Literal["success", "failure", "throttled", "unlocked"],
) -> None:
    conn.execute(
        "INSERT INTO admin.login_attempt (login, ip, outcome) VALUES (%s, %s, %s)",
        (normalize_login(login) if login else None, ip, outcome),
    )


def login_failures(conn: DictConnection, login: str) -> int:
    """Failures of ``login`` in the lock window since its last success or unlock."""
    row = conn.execute(
        """
        SELECT count(*) AS n FROM admin.login_attempt
        WHERE login = %(login)s AND outcome = 'failure' AND ts > now() - %(window)s
          AND ts > coalesce((SELECT max(ts) FROM admin.login_attempt
                             WHERE login = %(login)s AND outcome IN ('success', 'unlocked')),
                            '-infinity')
        """,
        {"login": normalize_login(login), "window": LOCK_WINDOW},
    ).fetchone()
    return int(row["n"]) if row else 0


def retry_after(conn: DictConnection, login: str | None, ip: str | None) -> int | None:
    """Seconds the caller must wait before another attempt, or ``None``."""
    row = conn.execute(
        """
        WITH since AS (
            SELECT coalesce(max(ts), '-infinity') AS ts FROM admin.login_attempt
            WHERE login = %(login)s AND outcome IN ('success', 'unlocked')
        )
        SELECT now() AS now,
               (SELECT count(*) FROM admin.login_attempt a, since
                 WHERE a.login = %(login)s AND a.outcome = 'failure' AND a.ts > since.ts
                   AND a.ts > now() - %(window)s) AS login_n,
               (SELECT max(a.ts) FROM admin.login_attempt a, since
                 WHERE a.login = %(login)s AND a.outcome = 'failure' AND a.ts > since.ts
                   AND a.ts > now() - %(window)s) AS login_last,
               (SELECT count(*) FROM admin.login_attempt a
                 WHERE a.ip = %(ip)s AND a.outcome = 'failure'
                   AND a.ts > now() - %(window)s) AS ip_n,
               (SELECT max(a.ts) FROM admin.login_attempt a
                 WHERE a.ip = %(ip)s AND a.outcome = 'failure'
                   AND a.ts > now() - %(window)s) AS ip_last
        """,
        {"login": normalize_login(login) if login else None, "ip": ip, "window": LOCK_WINDOW},
    ).fetchone()
    assert row is not None
    now: datetime = row["now"]
    until: list[datetime] = []
    n, last = int(row["login_n"]), row["login_last"]
    if login and last is not None:
        if n >= LOCK_THRESHOLD:
            until.append(last + LOCK_WINDOW)
        elif n >= 3:
            until.append(last + timedelta(seconds=min(2 ** (n - 3), 300)))
    n, last = int(row["ip_n"]), row["ip_last"]
    if ip and last is not None and n >= 5:
        until.append(last + timedelta(seconds=min(2 ** (n - 5), 900)))
    wait = max(until, default=now) - now
    seconds = int(wait.total_seconds() + 0.999)
    return seconds if seconds > 0 else None


# ----------------------------------------------------------------------------------- audit


def audit(
    conn: DictConnection,
    action: str,
    *,
    session: Session | None = None,
    user: User | None = None,
    login: str | None = None,
    ip: str | None = None,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    outcome: Literal["ok", "denied", "failed"] = "ok",
) -> None:
    """Append one audit entry (never pass secrets in ``details``)."""
    actor = user or (session.user if session else None)
    conn.execute(
        """
        INSERT INTO admin.audit_log (user_id, login, session_ref, ip, action, target, details,
                                     outcome)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor.id if actor else None,
            actor.login if actor else login,
            session.ref if session else None,
            ip,
            action,
            target,
            Jsonb(details or {}),
            outcome,
        ),
    )
