"""Fallback chains of independent sources (reference rates, proxy curves; WEB-16).

The production hub runs in Russia, where many European and U.S. hosts may be geo-blocked. A
loader with a chain tries its :class:`Attempt` objects in order and stores what the **first
source that answers** returned, under that source's own code (``md.fixing.source``,
``md.curve_snapshot.source``): sources are separate series, never merged (DESIGN §2). A source
answers when every request succeeds and the payload parses (fail closed: a format drift of one
source moves on to the next one, it is never partially stored); an empty but valid answer is an
answer ("no data in the range").

Every attempt but the last runs with one retry and a :data:`FALLBACK_TIMEOUT_S` timeout, so a
blocked host costs seconds, not minutes; the last runs with the job's normal HTTP settings.
``MDHUB_SKIP_SOURCES`` (:meth:`mdhub.config.Settings.skipped_sources`) drops sources from every
chain, a job's ``sources=`` parameter replaces the order. The result of a job lists every
attempt (``tried``: source, ok, error), so the owner sees which source served each run;
``mdhub probe-sources`` checks all of them from the server without writing anything.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from mdhub.core.errors import FormatDriftError, MdhubError
from mdhub.core.http import HttpClient, HttpError
from mdhub.core.redact import sanitize_text
from mdhub.jobs import JobContext, JobParamError

__all__ = ["FALLBACK_TIMEOUT_S", "Attempt", "Fetched", "Served", "SourcesFailed", "first_answer"]

FALLBACK_TIMEOUT_S: Final = 20.0


@dataclass(frozen=True, slots=True)
class Fetched:
    """One response of an attempt and its stable raw-capture endpoint name."""

    endpoint: str
    response: httpx.Response


@dataclass(frozen=True, slots=True)
class Attempt[T]:
    source: str
    """``ops.source.code`` the rows are stored under."""
    fetch: Callable[[HttpClient], list[Fetched]]
    parse: Callable[[list[Fetched]], T]
    parser_version: str
    note: str = ""
    """What this source is (``ECB administrator``, ``Bundesbank mirror``) for logs and docs."""


@dataclass(slots=True)
class Served[T]:
    attempt: Attempt[T]
    fetched: list[Fetched]
    parsed: T
    tried: list[dict[str, Any]] = field(default_factory=list)

    @property
    def source(self) -> str:
        return self.attempt.source


class SourcesFailed(MdhubError):  # noqa: N818 - reads as the outcome, like JobSkipped
    """No source of the chain answered."""

    def __init__(self, what: str, tried: list[dict[str, Any]]) -> None:
        errors = "; ".join(f"{t['source']}: {t['error']}" for t in tried) or "no source to try"
        super().__init__(f"{what}: no source answered ({errors})")
        self.tried = tried


def _order[T](
    ctx: JobContext, attempts: Sequence[Attempt[T]], sources: list[str] | None
) -> list[Attempt[T]]:
    by_source = {a.source: a for a in attempts}
    if sources:
        unknown = sorted(set(sources) - set(by_source))
        if unknown:
            raise JobParamError(f"unknown source(s) {unknown} (chain: {list(by_source)})")
        return [by_source[s] for s in sources]
    skipped = ctx.settings.skipped_sources()
    return [a for a in attempts if a.source not in skipped]


def first_answer[T](
    ctx: JobContext,
    what: str,
    attempts: Sequence[Attempt[T]],
    *,
    sources: list[str] | None = None,
) -> Served[T]:
    """Try ``attempts`` in order (``sources`` replaces it); return the first answer.

    :raises SourcesFailed: every source failed (the run then fails: an alert, never a silent
        empty day)."""
    chain = _order(ctx, attempts, sources)
    tried: list[dict[str, Any]] = []
    for i, attempt in enumerate(chain):
        last = i == len(chain) - 1
        kwargs: dict[str, Any] = {} if last else {"max_retries": 1, "timeout": FALLBACK_TIMEOUT_S}
        try:
            with ctx.http(**kwargs) as http:
                fetched = attempt.fetch(http)
            parsed = attempt.parse(fetched)
        except (HttpError, httpx.HTTPError, OSError, FormatDriftError) as exc:
            error = sanitize_text(f"{type(exc).__name__}: {exc}", ctx.settings.secret_values())
            tried.append({"source": attempt.source, "ok": False, "error": error[:300]})
            ctx.logger.warning("%s: source %s failed: %s", what, attempt.source, error[:300])
            continue
        tried.append({"source": attempt.source, "ok": True, "error": None})
        if i > 0:
            ctx.logger.warning(
                "%s: served by fallback source %s (%s)", what, attempt.source, attempt.note
            )
        return Served(attempt, fetched, parsed, tried)
    raise SourcesFailed(what, tried)
