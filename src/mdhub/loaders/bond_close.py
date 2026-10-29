"""Closing bid/ask of bonds at the close cut -> ``md.bond_close_quote`` (derived, no HTTP).

Job ``bond_close_quote`` (after the 19:00 ``bonds_intraday`` poll, which - 15 minutes delayed -
carries the quotes up to ~18:45): for every (secid, board) the latest stored row at or before
the cut (``MDHUB_BOND_CLOSE_CUT``, default 18:45 MSK) of the trade date:

* ``basis='bonds_marketdata'`` from ``md.bond_quote_intraday`` (all boards; ``BID``/``OFFER``
  in % of face),
* ``basis='zcyc_securities'`` from ``md.zcyc_security_quote`` (~29 OFZ-PD, near realtime; also
  the bid/ask yields), board ``ZCYC`` (the KBD block, not a trading board).

Both tables store changed rows only, so the latest row at or before the cut is the state at the
cut; ``quote_ts`` says how old it was. Only rows of the trade date are used (no forward fill
from an earlier day); a row with neither bid nor ask is not written. ``days_back`` /
``date_from``-``date_to`` rebuild past days from the stored intraday rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from psycopg.rows import dict_row

from mdhub.config import parse_hhmm
from mdhub.core.timeutil import MSK
from mdhub.core.upsert import upsert_rows
from mdhub.datasets import DatasetSpec, register_dataset
from mdhub.jobs import JobContext, job
from mdhub.loaders.fx_moex import resolve_dates

__all__ = ["bond_close_quote", "close_cut"]

CALENDAR: Final = "MOEX_STOCK"
KEY: Final = ["trade_date", "secid", "board", "basis"]
ZCYC_BOARD: Final = "ZCYC"

register_dataset(
    DatasetSpec(
        name="bond_close_quote",
        table="md.bond_close_quote",
        date_column="trade_date",
        calendar=CALENDAR,
        frequency="business_daily",
        max_lag_days=1,
        min_rows_per_day=20,
        description="Closing bid/ask of bonds at MDHUB_BOND_CLOSE_CUT (derived from intraday)",
    )
)

_SQL: Final = """
SELECT DISTINCT ON (q.secid, q.board)
       q.secid, q.board, q.ts AS quote_ts, q.bid AS close_bid_pct, q.offer AS close_ask_pct,
       NULL::numeric AS bid_yield_pct, NULL::numeric AS ask_yield_pct, q.source_ts, q.latency
FROM md.bond_quote_intraday q
WHERE q.ts >= %(day_start)s AND q.ts <= %(cut)s
ORDER BY q.secid, q.board, q.ts DESC
"""
_ZCYC_SQL: Final = """
SELECT DISTINCT ON (z.secid)
       z.secid, %(zcyc_board)s AS board, z.ts AS quote_ts, z.bid_price_pct AS close_bid_pct,
       z.ask_price_pct AS close_ask_pct, z.bid_yield_pct, z.ask_yield_pct, z.source_ts, z.latency
FROM md.zcyc_security_quote z
WHERE z.ts >= %(day_start)s AND z.ts <= %(cut)s AND z.trade_date = %(day)s
ORDER BY z.secid, z.ts DESC
"""


def close_cut(day: date, hhmm: str) -> datetime:
    """The close cut of ``day`` (MSK)."""
    h, m = parse_hhmm(hhmm)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=MSK)


@job("bond_close_quote", "Closing bid/ask of bonds at the close cut (from stored intraday rows)")
def bond_close_quote(
    ctx: JobContext,
    *,
    days_back: list[int] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    days = resolve_dates(
        ctx,
        CALENDAR,
        days_back=days_back,
        date_from=date_from,
        date_to=date_to,
        default_days_back=(0,),
    )
    written = 0
    for d in days:
        cut = close_cut(d, ctx.settings.bond_close_cut)
        params = {
            "day": d,
            "day_start": datetime(d.year, d.month, d.day, tzinfo=MSK),
            "cut": cut,
            "zcyc_board": ZCYC_BOARD,
        }
        with ctx.connection() as conn:
            rows: list[dict[str, Any]] = []
            with conn.cursor(row_factory=dict_row) as cur:
                for basis, query in (("bonds_marketdata", _SQL), ("zcyc_securities", _ZCYC_SQL)):
                    for r in cur.execute(query, params).fetchall():
                        if r["close_bid_pct"] is None and r["close_ask_pct"] is None:
                            continue
                        rows.append(
                            {
                                **r,
                                "trade_date": d,
                                "basis": basis,
                                "cut_ts": cut,
                                "run_id": ctx.run.id,
                            }
                        )
            if not rows:
                ctx.run.mark_partial(f"no bond quotes stored on {d} before {cut:%H:%M} MSK")
                continue
            result = upsert_rows(conn, "md.bond_close_quote", rows, KEY, run_id=ctx.run.id)
            ctx.run.add_counts(result)
            written += len(rows)
    return {"days": len(days), "rows": written}
