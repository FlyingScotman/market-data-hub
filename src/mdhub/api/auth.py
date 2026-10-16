"""Bearer-token authentication for ``/v1`` (tokens from ``MDHUB_API_TOKENS``).

* Tokens are compared as SHA-256 digests with :func:`hmac.compare_digest` against **every**
  configured token (no early exit, no length leak).
* No tokens configured -> every ``/v1`` request is rejected (fail closed).
* A 401 carries only ``WWW-Authenticate: Bearer`` (no realm, no error description) and a
  generic message; tokens are never logged or echoed.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

from fastapi import HTTPException, Request

__all__ = ["require_token", "token_digests"]


def token_digests(tokens: Iterable[str]) -> tuple[bytes, ...]:
    """SHA-256 digests of the configured tokens (the app keeps only these)."""
    return tuple(hashlib.sha256(t.encode("utf-8")).digest() for t in tokens if t)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
    )


def require_token(request: Request) -> None:
    """FastAPI dependency: 401 unless ``Authorization: Bearer <token>`` matches a token."""
    digests: tuple[bytes, ...] = getattr(request.app.state, "token_digests", ())
    header = request.headers.get("authorization") or ""
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential or not digests:
        raise _unauthorized()
    presented = hashlib.sha256(credential.encode("utf-8")).digest()
    ok = False
    for digest in digests:
        ok |= hmac.compare_digest(presented, digest)
    if not ok:
        raise _unauthorized()
