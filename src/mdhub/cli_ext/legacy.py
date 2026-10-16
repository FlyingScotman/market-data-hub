"""``mdhub legacy``: verbatim import of the legacy SQLite stores and its verification.

mdhub legacy import --md market-data.sqlite3 --bc bond_catalog.sqlite3 [--drop-existing]
mdhub legacy verify [--md PATH] [--bc PATH]
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database
from mdhub.legacy import LEGACY_SCHEMAS
from mdhub.legacy.import_sqlite import LegacyImportError, import_sqlite
from mdhub.legacy.verify import latest_import, verify_schema

name = "legacy"
app = typer.Typer(
    help="Legacy SQLite stores: verbatim import into legacy_md / legacy_bc and verification.",
    no_args_is_help=True,
)

_MdOpt = Annotated[
    Path | None,
    typer.Option("--md", help="market-data.sqlite3 (-> schema legacy_md).", dir_okay=False),
]
_BcOpt = Annotated[
    Path | None,
    typer.Option("--bc", help="bond_catalog.sqlite3 (-> schema legacy_bc).", dir_okay=False),
]


def _database() -> Database:
    try:
        return Database.from_settings(Settings())
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None


def _say(msg: str) -> None:
    typer.echo(msg)


@app.command("import")
def import_cmd(
    md: _MdOpt = None,
    bc: _BcOpt = None,
    drop_existing: Annotated[
        bool,
        typer.Option("--drop-existing", help="Replace existing tables in the target schema."),
    ] = False,
) -> None:
    """Copy every table and row of the SQLite file(s) into legacy_md / legacy_bc."""
    sources = [(LEGACY_SCHEMAS[k], p) for k, p in (("md", md), ("bc", bc)) if p is not None]
    if not sources:
        typer.echo("nothing to import: pass --md and/or --bc", err=True)
        raise typer.Exit(2)
    for _schema, path in sources:
        if not path.is_file():
            typer.echo(f"not a file: {path}", err=True)
            raise typer.Exit(2)
    db = _database()
    try:
        for schema, path in sources:
            try:
                res = import_sqlite(db, path, schema, drop_existing=drop_existing, progress=_say)
            except LegacyImportError as exc:
                typer.echo(f"import into {schema} failed: {exc}", err=True)
                raise typer.Exit(1) from None
            skipped = [c for c in res.constraints if c["status"] == "skipped"]
            typer.echo(
                f"{schema}: import #{res.import_id}: {len(res.table_counts)} tables, "
                f"{res.total_rows} rows in {res.duration_s:.1f}s "
                f"(sha256 {res.source_sha256[:12]}, "
                f"snapshot {'yes' if res.snapshot_found else 'no'}, "
                f"{len(res.constraints) - len(skipped)} constraints/indexes created, "
                f"{len(skipped)} skipped, {len(res.tagged_columns)} mixed-type columns)"
            )
            for c in skipped:
                typer.echo(
                    f"  skipped {c['kind']} {c['sqlite_name'] or ''} on {c['table']}"
                    f"({', '.join(c['columns'])}): {c.get('detail', '')}"
                )
        typer.echo("run `mdhub legacy verify` to compare contents")
    finally:
        db.close()


@app.command("verify")
def verify_cmd(
    md: _MdOpt = None,
    bc: _BcOpt = None,
    no_record: Annotated[
        bool, typer.Option("--no-record", help="Do not update ops.legacy_import.")
    ] = False,
) -> None:
    """Compare row counts and content checksums SQLite vs PostgreSQL vs SNAPSHOT.json."""
    db = _database()
    try:
        explicit = {LEGACY_SCHEMAS[k]: p for k, p in (("md", md), ("bc", bc)) if p is not None}
        if explicit:
            schemas = list(explicit)
        else:
            with db.connection() as conn:
                schemas = [s for s in LEGACY_SCHEMAS.values() if latest_import(conn, s)]
            if not schemas:
                typer.echo("no legacy import recorded; run `mdhub legacy import` first", err=True)
                raise typer.Exit(1)
        failed = False
        for schema in schemas:
            try:
                res = verify_schema(
                    db, schema, sqlite_path=explicit.get(schema), record=not no_record
                )
            except LegacyImportError as exc:
                typer.echo(f"{schema}: {exc}", err=True)
                failed = True
                continue
            for t in res.tables:
                snap = "-" if t.snapshot_rows is None else str(t.snapshot_rows)
                typer.echo(
                    f"{schema}.{t.table:<36} sqlite={t.sqlite_rows} pg={t.pg_rows} "
                    f"snapshot={snap} checksum={(t.pg_checksum or '-')[:16]} "
                    f"{'OK' if t.ok else 'MISMATCH'}"
                )
            for w in res.warnings:
                typer.echo(f"{schema}: warning: {w}")
            for p in res.failures:
                typer.echo(f"{schema}: MISMATCH {p}", err=True)
            typer.echo(
                f"{schema}: import #{res.import_id} {'verified OK' if res.ok else 'FAILED'} "
                f"({len(res.tables)} tables, {res.duration_s:.1f}s)"
            )
            failed = failed or not res.ok
        if failed:
            raise typer.Exit(1)
    finally:
        db.close()
