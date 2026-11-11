"""Redaction of secrets from URLs, error texts and log records."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from mdhub.config import Settings

__all__ = [
    "SecretRedactingFilter",
    "hide_values",
    "is_sensitive_param",
    "redact_url",
    "register_secrets",
    "sanitize_text",
    "stored_text",
    "stored_url",
]

_SENSITIVE_PARAM = re.compile(
    r"key|token|passw|pwd|login|secret|auth|session|signature|credential|cert",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
REDACTED = "***"
_registered: frozenset[str] = frozenset()


def register_secrets(values: Iterable[str]) -> None:
    """Redact ``values`` everywhere from now on (:func:`sanitize_text`, hence every
    :class:`SecretRedactingFilter`), for secrets learnt after start-up: the source credentials
    the worker decrypts (:mod:`mdhub.credentials`). Values shorter than 4 characters are
    skipped, as in :meth:`mdhub.config.Settings.secret_values`."""
    global _registered
    new = {v for v in values if len(v) >= 4} - _registered
    if new:
        _registered = _registered | new  # rebind, never mutate: readers iterate without a lock


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
    if parts.scheme and not netloc and not parts.path.startswith("/"):
        # Preserve scheme:path identifiers (including relative file: names). Python
        # patch releases disagree about adding file:/// to such paths.
        return parts.scheme + ":" + urlunsplit(("", "", parts.path, query, ""))
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def sanitize_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact URLs (via :func:`redact_url`), literal ``secrets`` and the values of
    :func:`register_secrets` in ``text``."""

    def _url(m: re.Match[str]) -> str:
        try:
            return redact_url(m.group(0))
        except ValueError:
            return REDACTED

    out = _URL_IN_TEXT.sub(_url, text)
    for s in sorted({s for s in secrets if s} | _registered, key=len, reverse=True):
        out = out.replace(s, REDACTED)
    return out


def hide_values(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """Replace every ``value`` of ``pairs`` (``(value, placeholder)``) in ``text`` by its
    placeholder, ignoring case, longest value first, in one pass (a placeholder is never searched
    again). For texts that are stored where the read API or the read-only role can serve them: the
    configured address of a source must not leave the hub that way (the log keeps the full text)."""
    entries: dict[str, tuple[str, str]] = {}
    for value, placeholder in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if value:
            entries.setdefault(value.lower(), (value, placeholder))
    if not entries:
        return text
    pattern = re.compile("|".join(re.escape(v) for v, _ in entries.values()), re.IGNORECASE)

    def _placeholder(m: re.Match[str]) -> str:
        # The regex folds case by Unicode rules that ``str.lower`` does not follow (a long s, a
        # Kelvin sign, a dotted capital I), so the match is looked up by the same rules; a match
        # that belongs to no value cannot happen, but a stored text must never raise here.
        for value, placeholder in entries.values():
            if re.fullmatch(re.escape(value), m.group(0), re.IGNORECASE):
                return placeholder
        return REDACTED

    return pattern.sub(_placeholder, text)


def stored_text(text: str, settings: Settings | None) -> str:
    """``text`` as it may be **stored in the database**: every column of ``ops`` / ``md`` / ``ref``
    is readable by the read-only role and some by the read API, so the configured Ratescalcs address
    (:meth:`mdhub.config.Settings.hidden_addresses`) is replaced by its placeholder there. Use it
    for every text that carries a URL or an error of a source (``ops.job_run.error``,
    ``ops.source_probe``, ``ops.backfill_task.last_error``, ``ops.data_gap.note``, ...); the full
    text belongs in the log. ``settings=None``: unchanged."""
    return text if settings is None else hide_values(text, settings.hidden_addresses())


def stored_url(url: str | httpx.URL, settings: Settings | None) -> str:
    """:func:`redact_url` plus :func:`stored_text`: ``<MDHUB_RATESCALCS_BASE_URL>/snapshot`` for a
    page of the Ratescalcs source (the page path stays for diagnostics)."""
    return stored_text(redact_url(url), settings)


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
