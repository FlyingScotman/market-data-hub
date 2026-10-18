"""Login, second factor, step-up, enrollment, logout and the user's own account
(ADMIN_DESIGN §5).

Flow: ``GET /login`` opens an anonymous session (it carries the CSRF token of the form and a
passkey challenge). The password moves it to stage ``password``; a passkey, TOTP or recovery
code then rotates it into a ``full`` session. A passkey alone logs in only users who turned
that on. Every failure answers the same text with the same status after one argon2
verification, whether the login exists or not; throttling and lockout key on the typed login.

Enrollment: ``/enroll/<token>`` (one-time, 15 min) opens an ``enroll`` session; the user sets
a password (that uses the link) and registers a passkey, which completes the login. A reset
link also removes the old passkeys, TOTP and recovery codes and revokes all sessions.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mdhub.admin import factors, security, store
from mdhub.admin.factors import FactorError, SuspectCredential
from mdhub.admin.web import (
    Ctx,
    Denied,
    clear_cookie,
    load,
    render,
    require,
    safe_next,
    set_cookie,
)
from mdhub.db.pool import DictConnection

__all__ = ["router"]

router = APIRouter()

GENERIC_FAILURE = "Invalid login or password."
FACTOR_FAILURE = "The code or passkey was not accepted."
THROTTLED = "Too many attempts. Try again later."

full = require("viewer")
full_step_up = require("viewer", step_up=True)
anonymous = require(stages=("anonymous",))
password_stage = require(stages=("password",))
enroll_stage = require(stages=("enroll",))
any_stage = require(stages=("anonymous", "password", "enroll", "full"))


def _redirect(location: str, cookie: str | None = None) -> RedirectResponse:
    response = RedirectResponse(location, status_code=303)
    if cookie is not None:
        set_cookie(response, cookie)
    return response


def _json_redirect(location: str, cookie: str | None = None) -> JSONResponse:
    response = JSONResponse({"redirect": location})
    if cookie is not None:
        set_cookie(response, cookie)
    return response


def _throttled(ctx: Ctx, conn: DictConnection, login: str | None) -> int | None:
    wait = store.retry_after(conn, login, ctx.ip)
    if wait is not None:
        store.record_attempt(conn, login, ctx.ip, "throttled")
    return wait


def _failure(ctx: Ctx, conn: DictConnection, login: str | None) -> None:
    store.record_attempt(conn, login, ctx.ip, "failure")
    if login and store.login_failures(conn, login) == store.LOCK_THRESHOLD:
        store.audit(
            conn,
            "login.lockout",
            login=store.normalize_login(login),
            ip=ctx.ip,
            target=store.normalize_login(login),
            details={"minutes": int(store.LOCK_WINDOW.total_seconds() // 60)},
            outcome="denied",
        )


def _complete_login(ctx: Ctx, conn: DictConnection, user: store.User, method: str) -> str:
    """Record the success and rotate into a full session; returns the new cookie."""
    store.record_attempt(conn, user.login, ctx.ip, "success")
    cookie = store.rotate_session(
        conn,
        ctx.session,
        user_id=user.id,
        stage="full",
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        mfa=True,
    )
    store.audit(conn, "login.success", user=user, ip=ctx.ip, details={"method": method})
    store.purge_expired(conn)
    return cookie


def _key(ctx: Ctx) -> bytes:
    if ctx.cfg.secret_key is None:
        raise Denied(503, "TOTP is not available: MDHUB_ADMIN_SECRET_KEY is not set.")
    return ctx.cfg.secret_key


def _rp(ctx: Ctx) -> factors.RelyingParty:
    assert ctx.cfg.rp is not None  # the middleware refuses requests without it
    return ctx.cfg.rp


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise Denied(400, "Malformed request.") from None
    if not isinstance(body, dict):
        raise Denied(400, "Malformed request.")
    return body


# ----------------------------------------------------------------------------------- login


@router.get("/login")
def login_page(request: Request, next: str | None = None) -> Response:
    ctx = load(request)
    s = ctx.session
    if s is not None and s.stage == "full":
        return _redirect(safe_next(next))
    if s is not None and s.stage == "password":
        return _redirect("/login/mfa")
    if s is not None and s.stage == "anonymous":
        return render(ctx, "login.html", next=safe_next(next))
    with ctx.db.connection() as conn:
        cookie = store.create_session(
            conn, user_id=None, stage="anonymous", ip=ctx.ip, user_agent=ctx.user_agent
        )
        ctx.session = store.load_session(conn, cookie)
    response = render(ctx, "login.html", next=safe_next(next))
    set_cookie(response, cookie)
    return response


@router.post("/login")
def login_submit(
    ctx: Annotated[Ctx, Depends(anonymous)],
    login: Annotated[str, Form(max_length=128)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    login = store.normalize_login(login)
    with ctx.db.connection() as conn:
        wait = _throttled(ctx, conn, login)
        if wait is not None:
            return render(
                ctx,
                "login.html",
                status_code=429,
                headers={"Retry-After": str(wait)},
                error=THROTTLED,
                next=safe_next(next),
            )
        user = store.user_by_login(conn, login) if login else None
        usable = user is not None and user.disabled_at is None
        ok, rehash = security.verify_password(
            user.password_hash if user is not None and usable else None, password
        )
        if not ok or user is None:
            _failure(ctx, conn, login)
            return render(
                ctx, "login.html", status_code=401, error=GENERIC_FAILURE, next=safe_next(next)
            )
        if rehash:
            conn.execute(
                "UPDATE admin.user_account SET password_hash = %s WHERE id = %s",
                (security.hash_password(password), user.id),
            )
        cookie = store.rotate_session(
            conn,
            ctx.session,
            user_id=user.id,
            stage="password",
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )
    return _redirect("/login/mfa?next=" + quote(safe_next(next)), cookie)


@router.get("/login/mfa")
def mfa_page(ctx: Annotated[Ctx, Depends(password_stage)], next: str | None = None) -> Response:
    with ctx.db.connection() as conn:
        f = factors.factors(conn, ctx.user.id)
    return render(ctx, "mfa.html", factors=f, next=safe_next(next))


def _code_login(ctx: Ctx, code: str, next_path: str, kind: str) -> Response:
    user = ctx.user
    with ctx.db.connection() as conn:
        wait = _throttled(ctx, conn, user.login)
        f = factors.factors(conn, user.id)
        if wait is not None:
            return render(
                ctx,
                "mfa.html",
                status_code=429,
                headers={"Retry-After": str(wait)},
                error=THROTTLED,
                factors=f,
                next=next_path,
            )
        if kind == "totp":
            ok = factors.check_totp(conn, user, _key(ctx), code)
        else:
            ok = factors.use_recovery_code(conn, user, code)
        if not ok:
            _failure(ctx, conn, user.login)
            return render(
                ctx, "mfa.html", status_code=401, error=FACTOR_FAILURE, factors=f, next=next_path
            )
        if kind == "recovery":
            store.audit(
                conn, "recovery.used", user=user, ip=ctx.ip, details={"left": f.recovery_left - 1}
            )
        cookie = _complete_login(ctx, conn, user, kind)
    return _redirect(next_path, cookie)


@router.post("/login/totp")
def login_totp(
    ctx: Annotated[Ctx, Depends(password_stage)],
    code: Annotated[str, Form(max_length=16)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    return _code_login(ctx, code, safe_next(next), "totp")


@router.post("/login/recovery")
def login_recovery(
    ctx: Annotated[Ctx, Depends(password_stage)],
    code: Annotated[str, Form(max_length=32)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    return _code_login(ctx, code, safe_next(next), "recovery")


@router.post("/logout")
def logout(ctx: Annotated[Ctx, Depends(any_stage)]) -> Response:
    assert ctx.session is not None
    with ctx.db.connection() as conn:
        store.revoke_session(conn, ctx.session.id_sha256)
        if ctx.session.user is not None:
            ctx.audit(conn, "logout")
    response = _redirect("/login")
    clear_cookie(response)
    return response


# --------------------------------------------------------------------------------- passkeys


@router.post("/webauthn/login/options")
def passkey_login_options(ctx: Annotated[Ctx, Depends(any_stage)]) -> Response:
    s = ctx.session
    assert s is not None
    if s.stage == "enroll":
        raise Denied(400, "Finish the enrollment first.")
    user = s.user if s.stage in ("password", "full") else None
    with ctx.db.connection() as conn:
        options = factors.authentication_options(conn, s, user, _rp(ctx))
    return Response(options, media_type="application/json")


@router.post("/webauthn/login/verify")
async def passkey_login_verify(
    request: Request, ctx: Annotated[Ctx, Depends(any_stage)]
) -> Response:
    s = ctx.session
    assert s is not None
    body = await _json(request)
    next_path = safe_next(str(body.get("next") or "/"))
    if s.stage == "enroll":
        raise Denied(400, "Finish the enrollment first.")
    expected = s.user if s.stage in ("password", "full") else None
    login = expected.login if expected else None
    with ctx.db.connection() as conn:
        wait = _throttled(ctx, conn, login)
        if wait is not None:
            raise Denied(429, THROTTLED, {"Retry-After": str(wait)})
        try:
            cred = factors.credential_json(body)
            owner = factors.verify_passkey(conn, s, _rp(ctx), cred, user=expected)
            if s.stage == "anonymous" and not owner.passkey_only:
                raise FactorError(FACTOR_FAILURE)
        except SuspectCredential as exc:
            store.audit(
                conn,
                "passkey.suspect",
                session=s,
                ip=ctx.ip,
                target=f"credential:{exc.credential_id}",
                details={"user_id": exc.user_id},
                outcome="denied",
            )
            _failure(ctx, conn, login)
            conn.commit()
            raise Denied(401, str(exc)) from None
        except FactorError:
            _failure(ctx, conn, login)
            conn.commit()
            raise Denied(401, FACTOR_FAILURE) from None
        if s.stage == "full":
            cookie = _step_up(ctx, conn, "passkey")
            return _json_redirect(next_path, cookie)
        cookie = _complete_login(ctx, conn, owner, "passkey" if expected else "passkey_only")
    return _json_redirect(next_path, cookie)


@router.post("/webauthn/register/options")
def passkey_register_options(ctx: Annotated[Ctx, Depends(any_stage)]) -> Response:
    s = ctx.session
    assert s is not None
    _may_register(ctx)
    with ctx.db.connection() as conn:
        options = factors.registration_options(conn, s, ctx.user, _rp(ctx))
    return Response(options, media_type="application/json")


def _may_register(ctx: Ctx) -> None:
    """Registering needs the enroll stage (password already set) or a stepped-up session."""
    s = ctx.session
    assert s is not None
    if s.stage == "enroll":
        with ctx.db.connection() as conn:
            row = conn.execute(
                "SELECT used_at IS NOT NULL AS used FROM admin.enrollment WHERE id = %s",
                (s.enrollment_id,),
            ).fetchone()
        if not row or not row["used"]:
            raise Denied(400, "Set your password first.")
        return
    if s.stage != "full":
        raise Denied(401, "Log in first.")
    if not s.stepped_up:
        raise Denied(403, "Confirm your identity first.")


@router.post("/webauthn/register/verify")
async def passkey_register_verify(
    request: Request, ctx: Annotated[Ctx, Depends(any_stage)]
) -> Response:
    s = ctx.session
    assert s is not None
    _may_register(ctx)
    body = await _json(request)
    name = str(body.get("name") or "passkey")[:64]
    user = ctx.user
    with ctx.db.connection() as conn:
        try:
            cred_id = factors.register_passkey(
                conn, s, user, _rp(ctx), factors.credential_json(body), name
            )
        except FactorError as exc:
            conn.commit()
            raise Denied(400, str(exc)) from None
        ctx.audit(conn, "passkey.add", target=f"credential:{cred_id}", details={"name": name})
        if s.stage == "enroll":
            ctx.audit(conn, "user.enrolled", target=user.login)
            cookie = _complete_login(ctx, conn, user, "enrollment")
            return _json_redirect("/account?enrolled=1", cookie)
    return _json_redirect("/account")


# ---------------------------------------------------------------------------------- step-up


def _step_up(ctx: Ctx, conn: DictConnection, method: str) -> str:
    cookie = store.rotate_session(
        conn,
        ctx.session,
        user_id=ctx.user.id,
        stage="full",
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        mfa=True,
    )
    ctx.audit(conn, "step_up", details={"method": method})
    return cookie


@router.get("/step-up")
def step_up_page(ctx: Annotated[Ctx, Depends(full)], next: str | None = None) -> Response:
    with ctx.db.connection() as conn:
        f = factors.factors(conn, ctx.user.id)
    return render(ctx, "stepup.html", factors=f, next=safe_next(next))


@router.post("/step-up/totp")
def step_up_totp(
    ctx: Annotated[Ctx, Depends(full)],
    code: Annotated[str, Form(max_length=16)] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    user = ctx.user
    with ctx.db.connection() as conn:
        f = factors.factors(conn, user.id)
        wait = _throttled(ctx, conn, user.login)
        if wait is not None:
            return render(
                ctx,
                "stepup.html",
                status_code=429,
                headers={"Retry-After": str(wait)},
                error=THROTTLED,
                factors=f,
                next=safe_next(next),
            )
        if not factors.check_totp(conn, user, _key(ctx), code):
            _failure(ctx, conn, user.login)
            return render(
                ctx,
                "stepup.html",
                status_code=401,
                error=FACTOR_FAILURE,
                factors=f,
                next=safe_next(next),
            )
        cookie = _step_up(ctx, conn, "totp")
    return _redirect(safe_next(next), cookie)


# ------------------------------------------------------------------------------- enrollment


@router.get("/enroll/{token}")
def enroll_link(request: Request, token: str) -> Response:
    ctx = load(request)
    with ctx.db.connection() as conn:
        enrollment = store.lookup_enrollment(conn, token) if len(token) <= 128 else None
        if enrollment is None:
            return render(
                ctx,
                "message.html",
                status_code=404,
                title="Link not valid",
                message="This link is invalid, used or expired. Ask an admin for a new one.",
            )
        if ctx.session is not None:
            store.revoke_session(conn, ctx.session.id_sha256)
        cookie = store.create_session(
            conn,
            user_id=enrollment["user_id"],
            stage="enroll",
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            enrollment_id=enrollment["id"],
        )
    return _redirect("/enroll", cookie)  # drop the token from the address bar


def _enrollment(conn: DictConnection, ctx: Ctx) -> dict[str, Any]:
    assert ctx.session is not None
    row = conn.execute(
        "SELECT id, purpose, used_at, expires_at > now() AS live FROM admin.enrollment "
        "WHERE id = %s",
        (ctx.session.enrollment_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


@router.get("/enroll")
def enroll_page(ctx: Annotated[Ctx, Depends(enroll_stage)]) -> Response:
    with ctx.db.connection() as conn:
        e = _enrollment(conn, ctx)
    return render(
        ctx,
        "enroll.html",
        step="passkey" if e["used_at"] else "password",
        purpose=e["purpose"],
        login=ctx.user.login,
    )


@router.post("/enroll/password")
def enroll_password(
    ctx: Annotated[Ctx, Depends(enroll_stage)],
    password: Annotated[str, Form(max_length=1024)] = "",
    password2: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    user = ctx.user
    problem = security.password_problem(password, login=user.login)
    if problem is None and password != password2:
        problem = "The passwords do not match."
    with ctx.db.connection() as conn:
        e = _enrollment(conn, ctx)
        if e["used_at"] is not None or not e["live"]:
            return render(
                ctx,
                "message.html",
                status_code=400,
                title="Link used",
                message="This enrollment link was already used or expired.",
            )
        if problem:
            return render(
                ctx,
                "enroll.html",
                status_code=400,
                step="password",
                error=problem,
                purpose=e["purpose"],
                login=user.login,
            )
        if e["purpose"] == "reset":
            conn.execute("DELETE FROM admin.webauthn_credential WHERE user_id = %s", (user.id,))
            conn.execute("DELETE FROM admin.totp_secret WHERE user_id = %s", (user.id,))
            conn.execute("DELETE FROM admin.recovery_code WHERE user_id = %s", (user.id,))
        assert ctx.session is not None
        store.revoke_user_sessions(conn, user.id, keep=ctx.session.id_sha256)
        conn.execute(
            "UPDATE admin.user_account SET password_hash = %s, password_changed_at = now() "
            "WHERE id = %s",
            (security.hash_password(password), user.id),
        )
        conn.execute("UPDATE admin.enrollment SET used_at = now() WHERE id = %s", (e["id"],))
        ctx.audit(conn, "password.set", target=user.login, details={"purpose": e["purpose"]})
    return _redirect("/enroll")


# ---------------------------------------------------------------------------------- account


@router.get("/account")
def account_page(ctx: Annotated[Ctx, Depends(full)], enrolled: int = 0) -> Response:
    return _account(ctx, enrolled=bool(enrolled))


def _account(ctx: Ctx, status_code: int = 200, **values: Any) -> Response:
    user = ctx.user
    key = ctx.cfg.secret_key
    with ctx.db.connection() as conn:
        f = factors.factors(conn, user.id)
        passkeys = factors.passkey_rows(conn, user.id)
        pending = factors.pending_totp(conn, user, key) if key else None
        sessions = conn.execute(
            """
            SELECT id_sha256, created_at, last_seen_at, host(ip) AS ip, user_agent
            FROM admin.session
            WHERE user_id = %s AND stage = 'full' AND revoked_at IS NULL AND expires_at > now()
              AND last_seen_at > now() - %s
            ORDER BY last_seen_at DESC
            """,
            (user.id, store.IDLE_TIMEOUT),
        ).fetchall()
    assert ctx.session is not None
    totp_setup = None
    if pending is not None:
        totp_setup = {
            "secret": base64.b32encode(pending).decode().rstrip("="),
            "uri": security.totp_uri(pending, login=user.login, issuer="mdhub admin"),
        }
    return render(
        ctx,
        "account.html",
        status_code=status_code,
        factors=f,
        passkeys=passkeys,
        sessions=[
            {
                **dict(r),
                "ref": bytes(r["id_sha256"]).hex()[:12],
                "current": bytes(r["id_sha256"]) == ctx.session.id_sha256,
            }
            for r in sessions
        ],
        totp_setup=totp_setup,
        totp_available=key is not None,
        **values,
    )


@router.post("/account/password")
def account_password(
    ctx: Annotated[Ctx, Depends(full)],
    current: Annotated[str, Form(max_length=1024)] = "",
    password: Annotated[str, Form(max_length=1024)] = "",
    password2: Annotated[str, Form(max_length=1024)] = "",
) -> Response:
    user = ctx.user
    with ctx.db.connection() as conn:
        wait = _throttled(ctx, conn, user.login)
        if wait is not None:
            raise Denied(429, THROTTLED, {"Retry-After": str(wait)})
        ok, _ = security.verify_password(user.password_hash, current)
        if not ok:
            _failure(ctx, conn, user.login)
            conn.commit()
            return _account(ctx, 400, error="The current password is wrong.")
        problem = security.password_problem(password, login=user.login)
        if problem is None and password != password2:
            problem = "The passwords do not match."
        if problem:
            return _account(ctx, 400, error=problem)
        conn.execute(
            "UPDATE admin.user_account SET password_hash = %s, password_changed_at = now() "
            "WHERE id = %s",
            (security.hash_password(password), user.id),
        )
        assert ctx.session is not None
        revoked = store.revoke_user_sessions(conn, user.id, keep=ctx.session.id_sha256)
        cookie = store.rotate_session(
            conn,
            ctx.session,
            user_id=user.id,
            stage="full",
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            mfa=True,
        )
        ctx.audit(conn, "password.change", target=user.login, details={"sessions_revoked": revoked})
    return _redirect("/account?changed=password", cookie)


@router.post("/account/passkeys/{cred_id}/delete")
def account_passkey_delete(ctx: Annotated[Ctx, Depends(full_step_up)], cred_id: int) -> Response:
    user = ctx.user
    with ctx.db.connection() as conn:
        f = factors.factors(conn, user.id)
        if f.passkeys + int(f.totp) <= 1:
            return _account(ctx, 400, error="Keep at least one passkey or authenticator app.")
        deleted = conn.execute(
            "DELETE FROM admin.webauthn_credential WHERE id = %s AND user_id = %s",
            (cred_id, user.id),
        ).rowcount
        if deleted:
            assert ctx.session is not None
            store.revoke_user_sessions(conn, user.id, keep=ctx.session.id_sha256)
            ctx.audit(conn, "passkey.remove", target=f"credential:{cred_id}")
    return _redirect("/account")


@router.post("/account/totp/begin")
def account_totp_begin(ctx: Annotated[Ctx, Depends(full_step_up)]) -> Response:
    with ctx.db.connection() as conn:
        if factors.begin_totp(conn, ctx.user, _key(ctx)) is None:
            return _account(ctx, 400, error="Remove the current authenticator app first.")
    return _redirect("/account#totp")


@router.post("/account/totp/confirm")
def account_totp_confirm(
    ctx: Annotated[Ctx, Depends(full_step_up)], code: Annotated[str, Form(max_length=16)] = ""
) -> Response:
    with ctx.db.connection() as conn:
        if not factors.confirm_totp(conn, ctx.user, _key(ctx), code):
            return _account(ctx, 400, error="The code did not match. Try the next one.")
        ctx.audit(conn, "totp.enable")
    return _redirect("/account")


@router.post("/account/totp/remove")
def account_totp_remove(ctx: Annotated[Ctx, Depends(full_step_up)]) -> Response:
    user = ctx.user
    with ctx.db.connection() as conn:
        f = factors.factors(conn, user.id)
        if f.totp and f.passkeys == 0:
            return _account(ctx, 400, error="Add a passkey before removing the authenticator app.")
        if factors.remove_totp(conn, user):
            assert ctx.session is not None
            store.revoke_user_sessions(conn, user.id, keep=ctx.session.id_sha256)
            ctx.audit(conn, "totp.remove")
    return _redirect("/account")


@router.post("/account/recovery-codes")
def account_recovery_codes(ctx: Annotated[Ctx, Depends(full_step_up)]) -> Response:
    with ctx.db.connection() as conn:
        codes = factors.new_recovery_codes(conn, ctx.user)
        ctx.audit(conn, "recovery.generate", details={"count": len(codes)})
    return _account(ctx, recovery_codes=codes)


@router.post("/account/passkey-only")
def account_passkey_only(
    ctx: Annotated[Ctx, Depends(full_step_up)], enabled: Annotated[str, Form()] = ""
) -> Response:
    on = enabled == "1"
    with ctx.db.connection() as conn:
        conn.execute(
            "UPDATE admin.user_account SET passkey_only = %s WHERE id = %s", (on, ctx.user.id)
        )
        ctx.audit(conn, "passkey_only.set", details={"enabled": on})
    return _redirect("/account")


@router.post("/account/sessions/{ref}/revoke")
def account_session_revoke(ctx: Annotated[Ctx, Depends(full)], ref: str) -> Response:
    with ctx.db.connection() as conn:
        n = conn.execute(
            "UPDATE admin.session SET revoked_at = now() WHERE user_id = %s "
            "AND revoked_at IS NULL AND left(encode(id_sha256, 'hex'), 12) = %s",
            (ctx.user.id, ref[:12]),
        ).rowcount
        if n:
            ctx.audit(conn, "session.revoke", target=f"session:{ref[:12]}")
    return _redirect("/account")
