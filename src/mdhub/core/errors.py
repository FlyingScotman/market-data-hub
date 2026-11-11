"""Exception hierarchy shared by sources, loaders and jobs."""

from __future__ import annotations

from typing import Final

__all__ = [
    "NOT_CONFIGURED",
    "FormatDriftError",
    "JobSkipped",
    "MdhubError",
    "SourceNotConfigured",
]

NOT_CONFIGURED: Final = "source_not_configured"
"""Start of the ``ops.job_run.error`` of a run skipped because its source is not configured
(credentials missing, source switched off): the backfill worker keeps the history load tasks that
end so waiting instead of counting them as failed attempts."""


class MdhubError(Exception):
    """Base class for all mdhub errors."""


class FormatDriftError(MdhubError, ValueError):
    """A source payload does not match the expected format (missing block/column, bad type).

    Parsers raise this and never return partial data (DESIGN §2 "fail closed").
    """


class JobSkipped(MdhubError):  # noqa: N818 - control-flow signal, not an error
    """Raise from a job to end it with status ``skipped`` (not ``failed``).

    Example: Cbonds answers HTTP 401 -> ``raise JobSkipped("cbonds auth 401")``.
    The message is stored in ``ops.job_run.error``.
    """


class SourceNotConfigured(JobSkipped):
    """A job is skipped because its source has no credentials / is switched off. The stored
    message starts with :data:`NOT_CONFIGURED` so that callers can tell it from other skips."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{NOT_CONFIGURED}: {reason}")
