"""Market Snapshot Contract 1.2 (additive): RUSFAR units, ``hub_ingested_at`` of fixings and
the MOEX linker / WAP analytics in ``bond_quotes.source_analytics``."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import DictRow

from mdhub.core.timeutil import MSK, utc_now
from mdhub.db.pool import Database
from mdhub.snapshot.market import (
    RUSFAR_INDEX_SERIES,
    RUSFAR_RATE_SERIES,
    rusfar_unit,
)
from mdhub.snapshot.runner import publish_market_snapshot
from mdhub.snapshot.schema import SCHEMA_VERSION, validate_bundle
from mdhub.sources.moex_market import INDEX_GROUPS
from test_api_seed import seed

Conn = psycopg.Connection[DictRow]
DAY = date(2026, 10, 15)


def _bundle(conn: Conn, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    doc: dict[str, Any] = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    return doc


def test_rusfar_series_lists() -> None:
    assert SCHEMA_VERSION == "market_snapshot/1.2"
    assert set(RUSFAR_RATE_SERIES) == set(INDEX_GROUPS["rusfar"]) | set(INDEX_GROUPS["rusfarrt"])
    assert RUSFAR_INDEX_SERIES == ("RUSFARIND",)
    assert rusfar_unit("RUSFAR") == "pct" and rusfar_unit("RUSFARCNY") == "pct"
    assert rusfar_unit("RUSFARIND") == "index"
    assert rusfar_unit("RUSFARNEW") is None


def test_market_snapshot_1_2_fields(conn: Conn, db: Database) -> None:
    seed(conn)
    conn.execute(
        """
        INSERT INTO md.index_eod (trade_date, secid, close, basis, ingested_at, updated_at)
        VALUES ('2026-10-15', 'RUSFARIND', 2508.17, 'history', '2026-10-15 16:00Z',
                '2026-10-15 17:30Z'),
               ('2026-10-15', 'RUSFARNEW', 1.23, 'history', '2026-10-15 16:00Z', NULL);
        UPDATE md.bond_eod_yield SET icpi = 1.2345, ir = 7.10, bei = 5.9, cbr = 4.0,
               duration_waprice_days = 1001, yield_last_coupon_pct = 15.25
         WHERE secid = 'SU26242RMFS0' AND trade_date = '2026-10-15';
        UPDATE md.bond_eod SET yield_last_coupon_pct = 17.5
         WHERE secid = 'RU000A105X12' AND trade_date = '2026-10-15';
        """
    )
    conn.commit()
    nxt = DAY + timedelta(days=1)
    out = publish_market_snapshot(
        db,
        cut_type="eod_final",
        business_date=DAY,
        cut_ts=datetime(nxt.year, nxt.month, nxt.day, 8, 0, tzinfo=MSK),
        known_at=utc_now(),
    )
    doc = _bundle(conn, out.market.id)
    validate_bundle(doc)
    assert doc["schema_version"] == "market_snapshot/1.2"

    fixings = {(f["index_code"], f["fixing_date"]): f for f in doc["data"]["fixings"]}
    ind = fixings[("RUSFARIND", "2026-10-15")]
    assert ind["unit"] == "index" and ind["value"] == "2508.17"
    assert ind["hub_ingested_at"] == "2026-10-15T17:30:00Z"  # version time (updated_at)
    assert fixings[("RUSFAR", "2026-10-15")]["unit"] == "pct"
    assert ("RUSFARNEW", "2026-10-15") not in fixings
    excluded = [e for e in doc["excluded"] if e["dataset"] == "fixings"]
    assert excluded == [
        {
            "dataset": "fixings",
            "key": {"fixing_date": "2026-10-15", "index_code": "RUSFARNEW"},
            "reason": "rusfar_series_unit_unknown",
        }
    ]
    ruonia = fixings[("RUONIA", "2026-10-14")]
    assert ruonia["hub_ingested_at"] == "2026-10-15T12:00:00Z"
    assert ruonia["published_at"] is None  # never invented
    assert all(f["hub_ingested_at"] for f in doc["data"]["fixings"])

    quotes = {(q["secid"], q["board"]): q for q in doc["data"]["bond_quotes"]}
    sa = quotes[("SU26242RMFS0", "TQOB")]["source_analytics"]
    assert (sa["icpi"], sa["ir"], sa["bei"], sa["cbr"]) == ("1.2345", "7.10", "5.9", "4.0")
    assert sa["duration_waprice_days"] == "1001" and sa["yield_last_coupon_pct"] == "15.25"
    # yields endpoint first, history row as fallback (like yield_to_offer_pct)
    floater = quotes[("RU000A105X12", "TQCB")]["source_analytics"]
    assert floater["yield_last_coupon_pct"] == "17.5" and floater["icpi"] is None
