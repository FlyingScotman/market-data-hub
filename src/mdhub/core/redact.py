"""Redaction of secrets from URLs, error texts and log records."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

__all__ = ["SecretRedactingFilter", "is_sensitive_param", "redact_url", "sanitize_text"]

_SENSITIVE_PARAM = re.compile(
    r"key|token|passw|pwd|login|secret|auth|session|signature|credential|cert",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
REDACTED = "***"


def is_sensitive_param(name: str) -> bool:
    """True if a query parameter name looks like it carries a credential."""
    return bool(_SENSITIVE_PARAM.search(name))


def redact_url(url: str | httpx.URL) -> str:
    """Return ``url`` without userinfo, fragment and credential-like query parameters.

    Parameters whose names contain key/token/password/login/secret/auth/session/... are
    removed entirely; all other parameters are kept in their original order.

    >>> redact_url("https://u:p@h.example/x?apikey=1&date=2026-01-01#f")
    'https://h.example/x?date=2026-01-01'
    """
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port is not None else "")
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not is_sensitive_param(k)
        ],
        safe=",:/",
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def sanitize_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact URLs (via :func:`redact_url`) and literal secret values in ``text``."""

    def _url(m: re.Match[str]) -> str:
        try:
            return redact_url(m.group(0))
        except ValueError:
            return REDACTED

    out = _URL_IN_TEXT.sub(_url, text)
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(s, REDACTED)
    return out


class SecretRedactingFilter(logging.Filter):
    """Logging filter that removes secret values and URL credentials from records.

    Attach to handlers (not loggers) so it applies to records from every logger.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = sanitize_text(message, self._secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = sanitize_text(record.exc_text, self._secrets)
        return True
