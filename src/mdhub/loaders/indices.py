"""MOEX ISS indices EOD -> ``md.index_eod`` (every index ISS lists, ~600 per day).

Source choice: ``/history/engines/stock/markets/index/securities.json?date=D`` returns all
indices of one day in ~7 cursor pages - cheaper than per-index history for the configured
list, so the whole universe is stored (RUSFAR family incl. CNY and real-time variants,
RGBI/RGBITR, RUCBI*, RUGBI*, IMOEX, MOEXREPO*, ...).

Same-day values: the history of day D only appears after the day closes, while RUSFAR is
fixed at 12:30 and RUSFARRT at 18:00. For today the job therefore also reads current
``marketdata`` of the group's indices (``group=all``: every index) and stores those with
``basis='marketdata'`` for indices missing from today's history. The next run that sees the
history replaces them (``basis='history'``; the preliminary image stays in
``ops.row_history``).

Scheduled (no dates): today (history + marketdata fallback) and the previous business day
(history). With ``day``/``date_from``/``date_to``: history only, all indices, every MOEX_STOCK
business day of the range; a day without rows is recorded as "no data".
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.http import HttpClient
from mdhub.core.iss import ISS_BASE_URL, iter_pages
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.bonds_eod import close_gap, record_no_data, resolve_trade_dates, run_dates
from mdhub.sources.moex_market import (
    INDEX_GROUPS,
    INDEX_HISTORY_PATH,
    INDEX_HISTORY_REQUIRED,
    INDEX_MARKETDATA_PATH,
    PARSER_VERSION,
    index_group_secids,
    parse_index_history,
    parse_index_marketdata,
)

__all__ = ["load_index_day"]

CALENDAR: Final = "MOEX_STOCK"
KEY: Final = ["trade_date", "secid"]
SOURCE: Final = "moex_iss"
DATASET_ALL: Final = "indices_all"

register_dataset(
    DatasetSpec(
        name=DATASET_ALL,
        table="md.index_eod",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="indices_eod",
        history_start=date(2022, 1, 3),
        min_rows_per_day=300,
        description="All MOEX ISS indices EOD (history by date)",
        where="basis = 'history'",
    )
)
for _group, _secids in (
    ("rusfar", INDEX_GROUPS["rusfar"]),
    ("rusfarrt", INDEX_GROUPS["rusfarrt"]),
    ("bonds", tuple(s for s in INDEX_GROUPS["bonds"] if s != "MOEXREPO")),
):
    register_dataset(
        DatasetSpec(
            name=f"indices_{_group}",
            table="md.index_eod",
            date_column="trade_date",
            calendar=CALENDAR,
            frequency="business_daily",
            max_lag_days=1,
            backfill_job="indices_eod",
            history_start=date(2022, 1, 3),
            min_rows_per_day=1,
            series_column="secid",
            series=_secids,
            description=f"MOEX ISS indices EOD, group {_group}",
        )
    )


def _load_history(
    ctx: JobContext, http: HttpClient, conn: Any, trade_date: date
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in iter_pages(
        http,
        INDEX_HISTORY_PATH,
        {"date": trade_date.isoformat()},
        required=INDEX_HISTORY_REQUIRED,
    ):
        cap = record_capture(
            conn, ctx.run.id, SOURCE, "index_history", None, page.response, PARSER_VERSION
        )
        for row in parse_index_history(page.rows, trade_date):
            row.update(capture_id=cap, run_id=ctx.run.id)
            rows.append(row)
    if len({r["secid"] for r in rows}) != len(rows):
        raise ValueError(f"index history {trade_date}: duplicate secid across pages")
    return rows


def _load_marketdata(
    ctx: JobContext,
    http: HttpClient,
    conn: Any,
    trade_date: date,
    secids: tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"iss.meta": "off", "iss.only": "marketdata"}
    if secids is not None:
        params["securities"] = ",".join(secids)
    res = http.get_json(INDEX_MARKETDATA_PATH, params=params)
    rows = parse_index_marketdata(res.data, trade_date, secids)
    source_ts = max((r["source_ts"] for r in rows if r["source_ts"]), default=None)
    cap = record_capture(
        conn,
        ctx.run.id,
        SOURCE,
        "index_marketdata",
        None,
        res.response,
        PARSER_VERSION,
        source_ts=source_ts,
    )
    for row in rows:
        row.update(capture_id=cap, run_id=ctx.run.id)
    return rows


def load_index_day(
    ctx: JobContext,
    http: HttpClient,
    trade_date: date,
    *,
    group: str = "all",
    use_marketdata: bool = False,
    record_gap: bool = True,
) -> dict[str, Any]:
    """Load one day: history (all indices) [+ same-day marketdata for the group]."""
    secids = index_group_secids(group)
    with ctx.connection() as conn:
        rows = _load_history(ctx, http, conn, trade_date)
        n_history = len(rows)
        n_md = 0
        if use_marketdata:
            have = {r["secid"] for r in rows}
            md_rows = [
                r
                for r in _load_marketdata(ctx, http, conn, trade_date, secids)
                if r["secid"] not in have
            ]
            n_md = len(md_rows)
            rows.extend(md_rows)
        if n_history:
            close_gap(conn, DATASET_ALL, trade_date)
        elif not use_marketdata:
            note = f"ISS index history returned 0 rows for {trade_date}"
            ctx.logger.warning(note)
            if record_gap:
                record_no_data(conn, DATASET_ALL, trade_date, note)
        result = upsert_rows(conn, "md.index_eod", rows, KEY, run_id=ctx.run.id)
        ctx.run.add_counts(result)
    ctx.logger.info(
        "indices_eod %s: %d history rows, %d marketdata rows (%d new, %d changed)",
        trade_date,
        n_history,
        n_md,
        result.inserted,
        result.updated,
    )
    return {
        "history": n_history,
        "marketdata": n_md,
        "inserted": result.inserted,
        "updated": result.updated,
    }


@job("indices_eod", "MOEX ISS indices EOD (all indices by date; RUSFAR same-day values)")
def indices_eod(
    ctx: JobContext,
    *,
    group: str = "all",
    days_back: list[int] | None = None,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    index_group_secids(group)  # validate early
    explicit = day is not None or date_from is not None or date_to is not None
    dates = resolve_trade_dates(
        ctx,
        days_back=days_back,
        default_days_back=[0, 1],
        day=day,
        date_from=date_from,
        date_to=date_to,
    )
    if not dates:
        ctx.run.mark_skipped("no business dates to load")
        return {}
    today = ctx.today()
    with ctx.http(base_url=ISS_BASE_URL) as http:
        return run_dates(
            ctx,
            dates,
            lambda d: load_index_day(
                ctx,
                http,
                d,
                group=group,
                use_marketdata=(d == today and not explicit),
                record_gap=d != today,
            ),
        )
