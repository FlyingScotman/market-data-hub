"""Shared ``ops.data_gap`` bookkeeping for loaders (the "no data" contract).

Every job that is a dataset's ``backfill_job`` must report a business day for which the
source *confirms* that it has no data (an empty, well-formed answer - e.g. a holiday that is
missing from ``ref.calendar_day``) with :func:`record_no_data`, and must close a day it loaded
with :func:`close_gap`. A no-data day is **not** a partial failure: do not call
``ctx.run.mark_partial`` for it (the backfill worker would otherwise keep the gap open and the
quality job would re-queue it for ever).

Grace period - counted in **business days of the dataset's calendar**, not calendar days:
data for the most recent days can be published late (ISS history for D-1 is sometimes empty
at 07:15). A no-data answer leaves the gap ``open`` (with a note) until at least
:data:`NO_DATA_GRACE_DAYS` business day(s) of the dataset's calendar lie strictly between the
day and today; the quality job re-queues it and a later answer decides. Only then does it
become ``unfillable`` (terminal: never re-queued). So the Monday final run that finds Friday
empty leaves Friday open (Saturday/Sunday are not business days) and the Tuesday re-check
decides; after the New-Year holidays D-1 = 12-30 seen on 01-11 stays open too. If data shows
up later anyway (another path, a manual run), the quality scan
(:func:`mdhub.quality.checks.sync_gaps`) and :func:`close_gap` turn the gap into
``backfilled``.

The calendar comes from the dataset's :class:`~mdhub.datasets.DatasetSpec` (``calendar``;
``ref.calendar_effective`` with the Monday-Friday fallback); unregistered datasets and
datasets without a calendar use Monday-Friday.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, Final

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.bizday import calendar_chain
from mdhub.core.timeutil import is_weekday
from mdhub.datasets import get_dataset

__all__ = [
    "NO_DATA_GRACE_DAYS",
    "NO_DATA_NOTE_PREFIX",
    "close_gap",
    "db_business_days",
    "no_data_status",
    "record_missed",
    "record_no_data",
]

NO_DATA_GRACE_DAYS: Final = 1
"""A source-confirmed empty day becomes ``unfillable`` once at least this many **business
days** (of the dataset's calendar) lie strictly between it and today (MSK); until then the gap
stays ``open``."""

NO_DATA_NOTE_PREFIX: Final = "no data from source"

_MAX_SCAN_DAYS: Final = 62
"""Business-day counting stops after this many calendar days (the grace is long over)."""


def no_data_status(
    day: date, today: date, is_business_day: Callable[[date], bool] = is_weekday
) -> str:
    """``unfillable`` for a source-confirmed empty ``day`` once :data:`NO_DATA_GRACE_DAYS`
    business days (``is_business_day``, default Monday-Friday) have passed between ``day`` and
    ``today`` (both exclusive), else ``open``."""
    passed = 0
    cur = day + timedelta(days=1)
    while cur < today:
        if (cur - day).days > _MAX_SCAN_DAYS:
            return "unfillable"
        if is_business_day(cur):
            passed += 1
            if passed >= NO_DATA_GRACE_DAYS:
                return "unfillable"
        cur += timedelta(days=1)
    return "open"


def db_business_days(
    conn: psycopg.Connection[Any], calendar: str | None, start: date, end: date
) -> Callable[[date], bool]:
    """Business-day predicate of ``calendar`` for ``[start, end]`` from ``ref.calendar_effective``
    (one query; a day without a row: the base calendar of a derived one -
    :data:`mdhub.core.bizday.CALENDAR_FALLBACK` -, else Monday-Friday, as for days outside the
    range). ``None`` calendar: Monday-Friday."""
    if calendar is None or end < start:
        return is_weekday
    end = min(end, start + timedelta(days=_MAX_SCAN_DAYS))
    with conn.cursor(row_factory=tuple_row) as cur:
        exists = cur.execute("SELECT to_regclass('ref.calendar_effective')").fetchone()
        if not exists or exists[0] is None:
            return is_weekday
        chain = list(calendar_chain(calendar))
        flags: dict[date, bool] = {
            d: bool(b)
            for d, b in cur.execute(
                """SELECT DISTINCT ON (day) day, is_business_day FROM ref.calendar_effective
                   WHERE calendar = ANY(%s) AND day BETWEEN %s AND %s
                   ORDER BY day, array_position(%s, calendar)""",
                (chain, start, end, chain),
            ).fetchall()
        }
    return lambda d: flags.get(d, is_weekday(d))


def _dataset_calendar(dataset: str) -> str | None:
    try:
        return get_dataset(dataset).calendar
    except KeyError:
        return None


def record_no_data(
    conn: psycopg.Connection[Any],
    dataset: str,
    day: date,
    note: str,
    *,
    today: date,
    key: Mapping[str, Any] | None = None,
    calendar: str | None = None,
) -> str:
    """Record "the source has no data for this expected day"; returns the gap status written.

    ``calendar`` (default: the registered dataset's calendar) counts the grace period in
    business days. An existing ``open`` gap is updated (it may become ``unfillable``);
    ``unfillable``, ``backfilled`` and ``not_expected`` gaps are left alone (data that was
    loaded earlier is never "un-loaded").
    """
    cal = calendar if calendar is not None else _dataset_calendar(dataset)
    pred = db_business_days(conn, cal, day + timedelta(days=1), today - timedelta(days=1))
    status = no_data_status(day, today, pred)
    text = f"{NO_DATA_NOTE_PREFIX}: {note}"
    if status == "open":
        text += f" (within the {NO_DATA_GRACE_DAYS}-business-day grace period; will be re-checked)"
    conn.execute(
        """
        INSERT INTO ops.data_gap (dataset, key, gap_date, status, note)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (dataset, key, gap_date) DO UPDATE
            SET status = EXCLUDED.status, note = EXCLUDED.note, detected_at = now()
            WHERE ops.data_gap.status = 'open'
        """,
        (dataset, Jsonb(dict(key or {})), day, status, text[:1000]),
    )
    return status


def close_gap(
    conn: psycopg.Connection[Any],
    dataset: str,
    day: date,
    *,
    key: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> None:
    """Mark an ``open``/``unfillable`` gap of ``dataset`` on ``day`` as backfilled (data
    arrived)."""
    conn.execute(
        """
        UPDATE ops.data_gap SET status = 'backfilled',
               note = coalesce(%s, 'data loaded (was ' || status || ')')
        WHERE dataset = %s AND key = %s AND gap_date = %s
          AND status IN ('open', 'unfillable')
        """,
        (note, dataset, Jsonb(dict(key or {})), day),
    )


def record_missed(
    conn: psycopg.Connection[Any],
    dataset: str,
    day: date,
    note: str,
    *,
    key: Mapping[str, Any] | None = None,
) -> None:
    """Open a gap for a scheduled product that was not produced (e.g. an intraday snapshot cut
    skipped because its lock stayed busy). Not a "no data" outcome: it stays ``open`` (visible
    in ``ops.v_open_gaps`` / ``/v1/gaps``) until :func:`close_gap` - a replay of the missed item
    - or an operator closes it. ``dataset`` need not be registered (no scan, no backfill)."""
    conn.execute(
        """
        INSERT INTO ops.data_gap (dataset, key, gap_date, status, note)
        VALUES (%s, %s, %s, 'open', %s)
        ON CONFLICT (dataset, key, gap_date) DO UPDATE
            SET status = 'open', note = EXCLUDED.note, detected_at = now()
            WHERE ops.data_gap.status IN ('open', 'not_expected')
        """,
        (dataset, Jsonb(dict(key or {})), day, note[:1000]),
    )
