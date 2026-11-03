"""Opt-in, bounded access to observed rating actions; coverage is not established."""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from mdhub.api.common import bad_request, json_response, read_connection, require_aware

router = APIRouter(prefix="/history", tags=["history"])


@router.get("/rating-actions", summary="Observed rating actions for one issue or issuer")
def rating_actions(
    request: Request,
    object_kind: Literal["issue", "issuer"],
    object_key: Annotated[str, Query(min_length=1, max_length=64)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    known_at: datetime,
    limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
) -> Response:
    """Return observed source events, not a complete rating or an as-of classification."""
    if not request.app.state.settings.rating_history_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    known = require_aware(known_at, "known_at")
    if date_from > date_to:
        raise bad_request("from must not be after to")
    if (date_to - date_from).days > 3660:
        raise bad_request("date range must not exceed 3660 days")
    key = object_key.strip()
    if not key:
        raise bad_request("object_key must not be empty")
    with read_connection(request) as conn:
        rows = conn.execute(
            """
            SELECT agency, object_kind, object_key, rating_type, scale, action_date,
                   rating, grade, outlook, watch, action, action_raw, ingested_at
            FROM ref.rating_action_known_at(%(known)s, %(start)s, %(end)s)
            WHERE object_kind = %(kind)s AND object_key = %(key)s
            ORDER BY action_date, agency, rating_type, scale, action_ref
            LIMIT %(limit)s
            """,
            {
                "known": known,
                "start": date_from,
                "end": date_to,
                "kind": object_kind,
                "key": key,
                "limit": limit + 1,
            },
        ).fetchall()
    return json_response(
        request,
        {
            "object_kind": object_kind,
            "object_key": key,
            "from": date_from,
            "to": date_to,
            "known_at": known,
            "coverage": "unverified",
            "complete": False,
            "truncated": len(rows) > limit,
            "actions": rows[:limit],
        },
        etag=False,
    )
