"""MOEX indices intraday (RGBI, RGBITR): values, 1-minute candles and the index composition.

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`):

* ``index_intraday`` - every minute of the stock session: one marketdata request for the
  indices of ``MDHUB_INTRADAY_INDICES`` -> ``md.index_intraday`` (changed values only), then
  per index the 1-minute candles since the last stored candle of today (``from=`` datetime; the
  running candle is revised) -> ``md.index_candle``. With ``date_from``/``date_to``: candle
  backfill of those days (``from``/``till``, ``start=`` paging, 500 rows per page); a business
  day with no candles is recorded with :func:`~mdhub.core.gaps.record_no_data`.
* ``index_constituents`` - composition and weights per index of ``MDHUB_CONSTITUENT_INDICES``
  and day (``/statistics/.../analytics/{index}.json?date=``, 20 rows per page, cursor) ->
  ``md.index_constituent``; validated: rows = cursor ``TOTAL`` and weights sum ~100 %. Daily
  after 20:05 MSK for the day; ``date_from``/``date_to`` backfill (history from 2016-12-13).
  ISS answers a non-trading date with the previous composition dated by its own ``tradedate``:
  a mismatching date fails closed in the parser, so only business days are requested.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from psycopg.rows import tuple_row

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.http import HttpClient
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, iter_pages, iter_pages_until_empty
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.loaders.fx_moex import only_changed, resolve_dates
from mdhub.sources.moex_intraday import (
    INDEX_ANALYTICS,
    INDEX_CANDLES,
    INDEX_INTRADAY_COMPARE,
    INDEX_MARKETDATA,
    PARSER_VERSION,
    parse_index_candles,
    parse_index_constituents,
    parse_index_intraday,
    validate_constituents,
)

__all__ = ["index_constituents", "index_intraday"]

SOURCE: Final = "moex_iss"
CALENDAR: Final = "MOEX_STOCK"
CANDLE_INTERVAL: Final = 1
CANDLE_KEY: Final = ["secid", "interval_min", "begin_ts"]
CONSTITUENT_KEY: Final = ["index_id", "trade_date", "secid"]
CONSTITUENTS_HISTORY_START: Final = date(2016, 12, 13)
"""First date of the RGBI analytics (``analytics.dates`` ``from``)."""
MAX_BACKFILL_DAYS: Final = 400

register_dataset(
    DatasetSpec(
        name="index_intraday",
        table="md.index_intraday",
        date_column="ts",
        calendar=CALENDAR,
        frequency="intraday",
        max_lag_days=1,
        min_rows_per_day=10,
        description="RGBI/RGBITR intraday values (realtime public ISS), change-only",
    )
)
register_dataset(
    DatasetSpec(
        name="index_candle",
        table="md.index_candle",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="index_intraday",
        history_start=date(2024, 1, 3),
        min_rows_per_day=300,
        max_task_days=31,
        description="RGBI/RGBITR 1-minute candles (index_intraday; backfill by date range)",
    )
)
register_dataset(
    DatasetSpec(
        name="index_constituent",
        table="md.index_constituent",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="index_constituents",
        history_start=CONSTITUENTS_HISTORY_START,
        min_rows_per_day=10,
        max_task_days=31,
        description="RGBI composition and weights (ISS index analytics), daily",
    )
)


# ------------------------------------------------------------------------ index_intraday


@job("index_intraday", "RGBI/RGBITR values + 1-minute candles (realtime ISS); candle backfill")
def index_intraday(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    indices = ctx.settings.intraday_index_list()
    if not indices:
        raise JobParamError("MDHUB_INTRADAY_INDICES is empty")
    with ctx.http(base_url=ISS_BASE_URL) as http:
        if date_from is not None or date_to is not None:
            days = resolve_dates(
                ctx, CALENDAR, days_back=None, date_from=date_from, date_to=date_to
            )
            if len(days) > MAX_BACKFILL_DAYS:
                raise JobParamError(f"at most {MAX_BACKFILL_DAYS} business days per run")
            for d in days:
                _candles_day(ctx, http, indices, d)
            return {"days": len(days)}
        values = _poll_values(ctx, http, indices)
        candles = sum(_poll_candles(ctx, http, secid) for secid in indices)
    return {"values_changed": values, "candles": candles}


def _poll_values(ctx: JobContext, http: HttpClient, indices: list[str]) -> int:
    res = http.get_json(
        INDEX_MARKETDATA,
        params={"iss.meta": "off", "iss.only": "marketdata", "securities": ",".join(indices)},
    )
    rows = parse_index_intraday(
        res.data, indices, is_trading_day=lambda d: ctx.is_trading_day(d, CALENDAR)
    )
    with ctx.connection() as conn:
        cap = record_capture(
            conn,
            ctx.run.id,
            SOURCE,
            "intraday:index_marketdata",
            None,
            res.response,
            PARSER_VERSION,
            source_ts=max((r["source_ts"] for r in rows), default=None),
        )
        changed, unchanged = only_changed(
            conn, "md.index_intraday", rows, ["secid"], INDEX_INTRADAY_COMPARE, logger=ctx.logger
        )
        for r in changed:
            r["capture_id"] = cap
            r["run_id"] = ctx.run.id
        result = upsert_rows(conn, "md.index_intraday", changed, ["secid", "ts"], run_id=ctx.run.id)
        ctx.run.add_counts(result + UpsertResult(unchanged=unchanged))
    return result.changed


def _poll_candles(ctx: JobContext, http: HttpClient, secid: str) -> int:
    """Today's candles from the last stored one (re-read: it may have been running)."""
    today = ctx.today()
    with ctx.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        cur.execute(
            "SELECT max(begin_ts) FROM md.index_candle "
            "WHERE secid = %s AND interval_min = %s AND trade_date = %s",
            (secid, CANDLE_INTERVAL, today),
        )
        row = cur.fetchone()
    last: datetime | None = row[0] if row else None
    start = (
        last.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
        if last is not None
        else today.isoformat()
    )
    return _load_candles(
        ctx, http, secid, {"from": start, "till": today.isoformat()}, "intraday:index_candles"
    )


def _candles_day(ctx: JobContext, http: HttpClient, indices: list[str], d: date) -> None:
    empty = []
    for secid in indices:
        n = _load_candles(
            ctx,
            http,
            secid,
            {"from": d.isoformat(), "till": d.isoformat()},
            "index_candles_history",
            day=d,
        )
        if n == 0:
            empty.append(secid)
    with ctx.connection() as conn:
        if empty:
            note = f"ISS returned no 1-minute candles of {', '.join(empty)} for {d}"
            ctx.logger.warning(note)
            record_no_data(conn, "index_candle", d, note, today=ctx.today())
        else:
            close_gap(conn, "index_candle", d)


def _load_candles(
    ctx: JobContext,
    http: HttpClient,
    secid: str,
    window: dict[str, str],
    endpoint: str,
    *,
    day: date | None = None,
) -> int:
    rows: list[dict[str, Any]] = []
    with ctx.connection() as conn:
        for page in iter_pages_until_empty(
            http,
            INDEX_CANDLES.format(secid=secid),
            {"interval": CANDLE_INTERVAL, **window},
            block="candles",
        ):
            cap = record_capture(
                conn, ctx.run.id, SOURCE, endpoint, None, page.response, PARSER_VERSION
            )
            for r in parse_index_candles(page.rows, secid, CANDLE_INTERVAL):
                r["capture_id"] = cap
                r["run_id"] = ctx.run.id
                rows.append(r)
        if day is not None:
            wrong = {r["trade_date"] for r in rows} - {day}
            if wrong:
                raise IssFormatError(f"candles of {secid} for {day} returned {sorted(wrong)}")
        ctx.run.add_counts(
            upsert_rows(conn, "md.index_candle", rows, CANDLE_KEY, run_id=ctx.run.id)
        )
    return len(rows)


# -------------------------------------------------------------------- index_constituents


@job("index_constituents", "Index composition and weights (RGBI...) per day, validated")
def index_constituents(
    ctx: JobContext,
    *,
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    index: str | None = None,
) -> dict[str, Any]:
    indices = [index] if index else ctx.settings.constituent_index_list()
    if not indices:
        raise JobParamError("MDHUB_CONSTITUENT_INDICES is empty")
    days = resolve_dates(
        ctx,
        CALENDAR,
        days_back=days_back,
        date_from=date_from,
        date_to=date_to,
        default_days_back=(0,),
    )
    days = [d for d in days if d >= CONSTITUENTS_HISTORY_START]
    if len(days) > MAX_BACKFILL_DAYS:
        raise JobParamError(f"at most {MAX_BACKFILL_DAYS} business days per run")
    loaded = 0
    with ctx.http(base_url=ISS_BASE_URL) as http:
        for d in days:
            empty = []
            for idx in indices:
                n = _constituents_day(ctx, http, idx, d)
                loaded += n
                if n == 0:
                    empty.append(idx)
            with ctx.connection() as conn:
                if empty:
                    note = f"ISS index analytics of {', '.join(empty)} empty for {d}"
                    ctx.logger.warning(note)
                    record_no_data(conn, "index_constituent", d, note, today=ctx.today())
                else:
                    close_gap(conn, "index_constituent", d)
    return {"days": len(days), "rows": loaded}


def _constituents_day(ctx: JobContext, http: HttpClient, index_id: str, d: date) -> int:
    what = f"index analytics {index_id} {d}"
    rows: list[dict[str, Any]] = []
    total = 0
    with ctx.connection() as conn:
        for page in iter_pages(
            http, INDEX_ANALYTICS.format(index=index_id), {"date": d.isoformat()}, block="analytics"
        ):
            cap = record_capture(
                conn, ctx.run.id, SOURCE, "index_analytics", None, page.response, PARSER_VERSION
            )
            assert page.cursor is not None  # iter_pages requires the cursor
            total = page.cursor.total
            for r in parse_index_constituents(page.rows, index_id, d):
                r["capture_id"] = cap
                r["run_id"] = ctx.run.id
                rows.append(r)
        validate_constituents(rows, total, what)
        ctx.run.add_counts(
            upsert_rows(conn, "md.index_constituent", rows, CONSTITUENT_KEY, run_id=ctx.run.id)
        )
    return len(rows)
