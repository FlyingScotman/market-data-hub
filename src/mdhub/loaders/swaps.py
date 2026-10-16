"""Dealer swap runs -> ``md.curve_snapshot`` + ``md.curve_quote``.

``swaps_ratescalcs()`` (every 30 min 09:00-23:00 MSK on business days) fetches, GET only:

* ``ruonia_ois``, ``key_rate_swap``, ``cny_ruonia_xccy`` - editor pages (textarea),
* ``cny_futures_yields``, ``usd_futures_yields`` - from the ``snapshot`` JSON (source clock
  ``timestamp``, ``market_closed``).

``swaps_otc_monitor()`` (every 15 min 09:00-20:00 MSK on business days) fetches the OTC Monitor
``/api/bond/`` JSON once and parses ``ruonia_ois``, ``key_rate_swap``, ``cny_ruonia_xccy`` and
``cny_rusfar_ois`` from it (:mod:`mdhub.sources.otc_monitor`; source clock = latest
``date_update`` of the curve). A curve that fails validation is left out and the run ends
``partial``; the other curves are stored. All four failing -> ``failed``. Missing token ->
``skipped``; a rejected token (HTTP 401/403) -> ``failed`` (an active source must alert).

Dedup (both jobs): each payload is parsed first; a dataset whose normalized content hash
(:func:`mdhub.sources.ratescalcs.content_hash`, :func:`mdhub.sources.otc_monitor.content_hash`)
equals the latest stored snapshot of ``(source, dataset)`` - legacy-mapped history included -
is counted as ``unchanged`` and nothing is written - neither a snapshot nor the raw capture.
Changed content stores the raw capture (one per fetch, shared by the curves parsed from it) and
a new snapshot with its quotes (mid recomputed as (bid+ask)/2; mid-only quotes keep the quoted
mid with bid/ask NULL). Without credentials a job ends ``skipped``.

The sources are separate series and never merged (DESIGN §8 "Curve sources").
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Final

import httpx
import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.capture import record_capture
from mdhub.core.errors import JobSkipped, MdhubError
from mdhub.core.timeutil import MSK, utc_now
from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.sources import otc_monitor as otc
from mdhub.sources import ratescalcs as src

__all__ = ["DATASETS", "OTC_DATASETS", "store_curve", "swaps_otc_monitor", "swaps_ratescalcs"]

SOURCE: Final = "ratescalcs"
DATASETS: Final = (
    "ruonia_ois",
    "key_rate_swap",
    "cny_ruonia_xccy",
    "cny_futures_yields",
    "usd_futures_yields",
)

for _ds in DATASETS:
    register_dataset(
        DatasetSpec(
            name=f"ratescalcs_{_ds}",
            table="md.curve_snapshot",
            date_column="snapshot_date",
            calendar="MOEX_STOCK",
            frequency="event",
            max_lag_days=4,
            where=f"source = 'ratescalcs' AND dataset = '{_ds}'",
            description=f"Ratescalcs {_ds} snapshots (stored only when content changes)",
        )
    )

OTC_SOURCE: Final = "otc_monitor"
OTC_DATASETS: Final = tuple(spec.dataset for spec in otc.CURVES)

for _ds in OTC_DATASETS:
    register_dataset(
        DatasetSpec(
            name=f"otc_monitor_{_ds}",
            table="md.curve_snapshot",
            date_column="snapshot_date",
            calendar="MOEX_STOCK",
            frequency="event",
            max_lag_days=4,
            where=f"source = 'otc_monitor' AND dataset = '{_ds}'",
            description=f"OTC Monitor {_ds} snapshots (stored only when content changes)",
        )
    )


def latest_content_hash(conn: psycopg.Connection[Any], source: str, dataset: str) -> str | None:
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """SELECT content_sha256 FROM md.curve_snapshot
               WHERE source = %s AND dataset = %s
               ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (source, dataset),
        ).fetchone()
    return str(row[0]) if row else None


def store_curve(
    conn: psycopg.Connection[Any],
    curve: src.ParsedCurve,
    *,
    source: str,
    fetched_at: datetime,
    capture_id: int | None,
    run_id: int | None,
    content_sha256: str,
    parser_version: str = src.PARSER_VERSION,
) -> int:
    """Insert one snapshot with its quotes; returns the snapshot id."""
    with conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            """
            INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at,
                snapshot_date, source_ts, market_closed, content_sha256, parser_version,
                label, meta, capture_id, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                source,
                curve.dataset,
                curve.quote_basis,
                fetched_at,
                fetched_at.astimezone(MSK).date(),
                curve.source_ts,
                curve.market_closed,
                content_sha256,
                parser_version,
                curve.label,
                Jsonb(dict(curve.meta)) if curve.meta else None,
                capture_id,
                run_id,
            ),
        ).fetchone()
    assert row is not None
    snapshot_id = int(row[0])
    quotes = [
        {
            "snapshot_id": snapshot_id,
            "tenor": q.tenor,
            "position": q.position,
            "tenor_months": q.tenor_months,
            "tenor_days": q.tenor_days,
            "pillar_date": q.pillar_date,
            "bid_pct": q.bid_pct,
            "ask_pct": q.ask_pct,
            "mid_pct": q.mid_pct,
            "extra": dict(q.extra) if q.extra else None,
        }
        for q in curve.quotes
    ]
    upsert_rows(conn, "md.curve_quote", quotes, ["snapshot_id", "tenor"])
    return snapshot_id


def _store_if_changed(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    endpoint: str,
    resp: httpx.Response,
    curves: Sequence[src.ParsedCurve],
    fetched_at: datetime,
    *,
    source: str = SOURCE,
    parser_version: str = src.PARSER_VERSION,
    hash_fn: Callable[[src.ParsedCurve], str] = src.content_hash,
) -> dict[str, str]:
    """Write the capture + snapshots for curves whose content changed."""
    changed = [
        (c, h) for c in curves if (h := hash_fn(c)) != latest_content_hash(conn, source, c.dataset)
    ]
    status = {c.dataset: "unchanged" for c in curves}
    if not changed:
        ctx.run.add_counts(UpsertResult(unchanged=len(curves)))
        return status
    source_ts = max((c.source_ts for c in curves if c.source_ts is not None), default=None)
    cap = record_capture(
        conn,
        ctx.run.id,
        source,
        endpoint,
        None,
        resp,
        parser_version,
        source_ts=source_ts,
        fetched_at=fetched_at,
    )
    for curve, digest in changed:
        store_curve(
            conn,
            curve,
            source=source,
            fetched_at=fetched_at,
            capture_id=cap,
            run_id=ctx.run.id,
            content_sha256=digest,
            parser_version=parser_version,
        )
        status[curve.dataset] = f"stored ({len(curve.quotes)} quotes)"
    ctx.run.add_counts(UpsertResult(inserted=len(changed), unchanged=len(curves) - len(changed)))
    return status


@job("swaps_ratescalcs", "Ratescalcs RUONIA OIS, KRS, CNY xccy and futures-implied yields")
def swaps_ratescalcs(ctx: JobContext) -> dict[str, str]:
    user, password = ctx.settings.ratescalc_username, ctx.settings.ratescalc_password
    if user is None or password is None:
        raise JobSkipped("RATESCALC_USERNAME/RATESCALC_PASSWORD not configured")
    auth = httpx.BasicAuth(user.get_secret_value(), password.get_secret_value())
    status: dict[str, str] = {}
    failures: list[str] = []
    with ctx.http(auth=auth) as http:
        targets: list[tuple[str, str]] = [(p.dataset, p.path) for p in src.PAGES]
        targets.append(("snapshot", src.SNAPSHOT_PATH))
        for endpoint, path in targets:
            try:
                resp = src.fetch(http, path)
                fetched_at = utc_now()
                if endpoint == "snapshot":
                    curves = src.parse_snapshot(resp.content)
                else:
                    curves = [src.parse_textarea_page(resp.text, endpoint)]
                with ctx.connection() as conn:
                    status.update(_store_if_changed(ctx, conn, endpoint, resp, curves, fetched_at))
            except (MdhubError, psycopg.Error) as exc:
                # never include page bodies in errors (credentials may appear in page text)
                ctx.logger.error("swaps_ratescalcs %s failed: %s", endpoint, type(exc).__name__)
                failures.append(f"{endpoint}: {type(exc).__name__}: {str(exc)[:200]}")
                status[endpoint] = "failed"
    if failures and len(failures) == len(targets):
        raise MdhubError("swaps_ratescalcs: all pages failed: " + "; ".join(failures))
    if failures:
        ctx.run.mark_partial("; ".join(failures))
    return status


@job(
    "swaps_otc_monitor",
    "OTC Monitor RUONIA OIS, KRS, CNY xccy vs RUONIA and CNY OIS vs RUSFAR CNY (15-min polls)",
)
def swaps_otc_monitor(ctx: JobContext) -> dict[str, str]:
    token = ctx.settings.otc_monitor_token
    if token is None:
        raise JobSkipped("OTC_MONITOR_TOKEN not configured")
    with ctx.http() as http:
        try:
            res = otc.fetch(http, token.get_secret_value())
        except otc.OtcMonitorAuthError as exc:
            raise MdhubError(f"swaps_otc_monitor: {exc}") from None
    fetched_at = utc_now()
    parsed = otc.parse_payload(res.data)  # malformed payload -> FormatDriftError -> failed
    status = dict.fromkeys(parsed.failures, "failed")
    failures = [f"{ds}: {reason[:200]}" for ds, reason in parsed.failures.items()]
    for line in failures:
        ctx.logger.error("swaps_otc_monitor curve failed closed: %s", line)
    if not parsed.curves:
        raise MdhubError("swaps_otc_monitor: all curves failed: " + "; ".join(failures))
    with ctx.connection() as conn:
        status.update(
            _store_if_changed(
                ctx,
                conn,
                otc.ENDPOINT,
                res.response,
                parsed.curves,
                fetched_at,
                source=OTC_SOURCE,
                parser_version=otc.PARSER_VERSION,
                hash_fn=otc.content_hash,
            )
        )
    if failures:
        ctx.run.mark_partial("; ".join(failures))
    return {ds: status[ds] for ds in OTC_DATASETS if ds in status}
