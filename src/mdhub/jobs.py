"""Job registry, job context and the job runner.

A job is a function ``fn(ctx: JobContext, **params)`` registered with :func:`job`::

    from datetime import date
    from mdhub.jobs import JobContext, job

    @job("bonds_eod", "MOEX bonds end-of-day history by date")
    def bonds_eod(ctx: JobContext, *, mode: str = "final", days_back: list[int] = [1],
                  date: date | None = None) -> None:
        with ctx.http() as http, ctx.connection() as conn:
            ...
            ctx.run.add_counts(upsert_rows(conn, "md.bond_eod", rows, KEY, run_id=ctx.run.id))

:func:`run_job` wraps every execution in :func:`mdhub.core.capture.job_run` (one
``ops.job_run`` row; ``ctx.run`` is the handle) and a PostgreSQL advisory lock so the same job
never runs twice concurrently (across processes). Parameters arriving as strings (CLI
``--param k=v``) are converted according to the function's type annotations
(:func:`coerce_params`).

Job modules are found by :func:`discover_jobs`, which imports every module of the packages in
:data:`JOB_PACKAGES` with :mod:`pkgutil` - new loader modules need no registration anywhere.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
import types
import typing
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, TypeVar

from psycopg.rows import tuple_row

from mdhub.config import Settings
from mdhub.core.bizday import DEFAULT_CALENDAR, CalendarPredicate, weekday_calendar
from mdhub.core.capture import JobRun, job_run
from mdhub.core.errors import JobSkipped, MdhubError
from mdhub.core.http import HttpClient
from mdhub.core.timeutil import business_days_back, msk_today
from mdhub.db.pool import Database, DictConnection

__all__ = [
    "JOB_PACKAGES",
    "REGISTRY",
    "DiscoveryReport",
    "JobAlreadyRunning",
    "JobContext",
    "JobOutcome",
    "JobParamError",
    "JobRegistry",
    "JobSkipped",
    "JobSpec",
    "UnknownJobError",
    "coerce_params",
    "discover_jobs",
    "job",
    "parse_param_pairs",
    "run_job",
]

log = logging.getLogger(__name__)

JOB_PACKAGES: Final = ("mdhub.loaders", "mdhub.quality")
"""Packages whose modules are imported by :func:`discover_jobs` (missing ones are skipped)."""

F = TypeVar("F", bound=Callable[..., Any])


class UnknownJobError(MdhubError, KeyError):
    """No job with that name is registered."""


class JobParamError(MdhubError, ValueError):
    """Job parameters are invalid (unknown name, missing, or not convertible)."""


class JobAlreadyRunning(JobSkipped):
    """Another process/thread holds the job's advisory lock."""


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A registered job."""

    name: str
    description: str
    fn: Callable[..., Any]

    @property
    def module(self) -> str:
        return str(self.fn.__module__)

    def accepted_params(self) -> tuple[set[str], bool]:
        """``(keyword parameter names, accepts **kwargs)`` - excluding ``ctx``."""
        sig = inspect.signature(self.fn)
        params = list(sig.parameters.values())[1:]
        names = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
        var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
        return names, var_kw


class JobRegistry:
    """Name -> :class:`JobSpec`. The process-wide instance is :data:`REGISTRY`."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobSpec] = {}

    def register(self, name: str, description: str, fn: Callable[..., Any]) -> JobSpec:
        """Register ``fn`` as ``name``.

        Re-registering the same function (same module + qualname, e.g. on reload) replaces
        it; a different function under an existing name raises ``ValueError``.
        """
        if not name or not name.replace("_", "").isalnum() or name != name.lower():
            raise ValueError(f"invalid job name {name!r} (use lower_snake_case)")
        if not inspect.signature(fn).parameters:
            raise TypeError(f"job {name}: function must accept ctx as first parameter")
        existing = self._jobs.get(name)
        if existing is not None and (
            existing.fn.__module__ != fn.__module__ or existing.fn.__qualname__ != fn.__qualname__
        ):
            raise ValueError(
                f"job {name!r} already registered by {existing.fn.__module__}."
                f"{existing.fn.__qualname__}"
            )
        spec = JobSpec(name=name, description=description, fn=fn)
        self._jobs[name] = spec
        return spec

    def get(self, name: str) -> JobSpec:
        """Return the spec or raise :class:`UnknownJobError`."""
        try:
            return self._jobs[name]
        except KeyError:
            raise UnknownJobError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._jobs

    def names(self) -> list[str]:
        return sorted(self._jobs)

    def all(self) -> list[JobSpec]:
        return [self._jobs[n] for n in self.names()]

    def unregister(self, name: str) -> None:
        self._jobs.pop(name, None)


REGISTRY: Final = JobRegistry()


def job(name: str, description: str) -> Callable[[F], F]:
    """Decorator registering ``fn(ctx, **params)`` in :data:`REGISTRY` as job ``name``."""

    def decorator(fn: F) -> F:
        REGISTRY.register(name, description, fn)
        return fn

    return decorator


# -------------------------------------------------------------------------- discovery


@dataclass(slots=True)
class DiscoveryReport:
    """Modules imported / failed by :func:`discover_jobs`."""

    imported: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def discover_jobs(
    packages: tuple[str, ...] = JOB_PACKAGES, *, strict: bool = False
) -> DiscoveryReport:
    """Import every public module (name not starting with ``_``) of ``packages``.

    Importing registers the module's ``@job`` functions. Missing packages are skipped. A
    module that fails to import is logged and reported in ``failed`` (or re-raised when
    ``strict``), so one broken loader does not take down the scheduler.
    """
    report = DiscoveryReport()
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ModuleNotFoundError as exc:
            if exc.name == pkg_name:
                continue
            raise
        path = getattr(pkg, "__path__", None)
        if path is None:
            continue
        for info in pkgutil.walk_packages(path, prefix=f"{pkg_name}.", onerror=lambda _n: None):
            short = info.name.rsplit(".", 1)[-1]
            if short.startswith("_"):
                continue
            try:
                importlib.import_module(info.name)
            except Exception as exc:
                if strict:
                    raise
                log.exception("failed to import job module %s", info.name)
                report.failed[info.name] = f"{type(exc).__name__}: {exc}"
            else:
                report.imported.append(info.name)
    return report


# ------------------------------------------------------------------------ parameters


def parse_param_pairs(pairs: list[str] | tuple[str, ...]) -> dict[str, str]:
    """``["k=v", ...]`` -> ``{"k": "v"}`` (CLI ``--param``). Later keys win."""
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise JobParamError(f"bad parameter {pair!r}, expected key=value")
        out[key] = value
    return out


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _convert(value: str, annotation: Any, name: str) -> Any:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if type(None) in args and value.strip().lower() in {"", "none", "null"}:
            return None
        if len(non_none) == 1:
            return _convert(value, non_none[0], name)
        return value
    if origin in (list, tuple, set, frozenset):
        item_type = args[0] if args else str
        text = value.strip()
        items: list[Any]
        if text.startswith("["):
            items = [str(v) for v in json.loads(text)]
        else:
            items = [v.strip() for v in text.split(",") if v.strip()]
        converted = [_convert(v, item_type, name) for v in items]
        return converted if origin is list else origin(converted)
    try:
        if annotation is bool:
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(value)
        if annotation is int:
            return int(value)
        if annotation is float:
            return float(value)
        if annotation is Decimal:
            return Decimal(value)
        if annotation is datetime:
            return datetime.fromisoformat(value)
        if annotation is date:
            return date.fromisoformat(value)
    except (ValueError, InvalidOperation):
        raise JobParamError(f"parameter {name}: cannot convert {value!r} to {annotation}") from None
    return value


def coerce_params(fn: Callable[..., Any], params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``params`` against ``fn``'s signature and convert string values.

    String values are converted per annotation (``int``, ``float``, ``bool``, ``Decimal``,
    ``date``, ``datetime``, ``X | None``, ``list[X]`` from ``"a,b"`` or JSON). Non-string
    values pass through unchanged.

    :raises JobParamError: unknown parameter (when ``fn`` has no ``**kwargs``) or bad value.
    """
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    params_list = list(sig.parameters.values())[1:]
    by_name = {
        p.name: p for p in params_list if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    var_kw = any(p.kind is p.VAR_KEYWORD for p in params_list)
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key not in by_name and not var_kw:
            raise JobParamError(
                f"unknown parameter {key!r} (accepted: {', '.join(sorted(by_name)) or 'none'})"
            )
        annotation = hints.get(key, by_name[key].annotation if key in by_name else Any)
        if isinstance(value, str) and annotation not in (str, Any, inspect.Parameter.empty):
            try:
                value = _convert(value, annotation, key)
            except JobParamError:
                raise
            except ValueError:
                raise JobParamError(f"parameter {key}: cannot convert {value!r}") from None
        out[key] = value
    missing = [
        p.name
        for p in by_name.values()
        if p.default is inspect.Parameter.empty and p.name not in out
    ]
    if missing:
        raise JobParamError(f"missing required parameter(s): {', '.join(missing)}")
    return out


# --------------------------------------------------------------------------- context


@dataclass(slots=True)
class JobContext:
    """Everything a job needs. Passed as the first argument to every job function.

    :ivar settings: process settings (secrets as ``SecretStr``).
    :ivar db: connection factory - use :meth:`connection`.
    :ivar run: the ``ops.job_run`` handle (``run.id``, ``run.add_counts``, ``run.mark_partial``).
    :ivar job: job name.
    :ivar params: coerced parameters (as passed to the function).
    :ivar logger: logger named ``mdhub.job.<name>``.
    :ivar calendar: business-day predicate ``(day, calendar) -> bool``.
    """

    settings: Settings
    db: Database
    run: JobRun
    job: str
    params: dict[str, Any]
    logger: logging.Logger
    calendar: CalendarPredicate = weekday_calendar
    http_factory: Callable[..., HttpClient] = HttpClient

    def connection(self) -> AbstractContextManager[DictConnection]:
        """A connection in a transaction (commit on success, rollback on error)."""
        return self.db.connection()

    def http(self, **kwargs: Any) -> HttpClient:
        """New :class:`HttpClient` configured from settings (use as a context manager).

        ``kwargs`` are passed to :class:`HttpClient` (``base_url``, ``headers``, ``auth``...).
        """
        kwargs.setdefault("settings", self.settings)
        return self.http_factory(**kwargs)

    def today(self) -> date:
        """Today's date in Moscow."""
        return msk_today()

    def is_business_day(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        return self.calendar(day, calendar)

    def business_dates(
        self,
        days_back: list[int] | tuple[int, ...],
        *,
        today: date | None = None,
        calendar: str = DEFAULT_CALENDAR,
    ) -> list[date]:
        """Resolve schedule ``days_back`` offsets (0 = today) to business dates."""
        return business_days_back(
            today or self.today(), days_back, lambda d: self.calendar(d, calendar)
        )


# ---------------------------------------------------------------------------- runner


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Result of :func:`run_job`."""

    job: str
    run_id: int | None
    status: str
    result: Any = None


@contextmanager
def _job_lock(db: Database, name: str) -> Iterator[bool]:
    conn = db.connect(autocommit=True)
    try:
        with conn.cursor(row_factory=tuple_row) as cur:
            row = cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (f"mdhub.job:{name}",)
            ).fetchone()
        locked = bool(row and row[0])
        yield locked
    finally:
        conn.close()  # closing the session releases the advisory lock


def run_job(
    name: str,
    params: Mapping[str, Any] | None = None,
    *,
    settings: Settings,
    db: Database,
    registry: JobRegistry = REGISTRY,
    calendar: CalendarPredicate = weekday_calendar,
    http_factory: Callable[..., HttpClient] = HttpClient,
    exclusive: bool = True,
) -> JobOutcome:
    """Run job ``name`` once and record it in ``ops.job_run``.

    :param params: raw parameters (strings are coerced per annotations).
    :param exclusive: hold advisory lock ``mdhub.job:<name>``; if another run holds it the
        job is recorded as ``skipped`` and not executed.
    :returns: outcome with the run id and final status.
    :raises UnknownJobError: job not registered.
    :raises JobParamError: invalid parameters (nothing recorded).
    :raises Exception: whatever the job raised (recorded as ``failed`` first).
    """
    spec = registry.get(name)
    coerced = coerce_params(spec.fn, params or {})
    logger = logging.getLogger(f"mdhub.job.{name}")

    lock_cm: AbstractContextManager[bool] = _job_lock(db, name) if exclusive else _no_lock()
    with lock_cm as locked:
        result: Any = None
        with job_run(db, name, coerced, settings=settings) as run:
            if not locked:
                raise JobAlreadyRunning(f"job {name} is already running")
            ctx = JobContext(
                settings=settings,
                db=db,
                run=run,
                job=name,
                params=coerced,
                logger=logger,
                calendar=calendar,
                http_factory=http_factory,
            )
            result = spec.fn(ctx, **coerced)
        status = _final_status(db, run.id)
        return JobOutcome(job=name, run_id=run.id, status=status, result=result)


@contextmanager
def _no_lock() -> Iterator[bool]:
    yield True


def _final_status(db: Database, run_id: int) -> str:
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute("SELECT status FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    return str(row[0]) if row else "unknown"
