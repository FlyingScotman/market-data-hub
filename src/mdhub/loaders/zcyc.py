"""MOEX zero-coupon yield curve (KBD / G-curve) -> ``md.zcyc_snapshot``.

``/iss/engines/stock/zcyc.json?iss.only=params,yearyields`` returns the latest publication
(parameters B1..B3, T1, G1..G9 and the standard-tenor yields, one tradetime); with
``date=D`` it returns the last publication of day D.

Every payload is captured. Validation (DESIGN §2 fail closed): params and yields must carry
the same publication clock (one retry after a short pause on a skew) and the standard
yields must be reproduced from the parameters with the KBD formula within 0.01 bp
(:func:`mdhub.sources.moex_market.check_zcyc`). A failing snapshot is not stored: it is
recorded in ``md.zcyc_reject`` and the run ends ``partial``. The check result is stored with
each accepted snapshot (``check_max_error_bp``).

Modes: ``intraday`` (latest publication; identical publications dedup on the key
(trade_date, trade_time)), ``eod`` (``days_back`` default ``[0]``) and ``final`` (default
``[1]``) read ``?date=`` and flag the row ``is_eod`` (one per day). ``day`` /
``date_from`` / ``date_to`` load history (EOD) for every MOEX_STOCK business day.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Final

from psycopg.types.json import Jsonb

from mdhub.core.capture import record_capture
from mdhub.core.gaps import close_gap, record_no_data
from mdhub.core.http import HttpClient
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.bonds_eod import resolve_trade_dates, run_dates
from mdhub.sources.moex_market import (
    PARSER_VERSION,
    ZCYC_PATH,
    ZCYC_TOLERANCE_BP,
    ZcycClockSkewError,
    ZcycSnapshot,
    check_zcyc,
    parse_zcyc,
)

__all__ = ["load_zcyc"]

KEY: Final = ["trade_date", "trade_time"]
SOURCE: Final = "moex_iss"
DATASET: Final = "zcyc"
SKEW_RETRY_SLEEP: Final = 5.0

register_dataset(
    DatasetSpec(
        name=DATASET,
        table="md.zcyc_snapshot",
        date_column="trade_date",
        calendar="MOEX_STOCK",
        frequency="business_daily",
        max_lag_days=1,
        backfill_job="zcyc",
        history_start=date(2014, 1, 6),  # first ISS publication
        min_rows_per_day=1,
        description="MOEX KBD (G-curve) parameters + standard yields, EOD publication",
        where="is_eod",
    )
)


def _fetch(
    ctx: JobContext,
    http: HttpClient,
    conn: Any,
    trade_date: date | None,
    sleep: Callable[[float], None],
) -> tuple[ZcycSnapshot | None, int, ZcycClockSkewError | None]:
    """``(snapshot or None, capture_id, clock-skew error after the retry or None)``."""
    params: dict[str, Any] = {"iss.meta": "off", "iss.only": "params,yearyields"}
    if trade_date is not None:
        params["date"] = trade_date.isoformat()
    skew: ZcycClockSkewError | None = None
    cap = 0
    for attempt in range(2):
        res = http.get_json(ZCYC_PATH, params=params)
        cap = record_capture(conn, ctx.run.id, SOURCE, "zcyc", None, res.response, PARSER_VERSION)
        try:
            snap = parse_zcyc(res.data, trade_date=trade_date)
        except ZcycClockSkewError as exc:
            skew = exc
            if attempt == 0:
                ctx.logger.warning("zcyc: params/yields clock skew, retrying: %s", exc)
                sleep(SKEW_RETRY_SLEEP)
            continue
        if snap is not None:
            conn.execute(
                "UPDATE ops.raw_capture SET source_ts = %s WHERE id = %s", (snap.source_ts, cap)
            )
        return snap, cap, None
    return None, cap, skew


def _reject(
    conn: Any,
    ctx: JobContext,
    cap: int,
    snap: ZcycSnapshot | None,
    err: Decimal | None,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO md.zcyc_reject
            (capture_id, trade_date, trade_time, max_error_bp, reason, run_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (capture_id) DO NOTHING
        """,
        (
            cap,
            snap.trade_date if snap else None,
            snap.trade_time if snap else None,
            err,
            reason,
            ctx.run.id,
        ),
    )


def load_zcyc(
    ctx: JobContext,
    http: HttpClient,
    trade_date: date | None,
    *,
    record_gap: bool = True,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Fetch, validate and store one publication (``trade_date=None``: latest, intraday)."""
    is_eod = trade_date is not None
    with ctx.connection() as conn:
        snap, cap, skew = _fetch(ctx, http, conn, trade_date, sleep or time.sleep)
        if skew is not None:
            _reject(conn, ctx, cap, None, None, str(skew))
            ctx.run.mark_partial(f"zcyc {trade_date or 'latest'}: {skew}")
            return {"rejected": str(skew)}
        if snap is None:
            note = f"ISS zcyc returned no curve for {trade_date or 'latest'}"
            ctx.logger.warning(note)
            if trade_date is not None and record_gap:
                record_no_data(conn, DATASET, trade_date, note, today=ctx.today())
            return {"no_data": True}
        check = check_zcyc(snap)
        if not check.passed:
            reason = (
                f"standard yields not reproduced from params: max error {check.max_error_bp} bp "
                f">= {ZCYC_TOLERANCE_BP} bp"
            )
            _reject(conn, ctx, cap, snap, check.max_error_bp, reason)
            ctx.logger.error("zcyc %s %s rejected: %s", snap.trade_date, snap.trade_time, reason)
            ctx.run.mark_partial(f"zcyc {snap.trade_date} {snap.trade_time}: {reason}")
            return {"rejected": reason, "trade_date": snap.trade_date.isoformat()}
        row = {
            "trade_date": snap.trade_date,
            "trade_time": snap.trade_time,
            "source_ts": snap.source_ts,
            "params": snap.params_json(),
            "standard_yields": Jsonb(snap.yields_json()),
            "check_max_error_bp": check.max_error_bp,
            "check_tolerance_bp": ZCYC_TOLERANCE_BP,
            "capture_id": cap,
            "run_id": ctx.run.id,
        }
        if is_eod:
            row["is_eod"] = True
            conn.execute(
                """
                UPDATE md.zcyc_snapshot SET is_eod = false
                WHERE trade_date = %s AND trade_time <> %s AND is_eod
                """,
                (snap.trade_date, snap.trade_time),
            )
            result = upsert_rows(conn, "md.zcyc_snapshot", [row], KEY, run_id=ctx.run.id)
            close_gap(conn, DATASET, snap.trade_date)
        else:
            # Never reset an EOD flag set by an eod/final run.
            result = upsert_rows(conn, "md.zcyc_snapshot", [row], KEY, run_id=ctx.run.id)
        ctx.run.add_counts(result)
    ctx.logger.info(
        "zcyc %s %s: max error %s bp, %s",
        snap.trade_date,
        snap.trade_time,
        check.max_error_bp,
        "new" if result.inserted else ("changed" if result.updated else "unchanged"),
    )
    return {
        "trade_date": snap.trade_date.isoformat(),
        "trade_time": snap.trade_time.isoformat(),
        "max_error_bp": str(check.max_error_bp),
        "inserted": result.inserted,
        "updated": result.updated,
    }


@job("zcyc", "MOEX KBD / G-curve parameters and standard yields (validated)")
def zcyc(
    ctx: JobContext,
    *,
    mode: str = "intraday",
    days_back: list[int] | None = None,
    day: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    if mode not in ("intraday", "eod", "final"):
        raise ValueError(f"zcyc: unknown mode {mode!r}")
    explicit = day is not None or date_from is not None or date_to is not None
    with ctx.http(base_url=ISS_BASE_URL) as http:
        if mode == "intraday" and not explicit:
            return load_zcyc(ctx, http, None)
        dates = resolve_trade_dates(
            ctx,
            days_back=days_back,
            default_days_back=[1] if mode == "final" else [0],
            day=day,
            date_from=date_from,
            date_to=date_to,
        )
        if not dates:
            ctx.run.mark_skipped("no business dates to load")
            return {}
        return run_dates(ctx, dates, lambda d: load_zcyc(ctx, http, d))
