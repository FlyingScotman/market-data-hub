"""Shared helpers of the read API: responses, ETags, paging, query building, parameters."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Annotated, Any, LiteralString

from fastapi import HTTPException, Query, Request
from fastapi.responses import Response
from psycopg import sql

from mdhub.api.jsonutil import dumps
from mdhub.core.timeutil import msk_today
from mdhub.db.pool import Database, DictConnection

__all__ = [
    "MAX_LIMIT",
    "Page",
    "Where",
    "date_range",
    "fetch_all",
    "json_response",
    "page_params",
    "read_connection",
    "require_aware",
    "split_list",
]

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10_000
STATEMENT_TIMEOUT_MS = 60_000

META_FIELDS = frozenset({"run_id", "capture_id", "last_seen_run"})
"""Provenance columns not exposed by the list endpoints (raw captures are internal)."""


# --------------------------------------------------------------------------- responses


class DecimalJSONResponse(Response):
    """``application/json`` rendered with :func:`mdhub.api.jsonutil.dumps`."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    for raw in header.split(","):
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def json_response(
    request: Request, payload: Any, *, status_code: int = 200, etag: bool = True
) -> Response:
    """Render ``payload``; with ``etag`` add a strong ETag and answer ``If-None-Match`` with 304.

    The ETag is the SHA-256 of the body, so it changes whenever any value (including
    revisions of past dates) changes. ``Cache-Control: private, no-cache`` makes clients
    revalidate every time.
    """
    body = dumps(payload).encode("utf-8")
    headers: dict[str, str] = {}
    if etag and status_code == 200:
        tag = '"' + hashlib.sha256(body).hexdigest()[:40] + '"'
        headers["ETag"] = tag
        headers["Cache-Control"] = "private, no-cache"
        if _etag_matches(request.headers.get("if-none-match"), tag):
            return Response(status_code=304, headers=headers)
    return Response(
        content=body, status_code=status_code, media_type="application/json", headers=headers
    )


# ------------------------------------------------------------------------------ db


@contextmanager
def read_connection(request: Request) -> Iterator[DictConnection]:
    """A read-only transaction with a statement timeout on the app's database."""
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with database.connection() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(STATEMENT_TIMEOUT_MS))
        )
        yield conn


def fetch_all(
    conn: DictConnection, query: sql.Composed | sql.SQL, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run ``query`` and return the rows as dicts (meta columns removed)."""
    rows = conn.execute(query, params or {}).fetchall()
    return [{k: v for k, v in r.items() if k not in META_FIELDS} for r in rows]


@dataclass
class Where:
    """AND-ed WHERE clauses from constant SQL snippets with ``%(name)s`` placeholders."""

    parts: list[sql.Composable] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    def add(self, clause: LiteralString, **params: Any) -> Where:
        self.parts.append(sql.SQL(clause))
        self.params.update(params)
        return self

    def sql(self) -> sql.Composed:
        if not self.parts:
            return sql.Composed([])
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(self.parts)


# --------------------------------------------------------------------------- paging


@dataclass(frozen=True, slots=True)
class Page:
    """``limit``/``offset`` paging; one extra row is fetched to detect a next page."""

    limit: int
    offset: int

    def sql(self) -> sql.Composed:
        return sql.SQL(" LIMIT {} OFFSET {}").format(
            sql.Literal(self.limit + 1), sql.Literal(self.offset)
        )

    def wrap(self, rows: Sequence[Any], **meta: Any) -> dict[str, Any]:
        more = len(rows) > self.limit
        data = list(rows[: self.limit])
        return {
            "data": data,
            "meta": {
                **meta,
                "limit": self.limit,
                "offset": self.offset,
                "returned": len(data),
                "next_offset": self.offset + self.limit if more else None,
            },
        }


def page_params(
    limit: Annotated[
        int, Query(ge=1, le=MAX_LIMIT, description=f"page size (max {MAX_LIMIT})")
    ] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="rows to skip (use meta.next_offset)")] = 0,
) -> Page:
    return Page(limit=limit, offset=offset)


# ----------------------------------------------------------------------- parameters


def bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def split_list(value: str | None, *, upper: bool = False) -> list[str] | None:
    """``"a,b , c"`` -> ``["a", "b", "c"]``; ``None``/blank -> ``None``."""
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        return None
    return [v.upper() for v in items] if upper else items


def date_range(
    day: date | None,
    date_from: date | None,
    date_to: date | None,
    *,
    default_days: int | None = None,
) -> tuple[date, date]:
    """Resolve ``date`` | ``from``/``to`` into an inclusive range.

    ``date`` excludes ``from``/``to``. Without any of them: last ``default_days`` calendar
    days up to today (MSK) when ``default_days`` is given, else 400. ``from`` alone runs to
    today (MSK); ``to`` alone starts ``default_days`` (or 0) days earlier.
    """
    if day is not None:
        if date_from is not None or date_to is not None:
            raise bad_request("use either date or from/to")
        return day, day
    if date_from is None and date_to is None and default_days is None:
        raise bad_request("date or from/to is required")
    end = date_to or msk_today()
    start = date_from or end - timedelta(days=default_days or 0)
    if start > end:
        raise bad_request("from must not be after to")
    return start, end


def require_aware(value: datetime | None, name: str) -> datetime | None:
    """Timestamps must carry a UTC offset (``2026-10-15T19:00:00+03:00`` or ``...Z``)."""
    if value is not None and value.utcoffset() is None:
        raise bad_request(f"{name} must include a UTC offset (e.g. 2026-10-15T19:00:00+03:00)")
    return value
