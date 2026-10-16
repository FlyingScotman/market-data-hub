"""Freshness and gap checks for registered datasets, ``ops.data_gap`` and backfill queueing.

Everything is driven by :class:`mdhub.datasets.DatasetSpec`:

* **expected latest date** (:func:`expected_latest`): ``business_daily``/``intraday`` - the
  last business day of ``calendar`` on or before today, moved back ``max_lag_days`` business
  days; ``daily``/``event`` - today minus ``max_lag_days`` calendar days; ``monthly`` - the
  first day of the month containing today minus ``max_lag_days`` days. A dataset whose latest
  stored date is earlier is **stale**.
* **gap scan** (:func:`scan_dataset`): over ``max(history_start, earliest stored date)`` ..
  expected latest, every expected day (business days of the calendar - ``ref.calendar_day``
  via the job's calendar predicate, weekday fallback; every day for ``daily``; month starts
  for ``monthly``; nothing for ``event``) with fewer than ``min_rows_per_day`` rows is a gap.
  With ``series_column`` the count is per series: listed ``series`` are checked from their
  own first stored date; without a list, every series seen in the window is checked between
  its first and last date in the window (holes only - a series that ended is not a gap).
  ``intraday`` datasets are scanned over the last :data:`INTRADAY_SCAN_DAYS` days only.
* **gap bookkeeping** (:func:`sync_gaps`): new gaps are inserted ``open``; gaps that are no
  longer missing become ``backfilled`` (also ``unfillable`` ones: data that appears later is
  recognised); still-missing ``unfillable`` gaps are left alone (never re-queued).
* **queueing** (:func:`queue_backfill`): open gap days not covered by an open task (or by a
  task that failed in the last :data:`FAILED_TASK_COOLDOWN_DAYS` days) of *any dataset sharing
  the same backfill job* (e.g. the six ``cbr_*`` datasets -> one ``cbr_daily`` task) are
  compressed into ranges of consecutive expected days (at most ``DatasetSpec.max_task_days``,
  default :data:`MAX_TASK_DAYS` calendar days, a year for monthly data) and inserted into
  ``ops.backfill_task`` with params ``{"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}``.

SQL identifiers come from the registered specs and are composed with :mod:`psycopg.sql`;
``DatasetSpec.where`` is inserted verbatim (it is a constant written by the module author;
``%`` is escaped automatically).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.bizday import CalendarPredicate
from mdhub.core.gaps import no_data_status
from mdhub.core.timeutil import MSK, add_business_days, date_range, is_weekday, msk_today
from mdhub.datasets import DatasetSpec, datasets_for_job

__all__ = [
    "FAILED_TASK_COOLDOWN_DAYS",
    "INTRADAY_SCAN_DAYS",
    "MAX_TASK_DAYS",
    "DatasetCheck",
    "Gap",
    "GapSync",
    "TableInfo",
    "compress_ranges",
    "count_rows",
    "day_predicate",
    "expected_days",
    "expected_latest",
    "gap_series",
    "job_datasets",
    "max_task_days",
    "month_start",
    "probe_table",
    "queue_backfill",
    "queue_range",
    "reconcile_gaps",
    "scan_dataset",
    "split_range",
    "sync_gaps",
]

INTRADAY_SCAN_DAYS: Final = 14
"""Calendar days scanned for gaps in ``intraday`` datasets (raw intraday is not backfillable
far back and the tables are large)."""

MAX_TASK_DAYS: Final = 31
"""Maximum calendar-day length of one backfill task range (daily-type datasets)."""

FAILED_TASK_COOLDOWN_DAYS: Final = 7
"""Days covered by a task that exhausted its attempts are not re-queued for this long."""

DayPredicate = Callable[[date], bool]
Conn = psycopg.Connection[Any]
SeriesKey = str | None


# ------------------------------------------------------------------------ date logic


def month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def day_predicate(spec: DatasetSpec, calendar: CalendarPredicate) -> DayPredicate:
    """Which days are expected to have data for ``spec``."""
    if spec.frequency == "daily":
        return lambda _d: True
    if spec.frequency == "monthly":
        return lambda d: d.day == 1
    if spec.calendar is None:
        return is_weekday
    cal = spec.calendar
    return lambda d: calendar(d, cal)


def expected_latest(spec: DatasetSpec, today: date, calendar: CalendarPredicate) -> date:
    """Latest date ``spec`` should contain on ``today`` (see module docstring)."""
    if spec.frequency in ("business_daily", "intraday"):
        pred = day_predicate(spec, calendar)
        last = today if pred(today) else add_business_days(today, -1, pred)
        return add_business_days(last, -spec.max_lag_days, pred)
    if spec.frequency == "monthly":
        return month_start(today - timedelta(days=spec.max_lag_days))
    return today - timedelta(days=spec.max_lag_days)


def expected_days(
    spec: DatasetSpec, start: date, end: date, calendar: CalendarPredicate
) -> list[date]:
    """Expected data days (month starts for ``monthly``) in ``[start, end]``."""
    if start > end:
        return []
    if spec.frequency == "monthly":
        out: list[date] = []
        cur = month_start(start)
        if cur < start:
            cur = _next_month(cur)
        while cur <= end:
            out.append(cur)
            cur = _next_month(cur)
        return out
    pred = day_predicate(spec, calendar)
    return [d for d in date_range(start, end) if pred(d)]


def max_task_days(spec: DatasetSpec) -> int:
    """Longest task range of ``spec`` (its ``max_task_days``, else 31 / 366 for monthly)."""
    if spec.max_task_days is not None:
        return spec.max_task_days
    return 366 if spec.frequency == "monthly" else MAX_TASK_DAYS


def job_datasets(spec: DatasetSpec) -> list[str]:
    """Names of all registered datasets sharing ``spec``'s backfill job (incl. ``spec``)."""
    names = {spec.name}
    if spec.backfill_job:
        names |= {s.name for s in datasets_for_job(spec.backfill_job)}
    return sorted(names)


def compress_ranges(
    days: Iterable[date], is_expected: DayPredicate, max_days: int = MAX_TASK_DAYS
) -> list[tuple[date, date]]:
    """Merge gap days into inclusive ranges.

    Two gap days belong to the same range when no *expected* day lies strictly between them
    (e.g. Friday and Monday of a weekday calendar) and the range stays within ``max_days``
    calendar days.
    """
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    ranges: list[tuple[date, date]] = []
    start: date | None = None
    prev: date | None = None
    for d in sorted(set(days)):
        if start is None or prev is None:
            start = prev = d
            continue
        between = date_range(prev + timedelta(days=1), d - timedelta(days=1))
        contiguous = not any(is_expected(x) for x in between)
        if contiguous and (d - start).days + 1 <= max_days:
            prev = d
        else:
            ranges.append((start, prev))
            start = prev = d
    if start is not None and prev is not None:
        ranges.append((start, prev))
    return ranges


def split_range(
    date_from: date, date_to: date, max_days: int = MAX_TASK_DAYS
) -> list[tuple[date, date]]:
    """Split ``[date_from, date_to]`` into consecutive chunks of at most ``max_days`` days."""
    if date_to < date_from:
        raise ValueError("date_to is before date_from")
    out: list[tuple[date, date]] = []
    cur = date_from
    while cur <= date_to:
        end = min(date_to, cur + timedelta(days=max_days - 1))
        out.append((cur, end))
        cur = end + timedelta(days=1)
    return out


# ----------------------------------------------------------------------- table access


@dataclass(frozen=True, slots=True)
class TableInfo:
    """Resolved SQL pieces for one dataset table."""

    spec: DatasetSpec
    date_type: str

    @property
    def table(self) -> sql.Identifier:
        schema, name = self.spec.table.split(".", 1)
        return sql.Identifier(schema, name)

    @property
    def col(self) -> sql.Identifier:
        return sql.Identifier(self.spec.date_column)

    @property
    def is_tz(self) -> bool:
        return self.date_type == "timestamp with time zone"

    @property
    def is_ts(self) -> bool:
        return self.date_type.startswith("timestamp")

    def day_expr(self) -> sql.Composable:
        """SQL expression giving the business date (MSK) of a row."""
        if self.is_tz:
            return sql.SQL("({} AT TIME ZONE 'Europe/Moscow')::date").format(self.col)
        if self.is_ts:
            return sql.SQL("({})::date").format(self.col)
        return self.col

    def bucket_expr(self) -> sql.Composable:
        """Day expression, truncated to the month for ``monthly`` datasets."""
        if self.spec.frequency == "monthly":
            return sql.SQL("date_trunc('month', {})::date").format(self.day_expr())
        return self.day_expr()

    def range_pred(self, start: date, end: date) -> tuple[sql.Composable, list[Any]]:
        """Index-friendly predicate on the raw column for business dates ``[start, end]``."""
        if self.is_tz:
            lo: Any = datetime.combine(start, time(0), tzinfo=MSK)
            hi: Any = datetime.combine(end + timedelta(days=1), time(0), tzinfo=MSK)
        elif self.is_ts:
            lo = datetime.combine(start, time(0))
            hi = datetime.combine(end + timedelta(days=1), time(0))
        else:
            lo, hi = start, end + timedelta(days=1)
        return sql.SQL("{c} >= %s AND {c} < %s").format(c=self.col), [lo, hi]

    def where_extra(self) -> sql.Composable:
        """`` AND (<spec.where>)`` or nothing."""
        if not self.spec.where:
            return sql.SQL("")
        return sql.SQL(" AND ({})").format(sql.SQL(self.spec.where.replace("%", "%%")))

    def to_day(self, value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(MSK).date()
            return value.date()
        if isinstance(value, date):
            return value
        raise TypeError(f"{self.spec.name}: unsupported date value {value!r}")


_DATE_TYPES: Final = ("date", "timestamp with time zone", "timestamp without time zone")


def probe_table(conn: Conn, spec: DatasetSpec) -> TableInfo | None:
    """Resolve the date column type; ``None`` if the table does not exist yet.

    :raises ValueError: the date/series column is missing or has an unsupported type.
    """
    schema, name = spec.table.split(".", 1)
    with conn.cursor(row_factory=tuple_row) as cur:
        exists = cur.execute(
            "SELECT to_regclass(format('%%I.%%I', %s::text, %s::text))", (schema, name)
        ).fetchone()
        if exists is None or exists[0] is None:
            return None
        rows = cur.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = format('%%I.%%I', %s::text, %s::text)::regclass
              AND a.attnum > 0 AND NOT a.attisdropped
            """,
            (schema, name),
        ).fetchall()
    cols = {str(c): str(t) for c, t in rows}
    typ = cols.get(spec.date_column)
    if typ is None:
        raise ValueError(f"{spec.name}: column {spec.date_column} not in {spec.table}")
    if typ not in _DATE_TYPES:
        raise ValueError(f"{spec.name}: unsupported date column type {typ}")
    if spec.series_column and spec.series_column not in cols:
        raise ValueError(f"{spec.name}: column {spec.series_column} not in {spec.table}")
    return TableInfo(spec=spec, date_type=typ)


def _bounds(conn: Conn, info: TableInfo) -> tuple[date | None, date | None, int]:
    q = sql.SQL("SELECT min({c}), max({c}), count(*) FROM {t} WHERE true{w}").format(
        c=info.col, t=info.table, w=info.where_extra()
    )
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(q, []).fetchone()  # params given: '%%' in `where` is unescaped
    assert row is not None
    return info.to_day(row[0]), info.to_day(row[1]), int(row[2])


def _series_bounds(
    conn: Conn,
    info: TableInfo,
    *,
    series: Sequence[str] | None = None,
    window: tuple[date, date] | None = None,
) -> dict[str, tuple[date, date]]:
    assert info.spec.series_column is not None
    scol = sql.Identifier(info.spec.series_column)
    parts: list[sql.Composable] = [sql.SQL("true")]
    params: list[Any] = []
    if window is not None:
        pred, p = info.range_pred(*window)
        parts.append(pred)
        params.extend(p)
    if series is not None:
        parts.append(sql.SQL("{}::text = ANY(%s)").format(scol))
        params.append(list(series))
    q = sql.SQL("SELECT {s}::text, min({c}), max({c}) FROM {t} WHERE {cond}{w} GROUP BY 1").format(
        s=scol,
        c=info.col,
        t=info.table,
        cond=sql.SQL(" AND ").join(parts),
        w=info.where_extra(),
    )
    out: dict[str, tuple[date, date]] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        for s, lo, hi in cur.execute(q, params).fetchall():
            lo_d, hi_d = info.to_day(lo), info.to_day(hi)
            if s is not None and lo_d is not None and hi_d is not None:
                out[str(s)] = (lo_d, hi_d)
    return out


def count_rows(
    conn: Conn,
    info: TableInfo,
    start: date,
    end: date,
    *,
    by_series: bool = False,
    series: Sequence[str] | None = None,
) -> dict[tuple[SeriesKey, date], int]:
    """Row counts per (series or ``None``, day bucket) for business dates ``[start, end]``."""
    spec = info.spec
    scan_end = end
    if spec.frequency == "monthly":
        start = month_start(start)
        scan_end = _next_month(month_start(end)) - timedelta(days=1)
    pred, params = info.range_pred(start, scan_end)
    parts: list[sql.Composable] = [pred]
    if by_series:
        assert spec.series_column is not None
        scol: sql.Composable = sql.SQL("{}::text").format(sql.Identifier(spec.series_column))
        if series is not None:
            parts.append(sql.SQL("{} = ANY(%s)").format(scol))
            params.append(list(series))
    else:
        scol = sql.SQL("NULL::text")
    q = sql.SQL(
        "SELECT {b} AS day, {s} AS series, count(*) FROM {t} WHERE {cond}{w} GROUP BY 1, 2"
    ).format(
        b=info.bucket_expr(),
        s=scol,
        t=info.table,
        cond=sql.SQL(" AND ").join(parts),
        w=info.where_extra(),
    )
    out: dict[tuple[SeriesKey, date], int] = {}
    with conn.cursor(row_factory=tuple_row) as cur:
        for day, s, n in cur.execute(q, params).fetchall():
            out[(None if s is None else str(s), day)] = int(n)
    return out


# --------------------------------------------------------------------------- the scan


@dataclass(frozen=True, slots=True)
class Gap:
    """One missing (dataset, key, day); ``key`` is ``()`` or ``((series_column, value),)``."""

    day: date
    key: tuple[tuple[str, str], ...] = ()
    rows: int = 0

    @property
    def key_dict(self) -> dict[str, str]:
        return dict(self.key)

    @property
    def ident(self) -> tuple[str, date]:
        return (_key_text(self.key_dict), self.day)


def _key_text(key: Mapping[str, Any]) -> str:
    return json.dumps(dict(key), sort_keys=True, separators=(",", ":"))


def gap_series(spec: DatasetSpec, key: Mapping[str, Any]) -> SeriesKey:
    """Series value of a gap key for ``spec`` (``None`` for dataset-level gaps)."""
    if not spec.series_column:
        return None
    value = key.get(spec.series_column)
    return None if value is None else str(value)


@dataclass(slots=True)
class DatasetCheck:
    """Result of checking one dataset."""

    dataset: str
    table: str
    frequency: str
    calendar: str | None
    backfill_job: str | None
    expected_latest: date | None = None
    first_date: date | None = None
    last_date: date | None = None
    row_count: int | None = None
    is_stale: bool = False
    stale_series: list[str] = field(default_factory=list)
    scan_from: date | None = None
    scan_to: date | None = None
    gaps: list[Gap] = field(default_factory=list)
    problem: str | None = None
    gaps_opened: int = 0
    gaps_closed: int = 0
    tasks_queued: list[tuple[date, date]] = field(default_factory=list)

    @property
    def scanned(self) -> bool:
        return self.scan_from is not None and self.scan_to is not None

    def to_dict(self) -> dict[str, Any]:
        days = sorted({g.day for g in self.gaps})
        return {
            "dataset": self.dataset,
            "table": self.table,
            "frequency": self.frequency,
            "calendar": self.calendar,
            "expected_latest": _iso(self.expected_latest),
            "first_date": _iso(self.first_date),
            "last_date": _iso(self.last_date),
            "row_count": self.row_count,
            "is_stale": self.is_stale,
            "stale_series": self.stale_series,
            "scan_from": _iso(self.scan_from),
            "scan_to": _iso(self.scan_to),
            "gap_count": len(self.gaps),
            "gap_days": len(days),
            "gap_first": _iso(days[0]) if days else None,
            "gap_last": _iso(days[-1]) if days else None,
            "gaps_opened": self.gaps_opened,
            "gaps_closed": self.gaps_closed,
            "tasks_queued": [[a.isoformat(), b.isoformat()] for a, b in self.tasks_queued],
            "problem": self.problem,
        }


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def scan_dataset(
    conn: Conn,
    spec: DatasetSpec,
    *,
    today: date,
    calendar: CalendarPredicate,
    lookback_days: int | None = None,
) -> DatasetCheck:
    """Freshness check and gap scan of one dataset (read-only)."""
    check = DatasetCheck(
        dataset=spec.name,
        table=spec.table,
        frequency=spec.frequency,
        calendar=spec.calendar,
        backfill_job=spec.backfill_job,
    )
    check.expected_latest = exp = expected_latest(spec, today, calendar)
    info = probe_table(conn, spec)
    if info is None:
        check.is_stale = True
        check.problem = f"table {spec.table} does not exist"
        return check
    first, last, n = _bounds(conn, info)
    check.first_date, check.last_date, check.row_count = first, last, n
    if first is None or last is None:
        check.is_stale = True
        check.problem = "no rows"
        return check
    check.is_stale = last < exp
    if spec.frequency == "event" or spec.min_rows_per_day == 0:
        return check

    start = max(first, spec.history_start) if spec.history_start else first
    if lookback_days is None and spec.frequency == "intraday":
        lookback_days = INTRADAY_SCAN_DAYS
    if lookback_days is not None:
        start = max(start, today - timedelta(days=lookback_days))
    if spec.frequency == "monthly":
        start = month_start(start)
    if start > exp:
        return check
    check.scan_from, check.scan_to = start, exp
    days = expected_days(spec, start, exp, calendar)
    min_rows = spec.min_rows_per_day

    if not spec.series_column:
        counts = count_rows(conn, info, start, exp)
        check.gaps = [
            Gap(day=d, rows=counts.get((None, d), 0))
            for d in days
            if counts.get((None, d), 0) < min_rows
        ]
        return check

    col = spec.series_column
    if spec.series:
        spans = _series_bounds(conn, info, series=spec.series)
        for s in spec.series:
            span = spans.get(s)
            if span is None or span[1] < exp:
                check.stale_series.append(s)
        if check.stale_series:
            check.is_stale = True
        windows = {s: (max(start, span[0]), exp) for s, span in spans.items()}
        counts = count_rows(conn, info, start, exp, by_series=True, series=list(spec.series))
    else:
        spans = _series_bounds(conn, info, window=(start, exp))
        windows = {s: (max(start, span[0]), min(exp, span[1])) for s, span in spans.items()}
        counts = count_rows(conn, info, start, exp, by_series=True)
    gaps: list[Gap] = []
    for s in sorted(windows):
        lo, hi = windows[s]
        if spec.frequency == "monthly":
            lo = month_start(lo)
        for d in days:
            if lo <= d <= hi and counts.get((s, d), 0) < min_rows:
                gaps.append(Gap(day=d, key=((col, s),), rows=counts.get((s, d), 0)))
    check.gaps = sorted(gaps, key=lambda g: (g.day, g.key))
    return check


# ----------------------------------------------------------------------- bookkeeping


@dataclass(frozen=True, slots=True)
class GapSync:
    opened: int
    closed: int


def sync_gaps(conn: Conn, spec: DatasetSpec, check: DatasetCheck, *, run_id: int | None) -> GapSync:
    """Write the scan result to ``ops.data_gap`` (only within the scanned window)."""
    if not check.scanned:
        return GapSync(0, 0)
    assert check.scan_from is not None
    assert check.scan_to is not None
    missing = {g.ident: g for g in check.gaps}
    with conn.cursor(row_factory=tuple_row) as cur:
        existing = cur.execute(
            """
            SELECT key, gap_date, status FROM ops.data_gap
            WHERE dataset = %s AND gap_date BETWEEN %s AND %s
            """,
            (spec.name, check.scan_from, check.scan_to),
        ).fetchall()
        known = {(_key_text(k), d): str(st) for k, d, st in existing}
        opened = 0
        for ident, gap in missing.items():
            status = known.get(ident)
            if status in ("open", "unfillable"):
                continue
            note = f"rows={gap.rows} < min {spec.min_rows_per_day} (quality run {run_id})"
            cur.execute(
                """
                INSERT INTO ops.data_gap (dataset, key, gap_date, status, note)
                VALUES (%s, %s, %s, 'open', %s)
                ON CONFLICT (dataset, key, gap_date) DO UPDATE
                SET status = 'open', note = EXCLUDED.note, detected_at = now()
                WHERE ops.data_gap.status = 'backfilled'
                """,
                (spec.name, Jsonb(gap.key_dict), gap.day, note),
            )
            opened += cur.rowcount
        closed = 0
        for (key_text, day), status in known.items():
            if status == "backfilled" or (key_text, day) in missing:
                continue
            cur.execute(
                """
                UPDATE ops.data_gap SET status = 'backfilled',
                       note = %s
                WHERE dataset = %s AND key = %s::jsonb AND gap_date = %s
                """,
                (f"data present (quality run {run_id}, was {status})", spec.name, key_text, day),
            )
            closed += cur.rowcount
    return GapSync(opened, closed)


def _task_ranges(conn: Conn, datasets: Sequence[str]) -> list[tuple[date, date]]:
    """Ranges of open (or recently failed) tasks of any of ``datasets`` - the datasets that
    share one backfill job, so one task per (job, range) serves all of them."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT params ->> 'date_from', params ->> 'date_to' FROM ops.backfill_task
            WHERE dataset = ANY(%s) AND params ? 'date_from' AND params ? 'date_to'
              AND (status IN ('pending', 'running')
                   OR (status = 'failed'
                       AND done_at > now() - make_interval(days => %s)))
            """,
            (list(datasets), FAILED_TASK_COOLDOWN_DAYS),
        ).fetchall()
    out: list[tuple[date, date]] = []
    for lo, hi in rows:
        try:
            out.append((date.fromisoformat(str(lo)), date.fromisoformat(str(hi))))
        except ValueError:
            continue
    return out


def _insert_task(
    conn: Conn, dataset: str, date_from: date, date_to: date, source: str
) -> int | None:
    params = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            INSERT INTO ops.backfill_task (dataset, params, source) VALUES (%s, %s, %s)
            ON CONFLICT (dataset, params) WHERE status IN ('pending', 'running') DO NOTHING
            RETURNING id
            """,
            (dataset, Jsonb(params), source),
        ).fetchone()
    return int(row[0]) if row else None


def queue_backfill(
    conn: Conn, spec: DatasetSpec, calendar: CalendarPredicate
) -> list[tuple[date, date]]:
    """Queue backfill tasks for the dataset's open gaps; returns the ranges inserted."""
    if not spec.backfill_job:
        return []
    with conn.cursor(row_factory=tuple_row) as cur:
        days = [
            r[0]
            for r in cur.execute(
                """
                SELECT DISTINCT gap_date FROM ops.data_gap
                WHERE dataset = %s AND status = 'open' ORDER BY 1
                """,
                (spec.name,),
            ).fetchall()
        ]
    covered = _task_ranges(conn, job_datasets(spec))
    todo = [d for d in days if not any(lo <= d <= hi for lo, hi in covered)]
    inserted: list[tuple[date, date]] = []
    for lo, hi in compress_ranges(todo, day_predicate(spec, calendar), max_task_days(spec)):
        if _insert_task(conn, spec.name, lo, hi, "quality") is not None:
            inserted.append((lo, hi))
    return inserted


def queue_range(
    conn: Conn, spec: DatasetSpec, date_from: date, date_to: date, *, source: str = "cli"
) -> list[tuple[int | None, date, date]]:
    """Queue ``[date_from, date_to]`` (split into task-sized chunks); ``None`` id = an
    identical open task already exists."""
    return [
        (_insert_task(conn, spec.name, lo, hi, source), lo, hi)
        for lo, hi in split_range(date_from, date_to, max_task_days(spec))
    ]


def reconcile_gaps(
    conn: Conn,
    spec: DatasetSpec,
    date_from: date,
    date_to: date,
    *,
    run_id: int | None,
    mark_unfillable: bool,
    today: date | None = None,
) -> tuple[int, int]:
    """After a backfill run over ``[date_from, date_to]``: re-count rows for every open gap
    in the range. Filled gaps -> ``backfilled``; still-missing gaps -> ``unfillable`` when
    ``mark_unfillable`` (the run succeeded, so the source has no data for that day, e.g. a
    holiday missing from the calendar) and the day is older than the no-data grace period
    (:data:`mdhub.core.gaps.NO_DATA_GRACE_DAYS`), otherwise they stay open with a note.

    :returns: ``(backfilled, unfillable)`` counts.
    """
    today = today or msk_today()
    with conn.cursor(row_factory=tuple_row) as cur:
        gaps = cur.execute(
            """
            SELECT key, gap_date FROM ops.data_gap
            WHERE dataset = %s AND status = 'open' AND gap_date BETWEEN %s AND %s
            """,
            (spec.name, date_from, date_to),
        ).fetchall()
    if not gaps:
        return 0, 0
    info = probe_table(conn, spec)
    by_series = bool(spec.series_column)
    counts = (
        count_rows(conn, info, date_from, date_to, by_series=by_series) if info is not None else {}
    )
    filled = unfillable = 0
    with conn.cursor(row_factory=tuple_row) as cur:
        for key, day in gaps:
            n = counts.get((gap_series(spec, key), day), 0)
            if n >= spec.min_rows_per_day:
                status, note = "backfilled", f"loaded by run {run_id} (rows={n})"
                filled += 1
            elif mark_unfillable and no_data_status(day, today) == "unfillable":
                status = "unfillable"
                note = (
                    f"no data from source after successful backfill run {run_id} (rows={n}); "
                    "likely a non-business day missing from the calendar"
                )
                unfillable += 1
            else:
                status, note = "open", f"still missing after run {run_id} (rows={n})"
            cur.execute(
                """
                UPDATE ops.data_gap SET status = %s, note = %s
                WHERE dataset = %s AND key = %s AND gap_date = %s
                """,
                (status, note, spec.name, Jsonb(key), day),
            )
    return filled, unfillable
