"""Rosstat monthly CPI -> ``md.fixing`` (``index_code = 'CPI_RU_MOM'``, ``source = 'rosstat'``).

``rosstat_cpi(date_from=None, date_to=None)`` (10:05 and 19:35 MSK, all days; Rosstat
publishes the monthly CPI in the evening around the middle of the next month) reads the price
page, follows the newest ``ipc_mes_MM-YYYY.xlsx`` link and stores the CPI to the previous month
for every month since 1991 (:func:`mdhub.sources.rosstat.parse_cpi_monthly`).

Row: ``fixing_date`` = first day of the month the index refers to, ``value`` = index in
percent of the previous month (``100.62``), ``unit = 'index'``, ``details.period`` =
``YYYY-MM``. ``published_on`` = the release date the price page states for the workbook (else
the workbook's ``Обновлено`` date), set on the **newest** month of the workbook when that month
has none yet; older months keep what they had (Rosstat does not publish past release dates per
month, so months loaded from history stay NULL - never guessed). ``ingested_at`` is when the hub
learnt the value (refreshed only on change, revisions go to ``ops.row_history``).

``cbr_inflation`` (10:15 and 19:45 MSK, all days) stores the official CPI y/y as CBR republishes
it (``/hd_base/infl/``, ``Инфляция, % г/г``, from 2013-09) as ``CPI_RU_YOY`` (source ``cbr``,
``unit = 'pct'``: ``6.35`` = prices +6.35 % on the same month of the previous year,
``fixing_date`` = first day of the month). Rosstat's machine-readable CPI tables on the price
page carry only m/m and Dec/Dec indices; its y/y table lives in EMISS (fedstat.ru, not reachable
from the development environment), so CBR is the source of the published y/y. Cross-check: the
chain of the twelve Rosstat ``CPI_RU_MOM`` indices ending in the month must reproduce the CBR
y/y within :data:`YOY_CHAIN_TOLERANCE_PP` (rounding of the monthly values); a larger difference
is logged as a warning and the run ends ``partial`` (nothing is changed).

Chain the monthly indices for any period (OFZ-IN face indexation, CPI-linked coupons).

Without dates an identical workbook (same SHA-256 as the previous capture) is not stored or
parsed again; with ``date_from``/``date_to`` (backfill) the workbook is always captured and
the months overlapping the range are upserted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.cbr import FIXING_KEY, capture_unless_unchanged, fixing_row
from mdhub.sources import cbr as cbr_src
from mdhub.sources import rosstat as src

__all__ = ["CPI_CODE", "CPI_YOY_CODE", "HISTORY_START", "cbr_inflation", "rosstat_cpi"]

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
        path, resp, page_date = src.fetch_cpi_workbook(http)
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
        parsed = src.parse_cpi_monthly(resp.content)
        newest = parsed[-1].period if parsed else None
        released = page_date or src.workbook_release_date(resp.content)
        months = [m for m in parsed if d1.replace(day=1) <= m.period <= d2]
        stored = {
            r["fixing_date"]: r["published_on"]
            for r in conn.execute(
                "SELECT fixing_date, published_on FROM md.fixing "
                "WHERE index_code = %s AND source = %s AND fixing_date = ANY(%s)",
                (CPI_CODE, SOURCE, [m.period for m in months]),
            ).fetchall()
        }

        def published_on(period: date) -> date | None:
            known: date | None = stored.get(period)
            if known is not None:
                return known
            return released if period == newest else None

        rows = [
            fixing_row(
                CPI_CODE,
                m.period,
                m.index_pct,
                capture_id=cap,
                run_id=ctx.run.id,
                known_at=known_at,
                unit="index",
                published_on=published_on(m.period),
                details={"period": m.period.strftime("%Y-%m")},
                source=SOURCE,
            )
            for m in months
        ]
        result = upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)
    ctx.run.add_counts(result)
    return {
        "workbook": path,
        "released": str(released) if released else None,
        "months": len(months),
        "first": str(months[0].period) if months else None,
        "last": str(months[-1].period) if months else None,
        "inserted": result.inserted,
        "updated": result.updated,
    }


# ------------------------------------------------------------------- CBR CPI y/y

CPI_YOY_CODE: Final = "CPI_RU_YOY"
YOY_HISTORY_START: Final = date(2013, 9, 1)
YOY_CHAIN_TOLERANCE_PP: Final = Decimal("0.05")
"""Allowed difference (percentage points) between the chained Rosstat m/m indices and the
published y/y: the monthly values are rounded to 0.01 of an index point."""
CBR_SOURCE: Final = "cbr"
CBR_ENDPOINT: Final = "inflation_page"

register_dataset(
    DatasetSpec(
        name="cbr_cpi_yoy",
        table="md.fixing",
        date_column="fixing_date",
        calendar=None,
        frequency="monthly",
        max_lag_days=50,  # as CPI_RU_MOM: month M appears mid-M+1
        backfill_job="cbr_inflation",
        history_start=YOY_HISTORY_START,
        where="index_code = 'CPI_RU_YOY' AND source = 'cbr'",
        description="Russia CPI y/y in % (official Rosstat figure as republished by CBR), "
        "monthly since 2013-09",
    )
)


def chained_yoy(moms: dict[date, Decimal], period: date) -> Decimal | None:
    """y/y in % from the twelve m/m indices ending in ``period`` (``None``: one is missing)."""
    out = Decimal(1)
    y, m = period.year, period.month
    for _ in range(12):
        v = moms.get(date(y, m, 1))
        if v is None:
            return None
        out *= v / 100
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return (out - 1) * 100


@job("cbr_inflation", "CBR: official CPI y/y (Инфляция, % г/г) -> md.fixing CPI_RU_YOY")
def cbr_inflation(
    ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
) -> dict[str, Any]:
    explicit = date_from is not None or date_to is not None
    d1 = (date_from or YOY_HISTORY_START).replace(day=1)
    d2 = date_to or ctx.today()
    if d1 > d2:
        raise ValueError(f"date_from {d1} after date_to {d2}")
    with ctx.http() as http:
        resp = cbr_src.fetch_hd_base(http, cbr_src.INFLATION_PATH, d1, d2)
    known_at = utc_now()
    with ctx.connection() as conn:
        if explicit:
            cap: int | None = record_capture(
                conn, ctx.run.id, CBR_SOURCE, CBR_ENDPOINT, None, resp, cbr_src.PARSER_VERSION
            )
        else:
            cap = capture_unless_unchanged(
                conn, ctx.run.id, CBR_SOURCE, CBR_ENDPOINT, resp, cbr_src.PARSER_VERSION
            )
        if cap is None:
            ctx.logger.info("cbr_inflation: page unchanged since the previous capture")
            return {"unchanged": True}
        parsed = [r for r in cbr_src.parse_inflation_page(resp.text) if d1 <= r.period <= d2]
        rows = [
            fixing_row(
                CPI_YOY_CODE,
                r.period,
                r.inflation_yoy_pct,
                capture_id=cap,
                run_id=ctx.run.id,
                known_at=known_at,
                unit="pct",
                details={"period": r.period.strftime("%Y-%m"), "definition": "cpi_yoy_minus_100"},
                source=CBR_SOURCE,
            )
            for r in parsed
        ]
        result = upsert_rows(conn, "md.fixing", rows, FIXING_KEY, run_id=ctx.run.id)
        moms = {
            r["fixing_date"]: r["value"]
            for r in conn.execute(
                "SELECT fixing_date, value FROM md.fixing WHERE index_code = %s AND source = %s",
                (CPI_CODE, SOURCE),
            ).fetchall()
        }
    ctx.run.add_counts(result)
    mismatches = []
    checked = 0
    for r in parsed:
        chained = chained_yoy(moms, r.period)
        if chained is None:
            continue
        checked += 1
        if abs(chained - r.inflation_yoy_pct) > YOY_CHAIN_TOLERANCE_PP:
            mismatches.append(f"{r.period:%Y-%m}: CBR {r.inflation_yoy_pct}, chain {chained:.2f}")
    if mismatches:
        ctx.logger.warning("cbr_inflation: y/y differs from the Rosstat m/m chain: %s", mismatches)
        ctx.run.mark_partial(f"y/y vs Rosstat m/m chain: {'; '.join(mismatches[:5])}")
    return {
        "months": len(parsed),
        "first": str(parsed[0].period) if parsed else None,
        "last": str(parsed[-1].period) if parsed else None,
        "chain_checked": checked,
        "chain_mismatches": mismatches,
        "inserted": result.inserted,
        "updated": result.updated,
    }
