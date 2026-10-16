"""MOEX APIM (``https://apim.moex.com/iss``): realtime ISS paths and AlgoPack datasets.

Auth: ``Authorization: Bearer <MOEX_API_KEY>`` header only (never in the URL). A token
without entitlement answers HTTP 200 ``text/html`` ("subscribers only") which
:meth:`~mdhub.core.http.HttpClient.get_json` raises as ``EntitlementError``; a missing or
invalid token answers 401.

Entitlement observed 2026-10-15/16: realtime FORTS marketdata (lag 0-1 s), AlgoPack ``fo``
tradestats/obstats and FUTOI; FX and bonds marketdata stay 15-min delayed.

AlgoPack FO (verified 2026-10-16):

* ``/datashop/algopack/fo/{tradestats,obstats}.json`` - all FORTS secids; ``latest=1`` = the
  last 5-minute interval only; ``date=`` one day (block ``data`` + ``data.cursor``, 1000 rows
  per page; tradestats ~30k rows/day, obstats ~100k rows/day). History from 2020-01-03.
* ``/datashop/algopack/fo/{tradestats,obstats}/{SECID}.json?from=&till=`` - one contract,
  date range (``date=`` is ignored there!), cursor-paged.
* ``/analyticalproducts/futoi/securities[/{ticker}].json`` - open interest by client group
  (``YUR``/``FIZ``) per asset ticker; ``latest=1`` / ``date=`` / ``from=&till=``; block
  ``futoi`` without a cursor (``start=`` paging until empty). History from 2020-01-03.
  (``/datashop/algopack/fo/futoi`` does not exist: 404.)

``tradedate + tradetime`` (MSK) is the published interval label; bars are published ~10-20 s
after it (``SYSTIME``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from mdhub.core.iss import IssFormatError, to_date, to_datetime, to_decimal, to_int, to_time
from mdhub.core.timeutil import MSK

__all__ = [
    "ALGOPACK_FO_OBSTATS",
    "ALGOPACK_FO_OBSTATS_SECID",
    "ALGOPACK_FO_TRADESTATS",
    "ALGOPACK_FO_TRADESTATS_SECID",
    "FUTOI_ALL",
    "FUTOI_TICKER",
    "OBSTATS_COLUMNS",
    "PARSER_VERSION",
    "TRADESTATS_COLUMNS",
    "apim_headers",
    "interval_ts",
    "parse_futoi",
    "parse_obstats",
    "parse_tradestats",
]

PARSER_VERSION: Final = "moex_apim/1"

ALGOPACK_FO_TRADESTATS: Final = "/datashop/algopack/fo/tradestats.json"
ALGOPACK_FO_OBSTATS: Final = "/datashop/algopack/fo/obstats.json"
ALGOPACK_FO_TRADESTATS_SECID: Final = "/datashop/algopack/fo/tradestats/{secid}.json"
ALGOPACK_FO_OBSTATS_SECID: Final = "/datashop/algopack/fo/obstats/{secid}.json"
FUTOI_ALL: Final = "/analyticalproducts/futoi/securities.json"
FUTOI_TICKER: Final = "/analyticalproducts/futoi/securities/{ticker}.json"


def apim_headers(api_key: str) -> dict[str, str]:
    """Request headers for APIM. ``api_key`` comes from ``Settings.moex_api_key``."""
    if not api_key:
        raise ValueError("empty MOEX API key")
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def interval_ts(trade_date: Any, trade_time: Any) -> tuple[date, datetime]:
    """``(tradedate, aware MSK datetime)`` of an AlgoPack/FUTOI row."""
    d = to_date(trade_date, field="tradedate")
    t = to_time(trade_time, field="tradetime")
    if d is None or t is None:
        raise IssFormatError(f"AlgoPack row without tradedate/tradetime: {trade_date} {trade_time}")
    return d, datetime.combine(d, t, tzinfo=MSK)


# column name in payload -> (db column, kind)
_INT = "int"
_NUM = "num"
TRADESTATS_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("pr_open", _NUM),
    ("pr_high", _NUM),
    ("pr_low", _NUM),
    ("pr_close", _NUM),
    ("pr_std", _NUM),
    ("vol", _NUM),
    ("val", _NUM),
    ("trades", _INT),
    ("pr_vwap", _NUM),
    ("pr_change", _NUM),
    ("trades_b", _INT),
    ("trades_s", _INT),
    ("val_b", _NUM),
    ("val_s", _NUM),
    ("vol_b", _NUM),
    ("vol_s", _NUM),
    ("disb", _NUM),
    ("pr_vwap_b", _NUM),
    ("pr_vwap_s", _NUM),
    ("im", _NUM),
    ("oi_open", _NUM),
    ("oi_high", _NUM),
    ("oi_low", _NUM),
    ("oi_close", _NUM),
    ("sec_pr_open", _NUM),
    ("sec_pr_high", _NUM),
    ("sec_pr_low", _NUM),
    ("sec_pr_close", _NUM),
)
OBSTATS_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("mid_price", _NUM),
    ("micro_price", _NUM),
    ("spread_l1", _NUM),
    ("spread_l2", _NUM),
    ("spread_l3", _NUM),
    ("spread_l5", _NUM),
    ("spread_l10", _NUM),
    ("spread_l20", _NUM),
    ("levels_b", _INT),
    ("levels_s", _INT),
    ("vol_b_l1", _NUM),
    ("vol_b_l2", _NUM),
    ("vol_b_l3", _NUM),
    ("vol_b_l5", _NUM),
    ("vol_b_l10", _NUM),
    ("vol_b_l20", _NUM),
    ("vol_s_l1", _NUM),
    ("vol_s_l2", _NUM),
    ("vol_s_l3", _NUM),
    ("vol_s_l5", _NUM),
    ("vol_s_l10", _NUM),
    ("vol_s_l20", _NUM),
    ("vwap_b_l3", _NUM),
    ("vwap_b_l5", _NUM),
    ("vwap_b_l10", _NUM),
    ("vwap_b_l20", _NUM),
    ("vwap_s_l3", _NUM),
    ("vwap_s_l5", _NUM),
    ("vwap_s_l10", _NUM),
    ("vwap_s_l20", _NUM),
)
_BASE_COLUMNS: Final = ("tradedate", "tradetime", "secid", "asset_code", "SYSTIME")


def _convert(value: Any, kind: str, field: str) -> Decimal | int | None:
    if kind == _INT:
        return to_int(value, field=field)
    return to_decimal(value, field=field)


def _parse_bars(
    rows: Sequence[Mapping[str, Any]], columns: tuple[tuple[str, str], ...], what: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    required = (*_BASE_COLUMNS, *(c for c, _ in columns))
    for r in rows:
        missing = [c for c in required if c not in r]
        if missing:
            raise IssFormatError(f"AlgoPack {what} row lacks {missing}")
        d, ts = interval_ts(r["tradedate"], r["tradetime"])
        secid = r.get("secid")
        if not secid:
            raise IssFormatError(f"AlgoPack {what} row without secid")
        row: dict[str, Any] = {
            "interval_ts": ts,
            "secid": str(secid),
            "trade_date": d,
            "asset_code": r["asset_code"] or None,
        }
        for col, kind in columns:
            row[col] = _convert(r[col], kind, col)
        row["source_ts"] = to_datetime(r["SYSTIME"], field="SYSTIME")
        out.append(row)
    return out


def parse_tradestats(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """AlgoPack FO tradestats ``data`` rows -> ``md.algopack_fo_tradestats`` rows
    (normalized columns are added by the loader)."""
    return _parse_bars(rows, TRADESTATS_COLUMNS, "tradestats")


def parse_obstats(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """AlgoPack FO obstats ``data`` rows -> ``md.algopack_fo_obstats`` rows."""
    return _parse_bars(rows, OBSTATS_COLUMNS, "obstats")


_FUTOI_REQUIRED: Final = (
    "sess_id",
    "seqnum",
    "tradedate",
    "tradetime",
    "ticker",
    "clgroup",
    "pos",
    "pos_long",
    "pos_short",
    "pos_long_num",
    "pos_short_num",
    "systime",
    "trade_session_date",
)


def parse_futoi(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """FUTOI ``futoi`` rows -> ``md.algopack_futoi`` rows."""
    out: list[dict[str, Any]] = []
    for r in rows:
        missing = [c for c in _FUTOI_REQUIRED if c not in r]
        if missing:
            raise IssFormatError(f"FUTOI row lacks {missing}")
        d, ts = interval_ts(r["tradedate"], r["tradetime"])
        if not r["ticker"] or not r["clgroup"]:
            raise IssFormatError(f"FUTOI row without ticker/clgroup: {dict(r)}")
        out.append(
            {
                "interval_ts": ts,
                "ticker": str(r["ticker"]),
                "clgroup": str(r["clgroup"]),
                "trade_date": d,
                "trade_session_date": to_date(r["trade_session_date"], field="trade_session_date"),
                "sess_id": to_int(r["sess_id"], field="sess_id"),
                "seqnum": to_int(r["seqnum"], field="seqnum"),
                "pos": to_decimal(r["pos"], field="pos"),
                "pos_long": to_decimal(r["pos_long"], field="pos_long"),
                "pos_short": to_decimal(r["pos_short"], field="pos_short"),
                "pos_long_num": to_decimal(r["pos_long_num"], field="pos_long_num"),
                "pos_short_num": to_decimal(r["pos_short_num"], field="pos_short_num"),
                "source_ts": to_datetime(r["systime"], field="systime"),
            }
        )
    return out
