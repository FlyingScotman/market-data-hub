"""``mdhub hostmon``: the host sampler service (ADMIN_DESIGN §10, :mod:`mdhub.hostmon.sampler`).

Runs until SIGTERM/SIGINT, writing ``ops.host_metric`` every 30 s and a heartbeat (process
``hostmon``). ``--once`` prints one sample instead (no database), e.g. to check the mounts::

    docker compose run --rm hostmon mdhub hostmon --once
"""

from __future__ import annotations

import re
import signal
import threading
import time
from types import FrameType
from typing import Annotated

import typer

from mdhub.config import ConfigError, Settings
from mdhub.core.heartbeat import Heartbeat
from mdhub.db.pool import Database, warn_if_owner
from mdhub.hostmon.sampler import HostPaths, Sampler, run_forever

name = "hostmon"
app = typer.Typer(help="Sample the host into ops.host_metric (every 30 s).")


@app.callback(invoke_without_command=True)
def hostmon(
    once: Annotated[
        bool, typer.Option("--once", help="Print one sample and exit (no database).")
    ] = False,
) -> None:
    try:
        settings = Settings()
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None
    paths = HostPaths.detect()
    sampler = Sampler(
        paths=paths,
        mounts=tuple(m for m in re.split(r"[\s,]+", settings.hostmon_mounts) if m),
        ntp_server=settings.ntp_server,
    )
    if once:
        sampler.sample()  # rates need a previous sample
        time.sleep(1)
        for s in sorted(sampler.sample(), key=lambda s: (s.metric, str(s.labels))):
            typer.echo(f"{s.metric:28} {s.labels or ''!s:36} {s.value:.6g}")
        return
    try:
        db = Database.from_settings(settings, pool_size=2)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    warn_if_owner(db.conninfo, "hostmon", "MDHUB_HOSTMON_DB_PASSWORD")
    stop = threading.Event()

    def _stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    heartbeat = Heartbeat(db, "hostmon")
    heartbeat.start()
    typer.echo(f"hostmon: proc={paths.proc} sys={paths.sys} root={paths.root or '/'}", err=True)
    try:
        run_forever(db, sampler, stop)
    finally:
        heartbeat.stop()
        db.close()
