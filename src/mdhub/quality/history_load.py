"""Automatic history load of datasets that are new to the hub (market-data-hub#138).

Owner decision of 2026-11-11: when a dataset is added, its history is backfilled **and** the
``eod_final`` cuts of the last 12 months are reissued once, so past cuts contain the new series -
without an operator. The ``snapshot_heal`` job drives it (no second scheduler); this module holds
the dataset side. The cut side (the one-off reissue pass) is in :mod:`mdhub.loaders.snapshot_heal`.

**New dataset.** A registered dataset that has a ``backfill_job`` and a ``history_start`` (daily,
business-daily or monthly, not event / intraday / rolling-window) whose earliest stored day is
later than its ``history_start`` with at least one expected day in between (an empty table: up to
the expected latest day), and that has no row in ``ops.dataset_history_load``. The scheduled gap
scan never sees that head of the history (it scans from the earliest stored day) and an empty table
is only reported ``no rows``, which is why a new dataset used to wait for a manual run.

**Load.** The head ``[history_start, earliest stored day - 1]`` is queued in ``ops.backfill_task``
(source :data:`HISTORY_SOURCE`, chunks of ``max_task_days``) - one set of tasks per backfill job
over the **union** of the heads of its datasets (they share the tasks; a stretch that is stored
between two heads is not downloaded again). A dataset's load is judged only when every task of its
job has finished, and a task is matched to a dataset by overlap with its head. The ordinary
``backfill_worker`` runs them, after the tasks of every other source and with the same lock
yielding, so live gap repairs are not delayed.
As far as each source allows: a source that has nothing for a day leaves it empty. A task that
failed is queued again once, :data:`RETRY_AFTER` later (:data:`MAX_ROUNDS` rounds in all); what is
still missing then stays missing and is noted. The worker's autocorrection of ``eod_final`` cuts
(which would rebuild the cuts once per task) is **not** applied to these tasks
(:mod:`mdhub.quality.backfill`): the reissue is one pass, below.

**One-off reissue.** When no dataset is still loading, the datasets whose load added rows (their
earliest stored day moved back) and whose job feeds the cut
(:data:`~mdhub.snapshot.readiness.SNAPSHOT_INPUT_JOBS`) are reissued together in **one** pass of the
current ``eod_final`` cuts of the last :data:`REISSUE_DAYS` days (``snapshot_heal``: night window,
bounded per run, ``snapshot_cut_new_series`` gaps). The row of a dataset stays after it is
``done``: nothing is detected, queued or reissued for it again.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.bizday import CalendarPredicate
from mdhub.datasets import DatasetSpec, all_datasets, get_dataset
from mdhub.db.pool import DictConnection
from mdhub.jobs import REGISTRY, JobRegistry
from mdhub.quality.checks import (
    expected_days,
    expected_latest,
    first_stored_day,
    job_datasets,
    max_task_days,
    queue_range,
    split_range,
)
from mdhub.snapshot.readiness import SNAPSHOT_INPUT_JOBS

log = logging.getLogger(__name__)

__all__ = [
    "HISTORY_SOURCE",
    "MAX_ROUNDS",
    "REISSUE_DAYS",
    "RETRY_AFTER",
    "Head",
    "HistoryLoad",
    "JobPlan",
    "active",
    "detect_and_queue",
    "find_heads",
    "finish_pass",
    "head_range",
    "load_rows",
    "merge_ranges",
    "pass_datasets",
    "plan_heads",
    "progress",
    "start_pass",
]

HISTORY_SOURCE: Final = "history_load"
"""``ops.backfill_task.source`` of the tasks of a history load."""
MAX_ROUNDS: Final = 2
"""Load rounds: the first and one retry of the failed ranges."""
RETRY_AFTER: Final = timedelta(hours=6)
"""A range that failed is queued again no sooner than this after its last attempt."""
REISSUE_DAYS: Final = 365
"""The reissue covers the current ``eod_final`` cuts of this many days back (12 months)."""

_ELIGIBLE_FREQUENCIES: Final = ("business_daily", "daily", "monthly")


@dataclass(frozen=True, slots=True)
class HistoryLoad:
    """A row of ``ops.dataset_history_load``."""

    dataset: str
    backfill_job: str
    status: str
    history_from: date
    head_to: date
    first_before: date | None
    first_after: date | None
    load_round: int
    pass_started_at: datetime | None
    note: str | None


_COLUMNS: Final = (
    "dataset, backfill_job, status, history_from, head_to, first_before, first_after, "
    "load_round, pass_started_at, note"
)


def load_rows(conn: DictConnection) -> list[HistoryLoad]:
    """Every row, oldest detection first."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            f"SELECT {_COLUMNS} FROM ops.dataset_history_load ORDER BY detected_at, dataset"
        ).fetchall()
    return [HistoryLoad(*r) for r in rows]


# ------------------------------------------------------------------------------- detection


def eligible(spec: DatasetSpec, registry: JobRegistry = REGISTRY) -> bool:
    """Whether ``spec`` can have its history loaded by the hub (module doc)."""
    return (
        spec.backfill_job is not None
        and spec.backfill_job in registry
        and spec.history_start is not None
        and spec.frequency in _ELIGIBLE_FREQUENCIES
        and spec.min_rows_per_day > 0
        and spec.rolling_window_setting is None
    )


def head_range(
    conn: DictConnection, spec: DatasetSpec, today: date, calendar: CalendarPredicate
) -> tuple[date, date, date | None] | None:
    """``(first day, last day, earliest stored day)`` of the part of the history that is missing
    in front of the stored data (an empty table: up to the expected latest day), ``None`` when no
    expected day of the dataset's calendar lies in it."""
    assert spec.history_start is not None
    first = first_stored_day(conn, spec)
    last = expected_latest(spec, today, calendar)
    if first is not None:
        last = min(last, first - timedelta(days=1))
    days = expected_days(spec, spec.history_start, last, calendar)
    return (spec.history_start, days[-1], first) if days else None


@dataclass(frozen=True, slots=True)
class Head:
    """The missing head of the history of one new dataset."""

    spec: DatasetSpec
    first_day: date
    last_day: date
    first_stored: date | None
    """Earliest stored day (``None``: no rows)."""


@dataclass(frozen=True, slots=True)
class JobPlan:
    """What the heads of the datasets of one backfill job need: the union of their ranges (not
    their hull: years that are stored between two tails are not downloaded again), split into
    tasks of the job's shortest ``max_task_days``. The tasks are queued under ``rep``."""

    job: str
    rep: DatasetSpec
    heads: tuple[Head, ...]
    ranges: tuple[tuple[date, date], ...]
    tasks: tuple[tuple[date, date], ...]

    def tasks_of(self, head: Head) -> int:
        """Number of tasks that cover (part of) the head of one dataset."""
        return sum(a <= head.last_day and b >= head.first_day for a, b in self.tasks)


def merge_ranges(ranges: Collection[tuple[date, date]]) -> list[tuple[date, date]]:
    """Union of inclusive day ranges (overlapping or adjacent ones are merged)."""
    out: list[tuple[date, date]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def find_heads(
    conn: DictConnection,
    *,
    today: date,
    calendar: CalendarPredicate,
    registry: JobRegistry = REGISTRY,
    only: Collection[str] | None = None,
) -> list[Head]:
    """The datasets that are new (no row yet) and have a missing head of history."""
    known = {r.dataset for r in load_rows(conn)}
    heads: list[Head] = []
    for spec in all_datasets():
        if spec.name in known or (only is not None and spec.name not in only):
            continue
        if not eligible(spec, registry):
            continue
        try:
            with conn.transaction():  # a savepoint: an SQL error does not poison the connection
                head = head_range(conn, spec, today, calendar)
        except Exception:  # a dataset whose table cannot be read must not stop the others
            log.exception("history load: %s could not be checked", spec.name)
            continue
        if head is not None:
            heads.append(Head(spec, *head))
    return heads


def plan_heads(heads: Collection[Head]) -> list[JobPlan]:
    """One :class:`JobPlan` per backfill job of ``heads`` (sorted by job name)."""
    by_job: dict[str, list[Head]] = {}
    for h in heads:
        assert h.spec.backfill_job is not None
        by_job.setdefault(h.spec.backfill_job, []).append(h)
    plans: list[JobPlan] = []
    for job_name, items in sorted(by_job.items()):
        rep = min((h.spec for h in items), key=lambda s: (max_task_days(s), s.name))
        ranges = merge_ranges([(h.first_day, h.last_day) for h in items])
        tasks = tuple(t for lo, hi in ranges for t in split_range(lo, hi, max_task_days(rep)))
        plans.append(JobPlan(job_name, rep, tuple(items), tuple(ranges), tasks))
    return plans


def detect_and_queue(
    conn: DictConnection,
    *,
    today: date,
    calendar: CalendarPredicate,
    hub_version: str,
    registry: JobRegistry = REGISTRY,
    only: Collection[str] | None = None,
) -> list[HistoryLoad]:
    """Register the datasets that are new (no row yet) and have a missing head of history, and
    queue their backfill tasks (:class:`JobPlan`: one set per backfill job, shared by its
    datasets, over the union of their heads). Returns the rows created. ``only``: look among
    these datasets only (manual runs, tests)."""
    created: list[HistoryLoad] = []
    heads = find_heads(conn, today=today, calendar=calendar, registry=registry, only=only)
    for plan in plan_heads(heads):
        for lo, hi in plan.ranges:
            queue_range(conn, plan.rep, lo, hi, source=HISTORY_SOURCE)
        for h in plan.heads:
            note = (
                f"loading {h.first_day}..{h.last_day}: {plan.tasks_of(h)} backfill task(s) "
                f"queued (job {plan.job}, shared by {len(plan.heads)} dataset(s))"
            )
            conn.execute(
                """
                INSERT INTO ops.dataset_history_load
                    (dataset, backfill_job, status, history_from, head_to, first_before,
                     hub_version, note)
                VALUES (%s, %s, 'loading', %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (
                    h.spec.name,
                    plan.job,
                    h.first_day,
                    h.last_day,
                    h.first_stored,
                    hub_version,
                    note,
                ),
            )
            created.append(
                HistoryLoad(
                    h.spec.name,
                    plan.job,
                    "loading",
                    h.first_day,
                    h.last_day,
                    h.first_stored,
                    None,
                    1,
                    None,
                    note,
                )
            )
    return created


# --------------------------------------------------------------------------------- progress


def _latest_tasks(
    conn: DictConnection, row: HistoryLoad
) -> list[tuple[str, str, dict[str, Any], Any]]:
    """The newest task of every range that overlaps this load's head: ``(dataset, status, params,
    done_at)``. Tasks are queued per backfill job over the union of the heads of its datasets (and
    under one dataset's name), so a task is matched by overlap, not by containment."""
    names = _job_dataset_names(row)
    with conn.cursor(row_factory=tuple_row) as cur:
        return [
            (r[0], r[1], dict(r[2]), r[3])
            for r in cur.execute(
                """
                SELECT DISTINCT ON (dataset, params) dataset, status, params, done_at
                FROM ops.backfill_task
                WHERE source = %(src)s AND dataset = ANY(%(names)s)
                  AND params ->> 'date_to' >= %(a)s AND params ->> 'date_from' <= %(b)s
                ORDER BY dataset, params, id DESC
                """,
                {
                    "src": HISTORY_SOURCE,
                    "names": names,
                    "a": row.history_from.isoformat(),
                    "b": row.head_to.isoformat(),
                },
            ).fetchall()
        ]


def _job_open(conn: DictConnection, row: HistoryLoad) -> int:
    """History tasks of the row's backfill job (any dataset, any range) that are still open: the
    rows of the job are judged only when the whole job has finished loading."""
    with conn.cursor(row_factory=tuple_row) as cur:
        found = cur.execute(
            "SELECT count(*) FROM ops.backfill_task WHERE source = %s AND dataset = ANY(%s) "
            "AND status IN ('pending', 'running')",
            (HISTORY_SOURCE, _job_dataset_names(row)),
        ).fetchone()
    return int(found[0]) if found else 0


def _job_dataset_names(row: HistoryLoad) -> list[str]:
    try:
        return job_datasets(get_dataset(row.dataset))
    except KeyError:
        return [row.dataset]


def _set(conn: DictConnection, dataset: str, note: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    conn.execute(
        f"UPDATE ops.dataset_history_load SET {sets + ', ' if sets else ''}note = %(note)s, "
        "updated_at = now() WHERE dataset = %(dataset)s",
        {**cols, "note": note[:300], "dataset": dataset},
    )


def progress(conn: DictConnection, *, now: datetime, requeue: bool = True) -> None:
    """Advance every ``loading`` row: note the tasks left; queue the failed ranges again once
    (:data:`MAX_ROUNDS`, :data:`RETRY_AFTER`); when nothing is open and nothing is to be
    retried, the load is finished: ``reissue`` when it added rows of a dataset that feeds the cut,
    else ``done``."""
    for row in load_rows(conn):
        if row.status != "loading":
            continue
        tasks = _latest_tasks(conn, row)
        open_ = [t for t in tasks if t[1] in ("pending", "running")]
        failed = [t for t in tasks if t[1] == "failed"]
        finished = len(tasks) - len(open_) - len(failed)
        job_open = _job_open(conn, row)
        if open_ or job_open:
            more = (
                f"; {job_open - len(open_)} more of job {row.backfill_job}"
                if job_open > len(open_)
                else ""
            )
            _set(
                conn,
                row.dataset,
                f"loading {row.history_from}..{row.head_to}: {len(tasks) - len(open_)} of "
                f"{len(tasks)} backfill task(s) finished (round {row.load_round}"
                f"{f', {len(failed)} failed' if failed else ''}{more})",
            )
            continue
        last_done = max((t[3] for t in failed if t[3] is not None), default=None)
        if failed and row.load_round < MAX_ROUNDS:
            if requeue and (last_done is None or now - last_done >= RETRY_AFTER):
                for dataset, _status, params, _at in failed:
                    conn.execute(
                        "INSERT INTO ops.backfill_task (dataset, params, source) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (dataset, Jsonb(params), HISTORY_SOURCE),
                    )
                _set(
                    conn,
                    row.dataset,
                    f"loading {row.history_from}..{row.head_to}: {len(failed)} failed "
                    f"range(s) queued again (round {row.load_round + 1})",
                    load_round=row.load_round + 1,
                )
            continue
        _finish_load(conn, row, finished=finished, failed=len(failed))


def _finish_load(conn: DictConnection, row: HistoryLoad, *, finished: int, failed: int) -> None:
    try:
        spec = get_dataset(row.dataset)
        first_after = first_stored_day(conn, spec)
    except KeyError:
        _set(conn, row.dataset, "dataset is no longer registered", status="done")
        return
    gained = first_after is not None and (
        row.first_before is None or first_after < row.first_before
    )
    feeds_cut = row.backfill_job in SNAPSHOT_INPUT_JOBS
    tail = f" ({failed} range(s) could not be loaded)" if failed else ""
    if gained and feeds_cut:
        _set(
            conn,
            row.dataset,
            f"history loaded from {first_after}{tail}; waiting for the one-off reissue of the "
            "eod_final cuts",
            status="reissue",
            first_after=first_after,
        )
    else:
        why = (
            "no rows were added"
            if not gained
            else f"job {row.backfill_job} does not feed the eod_final cut"
        )
        _set(
            conn,
            row.dataset,
            f"history load finished{tail}: {why}; no reissue needed",
            status="done",
            first_after=first_after,
        )


# -------------------------------------------------------------------------- the reissue pass


def start_pass(conn: DictConnection, *, now: datetime) -> list[str]:
    """Start the one-off reissue pass when no dataset is still loading and some are waiting for
    it (none has started one): marks them, returns their names (``[]``: nothing to start)."""
    rows = load_rows(conn)
    if any(r.status == "loading" for r in rows):
        return []
    waiting = [r for r in rows if r.status == "reissue"]
    if not waiting or any(r.pass_started_at is not None for r in waiting):
        return []
    for r in waiting:
        _set(
            conn,
            r.dataset,
            "reissue of the eod_final cuts of the last 12 months started (snapshot_cut_new_series)",
            pass_started_at=now,
        )
    return [r.dataset for r in waiting]


def pass_datasets(conn: DictConnection) -> list[str]:
    """The datasets of the pass in progress (``reissue`` with a start time)."""
    return [r.dataset for r in load_rows(conn) if r.status == "reissue" and r.pass_started_at]


def finish_pass(conn: DictConnection, *, note: str) -> list[str]:
    """Mark the datasets of the pass in progress ``done``; returns their names."""
    names = pass_datasets(conn)
    for n in names:
        _set(conn, n, note, status="done")
    return names


def active(rows: Collection[HistoryLoad]) -> bool:
    """Whether any dataset is still being loaded or reissued."""
    return any(r.status in ("loading", "reissue") for r in rows)
