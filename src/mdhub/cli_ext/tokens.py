"""Read-API consumer tokens (``admin.api_token``, ADMIN_DESIGN §3.3).

Registered as ``mdhub tokens <command>`` by :func:`mdhub.cli.register_extensions`:

* ``create NAME [--scope read|snapshots|export ...] [--expires YYYY-MM-DD | --expires-days N]``
  - new ``mdh_...`` token; the full token is printed **once** on stdout (everything else goes
  to stderr, so ``mdhub tokens create x > token.txt`` captures only the token),
* ``list [--all]`` - live tokens (``--all``: also revoked ones) with prefix, scopes, expiry and
  last use; never the token or its digest. ``MDHUB_API_TOKENS`` entries are listed by name,
* ``revoke NAME|ID`` - revoke the live token; the API stops accepting it within 30 s.
"""

from __future__ import annotations

import getpass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

import typer

from mdhub.api.auth import SCOPES, TokenError, create_token, list_tokens, revoke_token
from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database

name = "tokens"
app = typer.Typer(help="Read-API consumer tokens: create, list, revoke.", no_args_is_help=True)


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


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return str(value)


def _expiry(expires: str | None, expires_days: int | None) -> datetime | None:
    if expires is not None and expires_days is not None:
        typer.echo("use either --expires or --expires-days", err=True)
        raise typer.Exit(2)
    if expires_days is not None:
        if expires_days < 1:
            typer.echo("--expires-days must be >= 1", err=True)
            raise typer.Exit(2)
        return datetime.now(UTC) + timedelta(days=expires_days)
    if expires is None:
        return None
    try:
        day = date.fromisoformat(expires)
    except ValueError:
        typer.echo(f"--expires: expected YYYY-MM-DD, got {expires!r}", err=True)
        raise typer.Exit(2) from None
    return datetime.combine(day, time(0), tzinfo=UTC)


@app.command("create")
def create_cmd(
    token_name: Annotated[str, typer.Argument(metavar="NAME", help="Consumer name.")],
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", "-s", help=f"Scope ({', '.join(SCOPES)}); repeatable."),
    ] = None,
    expires: Annotated[str | None, typer.Option(help="Expiry date YYYY-MM-DD (00:00 UTC).")] = None,
    expires_days: Annotated[int | None, typer.Option(help="Expire after N days.")] = None,
    created_by: Annotated[
        str | None, typer.Option(help="Recorded creator (default: OS user).")
    ] = None,
) -> None:
    """Create a named token and print it once."""
    settings = _settings()
    expires_at = _expiry(expires, expires_days)
    db = _database(settings)
    try:
        with db.connection() as conn:
            token, row = create_token(
                conn,
                token_name,
                scope or ["read"],
                expires_at=expires_at,
                created_by=created_by or getpass.getuser(),
            )
    except TokenError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    finally:
        db.close()
    typer.echo(
        f"created token {row['name']} (id {row['id']}, scopes {_fmt(row['scopes'])}, "
        f"expires {_fmt(row['expires_at'])}). It is shown only now:",
        err=True,
    )
    typer.echo(token)


@app.command("list")
def list_cmd(
    show_all: Annotated[bool, typer.Option("--all", help="Include revoked tokens.")] = False,
) -> None:
    """List tokens (never the secret)."""
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = list_tokens(conn, include_revoked=show_all)
    finally:
        db.close()
    columns = [
        "id",
        "name",
        "prefix",
        "scopes",
        "created_at",
        "expires_at",
        "revoked_at",
        "last_used_at",
        "last_used_ip",
    ]
    cells = [[_fmt(r.get(c)) for c in columns] for r in rows]
    widths = [max([len(c), *(len(row[i]) for row in cells)]) for i, c in enumerate(columns)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip())
    for cell_row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(cell_row, widths, strict=True)).rstrip())
    env_names = [n for n, _ in settings.api_token_entries()]
    if env_names:
        typer.echo(f"MDHUB_API_TOKENS: {', '.join(env_names)} (all scopes)")


@app.command("revoke")
def revoke_cmd(
    name_or_id: Annotated[str, typer.Argument(metavar="NAME|ID", help="Token name or id.")],
) -> None:
    """Revoke a live token (effective in the API within 30 s)."""
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = revoke_token(conn, name_or_id)
    finally:
        db.close()
    if not rows:
        typer.echo(f"no live token {name_or_id!r}", err=True)
        raise typer.Exit(1)
    for r in rows:
        typer.echo(f"revoked {r['name']} (id {r['id']}, {r['prefix']}...)")
