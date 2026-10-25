"""``mdhub report seed``: read-only Markdown integrity report of the database (seed check).

``mdhub report seed --out docs/SEED_REPORT.md [--as-of YYYY-MM-DD] [--snapshot ID]`` reads
``MDHUB_DATABASE_URL`` in one ``REPEATABLE READ READ ONLY`` transaction (never writes) and
renders:

* row counts and on-disk size per schema / table (``md``, ``ref``, ``legacy_md``, ``legacy_bc``,
  ``ops``; exact ``count(*)``, ``pg_total_relation_size``);
* per registered dataset (:func:`mdhub.datasets.all_datasets` after
  :func:`mdhub.jobs.discover_jobs`): first/last date, distinct days, expected days in
  ``[history_start or first date, expected latest]`` (business days of the dataset calendar from
  ``ref.calendar_effective`` - same source precedence as :class:`mdhub.core.bizday.DbCalendar`,
  Monday-Friday for days without rows; every day for ``daily``; month starts for ``monthly``;
  none for ``event``), missing days (no row) with the first dates, days below
  ``min_rows_per_day``, days with rows that the calendar does not expect; per series for
  datasets with ``series_column`` (window from the series' own first date);
* ``ops.data_gap`` by dataset/status, ``ops.backfill_task`` by status/dataset, the last
  ``ops.job_run`` per job;
* bond static coverage (``ref.bond``, coupons, offers, coupon types, coupon statuses, floaters
  with a resolved formula);
* legacy import verification (``ops.legacy_import``).

Ordering is deterministic; the only volatile line is the generation timestamp. ``--snapshot``
reads a snapshot exported by another session (``pg_export_snapshot()``) so the report matches a
``pg_dump --snapshot`` of the same moment exactly (``deploy/make_bundle.sh``).
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, Final

import psycopg
import typer
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

from mdhub.config import ConfigError, Settings
from mdhub.core.bizday import DEFAULT_CALENDAR, CalendarPredicate, calendar_chain
from mdhub.core.timeutil import is_weekday, msk_today
from mdhub.datasets import DatasetSpec, all_datasets
from mdhub.jobs import discover_jobs
from mdhub.quality.checks import (
    TableInfo,
    count_rows,
    expected_days,
    expected_latest,
    month_start,
    probe_table,
)

__all__ = [
    "REPORT_SCHEMAS",
    "DatasetCoverage",
    "EffectiveCalendar",
    "SeriesCoverage",
    "build_seed_report",
    "dataset_coverage",
]

name = "report"
app = typer.Typer(help="Read-only database reports.", no_args_is_help=True)

REPORT_SCHEMAS: Final = ("md", "ref", "legacy_md", "legacy_bc", "ops")
"""Schemas listed in the table section, in this order."""

MISSING_LIST: Final = 10
"""Missing / thin dates listed per dataset or series."""

_SNAPSHOT_RE: Final = re.compile(r"^[0-9A-Fa-f]+-[0-9A-Fa-f]+(-[0-9]+)?$")

Conn = psycopg.Connection[Any]


# ----------------------------------------------------------------------------- calendar


class EffectiveCalendar(CalendarPredicate):
    """``ref.calendar_effective`` loaded once (the precedence of :class:`DbCalendar`);
    days without a row fall back to the base calendar of a derived one
    (:data:`~mdhub.core.bizday.CALENDAR_FALLBACK`), then to Monday-Friday."""

    def __init__(self, days: dict[tuple[str, date], bool]) -> None:
        self._days = days

    @classmethod
    def load(cls, conn: Conn) -> EffectiveCalendar:
        if not _exists(conn, "ref.calendar_effective"):
            return cls({})
        with conn.cursor(row_factory=tuple_row) as cur:
            rows = cur.execute(
                "SELECT calendar, day, is_business_day FROM ref.calendar_effective"
            ).fetchall()
        return cls({(str(c), d): bool(b) for c, d, b in rows if b is not None})

    def __call__(self, day: date, calendar: str = DEFAULT_CALENDAR) -> bool:
        for cal in calendar_chain(calendar):
            value = self._days.get((cal, day))
            if value is not None:
                return value
        return is_weekday(day)


# ----------------------------------------------------------------------------- datasets


@dataclass(slots=True)
class SeriesCoverage:
    """Coverage of one dataset (``series is None``) or one series of it."""

    series: str | None
    rows: int
    first_date: date | None
    last_date: date | None
    distinct_days: int
    window: tuple[date, date] | None = None
    expected: int | None = None
    missing: list[date] = field(default_factory=list)
    thin: list[date] = field(default_factory=list)
    off_calendar: int = 0


@dataclass(slots=True)
class DatasetCoverage:
    spec: DatasetSpec
    expected_latest: date | None = None
    total: SeriesCoverage | None = None
    series: list[SeriesCoverage] = field(default_factory=list)
    problem: str | None = None

    @property
    def is_stale(self) -> bool:
        if self.total is None or self.expected_latest is None:
            return True
        last = self.total.last_date
        return last is None or last < self.expected_latest

    @property
    def scanned(self) -> bool:
        return self.spec.frequency != "event" and self.spec.min_rows_per_day > 0


def _bounds(conn: Conn, info: TableInfo) -> tuple[date | None, date | None, int]:
    q = sql.SQL("SELECT min({c}), max({c}), count(*) FROM {t} WHERE true{w}").format(
        c=info.col, t=info.table, w=info.where_extra()
    )
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(q, []).fetchone()  # params given: '%%' in `where` is unescaped
    assert row is not None
    return info.to_day(row[0]), info.to_day(row[1]), int(row[2])


def _summarize(
    spec: DatasetSpec,
    series: str | None,
    by_day: dict[date, int],
    *,
    start: date | None,
    end: date | None,
    calendar: CalendarPredicate,
    thin_check: bool,
) -> SeriesCoverage:
    days = sorted(d for d, n in by_day.items() if n > 0)
    cov = SeriesCoverage(
        series=series,
        rows=sum(by_day.values()),
        first_date=days[0] if days else None,
        last_date=days[-1] if days else None,
        distinct_days=len(days),
    )
    scanned = spec.frequency != "event" and spec.min_rows_per_day > 0
    if not scanned or start is None or end is None:
        return cov
    if spec.frequency == "monthly":
        start = month_start(start)
    if start > end:
        return cov
    expected = expected_days(spec, start, end, calendar)
    expected_set = set(expected)
    cov.window = (start, end)
    cov.expected = len(expected)
    cov.missing = [d for d in expected if by_day.get(d, 0) == 0]
    if thin_check:
        cov.thin = [d for d in expected if 0 < by_day.get(d, 0) < spec.min_rows_per_day]
    cov.off_calendar = sum(1 for d in days if start <= d <= end and d not in expected_set)
    return cov


def dataset_coverage(
    conn: Conn,
    spec: DatasetSpec,
    *,
    today: date,
    calendar: CalendarPredicate,
    settings: Settings | None = None,
) -> DatasetCoverage:
    """Coverage of ``spec`` (read-only; raises on SQL errors - callers use a savepoint).

    A rolling-window dataset (``DatasetSpec.rolling_window_setting``, e.g. AlgoPack) is only
    expected from the window start on: older days are deleted by retention, not missing."""
    window_start = spec.window_start(today, settings or Settings())
    cov = DatasetCoverage(spec=spec)
    cov.expected_latest = exp = expected_latest(spec, today, calendar)
    try:
        info = probe_table(conn, spec)
    except ValueError as exc:
        cov.problem = str(exc)
        return cov
    if info is None:
        cov.problem = f"table {spec.table} does not exist"
        return cov
    first, last, _rows = _bounds(conn, info)
    lo = first or spec.history_start or exp
    hi = max(last, exp) if last is not None else exp
    lo = min(lo, hi)
    counts = count_rows(conn, info, lo, hi) if first is not None else {}
    total_by_day = {d: n for (_s, d), n in counts.items()}
    total_start = spec.history_start or first
    if window_start is not None:
        total_start = max(total_start or window_start, window_start)
    cov.total = _summarize(
        spec,
        None,
        total_by_day,
        start=total_start,
        end=exp,
        calendar=calendar,
        thin_check=spec.series_column is None,
    )
    if first is None:
        cov.problem = "no rows"
    if not spec.series_column:
        return cov

    listed = list(spec.series) if spec.series else None
    per_series: dict[str, dict[date, int]] = {}
    if first is not None:
        for (s, d), n in count_rows(conn, info, lo, hi, by_series=True, series=listed).items():
            if s is not None:
                per_series.setdefault(s, {})[d] = n
    names = listed if listed is not None else sorted(per_series)
    for s in names:
        by_day = per_series.get(s, {})
        s_days = sorted(d for d, n in by_day.items() if n > 0)
        cov.series.append(
            _summarize(
                spec,
                s,
                by_day,
                start=s_days[0] if s_days else None,
                end=exp,
                calendar=calendar,
                thin_check=True,
            )
        )
    return cov


# ----------------------------------------------------------------------------- queries


def _exists(conn: Conn, relation: str) -> bool:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,)).fetchone()
    return bool(row and row[0])


def _dicts(conn: Conn, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return list(cur.execute(query, params).fetchall())


def _one(conn: Conn, query: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    rows = _dicts(conn, query, params)
    return rows[0] if rows else {}


@dataclass(frozen=True, slots=True)
class TableStat:
    schema: str
    table: str
    rows: int
    size_bytes: int


def table_stats(conn: Conn, schemas: Sequence[str] = REPORT_SCHEMAS) -> list[TableStat]:
    """Exact row count and total size of every table in ``schemas``."""
    rels = _dicts(
        conn,
        """
        SELECT n.nspname AS schema, c.relname AS table, pg_total_relation_size(c.oid) AS size
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition AND n.nspname = ANY(%s)
        ORDER BY n.nspname, c.relname
        """,
        (list(schemas),),
    )
    order = {s: i for i, s in enumerate(schemas)}
    out: list[TableStat] = []
    with conn.cursor(row_factory=tuple_row) as cur:
        for r in rels:
            q = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(r["schema"], r["table"]))
            row = cur.execute(q).fetchone()
            assert row is not None
            out.append(TableStat(r["schema"], r["table"], int(row[0]), int(r["size"])))
    return sorted(out, key=lambda t: (order.get(t.schema, 99), t.schema, t.table))


# ----------------------------------------------------------------------------- formatting


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return str(value.strftime("%Y-%m-%d %H:%M:%S"))
    if isinstance(value, date):
        return str(value.isoformat())
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def _size(n: int | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _md_table(
    headers: Sequence[str], rows: Iterable[Sequence[Any]], *, right: int = 0
) -> list[str]:
    """Markdown table; the last ``right`` columns are right-aligned."""
    align = ["---"] * (len(headers) - right) + ["--:"] * right
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    body = ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return [*out, *body] if body else [*out, "| " + " | ".join(["-"] * len(headers)) + " |"]


def _dates(days: Sequence[date], limit: int = MISSING_LIST) -> str:
    text = ", ".join(d.isoformat() for d in days[:limit])
    return text + (f", ... (+{len(days) - limit})" if len(days) > limit else "")


def _window(w: tuple[date, date] | None) -> str:
    return f"{w[0].isoformat()}..{w[1].isoformat()}" if w else "-"


def _first_line(text: Any, limit: int = 100) -> str | None:
    if text is None:
        return None
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


# ----------------------------------------------------------------------------- sections


def _section_header(
    conn: Conn, *, generated_at: datetime, today: date, snapshot: str | None
) -> list[str]:
    info = _one(
        conn,
        """
        SELECT current_database() AS db, current_setting('server_version') AS server_version,
               version() AS version, pg_database_size(current_database()) AS db_size
        """,
    )
    migrations: list[dict[str, Any]] = []
    if _exists(conn, "ops.schema_migration"):
        migrations = _dicts(conn, "SELECT version FROM ops.schema_migration ORDER BY version")
    lines = [
        "# market-data-hub seed report",
        "",
        f"- generated at: {generated_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"- as of (business date, MSK): {today.isoformat()}",
        f"- database: `{info.get('db')}` ({_size(info.get('db_size'))})",
        f"- server: PostgreSQL {info.get('server_version')} (`{_cell(info.get('version'))}`)",
        f"- migrations applied: {len(migrations)}"
        + (f" (latest `{migrations[-1]['version']}`)" if migrations else ""),
    ]
    if snapshot:
        lines.append(f"- snapshot: `{snapshot}` (same snapshot as the accompanying pg_dump)")
    lines += [
        "- read-only: one `REPEATABLE READ READ ONLY` transaction; exact `count(*)` row counts",
        "",
    ]
    return lines


def _section_summary(
    datasets: Sequence[DatasetCoverage],
    gaps: Sequence[dict[str, Any]],
    runs: Sequence[dict[str, Any]],
) -> list[str]:
    stale = [c.spec.name for c in datasets if c.is_stale]
    missing = [
        f"{c.spec.name} ({len(c.total.missing)})"
        for c in datasets
        if c.total is not None and c.total.missing
    ]
    series_missing = [
        f"{c.spec.name}:{s.series} ({len(s.missing)})"
        for c in datasets
        for s in c.series
        if s.missing
    ]
    thin = [f"{c.spec.name} ({len(c.total.thin)})" for c in datasets if c.total and c.total.thin]
    thin += [
        f"{c.spec.name}:{s.series} ({len(s.thin)})" for c in datasets for s in c.series if s.thin
    ]
    problems = [f"{c.spec.name}: {c.problem}" for c in datasets if c.problem]
    open_gaps = sum(int(g["gaps"]) for g in gaps if g["status"] == "open")
    by_status: dict[str, int] = {}
    for g in gaps:
        by_status[str(g["status"])] = by_status.get(str(g["status"]), 0) + int(g["gaps"])
    closed = ", ".join(
        f"{st} {by_status.get(st, 0)}" for st in ("backfilled", "unfillable", "not_expected")
    )
    failed = [r["job"] for r in runs if r["status"] in ("failed", "partial")]
    return [
        "## Summary",
        "",
        f"- datasets: {len(datasets)}; stale (last date < expected latest): {len(stale)}"
        + (f" - {', '.join(stale)}" if stale else ""),
        "- datasets with missing days: " + (", ".join(missing) if missing else "none"),
        "- series with missing days: " + (", ".join(series_missing) if series_missing else "none"),
        "- days below min_rows_per_day: " + (", ".join(thin) if thin else "none"),
        "- dataset problems: " + ("; ".join(problems) if problems else "none"),
        f"- open gaps (ops.data_gap): {open_gaps} (other gap rows: {closed})",
        "- jobs whose last run failed/partial: " + (", ".join(failed) if failed else "none"),
        "",
    ]


def _section_tables(stats: Sequence[TableStat]) -> list[str]:
    lines = ["## Tables", ""]
    per_schema: dict[str, list[TableStat]] = {}
    for t in stats:
        per_schema.setdefault(t.schema, []).append(t)
    overview = []
    for schema in REPORT_SCHEMAS:
        ts = per_schema.get(schema, [])
        overview.append(
            (schema, len(ts), sum(t.rows for t in ts), _size(sum(t.size_bytes for t in ts)))
        )
    lines += _md_table(["schema", "tables", "rows", "size"], overview, right=3)
    for schema in REPORT_SCHEMAS:
        ts = per_schema.get(schema, [])
        lines += ["", f"### {schema}", ""]
        if not ts:
            lines.append("(schema absent or empty)")
            continue
        lines += _md_table(
            ["table", "rows", "size"], [(t.table, t.rows, _size(t.size_bytes)) for t in ts], right=2
        )
    lines.append("")
    return lines


def _section_datasets(
    datasets: Sequence[DatasetCoverage], calendars: Sequence[dict[str, Any]]
) -> list[str]:
    lines = [
        "## Datasets",
        "",
        "Window = `[history_start or first date, expected latest]`; expected = business days of "
        "the dataset calendar (`ref.calendar_effective`, weekday fallback), every day for "
        "`daily`, month starts for `monthly`, none for `event`. missing = expected day without "
        "rows; below min = rows < `min_rows_per_day` (per series for series datasets); "
        "off-cal = days with rows that the calendar does not expect.",
        "",
        "Calendars (`ref.calendar_effective`):",
        "",
    ]
    lines += _md_table(
        ["calendar", "first day", "last day", "days", "business days"],
        [
            (c["calendar"], c["first_day"], c["last_day"], c["days"], c["business_days"])
            for c in calendars
        ],
        right=2,
    )
    lines.append("")
    rows = []
    for c in datasets:
        s = c.spec
        t = c.total
        rows.append(
            (
                s.name,
                s.table,
                f"{s.frequency}/{s.calendar or '-'}",
                t.first_date if t else None,
                t.last_date if t else None,
                c.expected_latest,
                "STALE" if c.is_stale else "ok",
                t.rows if t else None,
                t.distinct_days if t else None,
                _window(t.window) if t else "-",
                t.expected if t else None,
                len(t.missing) if t and t.expected is not None else None,
                (
                    f"{len(t.thin)} (<{s.min_rows_per_day})"
                    if s.series_column is None
                    else "per series"
                )
                if t and t.expected is not None
                else None,
                t.off_calendar if t and t.expected is not None else None,
            )
        )
    lines += _md_table(
        [
            "dataset",
            "table",
            "freq/calendar",
            "first",
            "last",
            "expected latest",
            "fresh",
            "rows",
            "days",
            "window",
            "expected",
            "missing",
            "below min",
            "off-cal",
        ],
        rows,
        right=4,
    )
    problems = [(c.spec.name, c.problem) for c in datasets if c.problem]
    if problems:
        lines += ["", "Problems:", ""]
        lines += [f"- `{n}`: {p}" for n, p in problems]

    series_rows = []
    for c in datasets:
        for sc in c.series:
            series_rows.append(
                (
                    c.spec.name,
                    sc.series,
                    sc.first_date,
                    sc.last_date,
                    "STALE"
                    if c.expected_latest
                    and (sc.last_date is None or sc.last_date < c.expected_latest)
                    else "ok",
                    sc.rows,
                    sc.distinct_days,
                    _window(sc.window),
                    sc.expected,
                    len(sc.missing) if sc.expected is not None else None,
                    f"{len(sc.thin)} (<{c.spec.min_rows_per_day})"
                    if sc.expected is not None
                    else None,
                    sc.off_calendar if sc.expected is not None else None,
                )
            )
    if series_rows:
        lines += [
            "",
            "### Series",
            "",
            "Series windows start at the series' own first date.",
            "",
        ]
        lines += _md_table(
            [
                "dataset",
                "series",
                "first",
                "last",
                "fresh",
                "rows",
                "days",
                "window",
                "expected",
                "missing",
                "below min",
                "off-cal",
            ],
            series_rows,
            right=4,
        )

    detail: list[str] = []
    for c in datasets:
        parts: list[tuple[str, SeriesCoverage]] = []
        if c.total is not None:
            parts.append((c.spec.name, c.total))
        parts += [(f"{c.spec.name} / {sc.series}", sc) for sc in c.series]
        for label, sc in parts:
            if sc.missing:
                detail.append(f"- `{label}` missing {len(sc.missing)}: {_dates(sc.missing)}")
            if sc.thin:
                detail.append(
                    f"- `{label}` below {c.spec.min_rows_per_day} rows {len(sc.thin)}: "
                    f"{_dates(sc.thin)}"
                )
    lines += ["", f"### Missing and thin days (first {MISSING_LIST})", ""]
    lines += detail or ["none"]
    lines.append("")
    return lines


def _section_ops(
    gaps: Sequence[dict[str, Any]], tasks: Sequence[dict[str, Any]], runs: Sequence[dict[str, Any]]
) -> list[str]:
    lines = [
        "## Operations",
        "",
        "### ops.data_gap by dataset and status",
        "",
        "Status: `open` = missing, queued for backfill; `backfilled` = data present now; "
        "`unfillable` = the source confirmed no data after the grace period (one business "
        "day); `not_expected` = the calendar no longer expects the day and there is no data "
        "(e.g. a holiday added to the calendar).",
        "",
    ]
    lines += _md_table(
        ["dataset", "status", "gaps", "days", "first", "last"],
        [(g["dataset"], g["status"], g["gaps"], g["days"], g["first"], g["last"]) for g in gaps],
    )
    lines += ["", "### ops.backfill_task by status", ""]
    lines += _md_table(
        ["status", "dataset", "tasks", "attempts", "first created", "last created"],
        [
            (
                t["status"],
                t["dataset"],
                t["tasks"],
                t["attempts"],
                t["first_created"],
                t["last_created"],
            )
            for t in tasks
        ],
    )
    lines += ["", "### Last ops.job_run per job (UTC)", ""]
    lines += _md_table(
        [
            "job",
            "run",
            "status",
            "started",
            "finished",
            "inserted",
            "updated",
            "runs",
            "failed runs",
            "last succeeded",
            "error",
        ],
        [
            (
                r["job"],
                r["id"],
                r["status"],
                r["started_at"],
                r["finished_at"],
                r["rows_inserted"],
                r["rows_updated"],
                r["runs"],
                r["failed_runs"],
                r["last_succeeded"],
                _first_line(r["error"]),
            )
            for r in runs
        ],
    )
    lines.append("")
    return lines


def _section_bonds(conn: Conn, today: date) -> list[str]:
    lines = ["## Bond static coverage", ""]
    if not _exists(conn, "ref.bond"):
        return [*lines, "(ref.bond does not exist)", ""]
    totals = _one(
        conn,
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status = 'active') AS active,
               count(*) FILTER (WHERE status = 'matured') AS matured,
               count(*) FILTER (WHERE status = 'delisted') AS delisted,
               count(*) FILTER (WHERE has_cpn) AS with_coupons,
               count(*) FILTER (WHERE has_cpn AND status = 'active') AS active_with_coupons,
               count(*) FILTER (WHERE has_offer) AS with_offers,
               count(*) FILTER (WHERE has_offer AND status = 'active') AS active_with_offers,
               count(*) FILTER (WHERE has_amort) AS with_amort,
               count(*) FILTER (WHERE has_amort AND status = 'active') AS active_with_amort
        FROM (
            SELECT b.status,
                   EXISTS (SELECT 1 FROM ref.bond_coupon c
                           WHERE c.secid = b.secid AND NOT c.is_deleted) AS has_cpn,
                   EXISTS (SELECT 1 FROM ref.bond_offer o
                           WHERE o.secid = b.secid AND NOT o.is_deleted) AS has_offer,
                   EXISTS (SELECT 1 FROM ref.bond_amortization a
                           WHERE a.secid = b.secid AND NOT a.is_deleted) AS has_amort
            FROM ref.bond b
        ) x
        """,
    )
    lines += _md_table(
        ["measure", "all bonds", "active"],
        [
            ("bonds", totals["total"], totals["active"]),
            ("with coupon schedule", totals["with_coupons"], totals["active_with_coupons"]),
            ("with offers", totals["with_offers"], totals["active_with_offers"]),
            (
                "with amortization/redemption rows",
                totals["with_amort"],
                totals["active_with_amort"],
            ),
        ],
        right=2,
    )
    lines.append("")
    lines.append(
        f"Status: active {totals['active']}, matured {totals['matured']}, "
        f"delisted {totals['delisted']}."
    )
    ctype = _dicts(
        conn,
        """
        SELECT coupon_type, count(*) AS bonds, count(*) FILTER (WHERE status = 'active') AS active
        FROM ref.bond GROUP BY coupon_type ORDER BY coupon_type
        """,
    )
    lines += ["", "### coupon_type", ""]
    lines += _md_table(
        ["coupon_type", "bonds", "active"],
        [(r["coupon_type"], r["bonds"], r["active"]) for r in ctype],
        right=2,
    )
    cstatus = _dicts(
        conn,
        """
        SELECT c.status, count(*) AS coupons, count(DISTINCT c.secid) AS bonds,
               count(*) FILTER (WHERE c.pay_date >= %s) AS future,
               count(*) FILTER (WHERE c.pay_date >= %s AND b.status = 'active') AS future_active
        FROM ref.bond_coupon c JOIN ref.bond b ON b.secid = c.secid
        WHERE NOT c.is_deleted
        GROUP BY c.status ORDER BY c.status
        """,
        (today, today),
    )
    lines += ["", f"### ref.bond_coupon status (future = pay_date >= {today.isoformat()})", ""]
    lines += _md_table(
        ["status", "coupons", "bonds", "future", "future, active bonds"],
        [(r["status"], r["coupons"], r["bonds"], r["future"], r["future_active"]) for r in cstatus],
        right=4,
    )
    floaters = _one(
        conn,
        """
        SELECT count(*) AS floaters,
               count(*) FILTER (WHERE b.status = 'active') AS active,
               count(*) FILTER (WHERE f.benchmark IS NOT NULL) AS resolved,
               count(*) FILTER (WHERE f.benchmark IS NOT NULL AND b.status = 'active')
                   AS active_resolved,
               count(*) FILTER (WHERE f.benchmark IS NOT NULL AND f.spread_bp IS NOT NULL)
                   AS with_spread
        FROM ref.bond b
        LEFT JOIN ref.bond_rate_formula_resolved f ON f.secid = b.secid
        WHERE b.coupon_type = 'floating'
        """,
    )
    by_bench = _dicts(
        conn,
        """
        SELECT COALESCE(f.benchmark, '(none)') AS benchmark, COALESCE(f.status, '-') AS status,
               count(*) AS bonds, count(*) FILTER (WHERE b.status = 'active') AS active
        FROM ref.bond b
        LEFT JOIN ref.bond_rate_formula_resolved f ON f.secid = b.secid
        WHERE b.coupon_type = 'floating'
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
    )
    lines += [
        "",
        "### Floaters (coupon_type = floating) and ref.bond_rate_formula_resolved",
        "",
        f"- floaters: {floaters['floaters']} (active {floaters['active']})",
        f"- with resolved benchmark: {floaters['resolved']} (active {floaters['active_resolved']}),"
        f" with benchmark and spread: {floaters['with_spread']}",
        "",
    ]
    lines += _md_table(
        ["benchmark", "formula status", "bonds", "active"],
        [(r["benchmark"], r["status"], r["bonds"], r["active"]) for r in by_bench],
        right=2,
    )
    lines.append("")
    return lines


def _section_legacy(conn: Conn) -> list[str]:
    lines = ["## Legacy import (ops.legacy_import)", ""]
    if not _exists(conn, "ops.legacy_import"):
        return [*lines, "(ops.legacy_import does not exist)", ""]
    rows = _dicts(
        conn,
        """
        SELECT li.id, li.schema_name, li.source_path, li.source_sha256, li.imported_at,
               (SELECT count(*) FROM jsonb_object_keys(li.table_counts)) AS tables,
               (SELECT COALESCE(sum(value::bigint), 0) FROM jsonb_each_text(li.table_counts))
                   AS rows,
               li.verified, li.verified_at,
               li.verify_report ->> 'ok' AS report_ok,
               COALESCE(jsonb_array_length(li.verify_report -> 'problems'), 0) AS problems,
               COALESCE(jsonb_array_length(li.verify_report -> 'warnings'), 0) AS warnings,
               li.id = max(li.id) OVER (PARTITION BY li.schema_name) AS latest
        FROM ops.legacy_import li
        ORDER BY li.schema_name, li.id
        """,
    )
    lines += _md_table(
        [
            "id",
            "schema",
            "source file",
            "sha256",
            "imported (UTC)",
            "tables",
            "rows",
            "verified",
            "verified at (UTC)",
            "problems",
            "warnings",
            "latest",
        ],
        [
            (
                r["id"],
                r["schema_name"],
                Path(str(r["source_path"])).name,
                str(r["source_sha256"])[:12],
                r["imported_at"],
                r["tables"],
                r["rows"],
                r["verified"],
                r["verified_at"],
                r["problems"],
                r["warnings"],
                r["latest"],
            )
            for r in rows
        ],
    )
    latest = [r for r in rows if r["latest"]]
    unverified = [r["schema_name"] for r in latest if not r["verified"]]
    lines += [
        "",
        "Latest import per schema verified: "
        + ("yes" if latest and not unverified else "no" if latest else "no imports")
        + (f" (unverified: {', '.join(unverified)})" if unverified else ""),
        "",
    ]
    return lines


# ----------------------------------------------------------------------------- report


def _gaps(conn: Conn) -> list[dict[str, Any]]:
    if not _exists(conn, "ops.data_gap"):
        return []
    return _dicts(
        conn,
        """
        SELECT dataset, status, count(*) AS gaps, count(DISTINCT gap_date) AS days,
               min(gap_date) AS first, max(gap_date) AS last
        FROM ops.data_gap GROUP BY dataset, status ORDER BY dataset, status
        """,
    )


def _tasks(conn: Conn) -> list[dict[str, Any]]:
    if not _exists(conn, "ops.backfill_task"):
        return []
    return _dicts(
        conn,
        """
        SELECT status, dataset, count(*) AS tasks, sum(attempts) AS attempts,
               min(created_at) AS first_created, max(created_at) AS last_created
        FROM ops.backfill_task GROUP BY status, dataset ORDER BY status, dataset
        """,
    )


def _runs(conn: Conn) -> list[dict[str, Any]]:
    if not _exists(conn, "ops.job_run"):
        return []
    return _dicts(
        conn,
        """
        SELECT DISTINCT ON (r.job) r.job, r.id, r.status, r.started_at, r.finished_at,
               r.rows_inserted, r.rows_updated, r.error, s.runs, s.failed_runs, s.last_succeeded
        FROM ops.job_run r
        JOIN (
            SELECT job, count(*) AS runs, count(*) FILTER (WHERE status = 'failed') AS failed_runs,
                   max(started_at) FILTER (WHERE status = 'succeeded') AS last_succeeded
            FROM ops.job_run GROUP BY job
        ) s ON s.job = r.job
        ORDER BY r.job, r.started_at DESC, r.id DESC
        """,
    )


def _calendars(conn: Conn) -> list[dict[str, Any]]:
    if not _exists(conn, "ref.calendar_effective"):
        return []
    return _dicts(
        conn,
        """
        SELECT calendar, min(day) AS first_day, max(day) AS last_day, count(*) AS days,
               count(*) FILTER (WHERE is_business_day) AS business_days
        FROM ref.calendar_effective GROUP BY calendar ORDER BY calendar
        """,
    )


def _collect_datasets(
    conn: Conn, today: date, calendar: CalendarPredicate
) -> list[DatasetCoverage]:
    out: list[DatasetCoverage] = []
    for spec in all_datasets():
        try:
            with conn.transaction():  # savepoint: one bad spec does not abort the report
                out.append(dataset_coverage(conn, spec, today=today, calendar=calendar))
        except psycopg.Error as exc:
            cov = DatasetCoverage(spec=spec, problem=f"query failed: {type(exc).__name__}")
            out.append(cov)
    return out


def build_seed_report(
    conn: Conn,
    *,
    today: date | None = None,
    generated_at: datetime | None = None,
    snapshot: str | None = None,
) -> str:
    """Render the report from ``conn`` in one read-only REPEATABLE READ transaction.

    ``conn`` must be idle (no open transaction); it is rolled back afterwards and its
    ``read_only`` / ``isolation_level`` / ``autocommit`` settings are restored.
    """
    if snapshot is not None and not _SNAPSHOT_RE.match(snapshot):
        raise ValueError(f"invalid snapshot id {snapshot!r}")
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("connection has an open transaction")
    discover_jobs()
    today = today or msk_today()
    generated_at = generated_at or datetime.now(UTC)
    saved = (conn.autocommit, conn.read_only, conn.isolation_level)
    conn.autocommit = False
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    try:
        if snapshot is not None:
            # Must be the first statement of the transaction.
            conn.execute(sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot)))
        conn.execute("SET LOCAL TimeZone = 'UTC'")
        calendar = EffectiveCalendar.load(conn)
        lines = _section_header(conn, generated_at=generated_at, today=today, snapshot=snapshot)
        datasets = _collect_datasets(conn, today, calendar)
        gaps, tasks, runs = _gaps(conn), _tasks(conn), _runs(conn)
        lines += _section_summary(datasets, gaps, runs)
        lines += _section_tables(table_stats(conn))
        lines += _section_datasets(datasets, _calendars(conn))
        lines += _section_ops(gaps, tasks, runs)
        lines += _section_bonds(conn, today)
        lines += _section_legacy(conn)
    finally:
        conn.rollback()
        conn.autocommit, conn.read_only, conn.isolation_level = saved
    return "\n".join(lines).rstrip("\n") + "\n"


# ----------------------------------------------------------------------------- CLI


@app.command("seed")
def seed_cmd(
    out: Annotated[
        str, typer.Option("--out", "-o", help="Output Markdown file ('-' = stdout).")
    ] = "-",
    as_of: Annotated[
        str | None,
        typer.Option("--as-of", help="Business date for 'expected latest' (default: today MSK)."),
    ] = None,
    snapshot: Annotated[
        str | None,
        typer.Option("--snapshot", help="Read this exported snapshot (pg_export_snapshot())."),
    ] = None,
) -> None:
    """Write the read-only integrity report of MDHUB_DATABASE_URL as Markdown."""
    try:
        today = date.fromisoformat(as_of) if as_of else None
    except ValueError:
        typer.echo(f"--as-of: expected YYYY-MM-DD, got {as_of!r}", err=True)
        raise typer.Exit(2) from None
    if snapshot is not None and not _SNAPSHOT_RE.match(snapshot):
        typer.echo(f"--snapshot: invalid snapshot id {snapshot!r}", err=True)
        raise typer.Exit(2)
    try:
        url = Settings().require_database_url()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None
    started = time.monotonic()
    try:
        with psycopg.connect(url, application_name="mdhub-report") as conn:
            text = build_seed_report(conn, today=today, snapshot=snapshot)
    except psycopg.Error as exc:
        typer.echo(f"report failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from None
    if out == "-":
        sys.stdout.write(text)
    else:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    typer.echo(
        f"report written to {out if out != '-' else 'stdout'} in {time.monotonic() - started:.1f}s",
        err=True,
    )
