"""ECB euro reference rates -> ``md.ecb_fx``.

``ecb_fx(date_from=None, date_to=None)`` (18:30 MSK, all days): without dates loads
``eurofxref-daily.xml`` (the latest TARGET day; an empty table is seeded from
:data:`HISTORY_START` instead). With a range: ``eurofxref-hist-90d.xml`` when the range lies
within the last 89 days, else ``eurofxref-hist.zip`` (one request, filtered to the range).
Identical payloads (same SHA-256 as the previous capture of the endpoint) are not stored again.

Fallback (WEB-16; www.ecb.europa.eu does not answer from the hub VPS, WEB-4): when eurofxref
fails (HTTP, transport or format error) or ``ecb`` is in ``MDHUB_SKIP_SOURCES``, the same ECB
reference rates are read from the ECB Data Portal (``data-api.ecb.europa.eu``,
``EXR/D..EUR.SP00.A``, source ``ecb_data_portal``, endpoint ``exr_daily``) for the requested
range (without dates: the last :data:`FALLBACK_LOOKBACK_DAYS` days). They are the same
published values, so they go to the same ``md.ecb_fx`` series; the capture's source /
endpoint tells which host served a day. If both fail the run fails; the independent cross
rates of ``fx_cross`` (CBR, MOEX, FRED; ``md.fx_cross``) remain, labelled by kind.

Dataset calendar ``TARGET`` (:mod:`mdhub.loaders.calendar`, source ``rule_target``): weekends
and the TARGET closing days (Jan 1, Good Friday, Easter Monday, May 1, Dec 25, Dec 26) are not
expected by the gap scan; every other day is, so a failed load stays a gap. The calendar
refresh cross-checks the rule against the stored rate dates (warning, run ``partial``).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

import httpx

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpError
from mdhub.core.redact import sanitize_text
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.cbr import capture_unless_unchanged
from mdhub.sources import ecb as src
from mdhub.sources import ecb_data

__all__ = ["HISTORY_START", "ecb_fx"]

SOURCE: Final = "ecb"
KEY: Final = ["rate_date", "ccy"]
HISTORY_START: Final = date(2015, 1, 1)
FALLBACK_LOOKBACK_DAYS: Final = 10

register_dataset(
    DatasetSpec(
        name="ecb_fx",
        table="md.ecb_fx",
        date_column="rate_date",
        calendar="TARGET",
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="ecb_fx",
        history_start=HISTORY_START,
        min_rows_per_day=20,
        description="ECB euro reference rates (per EUR), TARGET business days (calendar TARGET)",
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
    error = "ecb skipped (MDHUB_SKIP_SOURCES)"
    if SOURCE not in ctx.settings.skipped_sources():
        try:
            with ctx.http(max_retries=1, timeout=20.0) as http:
                resp = src.fetch(http, url)
            if endpoint == "eurofxref_hist_zip":
                src.parse_hist_zip(resp.content, date_from=d1, date_to=d2)
            else:
                src.parse_xml(resp.content)
        except (HttpError, httpx.HTTPError, OSError, FormatDriftError) as exc:
            error = sanitize_text(f"{type(exc).__name__}: {exc}", ctx.settings.secret_values())
        else:
            return _store_eurofxref(ctx, resp, endpoint, explicit, d1, d2)
    ctx.logger.warning("ecb_fx: eurofxref failed (%s); ECB Data Portal fallback", error[:300])
    lo = d1 if explicit else today - timedelta(days=FALLBACK_LOOKBACK_DAYS)
    with ctx.http() as http:
        resp = ecb_data.fetch(http, ecb_data.EXR_FLOW_KEY, lo, d2)
    obs = ecb_data.exr_from_csv(resp.content)
    known_at = utc_now()
    with ctx.connection() as conn:
        cap = record_capture(
            conn, ctx.run.id, "ecb_data_portal", "exr_daily", None, resp, ecb_data.PARSER_VERSION
        )
        rows = [
            {
                "rate_date": o.day,
                "ccy": o.series,
                "rate_per_eur": o.value,
                "capture_id": cap,
                "run_id": ctx.run.id,
                "ingested_at": known_at,
            }
            for o in obs
        ]
        result = upsert_rows(conn, "md.ecb_fx", rows, KEY, run_id=ctx.run.id)
    ctx.run.add_counts(result)
    days = sorted({o.day for o in obs})
    return {
        "endpoint": "exr_daily",
        "source": "ecb_data_portal",
        "primary_error": error[:300],
        "days": len(days),
        "first": str(days[0]) if days else None,
        "last": str(days[-1]) if days else None,
        "inserted": result.inserted,
        "updated": result.updated,
    }


def _store_eurofxref(
    ctx: JobContext, resp: httpx.Response, endpoint: str, explicit: bool, d1: date, d2: date
) -> dict[str, Any]:
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
