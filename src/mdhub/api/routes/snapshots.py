"""``/v1/snapshots``: immutable published snapshots (Market Snapshot Contract v1).

* ``GET /v1/snapshots?kind&cut_type&business_date&latest`` - list (newest first);
  ``latest=true`` returns only the newest matching snapshot that no correction supersedes.
* ``GET /v1/snapshots/{id}/manifest`` - the envelope (``snapshot_id`` + manifest fields);
  ``meta`` carries mutable facts (``superseded_by``, publication time, sizes).
* ``GET /v1/snapshots/{id}/bundle`` - the gzipped canonical bundle, ``ETag: "<id>"``
  (``If-None-Match`` -> 304), immutable caching. ``sha256(gunzip(body)) == id``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response
from psycopg import sql

from mdhub.api.common import Page, Where, bad_request, json_response, page_params, read_connection

router = APIRouter(tags=["snapshots"])

_ID_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_KINDS: Final = ("static", "market")
_CUT_TYPES: Final = ("eod_preliminary", "eod_final", "intraday", "daily", "change")

_LIST_COLUMNS: Final = sql.SQL(
    """
    p.id AS snapshot_id, p.schema_version, p.kind, p.cut_type, p.business_date, p.cut_ts,
    p.known_at, p.static_snapshot_id, p.supersedes,
    (SELECT n.id FROM ops.published_snapshot n WHERE n.supersedes = p.id) AS superseded_by,
    p.content_sha256, p.calendar_fingerprint, p.size_bytes, p.gzip_bytes, p.excluded_count,
    (SELECT jsonb_object_agg(d ->> 'name', (d ->> 'rows')::bigint)
       FROM jsonb_array_elements(p.datasets) d) AS rows,
    p.published_at
    """
)

SnapshotId = Annotated[str, Path(description="snapshot id (sha256 hex)")]


def _check_id(snapshot_id: str) -> str:
    if not _ID_RE.match(snapshot_id):
        raise bad_request("snapshot id must be 64 lowercase hex characters")
    return snapshot_id


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="snapshot not found")


@router.get("/snapshots", summary="Published snapshots (ops.published_snapshot)")
def list_snapshots(
    request: Request,
    page: Annotated[Page, Depends(page_params)],
    kind: Annotated[str | None, Query(description="static | market")] = None,
    cut_type: Annotated[
        str | None,
        Query(
            description="eod_preliminary | eod_final | intraday (market); daily | change (static)"
        ),
    ] = None,
    business_date: Annotated[date | None, Query(description="Moscow business date")] = None,
    latest: Annotated[
        bool, Query(description="only the newest matching snapshot not superseded")
    ] = False,
) -> Response:
    """Newest first (business date, cut, publication). ``rows`` = row count per dataset."""
    where = Where()
    if kind is not None:
        if kind not in _KINDS:
            raise bad_request(f"kind must be one of {', '.join(_KINDS)}")
        where.add("p.kind = %(kind)s", kind=kind)
    if cut_type is not None:
        if cut_type not in _CUT_TYPES:
            raise bad_request(f"cut_type must be one of {', '.join(_CUT_TYPES)}")
        where.add("p.cut_type = %(cut_type)s", cut_type=cut_type)
    if business_date is not None:
        where.add("p.business_date = %(business_date)s", business_date=business_date)
    if latest:
        where.add("NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)")
    query = (
        sql.SQL("SELECT ")
        + _LIST_COLUMNS
        + sql.SQL(" FROM ops.published_snapshot p")
        + where.sql()
        + sql.SQL(" ORDER BY p.business_date DESC, p.cut_ts DESC, p.published_at DESC, p.id")
    )
    with read_connection(request) as conn:
        if latest:
            rows = conn.execute(query + sql.SQL(" LIMIT 1"), where.params).fetchall()
            body: dict[str, Any] = {"data": rows, "meta": {"returned": len(rows), "latest": True}}
        else:
            rows = conn.execute(query + page.sql(), where.params).fetchall()
            body = page.wrap(rows)
    return json_response(request, body)


@router.get("/snapshots/{snapshot_id}/manifest", summary="Snapshot manifest")
def manifest(request: Request, snapshot_id: SnapshotId) -> Response:
    """The immutable manifest in ``data`` (envelope of the bundle plus ``snapshot_id``)."""
    _check_id(snapshot_id)
    with read_connection(request) as conn:
        row = conn.execute(
            """
            SELECT p.manifest, p.published_at, p.size_bytes, p.gzip_bytes,
                   (SELECT n.id FROM ops.published_snapshot n WHERE n.supersedes = p.id)
                       AS superseded_by
            FROM ops.published_snapshot p WHERE p.id = %s
            """,
            (snapshot_id,),
        ).fetchone()
    if row is None:
        raise _not_found()
    return json_response(
        request,
        {
            "data": row["manifest"],
            "meta": {
                "published_at": row["published_at"],
                "superseded_by": row["superseded_by"],
                "size_bytes": row["size_bytes"],
                "gzip_bytes": row["gzip_bytes"],
            },
        },
    )


@router.get(
    "/snapshots/{snapshot_id}/bundle",
    summary="Snapshot bundle (gzipped canonical JSON)",
    response_class=Response,
    responses={200: {"content": {"application/gzip": {}}}, 304: {"description": "not modified"}},
)
def bundle(request: Request, snapshot_id: SnapshotId) -> Response:
    """Verify with ``sha256(gunzip(body)) == snapshot_id``."""
    _check_id(snapshot_id)
    etag = f'"{snapshot_id}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Snapshot-Sha256": snapshot_id,
    }
    with read_connection(request) as conn:
        if _etag_hit(request, etag):
            exists = conn.execute(
                "SELECT 1 FROM ops.published_snapshot WHERE id = %s", (snapshot_id,)
            ).fetchone()
            if exists is None:
                raise _not_found()
            return Response(status_code=304, headers=headers)
        row = conn.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (snapshot_id,)
        ).fetchone()
    if row is None:
        raise _not_found()
    headers["Content-Disposition"] = f'attachment; filename="{snapshot_id}.json.gz"'
    return Response(
        content=bytes(row["payload_gz"]), media_type="application/gzip", headers=headers
    )


def _etag_hit(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match")
    if not header:
        return False
    tags = [t.strip().removeprefix("W/") for t in header.split(",")]
    return "*" in tags or etag in tags
