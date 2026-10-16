"""``mdhub legacy``: verbatim import of the legacy SQLite stores and its verification.

mdhub legacy import --md market-data.sqlite3 --bc bond_catalog.sqlite3 [--drop-existing]
mdhub legacy verify [--md PATH] [--bc PATH]
mdhub legacy map-md [--only fixings,curves,...] [--json]
mdhub legacy map-bc [--only bonds,curated_formulas] [--json]
mdhub legacy andromeda-cny FILE.xlsx|FILE.csv ... [--exported-at ISO] [--dry-run] [--json]
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database
from mdhub.legacy import LEGACY_SCHEMAS
from mdhub.legacy.andromeda_cny import AndromedaImportError, import_andromeda_cny
from mdhub.legacy.import_sqlite import LegacyImportError, import_sqlite
from mdhub.legacy.map_bc import TARGETS as BC_TARGETS
from mdhub.legacy.map_bc import map_bc
from mdhub.legacy.map_md import TARGETS, TargetReport, map_md
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


@app.command("map-md")
def map_md_cmd(
    only: Annotated[
        str | None,
        typer.Option("--only", help=f"Comma list of targets ({', '.join(TARGETS)}); default all."),
    ] = None,
    allow_unverified: Annotated[
        bool,
        typer.Option("--allow-unverified", help="Map even if `legacy verify` did not pass."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
) -> None:
    """Map legacy_md into md.* / ref.cbr_meeting (idempotent; live rows always win)."""
    targets = [t.strip() for t in only.split(",") if t.strip()] if only else None
    db = _database()
    try:
        try:
            res = map_md(db, targets=targets, allow_unverified=allow_unverified, progress=_say)
        except LegacyImportError as exc:
            typer.echo(f"map-md: {exc}", err=True)
            raise typer.Exit(1) from None
        _print_reports(res.run_id, res.ok, res.targets, as_json, "map-md")
        if not res.ok:
            raise typer.Exit(1)
    finally:
        db.close()


def _print_reports(
    run_id: int, ok: bool, targets: list[TargetReport], as_json: bool, cmd: str
) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                {"run_id": run_id, "targets": {t.target: t.as_dict() for t in targets}},
                indent=2,
                default=str,
                ensure_ascii=False,
            )
        )
    else:
        for t in targets:
            for ex in t.mismatch_examples:
                typer.echo(f"  {t.target} live mismatch: {ex}")
            if t.notes:
                typer.echo(f"  {t.target} notes: {json.dumps(t.notes, default=str)[:2000]}")
    typer.echo(f"legacy {cmd} run {run_id}: {'OK' if ok else 'PARTIAL'}")


@app.command("map-bc")
def map_bc_cmd(
    only: Annotated[
        str | None,
        typer.Option(
            "--only", help=f"Comma list of targets ({', '.join(BC_TARGETS)}); default all."
        ),
    ] = None,
    allow_unverified: Annotated[
        bool,
        typer.Option("--allow-unverified", help="Map even if `legacy verify` did not pass."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
) -> None:
    """Map legacy_bc into ref.* for SECIDs absent from the live bonds_static load (+ curated
    formulas for all bonds)."""
    targets = [t.strip() for t in only.split(",") if t.strip()] if only else None
    db = _database()
    try:
        try:
            res = map_bc(db, targets=targets, allow_unverified=allow_unverified, progress=_say)
        except LegacyImportError as exc:
            typer.echo(f"map-bc: {exc}", err=True)
            raise typer.Exit(1) from None
        _print_reports(res.run_id, res.ok, res.targets, as_json, "map-bc")
        if not res.ok:
            raise typer.Exit(1)
    finally:
        db.close()


@app.command("andromeda-cny")
def andromeda_cny_cmd(
    files: Annotated[
        list[Path],
        typer.Argument(help="andromeda_cny_swaps.xlsx and/or the CSV exports.", dir_okay=False),
    ],
    exported_at: Annotated[
        str | None,
        typer.Option(
            "--exported-at",
            help="Export time with offset (e.g. 2026-10-16T18:05:15+03:00); default: the "
            "workbook's docProps modified time. Required for CSV-only imports.",
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Parse and check only; write nothing.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON.")] = False,
) -> None:
    """Import the owner's Andromeda CNY swap history export (cny_rusfar_ois, cny_ruonia_xccy)
    into md.curve_snapshot / md.curve_quote (source andromeda; idempotent, revisions to
    ops.row_history)."""
    for f in files:
        if not f.is_file():
            typer.echo(f"not a file: {f}", err=True)
            raise typer.Exit(2)
    when: datetime | None = None
    if exported_at:
        try:
            when = datetime.fromisoformat(exported_at)
        except ValueError:
            typer.echo(f"--exported-at: not an ISO 8601 time: {exported_at!r}", err=True)
            raise typer.Exit(2) from None
    db = _database()
    try:
        try:
            rep = import_andromeda_cny(db, files, exported_at=when, dry_run=dry_run, progress=_say)
        except AndromedaImportError as exc:
            typer.echo(f"andromeda-cny: {exc}", err=True)
            raise typer.Exit(1) from None
        data = rep.as_dict()
        if as_json:
            typer.echo(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        else:
            typer.echo(
                f"export time {data['exported_at_msk']} ({data['exported_at_basis']}); "
                f"snapshots {data['snapshots']}; quotes {data['quotes']}"
            )
            for ds, st in data["datasets"].items():
                typer.echo(f"  {ds}: flags {st['flags']}")
                for j in st["jumps_gt_1000bp"]:
                    typer.echo(f"    jump > 1000 bp: {j}")
        typer.echo(
            "andromeda-cny " + ("dry run (nothing written)" if dry_run else f"run {rep.run_id}: OK")
        )
    finally:
        db.close()
