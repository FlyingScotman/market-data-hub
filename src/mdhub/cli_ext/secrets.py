"""Source credentials store on the server (ADMIN_DESIGN §9, :mod:`mdhub.credentials`).

Registered as ``mdhub secrets <command>`` by :func:`mdhub.cli.register_extensions`. Run them
in the worker container (``docker compose exec worker mdhub secrets ...``): only the worker
has ``MDHUB_SECRETS_PRIVATE_KEY``.

* ``init-key`` - print a new ``MDHUB_SECRETS_PRIVATE_KEY=...`` line for the worker's ``.env``
  (stdout); the worker publishes the public key when it starts,
* ``import-env [--replace]`` - seal the credentials set in this environment into the store
  (names already in the store are kept unless ``--replace``); afterwards those lines can be
  deleted from ``.env``,
* ``rotate-key [--yes]`` - re-encrypt every stored value under a new key pair; prints the new
  key (stdout): put it into ``.env`` and restart the worker right away,
* ``list`` - every known credential with its state; never a value.

Changes are written to ``admin.audit_log`` with the OS user as actor (names, never values).
"""

from __future__ import annotations

import getpass
from typing import Annotated

import typer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from mdhub.admin import store
from mdhub.config import ConfigError, Settings
from mdhub.credentials import (
    REGISTRY,
    CredentialError,
    CredentialStore,
    env_names,
    generate_private_key,
    key_id,
    open_sealed,
    parse_private_key,
    public_bytes,
    seal,
)
from mdhub.db.pool import Database

name = "secrets"
app = typer.Typer(
    help="Source credentials store: init-key, import-env, rotate-key, list.", no_args_is_help=True
)


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


def _key(settings: Settings) -> X25519PrivateKey:
    raw = settings.secrets_private_key
    if raw is None:
        typer.echo("MDHUB_SECRETS_PRIVATE_KEY is not set (mdhub secrets init-key)", err=True)
        raise typer.Exit(2)
    try:
        return parse_private_key(raw.get_secret_value())
    except CredentialError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


@app.command("init-key")
def init_key_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Print a key even though one is set.")
    ] = False,
) -> None:
    """Print a new MDHUB_SECRETS_PRIVATE_KEY line for the worker .env."""
    if _settings().secrets_private_key is not None and not force:
        typer.echo(
            "MDHUB_SECRETS_PRIVATE_KEY is already set; a new key orphans the stored values "
            "(use rotate-key, or --force)",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(
        "Add this line to the worker's .env only, keep a copy with the .env backup, and restart "
        "the worker:",
        err=True,
    )
    typer.echo(f"MDHUB_SECRETS_PRIVATE_KEY={generate_private_key()}")


@app.command("import-env")
def import_env_cmd(
    replace: Annotated[
        bool, typer.Option("--replace", help="Overwrite values already in the store.")
    ] = False,
) -> None:
    """Seal the credentials of this environment (.env) into the store."""
    settings = _settings()
    private = _key(settings)
    db = _database(settings)
    try:
        CredentialStore(db, private).publish(env_names(settings))
        kid, public = key_id(public_bytes(private)), public_bytes(private)
        imported: list[str] = []
        with db.connection() as conn:
            have = {
                r["name"]
                for r in conn.execute("SELECT name FROM admin.source_credential").fetchall()
            }
            for spec in REGISTRY.values():
                value = getattr(settings, spec.field)
                if value is None or (spec.name in have and not replace):
                    continue
                conn.execute(
                    """
                    INSERT INTO admin.source_credential (name, ciphertext, key_id, set_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        ciphertext = EXCLUDED.ciphertext, key_id = EXCLUDED.key_id,
                        set_by = EXCLUDED.set_by, set_at = now(), verified_at = NULL,
                        verify_result = NULL
                    """,
                    (spec.name, seal(public, spec.name, value.get_secret_value()), kid, _actor()),
                )
                imported.append(spec.name)
            if imported:
                store.audit(
                    conn,
                    "credential.import_env",
                    login=_actor(),
                    details={"names": imported, "key_id": kid},
                )
    finally:
        db.close()
    for n in imported:
        typer.echo(f"imported {n}")
    typer.echo(
        f"{len(imported)} imported; those lines can now be deleted from .env "
        "(restart the worker after editing .env)",
        err=True,
    )


@app.command("rotate-key")
def rotate_key_cmd(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Re-encrypt every stored value under a new key (printed on stdout)."""
    settings = _settings()
    old = _key(settings)
    old_kid = key_id(public_bytes(old))
    if not yes:
        typer.confirm(
            "Re-encrypt all stored credentials now? Put the printed key into the worker .env "
            "and restart the worker right away",
            abort=True,
        )
    new_text = generate_private_key()
    new = parse_private_key(new_text)
    new_kid, new_public = key_id(public_bytes(new)), public_bytes(new)
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT name, key_id, ciphertext FROM admin.source_credential FOR UPDATE"
            ).fetchall()
            skipped = [r["name"] for r in rows if r["key_id"] != old_kid]
            for r in rows:
                if r["key_id"] != old_kid:
                    continue
                try:
                    value = open_sealed(old, r["name"], old_kid, bytes(r["ciphertext"]))
                except CredentialError as exc:
                    typer.echo(f"error: {exc}; nothing changed", err=True)
                    raise typer.Exit(1) from None
                conn.execute(
                    "UPDATE admin.source_credential SET ciphertext = %s, key_id = %s "
                    "WHERE name = %s",
                    (seal(new_public, r["name"], value), new_kid, r["name"]),
                )
            conn.execute("DELETE FROM admin.secret_key_public")
            conn.execute(
                "INSERT INTO admin.secret_key_public (key_id, public_key, env_names) "
                "VALUES (%s, %s, %s)",
                (new_kid, new_public, env_names(settings)),
            )
            store.audit(
                conn,
                "credential.rotate_key",
                login=_actor(),
                details={
                    "old_key_id": old_kid,
                    "new_key_id": new_kid,
                    "count": len(rows) - len(skipped),
                },
            )
    finally:
        db.close()
    for n in skipped:
        typer.echo(f"warning: {n} was sealed to another key and must be re-entered", err=True)
    typer.echo(
        f"re-encrypted {len(rows) - len(skipped)} credential(s) under key {new_kid}. Replace "
        "MDHUB_SECRETS_PRIVATE_KEY in the worker .env with this line and restart the worker "
        "(until then jobs fall back to .env):",
        err=True,
    )
    typer.echo(f"MDHUB_SECRETS_PRIVATE_KEY={new_text}")


@app.command("list")
def list_cmd() -> None:
    """Known credentials and their state (names only, never values)."""
    settings = _settings()
    kid = key_id(public_bytes(_key(settings))) if settings.secrets_private_key else None
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = {
                r["name"]: r
                for r in conn.execute(
                    "SELECT name, key_id, set_by, set_at, verify_result "
                    "FROM admin.source_credential"
                ).fetchall()
            }
    finally:
        db.close()
    in_env = set(env_names(settings))
    width = max(len(n) for n in REGISTRY)
    for spec in REGISTRY.values():
        row = rows.get(spec.name)
        env = spec.name in in_env
        state = ("both" if env else "console") if row else (".env" if env else "not set")
        extra = ""
        if row:
            if row["key_id"] != kid:
                state += " (re-enter: other key)"
            extra = f"  {row['set_by'] or '-'} {row['set_at']:%Y-%m-%d %H:%M}"
            extra += f"  check={row['verify_result'] or '-'}"
        typer.echo(f"{spec.name:<{width}}  {state}{extra}")
