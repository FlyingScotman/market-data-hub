"""Exception hierarchy shared by sources, loaders and jobs."""

from __future__ import annotations

__all__ = ["FormatDriftError", "JobSkipped", "MdhubError"]


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
