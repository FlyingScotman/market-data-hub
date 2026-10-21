"""Columnar responses of the history read API (``/v1/history/*``, DESIGN §8).

A history response is one JSON object::

    {"dataset": "bonds", "columns": ["trade_date", "secid", ...],
     "rows": [["2026-10-15", "SU26238RMFS4", ...], ...],
     "as_of": "2026-10-21T09:00:00.123456+00:00", "known_at": null,
     "data_version": "2026-10-21T04:15:02.000001+00:00", "next": "eyJ...", "meta": {...}}

* ``rows`` are rendered **by PostgreSQL** (``json_build_array(...)::text`` per row): numeric
  values keep their stored digits as JSON numbers, dates are ``YYYY-MM-DD``, timestamps ISO 8601
  with offset (sessions run in UTC), ``jsonb`` values are embedded as JSON. Python only joins
  the row texts, so a page of 50,000 rows is serialized in milliseconds.
* ``as_of`` = start of the read transaction (``REPEATABLE READ``: every statement of the request
  sees that state); ``known_at`` echoes the parameter (``null`` = current state);
  ``data_version`` = the latest hub write (``ingested_at`` / ``updated_at``) of any row of the
  whole selection (all pages), ``null`` for an empty selection.
* **Paging** is keyset-based: ``next`` is an opaque cursor (base64url JSON of the last key and a
  hash of the query) to pass as ``cursor=`` with the *same* query; ``null`` on the last page.
  A cursor of another query is rejected (400).
* **Caching**: a weak ``ETag`` derived from the query, the page and the selection's version
  (row counts + latest write time per table; for ``known_at`` queries: the body) plus
  ``Last-Modified`` (= ``data_version``); ``If-None-Match`` (preferred) or ``If-Modified-Since``
  answer ``304`` without reading the rows. ``Cache-Control: private, no-cache``.
* **gzip**: bodies are gzip-compressed when the client sends ``Accept-Encoding: gzip``
  (``Vary: Accept-Encoding``).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Final

from fastapi import HTTPException, Request
from fastapi.responses import Response
from psycopg import sql
from psycopg.rows import tuple_row

from mdhub import __version__
from mdhub.api.common import bad_request
from mdhub.api.jsonutil import dumps
from mdhub.db.pool import Database, DictConnection

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Columnar",
    "Version",
    "decode_cursor",
    "history_connection",
    "query_hash",
    "respond",
]

DEFAULT_LIMIT: Final = 10_000
MAX_LIMIT: Final = 50_000
STATEMENT_TIMEOUT_MS: Final = 120_000
GZIP_MIN_BYTES: Final = 1024
GZIP_LEVEL: Final = 5


@contextmanager
def history_connection(request: Request) -> Iterator[DictConnection]:
    """A ``REPEATABLE READ, READ ONLY`` transaction with a statement timeout: the version query
    and the page query of one request see the same state."""
    database: Database | None = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with database.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(STATEMENT_TIMEOUT_MS))
        )
        yield conn


def query_hash(endpoint: str, params: dict[str, Any]) -> str:
    """Stable hash of a normalized query (binds cursors to their query)."""
    text = json.dumps({"e": endpoint, "p": params}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def encode_cursor(qhash: str, key: Sequence[Any]) -> str:
    raw = json.dumps({"q": qhash, "k": list(key)}, separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, qhash: str, size: int) -> list[Any] | None:
    """The key of ``cursor`` (``None`` without one); 400 when it is malformed or belongs to
    another query."""
    if cursor is None or cursor == "":
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
        key = data["k"]
        ok = data["q"] == qhash and isinstance(key, list) and len(key) == size
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        ok = False
    if not ok:
        raise bad_request("cursor does not belong to this query (repeat the query without it)")
    return list(key)


@dataclass(frozen=True, slots=True)
class Version:
    """Version of a selection: row counts and latest write time per contributing table."""

    parts: tuple[Any, ...]
    data_version: datetime | None


@dataclass(slots=True)
class Columnar:
    """One page of a history response."""

    dataset: str
    columns: list[str]
    rows: list[str]
    """Rows as JSON array texts (rendered by PostgreSQL)."""
    as_of: datetime
    known_at: datetime | None
    next: str | None
    meta: dict[str, Any]
    data_version: datetime | None = None

    def body(self, *, with_as_of: bool = True) -> bytes:
        head = {
            "dataset": self.dataset,
            "columns": self.columns,
        }
        tail = {
            "as_of": self.as_of if with_as_of else None,
            "known_at": self.known_at,
            "data_version": self.data_version,
            "next": self.next,
            "meta": self.meta,
        }
        text = dumps(head)[:-1] + ',"rows":[' + ",".join(self.rows) + "]," + dumps(tail)[1:]
        return text.encode("utf-8")


def _etag_for(parts: Sequence[Any]) -> str:
    digest = hashlib.sha256(
        json.dumps([__version__, *parts], default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:40]
    return f'W/"{digest}"'


def _matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    want = etag.removeprefix("W/")
    for raw in header.split(","):
        tag = raw.strip()
        if tag == "*" or tag.removeprefix("W/") == want:
            return True
    return False


def _http_date(value: datetime) -> str:
    return format_datetime(value.astimezone(UTC).replace(microsecond=0), usegmt=True)


def _not_modified_since(request: Request, version: datetime | None) -> bool:
    header = request.headers.get("if-modified-since")
    if not header or version is None or request.headers.get("if-none-match"):
        return False
    try:
        since = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return version.replace(microsecond=0) <= since


def _accepts_gzip(request: Request) -> bool:
    for part in request.headers.get("accept-encoding", "").split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() in ("gzip", "*"):
            q = params.strip()
            return not (q.startswith("q=") and q[2:].strip() in ("0", "0.0", "0.00", "0.000"))
    return False


def cache_headers(etag: str, version: datetime | None) -> dict[str, str]:
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}
    if version is not None:
        headers["Last-Modified"] = _http_date(version)
    return headers


def not_modified(request: Request, etag: str, version: datetime | None) -> Response | None:
    """A 304 when the client's validators match (``If-None-Match`` takes precedence)."""
    if _matches(request.headers.get("if-none-match"), etag) or _not_modified_since(
        request, version
    ):
        return Response(status_code=304, headers=cache_headers(etag, version))
    return None


def respond(request: Request, page: Columnar, etag: str | None = None) -> Response:
    """Render ``page`` (gzip when accepted). ``etag`` from the version query; without one
    (``known_at`` reads) it is derived from the body."""
    body = page.body()
    if etag is None:
        etag = _etag_for(["body", hashlib.sha256(page.body(with_as_of=False)).hexdigest()])
        hit = not_modified(request, etag, page.data_version)
        if hit is not None:
            return hit
    headers = cache_headers(etag, page.data_version)
    if len(body) >= GZIP_MIN_BYTES and _accepts_gzip(request):
        body = gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)
        headers["Content-Encoding"] = "gzip"
    return Response(content=body, media_type="application/json", headers=headers)


def version_etag(endpoint: str, qhash: str, page_key: Sequence[Any], version: Version) -> str:
    """ETag of a page from the selection's version (no row read needed)."""
    return _etag_for([endpoint, qhash, list(page_key), list(version.parts)])


def fetch_version(
    conn: DictConnection, query: sql.Composed | sql.SQL, params: dict[str, Any]
) -> Version:
    """Run a version query returning one row ``(count, max_ts, count, max_ts, ...)``; the
    data version is the latest of the timestamps."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(query, params).fetchone()
    parts = tuple(row) if row is not None else ()
    stamps = [p for p in parts if isinstance(p, datetime)]
    return Version(parts=parts, data_version=max(stamps) if stamps else None)


def fetch_page(
    conn: DictConnection,
    query: sql.Composed | sql.SQL,
    params: dict[str, Any],
    limit: int,
) -> tuple[list[str], list[Any] | None]:
    """Run a page query selecting ``(json_build_array(<columns>)::text,
    json_build_array(<key>)::text)`` with ``LIMIT limit + 1``. Returns the row texts and, when
    there is a next page, the key of the last returned row (for the cursor)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        fetched = cur.execute(query, params).fetchall()
    more = len(fetched) > limit
    fetched = fetched[:limit]
    rows = [str(r[0]) for r in fetched]
    last_key = json.loads(str(fetched[-1][1])) if more and fetched else None
    return rows, last_key


def read_as_of(conn: DictConnection) -> datetime:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute("SELECT now()").fetchone()
    assert row is not None
    value: datetime = row[0]
    return value
