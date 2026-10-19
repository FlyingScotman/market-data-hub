"""Golden fixtures of the Market Snapshot Contract v1 (no tests here).

Source: a subset of the seed database (public MOEX / CBR / ECB / dealer-curve data) for ten
bonds and the business date 2026-10-15, stored as ``tests/fixtures/snapshot/source_rows.json.gz``
(row images per table, as ``to_jsonb``). The golden bundles in
``schemas/market_snapshot/v1/fixtures/`` are rebuilt from those rows by the hub's builders
(``tests/test_snapshot_golden.py``); they change only in a deliberate, reviewed change:

* extract the source rows again (needs a seed database, read-only)::

    uv run python tests/snapshot_golden.py extract "host=... dbname=mdhub user=postgres"

* rebuild the golden bundles (pytest provides the throw-away PostgreSQL)::

    MDHUB_REGEN_GOLDEN=1 uv run pytest tests/test_snapshot_golden.py
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE: Final = ROOT / "tests" / "fixtures" / "snapshot" / "source_rows.json.gz"
GOLDEN_DIR: Final = ROOT / "schemas" / "market_snapshot" / "v1" / "fixtures"

DAY: Final = date(2026, 10, 15)
GOLDEN_FIXING_LOOKBACK_DAYS: Final = 14
"""The golden market cut uses a short fixing window (recorded in its ``parameters``) so the
fixture stays reviewable; production EOD cuts default to 400 days."""

GOLDEN_SECIDS: Final = {
    "SU26238RMFS4": "OFZ-PD, fixed bullet (2041)",
    "SU29008RMFS8": "OFZ-PK, floater on RUONIA",
    "SU52005RMFS4": "OFZ-IN, inflation-indexed face",
    "SU46020RMFS2": "OFZ-AD, fixed amortizer",
    "RU000A109XR1": "corporate floater on the CBR key rate",
    "RU000A0JXQ44": "corporate fixed with a scheduled put offer (RZD)",
    "RU000A10BRM5": "corporate fixed amortizer",
    "RU000A100GW5": "perpetual subordinated (Alfa-Bank)",
    "RU000A1034P7": "perpetual subordinated (VTB)",
    "RU000A10GCT1": "corporate with coupon_type unknown",
}

# table -> WHERE clause (parameters: secids, day, lo, hi, cal_lo, cal_hi); order columns
_TABLES: Final[list[tuple[str, str, str]]] = [
    (
        "ref.issuer",
        "issuer_id IN (SELECT issuer_id FROM ref.bond WHERE secid = ANY(%(secids)s))",
        "issuer_id",
    ),
    ("ref.bond", "secid = ANY(%(secids)s)", "secid"),
    ("ref.bond_board", "secid = ANY(%(secids)s)", "secid, board"),
    ("ref.bond_coupon", "secid = ANY(%(secids)s)", "secid, coupon_no"),
    ("ref.bond_amortization", "secid = ANY(%(secids)s)", "secid, amort_date, data_source"),
    ("ref.bond_offer", "secid = ANY(%(secids)s)", "secid, source, offer_key"),
    ("ref.bond_rate_formula", "secid = ANY(%(secids)s)", "secid, source"),
    ("ref.cbr_meeting", "true", "meeting_date"),
    ("ref.calendar_day", "day BETWEEN %(cal_lo)s AND %(cal_hi)s", "calendar, day, source"),
    ("md.bond_eod", "trade_date = %(day)s AND secid = ANY(%(secids)s)", "secid, board"),
    ("md.bond_eod_yield", "trade_date = %(day)s AND secid = ANY(%(secids)s)", "secid, board"),
    (
        "md.bond_quote_intraday",
        "ts >= %(day)s::timestamp AT TIME ZONE 'Europe/Moscow' "
        "AND ts < (%(day)s::date + 1)::timestamp AT TIME ZONE 'Europe/Moscow' "
        "AND secid = ANY(%(secids)s)",
        "secid, board, ts",
    ),
    ("md.curve_snapshot", "snapshot_date = %(day)s", "id"),
    (
        "md.curve_quote",
        "snapshot_id IN (SELECT id FROM md.curve_snapshot WHERE snapshot_date = %(day)s)",
        "snapshot_id, position, tenor",
    ),
    ("md.zcyc_snapshot", "trade_date = %(day)s", "trade_time"),
    ("md.fixing", "fixing_date BETWEEN %(lo)s AND %(hi)s", "index_code, source, fixing_date"),
    (
        "md.index_eod",
        "trade_date BETWEEN %(lo)s AND %(day)s AND secid LIKE 'RUSFAR%%'",
        "secid, trade_date",
    ),
    ("md.fx_official", "effective_date BETWEEN %(fx_lo)s AND %(hi_fx)s", "effective_date, ccy"),
    ("md.fx_eod", "trade_date = %(day)s", "secid, board"),
    (
        "ref.fx_instrument",
        "(secid, board) IN (SELECT secid, board FROM md.fx_eod WHERE trade_date = %(day)s)",
        "secid, board",
    ),
    ("md.fx_indicative", "trade_date = %(day)s", "clearing, ccy_pair"),
    ("md.ecb_fx", "rate_date BETWEEN %(fx_lo)s AND %(day)s", "rate_date, ccy"),
]


def _params() -> dict[str, Any]:
    from datetime import timedelta

    return {
        "secids": sorted(GOLDEN_SECIDS),
        "day": DAY,
        "lo": DAY - timedelta(days=GOLDEN_FIXING_LOOKBACK_DAYS),
        "hi": DAY + timedelta(days=7),
        "fx_lo": DAY - timedelta(days=7),
        "hi_fx": DAY + timedelta(days=7),
        "cal_lo": date(2026, 1, 1),
        "cal_hi": date(2027, 12, 31),
    }


def extract(conninfo: str) -> dict[str, Any]:
    """Read the source rows from a seed database (read-only)."""
    out: dict[str, Any] = {"day": DAY.isoformat(), "secids": GOLDEN_SECIDS, "tables": {}}
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET TimeZone = 'UTC'")
        for table, where, order in _TABLES:
            schema, name = table.split(".")
            query = sql.SQL("SELECT to_jsonb(t) AS j FROM {t} t WHERE ").format(
                t=sql.Identifier(schema, name)
            ) + sql.SQL(where + " ORDER BY " + order)  # type: ignore[operator]
            rows = [r["j"] for r in conn.execute(query, _params()).fetchall()]
            for r in rows:
                if "capture_id" in r:
                    r["capture_id"] = None  # raw captures are not part of the fixture
            out["tables"][table] = rows
    return out


def load_source() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(gzip.decompress(SOURCE.read_bytes()))
    return data


def _version_time(table: str, row: dict[str, Any]) -> str | None:
    vals = [row.get(c) for c in ("updated_at", "ingested_at", "known_at", "fetched_at")]
    if table == "ref.calendar_day":
        vals = [row.get("ingested_at") or row.get("known_at")]
    present = [v for v in vals if v]
    return max(present) if present else None


def known_at_of(source: dict[str, Any]) -> datetime:
    """The replay instant: the latest version time of any source row (all rows known)."""
    latest = max(
        datetime.fromisoformat(t)
        for table, rows in source["tables"].items()
        for r in rows
        if (t := _version_time(table, r)) is not None
    )
    return latest.replace(microsecond=0) + __import__("datetime").timedelta(seconds=1)


def load(conn: psycopg.Connection[Any], source: dict[str, Any]) -> None:
    """Insert the source rows into a freshly migrated database (caller commits)."""
    conn.execute("SET session_replication_role = replica")  # no FK checks / triggers on load
    for table, rows in source["tables"].items():
        if not rows:
            continue
        schema, name = table.split(".")
        if table == "ref.calendar_day":
            for r in rows:  # seeds before migration 0500: version time = known_at
                r.setdefault("ingested_at", r["known_at"])
                r.setdefault("is_deleted", False)
        cols = list(rows[0])
        ident = sql.Identifier(schema, name)
        overriding = sql.SQL(" OVERRIDING SYSTEM VALUE" if table == "md.curve_snapshot" else "")
        conn.execute(
            sql.SQL("INSERT INTO {t} ({c})").format(
                t=ident, c=sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            )
            + overriding
            + sql.SQL(" SELECT {c} FROM jsonb_populate_recordset(NULL::{t}, %s::jsonb)").format(
                t=ident, c=sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            ),
            (json.dumps(rows),),
        )
    conn.execute("SET session_replication_role = origin")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "extract":
        data = extract(argv[2])
        SOURCE.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        SOURCE.write_bytes(gzip.compress(raw.encode("utf-8"), compresslevel=9, mtime=0))
        counts = {t: len(r) for t, r in data["tables"].items()}
        print(json.dumps(counts, indent=1))
        print(f"{SOURCE} ({SOURCE.stat().st_size} bytes)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
