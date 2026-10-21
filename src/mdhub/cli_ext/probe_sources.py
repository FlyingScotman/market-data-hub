"""``mdhub probe-sources``: which external sources of the reference-rate, cross-rate,
proxy-curve and CPI datasets answer from this machine (:mod:`mdhub.quality.source_check`).

``mdhub probe-sources [--source CODE ...] [--timeout S] [--json]`` sends one real request per
(source, dataset), parses it with the loader's parser and prints a table: source, dataset, role
in the fallback chain, ok, HTTP status, latency, size and what came back. Writes nothing, needs
no database. Exit status 0 when every **primary** source answered, 1 otherwise (fallbacks are
reported, not counted).
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from mdhub.config import Settings
from mdhub.quality.source_check import CHECKS, run_checks

name = "probe-sources"
app = typer.Typer(help="Check from this machine which external rate/FX/CPI sources answer.")


@app.callback(invoke_without_command=True)
def probe_sources(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="only this source code (repeatable)"),
    ] = None,
    timeout: Annotated[float, typer.Option(help="per-request timeout, seconds")] = 20.0,
    as_json: Annotated[bool, typer.Option("--json", help="one JSON object per line")] = False,
) -> None:
    known = {c.source for c in CHECKS}
    unknown = sorted(set(source or ()) - known)
    if unknown:
        typer.echo(f"unknown source(s) {unknown}; known: {sorted(known)}", err=True)
        raise typer.Exit(2)
    results = run_checks(Settings(), sources=set(source) if source else None, timeout=timeout)
    if as_json:
        for r in results:
            typer.echo(json.dumps(r.as_dict(), ensure_ascii=False))
    else:
        header = ("source", "dataset", "role", "ok", "http", "ms", "bytes", "result")
        rows = [
            (
                r.source,
                r.dataset,
                r.role,
                "OK" if r.ok else "FAIL",
                str(r.http_status or "-"),
                f"{r.latency_ms:.0f}",
                str(r.size_bytes or "-"),
                r.result,
            )
            for r in results
        ]
        widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(header[:-1])]
        for row in (header, *rows):
            cells = [c.ljust(w) for c, w in zip(row[:-1], widths, strict=True)]
            typer.echo("  ".join([*cells, row[-1]]))
    if any(not r.ok and r.role == "primary" for r in results):
        raise typer.Exit(1)
