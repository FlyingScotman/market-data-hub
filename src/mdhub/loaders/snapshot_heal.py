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

* **Outdated carried cut** - a current ``eod_final`` of a business day from
  ``MDHUB_SNAPSHOT_HEAL_FROM`` that carries a curve forward from an earlier day (contract 1.7,
  market-data-hub#143) while the store now holds a quote set of that curve for a day after the
  carried source day up to the cut's business date: the real data arrived late. The cut is rebuilt
  with its own ``cut_ts`` (a correction that ``supersedes`` it) and, the data being there, no
  longer carries it. A cut that legitimately carries a curve (nothing newer is stored) is not a
  defect and is left alone: the job never loops on it. Gaps: dataset :data:`CARRIED_GAPS`.

Status: every defect is an ``ops.data_gap`` row (``/v1/gaps?dataset=...`` lists the open ones
with the reason; a repaired one becomes ``backfilled``; a gap that is not detected any more
for another reason (the checked range moved, the calendar no longer counts the day) becomes
``not_expected`` with that reason; a day that cannot be repaired becomes ``unfillable`` with the
reason, and no day is left without a note). Rows are written only when they change.
``/v1/runs?job=snapshot_heal`` shows what each run built.

Behaviour, per run (a polling rule, every 30 minutes; the job lock keeps one run at a time):

1. Detection (read-only SQL) and gap bookkeeping, at any hour.
2. Repair only in ``repair`` mode, only inside ``MDHUB_SNAPSHOT_HEAL_WINDOW`` (night, so the
   live loads and cuts are not slowed down), at most ``MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN`` cuts,
   ``MDHUB_SNAPSHOT_HEAL_PAUSE_S`` seconds apart. Missing cuts first, oldest day first.
3. The state lives in the database (published cuts, gaps), so an interrupted run loses nothing:
   the next run detects what is left and continues. Nothing left: the run is ``skipped``
   ("nothing to heal") - a repeated run is a no-op. A cut that fails is retried after
   :data:`RETRY_AFTER` and given up (``unfillable``) after :data:`MAX_ATTEMPTS` attempts; a day
   given up because of failures (not because the rebuild changed nothing) is tried once more
   after :data:`GIVE_UP_RETRY_AFTER` and when the hub version changes, so no manual action is
   needed to get it repaired once the cause is gone.

A missing day without bond quotes is not built (a cut of nothing): its gap stays ``open`` with
that reason until the quotes are loaded (the ordinary ``bonds_eod`` backfill), then the next run
builds it. A missing day whose input data is still being filled - an ``open`` gap, or a
``pending`` / ``running`` backfill task, of a dataset the cut reads
(:data:`~mdhub.snapshot.readiness.SNAPSHOT_INPUT_JOBS`) - is not built either (the gap note
names the dataset): the autocorrection would publish a correction right after the cut, and the
consumer would download the day twice.
"""

from __future__ import annotations

import re
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub import __version__
from mdhub.config import in_clock_window, parse_clock_window
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import UpsertResult
from mdhub.datasets import datasets_for_job
from mdhub.db.pool import DictConnection
from mdhub.jobs import JobContext, job
from mdhub.loaders.snapshots import _cut_ts, market_params
from mdhub.snapshot.carry import pending_carried_cuts
from mdhub.snapshot.readiness import SNAPSHOT_INPUT_JOBS, eod_final_cuts
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.static import BOND_CALENDAR

__all__ = [
    "CARRIED_GAPS",
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

CARRIED_GAPS: Final = "snapshot_cut_carried"
"""``ops.data_gap`` dataset: ``eod_final`` cuts that carry a curve forward from an earlier day
(contract 1.7, :mod:`mdhub.snapshot.carry`) although the store now has newer data of it: outdated
cuts, one gap per cut (``key.cut_ts``). A cut that legitimately carries a curve (no newer data) is
not a gap."""

REISSUE_FROM: Final = date(2026, 1, 5)
REISSUE_TO: Final = date(2026, 10, 21)
"""The cuts of these business dates were published on statics that predate contract 1.4 (203 on
static 1.0, one on 1.3; #101, #106); the cuts of 2026-10-22 on carry static 1.5 with the terms."""

MAX_ATTEMPTS: Final = 5
RETRY_AFTER: Final = timedelta(hours=6)
"""A cut that failed is not tried again before this time (the gap's ``detected_at``)."""
GIVE_UP_RETRY_AFTER: Final = timedelta(days=7)
"""A day given up after :data:`MAX_ATTEMPTS` failures is a candidate again after this time (one
attempt; another failure gives it up again for the same time), and at once when the hub version
differs from the one that gave it up: a fix in the hub may have removed the cause."""

GapId = tuple[date, str]
"""Identity of a gap inside its dataset: ``(gap_date, cut_ts key)``. A missing cut is the day
(``""``); a reissue is one *cut* of the day (its ``cut_ts``, ISO UTC), because a day can have
more than one current ``eod_final`` cut."""

_FAILED: Final = "heal failed"
_ATTEMPT: Final = re.compile(r"^heal failed \((\d+)\)")
_GAVE_UP_HUB: Final = re.compile(r"; hub (\S+)\)$")
_NOTE_CHARS: Final = 300

# indirection for tests (no real sleeping / fixed clock)
_sleep: Callable[[float], None] = _time.sleep
_now: Callable[[], datetime] = utc_now
_hub_version: str = __version__


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


def input_datasets() -> list[str]:
    """Registered datasets whose rows of a day are read by the ``eod_final`` cut of that day
    (the datasets of the backfill jobs in :data:`SNAPSHOT_INPUT_JOBS`)."""
    return sorted({s.name for job_name in SNAPSHOT_INPUT_JOBS for s in datasets_for_job(job_name)})


def days_waiting_for_inputs(conn: DictConnection, days: list[date]) -> dict[date, list[str]]:
    """For each of ``days`` that still waits for input data, what it waits for: an ``open``
    ``ops.data_gap`` of an input dataset on that day, or a ``pending`` / ``running``
    ``ops.backfill_task`` of one whose range contains it. A cut built now would be corrected
    (a second download for the consumer) as soon as that backfill ends, so the day is left
    until then. Days without anything pending are not in the result."""
    datasets = input_datasets()
    if not days or not datasets:
        return {}
    waiting: dict[date, set[str]] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        for day, dataset in cur.execute(
            "SELECT DISTINCT gap_date, dataset FROM ops.data_gap "
            "WHERE status = 'open' AND dataset = ANY(%s) AND gap_date = ANY(%s)",
            (datasets, days),
        ).fetchall():
            waiting.setdefault(day, set()).add(f"{dataset} (open gap)")
        tasks = cur.execute(
            """
            SELECT dataset, status, params ->> 'date_from', params ->> 'date_to'
            FROM ops.backfill_task
            WHERE status IN ('pending', 'running') AND dataset = ANY(%s)
              AND params ? 'date_from' AND params ? 'date_to'
              AND params ->> 'date_to' >= %s AND params ->> 'date_from' <= %s
            """,
            (datasets, min(days).isoformat(), max(days).isoformat()),
        ).fetchall()
    for dataset, status, lo, hi in tasks:
        try:
            first, last = date.fromisoformat(str(lo)), date.fromisoformat(str(hi))
        except ValueError:
            continue
        for day in days:
            if first <= day <= last:
                waiting.setdefault(day, set()).add(f"{dataset} (backfill {status})")
    return {d: sorted(v) for d, v in waiting.items()}


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


def sync_gaps(
    conn: DictConnection,
    dataset: str,
    found: dict[GapId, str],
    closed_as: Callable[[GapId], tuple[str, str]],
) -> None:
    """Make ``ops.data_gap`` of ``dataset`` say what was found, writing only what changed:

    * a gap per entry of ``found``: opened; reopened when it had been closed (``backfilled`` /
      ``not_expected``); the note of an ``open`` gap refreshed when the reason changed (the note
      of a failed repair is kept); an ``unfillable`` gap is left alone;
    * every ``open`` or ``unfillable`` gap no longer found is closed with ``closed_as(gap)`` =
      ``(status, note)``: repaired (``backfilled``) or not expected any more (``not_expected``:
      outside the checked range, a day the calendar no longer counts), so the note says why.
    """
    state = _gap_state(conn, dataset)
    for gid, found_note in found.items():
        note = found_note[:_NOTE_CHARS]
        known = state.get(gid)
        if known is None:
            conn.execute(
                "INSERT INTO ops.data_gap (dataset, key, gap_date, status, note) "
                "VALUES (%s, %s, %s, 'open', %s) ON CONFLICT DO NOTHING",
                (dataset, _gap_key(gid), gid[0], note),
            )
        elif known[0] in ("backfilled", "not_expected"):
            conn.execute(
                "UPDATE ops.data_gap SET status = 'open', note = %s, detected_at = now() "
                "WHERE dataset = %s AND key = %s AND gap_date = %s",
                (note, dataset, _gap_key(gid), gid[0]),
            )
        elif known[0] == "open" and not known[1].startswith(_FAILED) and known[1] != note:
            _set_gap(conn, dataset, gid, "open", note)
    for gid, (status, _note, _at) in state.items():
        if status in ("open", "unfillable") and gid not in found:
            _set_gap(conn, dataset, gid, *closed_as(gid))


def _set_gap(conn: DictConnection, dataset: str, gid: GapId, status: str, note: str) -> None:
    conn.execute(
        "UPDATE ops.data_gap SET status = %s, note = %s "
        "WHERE dataset = %s AND key = %s AND gap_date = %s",
        (status, note[:_NOTE_CHARS], dataset, _gap_key(gid), gid[0]),
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
    _set_gap(conn, dataset, gid, "backfilled", note)


def _fail_gap(
    conn: DictConnection, dataset: str, gid: GapId, error: str, *, attempts: int, now: datetime
) -> str:
    """Note a failed repair (``detected_at`` = ``now`` starts the retry wait); the
    :data:`MAX_ATTEMPTS`-th failure gives the day up (``unfillable``, the note keeps the hub
    version that gave up: :func:`_due`). Returns the status."""
    status = "unfillable" if attempts >= MAX_ATTEMPTS else "open"
    head = f"{_FAILED} ({attempts}): "
    tail = (
        f" (gave up after {attempts} attempts; hub {_hub_version})"
        if status == "unfillable"
        else ""
    )
    note = head + error[: max(0, _NOTE_CHARS - len(head) - len(tail))] + tail
    conn.execute(
        "UPDATE ops.data_gap SET status = %s, note = %s, detected_at = %s "
        "WHERE dataset = %s AND key = %s AND gap_date = %s",
        (status, note[:_NOTE_CHARS], now, dataset, _gap_key(gid), gid[0]),
    )
    return status


def _missing_closed(
    first: date, last: date, is_business_day: Callable[[date], bool]
) -> Callable[[GapId], tuple[str, str]]:
    """Why a gap of a missing cut is not detected any more: the day left the checked range
    (``MDHUB_SNAPSHOT_HEAL_FROM``) or the calendar no longer counts it (``not_expected``), or
    else it has its cut now (``backfilled``)."""

    def closed_as(gid: GapId) -> tuple[str, str]:
        day = gid[0]
        if not first <= day <= last:
            return "not_expected", f"outside the checked range {first}..{last}; not looked at"
        if not is_business_day(day):
            return "not_expected", "not a business day of the calendar (any more)"
        return "backfilled", "an eod_final cut exists now"

    return closed_as


def _reissue_closed(conn: DictConnection) -> Callable[[GapId], tuple[str, str]]:
    """Why a gap of a reissue is not detected any more: the cut is current and no longer on the
    static of its build day (``backfilled``), or it is not a current cut any more
    (``not_expected``)."""
    return _cut_closed(
        conn, REISSUE_FROM, REISSUE_TO, "the cut is no longer on the static of its build day"
    )


def _carried_closed(
    conn: DictConnection, first: date, last: date
) -> Callable[[GapId], tuple[str, str]]:
    """Why a gap of an outdated carried cut is not detected any more: the cut is current and
    carries nothing that newer data replaces (``backfilled``), or it is not a current cut any
    more (``not_expected``)."""
    return _cut_closed(conn, first, last, "the cut no longer carries a curve with newer data")


def _cut_closed(
    conn: DictConnection, first: date, last: date, done: str
) -> Callable[[GapId], tuple[str, str]]:
    current: set[GapId] | None = None

    def closed_as(gid: GapId) -> tuple[str, str]:
        nonlocal current
        if current is None:
            current = {gap_id(d, ts) for d, ts in eod_final_cuts(conn, first, last)}
        if gid in current:
            return "backfilled", done
        return "not_expected", "the cut is not a current eod_final cut any more"

    return closed_as


def _missing_note(has_quotes: bool, waiting: list[str] | None) -> str:
    if not has_quotes:
        return (
            "no eod_final cut; no bond quotes in the store for the day yet "
            "(built once they are loaded)"
        )
    if waiting:
        return (
            "no eod_final cut; waiting for the input data of the day: "
            + ", ".join(waiting[:4])
            + (", ..." if len(waiting) > 4 else "")
            + " (built once they are loaded)"
        )
    return "no eod_final cut; will be built"


def _due(state: tuple[str, str, datetime] | None, now: datetime) -> bool:
    """Whether the repair of a day is due: not failed within the retry wait; a day given up
    after failures is due again after :data:`GIVE_UP_RETRY_AFTER` or under another hub version.
    A day given up for another reason (the rebuild leaves the cut as it was) never is."""
    if state is None:
        return True
    status, note, detected_at = state
    if status == "unfillable":
        if not note.startswith(_FAILED):
            return False
        gave_up = _GAVE_UP_HUB.search(note)
        return now - detected_at >= GIVE_UP_RETRY_AFTER or (
            gave_up is None or gave_up.group(1) != _hub_version
        )
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

    # one query for the whole range (a year of days), the calendar of ctx as the scheduler sees
    # it: a business day is what the calendar says, holidays and working weekend days included
    # (not the weekday)
    is_bday = ctx.calendar.for_range(first, last, BOND_CALENDAR)
    with ctx.connection() as conn:
        missing = missing_cut_days(conn, first, last, is_bday)
        with_quotes = days_with_bond_quotes(conn, missing, min_rows)
        waiting = days_waiting_for_inputs(conn, [d for d in missing if d in with_quotes])
        reissue = pending_reissue(conn)
        carried = pending_carried_cuts(conn, first, last)
        found_missing = {
            gap_id(d): _missing_note(d in with_quotes, waiting.get(d)) for d in missing
        }
        found_reissue = {
            gap_id(d, ts): "eod_final is on the static of its build day; will be reissued on "
            "the static as of the date"
            for d, ts in reissue
        }
        sync_gaps(conn, MISSING_GAPS, found_missing, _missing_closed(first, last, is_bday))
        found_carried = {
            gap_id(d, ts): "eod_final carries a curve forward from an earlier day, but newer data "
            "of it is stored now; will be reissued"
            for d, ts in carried
        }
        sync_gaps(conn, REISSUE_GAPS, found_reissue, _reissue_closed(conn))
        sync_gaps(conn, CARRIED_GAPS, found_carried, _carried_closed(conn, first, last))
        missing_state = _gap_state(conn, MISSING_GAPS)
        reissue_state = _gap_state(conn, REISSUE_GAPS)
        carried_state = _gap_state(conn, CARRIED_GAPS)
    states = {MISSING_GAPS: missing_state, REISSUE_GAPS: reissue_state, CARRIED_GAPS: carried_state}

    now = _now()
    todo_missing = [
        d
        for d in missing
        if d in with_quotes and d not in waiting and _due(missing_state.get(gap_id(d)), now)
    ]
    todo_reissue = [(d, ts) for d, ts in reissue if _due(reissue_state.get(gap_id(d, ts)), now)]
    todo_carried = [(d, ts) for d, ts in carried if _due(carried_state.get(gap_id(d, ts)), now)]
    result: dict[str, Any] = {
        "mode": mode,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "missing": len(missing),
        "missing_without_quotes": len(missing) - len(with_quotes),
        "missing_waiting_inputs": len(waiting),
        "reissue_pending": len(reissue),
        "carried_pending": len(carried),
        "built": [],
        "failed": [],
    }
    # one build per cut: a cut that is both on an old static and carrying an outdated curve is
    # rebuilt once and closes both of its gaps
    work: list[tuple[date, datetime | None, list[str]]] = [
        (d, None, [MISSING_GAPS]) for d in todo_missing
    ]
    by_cut: dict[GapId, tuple[date, datetime, list[str]]] = {}
    for dataset, cuts in ((REISSUE_GAPS, todo_reissue), (CARRIED_GAPS, todo_carried)):
        for d, ts in cuts:
            by_cut.setdefault(gap_id(d, ts), (d, ts, []))[2].append(dataset)
    work.extend(by_cut[k] for k in sorted(by_cut))
    pending = len(work)
    if not missing and not reissue and not carried:
        ctx.run.mark_skipped("nothing to heal")
        return result
    if mode == "detect" or pending == 0:
        ctx.run.mark_skipped(
            f"{len(missing)} missing, {len(reissue)} to reissue, {len(carried)} outdated "
            "carried; "
            + (
                "mode=detect"
                if mode == "detect"
                else "nothing due (waiting for quotes, inputs or retry)"
            )
        )
        return result
    window = parse_clock_window(s.snapshot_heal_window)

    def in_window() -> bool:
        return ignore_window or in_clock_window(_now().astimezone(s.zone).time(), window)

    if not in_window():
        ctx.run.mark_skipped(
            f"{len(missing)} missing, {len(reissue)} to reissue, {len(carried)} outdated "
            f"carried; builds only {s.snapshot_heal_window} (MSK)"
        )
        return result

    published = unchanged = built = 0
    failures: list[str] = []
    for day, cut_ts, datasets in work:
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
            ctx.logger.exception("snapshot_heal: %s %s failed", datasets, day)
            error = f"{type(exc).__name__}: {exc}"[:200]
            gid = gap_id(day, cut_ts)
            with ctx.connection() as conn:
                for dataset in datasets:
                    _fail_gap(
                        conn,
                        dataset,
                        gid,
                        error,
                        attempts=_attempts(states[dataset].get(gid)) + 1,
                        now=_now(),
                    )
            failures.append(f"{day}: {error}")
            result["failed"].append(
                {"dataset": datasets[0], "day": day.isoformat(), "error": error}
            )
            continue
        gid = gap_id(day, cut_ts)
        with ctx.connection() as conn:
            for dataset in datasets:
                if dataset == MISSING_GAPS:
                    _close_gap(conn, dataset, gid, f"cut published: snapshot {out.market.id}")
                    continue
                assert cut_ts is not None
                if dataset == REISSUE_GAPS:
                    still, why = (
                        _still_on_build_day_static(conn, day, cut_ts),
                        "left the cut on the static of its build day",
                    )
                else:
                    still, why = (
                        _still_carried_outdated(conn, day, cut_ts),
                        "left the cut carrying a curve for which newer data is stored",
                    )
                if not still:
                    _close_gap(conn, dataset, gid, f"reissued: snapshot {out.market.id}")
                else:
                    # rebuilt, yet the defect is still there: retrying would give the same
                    # result, so it is explained and left alone (never a loop)
                    conn.execute(
                        "UPDATE ops.data_gap SET status = 'unfillable', note = %s "
                        "WHERE dataset = %s AND key = %s AND gap_date = %s",
                        (
                            f"rebuild ({out.market.status}) {why}; not retried",
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
                "dataset": datasets[0],
                "datasets": datasets,
                "day": day.isoformat(),
                "status": out.market.status,
                "snapshot": out.market.id,
                "static": out.static.id,
            }
        )
        ctx.logger.info(
            "snapshot_heal: %s %s -> %s %s", datasets, day, out.market.id, out.market.status
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


def _still_carried_outdated(conn: DictConnection, day: date, cut_ts: datetime) -> bool:
    """Whether the current ``eod_final`` cut ``(day, cut_ts)`` still carries a curve for which
    newer data is stored (the rebuild did not change that)."""
    return any(ts == cut_ts for _, ts in pending_carried_cuts(conn, day, day))
