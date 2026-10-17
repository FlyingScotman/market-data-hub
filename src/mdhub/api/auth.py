"""Bearer-token authentication for ``/v1``: named tokens (``admin.api_token``) and
``MDHUB_API_TOKENS`` (ADMIN_DESIGN §3.3).

* Token format of named tokens: ``mdh_`` + base32 (lower case, no padding) of 32 random bytes
  (:func:`generate_token`); the prefix makes leaked tokens easy to find with secret scanners.
  Only the SHA-256 is stored; the full token is shown once, at creation
  (``mdhub tokens create``).
* ``MDHUB_API_TOKENS`` keeps working: every entry is a token with all scopes, named ``env:N``
  (bare token) or ``name`` (``name=token``). The presented token is compared as a SHA-256
  digest with :func:`hmac.compare_digest` against **every** env token (no early exit, no length
  leak).
* Otherwise the digest is looked up in ``admin.api_token`` (unique index), then compared with
  :func:`hmac.compare_digest`. Lookups - hits and misses - are cached in memory for
  :data:`TOKEN_CACHE_TTL_S` (30 s), so a revocation takes effect within 30 s; expiry is checked
  on every request.
* Scopes: ``read`` (all of ``/v1``), ``snapshots`` (``/v1/snapshots*`` only), ``export``
  (``/v1/export/*`` only); ``/v1/openapi.json`` accepts any scope. A valid token without the
  scope gets 403.
* No env tokens and no database -> every ``/v1`` request is rejected (fail closed). A database
  error during a lookup answers 503.
* A 401 carries only ``WWW-Authenticate: Bearer`` (no realm, no error description) and a
  generic message; tokens are never logged or echoed. ``request.state.token`` holds the
  :class:`TokenIdentity` of an authenticated request (read by the request log).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import psycopg
from fastapi import HTTPException, Request

from mdhub.db.pool import Database, DictConnection

__all__ = [
    "SCOPES",
    "TOKEN_CACHE_TTL_S",
    "TOKEN_PREFIX",
    "TokenError",
    "TokenIdentity",
    "TokenStore",
    "TokenStoreUnavailable",
    "create_token",
    "generate_token",
    "list_tokens",
    "require_token",
    "required_scopes",
    "revoke_token",
    "token_digest",
    "token_digests",
    "valid_token_name",
]

log = logging.getLogger(__name__)

SCOPES: Final = ("read", "snapshots", "export")
TOKEN_PREFIX: Final = "mdh_"  # noqa: S105 - a public prefix, not a secret
TOKEN_CACHE_TTL_S: Final = 30.0
TOKEN_CACHE_MAX: Final = 10_000
"""Cached lookups kept at most; the cache is cleared when full (unknown-token floods)."""
_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class TokenError(ValueError):
    """Invalid token name, scope or expiry (token management)."""


class TokenStoreUnavailable(RuntimeError):  # noqa: N818 - reads better than "...Error"
    """``admin.api_token`` could not be read (database down)."""


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    """Who is calling: the consumer behind an accepted token. Holds no secret."""

    name: str
    scopes: frozenset[str]
    source: Literal["env", "db"]
    token_id: int | None = None
    """``admin.api_token.id`` (``None`` for ``MDHUB_API_TOKENS`` entries)."""
    expires_at: datetime | None = None

    def allows(self, required: frozenset[str]) -> bool:
        return bool(self.scopes & required)


# ---------------------------------------------------------------------------- tokens


def generate_token() -> str:
    """New named-token value: ``mdh_`` + 52 base32 characters (256 random bits)."""
    body = base64.b32encode(secrets.token_bytes(32)).decode("ascii").rstrip("=").lower()
    return TOKEN_PREFIX + body


def token_digest(token: str) -> bytes:
    """SHA-256 of a token (what is stored and compared)."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def token_digests(tokens: Iterable[str]) -> tuple[bytes, ...]:
    """SHA-256 digests of the configured tokens (the app keeps only these)."""
    return tuple(token_digest(t) for t in tokens if t)


def valid_token_name(name: str) -> bool:
    """Lower-case ``[a-z0-9][a-z0-9._-]*``, at most 64 characters (no ``env:`` names)."""
    return bool(_NAME_RE.match(name))


def required_scopes(path: str) -> frozenset[str]:
    """Scopes of which a token needs at least one for ``path`` (under ``/v1``)."""
    if path == "/v1/openapi.json":
        return frozenset(SCOPES)
    if path == "/v1/snapshots" or path.startswith("/v1/snapshots/"):
        return frozenset({"read", "snapshots"})
    if path.startswith("/v1/export/"):
        return frozenset({"read", "export"})
    return frozenset({"read"})


# ----------------------------------------------------------------------------- store


@dataclass(frozen=True, slots=True)
class _Cached:
    at: float
    digest: bytes | None
    identity: TokenIdentity | None
    revoked: bool


class TokenStore:
    """Resolves presented tokens to :class:`TokenIdentity` (env tokens, then the database).

    :param env_tokens: ``(name, token)`` pairs from ``Settings.api_token_entries()``.
    :param db: database holding ``admin.api_token`` (``None``: env tokens only).
    :param ttl_s: cache lifetime of database lookups.
    :param clock: monotonic clock (tests).

    Thread-safe (the ``/v1`` dependency runs in the threadpool).
    """

    def __init__(
        self,
        env_tokens: Sequence[tuple[str, str]],
        db: Database | None = None,
        *,
        ttl_s: float = TOKEN_CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        all_scopes = frozenset(SCOPES)
        self._env = tuple(
            (token_digest(token), TokenIdentity(name=name, scopes=all_scopes, source="env"))
            for name, token in env_tokens
            if token
        )
        self._db = db
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[bytes, _Cached] = {}
        self._lock = threading.Lock()
        self._table_missing_logged = False

    @property
    def configured(self) -> bool:
        """Whether any token can be accepted at all (env tokens or a database)."""
        return bool(self._env) or self._db is not None

    @property
    def env_names(self) -> list[str]:
        return [identity.name for _, identity in self._env]

    def invalidate(self) -> None:
        """Drop all cached database lookups."""
        with self._lock:
            self._cache.clear()

    def authenticate(self, token: str, *, now: datetime | None = None) -> TokenIdentity | None:
        """Identity of ``token`` or ``None`` (unknown, revoked or expired).

        :raises TokenStoreUnavailable: the database lookup failed.
        """
        presented = token_digest(token)
        env_match: TokenIdentity | None = None
        for digest, identity in self._env:  # no early exit
            if hmac.compare_digest(presented, digest) and env_match is None:
                env_match = identity
        if env_match is not None:
            return env_match
        if self._db is None:
            return None
        entry = self._cached(presented)
        if entry is None:
            entry = self._lookup(presented)
        if entry.identity is None or entry.revoked or entry.digest is None:
            return None
        if not hmac.compare_digest(presented, entry.digest):
            return None
        expires = entry.identity.expires_at
        if expires is not None and expires <= (now or datetime.now(UTC)):
            return None
        return entry.identity

    def _cached(self, digest: bytes) -> _Cached | None:
        with self._lock:
            entry = self._cache.get(digest)
        if entry is None or self._clock() - entry.at >= self._ttl:
            return None
        return entry

    def _lookup(self, digest: bytes) -> _Cached:
        assert self._db is not None
        try:
            with self._db.connection() as conn:
                row = conn.execute(
                    "SELECT id, name, token_sha256, scopes, expires_at, revoked_at "
                    "FROM admin.api_token WHERE token_sha256 = %s",
                    (digest,),
                ).fetchone()
        except psycopg.errors.UndefinedTable:
            if not self._table_missing_logged:
                log.warning("admin.api_token does not exist (run `mdhub migrate`)")
                self._table_missing_logged = True
            row = None
        except psycopg.Error as exc:
            log.warning("token lookup failed: %s", type(exc).__name__)
            raise TokenStoreUnavailable("token store unavailable") from None
        entry = _Cached(at=self._clock(), digest=None, identity=None, revoked=False)
        if row is not None:
            entry = _Cached(
                at=entry.at,
                digest=bytes(row["token_sha256"]),
                identity=TokenIdentity(
                    name=row["name"],
                    scopes=frozenset(row["scopes"]),
                    source="db",
                    token_id=int(row["id"]),
                    expires_at=row["expires_at"],
                ),
                revoked=row["revoked_at"] is not None,
            )
        with self._lock:
            if len(self._cache) >= TOKEN_CACHE_MAX:
                self._cache.clear()
            self._cache[digest] = entry
        return entry


# ------------------------------------------------------------------------ dependency


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )


def require_token(request: Request) -> None:
    """FastAPI dependency: 401 unless ``Authorization: Bearer <token>`` is an accepted token,
    403 when the token lacks the scope for the path; sets ``request.state.token``."""
    store: TokenStore | None = getattr(request.app.state, "token_store", None)
    header = request.headers.get("authorization") or ""
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential or store is None or not store.configured:
        raise _unauthorized()
    try:
        identity = store.authenticate(credential)
    except TokenStoreUnavailable:
        raise HTTPException(status_code=503, detail="token store unavailable") from None
    if identity is None:
        raise _unauthorized()
    request.state.token = identity
    if not identity.allows(required_scopes(request.url.path)):
        raise HTTPException(status_code=403, detail="forbidden: token scope")


# ---------------------------------------------------------------------- management


def _check_scopes(scopes: Iterable[str]) -> list[str]:
    out = sorted(set(scopes), key=SCOPES.index) if all(s in SCOPES for s in scopes) else None
    if not out:
        raise TokenError(f"scopes must be a non-empty subset of {', '.join(SCOPES)}")
    return out


def create_token(
    conn: DictConnection,
    name: str,
    scopes: Iterable[str] = ("read",),
    *,
    expires_at: datetime | None = None,
    created_by: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Insert a new named token; returns ``(full token, row without the digest)``.

    The full token is not stored anywhere: show it once and drop it.

    :raises TokenError: bad name or scopes, expiry not in the future, or a live token with
        that name exists.
    """
    if not valid_token_name(name):
        raise TokenError(
            f"invalid token name {name!r} (lower-case letters, digits, '.', '_', '-'; max 64)"
        )
    scope_list = _check_scopes(list(scopes))
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise TokenError("expires_at must be timezone-aware")
        if expires_at <= datetime.now(UTC):
            raise TokenError("expires_at must be in the future")
    token = generate_token()
    try:
        with conn.transaction():
            row = conn.execute(
                """
                INSERT INTO admin.api_token
                    (name, token_sha256, prefix, scopes, created_by, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, name, prefix, scopes, created_by, created_at, expires_at
                """,
                (name, token_digest(token), token[:8], scope_list, created_by, expires_at),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise TokenError(f"a live token named {name!r} exists (revoke it first)") from None
    assert row is not None
    return token, dict(row)


def revoke_token(conn: DictConnection, name_or_id: str) -> list[dict[str, Any]]:
    """Revoke the live token(s) with that name or id; returns the revoked rows."""
    if name_or_id.isdigit():
        query = """
            UPDATE admin.api_token SET revoked_at = now()
            WHERE revoked_at IS NULL AND id = %s::bigint
            RETURNING id, name, prefix, revoked_at
        """
    else:
        query = """
            UPDATE admin.api_token SET revoked_at = now()
            WHERE revoked_at IS NULL AND name = %s
            RETURNING id, name, prefix, revoked_at
        """
    rows = conn.execute(query, (name_or_id,)).fetchall()
    return [dict(r) for r in rows]


def list_tokens(conn: DictConnection, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    """Token rows for display (never the digest), oldest first."""
    rows = conn.execute(
        """
        SELECT id, name, prefix, scopes, created_by, created_at, expires_at, revoked_at,
               last_used_at, host(last_used_ip) AS last_used_ip
        FROM admin.api_token
        WHERE %s OR revoked_at IS NULL
        ORDER BY id
        """,
        (include_revoked,),
    ).fetchall()
    return [dict(r) for r in rows]
