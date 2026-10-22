"""Credentials page (ADMIN_DESIGN §9): set, replace and remove source credentials, write-only.

The console seals each value to the worker's latest published public key
(:func:`mdhub.credentials.seal`) and stores only the ciphertext; it holds no private key and
cannot read a value back. Pages never show or pre-fill a value; audit entries name the
credential, never its value. **Check** queues a ``source_probe`` of the credential's source for
the worker (``ops.job_request``): the console makes no calls to sources. Viewing needs
``admin``, changes also a step-up.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import psycopg
from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse, Response
from psycopg.types.json import Jsonb

from mdhub.admin.web import Ctx, Denied, render, require
from mdhub.credentials import REGISTRY, Credential, seal
from mdhub.db.pool import DictConnection
from mdhub.quality.source_probe import PROBES

__all__ = ["router"]

router = APIRouter()

admin = require("admin")
admin_step_up = require("admin", step_up=True)

MAX_VALUE: Final = 4096
_PROBED: Final = frozenset(p.source for p in PROBES)
MESSAGES: Final = {
    "set": "The credential was saved. The worker uses it from its next job run.",
    "removed": "The credential was removed from the store.",
    "check": "A check of the source is queued; the result shows here within a minute.",
}


def _spec(name: str) -> Credential:
    spec = REGISTRY.get(name)
    if spec is None:
        raise Denied(404, "Unknown credential.")
    return spec


def _current_key(conn: DictConnection) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT key_id, public_key, env_names, published_at FROM admin.secret_key_public "
        "ORDER BY published_at DESC LIMIT 1"
    ).fetchone()


def _page(ctx: Ctx, status_code: int = 200, **values: Any) -> Response:
    with ctx.db.connection() as conn:
        key = _current_key(conn)
        stored = {
            r["name"]: r
            for r in conn.execute(
                "SELECT name, key_id, set_by, set_at FROM admin.source_credential"
            ).fetchall()
        }
        probes = {
            r["source"]: r
            for r in conn.execute(
                "SELECT source, auth, ok, http_status, probed_at, error FROM ops.v_source_status"
            )
        }
    in_env = set(key["env_names"]) if key else set()
    rows = []
    for spec in REGISTRY.values():
        row = stored.get(spec.name)
        env = spec.name in in_env
        state = ("both" if env else "console") if row else (".env fallback" if env else "not set")
        rows.append(
            {
                "spec": spec,
                "row": row,
                "state": state,
                "reenter": row is not None and (key is None or row["key_id"] != key["key_id"]),
                "probe": probes.get(spec.source) if spec.source else None,
                "checkable": spec.source is None or spec.source in _PROBED,
            }
        )
    return render(ctx, "credentials.html", status_code=status_code, rows=rows, key=key, **values)


@router.get("/credentials")
def credentials_page(ctx: Annotated[Ctx, Depends(admin)], done: str | None = None) -> Response:
    return _page(ctx, flash=MESSAGES.get(done or ""))


@router.post("/credentials/{name}")
def credential_set(
    ctx: Annotated[Ctx, Depends(admin_step_up)],
    name: str,
    value: Annotated[str, Form(max_length=MAX_VALUE)] = "",
) -> Response:
    spec = _spec(name)
    if not value.strip():
        return _page(ctx, 400, error=f"Enter a value for {spec.name}.")
    with ctx.db.connection() as conn:
        key = _current_key(conn)
        if key is None:
            return _page(ctx, 409, error="The worker has not published its key yet.")
        row = conn.execute(
            """
            INSERT INTO admin.source_credential (name, ciphertext, key_id, set_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                ciphertext = EXCLUDED.ciphertext, key_id = EXCLUDED.key_id,
                set_by = EXCLUDED.set_by, set_at = now(), verified_at = NULL,
                verify_result = NULL
            RETURNING (xmax <> 0) AS replaced
            """,
            (
                spec.name,
                seal(bytes(key["public_key"]), spec.name, value),
                key["key_id"],
                ctx.user.login,
            ),
        ).fetchone()
        assert row is not None
        ctx.audit(
            conn,
            "credential.replace" if row["replaced"] else "credential.set",
            target=f"credential:{spec.name}",
            details={"key_id": key["key_id"]},
        )
    return RedirectResponse("/credentials?done=set", status_code=303)


@router.post("/credentials/{name}/remove")
def credential_remove(ctx: Annotated[Ctx, Depends(admin_step_up)], name: str) -> Response:
    spec = _spec(name)
    with ctx.db.connection() as conn:
        gone = conn.execute(
            "DELETE FROM admin.source_credential WHERE name = %s RETURNING name", (spec.name,)
        ).fetchone()
        if gone is None:
            raise Denied(404, "That credential is not set in the console.")
        ctx.audit(conn, "credential.remove", target=f"credential:{spec.name}")
    return RedirectResponse("/credentials?done=removed", status_code=303)


@router.post("/credentials/{name}/check")
def credential_check(ctx: Annotated[Ctx, Depends(admin)], name: str) -> Response:
    spec = _spec(name)
    if spec.source is not None and spec.source not in _PROBED:
        raise Denied(404, "There is no check for this source.")
    params: dict[str, Any] = {"force": True}
    if spec.source is not None:
        params["sources"] = [spec.source]
    with ctx.db.connection() as conn:
        try:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO ops.job_request (job, params, requested_by, requested_login)
                    VALUES ('source_probe', %s, %s, %s) RETURNING id
                    """,
                    (Jsonb(params), ctx.user.id, ctx.user.login),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise Denied(409, "A source check is already queued or running.") from None
        assert row is not None
        ctx.audit(
            conn,
            "credential.check",
            target=f"credential:{spec.name}",
            details={"request_id": row["id"], "source": spec.source},
        )
    return RedirectResponse("/credentials?done=check", status_code=303)
