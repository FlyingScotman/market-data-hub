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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

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

    def __post_init__(self) -> None:
        if "." not in self.table:
            raise ValueError(f"{self.name}: table must be schema-qualified")
        if self.max_lag_days < 0 or self.min_rows_per_day < 0:
            raise ValueError(f"{self.name}: negative thresholds")
        if self.series and not self.series_column:
            raise ValueError(f"{self.name}: series requires series_column")


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


def unregister_dataset(name: str) -> None:
    """Test helper."""
    _REGISTRY.pop(name, None)
