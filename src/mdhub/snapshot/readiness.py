"""Input readiness of the ``eod_final`` market cut (review §6.3 / P1-4) and the business dates
whose published ``eod_final`` a backfill touched (P1-3).

**Readiness.** The ``eod_final`` cut of business date D (08:00 on D+1 by default) should be
built from the *final* loads of D: ``bonds_eod`` (07:15, post-session revisions) and ``zcyc``
(07:10, end-of-day KBD). An input is ready when a run of its job

* ended ``succeeded`` or ``partial``,
* **finished after D ended** (``>= D+1 00:00 MSK``: the 19:20 preliminary run of D does not
  count), and
* covered D: an explicit ``day`` / ``date_from``..``date_to`` range containing D, or - for the
  scheduled runs - a final mode whose ``days_back`` (resolved on the run's start date with the
  MOEX_STOCK calendar) contains D.

The ``snapshot_market`` job waits up to ``MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES`` for the
missing inputs, then builds anyway: the datasets they feed get stale reasons (no forward fill,
nothing invented) and the run ends ``partial``. A later backfill of the inputs corrects the cut
(:func:`eod_final_cuts`, job ``snapshot_rebuild``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.core.timeutil import MSK, business_days_back
from mdhub.db.pool import DictConnection

__all__ = [
    "EOD_FINAL_DATASET",
    "EOD_FINAL_INPUTS",
    "SNAPSHOT_INPUT_JOBS",
    "InputRule",
    "eod_final_cuts",
    "missing_eod_final_inputs",
    "run_covers",
]


@dataclass(frozen=True, slots=True)
class InputRule:
    """How to recognise a run of ``job`` that produced the final input of a day."""

    job: str
    dataset: str
    """Snapshot dataset that gets the stale reason while the input is missing."""
    default_mode: str
    final_modes: frozenset[str]
    """Modes of scheduled (``days_back``) runs that load the final data."""
    rejected_modes: frozenset[str]
    """Modes that never count, even with explicit dates."""
    default_days_back: Mapping[str, tuple[int, ...]]


EOD_FINAL_INPUTS: Final = (
    InputRule(
        job="bonds_eod",
        dataset="bond_quotes",
        default_mode="final",
        final_modes=frozenset({"final"}),
        rejected_modes=frozenset({"prelim"}),
        default_days_back={"final": (1,)},
    ),
    InputRule(
        job="zcyc",
        dataset="zcyc_params",
        default_mode="intraday",
        final_modes=frozenset({"final", "eod"}),
        rejected_modes=frozenset(),
        default_days_back={"final": (1,), "eod": (0,)},
    ),
)

EOD_FINAL_DATASET: Final = "snapshot_eod_final"
"""Dataset of the published ``eod_final`` cuts (registered by :mod:`mdhub.loaders.snapshots`;
backfill job ``snapshot_rebuild``)."""

SNAPSHOT_INPUT_JOBS: Final = frozenset(
    {
        "bonds_eod",
        "zcyc",
        "indices_eod",
        "fx_moex_eod",
        "fx_indicative",
        "cbr_daily",
        "ecb_fx",
        "rosstat_cpi",
        "cbr_inflation",
        "estr_fixing",
        "sofr_fixing",
        "fx_cross",
        "proxy_curves",
    }
)
"""Backfill jobs whose rows of a date D are read by the ``eod_final`` cut of D: a backfill run
of one of them that changed rows queues a rebuild of the touched cuts (``snapshot_rebuild``)."""


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def run_covers(
    rule: InputRule,
    params: Mapping[str, Any],
    started_on: date,
    day: date,
    is_business_day: Callable[[date], bool],
) -> bool:
    """Whether a run with ``params`` (as recorded in ``ops.job_run``) started on
    ``started_on`` (MSK) loaded ``day`` in a final mode."""
    mode = str(params.get("mode") or rule.default_mode)
    if mode in rule.rejected_modes:
        return False
    one = _date(params.get("day"))
    if one is not None:
        return one == day
    lo, hi = _date(params.get("date_from")), _date(params.get("date_to"))
    if lo is not None or hi is not None:
        start = lo or hi
        end = hi or started_on
        assert start is not None
        return start <= day <= end
    if mode not in rule.final_modes:
        return False
    raw = params.get("days_back")
    offsets: Sequence[int] = (
        [int(x) for x in raw] if isinstance(raw, list) else rule.default_days_back.get(mode, ())
    )
    return day in business_days_back(started_on, offsets, is_business_day)


def missing_eod_final_inputs(
    conn: DictConnection,
    day: date,
    is_business_day: Callable[[date], bool],
    *,
    until: datetime | None = None,
) -> list[InputRule]:
    """Inputs of the ``eod_final`` cut of ``day`` without a qualifying run (module doc); runs
    that finished after ``until`` are ignored (replay)."""
    since = datetime.combine(day + timedelta(days=1), time(0), tzinfo=MSK)
    missing: list[InputRule] = []
    for rule in EOD_FINAL_INPUTS:
        with conn.cursor(row_factory=tuple_row) as cur:
            rows = cur.execute(
                """
                SELECT params, started_at FROM ops.job_run
                WHERE job = %(job)s AND status IN ('succeeded', 'partial')
                  AND finished_at >= %(since)s
                  AND (%(until)s::timestamptz IS NULL OR finished_at <= %(until)s)
                ORDER BY finished_at DESC LIMIT 200
                """,
                {"job": rule.job, "since": since, "until": until},
            ).fetchall()
        ok = any(
            run_covers(
                rule, dict(params or {}), started.astimezone(MSK).date(), day, is_business_day
            )
            for params, started in rows
        )
        if not ok:
            missing.append(rule)
    return missing


def eod_final_cuts(
    conn: DictConnection, date_from: date, date_to: date
) -> list[tuple[date, datetime]]:
    """``(business_date, cut_ts)`` of the current (not superseded) ``eod_final`` snapshots of
    business dates in ``[date_from, date_to]``."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT DISTINCT p.business_date, p.cut_ts FROM ops.published_snapshot p
            WHERE p.kind = 'market' AND p.cut_type = 'eod_final'
              AND p.business_date BETWEEN %s AND %s
              AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
            ORDER BY 1, 2
            """,
            (date_from, date_to),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]
