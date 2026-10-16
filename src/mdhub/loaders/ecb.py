"""ECB euro reference rates -> ``md.ecb_fx``.

``ecb_fx(date_from=None, date_to=None)`` (18:30 MSK, all days): without dates loads
``eurofxref-daily.xml`` (the latest TARGET day; an empty table is seeded from
:data:`HISTORY_START` instead). With a range: ``eurofxref-hist-90d.xml`` when the range lies
within the last 89 days, else ``eurofxref-hist.zip`` (one request, filtered to the range).
Identical payloads (same SHA-256 as the previous capture of the endpoint) are not stored again.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.cbr import capture_unless_unchanged
from mdhub.sources import ecb as src

__all__ = ["HISTORY_START", "ecb_fx"]

SOURCE: Final = "ecb"
KEY: Final = ["rate_date", "ccy"]
HISTORY_START: Final = date(2015, 1, 1)

register_dataset(
    DatasetSpec(
        name="ecb_fx",
        table="md.ecb_fx",
        date_column="rate_date",
        calendar=None,
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="ecb_fx",
        history_start=HISTORY_START,
        min_rows_per_day=20,
        description="ECB euro reference rates (per EUR), TARGET days; holidays end unfillable",
    )
)


@job("ecb_fx", "ECB euro foreign exchange reference rates")
def ecb_fx(
    ctx: JobContext, *, date_from: date | None = None, date_to: date | None = None
) -> dict[str, Any]:
    today = ctx.today()
    explicit = date_from is not None or date_to is not None
    if not explicit:
        with ctx.connection() as conn:
            empty = conn.execute("SELECT 1 FROM md.ecb_fx LIMIT 1").fetchone() is None
        if empty:
            explicit, date_from, date_to = True, HISTORY_START, today
    d1 = date_from or date_to or today
    d2 = date_to or today
    if not explicit:
        url, endpoint = src.DAILY_URL, "eurofxref_daily"
    elif d1 >= today - timedelta(days=89):
        url, endpoint = src.HIST_90D_URL, "eurofxref_hist_90d"
    else:
        url, endpoint = src.HIST_ZIP_URL, "eurofxref_hist_zip"
    with ctx.http() as http:
        resp = src.fetch(http, url)
    known_at = utc_now()
    with ctx.connection() as conn:
        if explicit:  # ranges filter the payload: an unchanged file may still hold new days
            cap: int | None = record_capture(
                conn, ctx.run.id, SOURCE, endpoint, None, resp, src.PARSER_VERSION
            )
        else:
            cap = capture_unless_unchanged(
                conn, ctx.run.id, SOURCE, endpoint, resp, src.PARSER_VERSION
            )
        if cap is None:
            ctx.logger.info("ecb_fx: %s unchanged since the previous capture", endpoint)
            return {"endpoint": endpoint, "unchanged": True}
        if endpoint == "eurofxref_hist_zip":
            rates = src.parse_hist_zip(resp.content, date_from=d1, date_to=d2)
        else:
            rates = [
                r for r in src.parse_xml(resp.content) if not explicit or d1 <= r.rate_date <= d2
            ]
        rows = [
            {
                "rate_date": r.rate_date,
                "ccy": r.ccy,
                "rate_per_eur": r.rate_per_eur,
                "capture_id": cap,
                "run_id": ctx.run.id,
                "ingested_at": known_at,
            }
            for r in rates
        ]
        result = upsert_rows(conn, "md.ecb_fx", rows, KEY, run_id=ctx.run.id)
    ctx.run.add_counts(result)
    days = sorted({r.rate_date for r in rates})
    return {
        "endpoint": endpoint,
        "days": len(days),
        "first": str(days[0]) if days else None,
        "last": str(days[-1]) if days else None,
        "inserted": result.inserted,
        "updated": result.updated,
    }
