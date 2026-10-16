"""Dataset registry: what each stored time series is and how it is checked and backfilled.

Every loader module that writes a dated table registers one :class:`DatasetSpec` per logical
series at import time::

    from mdhub.datasets import DatasetSpec, register_dataset

    register_dataset(DatasetSpec(
        name="bonds_eod",
        table="md.bond_eod",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="bonds_eod",
        history_start=date(2024, 1, 2),
        min_rows_per_day=1000,
        description="MOEX bonds EOD history, all boards",
    ))

The ``quality`` job uses the registry to detect stale series and missing dates
(-> ``ops.data_gap``) and queues ``ops.backfill_task`` rows; ``backfill_worker`` executes them
by calling ``run_job(spec.backfill_job, {"date_from": d1, "date_to": d2})``.

Contract for ``backfill_job``: the job function must accept keyword parameters
``date_from: date | None = None`` and ``date_to: date | None = None`` (inclusive range of
business dates to (re)load) in addition to its scheduled parameters, and must be idempotent.
A day for which the source confirms that it has no data is reported with
:func:`mdhub.core.gaps.record_no_data` (not ``mark_partial``); a loaded day may be closed with
:func:`mdhub.core.gaps.close_gap`. Datasets sharing one ``backfill_job`` share its tasks: one
task per (job, range) refills all of them.

**Rolling window.** A dataset with ``rolling_window_setting`` keeps only the last N calendar
days (N = that :class:`~mdhub.config.Settings` field, e.g. ``algopack_retention_days`` =
``MDHUB_ALGOPACK_RETENTION_DAYS``): the window is :func:`rolling_window_start` (today - N + 1,
MSK) .. today. Older rows are deleted by ``maintenance mode=retention``; the gap scan does
not look before the window, open gaps before it are closed as ``not_expected``, pending
backfill tasks wholly before it are cancelled and the backfill job clamps its range to it.
Calendar days (not business days) so that the window does not depend on calendar data and
matches the raw-capture retention arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Literal

from mdhub.config import Settings

Frequency = Literal["business_daily", "daily", "monthly", "intraday", "event"]
"""``business_daily``: one or more rows per business day of ``calendar``;
``daily``: every calendar day (e.g. CBR FX by effective date incl. weekends);
``monthly``: one per month (CPI); ``intraday``: many per business day;
``event``: irregular (key-rate changes, meetings) - freshness only, no gap scan."""


@dataclass(frozen=True)
class DatasetSpec:
    """Description of one stored series for freshness/gap checks and backfill."""

    name: str
    table: str
    date_column: str
    calendar: str | None
    frequency: Frequency
    max_lag_days: int
    description: str
    backfill_job: str | None = None
    history_start: date | None = None
    min_rows_per_day: int = 1
    series_column: str | None = None
    series: tuple[str, ...] = ()
    where: str | None = None
    """Optional constant SQL predicate restricting the table (e.g. ``"source = 'cbr'"``);
    must be written by the module author, never built from external input."""
    max_task_days: int | None = None
    """Longest backfill task range in calendar days (default: 31, a year for monthly data).
    Heavy jobs use short ranges so that one task finishes well within the backfill worker's
    time budget and does not hold the job's lock across a scheduled run."""
    rolling_window_setting: str | None = None
    """Name of an ``int`` :class:`~mdhub.config.Settings` field holding the dataset's rolling
    window in calendar days (module docstring); ``None`` = full history is kept."""

    def __post_init__(self) -> None:
        if "." not in self.table:
            raise ValueError(f"{self.name}: table must be schema-qualified")
        if self.max_lag_days < 0 or self.min_rows_per_day < 0:
            raise ValueError(f"{self.name}: negative thresholds")
        if self.series and not self.series_column:
            raise ValueError(f"{self.name}: series requires series_column")
        if self.max_task_days is not None and self.max_task_days < 1:
            raise ValueError(f"{self.name}: max_task_days must be >= 1")
        if self.rolling_window_setting is not None:
            field = Settings.model_fields.get(self.rolling_window_setting)
            if field is None or field.annotation is not int:
                raise ValueError(
                    f"{self.name}: rolling_window_setting {self.rolling_window_setting!r} "
                    "is not an int Settings field"
                )

    def rolling_window_days(self, settings: Settings) -> int | None:
        """Rolling window in calendar days (``None``: full history)."""
        if self.rolling_window_setting is None:
            return None
        days = int(getattr(settings, self.rolling_window_setting))
        if days < 1:
            raise ValueError(f"{self.name}: rolling window must be >= 1 day, got {days}")
        return days

    def window_start(self, today: date, settings: Settings) -> date | None:
        """First day of the rolling window on ``today`` (``None``: no window)."""
        days = self.rolling_window_days(settings)
        return None if days is None else rolling_window_start(today, days)


def rolling_window_start(today: date, days: int) -> date:
    """First day of a rolling window of ``days`` calendar days ending on ``today`` (inclusive):
    ``today - (days - 1)``; e.g. 90 days on 2026-10-16 -> 2026-07-19."""
    if days < 1:
        raise ValueError("rolling window must be >= 1 day")
    return today - timedelta(days=days - 1)


_REGISTRY: Final[dict[str, DatasetSpec]] = {}


def register_dataset(spec: DatasetSpec) -> DatasetSpec:
    """Register ``spec``; re-registering an identical spec (module re-import) is a no-op."""
    existing = _REGISTRY.get(spec.name)
    if existing is not None and existing != spec:
        raise ValueError(f"dataset {spec.name!r} already registered with a different spec")
    _REGISTRY[spec.name] = spec
    return spec


def get_dataset(name: str) -> DatasetSpec:
    return _REGISTRY[name]


def all_datasets() -> list[DatasetSpec]:
    """All registered datasets, sorted by name (call ``discover_jobs()`` first)."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def datasets_for_job(job: str) -> list[DatasetSpec]:
    """Registered datasets whose ``backfill_job`` is ``job`` (sorted by name)."""
    return [s for s in all_datasets() if s.backfill_job == job]


def unregister_dataset(name: str) -> None:
    """Test helper."""
    _REGISTRY.pop(name, None)
