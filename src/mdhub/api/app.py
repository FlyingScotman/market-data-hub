"""FastAPI read API of the market data hub (DESIGN §8). Entry point ``mdhub.api.app:app``.

* ``/health/live`` (process up) and ``/health/ready`` (database reachable and all migrations
  applied with matching checksums) are unauthenticated.
* Everything under ``/v1`` requires ``Authorization: Bearer <token>`` with a token from
  ``MDHUB_API_TOKENS`` (see :mod:`mdhub.api.auth`); the OpenAPI document is served at
  ``/v1/openapi.json`` behind the same check.
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
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from mdhub import __version__
from mdhub.api.auth import require_token, token_digests
from mdhub.api.common import DecimalJSONResponse, json_response
from mdhub.api.routes import bonds, export, market, ops, snapshots
from mdhub.config import Settings
from mdhub.core.redact import SecretRedactingFilter, sanitize_text
from mdhub.db.migrate import applied_migrations, discover_migrations
from mdhub.db.pool import Database

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
        app.state.token_digests = token_digests(cfg.api_token_list())
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
        if not app.state.token_digests:
            log.warning("MDHUB_API_TOKENS is empty: every /v1 request will be rejected")
        try:
            yield
        finally:
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
    app.state.token_digests = ()
    app.state.database = None
    app.state.secrets = []

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = _request_id(request)
        request.state.request_id = rid
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
        access_log.info(
            "%s %s %s %.1fms [%s]",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            rid,
        )
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
        body: dict[str, Any] = {"status": "ok", "database": "ok"}
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

    @v1.get("/openapi.json", include_in_schema=False)
    def openapi() -> dict[str, Any]:
        return app.openapi()

    app.include_router(v1)
    return app


app = create_app()
