"""Request plumbing shared by the console's routes: configuration, client IP and allow-list,
session dependency with CSRF, role and step-up checks, templates and hashed static assets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Final
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, PackageLoader, select_autoescape

from mdhub import __version__
from mdhub.admin import security, store
from mdhub.admin.factors import RelyingParty
from mdhub.admin.store import Role, Session, Stage, User
from mdhub.config import Settings
from mdhub.db.pool import Database, DictConnection

__all__ = [
    "COOKIE",
    "AdminConfig",
    "Ctx",
    "Redirect",
    "client_address",
    "parse_networks",
    "render",
    "require",
]

log = logging.getLogger("mdhub.admin")

COOKIE: Final = "__Host-mdhub_admin"
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(value: str | None) -> tuple[Network, ...]:
    """Comma- or space-separated IPs / CIDRs (bad entries raise ``ValueError``)."""
    items = [v for v in re.split(r"[\s,]+", value or "") if v]
    return tuple(ipaddress.ip_network(v, strict=False) for v in items)


def _in(ip: str | None, networks: Iterable[Network]) -> bool:
    if ip is None:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in n for n in networks)


@dataclass(frozen=True, slots=True)
class AdminConfig:
    """Console settings derived from :class:`Settings` (``MDHUB_ADMIN_*``)."""

    rp: RelyingParty | None
    allow: tuple[Network, ...]
    proxies: tuple[Network, ...]
    secret_key: bytes | None
    problems: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> AdminConfig:
        problems: list[str] = []
        rp = None
        if settings.admin_domain:
            domain = settings.admin_domain.strip().lower()
            origin = (settings.admin_origin or f"https://{domain}").rstrip("/")
            if urlsplit(origin).hostname != domain:
                problems.append("MDHUB_ADMIN_ORIGIN does not match MDHUB_ADMIN_DOMAIN")
            else:
                rp = RelyingParty(rp_id=domain, origin=origin)
        else:
            problems.append("MDHUB_ADMIN_DOMAIN is not set")
        allow: tuple[Network, ...] = ()
        proxies: tuple[Network, ...] = ()
        try:
            allow = parse_networks(settings.admin_allow_cidrs)
        except ValueError:
            problems.append("MDHUB_ADMIN_ALLOW_CIDRS has an invalid entry")
        try:
            proxies = parse_networks(settings.admin_trusted_proxies)
        except ValueError:
            problems.append("MDHUB_ADMIN_TRUSTED_PROXIES has an invalid entry")
        if not allow:
            problems.append("MDHUB_ADMIN_ALLOW_CIDRS is empty: every request is refused")
        key = None
        try:
            raw = settings.admin_secret_key
            key = security.parse_secret_key(raw.get_secret_value() if raw else None)
        except security.SecretKeyError as exc:
            problems.append(str(exc))
        return cls(rp=rp, allow=allow, proxies=proxies, secret_key=key, problems=tuple(problems))

    @property
    def origin(self) -> str | None:
        return self.rp.origin if self.rp else None

    def allowed(self, ip: str | None) -> bool:
        return _in(ip, self.allow)


def client_address(
    peer: str | None, forwarded_for: str | None, proxies: Iterable[Network]
) -> str | None:
    """Client IP: the peer, or - only when the peer is a trusted proxy - the right-most
    ``X-Forwarded-For`` entry that is not itself a trusted proxy."""
    proxies = tuple(proxies)
    if not _in(peer, proxies) or not forwarded_for:
        return peer
    for hop in reversed([h.strip() for h in forwarded_for.split(",")]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return None  # garbage from the proxy: refuse rather than guess
        if not _in(hop, proxies):
            return hop
    return peer


# --------------------------------------------------------------------------------- assets


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    body: bytes
    digest: str
    integrity: str
    media_type: str

    @property
    def url(self) -> str:
        return f"/static/{self.digest}/{self.name}"


def load_assets() -> dict[str, Asset]:
    out: dict[str, Asset] = {}
    types = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
    for entry in files("mdhub.admin").joinpath("static").iterdir():
        suffix = "." + entry.name.rsplit(".", 1)[-1]
        if suffix not in types:
            continue
        body = entry.read_bytes()
        sri = base64.b64encode(hashlib.sha384(body).digest()).decode()
        out[entry.name] = Asset(
            name=entry.name,
            body=body,
            digest=hashlib.sha256(body).hexdigest()[:16],
            integrity=f"sha384-{sri}",
            media_type=types[suffix],
        )
    return out


# ------------------------------------------------------------------------------ templates


def _fmt_dt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _fmt_age(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    seconds = int(((now or datetime.now(UTC)) - value).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        text = f"{seconds} s"
    elif seconds < 5400:
        text = f"{seconds // 60} min"
    elif seconds < 172800:
        text = f"{seconds // 3600} h"
    else:
        text = f"{seconds // 86400} d"
    return f"in {text}" if future else f"{text} ago"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        return f"{value:,.1f}".replace(",", " ")
    return str(value)


ENV = Environment(
    loader=PackageLoader("mdhub.admin", "templates"),
    autoescape=select_autoescape(default=True),
    trim_blocks=True,
    lstrip_blocks=True,
)
ENV.filters["dt"] = _fmt_dt
ENV.filters["ago"] = _fmt_age
ENV.filters["num"] = _fmt_num
ENV.globals["version"] = __version__

NAV: Final[tuple[tuple[str, str, Role], ...]] = (
    ("/", "Overview", "viewer"),
    ("/jobs", "Jobs", "viewer"),
    ("/data", "Data", "viewer"),
    ("/sources", "Sources", "viewer"),
    ("/traffic", "API traffic", "viewer"),
    ("/tokens", "Tokens", "admin"),
    ("/users", "Users", "admin"),
    ("/audit", "Audit", "admin"),
    ("/updates", "Updates", "admin"),
)


# ------------------------------------------------------------------------------- context


class Redirect(Exception):  # noqa: N818 - control flow, not an error
    """Answer with a 303 to ``location`` (raised by dependencies)."""

    def __init__(self, location: str) -> None:
        self.location = location


class Denied(Exception):  # noqa: N818
    """Answer with an error page / JSON error of ``status``."""

    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass(slots=True)
class Ctx:
    """One request: database, config, client IP and the loaded session (if any)."""

    request: Request
    session: Session | None
    flash: list[str]

    @property
    def app_state(self) -> Any:
        return self.request.app.state

    @property
    def db(self) -> Database:
        db: Database = self.app_state.database
        return db

    @property
    def cfg(self) -> AdminConfig:
        cfg: AdminConfig = self.app_state.admin
        return cfg

    @property
    def ip(self) -> str | None:
        ip: str | None = self.request.state.client_ip
        return ip

    @property
    def user(self) -> User:
        assert self.session is not None
        assert self.session.user is not None
        return self.session.user

    @property
    def user_agent(self) -> str | None:
        return self.request.headers.get("user-agent")

    def audit(self, conn: DictConnection, action: str, **kwargs: Any) -> None:
        store.audit(conn, action, session=self.session, ip=self.ip, **kwargs)

    @property
    def settings(self) -> Settings:
        s: Settings = self.app_state.settings
        return s


def render(
    ctx: Ctx | Request,
    template: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **values: Any,
) -> HTMLResponse:
    request = ctx.request if isinstance(ctx, Ctx) else ctx
    session = ctx.session if isinstance(ctx, Ctx) else None
    user = session.user if session and session.stage == "full" else None
    assets: dict[str, Asset] = request.app.state.assets
    body = ENV.get_template(template).render(
        request=request,
        session=session,
        user=user,
        csrf=session.csrf_token if session else "",
        assets=assets,
        nav=[(href, label) for href, label, role in NAV if user and user.at_least(role)],
        path=request.url.path,
        **values,
    )
    return HTMLResponse(body, status_code=status_code, headers=headers)


def safe_next(value: str | None, default: str = "/") -> str:
    """A local path to go to after login / step-up (no scheme, host or ``//``)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def load(request: Request) -> Ctx:
    """Context with the session of the cookie (``None`` when missing or dead)."""
    db: Database = request.app.state.database
    cookie = request.cookies.get(COOKIE)
    session = None
    if cookie:
        with db.connection() as conn:
            session = store.load_session(conn, cookie)
    return Ctx(request=request, session=session, flash=[])


def _unsafe(request: Request) -> bool:
    return request.method not in ("GET", "HEAD", "OPTIONS")


async def check_csrf(request: Request, session: Session | None, cfg: AdminConfig) -> None:
    """Synchronizer token (header ``X-CSRF-Token`` or form field ``csrf_token``) of the
    session, plus a same-origin ``Origin`` / ``Sec-Fetch-Site``."""
    origin = request.headers.get("origin")
    if origin == "null":  # browsers send "null" on form posts under Referrer-Policy: no-referrer
        origin = None
    site = request.headers.get("sec-fetch-site")
    if origin is None and site is None:
        raise Denied(403, "Request refused (cross-site check).")
    if origin is not None and origin != cfg.origin:
        raise Denied(403, "Request refused (cross-site check).")
    if site is not None and site != "same-origin":
        raise Denied(403, "Request refused (cross-site check).")
    token = request.headers.get("x-csrf-token")
    if token is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        value = (await request.form()).get("csrf_token")
        token = value if isinstance(value, str) else None
    if session is None or not token or not hmac.compare_digest(token, session.csrf_token):
        raise Denied(403, "Request refused (expired form). Reload the page and try again.")


def require(
    role: Role = "viewer", *, step_up: bool = False, stages: tuple[Stage, ...] = ("full",)
) -> Callable[[Request], Awaitable[Ctx]]:
    """Dependency: a session in one of ``stages`` (login otherwise), CSRF on unsafe methods,
    at least ``role`` for full sessions and, with ``step_up``, a second-factor check within
    the last 5 minutes."""

    async def dependency(request: Request) -> Ctx:
        ctx = load(request)
        s = ctx.session
        if s is None or s.stage not in stages:
            if _unsafe(request) or request.url.path.startswith("/webauthn/"):
                raise Denied(401, "Your session has expired. Log in again.")
            if stages == ("full",):
                raise Redirect("/login?next=" + quote(request.url.path))
            raise Redirect("/login")
        if _unsafe(request):
            await check_csrf(request, s, ctx.cfg)
        if s.stage == "full" and s.user is not None and not s.user.at_least(role):
            with ctx.db.connection() as conn:
                ctx.audit(
                    conn,
                    "access.denied",
                    target=request.url.path,
                    details={"method": request.method, "needs": role},
                    outcome="denied",
                )
            raise Denied(403, "Your role does not allow this.")
        if step_up and not s.stepped_up:
            if request.url.path.startswith("/webauthn/"):
                raise Denied(403, "Confirm your identity first.")
            # No Referer (Referrer-Policy: no-referrer): go back to the page of the action.
            back = "/" + request.url.path.split("/")[1] if _unsafe(request) else request.url.path
            raise Redirect("/step-up?next=" + quote(back))
        return ctx

    return dependency


def set_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        COOKIE,
        value,
        max_age=int(store.ABSOLUTE_LIFETIME.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
