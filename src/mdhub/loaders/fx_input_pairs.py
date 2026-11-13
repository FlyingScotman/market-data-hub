"""Capture and retain timestamp-aligned, indicative FX/futures input observations.

These are input facts, not calculated curves, execution prices, or a pricing quality policy.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from psycopg.types.json import Jsonb

from mdhub.core.capture import load_capture_payload, record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpError
from mdhub.core.timeutil import utc_now
from mdhub.core.upsert import UpsertResult
from mdhub.db.pool import DictConnection
from mdhub.jobs import JobContext, job
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex, to_wire
from mdhub.sources import fx_pair_inputs as inputs
from mdhub.sources import ntpro

CNY_CAPTURE_WINDOW: Final = timedelta(minutes=10)
FUTURES_SOURCE: Final = "moex_apim"
FUTURES_ENDPOINT: Final = "intraday:futures_apim_marketdata"
CNY_ENDPOINT: Final = "intraday:fx_cets_marketdata"


def _capture(
    conn: DictConnection, source: str, endpoint: str, at: datetime, window: timedelta
) -> dict[str, Any] | None:
    """Newest committed capture inside one fixed window; never try a second source or row."""
    return conn.execute(
        """SELECT id, fetched_at, sha256 FROM ops.raw_capture
           WHERE source = %s AND endpoint = %s AND fetched_at <= %s AND fetched_at >= %s
           ORDER BY fetched_at DESC, id DESC LIMIT 1""",
        (source, endpoint, at, at - window),
    ).fetchone()


def _payload(conn: DictConnection, capture: dict[str, Any]) -> dict[str, Any]:
    """Read and verify the retained source payload before interpreting it."""
    value = json.loads(load_capture_payload(conn, capture["id"]), parse_float=Decimal)
    if not isinstance(value, dict):
        raise ValueError("capture is not an object")
    return value


def _cny(conn: DictConnection, known_at: datetime) -> tuple[inputs.Spot, dict[str, Any] | None]:
    capture = _capture(conn, "moex_iss", CNY_ENDPOINT, known_at, CNY_CAPTURE_WINDOW)
    if capture is None:
        return inputs.Spot(
            "CNY",
            "moex_iss",
            "CNYRUB_TOM",
            "last",
            reasons=("spot_capture_missing", "spot_value_date_unknown"),
        ), None
    try:
        spot = inputs.parse_cny_spot(_payload(conn, capture))
    except (FormatDriftError, ValueError, KeyError, OSError, EOFError, zlib.error):
        spot = inputs.Spot(
            "CNY",
            "moex_iss",
            "CNYRUB_TOM",
            "last",
            reasons=("spot_capture_invalid", "spot_value_date_unknown"),
        )
    return spot, capture


def _ntpro_spot(currency: str, quote: ntpro.Quote | None, error: str | None) -> inputs.Spot:
    if quote is None:
        return inputs.Spot(
            currency,
            ntpro.SOURCE,
            ntpro.INSTRUMENTS[currency],
            "indicative_bid_offer",
            reasons=(error or "spot_missing", "spot_value_date_unknown"),
        )
    return inputs.Spot(
        currency,
        ntpro.SOURCE,
        quote.instrument,
        "indicative_bid_offer",
        quote_ts=quote.quote_ts,
        bid=quote.bid,
        offer=quote.offer,
        reasons=(*quote.reasons, "spot_value_date_unknown"),
    )


def _group(
    conn: DictConnection,
    run_id: int,
    known_at: datetime,
    spot: inputs.Spot,
    capture: dict[str, Any] | None,
) -> dict[str, Any]:
    """Materialize one four-node group from the transaction's actual visible snapshot."""
    spot_doc = asdict(spot)
    # Existing MOEX collectors stamp raw captures after parsing; the exact network receipt
    # is not stored. NTPro is stamped immediately on return from the HTTP client.
    spot_doc["received_at"] = (
        capture["fetched_at"] if capture and spot.source == ntpro.SOURCE else None
    )
    spot_doc["captured_at"] = capture["fetched_at"] if capture else None
    spot_doc["capture_sha256"] = capture["sha256"] if capture else None
    spot_doc["clock_semantics"] = (
        "ntpro_creation_datetime" if spot.source == ntpro.SOURCE else "moex_last_trade_time"
    )
    spot_doc["source_variant"] = (
        "public_indicative" if spot.source == ntpro.SOURCE else "iss_public"
    )
    reasons = list(spot.reasons)
    if spot.quote_ts is not None and (capture is None or spot.quote_ts > capture["fetched_at"]):
        reasons.append("spot_time_after_capture")
    if capture is not None and capture["fetched_at"] > known_at:
        reasons.append("spot_capture_after_known_at")
    spot_doc["reasons"] = sorted(set(reasons))

    if spot.quote_ts is None or {"spot_time_after_capture", "spot_capture_after_known_at"} & set(
        reasons
    ):
        nodes = [inputs._empty_node(i, None, "spot_time_unavailable") for i in range(1, 5)]
    else:
        catalog = conn.execute(
            """SELECT c.*, raw.sha256 AS definition_capture_sha256,
                      raw.run_id AS definition_capture_run_id
               FROM ref.futures_contract_known_at(%s) AS c
               LEFT JOIN ops.raw_capture AS raw ON raw.id = c.capture_id
                                                 AND raw.fetched_at <= %s""",
            (spot.quote_ts, spot.quote_ts),
        ).fetchall()
        future_capture = _capture(
            conn, FUTURES_SOURCE, FUTURES_ENDPOINT, spot.quote_ts, inputs.FUTURE_CAPTURE_WINDOW
        )
        payload = None
        invalid = False
        if future_capture:
            try:
                payload = _payload(conn, future_capture)
            except (ValueError, KeyError, OSError, EOFError, zlib.error):
                invalid = True
        nodes = inputs.match_futures(
            spot.currency,
            spot.quote_ts,
            catalog,
            payload,
            captured_at=future_capture["fetched_at"] if future_capture else None,
            capture_sha256=future_capture["sha256"] if future_capture else None,
        )
        if invalid:
            for node in nodes:
                if node["reasons"] == ["future_capture_missing"]:
                    node["reasons"] = ["future_capture_invalid"]
                    assert future_capture is not None
                    node["captured_at"] = future_capture["fetched_at"]
                    node["capture_sha256"] = future_capture["sha256"]

    observed = sum(not node["reasons"] for node in nodes)
    spot_observed = (
        spot.quote_ts is not None
        and capture is not None
        and spot.quote_ts <= capture["fetched_at"] <= known_at
        and (
            (spot.quote_kind == "last" and spot.last is not None and spot.last > 0)
            or (
                spot.quote_kind == "indicative_bid_offer"
                and spot.bid is not None
                and spot.offer is not None
                and 0 < spot.bid <= spot.offer
            )
        )
    )
    return {
        "run_id": run_id,
        "currency": spot.currency,
        "known_at": known_at,
        "future_source": FUTURES_SOURCE,
        "future_source_variant": "apim_marketdata",
        "spot": spot_doc,
        "nodes": nodes,
        "coverage": {
            "expected_nodes": 4,
            "observed_nodes": observed,
            "spot_observed": spot_observed,
            "all_four_observed": spot_observed and observed == 4,
        },
    }


@job("fx_input_pairs", "Capture timestamp-aligned indicative FX and futures input facts")
def fx_input_pairs(ctx: JobContext) -> dict[str, Any]:
    quotes: dict[str, ntpro.Quote] = {}
    capture_id: int | None = None
    error: str | None = None
    try:
        with ctx.http(max_retries=0) as http:
            response = http.get(ntpro.URL)
        received_at = utc_now()
        with ctx.connection() as conn:
            capture_id = record_capture(
                conn,
                ctx.run.id,
                ntpro.SOURCE,
                ntpro.ENDPOINT,
                None,
                response,
                ntpro.PARSER_VERSION,
                fetched_at=received_at,
            )
    except HttpError:
        error = "spot_source_unavailable"

    # The first SELECT acquires a single MVCC snapshot. Every selected row was already committed
    # and visible at this instant, even when a concurrent transaction used an earlier fetched_at.
    with ctx.connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        clock = conn.execute("SELECT clock_timestamp() AS known_at").fetchone()
        assert clock is not None
        known_at = clock["known_at"]
        ntpro_capture = None
        if capture_id is not None:
            ntpro_capture = conn.execute(
                "SELECT id, fetched_at, sha256 FROM ops.raw_capture WHERE id = %s", (capture_id,)
            ).fetchone()
            if ntpro_capture is None:
                error = "spot_capture_missing"
            else:
                try:
                    quotes = ntpro.parse_snapshot(load_capture_payload(conn, capture_id))
                except (FormatDriftError, ValueError, KeyError, OSError, EOFError, zlib.error):
                    error = "spot_capture_invalid"
        if error:
            ctx.run.mark_partial(error)
        cny, cny_capture = _cny(conn, known_at)
        spots = [(_ntpro_spot(c, quotes.get(c), error), ntpro_capture) for c in ("USD", "EUR")]
        spots.append((cny, cny_capture))
        groups = [_group(conn, ctx.run.id, known_at, spot, capture) for spot, capture in spots]
        for group in groups:
            wire = to_wire(group)
            digest = sha256_hex(canonical_bytes(wire))
            conn.execute(
                "INSERT INTO md.fx_input_pair_group "
                "(run_id, currency, known_at, document, content_sha256) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ctx.run.id, group["currency"], known_at, Jsonb(wire), digest),
            )
        ctx.run.add_counts(UpsertResult(inserted=len(groups)))
        if any(group["coverage"]["observed_nodes"] < 4 for group in groups):
            ctx.run.mark_partial("fx_input_pairs_incomplete")
    return {"groups": len(groups)}
