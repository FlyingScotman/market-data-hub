"""FastAPI read API of the market data hub (DESIGN §8). Entry point ``mdhub.api.app:app``.

* ``/health/live`` (process up) and ``/health/ready`` (database reachable and all migrations
  applied with matching checksums; reports the running ``version``) are unauthenticated.
* Everything under ``/v1`` requires ``Authorization: Bearer <token>`` with a named token from
  ``admin.api_token`` or a token from ``MDHUB_API_TOKENS``, with the scope the path needs
  (see :mod:`mdhub.api.auth`); the OpenAPI document is served at ``/v1/openapi.json`` behind
  the same check.
* Every request except ``/health/*`` is recorded in ``ops.api_request`` by a batch writer off
  the request path (:mod:`mdhub.api.requestlog`).
* JSON bodies: ``{"data": ..., "meta": {...}}``; dates ISO 8601, timestamps with offset,
  ``numeric`` values as JSON numbers with their exact stored digits
  (:mod:`mdhub.api.jsonutil`). Lists page with ``limit``/``offset``
  (``meta.next_offset``). GET responses carry a strong ``ETag`` and honour
  ``If-None-Match``.
* Every response carries ``X-Request-ID`` (echoed when the client sent a sane one, else
  generated); access log lines include it. Headers (tokens) are never logged.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from mdhub import __version__
from mdhub.api.auth import TokenStore, require_token
from mdhub.api.common import DecimalJSONResponse, json_response
from mdhub.api.requestlog import (
    RequestLogWriter,
    RequestRecord,
    client_ip,
    redact_query,
    route_template,
    truncate_user_agent,
)
from mdhub.api.routes import bonds, export, history, market, ops, snapshots
from mdhub.config import Settings
from mdhub.core.redact import SecretRedactingFilter, sanitize_text
from mdhub.db.migrate import applied_migrations, discover_migrations
from mdhub.db.pool import Database, warn_if_owner

__all__ = ["app", "create_app"]

log = logging.getLogger("mdhub.api")
access_log = logging.getLogger("mdhub.api.access")

API_POOL_SIZE = 8
"""Default of ``MDHUB_API_POOL_SIZE`` (``Settings.api_pool_size``)."""
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _request_id(request: Request) -> str:
    candidate = request.headers.get("x-request-id", "")
    return candidate if _REQUEST_ID_RE.match(candidate) else uuid.uuid4().hex


def _error(
    request: Request, status: int, message: Any, headers: dict[str, str] | None = None
) -> Response:
    return DecimalJSONResponse(
        {
            "error": {"status": status, "message": message},
            "request_id": getattr(request.state, "request_id", None),
        },
        status_code=status,
        headers=headers,
    )


def _record(
    writer: RequestLogWriter,
    request: Request,
    response: Response,
    received: datetime,
    elapsed_ms: float,
    rid: str,
) -> None:
    identity = getattr(request.state, "token", None)
    length = response.headers.get("content-length")
    writer.submit(
        RequestRecord(
            ts=received,
            method=request.method,
            path=route_template(request.scope),
            status=response.status_code,
            duration_ms=round(elapsed_ms, 1),
            token_id=getattr(identity, "token_id", None),
            token_name=getattr(identity, "name", None),
            client_ip=client_ip(request.client.host if request.client else None),
            query=redact_query(request.url.query),
            response_bytes=int(length) if length and length.isdigit() else None,
            request_id=rid,
            user_agent=truncate_user_agent(request.headers.get("user-agent")),
        )
    )


def _configure_logging(settings: Settings) -> None:
    """Attach the secret-redacting filter to root handlers (uvicorn configures them)."""
    flt = SecretRedactingFilter(settings.secret_values())
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(flt)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(flt)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    logging.getLogger("mdhub").setLevel(settings.log_level)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    """Build the app. Settings/database are resolved at startup (not import) when omitted;
    a ``database`` passed in is not closed by the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings()
        app.state.settings = cfg
        app.state.secrets = cfg.secret_values()
        owned: Database | None = None
        if database is not None:
            app.state.database = database
        elif cfg.database_url is not None:
            owned = Database.from_settings(cfg, pool_size=cfg.api_pool_size)
            app.state.database = owned
        else:
            app.state.database = None
        if settings is None:
            _configure_logging(cfg)
        if app.state.database is not None:
            warn_if_owner(app.state.database.conninfo, "api", "MDHUB_API_DB_PASSWORD")
        app.state.token_store = TokenStore(cfg.api_token_entries(), app.state.database)
        if not app.state.token_store.configured:
            log.warning("no MDHUB_API_TOKENS and no database: every /v1 request will be rejected")
        writer: RequestLogWriter | None = None
        if app.state.database is not None:
            writer = RequestLogWriter(app.state.database)
            writer.start()
        app.state.request_log = writer
        try:
            yield
        finally:
            app.state.request_log = None
            if writer is not None:
                writer.stop()
            if owned is not None:
                owned.close()

    app = FastAPI(
        title="market-data-hub read API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=DecimalJSONResponse,
    )
    app.state.token_store = None
    app.state.request_log = None
    app.state.database = None
    app.state.secrets = []

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = _request_id(request)
        request.state.request_id = rid
        received = datetime.now(UTC)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            log.error(
                "request %s %s failed [%s]: %s",
                request.method,
                request.url.path,
                rid,
                sanitize_text(f"{type(exc).__name__}: {exc}", request.app.state.secrets),
            )
            response = _error(request, 500, "internal error")
        response.headers["X-Request-ID"] = rid
        elapsed_ms = (time.perf_counter() - started) * 1000
        access_log.info(
            "%s %s %s %.1fms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            rid,
        )
        writer: RequestLogWriter | None = request.app.state.request_log
        if writer is not None and not request.url.path.startswith("/health/"):
            _record(writer, request, response, received, elapsed_ms, rid)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        return _error(request, exc.status_code, exc.detail, dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        details = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return _error(request, 422, details)

    # ------------------------------------------------------------------ health
    @app.get("/health/live", tags=["health"], summary="Process is up")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], summary="Database reachable, migrations current")
    def ready(request: Request) -> Response:
        db: Database | None = request.app.state.database
        body: dict[str, Any] = {"status": "ok", "version": __version__, "database": "ok"}
        if db is None:
            return _error(request, 503, "database not configured")
        try:
            with db.connection() as conn:
                conn.execute("SELECT 1")
                applied = applied_migrations(conn)
        except Exception as exc:
            log.warning("readiness: database unreachable: %s", type(exc).__name__)
            body.update(status="not_ready", database="unreachable")
            return json_response(request, body, status_code=503, etag=False)
        files = discover_migrations()
        pending = [m.version for m in files if m.version not in applied]
        changed = [
            m.version for m in files if m.version in applied and applied[m.version] != m.checksum
        ]
        body["migrations"] = {
            "applied": len(applied),
            "known": len(files),
            "pending": len(pending),
            "checksum_mismatch": len(changed),
        }
        if pending or changed:
            body["status"] = "not_ready"
            return json_response(request, body, status_code=503, etag=False)
        return json_response(request, body, etag=False)

    # ---------------------------------------------------------------------- v1
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])
    v1.include_router(ops.router)
    v1.include_router(bonds.router)
    v1.include_router(market.router)
    v1.include_router(export.router)
    v1.include_router(snapshots.router)
    v1.include_router(history.router)

    @v1.get("/openapi.json", include_in_schema=False)
    def openapi() -> dict[str, Any]:
        return app.openapi()

    app.include_router(v1)
    return app


app = create_app()
