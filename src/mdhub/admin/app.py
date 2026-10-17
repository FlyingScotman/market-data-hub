"""Admin console of the market data hub (ADMIN_DESIGN §2, §4, §5). Entry point
``mdhub.admin.app:app`` (``uvicorn mdhub.admin.app:app --host 127.0.0.1 --port 8081``).

A separate ASGI app: the read API (:mod:`mdhub.api.app`) imports none of this package.

* **Allow-list.** Every request is refused (403) unless the client IP is in
  ``MDHUB_ADMIN_ALLOW_CIDRS``; ``X-Forwarded-For`` is used only when the peer is in
  ``MDHUB_ADMIN_TRUSTED_PROXIES`` (Caddy). An empty list refuses everything (fail closed), and
  so does a missing ``MDHUB_ADMIN_DOMAIN`` (503).
* **Headers.** HSTS (2 years, preload-ready), CSP ``default-src 'self'`` without inline
  scripts or styles and ``frame-ancestors 'none'``, ``nosniff``, ``Referrer-Policy:
  no-referrer``, COOP/CORP ``same-origin``, a ``Permissions-Policy`` that allows only WebAuthn,
  ``Cache-Control: no-store`` on every page. Static assets are served under their content
  hash (immutable) and carry SRI hashes.
* **Pages** are server-rendered Jinja2 with vendored htmx (Overview refreshes every 15 s).
  Auth: :mod:`mdhub.admin.auth`; pages and actions: :mod:`mdhub.admin.pages`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from mdhub import __version__
from mdhub.admin import auth, credentials, pages
from mdhub.admin.web import (
    AdminConfig,
    Asset,
    Denied,
    Redirect,
    client_address,
    load_assets,
    render,
)
from mdhub.config import Settings
from mdhub.core.redact import SecretRedactingFilter, sanitize_text
from mdhub.db.pool import Database
from mdhub.jobs import discover_jobs

__all__ = ["SECURITY_HEADERS", "app", "create_app"]

log = logging.getLogger("mdhub.admin")

ADMIN_POOL_SIZE: Final = 6
CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
SECURITY_HEADERS: Final = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "publickey-credentials-create=(self), publickey-credentials-get=(self), "
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
    ),
}


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/webauthn/")


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    """Build the console. Settings and database are resolved at startup when omitted; a
    ``database`` passed in is not closed by the app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings()
        app.state.settings = cfg
        app.state.secrets = cfg.secret_values()
        app.state.admin = AdminConfig.from_settings(cfg)
        for problem in app.state.admin.problems:
            log.warning("admin console: %s", problem)
        if settings is None:
            flt = SecretRedactingFilter(app.state.secrets)
            for handler in logging.getLogger().handlers:
                handler.addFilter(flt)
            logging.getLogger("mdhub").setLevel(cfg.log_level)
        owned: Database | None = None
        if database is not None:
            app.state.database = database
        else:
            owned = Database.from_settings(cfg, pool_size=ADMIN_POOL_SIZE)
            app.state.database = owned
        report = discover_jobs()
        for module, err in report.failed.items():
            log.warning("job module %s failed to import: %s", module, err)
        try:
            yield
        finally:
            if owned is not None:
                owned.close()

    app = FastAPI(
        title="market-data-hub admin",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.assets = load_assets()
    app.state.secrets = []

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cfg: AdminConfig = request.app.state.admin
        peer = request.client.host if request.client else None
        ip = client_address(peer, request.headers.get("x-forwarded-for"), cfg.proxies)
        request.state.client_ip = ip
        if request.url.path == "/health/live" and peer in ("127.0.0.1", "::1") and ip == peer:
            response: Response = PlainTextResponse("ok")  # container health check
        elif not cfg.allowed(ip):
            response = PlainTextResponse("Forbidden", status_code=403)
        elif cfg.rp is None:
            response = PlainTextResponse("Admin console not configured", status_code=503)
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                log.error(
                    "request %s %s failed: %s",
                    request.method,
                    request.url.path,
                    sanitize_text(f"{type(exc).__name__}: {exc}", request.app.state.secrets),
                )
                response = PlainTextResponse("Internal error", status_code=500)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Redirect)
    async def redirect(request: Request, exc: Redirect) -> Response:
        return RedirectResponse(exc.location, status_code=303)

    @app.exception_handler(Denied)
    async def denied(request: Request, exc: Denied) -> Response:
        if _wants_json(request):
            return JSONResponse({"error": exc.message}, status_code=exc.status, headers=exc.headers)
        return render(
            request,
            "message.html",
            status_code=exc.status,
            headers=exc.headers,
            title="Not allowed" if exc.status in (401, 403) else "Error",
            message=exc.message,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = "Not found." if exc.status_code == 404 else str(exc.detail)
        if _wants_json(request):
            return JSONResponse({"error": message}, status_code=exc.status_code)
        return render(
            request, "message.html", status_code=exc.status_code, title="Error", message=message
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        if _wants_json(request):
            return JSONResponse({"error": "invalid request"}, status_code=422)
        return render(
            request,
            "message.html",
            status_code=422,
            title="Invalid input",
            message="Some fields were missing or invalid. Go back and check them.",
        )

    @app.get("/static/{digest}/{name}", include_in_schema=False)
    def static(digest: str, name: str) -> Response:
        assets: dict[str, Asset] = app.state.assets
        asset = assets.get(name)
        if asset is None or asset.digest != digest:
            return PlainTextResponse("Not found", status_code=404)
        return Response(
            asset.body,
            media_type=asset.media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(credentials.router)
    return app


app = create_app()
