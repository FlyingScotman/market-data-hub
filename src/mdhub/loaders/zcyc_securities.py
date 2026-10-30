"""KBD quotes per OFZ (``zcyc.json`` block ``securities``) -> ``md.zcyc_security_quote``.

Job ``zcyc_securities``: the schedule fires it every minute of the stock market's main session;
one run polls :data:`DEFAULT_POLLS` times :data:`DEFAULT_INTERVAL_S` seconds apart (cron has
minute granularity; a 10 s poll is wanted). Each poll is one request (~6 KB, ~29 OFZ-PD), its
raw answer is captured (``intraday:zcyc_securities``) and a row is stored per secid only when
one of :data:`~mdhub.sources.moex_intraday.ZCYC_SECURITY_COMPARE` changed since the last stored
row (change-only; a same-``ts`` revision updates in place, previous image in row history).

``latency``: the freshest ``tradetime`` of the answer vs the request time (realtime within
120 s; observed lag 1-3 s on public ISS). A poll that fails (ISS resets about every 3rd-4th
connection; :class:`~mdhub.core.http.HttpClient` already retries with exponential backoff and
jitter) is logged and the run marked partial; the remaining polls still run.
"""

from __future__ import annotations

import time
from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.http import HttpError
from mdhub.core.iss import ISS_BASE_URL, IssFormatError
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, JobParamError, job
from mdhub.loaders.fx_moex import only_changed
from mdhub.sources.moex_intraday import (
    PARSER_VERSION,
    ZCYC_SECURITIES,
    ZCYC_SECURITY_COMPARE,
    parse_zcyc_securities,
)

__all__ = ["poll_zcyc_securities", "zcyc_securities"]

SOURCE: Final = "moex_iss"
ENDPOINT: Final = "intraday:zcyc_securities"
TABLE: Final = "md.zcyc_security_quote"
CALENDAR: Final = "MOEX_STOCK"
DEFAULT_POLLS: Final = 6
DEFAULT_INTERVAL_S: Final = 10.0
MAX_POLLS: Final = 12

register_dataset(
    DatasetSpec(
        name="zcyc_security_quote",
        table=TABLE,
        date_column="ts",
        calendar=CALENDAR,
        frequency="intraday",
        max_lag_days=1,
        min_rows_per_day=20,
        description="KBD block securities: OFZ bid/ask (price, yield) near realtime, change-only",
    )
)


@job("zcyc_securities", "KBD per-OFZ bid/ask (zcyc.json securities), polled every ~10 s")
def zcyc_securities(
    ctx: JobContext,
    *,
    polls: int = DEFAULT_POLLS,
    interval_s: float = DEFAULT_INTERVAL_S,
) -> dict[str, Any]:
    if not 1 <= polls <= MAX_POLLS:
        raise JobParamError(f"polls must be 1..{MAX_POLLS}")
    if interval_s < 0 or polls * interval_s > 60:
        raise JobParamError("polls x interval_s must fit in one minute")
    stored = 0
    failed: list[str] = []
    started = time.monotonic()
    with ctx.http(base_url=ISS_BASE_URL) as http:
        for i in range(polls):
            if i:
                time.sleep(max(0.0, started + i * interval_s - time.monotonic()))
            try:
                stored += poll_zcyc_securities(ctx, http)
            except (HttpError, IssFormatError) as exc:
                failed.append(f"poll {i + 1}: {type(exc).__name__}")
                ctx.logger.warning("zcyc_securities poll %d failed: %s", i + 1, exc)
    if failed and len(failed) == polls:
        raise IssFormatError(f"every zcyc_securities poll failed: {'; '.join(failed)}")
    if failed:
        ctx.run.mark_partial("; ".join(failed))
    return {"polls": polls, "failed": len(failed), "stored": stored}


def poll_zcyc_securities(ctx: JobContext, http: Any) -> int:
    """One request -> changed rows; returns the number of rows written."""
    res = http.get_json(ZCYC_SECURITIES, params={"iss.meta": "off", "iss.only": "securities"})
    fetched_at = utc_now()
    rows = parse_zcyc_securities(res.data, fetched_at)
    with ctx.connection() as conn:
        cap = record_capture(
            conn,
            ctx.run.id,
            SOURCE,
            ENDPOINT,
            None,
            res.response,
            PARSER_VERSION,
            source_ts=max((r["source_ts"] for r in rows), default=None),
            fetched_at=fetched_at,
        )
        changed, unchanged = only_changed(
            conn, TABLE, rows, ["secid"], ZCYC_SECURITY_COMPARE, logger=ctx.logger
        )
        for r in changed:
            r["capture_id"] = cap
            r["run_id"] = ctx.run.id
        result = upsert_rows(conn, TABLE, changed, ["secid", "ts"], run_id=ctx.run.id)
        ctx.run.add_counts(result + UpsertResult(unchanged=unchanged))
    return result.changed
