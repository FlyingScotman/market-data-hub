"""Curves carried forward from the previous working day (contract 1.7; market-data-hub#143).

Owner decision of 2026-11-10 / 2026-11-11: an approximate, clearly flagged number is better than
an empty one, and gaps are not restored by hand. When **no source** delivered a curve of the
carried set (default ``ruonia_ois`` and ``key_rate_swap``: :data:`DEFAULT_CARRY_DATASETS`) on the
business date of a market cut, the cut takes that curve - every quote set of it, per source - from
the **latest previous day that has it**. The carry may span more than one day; there is no
refusal by age: ``max_age`` only sets ``age_limit_exceeded`` in the marker. The source day is the
latest day whose quote set, chosen by the same per-source rule as for the business date itself,
holds quotes (:func:`source_days`): a day that has only sets the rule does not choose, or empty
ones, is skipped and is never named in a marker or a stale reason.

The substitution is never silent:

* every carried quote set has ``carried_forward`` (:func:`marker`: source day, age in working days,
  reason, limit and whether it is exceeded), the label ``carried_forward`` and its own
  ``snapshot_date`` (the source day, not the business date);
* the manifest lists them in ``carried_curves`` (:func:`summary_item`);
* the dataset ``curve_quote_sets`` is stale with the reason (as before when a required curve
  was missing) and its coverage names the carried curves;
* ``/v1/carried-curves`` and the quality report list the curves currently carried
  (:func:`carried_curves`).

Nothing is carried when the day has the curve (the snapshot is then byte for byte what it was
before contract 1.7, apart from ``schema_version``), when the curve is not in the carried set, or
when there is no earlier day with it (the start of history: the curve stays absent, as before).

A carried cut is **outdated** as soon as real data of a day after its source day, up to its own
business date, is stored (:func:`pending_carried_cuts`): a rebuild then differs, and
``snapshot_heal`` reissues the cut (a correction that ``supersedes`` it).

The working days are the ``MOEX_STOCK`` business days, the calendar of the dataset-level curve gap
scan (``curves_<dataset>``), as known at the cut's calendar version, Monday-Friday where the
calendar has no row.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.db.pool import DictConnection

__all__ = [
    "CARRY_CALENDAR",
    "CARRY_LABEL",
    "DEFAULT_CARRY_DATASETS",
    "DEFAULT_CARRY_MAX_AGE",
    "MAX_CANDIDATE_DAYS",
    "REASON_NO_QUOTE_SET",
    "CarryRule",
    "carried_curves",
    "fresher_set_ids",
    "marker",
    "pending_carried_cuts",
    "source_days",
    "summary_item",
    "working_days_between",
]

DEFAULT_CARRY_DATASETS: Final = ("key_rate_swap", "ruonia_ois")
"""The curves a market cut needs in every cut (``REQUIRED_CURVE_DATASETS``); configurable
(``MDHUB_CURVE_CARRY_DATASETS``)."""
DEFAULT_CARRY_MAX_AGE: Final = 5
"""Working days after which the marker says ``age_limit_exceeded`` (a week); the curve is still
served (``MDHUB_CURVE_CARRY_MAX_AGE_DAYS``)."""
MAX_CANDIDATE_DAYS: Final = 30
"""At most this many candidate source days are looked at for one carried curve (each costs two
queries): a curve whose newest 30 stored days are all unusable (their selected set is empty) is not
carried, and the build says so in the log. In practice the first candidate is the source day."""
REASON_NO_QUOTE_SET: Final = "no_quote_set_on_business_date"
"""``carried_forward.reason``: no source stored a quote set of the curve on the business date."""
CARRY_LABEL: Final = "carried_forward"
CARRY_CALENDAR: Final = "MOEX_STOCK"


@dataclass(frozen=True, slots=True)
class CarryRule:
    """What a build may carry (from the settings): the curves and the age limit of the marker."""

    datasets: tuple[str, ...] = DEFAULT_CARRY_DATASETS
    max_age: int = DEFAULT_CARRY_MAX_AGE


def source_days(
    conn: DictConnection, t: datetime, d: date, dataset: str, limit: int | None = None
) -> list[date]:
    """The ``snapshot_date`` s before ``d`` with a stored, non-empty quote set of ``dataset`` from
    any source known at ``t``, latest first, at most ``limit`` (default
    :data:`MAX_CANDIDATE_DAYS`): the candidates for the source day of a carry (the first one whose
    per-source EOD selection holds a set with quotes is used; see
    :func:`mdhub.snapshot.market._curve_sets`)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT DISTINCT s.snapshot_date FROM md.curve_snapshot s
            WHERE s.dataset = %(ds)s AND s.snapshot_date < %(d)s
              AND s.fetched_at <= %(t)s AND s.ingested_at <= %(t)s
              AND EXISTS (SELECT 1 FROM md.curve_quote q WHERE q.snapshot_id = s.id)
            ORDER BY 1 DESC
            LIMIT %(limit)s
            """,
            {"ds": dataset, "d": d, "t": t, "limit": limit or MAX_CANDIDATE_DAYS},
        ).fetchall()
    return [r[0] for r in rows]


def working_days_between(
    conn: DictConnection, calendar_t: datetime, after: date, upto: date
) -> int:
    """Working days ``day`` with ``after < day <= upto`` (calendar as known at ``calendar_t``):
    the age of a curve of ``after`` on ``upto``. 0 when ``upto`` is not after ``after``."""
    if upto <= after:
        return 0
    with conn.cursor(row_factory=tuple_row) as cur:
        flags = {
            r[0]: bool(r[1])
            for r in cur.execute(
                "SELECT day, is_business_day FROM ref.calendar_effective_known_at(%(t)s) "
                "WHERE calendar = %(c)s AND day > %(a)s AND day <= %(u)s",
                {"t": calendar_t, "c": CARRY_CALENDAR, "a": after, "u": upto},
            ).fetchall()
        }
    n, day = 0, after + timedelta(days=1)
    while day <= upto:
        n += flags.get(day, day.weekday() < 5)
        day += timedelta(days=1)
    return n


def marker(source_day: date, age: int, max_age: int) -> dict[str, Any]:
    """The per-curve marker ``carried_forward`` of contract 1.7."""
    return {
        "source_day": source_day,
        "age_working_days": age,
        "age_limit_working_days": max_age,
        "age_limit_exceeded": age > max_age,
        "reason": REASON_NO_QUOTE_SET,
    }


def summary_item(quote_set: Mapping[str, Any]) -> dict[str, Any]:
    """An entry of the snapshot-level list ``carried_curves`` for a carried quote set."""
    m = quote_set["carried_forward"]
    return {
        "dataset": quote_set["dataset"],
        "source": quote_set["source"],
        "quote_set_id": quote_set["quote_set_id"],
        "source_day": m["source_day"],
        "age_working_days": m["age_working_days"],
        "age_limit_working_days": m["age_limit_working_days"],
        "age_limit_exceeded": m["age_limit_exceeded"],
        "reason": m["reason"],
    }


# ---------------------------------------------------------------- status: what is carried now


def carried_curves(
    conn: DictConnection, *, since: date, cut_types: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """The curves carried in the **current** (not superseded) market snapshots of business dates
    from ``since``, newest first: one row per carried curve with the snapshot it sits in. A cut
    reissued after the real data arrived is superseded, so it is not listed any more."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT p.id, p.cut_type, p.business_date, p.cut_ts, c.value
            FROM ops.published_snapshot p
            CROSS JOIN LATERAL jsonb_array_elements(p.manifest -> 'carried_curves') c
            WHERE p.kind = 'market' AND p.business_date >= %(since)s
              AND jsonb_typeof(p.manifest -> 'carried_curves') = 'array'
              AND (%(ct)s::text[] IS NULL OR p.cut_type = ANY(%(ct)s))
              AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
            ORDER BY p.business_date DESC, p.cut_ts DESC, p.id, c.value ->> 'dataset',
                     c.value ->> 'source'
            """,
            {"since": since, "ct": list(cut_types) if cut_types is not None else None},
        ).fetchall()
    out = []
    for sid, cut_type, business_date, cut_ts, item in rows:
        out.append(
            {
                "snapshot_id": sid,
                "cut_type": cut_type,
                "business_date": business_date,
                "cut_ts": cut_ts,
                "dataset": item["dataset"],
                "source": item["source"],
                "source_day": item["source_day"],
                "age_working_days": item["age_working_days"],
                "age_limit_exceeded": item["age_limit_exceeded"],
                "reason": item["reason"],
            }
        )
    return out


def fresher_set_ids(
    conn: DictConnection, first: date, last: date
) -> dict[tuple[date, datetime], int]:
    """``(business_date, cut_ts) -> id`` of the newest quote set that makes a cut outdated, for the
    current ``eod_final`` cuts of ``[first, last]`` that carry a curve for which the store now has
    a quote set of a day after the carried source day, up to the cut's own business date: the
    real data arrived (or a newer carry source), so a rebuild differs. The id says *which* data:
    a later one means that more data arrived since."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT p.business_date, p.cut_ts, max(s.id)
            FROM ops.published_snapshot p
            CROSS JOIN LATERAL jsonb_array_elements(p.manifest -> 'carried_curves') c
            JOIN md.curve_snapshot s
              ON s.dataset = c ->> 'dataset'
             AND s.snapshot_date > (c ->> 'source_day')::date
             AND s.snapshot_date <= p.business_date
             AND EXISTS (SELECT 1 FROM md.curve_quote q WHERE q.snapshot_id = s.id)
            WHERE p.kind = 'market' AND p.cut_type = 'eod_final'
              AND p.business_date BETWEEN %(a)s AND %(b)s
              AND jsonb_typeof(p.manifest -> 'carried_curves') = 'array'
              AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
            GROUP BY p.business_date, p.cut_ts
            ORDER BY 1, 2
            """,
            {"a": first, "b": last},
        ).fetchall()
    return {(r[0], r[1]): int(r[2]) for r in rows}


def pending_carried_cuts(
    conn: DictConnection, first: date, last: date
) -> list[tuple[date, datetime]]:
    """``(business_date, cut_ts)`` of the current ``eod_final`` cuts of ``[first, last]`` that
    carry a curve for which the store now has a quote set of a day after the carried source day,
    up to the cut's own business date: the real data arrived (or a newer carry source), so a
    rebuild differs and the cut is outdated."""
    return list(fresher_set_ids(conn, first, last))
