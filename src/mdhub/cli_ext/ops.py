"""Operations commands: coverage, gaps, backfill, backup, restore.

Registered as ``mdhub ops <command>`` by :func:`mdhub.cli.register_extensions`
(``toplevel = True`` asks the CLI to also expose them as ``mdhub <command>`` once it supports
flattening extension groups).

* ``coverage`` - ``ops.v_coverage`` (refreshed by the ``quality`` job),
* ``gaps`` - open gaps (``ops.v_open_gaps``), per dataset or as a per-dataset summary,
* ``static-terms --from D --to D [--json FILE]`` - per date, the 1.4 bond terms missing in
  the static of the current ``eod_final`` cut (:mod:`mdhub.snapshot.terms_coverage`),
* ``backfill DATASET --from D --to D [--now]`` - queue ``ops.backfill_task`` rows (default)
  or run the dataset's backfill job inline now (recorded in ``ops.job_run``),
* ``backup [--keep N] [--dir PATH]`` - run ``maintenance`` with ``mode=backup``,
* ``restore FILE [--clean] [--jobs N] [--yes]`` - ``pg_restore`` a custom-format dump.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from mdhub.config import ConfigError, Settings
from mdhub.core.bizday import DbCalendar
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import JobParamError, UnknownJobError, discover_jobs, run_job
from mdhub.snapshot.terms_coverage import TERM_FIELDS, terms_coverage_report

name = "ops"
toplevel = True
app = typer.Typer(
    help="Operations: coverage, gaps, backfill, backup, restore.", no_args_is_help=True
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


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        typer.echo(f"{option}: expected YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(2) from None


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime | date):
        text = value.isoformat()
        return text[:19] if "T" in text else text
    return str(value)


def _table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        return
    cells = [[_fmt(r.get(c)) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip())
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())


@app.command("coverage")
def coverage_cmd(
    stale: Annotated[bool, typer.Option("--stale", help="Only stale datasets.")] = False,
) -> None:
    """Per dataset: first/last date, rows, staleness, gaps, tasks, last run of its job."""
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT dataset, first_date, last_date, row_count, expected_latest, is_stale,
                       open_gaps, unfillable_gaps, not_expected_gaps, open_tasks, backfill_job,
                       last_run_status, last_run_at, problem, checked_at
                FROM ops.v_coverage
                WHERE (NOT %s OR is_stale)
                ORDER BY dataset
                """,
                (stale,),
            ).fetchall()
    finally:
        db.close()
    if not rows:
        typer.echo("no coverage data yet - run `mdhub run quality`")
        return
    _table(
        [dict(r) for r in rows],
        [
            "dataset",
            "first_date",
            "last_date",
            "row_count",
            "expected_latest",
            "is_stale",
            "open_gaps",
            "unfillable_gaps",
            "not_expected_gaps",
            "open_tasks",
            "last_run_status",
            "last_run_at",
            "problem",
        ],
    )
    typer.echo(f"checked at {_fmt(max(r['checked_at'] for r in rows))} UTC")


@app.command("gaps")
def gaps_cmd(
    dataset: Annotated[
        str | None, typer.Option("--dataset", "-d", help="Only this dataset.")
    ] = None,
    summary: Annotated[bool, typer.Option("--summary", help="One line per dataset.")] = False,
    limit: Annotated[int, typer.Option(help="Maximum rows to print.")] = 200,
) -> None:
    """List open data gaps (with the backfill task covering each day)."""
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            if summary:
                rows = conn.execute(
                    """
                    SELECT dataset, count(*) AS gaps, count(DISTINCT gap_date) AS days,
                           min(gap_date) AS first_gap, max(gap_date) AS last_gap,
                           count(*) FILTER (WHERE task_status IN ('pending', 'running'))
                               AS queued,
                           count(*) FILTER (WHERE task_status = 'failed') AS failed
                    FROM ops.v_open_gaps
                    WHERE (%s::text IS NULL OR dataset = %s)
                    GROUP BY dataset ORDER BY dataset
                    """,
                    (dataset, dataset),
                ).fetchall()
                columns = ["dataset", "gaps", "days", "first_gap", "last_gap", "queued", "failed"]
            else:
                rows = conn.execute(
                    """
                    SELECT dataset, key::text AS key, gap_date, task_id, task_status,
                           task_attempts, note
                    FROM ops.v_open_gaps
                    WHERE (%s::text IS NULL OR dataset = %s)
                    ORDER BY dataset, gap_date, key::text
                    LIMIT %s
                    """,
                    (dataset, dataset, limit),
                ).fetchall()
                columns = [
                    "dataset",
                    "key",
                    "gap_date",
                    "task_id",
                    "task_status",
                    "task_attempts",
                    "note",
                ]
    finally:
        db.close()
    if not rows:
        typer.echo("no open gaps")
        return
    _table([dict(r) for r in rows], columns)


@app.command("static-terms")
def static_terms_cmd(
    date_from: Annotated[str, typer.Option("--from", help="First business date, YYYY-MM-DD.")],
    date_to: Annotated[str, typer.Option("--to", help="Last business date, YYYY-MM-DD.")],
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="Reference static snapshot id (default: the newest)."),
    ] = None,
    json_out: Annotated[
        Path | None,
        typer.Option("--json", help="Also write the full report (with secids) to this file."),
    ] = None,
) -> None:
    """Per date: 1.4 floater terms of today's static missing in the static of the current
    eod_final cut (read only)."""
    start, end = _parse_date(date_from, "--from"), _parse_date(date_to, "--to")
    settings = _settings()
    db = _database(settings)
    try:
        with db.connection() as conn:
            ref, rows = terms_coverage_report(conn, start, end, reference_id=reference)
    except LookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        db.close()
    typer.echo(f"reference static {ref.static_id}: {len(ref.terms)} bonds with 1.4 terms")
    if not rows:
        typer.echo("no current eod_final cut in the range")
        return
    wire = [r.wire() for r in rows]
    _table(
        [
            {
                **{k: w[k] for k in ("business_date", "bonds", "complete", "not_in_static")},
                "schema": w["static_schema_version"].removeprefix("market_snapshot/"),
                "basis": "as_of_date" if w["static_basis"] else None,
                "no_formula": w["no_formula"],
                **w["missing_by_field"],
            }
            for w in wire
        ],
        [
            "business_date",
            "schema",
            "basis",
            "bonds",
            "complete",
            "not_in_static",
            "no_formula",
            *TERM_FIELDS,
        ],
    )
    if json_out is not None:
        payload = {"reference_static_id": ref.static_id, "dates": wire}
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        typer.echo(f"written {json_out}")


@app.command("backfill")
def backfill_cmd(
    dataset: Annotated[str, typer.Argument(help="Dataset name (see `mdhub ops coverage`).")],
    date_from: Annotated[str, typer.Option("--from", help="First date, YYYY-MM-DD.")],
    date_to: Annotated[str, typer.Option("--to", help="Last date, YYYY-MM-DD.")],
    now: Annotated[
        bool, typer.Option("--now", help="Run the backfill job inline instead of queueing.")
    ] = False,
) -> None:
    """Queue (default) or run (--now) a backfill of DATASET for an inclusive date range."""
    from mdhub.quality.backfill import run_backfill_range
    from mdhub.quality.checks import max_task_days, queue_range, split_range

    d1 = _parse_date(date_from, "--from")
    d2 = _parse_date(date_to, "--to")
    if d2 < d1:
        typer.echo("--to is before --from", err=True)
        raise typer.Exit(2)
    settings = _settings()
    report = discover_jobs()
    try:
        spec = get_dataset(dataset)
    except KeyError:
        typer.echo(f"unknown dataset {dataset!r}", err=True)
        for module, err in report.failed.items():
            typer.echo(f"import failed: {module}: {err}", err=True)
        raise typer.Exit(2) from None
    if not spec.backfill_job:
        typer.echo(f"dataset {dataset} has no backfill job", err=True)
        raise typer.Exit(2)
    db = _database(settings)
    failed = False
    try:
        if not now:
            with db.connection() as conn:
                queued = queue_range(conn, spec, d1, d2, source="cli")
            for task_id, lo, hi in queued:
                state = f"task {task_id}" if task_id is not None else "already queued"
                typer.echo(f"{dataset} {lo}..{hi}: {state}")
            return
        calendar = DbCalendar(db)
        for lo, hi in split_range(d1, d2, max_task_days(spec)):
            try:
                res = run_backfill_range(spec, lo, hi, settings=settings, db=db, calendar=calendar)
            except (JobParamError, UnknownJobError) as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(2) from None
            typer.echo(
                f"{dataset} {lo}..{hi}: {res.status} (run {res.run_id}), "
                f"gaps backfilled={res.backfilled} unfillable={res.unfillable}"
                + (f" - {res.error}" if res.error and res.status != "succeeded" else "")
            )
            failed = failed or res.status == "failed"
    finally:
        db.close()
    if failed:
        raise typer.Exit(1)


@app.command("backup")
def backup_cmd(
    keep: Annotated[int, typer.Option(help="Dumps to keep after rotation.")] = 14,
    directory: Annotated[
        Path | None, typer.Option("--dir", help="Backup directory (default MDHUB_BACKUP_DIR).")
    ] = None,
) -> None:
    """pg_dump the database now (maintenance mode=backup, recorded in ops.job_run)."""
    settings = _settings()
    discover_jobs()
    db = _database(settings)
    params: dict[str, Any] = {"mode": "backup", "keep": keep}
    if directory is not None:
        params["backup_dir_path"] = str(directory)
    try:
        outcome = run_job("maintenance", params, settings=settings, db=db, trigger="cli")
    except (UnknownJobError, JobParamError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    except Exception as exc:
        typer.echo(f"backup failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        db.close()
    result = outcome.result if isinstance(outcome.result, dict) else {}
    typer.echo(f"backup {outcome.status}: {result.get('path')} ({result.get('size_bytes')} bytes)")
    for p in result.get("removed", []):
        typer.echo(f"removed {p}")


@app.command("restore")
def restore_cmd(
    file: Annotated[Path, typer.Argument(help="Custom-format dump (pg_dump -Fc).")],
    clean: Annotated[
        bool, typer.Option("--clean", help="Drop existing objects first (--clean --if-exists).")
    ] = False,
    jobs: Annotated[int, typer.Option(help="Parallel restore jobs (1 = single transaction).")] = 1,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """pg_restore FILE into MDHUB_DATABASE_URL (then run `mdhub migrate`).

    Best restored into an empty database; ownership and grants are not restored.
    """
    from mdhub.loaders.maintenance import restore_command, run_command

    if not file.is_file():
        typer.echo(f"no such file: {file}", err=True)
        raise typer.Exit(2)
    settings = _settings()
    db = _database(settings)
    try:
        if not yes:
            typer.confirm(f"Restore {file} into {db.redacted}?", abort=True)
        try:
            argv, env = restore_command(db.conninfo, file, clean=clean, jobs=jobs)
            run_command(argv, env, timeout=24 * 3600, secrets=settings.secret_values())
        except (RuntimeError, ValueError) as exc:
            typer.echo(f"restore failed: {exc}", err=True)
            raise typer.Exit(1) from None
    finally:
        db.close()
    typer.echo("restore complete; now run `mdhub migrate` and re-apply read-only grants")
