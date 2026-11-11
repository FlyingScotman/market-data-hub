"""Operations endpoints: coverage, job runs, job health, open gaps, carried curves."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from psycopg import sql

from mdhub.api.common import Page, Where, fetch_all, json_response, page_params, read_connection
from mdhub.core.timeutil import msk_today
from mdhub.snapshot.carry import carried_curves

router = APIRouter(tags=["ops"])


@router.get("/coverage", summary="Per-dataset coverage (ops.v_coverage)")
def coverage(request: Request) -> Response:
    """First/last date, rows, staleness, open gaps and last run per dataset (written by the
    ``quality`` job)."""
    with read_connection(request) as conn:
        rows = fetch_all(conn, sql.SQL("SELECT * FROM ops.v_coverage ORDER BY dataset"))
    return json_response(request, {"data": rows, "meta": {"returned": len(rows)}})


@router.get("/runs", summary="Recent job runs (ops.job_run)")
def runs(
    request: Request,
    page: Annotated[Page, Depends(page_params)],
    job: Annotated[str | None, Query(description="job name")] = None,
    status: Annotated[
        str | None, Query(description="running|succeeded|partial|failed|skipped")
    ] = None,
) -> Response:
    """Newest first. ``error`` is the sanitized error / partial reason; ``trigger`` who started
    the run (schedule, catch_up, backfill, cli, api, other; null before migration 0421)."""
    where = Where()
    if job:
        where.add("job = %(job)s", job=job)
    if status:
        where.add("status = %(status)s", status=status)
    query = (
        sql.SQL(
            "SELECT id, job, status, started_at, finished_at, params, rows_inserted, "
            "rows_updated, rows_unchanged, error, host, trigger FROM ops.job_run"
        )
        + where.sql()
        + sql.SQL(" ORDER BY started_at DESC, id DESC")
        + page.sql()
    )
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, page.wrap(rows), etag=False)


@router.get("/runs/health", summary="Per-job health (ops.v_job_health)")
def job_health(request: Request) -> Response:
    with read_connection(request) as conn:
        rows = fetch_all(conn, sql.SQL("SELECT * FROM ops.v_job_health ORDER BY job"))
    return json_response(request, {"data": rows, "meta": {"returned": len(rows)}}, etag=False)


@router.get("/gaps", summary="Open data gaps (ops.v_open_gaps)")
def gaps(
    request: Request,
    page: Annotated[Page, Depends(page_params)],
    dataset: str | None = None,
) -> Response:
    where = Where()
    if dataset:
        where.add("dataset = %(dataset)s", dataset=dataset)
    query = (
        sql.SQL("SELECT * FROM ops.v_open_gaps")
        + where.sql()
        + sql.SQL(" ORDER BY dataset, gap_date, key::text")
        + page.sql()
    )
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, page.wrap(rows), etag=False)


@router.get(
    "/carried-curves",
    summary="Curves carried forward in the current market snapshots (contract 1.7)",
)
def carried(
    request: Request,
    days: Annotated[int, Query(ge=0, le=400, description="business dates from today - days")] = 30,
    cut_type: Annotated[str | None, Query(description="eod_preliminary|eod_final|intraday")] = None,
) -> Response:
    """The curves that no source delivered on the business date of a market snapshot and that the
    hub took, flagged, from the latest earlier day that has them. One row per carried curve of a
    **current** (not superseded) snapshot, newest first: the snapshot, the carried ``dataset`` and
    ``source``, ``source_day``, ``age_working_days``, ``age_limit_exceeded`` and ``reason``. A cut
    reissued after the real data arrived is superseded, so it drops out of the list; an empty list
    means no curve is carried."""
    since = msk_today() - timedelta(days=days)
    with read_connection(request) as conn:
        rows = carried_curves(conn, since=since, cut_types=[cut_type] if cut_type else None)
    return json_response(
        request,
        {"data": rows, "meta": {"returned": len(rows), "since": since.isoformat()}},
        etag=False,
    )
