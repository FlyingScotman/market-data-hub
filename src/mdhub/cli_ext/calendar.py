"""``mdhub calendar seed [--since YYYY-MM-DD]``: (re)seed the business-day calendars.

Runs :func:`mdhub.loaders.calendar.seed_calendar` (one ``ops.job_run`` named
``calendar_seed``): observed MOEX trading days and CBR RUONIA observation days from ``--since``
(default 2019-01-01), the days the MOEX ISS calendar lists (plus the weekend sessions of each
engine -> ``ref.weekend_session``), the curated file and the TARGET closing-day rule (from
2002-01-01, or ``--since`` if later; cross-checked against the ECB rate dates). Re-running it
repairs earlier data (ISS rows no longer listed are removed unless the ISS answer looks
incomplete; source precedence decides).
Load RUONIA history first (``cbr_daily``) for RU_PRODUCTION (and ECB history, ``ecb_fx``, for
the TARGET cross-check).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated

import typer

from mdhub import credentials
from mdhub.config import ConfigError, Settings
from mdhub.db.pool import Database

name = "calendar"
app = typer.Typer(help="Business-day calendars (ref.calendar_day).", no_args_is_help=True)


@app.command("seed")
def seed(
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="first day YYYY-MM-DD (default 2019-01-01; TARGET rule: 2002-01-01)",
        ),
    ] = None,
) -> None:
    """Full refresh of all calendars from ``--since`` (observed history, ISS, curated)."""
    from mdhub.loaders import calendar as cal

    try:
        start = date.fromisoformat(since) if since else None
    except ValueError:
        typer.echo(f"--since: expected YYYY-MM-DD, got {since!r}", err=True)
        raise typer.Exit(2) from None
    try:
        settings = Settings()
        db = Database.from_settings(settings)
        credentials.activate(settings, db, worker=False)  # MDHUB_HTTP_PROXY from the store
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    try:
        summary = cal.seed_calendar(settings, db, since=start)
    finally:
        db.close()
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
