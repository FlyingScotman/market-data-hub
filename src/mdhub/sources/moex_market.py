"""MOEX ISS market data: bonds EOD/intraday, indices, ZCYC (G-curve / KBD). Fetch + parse, no DB.

Endpoints (public ISS, ``https://iss.moex.com/iss``):

==========================  ==========================================================  ======
what                        path                                                        paging
==========================  ==========================================================  ======
bonds EOD, all boards       ``/history/engines/stock/markets/bonds/securities.json``     cursor
bonds EOD yields            ``/history/engines/stock/markets/bonds/yields.json``         start=
bonds intraday snapshot     ``/engines/stock/markets/bonds/securities.json``             none
index EOD, all indices      ``/history/engines/stock/markets/index/securities.json``     cursor
index current values        ``/engines/stock/markets/index/securities.json``             none
ZCYC params + std yields    ``/engines/stock/zcyc.json`` (``?date=`` for a past day)     none
==========================  ==========================================================  ======

* ``history/.../yields.json`` has **no cursor block**: it is paged with ``start=`` in pages of
  :data:`YIELDS_PAGESIZE` rows; :func:`iter_bond_yield_pages` stops on a short page and the
  loader cross-checks the row count against the ``history`` TOTAL of the same day.
* Every parser validates the declared columns (fail closed on format drift) and that ISS
  returned the requested trade date.
* Bond marketdata on public ISS is 15 minutes delayed: ``SYSTIME`` is the snapshot clock and
  ``UPDATETIME`` the (delayed) quote clock; :func:`detect_latency` labels a snapshot.
* ZCYC: :func:`kbd_zero_rate_bp` implements the MOEX KBD (Nelson-Siegel-Svensson with nine
  Gaussian bumps) formula; :func:`check_zcyc` reproduces the published standard-tenor yields
  from the parameters (tolerance :data:`ZCYC_TOLERANCE_BP`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from mdhub.core.http import HttpClient
from mdhub.core.iss import (
    IssFormatError,
    IssPage,
    IssPagingError,
    parse_block,
    to_date,
    to_datetime,
    to_decimal,
    to_int,
    to_str,
    to_time,
)
from mdhub.core.timeutil import MSK, date_range, msk_datetime, previous_session_day

__all__ = [
    "BONDS_HISTORY_PATH",
    "BONDS_MARKETDATA_PATH",
    "BONDS_YIELDS_PATH",
    "BOND_EOD_FIELDS",
    "BOND_EOD_YIELD_FIELDS",
    "BOND_HISTORY_REQUIRED",
    "BOND_QUOTE_COMPARE",
    "BOND_QUOTE_FIELDS",
    "INDEX_GROUPS",
    "INDEX_HISTORY_PATH",
    "INDEX_HISTORY_REQUIRED",
    "INDEX_MARKETDATA_PATH",
    "PARSER_VERSION",
    "YIELDS_PAGESIZE",
    "ZCYC_PATH",
    "ZCYC_TOLERANCE_BP",
    "BondSnapshot",
    "ZcycCheck",
    "ZcycClockSkewError",
    "ZcycSnapshot",
    "business_days_between",
    "check_zcyc",
    "detect_latency",
    "index_group_secids",
    "iter_bond_yield_pages",
    "kbd_yield_pct",
    "kbd_zero_rate_bp",
    "parse_bond_history",
    "parse_bond_marketdata",
    "parse_bond_yields",
    "parse_index_history",
    "parse_index_marketdata",
    "parse_zcyc",
]

PARSER_VERSION: Final = "2"
"""2: bonds marketdata ``LCLOSEPRICE`` (``legal_close``, official close after the session)."""

BONDS_HISTORY_PATH: Final = "/history/engines/stock/markets/bonds/securities.json"
BONDS_YIELDS_PATH: Final = "/history/engines/stock/markets/bonds/yields.json"
BONDS_MARKETDATA_PATH: Final = "/engines/stock/markets/bonds/securities.json"
INDEX_HISTORY_PATH: Final = "/history/engines/stock/markets/index/securities.json"
INDEX_MARKETDATA_PATH: Final = "/engines/stock/markets/index/securities.json"
ZCYC_PATH: Final = "/engines/stock/zcyc.json"

YIELDS_PAGESIZE: Final = 100
"""Rows per page of ``history/.../yields.json`` (no cursor block is returned)."""

Kind = Literal["dec", "int", "str", "date", "datetime"]
FieldSpec = tuple[str, str, Kind]
"""``(db column, ISS column, converter kind)``."""


def _convert(value: Any, kind: Kind, name: str) -> Any:
    if kind == "dec":
        return to_decimal(value, field=name)
    if kind == "int":
        return to_int(value, field=name)
    if kind == "str":
        return to_str(value)
    if kind == "date":
        return to_date(value, field=name)
    return to_datetime(value, field=name)


def _map_row(raw: Mapping[str, Any], fields: Sequence[FieldSpec]) -> dict[str, Any]:
    return {col: _convert(raw[iss], kind, iss) for col, iss, kind in fields}


def _require_str(value: Any, name: str) -> str:
    text = to_str(value)
    if text is None:
        raise IssFormatError(f"{name} is empty")
    return text


# ---------------------------------------------------------------------------- dates


def business_days_between(
    start: date, end: date, is_business_day: Callable[[date], bool]
) -> list[date]:
    """Business days in ``[start, end]`` (inclusive), ascending."""
    if end < start:
        raise ValueError(f"date_to {end} is before date_from {start}")
    return [d for d in date_range(start, end) if is_business_day(d)]


# ------------------------------------------------------------------------- bonds EOD

BOND_EOD_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("short_name", "SHORTNAME", "str"),
    ("open", "OPEN", "dec"),
    ("low", "LOW", "dec"),
    ("high", "HIGH", "dec"),
    ("close", "CLOSE", "dec"),
    ("legal_close", "LEGALCLOSEPRICE", "dec"),
    ("waprice", "WAPRICE", "dec"),
    ("market_price2", "MARKETPRICE2", "dec"),
    ("market_price3", "MARKETPRICE3", "dec"),
    ("admitted_quote", "ADMITTEDQUOTE", "dec"),
    ("mp2_val_trd", "MP2VALTRD", "dec"),
    ("market_price3_trades_value", "MARKETPRICE3TRADESVALUE", "dec"),
    ("admitted_value", "ADMITTEDVALUE", "dec"),
    ("accrued_int", "ACCINT", "dec"),
    ("face_value", "FACEVALUE", "dec"),
    ("face_unit", "FACEUNIT", "str"),
    ("currency_id", "CURRENCYID", "str"),
    ("volume", "VOLUME", "dec"),
    ("value", "VALUE", "dec"),
    ("num_trades", "NUMTRADES", "int"),
    ("yield_close_pct", "YIELDCLOSE", "dec"),
    ("yield_at_waprice_pct", "YIELDATWAP", "dec"),
    ("yield_to_offer_pct", "YIELDTOOFFER", "dec"),
    ("yield_last_coupon_pct", "YIELDLASTCOUPON", "dec"),
    ("duration_days", "DURATION", "dec"),
    ("coupon_pct", "COUPONPERCENT", "dec"),
    ("coupon_value", "COUPONVALUE", "dec"),
    ("mat_date", "MATDATE", "date"),
    ("offer_date", "OFFERDATE", "date"),
    ("buyback_date", "BUYBACKDATE", "date"),
    ("call_option_date", "CALLOPTIONDATE", "date"),
    ("call_option_yield_pct", "CALLOPTIONYIELD", "dec"),
    ("call_option_duration_days", "CALLOPTIONDURATION", "dec"),
    ("put_option_date", "PUTOPTIONDATE", "date"),
    ("date_yield_from_issuer", "DATEYIELDFROMISSUER", "date"),
    ("last_trade_date", "LASTTRADEDATE", "date"),
    ("zspread_bp", "ZSPREAD", "dec"),
    ("zspread_at_waprice_bp", "ZSPREADATWAPRICE", "dec"),
    ("iricpi_close", "IRICPICLOSE", "dec"),
    ("bei_close", "BEICLOSE", "dec"),
    ("cbr_close", "CBRCLOSE", "dec"),
    ("trading_session", "TRADINGSESSION", "str"),
    ("bond_type", "BONDTYPE", "str"),
    ("bond_subtype", "BONDSUBTYPE", "str"),
)
"""``md.bond_eod`` columns from ``history`` (all required: fail closed on drift)."""

BOND_HISTORY_REQUIRED: Final = ("BOARDID", "TRADEDATE", "SECID", *(f[1] for f in BOND_EOD_FIELDS))

BOND_EOD_YIELD_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("yield_date", "YIELDDATE", "date"),
    ("yield_date_type", "YIELDDATETYPE", "str"),
    ("price", "PRICE", "dec"),
    ("accrued_int", "ACCINT", "dec"),
    ("zcyc_moment", "ZCYCMOMENT", "datetime"),
    ("effective_yield_pct", "EFFECTIVEYIELD", "dec"),
    ("duration_days", "DURATION", "dec"),
    ("zspread_bp", "ZSPREADBP", "dec"),
    ("gspread_bp", "GSPREADBP", "dec"),
    ("waprice", "WAPRICE", "dec"),
    ("effective_yield_waprice_pct", "EFFECTIVEYIELDWAPRICE", "dec"),
    ("duration_waprice_days", "DURATIONWAPRICE", "dec"),
    ("ir", "IR", "dec"),
    ("icpi", "ICPI", "dec"),
    ("bei", "BEI", "dec"),
    ("cbr", "CBR", "dec"),
    ("yield_to_offer_pct", "YIELDTOOFFER", "dec"),
    ("yield_last_coupon_pct", "YIELDLASTCOUPON", "dec"),
    ("trade_moment", "TRADEMOMENT", "date"),
)
"""``md.bond_eod_yield`` columns from ``history_yields``."""

BOND_YIELDS_REQUIRED: Final = (
    "TRADEDATE",
    "SECID",
    "BOARDID",
    *(f[1] for f in BOND_EOD_YIELD_FIELDS),
)


def _check_trade_date(raw: Mapping[str, Any], trade_date: date, what: str) -> None:
    got = to_date(raw["TRADEDATE"], field="TRADEDATE")
    if got != trade_date:
        raise IssFormatError(f"{what}: ISS returned TRADEDATE {got} for requested {trade_date}")


def _keyed(raw: Mapping[str, Any], trade_date: date) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "secid": _require_str(raw["SECID"], "SECID"),
        "board": _require_str(raw["BOARDID"], "BOARDID"),
    }


def parse_bond_history(rows: Iterable[Mapping[str, Any]], trade_date: date) -> list[dict[str, Any]]:
    """``history`` rows (already column-checked) -> ``md.bond_eod`` rows (without meta).

    :raises IssFormatError: a row for another trade date, bad values, duplicate key.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        _check_trade_date(raw, trade_date, "bonds history")
        row = _keyed(raw, trade_date)
        key = (row["secid"], row["board"])
        if key in seen:
            raise IssFormatError(f"bonds history {trade_date}: duplicate {key}")
        seen.add(key)
        row.update(_map_row(raw, BOND_EOD_FIELDS))
        out.append(row)
    return out


def parse_bond_yields(rows: Iterable[Mapping[str, Any]], trade_date: date) -> list[dict[str, Any]]:
    """``history_yields`` rows -> ``md.bond_eod_yield`` rows; rows whose value columns are
    all empty (ISS lists every board, most carry no yields) are dropped - no value, no row."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        _check_trade_date(raw, trade_date, "bonds yields")
        row = _keyed(raw, trade_date)
        key = (row["secid"], row["board"])
        if key in seen:
            raise IssFormatError(f"bonds yields {trade_date}: duplicate {key}")
        seen.add(key)
        values = _map_row(raw, BOND_EOD_YIELD_FIELDS)
        if all(v is None for v in values.values()):
            continue
        row.update(values)
        out.append(row)
    return out


def iter_bond_yield_pages(
    http: HttpClient, trade_date: date, *, max_pages: int = 1000
) -> Iterator[IssPage]:
    """Pages of ``history/.../yields.json?date=`` (no cursor block).

    Stops after a page shorter than :data:`YIELDS_PAGESIZE` (an empty page is fetched only
    when the total is an exact multiple). Use the ``history`` TOTAL to cross-check the count.
    """
    start = 0
    for _ in range(max_pages):
        res = http.get_json(
            BONDS_YIELDS_PATH,
            params={"iss.meta": "off", "date": trade_date.isoformat(), "start": start},
        )
        rows = parse_block(res.data, "history_yields", required=BOND_YIELDS_REQUIRED)
        if len(rows) > YIELDS_PAGESIZE:
            raise IssPagingError(f"yields {trade_date}: page of {len(rows)} > {YIELDS_PAGESIZE}")
        yield IssPage(rows=rows, cursor=None, response=res.response, payload=res.data, start=start)
        if len(rows) < YIELDS_PAGESIZE:
            return
        start += len(rows)
    raise IssPagingError(f"yields {trade_date}: more than {max_pages} pages")


# -------------------------------------------------------------------- bonds intraday

BOND_QUOTE_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("bid", "BID", "dec"),
    ("offer", "OFFER", "dec"),
    ("last", "LAST", "dec"),
    ("open", "OPEN", "dec"),
    ("low", "LOW", "dec"),
    ("high", "HIGH", "dec"),
    ("waprice", "WAPRICE", "dec"),
    ("market_price", "MARKETPRICE", "dec"),
    ("market_price_today", "MARKETPRICETODAY", "dec"),
    ("lcurrent_price", "LCURRENTPRICE", "dec"),
    ("close_price", "CLOSEPRICE", "dec"),
    ("legal_close", "LCLOSEPRICE", "dec"),
    ("yield_pct", "YIELD", "dec"),
    ("yield_at_waprice_pct", "YIELDATWAPRICE", "dec"),
    ("yield_to_offer_pct", "YIELDTOOFFER", "dec"),
    ("duration_days", "DURATION", "dec"),
    ("num_trades", "NUMTRADES", "int"),
    ("vol_today", "VOLTODAY", "dec"),
    ("val_today", "VALTODAY", "dec"),
    ("val_today_rub", "VALTODAY_RUR", "dec"),
    ("num_bids", "NUMBIDS", "int"),
    ("num_offers", "NUMOFFERS", "int"),
    ("trading_status", "TRADINGSTATUS", "str"),
    ("trading_session", "TRADINGSESSION", "str"),
    ("zspread_bp", "ZSPREAD", "dec"),
)
"""``md.bond_quote_intraday`` columns from ``marketdata``."""

BOND_QUOTE_YIELD_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("effective_yield_pct", "EFFECTIVEYIELD", "dec"),
    ("yield_date", "YIELDDATE", "date"),
    ("yield_date_type", "YIELDDATETYPE", "str"),
    ("gspread_bp", "GSPREADBP", "dec"),
)
"""Columns from ``marketdata_yields`` (joined on SECID, BOARDID)."""

BOND_QUOTE_COMPARE: Final = (
    "bid",
    "offer",
    "last",
    "open",
    "low",
    "high",
    "waprice",
    "market_price",
    "lcurrent_price",
    "close_price",
    "legal_close",
    "yield_pct",
    "yield_at_waprice_pct",
    "num_trades",
    "vol_today",
    "val_today",
    "num_bids",
    "num_offers",
    "trading_status",
    "trading_session",
)
"""Fields whose change makes a new intraday row. Curve-dependent analytics (G/Z-spread,
effective yield, duration) are stored with each row but do not by themselves create one -
they move with every ZCYC tick for every bond."""

Latency = Literal["realtime", "delayed"]
REALTIME_MAX_LAG: Final = timedelta(seconds=120)
"""A snapshot whose freshest quote is at most this far behind ``SYSTIME`` is realtime."""


@dataclass(frozen=True, slots=True)
class BondSnapshot:
    """Parsed ``/engines/stock/markets/bonds/securities.json`` (marketdata + yields)."""

    rows: list[dict[str, Any]]
    source_ts: datetime | None
    latency: Latency
    min_lag_seconds: float | None


def _quote_ts(
    systime: datetime,
    update_time: time,
    is_trading_day: Callable[[date], bool] | None = None,
) -> datetime:
    """Quote datetime: SYSTIME's date + UPDATETIME; a time after SYSTIME is from the previous
    session - the previous **trading day** of the market (``is_trading_day``, default
    Monday-Friday; :func:`mdhub.core.timeutil.previous_session_day`), not simply the day
    before (a Monday pre-session snapshot carries the last session's update)."""
    ts = msk_datetime(systime.date(), update_time)
    if ts > systime + timedelta(minutes=1):
        ts = msk_datetime(previous_session_day(systime.date(), is_trading_day), update_time)
    return ts


def detect_latency(lags: Iterable[float]) -> tuple[Latency, float | None]:
    """``('delayed'|'realtime', min lag s)`` from per-row ``SYSTIME - UPDATETIME`` lags.

    The freshest row decides: public ISS stamps quotes ~900 s behind SYSTIME. Without any
    row (or outside the session, when nothing is fresh) the snapshot is ``delayed``.
    """
    values = [x for x in lags if x >= 0]
    if not values:
        return "delayed", None
    lag = min(values)
    return ("realtime" if lag <= REALTIME_MAX_LAG.total_seconds() else "delayed"), lag


def parse_bond_marketdata(
    payload: Mapping[str, Any], *, is_trading_day: Callable[[date], bool] | None = None
) -> BondSnapshot:
    """Parse the all-boards bond snapshot (``iss.only=marketdata,marketdata_yields``).

    ``is_trading_day``: days on which bond boards trade (dates quotes older than SYSTIME).

    Rows lacking ``UPDATETIME``/``SYSTIME`` are skipped (no quote clock, nothing to store).
    """
    md = parse_block(
        payload,
        "marketdata",
        required=("SECID", "BOARDID", "UPDATETIME", "SYSTIME", *(f[1] for f in BOND_QUOTE_FIELDS)),
    )
    my = parse_block(
        payload,
        "marketdata_yields",
        required=("SECID", "BOARDID", *(f[1] for f in BOND_QUOTE_YIELD_FIELDS)),
    )
    yields: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in my:
        key = (_require_str(raw["SECID"], "SECID"), _require_str(raw["BOARDID"], "BOARDID"))
        if key in yields:
            raise IssFormatError(f"bond marketdata_yields: duplicate {key}")
        yields[key] = _map_row(raw, BOND_QUOTE_YIELD_FIELDS)
    empty_yields = {f[0]: None for f in BOND_QUOTE_YIELD_FIELDS}

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    lags: list[float] = []
    max_sys: datetime | None = None
    for raw in md:
        secid = _require_str(raw["SECID"], "SECID")
        board = _require_str(raw["BOARDID"], "BOARDID")
        if (secid, board) in seen:
            raise IssFormatError(f"bond marketdata: duplicate {(secid, board)}")
        seen.add((secid, board))
        systime = to_datetime(raw["SYSTIME"], field="SYSTIME")
        upd = to_time(raw["UPDATETIME"], field="UPDATETIME")
        if systime is None or upd is None:
            continue
        ts = _quote_ts(systime, upd, is_trading_day)
        lags.append((systime - ts).total_seconds())
        max_sys = systime if max_sys is None or systime > max_sys else max_sys
        row: dict[str, Any] = {"secid": secid, "board": board, "ts": ts, "source_ts": systime}
        row.update(_map_row(raw, BOND_QUOTE_FIELDS))
        row.update(yields.get((secid, board), empty_yields))
        rows.append(row)
    latency, min_lag = detect_latency(lags)
    for row in rows:
        row["latency"] = latency
    return BondSnapshot(rows=rows, source_ts=max_sys, latency=latency, min_lag_seconds=min_lag)


# --------------------------------------------------------------------------- indices

INDEX_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    # Fixed once a day at 12:30 MSK (CALCMODE RF-MAIN).
    "rusfar": (
        "RUSFAR",
        "RUSFAR1W",
        "RUSFAR2W",
        "RUSFAR1M",
        "RUSFAR3M",
        "RUSFARCNY",
        "RUSFARCN1W",
    ),
    # Real-time and real-time compound variants, final at 18:00 MSK (RF-RT, RF-N).
    "rusfarrt": (
        "RUSFARRT",
        "RUSFAR1WRT",
        "RUSFAR2WRT",
        "RUSFAR1MRT",
        "RUSFAR3MRT",
        "RUSFARCNRT",
        "RUSFARC1WR",
        "RUSFARN",
        "RUSFAR1WN",
        "RUSFAR2WN",
        "RUSFAR1MN",
        "RUSFAR3MN",
        "RUSFARCNN",
        "RUSFARC1WN",
    ),
    # Bond / repo benchmarks of interest (history stores every index anyway).
    "bonds": (
        "RGBI",
        "RGBITR",
        "RUCBITR1Y",
        "RUCBICP1Y",
        "RUCBITRL1",
        "RUCBITRL2",
        "RUCBITRL3",
        "RUGBITR1Y",
        "RUGBITR3Y",
        "RUGBITR5Y",
        "RUGBITR10Y",
        "RUGBINFTR",
        "MOEXREPO",
        "IMOEX",
    ),
}
"""Configured index groups. ``RUSFARUSD`` (discontinued 2022-12) and ``MOEXREPO`` (last value
2024-12) are kept in history but not expected daily. Group ``all`` = every index ISS lists."""


def index_group_secids(group: str) -> tuple[str, ...] | None:
    """Secids of ``group``; ``None`` for ``all``. Unknown group -> ``ValueError``."""
    if group == "all":
        return None
    try:
        return INDEX_GROUPS[group]
    except KeyError:
        have = ", ".join(INDEX_GROUPS)
        raise ValueError(f"unknown index group {group!r} (have: all, {have})") from None


INDEX_HISTORY_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("board", "BOARDID", "str"),
    ("short_name", "SHORTNAME", "str"),
    ("open", "OPEN", "dec"),
    ("high", "HIGH", "dec"),
    ("low", "LOW", "dec"),
    ("close", "CLOSE", "dec"),
    ("value", "VALUE", "dec"),
    ("volume", "VOLUME", "dec"),
    ("duration_days", "DURATION", "dec"),
    ("yield_pct", "YIELD", "dec"),
    ("capitalization", "CAPITALIZATION", "dec"),
    ("currency_id", "CURRENCYID", "str"),
    ("trading_session", "TRADINGSESSION", "str"),
)

INDEX_MARKETDATA_FIELDS: Final[tuple[FieldSpec, ...]] = (
    ("board", "BOARDID", "str"),
    ("open", "OPENVALUE", "dec"),
    ("high", "HIGH", "dec"),
    ("low", "LOW", "dec"),
    ("close", "CURRENTVALUE", "dec"),
    ("value", "VALTODAY", "dec"),
    ("capitalization", "CAPITALIZATION", "dec"),
    ("trading_session", "TRADINGSESSION", "str"),
)


def parse_index_history(
    rows: Iterable[Mapping[str, Any]], trade_date: date
) -> list[dict[str, Any]]:
    """Index ``history`` rows -> ``md.index_eod`` rows (``basis='history'``)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        _check_trade_date(raw, trade_date, "index history")
        secid = _require_str(raw["SECID"], "SECID")
        if secid in seen:
            raise IssFormatError(f"index history {trade_date}: duplicate {secid}")
        seen.add(secid)
        row: dict[str, Any] = {"trade_date": trade_date, "secid": secid}
        row.update(_map_row(raw, INDEX_HISTORY_FIELDS))
        row.update(basis="history", source_ts=None)
        out.append(row)
    return out


INDEX_HISTORY_REQUIRED: Final = ("SECID", "TRADEDATE", *(f[1] for f in INDEX_HISTORY_FIELDS))


def parse_index_marketdata(
    payload: Mapping[str, Any], trade_date: date, secids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Current index values (``marketdata``) for ``trade_date`` -> ``md.index_eod`` rows with
    ``basis='marketdata'`` (preliminary). Rows whose TRADEDATE is not ``trade_date`` (index not
    yet fixed today) or without a current value are skipped; ``secids`` filters (``None`` =
    all)."""
    md = parse_block(
        payload,
        "marketdata",
        required=("SECID", "TRADEDATE", "SYSTIME", *(f[1] for f in INDEX_MARKETDATA_FIELDS)),
    )
    wanted = set(secids) if secids is not None else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in md:
        secid = _require_str(raw["SECID"], "SECID")
        if wanted is not None and secid not in wanted:
            continue
        if to_date(raw["TRADEDATE"], field="TRADEDATE") != trade_date:
            continue
        if secid in seen:
            raise IssFormatError(f"index marketdata: duplicate {secid}")
        seen.add(secid)
        row: dict[str, Any] = {"trade_date": trade_date, "secid": secid}
        row.update(_map_row(raw, INDEX_MARKETDATA_FIELDS))
        if row["close"] is None:
            continue
        row.update(
            short_name=None,
            volume=None,
            duration_days=None,
            yield_pct=None,
            currency_id=None,
            basis="marketdata",
            source_ts=to_datetime(raw["SYSTIME"], field="SYSTIME"),
        )
        out.append(row)
    return out


# ------------------------------------------------------------------------------ ZCYC

ZCYC_PARAM_NAMES: Final = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))
ZCYC_TOLERANCE_BP: Final = Decimal("0.01")
"""Max |model - published| standard-tenor yield difference (published to 1e-4 % = 0.01 bp)."""


def _kbd_grid() -> tuple[tuple[float, ...], tuple[float, ...]]:
    # MOEX KBD methodology: a1 = 0, a2 = 0.6, a(i+1) = a(i) + 0.6 * 1.6^(i-1); b1 = 0.6,
    # b(i+1) = 1.6 * b(i)  (as in market-data-store gcurve.py, verified against ISS).
    centres = [0.0, 0.6]
    for power in range(1, 8):
        centres.append(centres[-1] + 0.6 * 1.6**power)
    widths = [0.6]
    for _ in range(8):
        widths.append(widths[-1] * 1.6)
    return tuple(centres), tuple(widths)


_CENTRES, _WIDTHS = _kbd_grid()


def kbd_zero_rate_bp(params: Mapping[str, float], tenor_years: float) -> float:
    """Continuously compounded zero rate (bp) of the KBD curve at ``tenor_years``::

    G(t) = B1 + B2 * L + B3 * (L - e^(-t/T1)) + sum_i G_i * exp(-((t - a_i) / b_i)^2),
    L = T1 / t * (1 - e^(-t/T1))
    """
    if not math.isfinite(tenor_years) or tenor_years <= 0:
        raise ValueError("tenor must be positive")
    tau = params["T1"]
    if tau <= 0:
        raise ValueError("T1 must be positive")
    decay = math.exp(-tenor_years / tau)
    loading = tau / tenor_years * (1.0 - decay)
    rate = params["B1"] + params["B2"] * loading + params["B3"] * (loading - decay)
    for i, (centre, width) in enumerate(zip(_CENTRES, _WIDTHS, strict=True), start=1):
        rate += params[f"G{i}"] * math.exp(-(((tenor_years - centre) / width) ** 2))
    return rate


def kbd_yield_pct(params: Mapping[str, float], tenor_years: float) -> float:
    """Effective annual yield (%) as MOEX publishes it: ``(exp(G/10000) - 1) * 100``."""
    return math.expm1(kbd_zero_rate_bp(params, tenor_years) / 10_000.0) * 100.0


class ZcycClockSkewError(IssFormatError):
    """``params`` and ``yearyields`` are from different publication ticks."""


@dataclass(frozen=True, slots=True)
class ZcycCheck:
    """Result of reproducing the published standard yields from the parameters."""

    max_error_bp: Decimal
    passed: bool
    errors_bp: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ZcycSnapshot:
    """One published KBD state."""

    trade_date: date
    trade_time: time
    params: dict[str, float]
    standard_yields: list[tuple[float, float]]
    """``(tenor_years, yield_pct)`` in ISS order."""

    @property
    def source_ts(self) -> datetime:
        return datetime.combine(self.trade_date, self.trade_time, tzinfo=MSK)

    def params_json(self) -> dict[str, float]:
        return dict(self.params)

    def yields_json(self) -> list[dict[str, float]]:
        return [{"tenor_years": t, "yield_pct": y} for t, y in self.standard_yields]


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IssFormatError(f"zcyc {name}: not a number: {value!r}")
    out = float(value)
    if not math.isfinite(out):
        raise IssFormatError(f"zcyc {name}: not finite")
    return out


def parse_zcyc(
    payload: Mapping[str, Any], *, trade_date: date | None = None
) -> ZcycSnapshot | None:
    """Parse ``zcyc.json`` (blocks ``params`` + ``yearyields``).

    Numbers stay as the JSON floats ISS sent (their shortest repr is the published text, and
    they are stored as jsonb numerics, i.e. exactly). ``None`` when ISS has no curve for the
    day (both blocks empty).

    :param trade_date: requested date; a different returned date raises.
    :raises ZcycClockSkewError: params and yields carry different trade date/time.
    :raises IssFormatError: other shape problems.
    """
    params_rows = parse_block(
        payload, "params", required=("tradedate", "tradetime", *ZCYC_PARAM_NAMES)
    )
    yield_rows = parse_block(
        payload, "yearyields", required=("tradedate", "tradetime", "period", "value")
    )
    if not params_rows and not yield_rows:
        return None
    if len(params_rows) != 1 or not yield_rows:
        raise IssFormatError(
            f"zcyc: expected 1 params row and standard yields, "
            f"got {len(params_rows)}/{len(yield_rows)}"
        )
    p = params_rows[0]
    d = to_date(p["tradedate"], field="tradedate")
    t = to_time(p["tradetime"], field="tradetime")
    if d is None or t is None:
        raise IssFormatError("zcyc: params without tradedate/tradetime")
    if trade_date is not None and d != trade_date:
        raise IssFormatError(f"zcyc: ISS returned {d} for requested {trade_date}")
    for r in yield_rows:
        if to_date(r["tradedate"], field="tradedate") != d or to_time(r["tradetime"]) != t:
            raise ZcycClockSkewError(
                f"zcyc: params at {d} {t} but yearyields at {r['tradedate']} {r['tradetime']}"
            )
    params = {name: _finite_float(p[name], name) for name in ZCYC_PARAM_NAMES}
    yields = [
        (_finite_float(r["period"], "period"), _finite_float(r["value"], "value"))
        for r in yield_rows
    ]
    if any(tenor <= 0 for tenor, _ in yields):
        raise IssFormatError("zcyc: non-positive tenor")
    return ZcycSnapshot(trade_date=d, trade_time=t, params=params, standard_yields=yields)


def check_zcyc(snapshot: ZcycSnapshot, tolerance_bp: Decimal = ZCYC_TOLERANCE_BP) -> ZcycCheck:
    """Reproduce the published standard yields from the parameters.

    ``max_error_bp`` is rounded to 1e-6 bp (deterministic, so reloads are no-ops).
    """
    errors: dict[str, Decimal] = {}
    for tenor, published in snapshot.standard_yields:
        err = abs(kbd_yield_pct(snapshot.params, tenor) - published) * 100.0
        errors[repr(tenor)] = Decimal(repr(round(err, 6)))
    worst = max(errors.values())
    return ZcycCheck(max_error_bp=worst, passed=worst < tolerance_bp, errors_bp=errors)
