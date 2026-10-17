"""Admin console users on the server (ADMIN_DESIGN §5 "Bootstrap").

Registered as ``mdhub admin <command>`` by :func:`mdhub.cli.register_extensions`:

* ``create-user LOGIN --role viewer|operator|admin [--name TEXT]`` - new user; prints a
  one-time enrollment link (15 minutes) on stdout, where the user sets a password and
  registers a passkey,
* ``list`` - users with role, factors and state (never hashes),
* ``reset LOGIN`` - new one-time link that replaces the user's password and factors; logs the
  user out everywhere,
* ``unlock LOGIN`` - lift a lockout after repeated failed logins,
* ``disable LOGIN`` / ``enable LOGIN`` - block or allow the account (disable logs it out),
* ``rotate-key`` - re-encrypt the TOTP secrets with a new ``MDHUB_ADMIN_SECRET_KEY`` (printed
  once on stdout); put it into ``.env`` and restart the console.

Every change is written to ``admin.audit_log`` with the OS user as actor.
"""

from __future__ import annotations

import getpass
import secrets
from typing import Annotated, Literal

import typer

from mdhub.admin import factors, security, store
from mdhub.admin.web import AdminConfig
from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database, DictConnection

name = "admin"
app = typer.Typer(help="Admin console users: create, reset, unlock, disable.", no_args_is_help=True)


def _settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None


def _database(settings: Settings) -> Database:
    try:
        return Database.from_settings(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def _origin(settings: Settings) -> str:
    cfg = AdminConfig.from_settings(settings)
    if cfg.rp is None:
        typer.echo(
            "set MDHUB_ADMIN_DOMAIN (and MDHUB_ADMIN_ORIGIN for a non-https origin)", err=True
        )
        raise typer.Exit(2)
    return cfg.rp.origin


def _user(conn: DictConnection, login: str) -> store.User:
    user = store.user_by_login(conn, login)
    if user is None:
        typer.echo(f"no user {login!r}", err=True)
        raise typer.Exit(1)
    return user


def _link(settings: Settings, login: str, purpose: Literal["invite", "reset"]) -> None:
    origin = _origin(settings)
    db = _database(settings)
    try:
        with db.connection() as conn:
            user = _user(conn, login)
            token = store.create_enrollment(conn, user.id, purpose, _actor())
            if purpose == "reset":
                store.revoke_user_sessions(conn, user.id)
            store.audit(
                conn, f"user.{purpose}", login=_actor(), target=user.login, details={"via": "cli"}
            )
    finally:
        db.close()
    typer.echo(f"one-time {purpose} link for {login} (valid 15 minutes):", err=True)
    typer.echo(f"{origin}/enroll/{token}")


@app.command("create-user")
def create_user_cmd(
    login: Annotated[str, typer.Argument(metavar="LOGIN")],
    role: Annotated[str, typer.Option(help="viewer, operator or admin.")] = "viewer",
    display_name: Annotated[str, typer.Option("--name", help="Display name.")] = "",
) -> None:
    """Create a user and print a one-time enrollment link."""
    settings = _settings()
    origin = _origin(settings)
    db = _database(settings)
    try:
        with db.connection() as conn:
            try:
                user = store.create_user(
                    conn, login, role, display_name=display_name, created_by=_actor()
                )
            except store.UserError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
            token = store.create_enrollment(conn, user.id, "invite", _actor())
            store.audit(
                conn,
                "user.create",
                login=_actor(),
                target=user.login,
                details={"role": user.role, "via": "cli"},
            )
    finally:
        db.close()
    typer.echo(f"created {user.login} ({user.role}); enrollment link (valid 15 minutes):", err=True)
    typer.echo(f"{origin}/enroll/{token}")


@app.command("list")
def list_cmd() -> None:
    """List users."""
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT id, login, role, disabled_at, password_hash IS NOT NULL AS pw "
                "FROM admin.user_account ORDER BY login"
            ).fetchall()
            for r in rows:
                f = factors.factors(conn, int(r["id"]))
                locked = store.login_failures(conn, r["login"]) >= store.LOCK_THRESHOLD
                state = "disabled" if r["disabled_at"] else ("locked" if locked else "active")
                typer.echo(
                    f"{r['login']:<24} {r['role']:<9} {state:<9} "
                    f"password={'yes' if r['pw'] else 'no'} passkeys={f.passkeys} "
                    f"totp={'yes' if f.totp else 'no'} recovery={f.recovery_left}"
                )
    finally:
        db.close()
    if not rows:
        typer.echo(
            "no users (create one with `mdhub admin create-user LOGIN --role admin`)", err=True
        )


@app.command("reset")
def reset_cmd(login: Annotated[str, typer.Argument(metavar="LOGIN")]) -> None:
    """Print a one-time reset link (new password and passkey); logs the user out."""
    _link(_settings(), login, "reset")


def _simple(login: str, action: str) -> None:
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            user = _user(conn, login)
            if action == "unlock":
                store.record_attempt(conn, user.login, None, "unlocked")
            elif action == "disable":
                conn.execute(
                    "UPDATE admin.user_account SET disabled_at = coalesce(disabled_at, now()) "
                    "WHERE id = %s",
                    (user.id,),
                )
                store.revoke_user_sessions(conn, user.id)
            else:
                conn.execute(
                    "UPDATE admin.user_account SET disabled_at = NULL WHERE id = %s", (user.id,)
                )
            store.audit(
                conn, f"user.{action}", login=_actor(), target=user.login, details={"via": "cli"}
            )
    finally:
        db.close()
    typer.echo(f"{action}d {login}" if action != "unlock" else f"unlocked {login}")


@app.command("unlock")
def unlock_cmd(login: Annotated[str, typer.Argument(metavar="LOGIN")]) -> None:
    """Lift the lockout of LOGIN."""
    _simple(login, "unlock")


@app.command("disable")
def disable_cmd(login: Annotated[str, typer.Argument(metavar="LOGIN")]) -> None:
    """Disable LOGIN and end its sessions."""
    _simple(login, "disable")


@app.command("enable")
def enable_cmd(login: Annotated[str, typer.Argument(metavar="LOGIN")]) -> None:
    """Enable a disabled LOGIN."""
    _simple(login, "enable")


@app.command("rotate-key")
def rotate_key_cmd(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Re-encrypt TOTP secrets with a new MDHUB_ADMIN_SECRET_KEY (printed on stdout)."""
    settings = _settings()
    try:
        raw = settings.admin_secret_key
        old = security.parse_secret_key(raw.get_secret_value() if raw else None)
    except security.SecretKeyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    if not yes:
        typer.confirm(
            "Re-encrypt all TOTP secrets now? Save the printed key in .env right away", abort=True
        )
    new = secrets.token_bytes(32)
    db = _database(settings)
    try:
        with db.connection() as conn:
            try:
                n = factors.reencrypt_totp(conn, old, new)
            except security.SecretKeyError as exc:
                typer.echo(f"error: {exc}; nothing changed", err=True)
                raise typer.Exit(1) from None
            store.audit(conn, "secret_key.rotate", login=_actor(), details={"totp_secrets": n})
    finally:
        db.close()
    typer.echo(
        f"re-encrypted {n} TOTP secret(s). New MDHUB_ADMIN_SECRET_KEY (set it in .env "
        "and restart the admin service; the old key no longer works):",
        err=True,
    )
    typer.echo(new.hex())
