"""MOEX ISS intraday inputs of the RGBI futures monitor: fetch paths + parsers, no DB.

Paths (public ISS ``https://iss.moex.com/iss``, APIM for the realtime FORTS trades):

* FORTS trades of one contract: ``/engines/futures/markets/forts/securities/{secid}/trades.json``
  (incremental ``tradeno=``),
* KBD quotes per OFZ: ``/engines/stock/zcyc.json`` block ``securities`` (one answer),
* index marketdata: ``/engines/stock/markets/index/securities.json?securities=`` (one answer),
* index candles: ``/engines/stock/markets/index/securities/{secid}/candles.json`` (``start=``),
* index composition: ``/statistics/engines/stock/markets/index/analytics/{index}.json`` (cursor),
* repo marketdata (ccp, gcc): ``/engines/stock/markets/{market}/boards/{board}/securities.json``,
* repo history (ccp, gcc): the same path under ``/history`` (cursor).

Observed 2026-10-29 on public ISS (fixtures ``tests/fixtures/moex_intraday``):

* FORTS trades: the whole day in one answer (no cursor); ``tradeno=N&next_trade=1`` returns the
  trades after ``N`` - the incremental read. 15 minutes delayed on public ISS (``SYSTIME`` ~
  ``TRADETIME``, published late), realtime through APIM.
* ``zcyc.json`` ``securities``: ~29 OFZ-PD with the order-book bid/ask (price and yield), last
  trade and the KBD price; ``tradetime``/``updatetime`` lag the request by seconds (near
  realtime, no subscription needed). The block has no SYSTIME: ``tradetime`` is its clock.
* Index marketdata (RGBI, RGBITR): realtime (``UPDATETIME`` = ``SYSTIME``).
* Index candles: 500 rows per page, ``start=`` paging, ``begin``/``end`` MSK wall clock.
* Index analytics: 20 rows per page with a cursor ``analytics.cursor`` (``TOTAL``); weights in %.
* Repo marketdata (ccp, gcc): 15 minutes delayed (``UPDATETIME`` ~900 s behind ``SYSTIME``);
  rates in % p.a. The ccp boards list every share and bond: only OFZ rows are kept
  (:func:`is_ofz`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from mdhub.core.iss import (
    IssFormatError,
    parse_block,
    to_date,
    to_datetime,
    to_decimal,
    to_int,
    to_str,
    to_time,
)
from mdhub.core.timeutil import MSK
from mdhub.sources.moex_fx import snapshot_ts
from mdhub.sources.moex_market import REALTIME_MAX_LAG, FieldSpec, Latency, _map_row

__all__ = [
    "CCP_BOARDS",
    "FORTS_TRADES",
    "GCC_BOARDS",
    "GC_BOND_BASKET",
    "INDEX_ANALYTICS",
    "INDEX_CANDLES",
    "INDEX_MARKETDATA",
    "PARSER_VERSION",
    "REPO_HISTORY",
    "REPO_MARKETDATA",
    "TRADES_PAGE_HINT",
    "ZCYC_SECURITIES",
    "is_ofz",
    "latency_of",
    "parse_forts_trades",
    "parse_index_candles",
    "parse_index_constituents",
    "parse_index_intraday",
    "parse_repo_history",
    "parse_repo_marketdata",
    "parse_zcyc_securities",
    "validate_constituents",
]

PARSER_VERSION: Final = "moex_intraday/1"

FORTS_TRADES: Final = "/engines/futures/markets/forts/securities/{secid}/trades.json"
ZCYC_SECURITIES: Final = "/engines/stock/zcyc.json"
INDEX_MARKETDATA: Final = "/engines/stock/markets/index/securities.json"
INDEX_CANDLES: Final = "/engines/stock/markets/index/securities/{secid}/candles.json"
INDEX_ANALYTICS: Final = "/statistics/engines/stock/markets/index/analytics/{index}.json"
REPO_MARKETDATA: Final = "/engines/stock/markets/{market}/boards/{board}/securities.json"
REPO_HISTORY: Final = "/history/engines/stock/markets/{market}/boards/{board}/securities.json"

CCP_BOARDS: Final = ("EQRP", "EQWP", "EQMP", "EQTP")
"""Repo with the CCP per security (market ``ccp``): 1 day, 1 week, 1 month, 3 months."""
GCC_BOARDS: Final = ("GCRP", "GCOW", "GCSW", "GCOM", "GCSM", "GCTM", "GCUM", "GCNM", "GCOY")
"""GC repo with the CCP (market ``gcc``) by term: 1 day ... 1 year."""
GC_BOND_BASKET: Final = "RU000A0JW4Z1"
"""GC Bonds basket (КСУ облигаций)."""

TRADES_PAGE_HINT: Final = 1000
"""An incremental trades answer with at least this many rows is read again from its last
TRADENO (the endpoint returned a whole day of ~2000 rows in one answer; a cap is not
documented, so a large answer is never taken as complete)."""

_OFZ_RE: Final = re.compile(r"^SU\d{5}RMFS\d$")


def is_ofz(secid: str) -> bool:
    """OFZ SECID (``SU26238RMFS4``)."""
    return bool(_OFZ_RE.match(secid))


def latency_of(lags: Iterable[timedelta]) -> Latency:
    """``realtime`` when the freshest row lags its clock by at most
    :data:`~mdhub.sources.moex_market.REALTIME_MAX_LAG` (120 s); public ISS delayed data lag
    ~900 s. No rows: ``delayed`` (conservative)."""
    values = [lag for lag in lags if lag >= timedelta(0)]
    if not values:
        return "delayed"
    return "realtime" if min(values) <= REALTIME_MAX_LAG else "delayed"


def _req_str(value: Any, name: str) -> str:
    text = to_str(value)
    if text is None:
        raise IssFormatError(f"{name} is empty")
    return text


def _req_dt(value: Any, name: str) -> datetime:
    dt = to_datetime(value, field=name)
    if dt is None:
        raise IssFormatError(f"{name} is empty")
    return dt


def _msk(day: date, t: Any, name: str) -> datetime:
    tt = to_time(t, field=name)
    if tt is None:
        raise IssFormatError(f"{name} is empty")
    return datetime.combine(day, tt, tzinfo=MSK)


# ------------------------------------------------------------------------- FORTS trades

_TRADES_REQUIRED: Final = (
    "TRADENO",
    "SECID",
    "TRADEDATE",
    "TRADETIME",
    "PRICE",
    "QUANTITY",
    "SYSTIME",
    "RECNO",
    "OPENPOSITION",
    "OFFMARKETDEAL",
    "BUYSELL",
    "TRADE_SESSION_DATE",
)


def parse_forts_trades(payload: Mapping[str, Any], secid: str) -> list[dict[str, Any]]:
    """``trades`` block -> ``md.futures_trade`` rows (without source/latency/provenance).
    Rows of another SECID fail closed."""
    out: list[dict[str, Any]] = []
    for r in parse_block(payload, "trades", required=_TRADES_REQUIRED):
        if r["SECID"] != secid:
            raise IssFormatError(f"FORTS trades of {secid}: row of {r['SECID']!r}")
        trade_no = to_int(r["TRADENO"], field="TRADENO")
        day = to_date(r["TRADEDATE"], field="TRADEDATE")
        price = to_decimal(r["PRICE"], field="PRICE")
        qty = to_decimal(r["QUANTITY"], field="QUANTITY")
        if trade_no is None or day is None or price is None or qty is None:
            raise IssFormatError(f"FORTS trade of {secid} without TRADENO/date/price/quantity")
        side = to_str(r["BUYSELL"])
        if side not in (None, "B", "S"):
            raise IssFormatError(f"FORTS trade {trade_no}: BUYSELL {side!r}")
        off = to_int(r["OFFMARKETDEAL"], field="OFFMARKETDEAL")
        out.append(
            {
                "secid": secid,
                "trade_no": trade_no,
                "trade_ts": _msk(day, r["TRADETIME"], "TRADETIME"),
                "trade_date": day,
                "session_date": to_date(r["TRADE_SESSION_DATE"], field="TRADE_SESSION_DATE"),
                "price": price,
                "quantity": qty,
                "buysell": side,
                "open_position": to_decimal(r["OPENPOSITION"], field="OPENPOSITION"),
                "off_market": None if off is None else bool(off),
                "rec_no": to_int(r["RECNO"], field="RECNO"),
                "source_ts": _req_dt(r["SYSTIME"], "SYSTIME"),
            }
        )
    return out


# ------------------------------------------------------------------- zcyc securities

ZCYC_SECURITY_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("short_name", "shortname", "str"),
    ("exp_date", "expdate", "date"),
    ("bid_price_pct", "bidprice", "dec"),
    ("bid_yield_pct", "bidyield", "dec"),
    ("ask_price_pct", "askprice", "dec"),
    ("ask_yield_pct", "askyield", "dec"),
    ("trd_price_pct", "trdprice", "dec"),
    ("trd_yield_pct", "trdyield", "dec"),
    ("clc_price_pct", "clcprice", "dec"),
    ("clc_yield_pct", "clcyield", "dec"),
    ("crt_price_pct", "crtprice", "dec"),
    ("crt_yield_pct", "crtyield", "dec"),
    ("correction", "correction", "dec"),
    ("crt_duration_days", "crtduration", "dec"),
    ("bid_duration_days", "bidduration", "dec"),
    ("ask_duration_days", "askduration", "dec"),
)
ZCYC_SECURITY_COMPARE: Final = tuple(f[0] for f in ZCYC_SECURITY_FIELDS if f[0] != "short_name")
"""Value columns whose change stores a new ``md.zcyc_security_quote`` row."""


def parse_zcyc_securities(payload: Mapping[str, Any], fetched_at: datetime) -> list[dict[str, Any]]:
    """``zcyc.json`` block ``securities`` -> ``md.zcyc_security_quote`` rows.

    ``ts`` = tradedate + updatetime (quote), ``calc_ts`` = ``source_ts`` = tradedate + tradetime
    (the block's clock). ``latency`` compares the freshest ``calc_ts`` with ``fetched_at`` (the
    request time): realtime within 120 s."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in parse_block(
        payload,
        "securities",
        required=(
            "tradedate",
            "tradetime",
            "secid",
            "benchmark",
            "updatetime",
            *(f[1] for f in ZCYC_SECURITY_FIELDS),
        ),
    ):
        secid = _req_str(r["secid"], "secid")
        if secid in seen:
            raise IssFormatError(f"zcyc securities: duplicate {secid}")
        seen.add(secid)
        day = to_date(r["tradedate"], field="tradedate")
        if day is None:
            raise IssFormatError(f"zcyc securities {secid}: no tradedate")
        calc_ts = _msk(day, r["tradetime"], "tradetime")
        upd = to_time(r["updatetime"], field="updatetime")
        bench = to_int(r["benchmark"], field="benchmark")
        row: dict[str, Any] = {
            "secid": secid,
            "ts": datetime.combine(day, upd, tzinfo=MSK) if upd is not None else calc_ts,
            "trade_date": day,
            "calc_ts": calc_ts,
            "benchmark": None if bench is None else bool(bench),
            "source_ts": calc_ts,
        }
        row.update(_map_row(r, ZCYC_SECURITY_FIELDS))
        rows.append(row)
    latency = latency_of(fetched_at - r["source_ts"] for r in rows)
    for row in rows:
        row["latency"] = latency
    return rows


# --------------------------------------------------------------------- index intraday

INDEX_INTRADAY_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("board", "BOARDID", "str"),
    ("current_value", "CURRENTVALUE", "dec"),
    ("last_value", "LASTVALUE", "dec"),
    ("open_value", "OPENVALUE", "dec"),
    ("high", "HIGH", "dec"),
    ("low", "LOW", "dec"),
    ("last_change", "LASTCHANGE", "dec"),
    ("last_change_prc", "LASTCHANGEPRC", "dec"),
    ("val_today", "VALTODAY", "dec"),
    ("trading_session", "TRADINGSESSION", "str"),
    ("trade_date", "TRADEDATE", "date"),
)
INDEX_INTRADAY_COMPARE: Final = (
    "current_value",
    "last_value",
    "open_value",
    "high",
    "low",
    "last_change",
    "last_change_prc",
    "val_today",
    "trading_session",
    "trade_date",
)


def parse_index_intraday(
    payload: Mapping[str, Any],
    secids: Iterable[str],
    *,
    is_trading_day: Callable[[date], bool] | None = None,
) -> list[dict[str, Any]]:
    """Index ``marketdata`` -> ``md.index_intraday`` rows for ``secids`` (rows without a
    current value or quote time are skipped; a requested index missing from the answer fails
    closed). ``ts`` = UPDATETIME on the SYSTIME session day."""
    wanted = set(secids)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in parse_block(
        payload,
        "marketdata",
        required=("SECID", "UPDATETIME", "SYSTIME", *(f[1] for f in INDEX_INTRADAY_FIELDS)),
    ):
        secid = _req_str(r["SECID"], "SECID")
        if secid not in wanted:
            continue
        if secid in seen:
            raise IssFormatError(f"index marketdata: duplicate {secid}")
        seen.add(secid)
        systime = _req_dt(r["SYSTIME"], "SYSTIME")
        ts = snapshot_ts(r["UPDATETIME"], systime, is_trading_day=is_trading_day)
        row: dict[str, Any] = {"secid": secid, "ts": ts, "source_ts": systime}
        row.update(_map_row(r, INDEX_INTRADAY_FIELDS))
        if ts is None or row["current_value"] is None:
            continue
        rows.append(row)
    missing = wanted - seen
    if missing:
        raise IssFormatError(f"index marketdata lacks {sorted(missing)}")
    latency = latency_of(r["source_ts"] - r["ts"] for r in rows)
    for row in rows:
        row["latency"] = latency
    return rows


# ------------------------------------------------------------------------ index candles

_CANDLE_REQUIRED: Final = ("open", "close", "high", "low", "value", "volume", "begin", "end")


def parse_index_candles(
    rows: Iterable[Mapping[str, Any]], secid: str, interval_min: int
) -> list[dict[str, Any]]:
    """``candles`` rows -> ``md.index_candle`` rows. ``source_ts`` = the candle's end;
    ``latency`` = ``realtime`` (index data is not delayed on public ISS; the candle of the
    running minute is revised by later polls)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        missing = [c for c in _CANDLE_REQUIRED if c not in r]
        if missing:
            raise IssFormatError(f"candles of {secid} lack {missing}")
        begin = _req_dt(r["begin"], "begin")
        end = _req_dt(r["end"], "end")
        out.append(
            {
                "secid": secid,
                "interval_min": interval_min,
                "begin_ts": begin,
                "end_ts": end,
                "trade_date": begin.date(),
                "open": to_decimal(r["open"], field="open"),
                "high": to_decimal(r["high"], field="high"),
                "low": to_decimal(r["low"], field="low"),
                "close": to_decimal(r["close"], field="close"),
                "value": to_decimal(r["value"], field="value"),
                "volume": to_decimal(r["volume"], field="volume"),
                "source_ts": end,
                "latency": "realtime",
            }
        )
    return out


# ----------------------------------------------------------------- index constituents

_ANALYTICS_REQUIRED: Final = (
    "indexid",
    "tradedate",
    "ticker",
    "shortnames",
    "secids",
    "weight",
    "tradingsession",
    "trade_session_date",
)
WEIGHT_SUM_TOLERANCE: Final = Decimal("0.5")
"""|sum of weights - 100| allowed (weights are published with 2 decimals)."""


def parse_index_constituents(
    rows: Iterable[Mapping[str, Any]], index_id: str, trade_date: date
) -> list[dict[str, Any]]:
    """``analytics`` rows -> ``md.index_constituent`` rows; another index or date fails
    closed. ``secids`` is the SECID (one per row for bond indices)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        missing = [c for c in _ANALYTICS_REQUIRED if c not in r]
        if missing:
            raise IssFormatError(f"index analytics {index_id} lacks {missing}")
        if r["indexid"] != index_id or to_date(r["tradedate"], field="tradedate") != trade_date:
            raise IssFormatError(
                f"index analytics {index_id} {trade_date}: row of {r['indexid']} {r['tradedate']}"
            )
        secid = _req_str(r["secids"], "secids")
        if "," in secid or " " in secid:
            raise IssFormatError(f"index analytics {index_id}: several SECIDs in one row {secid!r}")
        if secid in seen:
            raise IssFormatError(f"index analytics {index_id} {trade_date}: duplicate {secid}")
        seen.add(secid)
        weight = to_decimal(r["weight"], field="weight")
        if weight is None:
            raise IssFormatError(f"index analytics {index_id} {secid}: no weight")
        out.append(
            {
                "index_id": index_id,
                "trade_date": trade_date,
                "secid": secid,
                "ticker": to_str(r["ticker"]),
                "short_name": to_str(r["shortnames"]),
                "weight_pct": weight,
                "trading_session": to_int(r["tradingsession"], field="tradingsession"),
                "session_date": to_date(r["trade_session_date"], field="trade_session_date"),
            }
        )
    return out


def validate_constituents(rows: list[dict[str, Any]], total: int, what: str) -> None:
    """Fail closed unless the row count equals the cursor ``TOTAL`` and the weights sum to
    100 % within :data:`WEIGHT_SUM_TOLERANCE`."""
    if len(rows) != total:
        raise IssFormatError(f"{what}: {len(rows)} rows, cursor TOTAL {total}")
    if not rows:
        return
    weights = sum((r["weight_pct"] for r in rows), Decimal(0))
    if abs(weights - 100) > WEIGHT_SUM_TOLERANCE:
        raise IssFormatError(f"{what}: weights sum to {weights}, expected ~100")


# -------------------------------------------------------------------------------- repo

REPO_MD_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("bid_pct", "BID", "dec"),
    ("offer_pct", "OFFER", "dec"),
    ("open_pct", "OPEN", "dec"),
    ("low_pct", "LOW", "dec"),
    ("high_pct", "HIGH", "dec"),
    ("last_pct", "LAST", "dec"),
    ("waprice_pct", "WAPRICE", "dec"),
    ("num_trades", "NUMTRADES", "int"),
    ("vol_today", "VOLTODAY", "dec"),
    ("val_today_rub", "VALTODAY_RUR", "dec"),
    ("trading_status", "TRADINGSTATUS", "str"),
)
REPO_MD_COMPARE: Final = tuple(f[0] for f in REPO_MD_FIELDS)


def parse_repo_marketdata(
    payload: Mapping[str, Any],
    market: str,
    board: str,
    *,
    keep: Callable[[str], bool] = lambda _s: True,
    is_trading_day: Callable[[date], bool] | None = None,
) -> list[dict[str, Any]]:
    """Repo board ``securities`` + ``marketdata`` -> ``md.repo_intraday`` rows of the SECIDs
    ``keep`` accepts. Rows without a quote clock, or without any rate, are skipped (nothing
    traded or quoted). ``settle_date(2)`` come from the securities block (``SETTLEDATE2`` is
    absent on gcc)."""
    sec_rows = parse_block(payload, "securities", required=("SECID", "BOARDID", "SETTLEDATE"))
    settle: dict[str, tuple[date | None, date | None]] = {}
    for s in sec_rows:
        sid = _req_str(s["SECID"], "SECID")
        settle[sid] = (
            to_date(s["SETTLEDATE"], field="SETTLEDATE"),
            to_date(s.get("SETTLEDATE2"), field="SETTLEDATE2"),
        )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in parse_block(
        payload,
        "marketdata",
        required=("SECID", "BOARDID", "UPDATETIME", "SYSTIME", *(f[1] for f in REPO_MD_FIELDS)),
    ):
        secid = _req_str(r["SECID"], "SECID")
        if not keep(secid):
            continue
        if r["BOARDID"] != board:
            raise IssFormatError(f"repo {market}/{board}: row of board {r['BOARDID']!r}")
        if secid in seen:
            raise IssFormatError(f"repo {market}/{board}: duplicate {secid}")
        seen.add(secid)
        systime = to_datetime(r["SYSTIME"], field="SYSTIME")
        ts = snapshot_ts(r["UPDATETIME"], systime, is_trading_day=is_trading_day)
        if systime is None or ts is None:
            continue
        row: dict[str, Any] = {"market": market, "board": board, "secid": secid, "ts": ts}
        row.update(_map_row(r, REPO_MD_FIELDS))
        if all(row[c] is None for c in ("bid_pct", "offer_pct", "last_pct", "waprice_pct")):
            continue
        s1, s2 = settle.get(secid, (None, None))
        row.update(trade_date=ts.date(), settle_date=s1, settle_date2=s2, source_ts=systime)
        rows.append(row)
    latency = latency_of(r["source_ts"] - r["ts"] for r in rows)
    for row in rows:
        row["latency"] = latency
    return rows


REPO_HISTORY_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("short_name", "SHORTNAME", "str"),
    ("num_trades", "NUMTRADES", "int"),
    ("value_rub", "VALUE", "dec"),
    ("open_pct", "OPEN", "dec"),
    ("low_pct", "LOW", "dec"),
    ("high_pct", "HIGH", "dec"),
    ("close_pct", "CLOSE", "dec"),
    ("waprice_pct", "WAPRICE", "dec"),
    ("trading_session", "TRADINGSESSION", "int"),
)
REPO_HISTORY_REQUIRED: Final = (
    "BOARDID",
    "TRADEDATE",
    "SECID",
    *(f[1] for f in REPO_HISTORY_FIELDS),
)


def parse_repo_history(
    rows: Iterable[Mapping[str, Any]],
    market: str,
    board: str,
    trade_date: date,
    *,
    keep: Callable[[str], bool] = lambda _s: True,
) -> list[dict[str, Any]]:
    """Repo ``history`` rows -> ``md.repo_eod`` rows (another board or date fails closed)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        secid = _req_str(r["SECID"], "SECID")
        if r["BOARDID"] != board or to_date(r["TRADEDATE"], field="TRADEDATE") != trade_date:
            raise IssFormatError(
                f"repo history {market}/{board} {trade_date}: row of {r['BOARDID']} "
                f"{r['TRADEDATE']}"
            )
        if not keep(secid):
            continue
        if secid in seen:
            raise IssFormatError(f"repo history {market}/{board} {trade_date}: duplicate {secid}")
        seen.add(secid)
        row: dict[str, Any] = {
            "market": market,
            "trade_date": trade_date,
            "board": board,
            "secid": secid,
        }
        row.update(_map_row(r, REPO_HISTORY_FIELDS))
        out.append(row)
    return out
