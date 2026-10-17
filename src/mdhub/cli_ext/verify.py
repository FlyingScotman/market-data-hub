"""``mdhub verify static``: read-only verification of bond static data against MOEX EOD.

``mdhub verify static --out docs/STATIC_VERIFICATION.md --bonds-csv FILE [--from D] [--to D]
[--secid S ...] [--yield-sample month_end|all|none]`` reads ``MDHUB_DATABASE_URL`` in one
``REPEATABLE READ READ ONLY`` transaction (never writes), recomputes accrued interest and yields
from ``ref.bond*`` and checks the static invariants (:mod:`mdhub.verify.static`). The Markdown
report replaces the generated block of an existing file and keeps hand-written text around it
(:func:`mdhub.verify.render.merge_generated`); the CSV has one row per bond with mismatch
reasons.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from mdhub.config import ConfigError, Settings
from mdhub.verify.render import merge_generated, render_csv, render_markdown
from mdhub.verify.static import run_verification

name = "verify"
app = typer.Typer(help="Read-only verification of loaded data.", no_args_is_help=True)

YIELD_SAMPLES = ("month_end", "all", "none")
TITLE = "Bond static verification"


def _parse_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        typer.echo(f"{option}: expected YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(2) from None


@app.command("static")
def static_cmd(
    out: Annotated[str, typer.Option("--out", "-o", help="Markdown report ('-' = stdout).")] = "-",
    bonds_csv: Annotated[
        str | None, typer.Option("--bonds-csv", help="Per-bond result CSV.")
    ] = None,
    date_from: Annotated[
        str | None, typer.Option("--from", help="First EOD trade date (YYYY-MM-DD).")
    ] = None,
    date_to: Annotated[
        str | None, typer.Option("--to", help="Last EOD trade date (YYYY-MM-DD).")
    ] = None,
    secid: Annotated[
        list[str] | None, typer.Option("--secid", help="Only these SECIDs (repeatable).")
    ] = None,
    yield_sample: Annotated[
        str,
        typer.Option(
            "--yield-sample",
            help="Yield dates: month_end (last trade date of each month + last 5), all, none.",
        ),
    ] = "month_end",
) -> None:
    """Verify bond static data (accrued interest, yields, invariants) against MOEX EOD."""
    d_from = _parse_date(date_from, "--from")
    d_to = _parse_date(date_to, "--to")
    if yield_sample not in YIELD_SAMPLES:
        typer.echo(f"--yield-sample: one of {', '.join(YIELD_SAMPLES)}", err=True)
        raise typer.Exit(2)
    try:
        url = Settings().require_database_url()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None
    started = time.monotonic()
    try:
        with psycopg.connect(url, application_name="mdhub-verify") as conn:
            run = run_verification(
                conn,
                date_from=d_from,
                date_to=d_to,
                secids=secid or None,
                yield_sample=yield_sample,
                progress=lambda msg: typer.echo(msg, err=True),
            )
    except psycopg.Error as exc:
        typer.echo(f"verification failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None
    generated = render_markdown(run)
    if out == "-":
        sys.stdout.write(generated)
    else:
        path = Path(out)
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(merge_generated(existing, generated, TITLE), encoding="utf-8")
    if bonds_csv:
        csv_path = Path(bonds_csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(render_csv(run), encoding="utf-8")
    typer.echo(
        f"verified {len(run.bonds)} bonds / {run.eod_rows} EOD rows in "
        f"{time.monotonic() - started:.1f}s -> {out}" + (f", {bonds_csv}" if bonds_csv else ""),
        err=True,
    )
