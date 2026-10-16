"""Outbound HTTP: one hardened :class:`HttpClient` for every source.

Features (DESIGN §2):

* TLS: certifi bundle **plus** the bundled Russian Trusted Root CA (needed by
  ``iss.moex.com``/``apim.moex.com``) in one :class:`ssl.SSLContext`, used for all hosts.
* Honest ``User-Agent: market-data-hub/<version>``.
* Per-host token-bucket rate limiting, shared process-wide by default
  (:data:`GLOBAL_RATE_LIMITER`), so concurrent jobs hitting ISS stay within ``MDHUB_ISS_RPS``.
* Retries with exponential backoff (+ ``Retry-After``) on transport errors, 429 and 5xx -
  for idempotent methods only unless ``retry=True`` is passed.
* No cross-host redirects (same-host redirects for GET/HEAD are followed, up to 5).
* Optional egress proxy from ``MDHUB_HTTP_PROXY``.
* :class:`EntitlementError` when a JSON endpoint answers 200 ``text/html`` (MOEX
  "subscribers only" page).
* Error messages carry only redacted URLs (:func:`redact_url`).

Typical use::

    with HttpClient(settings=ctx.settings, base_url=ISS_BASE_URL) as http:
        res = http.get_json("/history/engines/stock/markets/bonds/securities.json",
                            params={"date": "2026-10-15", "iss.meta": "off"})
        res.response   # httpx.Response (store with record_capture)
        res.data       # parsed JSON
"""

from __future__ import annotations

import email.utils
import logging
import random
import re
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from types import TracebackType
from typing import Any, Final, Self

import certifi
import httpx

from mdhub import USER_AGENT
from mdhub.config import Settings
from mdhub.core.errors import MdhubError
from mdhub.core.redact import redact_url

__all__ = [
    "GLOBAL_RATE_LIMITER",
    "EntitlementError",
    "HttpClient",
    "HttpError",
    "HttpStatusError",
    "JsonResult",
    "RateLimiter",
    "RedirectError",
    "ResponseFormatError",
    "TokenBucket",
    "TransportFailure",
    "build_ssl_context",
    "default_rate_limits",
    "redact_url",
]

log = logging.getLogger(__name__)

RU_CA_RESOURCE: Final = "russian_trusted_root_ca.pem"
IDEMPOTENT_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
DEFAULT_HOST_RPS: Final = 5.0
"""Rate for hosts without an explicit limit."""


# --------------------------------------------------------------------------- errors


class HttpError(MdhubError):
    """Base class for HTTP failures. ``url`` is always redacted."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


class TransportFailure(HttpError):  # noqa: N818 - "failure" reads better than "error" here
    """Connection/timeout/protocol error after all retries."""


class HttpStatusError(HttpError):
    """Non-2xx final response (after retries)."""

    def __init__(self, status_code: int, *, url: str, body_snippet: str = "") -> None:
        msg = f"HTTP {status_code} for {url}"
        if body_snippet:
            msg += f": {body_snippet}"
        super().__init__(msg, url=url)
        self.status_code = status_code
        self.body_snippet = body_snippet


class EntitlementError(HttpError):
    """JSON was expected but the server answered 200 ``text/html`` (no subscription)."""


class ResponseFormatError(HttpError):
    """JSON was expected but the response is not JSON (wrong content type / bad body)."""


class RedirectError(HttpError):
    """Redirect to another host/scheme, too many redirects, or redirect of a non-GET."""


# ------------------------------------------------------------------------------ TLS


@lru_cache(maxsize=1)
def build_ssl_context() -> ssl.SSLContext:
    """SSL context trusting certifi's bundle plus the Russian Trusted Root CA."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    pem = resources.files("mdhub.certs").joinpath(RU_CA_RESOURCE).read_text("ascii")
    ctx.load_verify_locations(cadata=pem)
    return ctx


# ----------------------------------------------------------------------- rate limits


class TokenBucket:
    """Thread-safe token bucket: ``rate`` tokens/second, burst ``capacity``.

    :meth:`acquire` reserves a token (the balance may go negative) and sleeps outside the
    lock until the reservation is due, so waiters are served in FIFO order.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._rate = float(rate)
        self._capacity = float(capacity) if capacity is not None else max(1.0, float(rate))
        self._tokens = self._capacity
        self._updated = clock()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float, capacity: float | None = None) -> None:
        """Change the rate (and capacity, default ``max(1, rate)``)."""
        if rate <= 0:
            raise ValueError("rate must be > 0")
        with self._lock:
            self._refill()
            self._rate = float(rate)
            self._capacity = float(capacity) if capacity is not None else max(1.0, float(rate))
            self._tokens = min(self._tokens, self._capacity)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def acquire(self, tokens: float = 1.0) -> float:
        """Take ``tokens``; block as needed. Returns the seconds waited."""
        with self._lock:
            self._refill()
            self._tokens -= tokens
            wait = 0.0 if self._tokens >= 0 else -self._tokens / self._rate
        if wait > 0:
            self._sleep(wait)
        return wait


class RateLimiter:
    """Per-host collection of :class:`TokenBucket` objects.

    :param default_rps: rate for hosts without an explicit rate (``None`` = unlimited).
    """

    def __init__(
        self,
        default_rps: float | None = DEFAULT_HOST_RPS,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._default = default_rps
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def set_rate(self, host: str, rps: float) -> None:
        """Set the rate for ``host`` (case-insensitive)."""
        host = host.lower()
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                self._buckets[host] = TokenBucket(rps, clock=self._clock, sleep=self._sleep)
            elif bucket.rate != rps:
                bucket.set_rate(rps)

    def rate_for(self, host: str) -> float | None:
        """Configured rate for ``host`` (``None`` = unlimited)."""
        with self._lock:
            bucket = self._buckets.get(host.lower())
        return bucket.rate if bucket else self._default

    def acquire(self, host: str) -> float:
        """Block until a request to ``host`` is allowed; returns seconds waited."""
        host = host.lower()
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                if self._default is None:
                    return 0.0
                bucket = TokenBucket(self._default, clock=self._clock, sleep=self._sleep)
                self._buckets[host] = bucket
        return bucket.acquire()


GLOBAL_RATE_LIMITER: Final = RateLimiter()
"""Process-wide limiter shared by all :class:`HttpClient` instances by default."""


def default_rate_limits(settings: Settings) -> dict[str, float]:
    """Per-host request rates (requests/second) used by :class:`HttpClient`."""
    return {
        "iss.moex.com": settings.iss_rps,
        "apim.moex.com": settings.iss_rps,
        "passport.moex.com": 1.0,
        "www.cbr.ru": 1.0,
        "cbr.ru": 1.0,
        "ratescalcs.ru": 1.0,
        "otc-monitor.ru": 1.0,
        "ws2.cbonds.info": 2.0,
        "ws.cbonds.info": 2.0,
        "www.ecb.europa.eu": 2.0,
    }


# --------------------------------------------------------------------------- client


@dataclass(frozen=True, slots=True)
class JsonResult:
    """Result of :meth:`HttpClient.get_json`: the raw response and the parsed body."""

    response: httpx.Response
    data: Any


class HttpClient:
    """``httpx.Client`` wrapper with retries, rate limiting, TLS and entitlement checks.

    :param settings: provides proxy, timeout and ISS rate (default: ``Settings()``).
    :param base_url: optional base URL for relative request paths.
    :param headers: extra default headers (``User-Agent`` is always set unless overridden).
    :param auth: httpx auth (e.g. ``httpx.BasicAuth``); prefer headers built from env secrets.
    :param rate_limits: per-host rps overrides merged over :func:`default_rate_limits`.
    :param limiter: rate limiter (default: process-wide :data:`GLOBAL_RATE_LIMITER`).
    :param max_retries: retries after the first attempt (default 4 => up to 5 attempts).
    :param backoff_base: first backoff in seconds, doubled per retry, capped at
        ``backoff_max``, with +-20 % jitter.
    :param transport: custom httpx transport (tests).
    :param sleep: sleep function used for backoff (tests).
    :param proxy: explicit proxy URL; default from ``settings.http_proxy``.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        base_url: str = "",
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout: float | None = None,
        rate_limits: Mapping[str, float] | None = None,
        limiter: RateLimiter | None = None,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_redirects: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        proxy: str | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_redirects = max_redirects
        self._sleep = sleep
        self._limiter = limiter if limiter is not None else GLOBAL_RATE_LIMITER
        for host, rps in {**default_rate_limits(self.settings), **(rate_limits or {})}.items():
            self._limiter.set_rate(host, rps)
        all_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        all_headers.update(headers or {})
        proxy_url = proxy if proxy is not None else self.settings.proxy_url()
        self._client = httpx.Client(
            base_url=base_url,
            headers=all_headers,
            auth=auth,
            timeout=timeout if timeout is not None else self.settings.http_timeout,
            verify=build_ssl_context(),
            follow_redirects=False,
            proxy=proxy_url if transport is None else None,
            transport=transport,
            trust_env=False,
        )

    # -------------------------------------------------------------- context manager

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def cookies(self) -> httpx.Cookies:
        """Session cookies (e.g. MOEX Passport ``MicexPassportCert``)."""
        return self._client.cookies

    # ------------------------------------------------------------------- requests

    def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | None = None,
        json: Any = None,
        expect_json: bool = False,
        raise_for_status: bool = True,
        retry: bool | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send a request with rate limiting, retries and redirect policy.

        :param expect_json: verify the final 2xx response is JSON; raise
            :class:`EntitlementError` for 200 ``text/html`` and
            :class:`ResponseFormatError` for any other non-JSON content type.
        :param raise_for_status: raise :class:`HttpStatusError` for non-2xx final responses.
        :param retry: force retry behaviour; default retries idempotent methods only.
        :returns: the final response with its body read.
        """
        method = method.upper()
        retry_enabled = method in IDEMPOTENT_METHODS if retry is None else retry
        extensions: dict[str, Any] = {}
        if timeout is not None:
            extensions["timeout"] = httpx.Timeout(timeout).as_dict()
        req = self._client.build_request(
            method,
            url,
            params=_clean_params(params),
            headers=headers,
            content=content,
            data=data,
            json=json,
            extensions=extensions or None,
        )
        origin = req.url
        attempt = 0
        redirects = 0
        while True:
            self._limiter.acquire(req.url.host)
            try:
                response = self._client.send(req)
            except httpx.TransportError as exc:
                if retry_enabled and attempt < self.max_retries:
                    delay = self._backoff(attempt)
                    log.warning(
                        "%s %s: %s; retry %d/%d in %.1fs",
                        method,
                        redact_url(req.url),
                        type(exc).__name__,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise TransportFailure(
                    f"{type(exc).__name__} for {redact_url(req.url)} "
                    f"after {attempt + 1} attempt(s)",
                    url=redact_url(req.url),
                ) from None

            if response.is_redirect:
                req = self._redirect_request(req, response, origin, redirects)
                redirects += 1
                continue

            status = response.status_code
            if retry_enabled and _retryable(status) and attempt < self.max_retries:
                retry_after = _retry_after(response, self.backoff_max)
                wait = self._backoff(attempt) if retry_after is None else retry_after
                response.close()
                log.warning(
                    "%s %s: HTTP %d; retry %d/%d in %.1fs",
                    method,
                    redact_url(req.url),
                    status,
                    attempt + 1,
                    self.max_retries,
                    wait,
                )
                self._sleep(wait)
                attempt += 1
                continue
            break

        if raise_for_status and not response.is_success:
            raise HttpStatusError(
                response.status_code, url=redact_url(req.url), body_snippet=_snippet(response)
            )
        if expect_json and response.is_success:
            check_json_response(response)
        return response

    def get(
        self,
        url: str | httpx.URL,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = False,
        raise_for_status: bool = True,
        timeout: float | None = None,
    ) -> httpx.Response:
        """GET (see :meth:`request`)."""
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            expect_json=expect_json,
            raise_for_status=raise_for_status,
            timeout=timeout,
        )

    def get_json(
        self,
        url: str | httpx.URL,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> JsonResult:
        """GET a JSON document; raises :class:`EntitlementError` on an HTML 200."""
        response = self.get(url, params=params, headers=headers, expect_json=True, timeout=timeout)
        try:
            data = response.json()
        except ValueError as exc:
            raise ResponseFormatError(
                f"invalid JSON from {redact_url(response.request.url)}: {exc}",
                url=redact_url(response.request.url),
            ) from None
        return JsonResult(response=response, data=data)

    # -------------------------------------------------------------------- helpers

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_base * (2**attempt))
        return float(base * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, not crypto

    def _redirect_request(
        self,
        req: httpx.Request,
        response: httpx.Response,
        origin: httpx.URL,
        redirects: int,
    ) -> httpx.Request:
        location = response.headers.get("location")
        response.close()
        if not location:
            raise RedirectError(
                f"redirect without Location from {redact_url(req.url)}", url=redact_url(req.url)
            )
        target = req.url.join(location)
        if target.host.lower() != origin.host.lower() or (
            origin.scheme == "https" and target.scheme != "https"
        ):
            raise RedirectError(
                f"refusing cross-host redirect {redact_url(req.url)} -> {redact_url(target)}",
                url=redact_url(req.url),
            )
        if req.method not in {"GET", "HEAD"}:
            raise RedirectError(
                f"refusing to follow redirect of {req.method} {redact_url(req.url)}",
                url=redact_url(req.url),
            )
        if redirects >= self.max_redirects:
            raise RedirectError(
                f"too many redirects from {redact_url(origin)}", url=redact_url(origin)
            )
        headers = {k: v for k, v in req.headers.items() if k.lower() != "host"}
        return self._client.build_request(
            req.method, target, headers=headers, extensions=req.extensions
        )


def check_json_response(response: httpx.Response) -> None:
    """Raise unless ``response`` declares a JSON content type.

    :raises EntitlementError: 200 with ``text/html`` (MOEX "subscribers only").
    :raises ResponseFormatError: any other non-JSON content type.
    """
    ctype = response.headers.get("content-type", "").lower()
    url = redact_url(response.request.url)
    if "json" in ctype:
        return
    if "text/html" in ctype:
        raise EntitlementError(
            f"HTML instead of JSON from {url} (entitlement missing?): {_snippet(response)}",
            url=url,
        )
    raise ResponseFormatError(
        f"expected JSON from {url}, got {ctype or 'no content-type'}", url=url
    )


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _retry_after(response: httpx.Response, cap: float) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return max(0.0, min(cap, seconds))


def _snippet(response: httpx.Response, limit: int = 200) -> str:
    try:
        text = response.text
    except (UnicodeDecodeError, httpx.ResponseNotRead):
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:limit]
