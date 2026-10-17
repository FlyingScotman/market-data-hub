"""Read-API request log: ``ops.api_request`` written in batches off the request path
(ADMIN_DESIGN §3.2).

The request middleware builds one :class:`RequestRecord` per request and hands it to
:meth:`RequestLogWriter.submit`, which only enqueues it (never blocks, drops and counts rows
when the queue is full). A background thread writes a batch with ``COPY`` when
:data:`FLUSH_BATCH` (200) rows are queued or :data:`FLUSH_INTERVAL_S` (1 s) after the first
row of a batch, and moves ``admin.api_token.last_used_at`` / ``last_used_ip`` forward for the
named tokens in it. A failed write is logged (exception type only) and the batch dropped: the
log must never take the API down.

What is recorded: the route template (``/v1/bonds/{secid}``, never the raw path; ``<unmatched>``
when no route matched), the query string with values of credential-like parameters replaced by
``***`` (:func:`redact_query`), the token *name* and id (never the token), the client IP
(uvicorn ``--proxy-headers`` resolves it behind Caddy), status, duration, response size from
``Content-Length``, request id and user agent. ``/health/*`` requests (container health checks,
monitoring) are not recorded.
"""

from __future__ import annotations

import ipaddress
import logging
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode

from psycopg import sql
from starlette.routing import replace_params

from mdhub.core.redact import REDACTED, is_sensitive_param
from mdhub.db.pool import Database

__all__ = [
    "FLUSH_BATCH",
    "FLUSH_INTERVAL_S",
    "UNMATCHED_PATH",
    "RequestLogWriter",
    "RequestRecord",
    "client_ip",
    "redact_query",
    "route_template",
    "truncate_user_agent",
]

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S: Final = 1.0
FLUSH_BATCH: Final = 200
MAX_QUEUE: Final = 20_000
UNMATCHED_PATH: Final = "<unmatched>"
_MAX_QUERY = 1000
_MAX_USER_AGENT = 256
_COLUMNS: Final = (
    "ts",
    "token_id",
    "token_name",
    "client_ip",
    "method",
    "path",
    "query",
    "status",
    "duration_ms",
    "response_bytes",
    "request_id",
    "user_agent",
)
_COPY: Final = sql.SQL("COPY ops.api_request ({}) FROM STDIN").format(
    sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)
)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One ``ops.api_request`` row."""

    ts: datetime
    method: str
    path: str
    status: int
    duration_ms: float
    token_id: int | None = None
    token_name: str | None = None
    client_ip: str | None = None
    query: str | None = None
    response_bytes: int | None = None
    request_id: str | None = None
    user_agent: str | None = None

    def row(self) -> tuple[Any, ...]:
        return tuple(getattr(self, c) for c in _COLUMNS)


def redact_query(query: str) -> str | None:
    """Query string with values of credential-like parameters (``token``, ``key``, ``auth``
    ...) replaced by ``***``; at most 1000 characters; ``None`` when empty."""
    if not query:
        return None
    pairs = [
        (k, REDACTED if is_sensitive_param(k) else v)
        for k, v in parse_qsl(query, keep_blank_values=True)
    ]
    text = urlencode(pairs, safe=",:/*")
    return text[:_MAX_QUERY] or None


def route_template(scope: Mapping[str, Any]) -> str:
    """Path template of the matched route (``/v1/bonds/{secid}``), or :data:`UNMATCHED_PATH`.

    FastAPI >= 0.140 includes routers lazily: ``scope["route"]`` is the route as declared in
    its own router (``/bonds/{secid}``), without the prefixes it was included under. The
    prefix is the part of the concrete path before the route's own (re-built) path.
    """
    route = scope.get("route")
    fmt = getattr(route, "path_format", None)
    if not isinstance(fmt, str) or not fmt:
        return UNMATCHED_PATH
    path = str(scope.get("path", ""))
    try:
        concrete, _ = replace_params(
            fmt, getattr(route, "param_convertors", {}), dict(scope.get("path_params") or {})
        )
    except Exception:  # unexpected convertor state: the declared template is still useful
        return fmt
    if concrete and path.endswith(concrete):
        return path[: len(path) - len(concrete)] + fmt
    return fmt


def client_ip(host: str | None) -> str | None:
    """``host`` if it is an IP address (test clients send names), else ``None``."""
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def truncate_user_agent(value: str | None) -> str | None:
    return value[:_MAX_USER_AGENT] if value else None


_STOP: Final = object()


class RequestLogWriter:
    """Background batch writer for ``ops.api_request``.

    :param db: database (the API's pool).
    :param flush_interval_s: write a batch this long after its first row at the latest.
    :param batch_size: write as soon as this many rows are queued.
    :param max_queue: rows beyond this are dropped (counted in :attr:`dropped`).
    """

    def __init__(
        self,
        db: Database,
        *,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        batch_size: int = FLUSH_BATCH,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        self._db = db
        self._interval = flush_interval_s
        self._batch_size = batch_size
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self.written = 0
        self.dropped = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mdhub-request-log", daemon=True)
            self._thread.start()

    def submit(self, record: RequestRecord) -> bool:
        """Enqueue ``record`` (non-blocking); ``False`` when the queue is full."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            return False
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Write what is queued and stop the thread."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            log.warning("request log queue full at shutdown; %d row(s) lost", self._queue.qsize())
            return
        thread.join(timeout)

    # ------------------------------------------------------------------- worker

    def _run(self) -> None:
        stopping = False
        while not stopping:
            first = self._queue.get()
            if first is _STOP:
                break
            batch = [first]
            deadline = time.monotonic() + self._interval
            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is _STOP:
                    stopping = True
                    break
                batch.append(item)
            self._write([r for r in batch if isinstance(r, RequestRecord)])

    def _write(self, batch: list[RequestRecord]) -> None:
        if not batch:
            return
        last_use: dict[int, RequestRecord] = {}
        for rec in batch:
            if rec.token_id is not None:
                prev = last_use.get(rec.token_id)
                if prev is None or rec.ts >= prev.ts:
                    last_use[rec.token_id] = rec
        try:
            with self._db.connection() as conn, conn.cursor() as cur:
                with cur.copy(_COPY) as copy:
                    for rec in batch:
                        copy.write_row(rec.row())
                if last_use:
                    cur.executemany(
                        """
                            UPDATE admin.api_token
                            SET last_used_at = %(ts)s, last_used_ip = %(ip)s
                            WHERE id = %(id)s
                              AND (last_used_at IS NULL OR last_used_at < %(ts)s)
                            """,
                        [
                            {"id": tid, "ts": rec.ts, "ip": rec.client_ip}
                            for tid, rec in last_use.items()
                        ],
                    )
        except Exception as exc:
            self.dropped += len(batch)
            log.warning("request log: %d row(s) not written: %s", len(batch), type(exc).__name__)
            return
        self.written += len(batch)
