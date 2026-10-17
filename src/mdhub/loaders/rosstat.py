"""Rosstat monthly CPI -> ``md.fixing`` (``index_code = 'CPI_RU_MOM'``, ``source = 'rosstat'``).

``rosstat_cpi(date_from=None, date_to=None)`` (10:05 and 19:35 MSK, all days; Rosstat
publishes the monthly CPI in the evening around the middle of the next month) reads the price
page, follows the newest ``ipc_mes_MM-YYYY.xlsx`` link and stores the CPI to the previous month
for every month since 1991 (:func:`mdhub.sources.rosstat.parse_cpi_monthly`).

Row: ``fixing_date`` = first day of the month the index refers to, ``value`` = index in
percent of the previous month (``100.62``), ``unit = 'index'``, ``details.period`` =
``YYYY-MM``. The release date is not in the workbook per month, so ``published_on`` stays
NULL; ``ingested_at`` is when the hub learnt the value (refreshed only on change, revisions
go to ``ops.row_history``). Chain the monthly indices for any period (OFZ-IN face indexation,
CPI-linked coupons).

Without dates an identical workbook (same SHA-256 as the previous capture) is not stored or
parsed again; with ``date_from``/``date_to`` (backfill) the workbook is always captured and
the months overlapping the range are upserted.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.cbr import FIXING_KEY, capture_unless_unchanged, fixing_row
from mdhub.sources import rosstat as src

__all__ = ["CPI_CODE", "HISTORY_START", "rosstat_cpi"]

SOURCE: Final = "rosstat"
CPI_CODE: Final = "CPI_RU_MOM"
ENDPOINT: Final = "cpi_monthly_xlsx"
HISTORY_START: Final = date(src.FIRST_YEAR, 1, 1)

register_dataset(
    DatasetSpec(
        name="rosstat_cpi",
        table="md.fixing",
        date_column="fixing_date",
        calendar=None,
        frequency="monthly",
        # CPI of month M appears around the 10th-17th of M+1: on day 50 after a month start
        # the month before last must be there.
        max_lag_days=50,
        backfill_job="rosstat_cpi",
        history_start=HISTORY_START,
        where="index_code = 'CPI_RU_MOM' AND source = 'rosstat'",
        description="Rosstat CPI to the previous month (%), Russia, monthly since 1991",
    )
)


@job("rosstat_cpi", "Rosstat monthly consumer price index (CPI to the previous month)")
def rosstat_cpi(
    ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
) -> dict[str, Any]:
    explicit = date_from is not None or date_to is not None
    d1 = date_from or HISTORY_START
    d2 = date_to or ctx.today()
    if d1 > d2:
        raise ValueError(f"date_from {d1} after date_to {d2}")
    with ctx.http() as http:
        path, resp = src.fetch_cpi_workbook(http)
    known_at = utc_now()
    with ctx.connection() as conn:
        if explicit:
            cap: int | None = record_capture(
                conn, ctx.run.id, SOURCE, ENDPOINT, None, resp, src.PARSER_VERSION
            )
        else:
            cap = capture_unless_unchanged(
                conn, ctx.run.id, SOURCE, ENDPOINT, resp, src.PARSER_VERSION
            )
        if cap is None:
            ctx.logger.info("rosstat_cpi: %s unchanged since the previous capture", path)
            return {"workbook": path, "unchanged": True}
        months = [
            m for m in src.parse_cpi_monthly(resp.content) if d1.replace(day=1) <= m.period <= d2
        ]
        rows = [
            fixing_row(
                CPI_CODE,
                m.period,
                m.index_pct,
                capture_id=cap,
                run_id=ctx.run.id,
                known_at=known_at,
                unit="index",
                details={"period": m.period.strftime("%Y-%m")},
                source=SOURCE,
            )
            for m in months
        ]
        result = upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)
    ctx.run.add_counts(result)
    return {
        "workbook": path,
        "months": len(months),
        "first": str(months[0].period) if months else None,
        "last": str(months[-1].period) if months else None,
        "inserted": result.inserted,
        "updated": result.updated,
    }
