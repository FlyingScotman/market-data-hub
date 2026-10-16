"""MOEX ISS: currency market (``engine currency / market selt``) and FORTS FX futures.

Pure parsing (no DB, no settings). Every parser validates the columns it needs and raises
:class:`~mdhub.core.iss.IssFormatError` on drift - never returns partial data.

Endpoints (relative to ``https://iss.moex.com/iss``; APIM ``https://apim.moex.com/iss`` serves
the same paths with realtime FORTS marketdata):

========================  ===========================================================
``SELT_SECURITIES``       all boards of the selt market: static + marketdata
``CETS_SECURITIES``       CETS board only (intraday snapshot, ~200 instruments)
``SELT_HISTORY``          daily OHLC/WAP per board, ``?date=``, cursor-paged (100/page);
                          the market publishes no volume/value columns
``INDICATIVE``            indicative FX rates ``?date=``; tc 14:00 / mc 19:00 (history 2013-)
``FIXING_LIST``           currency fixings, latest per secid (``?date=`` single day)
``FIXING_SECID``          one fixing ``?from=&till=`` (cursor-paged, has ``rateprc``)
``RFUD_SECURITIES``       FORTS futures static + marketdata (all ~500 contracts)
``FORTS_SECURITIES``      FORTS ``?securities=a,b`` (subset; APIM = realtime)
``FORTS_HISTORY``         FORTS daily history ``?date=``, cursor-paged
``SECURITY_DESCRIPTION``  ``/securities/{secid}.json`` (expired contracts' dates)
========================  ===========================================================

Futures price normalization (owner baseline, audit 03 §3.4)::

    RUB per 1 unit of base currency = raw price x STEPPRICE / MINSTEP / LOTVOLUME

STEPPRICE is the RUB value of one MINSTEP for one lot. It is constant for RUB-quoted
contracts (Si: 1/1/1000, CNY: 1/0.001/1000, USDRUBF: 10/0.01/1000) and is re-set daily for
crosses (ED, GBPU, UCNY, UJPY...: it embeds the exchange's quote-ccy/RUB rate), so the
normalized price of a cross is only computed with a STEPPRICE of the same session.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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

__all__ = [
    "CETS_SECURITIES",
    "FIXING_LIST",
    "FIXING_SECID",
    "FORTS_HISTORY",
    "FORTS_SECURITIES",
    "FX_FUTURES_ASSETS",
    "INDICATIVE",
    "PARSER_VERSION",
    "PERPETUAL_SENTINEL",
    "RFUD_SECURITIES",
    "SECURITY_DESCRIPTION",
    "SELT_HISTORY",
    "SELT_SECURITIES",
    "FxAsset",
    "classify_fx_secid",
    "classify_latency",
    "fx_asset",
    "fx_futoi_tickers",
    "normalize_price",
    "parse_fixing_rows",
    "parse_forts_history",
    "parse_futures_marketdata",
    "parse_futures_securities",
    "parse_fx_history",
    "parse_fx_instruments",
    "parse_fx_marketdata",
    "parse_indicative",
    "parse_security_description",
    "price_factor",
    "snapshot_ts",
]

PARSER_VERSION: Final = "moex_fx/1"

SELT_SECURITIES: Final = "/engines/currency/markets/selt/securities.json"
CETS_SECURITIES: Final = "/engines/currency/markets/selt/boards/CETS/securities.json"
SELT_HISTORY: Final = "/history/engines/currency/markets/selt/securities.json"
INDICATIVE: Final = "/statistics/engines/futures/markets/indicativerates/securities.json"
FIXING_LIST: Final = "/statistics/engines/currency/markets/fixing.json"
FIXING_SECID: Final = "/statistics/engines/currency/markets/fixing/{secid}.json"
RFUD_SECURITIES: Final = "/engines/futures/markets/forts/boards/RFUD/securities.json"
FORTS_SECURITIES: Final = "/engines/futures/markets/forts/securities.json"
FORTS_HISTORY: Final = "/history/engines/futures/markets/forts/securities.json"
SECURITY_DESCRIPTION: Final = "/securities/{secid}.json"

PERPETUAL_SENTINEL: Final = date(2100, 1, 1)
"""MOEX LASTTRADEDATE/LASTDELDATE of perpetual futures."""

DELAYED_THRESHOLD: Final = timedelta(minutes=5)
"""Snapshot is ``delayed`` when even its freshest row lags SYSTIME by at least this much
(public ISS lags 900 s; realtime APIM lags 0-1 s)."""


# ------------------------------------------------------------------- shared helpers


def _req(row: Mapping[str, Any], col: str) -> Any:
    value = row.get(col)
    if value is None or value == "":
        raise IssFormatError(f"required value {col} missing in row {dict(row)}")
    return value


def _price(value: Any, *, field: str, zero_is_null: bool) -> Decimal | None:
    d = to_decimal(value, field=field)
    if d is not None and zero_is_null and d == 0:
        return None
    return d


def snapshot_ts(
    update_time: Any, systime: datetime | None, *, field: str = "UPDATETIME"
) -> datetime | None:
    """Combine an ISS ``UPDATETIME`` (MSK wall clock) with the date of ``SYSTIME``.

    If the time lies after SYSTIME (snapshot taken after midnight for a pre-midnight
    update) the previous day is used.
    """
    t = to_time(update_time, field=field)
    if t is None or systime is None:
        return None
    sys_msk = systime.astimezone(MSK)
    ts = datetime.combine(sys_msk.date(), t, tzinfo=MSK)
    if ts > sys_msk + timedelta(seconds=1):
        ts -= timedelta(days=1)
    return ts


def classify_latency(pairs: Iterable[tuple[datetime | None, datetime | None]]) -> str:
    """``realtime`` if the freshest row of a snapshot lags its SYSTIME by < 5 min.

    ``pairs`` are ``(ts, source_ts)`` per row. In a delayed (public ISS) snapshot every row
    lags by >= 900 s; in a realtime one the most active instrument lags by ~0 s. A quiet
    realtime market can therefore be labelled ``delayed`` (conservative), never vice versa.
    """
    lags = [src - ts for ts, src in pairs if ts is not None and src is not None]
    if not lags:
        return "delayed"
    return "realtime" if min(lags) < DELAYED_THRESHOLD else "delayed"


# ------------------------------------------------------------- currency instruments

_SWAP_TENOR = re.compile(r"TOM(\d+[DWMY])$")
_SWAP_PAIRS: Final = {
    "TODTOM": ("TOD", "TOM"),
    "TDSTMS": ("TDS", "TMS"),
    "TMSSTS": ("TMS", "STS"),
}
_OUTRIGHT_SUFFIX: Final = (
    ("_TOD", "TOD"),
    ("_TOM", "TOM"),
    ("_SPT", "SPT"),
    ("_TDS", "TDS"),
    ("_TMS", "TMS"),
    ("_TDB", "TDB"),
    ("_TMB", "TMB"),
    ("_STS", "STS"),
    ("_FWD", "FWD"),
    ("_FWS", "FWS"),
    ("TOD", "TOD"),  # USD000000TOD, CNY000000TOD, EURUSD000TOD
    ("TOM", "TOM"),  # USD000UTSTOM, EURUSD000TOM, UAH000000TOM
)


def classify_fx_secid(secid: str) -> tuple[str, str | None, str | None, str | None]:
    """``(kind, tenor, near_leg, far_leg)`` for a selt SECID.

    Kinds: ``outright`` (TOD/TOM/SPT/TDS/TMS/TDB/TMB/STS/FWD/FWS), ``swap`` (two legs,
    e.g. ``CNYRUBTODTOM`` -> TOD/TOM, ``CNYRUB_TOM1W`` -> TOM/1W, ``*_TOM1D`` -> TOM/SPT),
    ``fixing`` (``*_FIX0`` fixing auctions), ``wap`` (``*_WAP0``), else ``other``.
    """
    s = secid.upper()
    if s.endswith("_FIX0"):
        return "fixing", "FIX", None, None
    if s.endswith("_WAP0"):
        return "wap", "WAP", None, None
    for suffix, (near, far) in _SWAP_PAIRS.items():
        if s.endswith(suffix):
            return "swap", f"{near}/{far}", near, far
    m = _SWAP_TENOR.search(s)
    if m:
        far = "SPT" if m.group(1) == "1D" else m.group(1)
        return "swap", f"TOM/{far}", "TOM", far
    for suffix, tenor in _OUTRIGHT_SUFFIX:
        if s.endswith(suffix):
            return "outright", tenor, None, None
    return "other", None, None, None


_FX_SEC_REQUIRED: Final = (
    "SECID",
    "BOARDID",
    "SHORTNAME",
    "SECNAME",
    "LATNAME",
    "FACEUNIT",
    "CURRENCYID",
    "LOTSIZE",
    "MINSTEP",
    "DECIMALS",
    "FACEVALUE",
    "MARKETCODE",
    "STATUS",
    "SETTLEDATE",
)


def parse_fx_instruments(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``securities`` block of a selt securities response -> ``ref.fx_instrument`` rows
    (without provenance/first_seen columns)."""
    out: list[dict[str, Any]] = []
    for r in parse_block(payload, "securities", required=_FX_SEC_REQUIRED):
        secid = str(_req(r, "SECID"))
        kind, tenor, near, far = classify_fx_secid(secid)
        out.append(
            {
                "secid": secid,
                "board": str(_req(r, "BOARDID")),
                "short_name": to_str(r["SHORTNAME"]),
                "sec_name": to_str(r["SECNAME"]),
                "lat_name": to_str(r["LATNAME"]),
                "base_ccy": to_str(r["FACEUNIT"]),
                "quote_ccy": to_str(r["CURRENCYID"]),
                "kind": kind,
                "tenor": tenor,
                "near_leg": near,
                "far_leg": far,
                "lot_size": to_decimal(r["LOTSIZE"], field="LOTSIZE"),
                "min_step": to_decimal(r["MINSTEP"], field="MINSTEP"),
                "decimals": to_int(r["DECIMALS"], field="DECIMALS"),
                "face_value": to_decimal(r["FACEVALUE"], field="FACEVALUE"),
                "market_code": to_str(r["MARKETCODE"]),
                "status": to_str(r["STATUS"]),
                "settle_date": to_date(r["SETTLEDATE"], field="SETTLEDATE"),
            }
        )
    return out


_FX_MD_REQUIRED: Final = (
    "SECID",
    "BOARDID",
    "BID",
    "OFFER",
    "LAST",
    "WAPRICE",
    "OPEN",
    "HIGH",
    "LOW",
    "NUMTRADES",
    "VOLTODAY",
    "VALTODAY",
    "TRADINGSTATUS",
    "UPDATETIME",
    "SYSTIME",
)
FX_INTRADAY_VALUE_COLUMNS: Final = (
    "bid",
    "offer",
    "last",
    "waprice",
    "open",
    "high",
    "low",
    "num_trades",
    "vol_today",
    "val_today",
    "trading_status",
)


def parse_fx_marketdata(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """``marketdata`` block of a selt/CETS snapshot -> (rows for ``md.fx_intraday``, latency).

    Rows without any price (bid, offer, last, waprice all empty) are dropped - there is
    nothing to store. ``latency`` is set on every row (see :func:`classify_latency`).
    """
    rows: list[dict[str, Any]] = []
    for r in parse_block(payload, "marketdata", required=_FX_MD_REQUIRED):
        systime = to_datetime(r["SYSTIME"], field="SYSTIME")
        ts = snapshot_ts(r["UPDATETIME"], systime)
        row = {
            "ts": ts,
            "secid": str(_req(r, "SECID")),
            "board": str(_req(r, "BOARDID")),
            "bid": _price(r["BID"], field="BID", zero_is_null=True),
            "offer": _price(r["OFFER"], field="OFFER", zero_is_null=True),
            "last": _price(r["LAST"], field="LAST", zero_is_null=False),
            "waprice": _price(r["WAPRICE"], field="WAPRICE", zero_is_null=False),
            "open": _price(r["OPEN"], field="OPEN", zero_is_null=False),
            "high": _price(r["HIGH"], field="HIGH", zero_is_null=False),
            "low": _price(r["LOW"], field="LOW", zero_is_null=False),
            "num_trades": to_int(r["NUMTRADES"], field="NUMTRADES"),
            "vol_today": to_decimal(r["VOLTODAY"], field="VOLTODAY"),
            "val_today": to_decimal(r["VALTODAY"], field="VALTODAY"),
            "trading_status": to_str(r["TRADINGSTATUS"]),
            "source_ts": systime,
        }
        rows.append(row)
    latency = classify_latency(
        (r["ts"], r["source_ts"]) for r in rows if r["trading_status"] in (None, "T")
    )
    kept = []
    for row in rows:
        if all(row[c] is None for c in ("bid", "offer", "last", "waprice")):
            continue
        if row["ts"] is None:
            raise IssFormatError(f"CETS marketdata {row['secid']}: priced row without UPDATETIME")
        row["latency"] = latency
        kept.append(row)
    return kept, latency


_FX_HIST_REQUIRED: Final = (
    "BOARDID",
    "TRADEDATE",
    "SECID",
    "OPEN",
    "LOW",
    "HIGH",
    "CLOSE",
    "NUMTRADES",
    "WAPRICE",
)


def parse_fx_history(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """selt ``history`` rows -> ``md.fx_eod`` rows.

    MOEX reports 0 for OPEN/LOW/HIGH/CLOSE when nothing traded; with ``NUMTRADES == 0`` a
    zero price is stored as NULL (missing, not zero). Swap prices may legitimately be 0
    when trades happened, so zeros are kept then.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        missing = [c for c in _FX_HIST_REQUIRED if c not in r]
        if missing:
            raise IssFormatError(f"selt history row lacks {missing}")
        n = to_int(r["NUMTRADES"], field="NUMTRADES")
        no_trades = not n
        out.append(
            {
                "trade_date": to_date(_req(r, "TRADEDATE"), field="TRADEDATE"),
                "secid": str(_req(r, "SECID")),
                "board": str(_req(r, "BOARDID")),
                "open": _price(r["OPEN"], field="OPEN", zero_is_null=no_trades),
                "low": _price(r["LOW"], field="LOW", zero_is_null=no_trades),
                "high": _price(r["HIGH"], field="HIGH", zero_is_null=no_trades),
                "close": _price(r["CLOSE"], field="CLOSE", zero_is_null=no_trades),
                "waprice": _price(r["WAPRICE"], field="WAPRICE", zero_is_null=no_trades),
                "num_trades": n,
            }
        )
    return out


def parse_indicative(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Indicative rates -> ``md.fx_indicative`` rows (``ccy_pair`` as given + base/quote)."""
    out: list[dict[str, Any]] = []
    for r in parse_block(
        payload, "securities", required=("tradedate", "tradetime", "secid", "rate", "clearing")
    ):
        pair = str(_req(r, "secid"))
        base, sep, quote = pair.partition("/")
        if not sep or len(base) != 3 or len(quote) != 3:
            raise IssFormatError(f"indicative rate pair {pair!r} is not 'XXX/YYY'")
        clearing = str(_req(r, "clearing"))
        if clearing not in ("tc", "mc"):
            raise IssFormatError(f"indicative rate clearing {clearing!r} not tc/mc")
        rate = to_decimal(_req(r, "rate"), field="rate")
        out.append(
            {
                "trade_date": to_date(_req(r, "tradedate"), field="tradedate"),
                "clearing": clearing,
                "ccy_pair": pair,
                "base_ccy": base.upper(),
                "quote_ccy": quote.upper(),
                "trade_time": to_time(r["tradetime"], field="tradetime"),
                "rate": rate,
                "source": "moex",
            }
        )
    return out


def parse_fixing_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fixing ``history`` rows -> ``md.fx_fixing`` rows (``rateprc`` optional)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        for c in ("tradedate", "secid", "rate"):
            if c not in r:
                raise IssFormatError(f"fixing row lacks {c}")
        out.append(
            {
                "fixing_date": to_date(_req(r, "tradedate"), field="tradedate"),
                "secid": str(_req(r, "secid")),
                "rate": to_decimal(r["rate"], field="rate"),
                "rate_pct": to_decimal(r.get("rateprc"), field="rateprc"),
            }
        )
    return out


# ------------------------------------------------------------------- FX futures


@dataclass(frozen=True, slots=True)
class FxAsset:
    """Currency of a FORTS asset code: price is ``quote_ccy`` per 1 ``base_ccy``
    (scaled by the contract's lot)."""

    base_ccy: str
    quote_ccy: str
    perpetual: bool = False


FX_FUTURES_ASSETS: Final[Mapping[str, FxAsset]] = {
    # vs RUB, dated (RFUD ASSETCODE)
    "Si": FxAsset("USD", "RUB"),
    "USDM": FxAsset("USD", "RUB"),  # USD/RUB mini (lot 100)
    "Eu": FxAsset("EUR", "RUB"),
    "EURM": FxAsset("EUR", "RUB"),  # EUR/RUB mini (lot 100)
    "CNY": FxAsset("CNY", "RUB"),  # secids CR*
    "AED": FxAsset("AED", "RUB"),
    "AMD": FxAsset("AMD", "RUB"),
    "BYN": FxAsset("BYN", "RUB"),
    "HKD": FxAsset("HKD", "RUB"),
    "INR": FxAsset("INR", "RUB"),
    "KZT": FxAsset("KZT", "RUB"),
    "TRY": FxAsset("TRY", "RUB"),
    # crosses, dated
    "ED": FxAsset("EUR", "USD"),
    "GBPU": FxAsset("GBP", "USD"),
    "AUDU": FxAsset("AUD", "USD"),
    "UCAD": FxAsset("USD", "CAD"),
    "UCHF": FxAsset("USD", "CHF"),
    "UCNY": FxAsset("USD", "CNY"),
    "UINR": FxAsset("USD", "INR"),
    "UJPY": FxAsset("USD", "JPY"),
    "UKZT": FxAsset("USD", "KZT"),
    "UTRY": FxAsset("USD", "TRY"),
    "ECAD": FxAsset("EUR", "CAD"),
    "EGBP": FxAsset("EUR", "GBP"),
    "EJPY": FxAsset("EUR", "JPY"),
    # perpetual (one-day futures with auto-prolongation) - proxies, never spot
    "USDRUBTOM": FxAsset("USD", "RUB", perpetual=True),
    "EURRUBTOM": FxAsset("EUR", "RUB", perpetual=True),
    "CNYRUBTOM": FxAsset("CNY", "RUB", perpetual=True),
}
"""FORTS currency futures by ASSETCODE (verified against RFUD 2026-10-16). Metal assets
(GLDRUBTOM, SLVRUBTOM, GL, GOLD, SILV...) and index-in-CNY (MOEXCNY) are not FX."""


def fx_asset(asset_code: str | None) -> FxAsset | None:
    """FX classification of a FORTS asset code (``None`` = not a currency future)."""
    if not asset_code:
        return None
    return FX_FUTURES_ASSETS.get(asset_code)


def fx_futoi_tickers(sec_types: Iterable[str | None], perpetual_secids: Iterable[str]) -> set[str]:
    """FUTOI tickers of FX assets: SECTYPE codes of dated contracts (``Si``, ``CR``, ``ED``,
    ``UC``...) and the perpetual secids themselves (``USDRUBF``...)."""
    return {t for t in sec_types if t} | set(perpetual_secids)


def price_factor(
    step_price: Decimal | None, min_step: Decimal | None, lot_volume: Decimal | None
) -> Decimal | None:
    """``STEPPRICE / MINSTEP / LOTVOLUME`` or ``None`` if any input is missing or zero."""
    if step_price is None or min_step is None or lot_volume is None:
        return None
    if min_step == 0 or lot_volume == 0:
        return None
    return step_price / min_step / lot_volume


def normalize_price(raw: Decimal | None, factor: Decimal | None) -> Decimal | None:
    """``raw x factor`` (RUB per 1 unit of base currency); ``None`` if either is missing."""
    if raw is None or factor is None:
        return None
    return raw * factor


def _contract_dates(
    last_trade: date | None, last_del: date | None, asset: FxAsset | None
) -> tuple[str, date | None, date | None]:
    perpetual = (asset is not None and asset.perpetual) or (
        last_trade is not None and last_trade >= PERPETUAL_SENTINEL
    )
    if perpetual:
        return "perpetual", None, None
    return "dated", last_trade, last_del


_FUT_SEC_REQUIRED: Final = (
    "SECID",
    "BOARDID",
    "SHORTNAME",
    "SECNAME",
    "LATNAME",
    "MINSTEP",
    "LASTTRADEDATE",
    "LASTDELDATE",
    "SECTYPE",
    "ASSETCODE",
    "LOTVOLUME",
    "STEPPRICE",
    "DECIMALS",
    "FRSTTRADE",
)


def parse_futures_securities(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``securities`` block of an RFUD/FORTS response -> ``ref.futures_contract`` rows."""
    out: list[dict[str, Any]] = []
    for r in parse_block(payload, "securities", required=_FUT_SEC_REQUIRED):
        asset_code = to_str(r["ASSETCODE"])
        asset = fx_asset(asset_code)
        ctype, ltd, ldd = _contract_dates(
            to_date(r["LASTTRADEDATE"], field="LASTTRADEDATE"),
            to_date(r["LASTDELDATE"], field="LASTDELDATE"),
            asset,
        )
        out.append(
            {
                "secid": str(_req(r, "SECID")),
                "board": to_str(r["BOARDID"]),
                "short_name": to_str(r["SHORTNAME"]),
                "sec_name": to_str(r["SECNAME"]),
                "lat_name": to_str(r["LATNAME"]),
                "asset_code": asset_code,
                "sec_type": to_str(r["SECTYPE"]),
                "is_fx": asset is not None,
                "base_ccy": asset.base_ccy if asset else None,
                "quote_ccy": asset.quote_ccy if asset else None,
                "contract_type": ctype,
                "first_trade_date": to_date(r["FRSTTRADE"], field="FRSTTRADE"),
                "last_trade_date": ltd,
                "last_del_date": ldd,
                "min_step": to_decimal(r["MINSTEP"], field="MINSTEP"),
                "step_price": to_decimal(r["STEPPRICE"], field="STEPPRICE"),
                "lot_volume": to_decimal(r["LOTVOLUME"], field="LOTVOLUME"),
                "decimals": to_int(r["DECIMALS"], field="DECIMALS"),
                "spec_source": "rfud",
            }
        )
    return out


def parse_security_description(payload: Mapping[str, Any], secid: str) -> dict[str, Any]:
    """``/securities/{secid}.json`` ``description`` -> partial ``ref.futures_contract`` row
    for an expired contract (no STEPPRICE/MINSTEP there; LOTSIZE = lot volume)."""
    rows = parse_block(payload, "description", required=("name", "value"))
    desc = {str(r["name"]): r["value"] for r in rows}
    if desc.get("SECID") != secid:
        raise IssFormatError(f"description for {secid}: SECID is {desc.get('SECID')!r}")
    asset_code = to_str(desc.get("ASSETCODE"))
    asset = fx_asset(asset_code)
    ctype, ltd, ldd = _contract_dates(
        to_date(desc.get("LSTTRADE"), field="LSTTRADE"),
        to_date(desc.get("LSTDELDATE"), field="LSTDELDATE"),
        asset,
    )
    if desc.get("PERPETUAL_FUTURES") == "1":
        ctype, ltd, ldd = "perpetual", None, None
    return {
        "secid": secid,
        "board": None,
        "short_name": to_str(desc.get("SHORTNAME")),
        "sec_name": to_str(desc.get("NAME")),
        "lat_name": to_str(desc.get("LATNAME")),
        "asset_code": asset_code,
        "sec_type": None,
        "is_fx": asset is not None,
        "base_ccy": asset.base_ccy if asset else None,
        "quote_ccy": asset.quote_ccy if asset else None,
        "contract_type": ctype,
        "first_trade_date": to_date(desc.get("FRSTTRADE"), field="FRSTTRADE"),
        "last_trade_date": ltd,
        "last_del_date": ldd,
        "min_step": None,
        "step_price": None,
        "lot_volume": to_decimal(desc.get("LOTSIZE"), field="LOTSIZE"),
        "decimals": None,
        "spec_source": "iss_description",
    }


_FUT_HIST_REQUIRED: Final = (
    "BOARDID",
    "TRADEDATE",
    "SECID",
    "OPEN",
    "LOW",
    "HIGH",
    "CLOSE",
    "OPENPOSITIONVALUE",
    "VALUE",
    "VOLUME",
    "OPENPOSITION",
    "SETTLEPRICE",
    "SWAPRATE",
    "WAPRICE",
    "NUMTRADES",
    "SHORTNAME",
    "ASSETCODE",
)


def parse_forts_history(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FORTS ``history`` rows -> (``md.futures_eod`` rows, skipped calendar-spread rows).

    Calendar spreads (empty ASSETCODE) are skipped. OHLC/WAP zeros with no trades -> NULL;
    a zero settlement price -> NULL. Normalized columns are filled by the loader.
    """
    out: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for r in rows:
        missing = [c for c in _FUT_HIST_REQUIRED if c not in r]
        if missing:
            raise IssFormatError(f"FORTS history row lacks {missing}")
        asset_code = to_str(r["ASSETCODE"])
        if asset_code is None:
            skipped.append(dict(r))
            continue
        n = to_int(r["NUMTRADES"], field="NUMTRADES")
        no_trades = not n
        out.append(
            {
                "trade_date": to_date(_req(r, "TRADEDATE"), field="TRADEDATE"),
                "secid": str(_req(r, "SECID")),
                "board": str(_req(r, "BOARDID")),
                "asset_code": asset_code,
                "short_name": to_str(r["SHORTNAME"]),
                "open": _price(r["OPEN"], field="OPEN", zero_is_null=no_trades),
                "low": _price(r["LOW"], field="LOW", zero_is_null=no_trades),
                "high": _price(r["HIGH"], field="HIGH", zero_is_null=no_trades),
                "close": _price(r["CLOSE"], field="CLOSE", zero_is_null=no_trades),
                "settle_price": _price(r["SETTLEPRICE"], field="SETTLEPRICE", zero_is_null=True),
                "waprice": _price(r["WAPRICE"], field="WAPRICE", zero_is_null=no_trades),
                "volume": to_decimal(r["VOLUME"], field="VOLUME"),
                "value": to_decimal(r["VALUE"], field="VALUE"),
                "open_position": to_decimal(r["OPENPOSITION"], field="OPENPOSITION"),
                "open_position_value": to_decimal(
                    r["OPENPOSITIONVALUE"], field="OPENPOSITIONVALUE"
                ),
                "num_trades": n,
                "swap_rate": to_decimal(r["SWAPRATE"], field="SWAPRATE"),
            }
        )
    return out, skipped


_FUT_MD_REQUIRED: Final = (
    "SECID",
    "BID",
    "OFFER",
    "LAST",
    "OPEN",
    "HIGH",
    "LOW",
    "SETTLEPRICE",
    "OPENPOSITION",
    "NUMTRADES",
    "VOLTODAY",
    "VALTODAY",
    "UPDATETIME",
    "SYSTIME",
    "TRADEDATE",
    "SWAPRATE",
)
FUTURES_INTRADAY_VALUE_COLUMNS: Final = (
    "bid",
    "offer",
    "last",
    "open",
    "high",
    "low",
    "settle_price",
    "open_position",
    "num_trades",
    "vol_today",
    "val_today",
    "swap_rate",
)


def parse_futures_marketdata(
    payload: Mapping[str, Any], factors: Mapping[str, Decimal | None]
) -> tuple[list[dict[str, Any]], str]:
    """FORTS ``marketdata`` -> (``md.futures_intraday`` rows without source_variant, latency).

    ``factors`` maps secid -> :func:`price_factor` (from the same response's securities
    block); rows for secids not in ``factors`` are ignored. Rows without bid/offer/last/
    settle are dropped.
    """
    rows: list[dict[str, Any]] = []
    for r in parse_block(payload, "marketdata", required=_FUT_MD_REQUIRED):
        secid = str(_req(r, "SECID"))
        if secid not in factors:
            continue
        systime = to_datetime(r["SYSTIME"], field="SYSTIME")
        factor = factors[secid]
        bid = _price(r["BID"], field="BID", zero_is_null=True)
        offer = _price(r["OFFER"], field="OFFER", zero_is_null=True)
        last = _price(r["LAST"], field="LAST", zero_is_null=True)
        rows.append(
            {
                "ts": snapshot_ts(r["UPDATETIME"], systime),
                "secid": secid,
                "trade_date": to_date(r["TRADEDATE"], field="TRADEDATE"),
                "bid": bid,
                "offer": offer,
                "last": last,
                "open": _price(r["OPEN"], field="OPEN", zero_is_null=True),
                "high": _price(r["HIGH"], field="HIGH", zero_is_null=True),
                "low": _price(r["LOW"], field="LOW", zero_is_null=True),
                "settle_price": _price(r["SETTLEPRICE"], field="SETTLEPRICE", zero_is_null=True),
                "open_position": to_decimal(r["OPENPOSITION"], field="OPENPOSITION"),
                "num_trades": to_int(r["NUMTRADES"], field="NUMTRADES"),
                "vol_today": to_decimal(r["VOLTODAY"], field="VOLTODAY"),
                "val_today": to_decimal(r["VALTODAY"], field="VALTODAY"),
                "swap_rate": to_decimal(r["SWAPRATE"], field="SWAPRATE"),
                "bid_rub_per_unit": normalize_price(bid, factor),
                "offer_rub_per_unit": normalize_price(offer, factor),
                "last_rub_per_unit": normalize_price(last, factor),
                "source_ts": systime,
            }
        )
    latency = classify_latency((r["ts"], r["source_ts"]) for r in rows)
    kept = []
    for row in rows:
        if all(row[c] is None for c in ("bid", "offer", "last", "settle_price")):
            continue
        if row["ts"] is None:
            raise IssFormatError(f"FORTS marketdata {row['secid']}: priced row without UPDATETIME")
        row["latency"] = latency
        kept.append(row)
    return kept, latency
