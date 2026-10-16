"""MOEX bonds intraday snapshot (all boards) -> ``md.bond_quote_intraday`` (changed rows only).

One request per run: ``/engines/stock/markets/bonds/securities.json?iss.only=marketdata,
marketdata_yields`` (~3.5k rows, ~2 MB, not paged). The raw payload is always captured; a
quote row is written only when one of :data:`~mdhub.sources.moex_market.BOND_QUOTE_COMPARE`
differs from ``md.bond_quote_last`` for that (secid, board) - the last-state table is updated
in the same transaction. ``ts`` is the quote's UPDATETIME (MSK), ``source_ts`` its SYSTIME;
``latency`` is ``delayed`` for public ISS (freshest quote ~900 s behind SYSTIME).

If the same (secid, board, ts) changes again (e.g. yields recomputed without a new
UPDATETIME) the row is updated in place and the previous image goes to ``ops.row_history``.
"""

from __future__ import annotations

from typing import Any, Final

from mdhub.core.capture import record_capture
from mdhub.core.iss import ISS_BASE_URL
from mdhub.core.upsert import upsert_rows
from mdhub.jobs import JobContext, job
from mdhub.sources.moex_market import (
    BOND_QUOTE_COMPARE,
    BONDS_MARKETDATA_PATH,
    PARSER_VERSION,
    parse_bond_marketdata,
)

__all__ = ["changed_rows"]

SOURCE: Final = "moex_iss"
INTRADAY_ENDPOINT: Final = "intraday:bonds_marketdata"
"""``intraday:`` prefix: removed by raw-capture retention (maintenance); the capture FKs of
``md.bond_quote_intraday``/``md.bond_quote_last`` are ``ON DELETE SET NULL`` (0420)."""
CALENDAR: Final = "MOEX_STOCK"


def changed_rows(
    rows: list[dict[str, Any]], last: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rows whose compared quote fields differ from the last stored state (new keys too)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        prev = last.get((row["secid"], row["board"]))
        if prev is None or any(prev.get(f) != row[f] for f in BOND_QUOTE_COMPARE):
            out.append(row)
    return out


@job("bonds_intraday", "MOEX bonds marketdata snapshot (all boards), changed quotes only")
def bonds_intraday(ctx: JobContext) -> dict[str, Any]:
    with ctx.http(base_url=ISS_BASE_URL) as http:
        res = http.get_json(
            BONDS_MARKETDATA_PATH,
            params={"iss.meta": "off", "iss.only": "marketdata,marketdata_yields"},
        )
    with ctx.connection() as conn:
        snapshot = parse_bond_marketdata(
            res.data, is_business_day=lambda d: ctx.is_business_day(d, CALENDAR)
        )
        cap = record_capture(
            conn,
            ctx.run.id,
            SOURCE,
            INTRADAY_ENDPOINT,
            None,
            res.response,
            PARSER_VERSION,
            source_ts=snapshot.source_ts,
        )
        cols = ", ".join(BOND_QUOTE_COMPARE)
        last = {
            (r["secid"], r["board"]): dict(r)
            for r in conn.execute(f"SELECT secid, board, {cols} FROM md.bond_quote_last").fetchall()
        }
        changed = changed_rows(snapshot.rows, last)
        for row in changed:
            row["capture_id"] = cap
            row["run_id"] = ctx.run.id
        result = upsert_rows(
            conn, "md.bond_quote_intraday", changed, ["secid", "board", "ts"], run_id=ctx.run.id
        )
        ctx.run.add_counts(result)
        upsert_rows(conn, "md.bond_quote_last", changed, ["secid", "board"])
    ctx.logger.info(
        "bonds_intraday: %d rows in snapshot, %d changed (latency %s, min lag %s s)",
        len(snapshot.rows),
        len(changed),
        snapshot.latency,
        snapshot.min_lag_seconds,
    )
    return {
        "rows": len(snapshot.rows),
        "changed": len(changed),
        "latency": snapshot.latency,
        "min_lag_seconds": snapshot.min_lag_seconds,
    }
