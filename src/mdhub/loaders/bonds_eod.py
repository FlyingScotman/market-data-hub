"""MOEX bonds end-of-day history, all boards -> ``md.bond_eod`` + ``md.bond_eod_yield``.

Per trade date (one transaction each, fail closed per date):

1. ``history/engines/stock/markets/bonds/securities.json?date=D`` paged by ``history.cursor``
   (TOTAL verified), every row must carry TRADEDATE = D; key (trade_date, secid, board) - one
   row per board (the legacy loader dropped multi-board rows).
2. ``history/engines/stock/markets/bonds/yields.json?date=D`` paged by ``start=``; its row count
   must equal the history TOTAL and its keys must be a subset of the history keys.
3. Both upserted idempotently; changed values (post-session revisions) land in
   ``ops.row_history``.

A business day with zero ISS rows is a recorded "no data" outcome (warning,
:func:`mdhub.core.gaps.record_no_data` in ``final`` mode / backfill: ``unfillable`` once the
day is older than the grace period, else ``open``), not an error. ``prelim`` (19:20, same day)
just notes it - history may not be published yet.

Parameters: ``mode`` (``prelim``/``final``), ``days_back`` (business days of MOEX_STOCK before
today, default ``[1]``), ``day`` (one date), ``date_from``/``date_to`` (inclusive backfill
range; every MOEX_STOCK business day).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, IssPagingError, iter_pages
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.sources.moex_market import (
    BOND_HISTORY_REQUIRED,
    BONDS_HISTORY_PATH,
    PARSER_VERSION,
    business_days_between,
    iter_bond_yield_pages,
    parse_bond_history,
    parse_bond_yields,
)

__all__ = [
    "KEY",
    "close_gap",
    "load_bonds_eod_day",
    "record_no_data",
    "resolve_trade_dates",
    "run_dates",
]

CALENDAR: Final = "MOEX_STOCK"
KEY: Final = ["trade_date", "secid", "board"]
SOURCE: Final = "moex_iss"
BACKFILL_TASK_DAYS: Final = 5
"""~70 ISS pages per trade date at 3 rps: a 5-day task takes ~2 minutes, well within the
backfill worker budget, so a backfill never holds the job lock across a scheduled run for long."""

register_dataset(
    DatasetSpec(
        name="bonds_eod",
        table="md.bond_eod",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="bonds_eod",
        history_start=date(2024, 1, 3),
        min_rows_per_day=1000,
        max_task_days=BACKFILL_TASK_DAYS,
        description="MOEX bonds EOD history, all boards of market 'bonds' (ISS history by date)",
    )
)
register_dataset(
    DatasetSpec(
        name="bonds_eod_yield",
        table="md.bond_eod_yield",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="bonds_eod",
        history_start=date(2024, 1, 3),
        min_rows_per_day=500,
        max_task_days=BACKFILL_TASK_DAYS,
        description="MOEX bonds EOD yields/duration/G- and Z-spreads (ISS history yields by date)",
    )
)


# ------------------------------------------------------------------- shared helpers


def resolve_trade_dates(
    ctx: JobContext,
    *,
    days_back: Sequence[int] | None,
    default_days_back: Sequence[int],
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    calendar: str = CALENDAR,
) -> list[date]:
    """Dates to load: explicit ``day``; else the business days of ``date_from..date_to``
    (``date_from`` alone: up to today; ``date_to`` alone: that day); else ``days_back``
    offsets (0 = today, n = n-th business day before today)."""
    if day is not None:
        return [day]
    if date_from is not None or date_to is not None:
        start = date_from or date_to
        end = date_to or ctx.today()
        assert start is not None
        return business_days_between(start, end, lambda d: ctx.is_business_day(d, calendar))
    offsets = list(days_back) if days_back is not None else list(default_days_back)
    return ctx.business_dates(offsets, calendar=calendar)


def run_dates(
    ctx: JobContext, dates: Sequence[date], load: Callable[[date], Any]
) -> dict[str, Any]:
    """Call ``load(day) -> summary`` for every date; a failing date does not stop the others.

    The run ends ``partial`` if some dates failed; if all failed the last error is raised.
    """
    results: dict[str, Any] = {}
    errors: list[tuple[date, Exception]] = []
    for d in dates:
        try:
            results[d.isoformat()] = load(d)
        except Exception as exc:
            ctx.logger.exception("%s: %s failed", ctx.job, d)
            errors.append((d, exc))
            results[d.isoformat()] = {"error": f"{type(exc).__name__}: {exc}"}
    if errors:
        if len(errors) == len(dates):
            raise errors[-1][1]
        for d, err in errors:
            ctx.run.mark_partial(f"{d}: {type(err).__name__}: {err}")
    return results


# ---------------------------------------------------------------------------- loader


def _with_meta(rows: list[dict[str, Any]], capture_id: int, run_id: int) -> list[dict[str, Any]]:
    for row in rows:
        row["capture_id"] = capture_id
        row["run_id"] = run_id
    return rows


def _unique_keys(rows: Sequence[dict[str, Any]], what: str) -> set[tuple[str, str]]:
    keys = {(r["secid"], r["board"]) for r in rows}
    if len(keys) != len(rows):
        raise IssFormatError(f"{what}: duplicate (secid, board) across pages")
    return keys


def load_bonds_eod_day(
    ctx: JobContext, http: Any, trade_date: date, *, mode: str = "final"
) -> dict[str, Any]:
    """Load one trade date (one transaction). Returns a small summary."""
    with ctx.connection() as conn:
        parsed: list[dict[str, Any]] = []
        total = 0
        for page in iter_pages(
            http,
            BONDS_HISTORY_PATH,
            {"date": trade_date.isoformat()},
            required=BOND_HISTORY_REQUIRED,
        ):
            cap = record_capture(
                conn, ctx.run.id, SOURCE, "bonds_history", None, page.response, PARSER_VERSION
            )
            assert page.cursor is not None
            total = page.cursor.total
            parsed.extend(_with_meta(parse_bond_history(page.rows, trade_date), cap, ctx.run.id))
        history_keys = _unique_keys(parsed, f"bonds history {trade_date}")

        if not parsed:
            note = f"ISS bonds history returned 0 rows for {trade_date} (mode={mode})"
            ctx.logger.warning(note)
            if mode != "prelim":
                today = ctx.today()
                record_no_data(conn, "bonds_eod", trade_date, note, today=today)
                record_no_data(conn, "bonds_eod_yield", trade_date, note, today=today)
            return {"history": 0, "yields": 0, "no_data": True}

        yields: list[dict[str, Any]] = []
        received = 0
        for page in iter_bond_yield_pages(http, trade_date):
            cap = record_capture(
                conn,
                ctx.run.id,
                SOURCE,
                "bonds_history_yields",
                None,
                page.response,
                PARSER_VERSION,
            )
            received += len(page.rows)
            yields.extend(_with_meta(parse_bond_yields(page.rows, trade_date), cap, ctx.run.id))
        if received != total:
            raise IssPagingError(
                f"bonds yields {trade_date}: {received} rows, history TOTAL {total}"
            )
        extra = _unique_keys(yields, f"bonds yields {trade_date}") - history_keys
        if extra:
            raise IssPagingError(
                f"bonds yields {trade_date}: keys not in history: {sorted(extra)[:5]}"
            )

        res_h = upsert_rows(conn, "md.bond_eod", parsed, KEY, run_id=ctx.run.id)
        res_y = upsert_rows(conn, "md.bond_eod_yield", yields, KEY, run_id=ctx.run.id)
        ctx.run.add_counts(res_h)
        ctx.run.add_counts(res_y)
        close_gap(conn, "bonds_eod", trade_date)
        if yields:
            close_gap(conn, "bonds_eod_yield", trade_date)
    ctx.logger.info(
        "bonds_eod %s: %d history rows (%d new, %d changed), %d yield rows",
        trade_date,
        len(parsed),
        res_h.inserted,
        res_h.updated,
        len(yields),
    )
    return {
        "history": len(parsed),
        "yields": len(yields),
        "inserted": res_h.inserted + res_y.inserted,
        "updated": res_h.updated + res_y.updated,
    }


@job("bonds_eod", "MOEX bonds EOD history + yields by trade date, all boards")
def bonds_eod(
    ctx: JobContext,
    *,
    mode: str = "final",
    days_back: list[int] | None = None,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    if mode not in ("prelim", "final"):
        raise ValueError(f"bonds_eod: unknown mode {mode!r}")
    dates = resolve_trade_dates(
        ctx,
        days_back=days_back,
        default_days_back=[1],
        day=day,
        date_from=date_from,
        date_to=date_to,
    )
    if not dates:
        ctx.run.mark_skipped("no business dates to load")
        return {}
    with ctx.http(base_url=ISS_BASE_URL) as http:
        return run_dates(ctx, dates, lambda d: load_bonds_eod_day(ctx, http, d, mode=mode))
