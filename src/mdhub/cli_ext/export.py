"""``mdhub export tdm --date D --out DIR``: Trading Desk Monitor contract-1.3 CSV export."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database
from mdhub.export.tdm import PRICE_BASES, TdmExportOptions, build_tdm_export

name = "export"
app = typer.Typer(help="Export hub data in consumer formats.", no_args_is_help=True)


def _parse_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        typer.echo(f"{option}: expected YYYY-MM-DD, got {value!r}", err=True)
        raise typer.Exit(2) from None


@app.command("tdm")
def tdm(
    day: Annotated[str, typer.Option("--date", help="export (TDM run) date YYYY-MM-DD")],
    out: Annotated[Path, typer.Option("--out", help="output directory")],
    date_from: Annotated[
        str | None, typer.Option("--from", help="market series from (default: --date)")
    ] = None,
    price_basis: Annotated[
        str, typer.Option(help=f"md_bond_quotes price: {', '.join(PRICE_BASES)}")
    ] = "waprice",
    calendar: Annotated[str, typer.Option(help="hub calendar exported as TDM calendar")] = (
        "MOEX_STOCK"
    ),
    fx_candidate: Annotated[
        bool,
        typer.Option(
            "--fx-candidate/--no-fx-candidate",
            help="write md_fx candidate from MOEX 19:00 indicative rates (opt-in: TDM "
            "loads it as the market FX mark and would overwrite the internal mark)",
        ),
    ] = False,
    as_zip: Annotated[bool, typer.Option("--zip", help="write one tdm_<date>.zip")] = False,
) -> None:
    """Write TDM CSV files (``;``, UTF-8 BOM) and ``manifest.json`` into ``--out``."""
    d_to = _parse_date(day, "--date")
    assert d_to is not None
    try:
        options = TdmExportOptions(
            date_to=d_to,
            date_from=_parse_date(date_from, "--from"),
            price_basis=price_basis,
            calendar=calendar.upper(),
            include_fx_candidate=fx_candidate,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    try:
        settings = Settings()
        database = Database.from_settings(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None
    with database.connection() as conn:
        export = build_tdm_export(conn, options)
    if as_zip:
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"tdm_{options.suffix}.zip"
        target.write_bytes(export.to_zip())
        typer.echo(str(target))
    else:
        export.write_dir(out)
    for f in export.files:
        typer.echo(f"{f.name}\t{f.dataset}\t{len(f.rows)}")
    for reason, count in sorted(export.excluded.items()):
        typer.echo(f"excluded\t{reason}\t{count}", err=True)
