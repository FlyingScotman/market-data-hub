"""``snapshot_heal``: the hub repairs its own ``eod_final`` history (market-data-hub#138).

Two kinds of defects of the published history are found from the database and repaired without
an operator:

* **Missing cut** - a business day of ``MOEX_STOCK`` between ``MDHUB_SNAPSHOT_HEAL_FROM`` and
  the day before the previous business day that has bond quotes in the store but no
  ``eod_final`` cut. The cut is published like ``mdhub run snapshot_market -p mode=eod_final
  -p day=D`` does: as known now, bound to a static built for D
  (``parameters.static_basis``, #102/#104). Gaps: dataset :data:`MISSING_GAPS` of ``ops.data_gap``.
* **Cut on the static of its build day** - a current ``eod_final`` of a business day in
  :data:`REISSUE_FROM` .. :data:`REISSUE_TO` (the range of #106: cuts published before contract
  1.4 and the as-of-date static) whose linked static has no ``parameters.static_basis``. The cut
  is rebuilt like ``snapshot_rebuild`` does (same ``cut_ts``; a correction that ``supersedes``
  the old snapshot, which stays readable). Gaps: dataset :data:`REISSUE_GAPS`, one per cut
  (``key.cut_ts``; a day can have more than one current cut). Nothing outside
  that range is ever reissued here; the range is a constant, not a setting.

Status: every defect is an ``ops.data_gap`` row (``/v1/gaps?dataset=...`` lists the open ones
with the reason; a repaired one becomes ``backfilled``; a day that cannot be repaired becomes
``unfillable`` with the reason, and no day is left without a note). ``/v1/runs?job=snapshot_heal``
shows what each run built.

Behaviour, per run (a polling rule, every 30 minutes; the job lock keeps one run at a time):

1. Detection (read-only SQL) and gap bookkeeping, at any hour.
2. Repair only in ``repair`` mode, only inside ``MDHUB_SNAPSHOT_HEAL_WINDOW`` (night, so the
   live loads and cuts are not slowed down), at most ``MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN`` cuts,
   ``MDHUB_SNAPSHOT_HEAL_PAUSE_S`` seconds apart. Missing cuts first, oldest day first.
3. The state lives in the database (published cuts, gaps), so an interrupted run loses nothing:
   the next run detects what is left and continues. Nothing left: the run is ``skipped``
   ("nothing to heal") - a repeated run is a no-op. A cut that fails is retried after
   :data:`RETRY_AFTER` and given up (``unfillable``) after :data:`MAX_ATTEMPTS` attempts.

A missing day without bond quotes is not built (a cut of nothing): its gap stays ``open`` with
that reason until the quotes are loaded (the ordinary ``bonds_eod`` backfill), then the next run
builds it.
"""

from __future__ import annotations

import re
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.config import in_clock_window, parse_clock_window
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import UpsertResult
from mdhub.db.pool import DictConnection
from mdhub.jobs import JobContext, job
from mdhub.loaders.snapshots import _cut_ts, market_params
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.static import BOND_CALENDAR

__all__ = [
    "MISSING_GAPS",
    "REISSUE_FROM",
    "REISSUE_GAPS",
    "REISSUE_TO",
    "snapshot_heal",
]

MISSING_GAPS: Final = "snapshot_cut_missing"
"""``ops.data_gap`` dataset: business days without an ``eod_final`` cut."""
REISSUE_GAPS: Final = "snapshot_cut_reissue"
"""``ops.data_gap`` dataset: ``eod_final`` cuts still on the static of their build day."""

REISSUE_FROM: Final = date(2026, 1, 5)
REISSUE_TO: Final = date(2026, 10, 21)
"""The cuts of these business dates were published on statics that predate contract 1.4 (203 on
static 1.0, one on 1.3; #101, #106); the cuts of 2026-10-22 on carry static 1.5 with the terms."""

MAX_ATTEMPTS: Final = 5
RETRY_AFTER: Final = timedelta(hours=6)
"""A cut that failed is not tried again before this time (the gap's ``detected_at``)."""

GapId = tuple[date, str]
"""Identity of a gap inside its dataset: ``(gap_date, cut_ts key)``. A missing cut is the day
(``""``); a reissue is one *cut* of the day (its ``cut_ts``, ISO UTC), because a day can have
more than one current ``eod_final`` cut."""

_FAILED: Final = "heal failed"
_ATTEMPT: Final = re.compile(r"^heal failed \((\d+)\)")
_NOTE_CHARS: Final = 300

# indirection for tests (no real sleeping / fixed clock)
_sleep: Callable[[float], None] = _time.sleep
_now: Callable[[], datetime] = utc_now


# ------------------------------------------------------------------------------ detection


def missing_cut_days(
    conn: DictConnection,
    first: date,
    last: date,
    is_business_day: Callable[[date], bool],
) -> list[date]:
    """Business days of ``[first, last]`` without any ``eod_final`` market snapshot (a
    superseded one counts: the day has been cut)."""
    if last < first:
        return []
    with conn.cursor(row_factory=tuple_row) as cur:
        cut = {
            r[0]
            for r in cur.execute(
                """
                SELECT DISTINCT business_date FROM ops.published_snapshot
                WHERE kind = 'market' AND cut_type = 'eod_final'
                  AND business_date BETWEEN %s AND %s
                """,
                (first, last),
            ).fetchall()
        }
    day, out = first, []
    while day <= last:
        if day not in cut and is_business_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def days_with_bond_quotes(conn: DictConnection, days: list[date], min_rows: int) -> set[date]:
    """The ``days`` that have at least ``min_rows`` rows in ``md.bond_eod`` or in
    ``md.bond_eod_yield`` (the inputs of the bond quotes of an EOD cut)."""
    if not days:
        return set()
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT trade_date, sum(n) FROM (
                SELECT trade_date, count(*) AS n FROM md.bond_eod
                WHERE trade_date = ANY(%(d)s) GROUP BY trade_date
                UNION ALL
                SELECT trade_date, count(*) FROM md.bond_eod_yield
                WHERE trade_date = ANY(%(d)s) GROUP BY trade_date
            ) q GROUP BY trade_date HAVING max(n) >= %(m)s
            """,
            {"d": days, "m": min_rows},
        ).fetchall()
    return {r[0] for r in rows}


def pending_reissue(
    conn: DictConnection, first: date = REISSUE_FROM, last: date = REISSUE_TO
) -> list[tuple[date, datetime]]:
    """``(business_date, cut_ts)`` of the current ``eod_final`` cuts of ``[first, last]`` whose
    static snapshot carries no ``parameters.static_basis`` (built for the build day)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT DISTINCT p.business_date, p.cut_ts
            FROM ops.published_snapshot p
            JOIN ops.published_snapshot s ON s.id = p.static_snapshot_id
            WHERE p.kind = 'market' AND p.cut_type = 'eod_final'
              AND p.business_date BETWEEN %s AND %s
              AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
              AND (s.manifest -> 'parameters' ->> 'static_basis') IS NULL
            ORDER BY 1, 2
            """,
            (first, last),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ------------------------------------------------------------------------------ gaps


def gap_id(day: date, cut_ts: datetime | None = None) -> GapId:
    """The :data:`GapId` of a missing cut (no ``cut_ts``) or of one cut to reissue."""
    return (day, "" if cut_ts is None else cut_ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _gap_key(gid: GapId) -> Jsonb:
    return Jsonb({"cut_ts": gid[1]} if gid[1] else {})


def sync_gaps(conn: DictConnection, dataset: str, found: dict[GapId, str]) -> None:
    """Make ``ops.data_gap`` of ``dataset`` say what was found: a gap per entry of ``found``
    (opened, or reopened when it had been closed; a note of a failed repair is kept; an
    ``unfillable`` gap is left alone), and every ``open`` gap no longer found closed as
    ``backfilled``."""
    for gid, note in found.items():
        conn.execute(
            """
            INSERT INTO ops.data_gap (dataset, key, gap_date, status, note)
            VALUES (%(ds)s, %(key)s, %(d)s, 'open', %(note)s)
            ON CONFLICT (dataset, key, gap_date) DO UPDATE SET
                status = CASE WHEN ops.data_gap.status = 'backfilled' THEN 'open'
                              ELSE ops.data_gap.status END,
                note = CASE WHEN ops.data_gap.status = 'unfillable'
                                 OR ops.data_gap.note LIKE %(failed)s THEN ops.data_gap.note
                            ELSE EXCLUDED.note END,
                detected_at = CASE WHEN ops.data_gap.status = 'backfilled' THEN now()
                                   ELSE ops.data_gap.detected_at END
            """,
            {
                "ds": dataset,
                "key": _gap_key(gid),
                "d": gid[0],
                "note": note[:_NOTE_CHARS],
                "failed": f"{_FAILED}%",
            },
        )
    for gid, (status, _note, _at) in _gap_state(conn, dataset).items():
        if status == "open" and gid not in found:
            _close_gap(
                conn,
                dataset,
                gid,
                "no longer detected (repaired, or outside the checked range)",
            )


def _gap_state(conn: DictConnection, dataset: str) -> dict[GapId, tuple[str, str, datetime]]:
    with conn.cursor(row_factory=tuple_row) as cur:
        return {
            (r[0], r[1]): (r[2], r[3] or "", r[4])
            for r in cur.execute(
                "SELECT gap_date, coalesce(key ->> 'cut_ts', ''), status, note, detected_at "
                "FROM ops.data_gap WHERE dataset = %s",
                (dataset,),
            ).fetchall()
        }


def _close_gap(conn: DictConnection, dataset: str, gid: GapId, note: str) -> None:
    conn.execute(
        "UPDATE ops.data_gap SET status = 'backfilled', note = %s "
        "WHERE dataset = %s AND key = %s AND gap_date = %s",
        (note[:_NOTE_CHARS], dataset, _gap_key(gid), gid[0]),
    )


def _fail_gap(
    conn: DictConnection, dataset: str, gid: GapId, error: str, *, attempts: int, now: datetime
) -> str:
    """Note a failed repair (``detected_at`` = ``now`` starts the retry wait); the
    :data:`MAX_ATTEMPTS`-th failure gives the day up (``unfillable``). Returns the status."""
    status = "unfillable" if attempts >= MAX_ATTEMPTS else "open"
    note = f"{_FAILED} ({attempts}): {error}"
    if status == "unfillable":
        note += f" (gave up after {MAX_ATTEMPTS} attempts)"
    conn.execute(
        "UPDATE ops.data_gap SET status = %s, note = %s, detected_at = %s "
        "WHERE dataset = %s AND key = %s AND gap_date = %s",
        (status, note[:_NOTE_CHARS], now, dataset, _gap_key(gid), gid[0]),
    )
    return status


def _due(state: tuple[str, str, datetime] | None, now: datetime) -> bool:
    """Whether the repair of a day is due: not given up, and not failed within the retry wait."""
    if state is None:
        return True
    status, note, detected_at = state
    if status != "open":
        return False
    return not (note.startswith(_FAILED) and now - detected_at < RETRY_AFTER)


def _attempts(state: tuple[str, str, datetime] | None) -> int:
    m = _ATTEMPT.match(state[1]) if state else None
    return int(m.group(1)) if m else 0


# ------------------------------------------------------------------------------ the job


@job("snapshot_heal", "Find and repair missing / outdated eod_final cuts (self-healing history)")
def snapshot_heal(
    ctx: JobContext,
    *,
    mode: str | None = None,
    date_from: date | None = None,
    max_cuts: int | None = None,
    min_bond_rows: int | None = None,
    ignore_window: bool = False,
) -> dict[str, Any]:
    """Detect and repair (module doc).

    :param mode: ``off`` / ``detect`` / ``repair`` (default ``MDHUB_SNAPSHOT_HEAL``).
    :param date_from: first business date to look at (default ``MDHUB_SNAPSHOT_HEAL_FROM``).
    :param max_cuts: cuts built by this run (default ``MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN``).
    :param min_bond_rows: rows a day needs in ``md.bond_eod`` to be cut (default
        ``MDHUB_BONDS_EOD_PRELIM_MIN_ROWS``).
    :param ignore_window: build outside ``MDHUB_SNAPSHOT_HEAL_WINDOW`` too (manual runs, tests).
    """
    s = ctx.settings
    mode = mode or s.snapshot_heal
    if mode not in ("off", "detect", "repair"):
        ctx.run.mark_skipped(f"unknown mode {mode!r} (off, detect, repair)")
        return {}
    if mode == "off":
        ctx.run.mark_skipped("MDHUB_SNAPSHOT_HEAL=off")
        return {}
    first = date_from or s.snapshot_heal_from
    limit = max_cuts if max_cuts is not None else s.snapshot_heal_max_per_run
    min_rows = min_bond_rows if min_bond_rows is not None else s.bonds_eod_prelim_min_rows
    # only days older than the previous business day: its cut belongs to the scheduled eod_final
    # (waits for its inputs; the quality job catches it up)
    last_live = ctx.business_dates([1], today=ctx.today(), calendar=BOND_CALENDAR)[0]
    last = last_live - timedelta(days=1)

    # one query for the whole range (a year of days), the calendar of ctx: holidays and weekend
    # sessions of the engine as the scheduler sees them
    is_bday = ctx.calendar.for_range(first, last, BOND_CALENDAR)
    with ctx.connection() as conn:
        missing = missing_cut_days(conn, first, last, is_bday)
        with_quotes = days_with_bond_quotes(conn, missing, min_rows)
        reissue = pending_reissue(conn)
        found_missing = {
            gap_id(d): (
                "no eod_final cut; will be built"
                if d in with_quotes
                else "no eod_final cut; no bond quotes in the store for the day yet "
                "(built once they are loaded)"
            )
            for d in missing
        }
        found_reissue = {
            gap_id(d, ts): "eod_final is on the static of its build day; will be reissued on "
            "the static as of the date"
            for d, ts in reissue
        }
        sync_gaps(conn, MISSING_GAPS, found_missing)
        sync_gaps(conn, REISSUE_GAPS, found_reissue)
        missing_state = _gap_state(conn, MISSING_GAPS)
        reissue_state = _gap_state(conn, REISSUE_GAPS)

    now = _now()
    todo_missing = [
        d for d in missing if d in with_quotes and _due(missing_state.get(gap_id(d)), now)
    ]
    todo_reissue = [(d, ts) for d, ts in reissue if _due(reissue_state.get(gap_id(d, ts)), now)]
    result: dict[str, Any] = {
        "mode": mode,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "missing": len(missing),
        "missing_without_quotes": len(missing) - len(with_quotes),
        "reissue_pending": len(reissue),
        "built": [],
        "failed": [],
    }
    pending = len(todo_missing) + len(todo_reissue)
    if not missing and not reissue:
        ctx.run.mark_skipped("nothing to heal")
        return result
    if mode == "detect" or pending == 0:
        ctx.run.mark_skipped(
            f"{len(missing)} missing, {len(reissue)} to reissue; "
            + ("mode=detect" if mode == "detect" else "nothing due (waiting for quotes or retry)")
        )
        return result
    window = parse_clock_window(s.snapshot_heal_window)

    def in_window() -> bool:
        return ignore_window or in_clock_window(_now().astimezone(s.zone).time(), window)

    if not in_window():
        ctx.run.mark_skipped(
            f"{len(missing)} missing, {len(reissue)} to reissue; builds only "
            f"{s.snapshot_heal_window} (MSK)"
        )
        return result

    published = unchanged = built = 0
    failures: list[str] = []
    work: list[tuple[str, date, datetime | None]] = [
        (MISSING_GAPS, d, None) for d in todo_missing
    ] + [(REISSUE_GAPS, d, ts) for d, ts in todo_reissue]
    for dataset, day, cut_ts in work:
        if built >= limit or not in_window():
            break
        if built:
            _sleep(s.snapshot_heal_pause_s)
            if not in_window():  # the pause may have run into the end of the window
                break
        built += 1
        ka = utc_now()
        try:
            out = publish_market_snapshot(
                ctx.db,
                cut_type="eod_final",
                business_date=day,
                cut_ts=cut_ts or _cut_ts(ctx, "eod_final", day, None, ka),
                known_at=ka,
                run_id=ctx.run.id,
                params=market_params(ctx),
                historical=True,
            )
        except Exception as exc:  # one bad day must not stop the others
            ctx.logger.exception("snapshot_heal: %s %s failed", dataset, day)
            error = f"{type(exc).__name__}: {exc}"[:200]
            gid = gap_id(day, cut_ts)
            state = (missing_state if dataset == MISSING_GAPS else reissue_state).get(gid)
            with ctx.connection() as conn:
                _fail_gap(conn, dataset, gid, error, attempts=_attempts(state) + 1, now=_now())
            failures.append(f"{day}: {error}")
            result["failed"].append({"dataset": dataset, "day": day.isoformat(), "error": error})
            continue
        gid = gap_id(day, cut_ts)
        with ctx.connection() as conn:
            if dataset == MISSING_GAPS:
                _close_gap(conn, dataset, gid, f"cut published: snapshot {out.market.id}")
            elif cut_ts is not None and not _still_on_build_day_static(conn, day, cut_ts):
                _close_gap(conn, dataset, gid, f"reissued: snapshot {out.market.id}")
            else:
                # rebuilt, yet this cut still sits on a static without static_basis: retrying
                # would give the same result, so it is explained and left alone
                conn.execute(
                    "UPDATE ops.data_gap SET status = 'unfillable', note = %s "
                    "WHERE dataset = %s AND key = %s AND gap_date = %s",
                    (
                        f"rebuild ({out.market.status}) left the cut on the static of its "
                        "build day; not retried",
                        dataset,
                        _gap_key(gid),
                        day,
                    ),
                )
        if out.market.status == "published":
            published += 1
        else:
            unchanged += 1
        result["built"].append(
            {
                "dataset": dataset,
                "day": day.isoformat(),
                "status": out.market.status,
                "snapshot": out.market.id,
                "static": out.static.id,
            }
        )
        ctx.logger.info(
            "snapshot_heal: %s %s -> %s %s", dataset, day, out.market.id, out.market.status
        )
    ctx.run.add_counts(UpsertResult(inserted=published, unchanged=unchanged))
    result["remaining"] = max(0, pending - built)
    if failures:
        ctx.run.mark_partial(f"{len(failures)} cut(s) failed: " + "; ".join(failures)[:500])
    return result


def _still_on_build_day_static(conn: DictConnection, day: date, cut_ts: datetime) -> bool:
    """Whether the current ``eod_final`` cut ``(day, cut_ts)`` is still pending a reissue (the
    other cuts of the day have their own gaps)."""
    return any(ts == cut_ts for _, ts in pending_reissue(conn, day, day))
