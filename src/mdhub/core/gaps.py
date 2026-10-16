"""Shared ``ops.data_gap`` bookkeeping for loaders (the "no data" contract).

Every job that is a dataset's ``backfill_job`` must report a business day for which the
source *confirms* that it has no data (an empty, well-formed answer - e.g. a holiday that is
missing from ``ref.calendar_day``) with :func:`record_no_data`, and must close a day it loaded
with :func:`close_gap`. A no-data day is **not** a partial failure: do not call
``ctx.run.mark_partial`` for it (the backfill worker would otherwise keep the gap open and the
quality job would re-queue it for ever).

Grace period: data for the most recent days can be published late (ISS history for D-1 is
sometimes empty at 07:15). A no-data answer for a day that is at most
:data:`NO_DATA_GRACE_DAYS` calendar days old therefore leaves the gap ``open`` (with a note) -
the quality job re-queues it and a later run decides. An older day becomes ``unfillable``
(terminal: never re-queued). If data shows up later anyway (another path, a manual run), the
quality scan (:func:`mdhub.quality.checks.sync_gaps`) and :func:`close_gap` turn the gap into
``backfilled``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

__all__ = [
    "NO_DATA_GRACE_DAYS",
    "NO_DATA_NOTE_PREFIX",
    "close_gap",
    "no_data_status",
    "record_no_data",
]

NO_DATA_GRACE_DAYS: Final = 1
"""A source-confirmed empty day becomes ``unfillable`` once it is more than this many calendar
days before today (MSK); until then the gap stays ``open``."""

NO_DATA_NOTE_PREFIX: Final = "no data from source"


def no_data_status(day: date, today: date) -> str:
    """``unfillable`` for a source-confirmed empty ``day`` older than the grace period, else
    ``open``."""
    return "unfillable" if (today - day).days > NO_DATA_GRACE_DAYS else "open"


def record_no_data(
    conn: psycopg.Connection[Any],
    dataset: str,
    day: date,
    note: str,
    *,
    today: date,
    key: Mapping[str, Any] | None = None,
) -> str:
    """Record "the source has no data for this expected day"; returns the gap status written.

    An existing ``open`` gap is updated (it may become ``unfillable``); ``unfillable`` and
    ``backfilled`` gaps are left alone (data that was loaded earlier is never "un-loaded").
    """
    status = no_data_status(day, today)
    text = f"{NO_DATA_NOTE_PREFIX}: {note}"
    if status == "open":
        text += f" (within the {NO_DATA_GRACE_DAYS}-day grace period; will be re-checked)"
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
