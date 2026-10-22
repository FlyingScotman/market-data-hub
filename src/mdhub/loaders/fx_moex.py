"""MOEX currency market loaders: CETS EOD/intraday, indicative rates, currency fixings.

Jobs (names fixed by :data:`mdhub.schedule.KNOWN_JOBS`):

* ``fx_moex_eod`` - ``md.fx_eod`` from ISS selt history (all boards: CETS, CNGD, LICU,
  FIXN/FIXS fixing auctions, WAPN/WAPS, SDBP), one transaction per date; refreshes
  ``ref.fx_instrument`` from the full selt listing first. Both listings (this one and the
  intraday CETS board) also store the session's SETTLEDATE per instrument in
  ``md.fx_settle_date`` (contract 1.5, H2; session = ``dataversion.trade_date``): the history
  carries no value date, the listing only the current session's.
* ``fx_moex_intraday`` - ``md.fx_intraday`` from the CETS board snapshot (public ISS =
  15-min delayed); only rows whose values changed since the last stored row are written.
* ``fx_indicative`` - ``md.fx_indicative`` (MOEX indicative rates tc 14:00 / mc 19:00, by
  date, history since 2013) and ``md.fx_fixing`` (currency-market fixings, e.g.
  ``SRATE_CNY_ON``, by date range).

Missing data is never invented: a business date for which ISS returns no rows is recorded with
:func:`mdhub.core.gaps.record_no_data` (``open`` within the grace period, then ``unfillable``),
not as a partial run (``prelim`` runs only log it).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.iss import ISS_BASE_URL, IssFormatError, iter_pages, parse_block
from mdhub.core.timeutil import date_range
from mdhub.core.upsert import UpsertResult, touch_last_seen, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.sources.moex_fx import (
    CETS_SECURITIES,
    FIXING_LIST,
    FIXING_SECID,
    FX_INTRADAY_VALUE_COLUMNS,
    INDICATIVE,
    PARSER_VERSION,
    SELT_HISTORY,
    SELT_SECURITIES,
    parse_fixing_rows,
    parse_fx_history,
    parse_fx_instruments,
    parse_fx_marketdata,
    parse_fx_settle_dates,
    parse_indicative,
)

__all__ = [
    "fixing_list_dates",
    "fx_indicative",
    "fx_moex_eod",
    "fx_moex_intraday",
    "latest_rows",
    "only_changed",
    "refresh_fx_instruments",
    "resolve_dates",
]

SOURCE: Final = "moex_iss"
FX_CALENDAR: Final = "MOEX_FX"
FORTS_CALENDAR: Final = "MOEX_FORTS"
DEFAULT_FIXINGS: Final = ("SRATE_CNY_ON",)
FIXING_LOOKBACK_DAYS: Final = 7
FIXING_LIST_STEP_DAYS: Final = 28
INDICATIVE_START: Final = date(2024, 1, 3)
FIXING_START: Final = date(2013, 3, 1)
"""Scheduled ``fx_indicative`` runs re-read the last week of fixings (late publication)."""

FX_EOD_KEY: Final = ["trade_date", "secid", "board"]
FX_INTRADAY_SERIES: Final = ["secid", "board", "source", "source_variant"]
FX_INTRADAY_KEY: Final = [*FX_INTRADAY_SERIES, "ts"]
FX_INTRADAY_VARIANT: Final = "iss_public"
INTRADAY_ENDPOINT: Final = "intraday:fx_cets_marketdata"
"""Endpoints prefixed ``intraday:`` are removed by raw-capture retention (maintenance)."""
FX_INSTRUMENT_KEY: Final = ["secid", "board"]
FX_SETTLE_KEY: Final = ["trade_date", "secid", "board"]
FX_INSTRUMENT_OBSERVED: Final = ("settle_date",)
"""Observation columns of ``ref.fx_instrument`` (history meta): refreshed separately, never a
data change (the settlement date of a TOM instrument moves every day)."""

register_dataset(
    DatasetSpec(
        name="fx_eod",
        table="md.fx_eod",
        date_column="trade_date",
        calendar=FX_CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="fx_moex_eod",
        history_start=date(2024, 1, 3),
        min_rows_per_day=100,
        description="MOEX currency market (selt, all boards incl. CETS) daily OHLC/WAP",
    )
)
register_dataset(
    DatasetSpec(
        name="fx_intraday",
        table="md.fx_intraday",
        date_column="ts",
        calendar=FX_CALENDAR,
        frequency="intraday",
        max_lag_days=1,
        description="CETS marketdata snapshots (public ISS, 15-min delayed), change-only",
    )
)
register_dataset(
    DatasetSpec(
        name="fx_indicative",
        table="md.fx_indicative",
        date_column="trade_date",
        calendar=FORTS_CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="fx_indicative",
        history_start=INDICATIVE_START,
        min_rows_per_day=10,
        description="MOEX indicative FX rates vs RUB at 14:00 (tc) and 19:00 (mc) clearing",
    )
)
register_dataset(
    DatasetSpec(
        name="fx_fixing",
        table="md.fx_fixing",
        date_column="fixing_date",
        calendar=FX_CALENDAR,
        frequency="event",
        max_lag_days=5,
        backfill_job="fx_indicative",
        history_start=FIXING_START,
        description="MOEX currency-market fixings (USD/EUR/CNY/... FIXME, SRATE_*); "
        "not published every day",
    )
)


# ---------------------------------------------------------------------- helpers


def resolve_dates(
    ctx: JobContext,
    calendar: str,
    *,
    days_back: list[int] | None,
    date_from: date | None,
    date_to: date | None,
    default_days_back: Sequence[int] = (1,),
) -> list[date]:
    """Business dates to load: the explicit range (inclusive, business days of ``calendar``)
    if ``date_from``/``date_to`` is given, else ``days_back`` offsets."""
    if date_from is not None or date_to is not None:
        start = date_from or date_to
        end = date_to or date_from
        assert start is not None
        assert end is not None
        if start > end:
            raise JobParamError(f"date_from {start} is after date_to {end}")
        return [d for d in date_range(start, end) if ctx.is_business_day(d, calendar)]
    return ctx.business_dates(list(days_back or default_days_back), calendar=calendar)


def _with_provenance(
    rows: list[dict[str, Any]], capture_id: int, run_id: int
) -> list[dict[str, Any]]:
    for r in rows:
        r["capture_id"] = capture_id
        r["run_id"] = run_id
    return rows


def refresh_fx_instruments(
    conn: psycopg.Connection[Any],
    payload: Mapping[str, Any],
    *,
    capture_id: int,
    run_id: int,
    today: date,
) -> UpsertResult:
    """Upsert ``ref.fx_instrument`` from a selt ``securities`` block; ``first_seen`` is only
    written on insert, ``last_seen`` is set to ``today``. With a ``dataversion`` block the
    session's SETTLEDATE of every instrument goes to ``md.fx_settle_date`` (contract 1.5, H2:
    the value date of an outright, the near-leg date of a swap, keyed by the session date)."""
    rows = _with_provenance(parse_fx_instruments(payload), capture_id, run_id)
    if not rows:
        return UpsertResult()
    for r in rows:
        r["first_seen"] = today
    update_cols = [
        c for c in rows[0] if c not in (*FX_INSTRUMENT_KEY, "first_seen", *FX_INSTRUMENT_OBSERVED)
    ]
    result = upsert_rows(
        conn, "ref.fx_instrument", rows, FX_INSTRUMENT_KEY, update_cols, run_id=run_id
    )
    _observe_fx_settlement(conn, rows)
    touch_last_seen(conn, "ref.fx_instrument", FX_INSTRUMENT_KEY, rows, run_id)
    _, settle = parse_fx_settle_dates(payload)
    if settle:
        result += upsert_rows(
            conn,
            "md.fx_settle_date",
            _with_provenance(settle, capture_id, run_id),
            FX_SETTLE_KEY,
            run_id=run_id,
        )
    return result


def _observe_fx_settlement(conn: psycopg.Connection[Any], rows: list[dict[str, Any]]) -> None:
    """Refresh the observation columns (``settle_date`` moves every day; history meta) without
    counting a data change or bumping ``updated_at``."""
    obs = [r for r in rows if "settle_date" in r]
    if not obs:
        return
    conn.execute(
        """
        UPDATE ref.fx_instrument i SET settle_date = v.sd
        FROM unnest(%s::text[], %s::text[], %s::date[]) AS v(secid, board, sd)
        WHERE i.secid = v.secid AND i.board = v.board AND i.settle_date IS DISTINCT FROM v.sd
        """,
        (
            [r["secid"] for r in obs],
            [r["board"] for r in obs],
            [r["settle_date"] for r in obs],
        ),
    )


def latest_rows(
    conn: psycopg.Connection[Any],
    table: str,
    series_cols: Sequence[str],
    keys: Sequence[tuple[Any, ...]],
    value_cols: Sequence[str],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Last stored row (by ``ts``) per series key of an intraday table (PK index lookup)."""
    if not keys:
        return {}
    schema, name = table.split(".")
    arrays = [list(col) for col in zip(*keys, strict=True)]
    unnest = sql.SQL(", ").join(sql.SQL("%s::text[]") for _ in series_cols)
    key_ids = sql.SQL(", ").join(sql.Identifier(c) for c in series_cols)
    join = sql.SQL(" AND ").join(
        sql.SQL("t.{c} = k.{c}").format(c=sql.Identifier(c)) for c in series_cols
    )
    cols = sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(c)) for c in value_cols)
    query = sql.SQL(
        "SELECT k.*, l.* FROM unnest({unnest}) AS k({key_ids}) "
        "CROSS JOIN LATERAL (SELECT t.ts, {cols} FROM {tbl} t WHERE {join} "
        "ORDER BY t.ts DESC LIMIT 1) l"
    ).format(
        unnest=unnest,
        key_ids=key_ids,
        cols=cols,
        tbl=sql.Identifier(schema, name),
        join=join,
    )
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(query, arrays).fetchall()
    return {tuple(r[c] for c in series_cols): r for r in rows}


def only_changed(
    conn: psycopg.Connection[Any],
    table: str,
    rows: list[dict[str, Any]],
    series_cols: Sequence[str],
    value_cols: Sequence[str],
    *,
    logger: logging.Logger | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Drop rows whose values equal the last stored row of their series.

    A changed row whose ``ts`` is *earlier* than the last stored ``ts`` of its series (the
    quote dating rule picked a wrong session day, or the source went back in time) is still
    stored but logged as a warning on ``logger`` - quote times must never silently regress.

    :returns: (rows to store, number skipped as unchanged).
    """
    keys = sorted({tuple(str(r[c]) for c in series_cols) for r in rows})
    last = latest_rows(conn, table, series_cols, keys, value_cols)
    keep: list[dict[str, Any]] = []
    older: list[str] = []
    for r in rows:
        prev = last.get(tuple(str(r[c]) for c in series_cols))
        if prev is not None and all(prev[c] == r[c] for c in value_cols):
            continue
        if prev is not None and r.get("ts") is not None and prev["ts"] > r["ts"]:
            older.append(f"{'/'.join(str(r[c]) for c in series_cols)} {r['ts']} < {prev['ts']}")
        keep.append(r)
    if older and logger is not None:
        logger.warning(
            "%s: %d changed row(s) older than the last stored ts of their series: %s",
            table,
            len(older),
            "; ".join(older[:5]),
        )
    return keep, len(rows) - len(keep)


# ------------------------------------------------------------------------- jobs


@job("fx_moex_eod", "MOEX currency market (selt) daily history -> md.fx_eod")
def fx_moex_eod(
    ctx: JobContext,
    *,
    mode: str = "final",
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> None:
    """Load ``md.fx_eod`` for business dates (``days_back`` or ``date_from..date_to``).

    ``mode`` (``prelim``/``final``) only labels the run: both reload the same history; a
    later run that sees revised values writes the old image to ``ops.row_history``.
    """
    dates = resolve_dates(
        ctx, FX_CALENDAR, days_back=days_back, date_from=date_from, date_to=date_to
    )
    run_id = ctx.run.id
    with ctx.http(base_url=ISS_BASE_URL) as http:
        res = http.get_json(
            SELT_SECURITIES, params={"iss.meta": "off", "iss.only": "securities,dataversion"}
        )
        with ctx.connection() as conn:
            cap = record_capture(
                conn, run_id, SOURCE, "selt_securities", None, res.response, PARSER_VERSION
            )
            ctx.run.add_counts(
                refresh_fx_instruments(
                    conn, res.data, capture_id=cap, run_id=run_id, today=ctx.today()
                )
            )
        for d in dates:
            with ctx.connection() as conn:
                rows: list[dict[str, Any]] = []
                for page in iter_pages(http, SELT_HISTORY, {"date": d.isoformat()}):
                    cap = record_capture(
                        conn, run_id, SOURCE, "selt_history", None, page.response, PARSER_VERSION
                    )
                    rows.extend(_with_provenance(parse_fx_history(page.rows), cap, run_id))
                wrong = {r["trade_date"] for r in rows} - {d}
                if wrong:
                    raise IssFormatError(f"selt history for {d} returned dates {sorted(wrong)}")
                if not rows:
                    note = f"ISS selt history returned 0 rows for {d} (mode={mode})"
                    ctx.logger.warning(note)
                    if mode != "prelim":
                        record_no_data(conn, "fx_eod", d, note, today=ctx.today())
                    continue
                ctx.run.add_counts(upsert_rows(conn, "md.fx_eod", rows, FX_EOD_KEY, run_id=run_id))
                close_gap(conn, "fx_eod", d)
                ctx.logger.info("fx_eod %s: %d rows", d, len(rows))


@job("fx_moex_intraday", "CETS marketdata snapshot (change-only) -> md.fx_intraday")
def fx_moex_intraday(ctx: JobContext) -> None:
    run_id = ctx.run.id
    with ctx.http(base_url=ISS_BASE_URL) as http:
        res = http.get_json(
            CETS_SECURITIES,
            params={"iss.meta": "off", "iss.only": "securities,marketdata,dataversion"},
        )
    rows, latency = parse_fx_marketdata(
        res.data, is_trading_day=lambda d: ctx.is_trading_day(d, FX_CALENDAR)
    )
    for r in rows:
        r["source"] = SOURCE
        r["source_variant"] = FX_INTRADAY_VARIANT
    source_ts = max((r["source_ts"] for r in rows if r["source_ts"]), default=None)
    with ctx.connection() as conn:
        cap = record_capture(
            conn,
            run_id,
            SOURCE,
            INTRADAY_ENDPOINT,
            None,
            res.response,
            PARSER_VERSION,
            source_ts=source_ts,
        )
        ctx.run.add_counts(
            refresh_fx_instruments(conn, res.data, capture_id=cap, run_id=run_id, today=ctx.today())
        )
        changed, unchanged = only_changed(
            conn,
            "md.fx_intraday",
            rows,
            FX_INTRADAY_SERIES,
            FX_INTRADAY_VALUE_COLUMNS,
            logger=ctx.logger,
        )
        _with_provenance(changed, cap, run_id)
        result = upsert_rows(conn, "md.fx_intraday", changed, FX_INTRADAY_KEY, run_id=run_id)
        ctx.run.add_counts(result + UpsertResult(unchanged=unchanged))
    ctx.logger.info(
        "fx_intraday: %d priced rows, %d changed, latency %s", len(rows), len(changed), latency
    )


@job("fx_indicative", "MOEX indicative FX rates (tc/mc) and currency fixings")
def fx_indicative(
    ctx: JobContext,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> None:
    """Indicative rates for each business date from :data:`INDICATIVE_START` (default: today)
    and fixings for the range (default: the last :data:`FIXING_LOOKBACK_DAYS` days)."""
    run_id = ctx.run.id
    explicit = date_from is not None or date_to is not None
    if explicit:
        dates = resolve_dates(
            ctx, FORTS_CALENDAR, days_back=None, date_from=date_from, date_to=date_to
        )
        # a fixings backfill reaching before the indicative history leaves those days alone
        dates = [d for d in dates if d >= INDICATIVE_START]
        fix_from = date_from or date_to
        fix_to = date_to or date_from
    else:
        dates = ctx.business_dates([0], calendar=FORTS_CALENDAR)
        fix_to = ctx.today()
        fix_from = fix_to - timedelta(days=FIXING_LOOKBACK_DAYS)
    assert fix_from is not None
    assert fix_to is not None
    with ctx.http(base_url=ISS_BASE_URL) as http:
        for d in dates:
            res = http.get_json(INDICATIVE, params={"iss.meta": "off", "date": d.isoformat()})
            with ctx.connection() as conn:
                cap = record_capture(
                    conn, run_id, SOURCE, "indicative_rates", None, res.response, PARSER_VERSION
                )
                rows = _with_provenance(parse_indicative(res.data), cap, run_id)
                wrong = {r["trade_date"] for r in rows} - {d}
                if wrong:
                    raise IssFormatError(f"indicative rates for {d} returned {sorted(wrong)}")
                if not rows:
                    note = f"ISS indicative rates returned 0 rows for {d}"
                    ctx.logger.warning(note)
                    record_no_data(conn, "fx_indicative", d, note, today=ctx.today())
                    continue
                ctx.run.add_counts(
                    upsert_rows(
                        conn,
                        "md.fx_indicative",
                        rows,
                        ["trade_date", "clearing", "ccy_pair"],
                        run_id=run_id,
                    )
                )
                close_gap(conn, "fx_indicative", d)
        _load_fixings(ctx, http, fix_from, fix_to)


def fixing_list_dates(start: date, end: date) -> list[date]:
    """Dates whose fixing list is read for ``[start, end]``: both ends and every
    :data:`FIXING_LIST_STEP_DAYS` days between, so that a fixing discontinued inside a long
    backfill range (USDFIXME after 2024-06-11) is still found."""
    out: list[date] = []
    d = start
    while d < end:
        out.append(d)
        d += timedelta(days=FIXING_LIST_STEP_DAYS)
    out.append(end)
    return out


def _load_fixings(ctx: JobContext, http: Any, start: date, end: date) -> None:
    run_id = ctx.run.id
    secids = set(DEFAULT_FIXINGS)
    for day in fixing_list_dates(start, end):
        res = http.get_json(FIXING_LIST, params={"iss.meta": "off", "date": day.isoformat()})
        with ctx.connection() as conn:
            cap = record_capture(
                conn, run_id, SOURCE, "fx_fixing_list", None, res.response, PARSER_VERSION
            )
            listed = parse_fixing_rows(
                parse_block(res.data, "history", required=("tradedate", "secid", "rate"))
            )
        secids |= {r["secid"] for r in listed}
        ctx.logger.debug("fixing list %s capture %d: %s", day, cap, sorted(secids))
    for secid in sorted(secids):
        with ctx.connection() as conn:
            rows: list[dict[str, Any]] = []
            for page in iter_pages(
                http,
                FIXING_SECID.format(secid=secid),
                {"from": start.isoformat(), "till": end.isoformat()},
                required=("tradedate", "secid", "rate"),
            ):
                cap = record_capture(
                    conn, run_id, SOURCE, "fx_fixing", None, page.response, PARSER_VERSION
                )
                rows.extend(_with_provenance(parse_fixing_rows(page.rows), cap, run_id))
            rows = [r for r in rows if r["secid"] == secid and start <= r["fixing_date"] <= end]
            ctx.run.add_counts(
                upsert_rows(conn, "md.fx_fixing", rows, ["fixing_date", "secid"], run_id=run_id)
            )
