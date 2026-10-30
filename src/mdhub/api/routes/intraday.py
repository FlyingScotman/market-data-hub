"""Intraday read API (``/v1/intraday/*``) and the daily series of the RGBI monitor inputs
(``/v1/index/constituents``, ``/v1/repo/{ccp,gc}``, ``/v1/history/bond_close``).

Same envelope, paging (keyset cursor), caching (ETag / Last-Modified, 304) and gzip as
``/v1/history/*`` (:mod:`mdhub.api.columnar`, docs/INTRADAY_API.md). Scope ``read``.

* Intraday endpoints select an **instant** range on the row's exchange time (``ts``,
  ``trade_ts``, ``begin_ts``): ``from``/``to`` are a date (whole Moscow day; ``to`` inclusive) or
  an ISO 8601 instant with offset; rows are ordered by time, then the series key, so a client can
  poll "everything after the last cursor/instant". Each row carries ``source_ts`` (exchange
  clock), ``ingested_at`` (hub write) and ``latency`` (``realtime`` / ``delayed``).
* ``known_at`` reads the rows as the hub knew them then: rows written later are absent, revised
  rows show their earlier image (``ops.row_history``; readers of migration 0560).
* ``GET /v1/intraday/latency``: actual delay per intraday dataset (``now - source_ts`` of the
  freshest row, and ``ingested_at - source_ts`` percentiles over the last hour).

The stored tables add no values: change-only tables hold a row when a value changed, so the
state at an instant is the latest row at or before it (no forward fill is done here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Final, LiteralString

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from psycopg import sql

from mdhub.api.columnar import (
    DEFAULT_LIMIT,
    decode_cursor,
    history_connection,
    query_hash,
)
from mdhub.api.common import bad_request, json_response, read_connection, split_list
from mdhub.api.routes.history import (
    Col,
    CursorQ,
    KnownAtQ,
    LimitQ,
    _cursor_params,
    _json_array,
    _known,
    _page,
    _select_fields,
    _version_or_304,
)
from mdhub.core.timeutil import MSK, msk_today

router = APIRouter(tags=["intraday"])

MAX_SECIDS: Final = 500
MAX_RANGE_DAYS: Final = 366
_INSTANT_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FromIQ = Annotated[
    str,
    Query(
        alias="from",
        description="start: a date (YYYY-MM-DD, from 00:00 MSK) or an ISO 8601 instant with "
        "offset (inclusive)",
    ),
]
ToIQ = Annotated[
    str | None,
    Query(
        alias="to",
        description="end: a date (whole day, inclusive) or an instant with offset (inclusive); "
        "default now",
    ),
]
FromDQ = Annotated[date, Query(alias="from", description="first date (inclusive, MSK)")]
ToDQ = Annotated[
    date | None, Query(alias="to", description="last date (inclusive); default today (MSK)")
]
SecidQ = Annotated[
    str | None,
    Query(description=f"comma list of SECIDs (case-sensitive), at most {MAX_SECIDS}"),
]
BoardQ = Annotated[str | None, Query(description="comma list of boards")]
FieldsQ = Annotated[str | None, Query(description="comma list of columns (keys always included)")]


def _instant(value: str, *, end: bool, name: str) -> datetime:
    """A date (MSK day start; ``end``: start of the next day) or an aware instant."""
    text = value.strip()
    try:
        if _INSTANT_RE.match(text):
            d = date.fromisoformat(text)
            if end:
                d += timedelta(days=1)
            return datetime.combine(d, time(0), tzinfo=MSK)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            raise bad_request(f"{name} must include a UTC offset (e.g. 2026-10-29T10:00:00+03:00)")
        # inclusive end instant -> exclusive bound
        return (dt + timedelta(microseconds=1) if end else dt).astimezone(UTC)
    except (ValueError, OverflowError):
        raise bad_request(f"{name} must be YYYY-MM-DD or an ISO 8601 instant") from None


def _instant_range(date_from: str, date_to: str | None) -> tuple[datetime, datetime]:
    start = _instant(date_from, end=False, name="from")
    stop = (
        _instant(date_to, end=True, name="to")
        if date_to
        else datetime.now(UTC) + timedelta(microseconds=1)
    )
    if start >= stop:
        raise bad_request("from must be before to")
    if stop - start > timedelta(days=MAX_RANGE_DAYS):
        raise bad_request(f"at most {MAX_RANGE_DAYS} days per call (page with cursor inside)")
    return start, stop


def _date_range(date_from: date, date_to: date | None) -> tuple[date, date]:
    end = date_to or msk_today()
    if date_from > end:
        raise bad_request("from must not be after to")
    return date_from, end


@dataclass(frozen=True, slots=True)
class Spec:
    """One endpoint over one table."""

    dataset: str
    table: LiteralString
    keys: tuple[tuple[str, LiteralString], ...]
    """Order / cursor columns with their SQL types; the first is the range column."""
    columns: list[Col]
    units: str
    known_reader: LiteralString | None
    """As-known-at reader ``f(known_at, from, to)``; ``None``: filter ``ingested_at``."""
    instant: bool = True
    """Range column is a timestamptz (instant range) - else a date."""
    fixed: dict[str, str] = field(default_factory=dict)
    """Constant filters ``column = value`` (e.g. market)."""
    filters: tuple[str, ...] = ("secid",)
    """List filters the endpoint accepts (``secid``, ``board``, ``source``, ``index_id``,
    ``basis``)."""


def _c(*names: LiteralString) -> list[Col]:
    return [Col(n, "t." + n) for n in names]


_PROVENANCE: Final = ("source_ts", "ingested_at", "latency")

FUTURES: Final = Spec(
    dataset="intraday_futures",
    table="md.futures_intraday",
    keys=(("ts", "timestamptz"), ("secid", "text"), ("source", "text"), ("source_variant", "text")),
    columns=_c(
        "ts",
        "secid",
        "source",
        "source_variant",
        "trade_date",
        "bid",
        "offer",
        "bid_depth",
        "offer_depth",
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
        "bid_rub_per_unit",
        "offer_rub_per_unit",
        "last_rub_per_unit",
        *_PROVENANCE,
    ),
    units="prices as MOEX publishes them (RGBI futures in index points x 100 for RB*, RGBIF in "
    "index points; KEYRATE/RUONIA futures 100 - rate); *_rub_per_unit only for FX contracts; "
    "settle_price = current settlement price estimate; ts = quote time (MSK exchange clock), "
    "source_ts = SYSTIME",
    known_reader="md.futures_intraday_known_at",
    filters=("secid", "source"),
)
FUTURES_TRADES: Final = Spec(
    dataset="intraday_futures_trades",
    table="md.futures_trade",
    keys=(
        ("trade_ts", "timestamptz"),
        ("secid", "text"),
        ("source", "text"),
        ("trade_no", "bigint"),
    ),
    columns=_c(
        "trade_ts",
        "secid",
        "source",
        "trade_no",
        "trade_date",
        "session_date",
        "price",
        "quantity",
        "buysell",
        "open_position",
        "off_market",
        *_PROVENANCE,
    ),
    units="price as MOEX publishes it; quantity in contracts; buysell = aggressor side (B/S); "
    "open_position after the trade; trade_ts = exchange time (MSK)",
    known_reader=None,
    filters=("secid", "source"),
)
BONDS: Final = Spec(
    dataset="intraday_bonds",
    table="md.bond_quote_intraday",
    keys=(("ts", "timestamptz"), ("secid", "text"), ("board", "text")),
    columns=_c(
        "ts",
        "secid",
        "board",
        "bid",
        "offer",
        "last",
        "open",
        "low",
        "high",
        "waprice",
        "market_price",
        "market_price_today",
        "lcurrent_price",
        "close_price",
        "yield_pct",
        "yield_at_waprice_pct",
        "yield_to_offer_pct",
        "duration_days",
        "num_trades",
        "vol_today",
        "val_today",
        "val_today_rub",
        "num_bids",
        "num_offers",
        "trading_status",
        "trading_session",
        "zspread_bp",
        "effective_yield_pct",
        "yield_date",
        "yield_date_type",
        "gspread_bp",
        *_PROVENANCE,
    ),
    units="prices in % of face as MOEX publishes them, yields % p.a., *_bp basis points; "
    "a row per change of the quote (md.bond_quote_intraday)",
    known_reader="md.bond_quote_intraday_known_at",
    filters=("secid", "board"),
)
ZCYC_SECURITIES: Final = Spec(
    dataset="intraday_zcyc_securities",
    table="md.zcyc_security_quote",
    keys=(("ts", "timestamptz"), ("secid", "text")),
    columns=_c(
        "ts",
        "secid",
        "trade_date",
        "calc_ts",
        "short_name",
        "benchmark",
        "exp_date",
        "bid_price_pct",
        "bid_yield_pct",
        "ask_price_pct",
        "ask_yield_pct",
        "trd_price_pct",
        "trd_yield_pct",
        "clc_price_pct",
        "clc_yield_pct",
        "crt_price_pct",
        "crt_yield_pct",
        "correction",
        "crt_duration_days",
        "bid_duration_days",
        "ask_duration_days",
        *_PROVENANCE,
    ),
    units="*_price_pct % of face, *_yield_pct % p.a. (MOEX), durations in days; ts = quote "
    "update time, calc_ts = source_ts = KBD calculation time (MSK)",
    known_reader="md.zcyc_security_quote_known_at",
)
INDEX: Final = Spec(
    dataset="intraday_index",
    table="md.index_intraday",
    keys=(("ts", "timestamptz"), ("secid", "text")),
    columns=_c(
        "ts",
        "secid",
        "board",
        "trade_date",
        "current_value",
        "last_value",
        "open_value",
        "high",
        "low",
        "last_change",
        "last_change_prc",
        "val_today",
        "trading_session",
        *_PROVENANCE,
    ),
    units="index points; last_value = previous close; last_change_prc %; val_today RUB",
    known_reader="md.index_intraday_known_at",
)
INDEX_CANDLES: Final = Spec(
    dataset="intraday_index_candles",
    table="md.index_candle",
    keys=(("begin_ts", "timestamptz"), ("secid", "text"), ("interval_min", "integer")),
    columns=_c(
        "begin_ts",
        "secid",
        "interval_min",
        "end_ts",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "value",
        "volume",
        *_PROVENANCE,
    ),
    units="index points; value RUB; the candle of the running minute is revised until it ends",
    known_reader="md.index_candle_known_at",
)
REPO: Final = Spec(
    dataset="intraday_repo",
    table="md.repo_intraday",
    keys=(("ts", "timestamptz"), ("market", "text"), ("board", "text"), ("secid", "text")),
    columns=_c(
        "ts",
        "market",
        "board",
        "secid",
        "trade_date",
        "settle_date",
        "settle_date2",
        "bid_pct",
        "offer_pct",
        "open_pct",
        "low_pct",
        "high_pct",
        "last_pct",
        "waprice_pct",
        "num_trades",
        "vol_today",
        "val_today_rub",
        "trading_status",
        *_PROVENANCE,
    ),
    units="repo rates % p.a.; market ccp = per-OFZ boards EQRP (1d) EQWP (7d) EQMP (1m) EQTP "
    "(3m), gcc = GC baskets by term (GCRP 1d ... GCOY 1y)",
    known_reader="md.repo_intraday_known_at",
    filters=("secid", "board", "market"),
)
CONSTITUENTS: Final = Spec(
    dataset="index_constituents",
    table="md.index_constituent",
    keys=(("trade_date", "date"), ("index_id", "text"), ("secid", "text")),
    columns=_c(
        "trade_date",
        "index_id",
        "secid",
        "ticker",
        "short_name",
        "weight_pct",
        "trading_session",
        "session_date",
        "ingested_at",
    ),
    units="weight_pct % of the index (sum ~100 per index and day)",
    known_reader="md.index_constituent_known_at",
    instant=False,
    filters=("secid", "index_id"),
)
_REPO_EOD_COLS: Final = _c(
    "trade_date",
    "board",
    "secid",
    "short_name",
    "num_trades",
    "value_rub",
    "open_pct",
    "low_pct",
    "high_pct",
    "close_pct",
    "waprice_pct",
    "trading_session",
    "ingested_at",
)
REPO_CCP: Final = Spec(
    dataset="repo_ccp",
    table="md.repo_eod",
    keys=(("trade_date", "date"), ("board", "text"), ("secid", "text")),
    columns=_REPO_EOD_COLS,
    units="repo rates % p.a.; value_rub turnover; boards EQRP 1d, EQWP 7d, EQMP 1m, EQTP 3m",
    known_reader="md.repo_eod_known_at",
    instant=False,
    fixed={"market": "ccp"},
    filters=("secid", "board"),
)
REPO_GC: Final = Spec(
    dataset="repo_gc",
    table="md.repo_eod",
    keys=(("trade_date", "date"), ("board", "text"), ("secid", "text")),
    columns=_REPO_EOD_COLS,
    units="repo rates % p.a.; GC baskets (RU000A0JW4Z1 = GC Bonds) by term GCRP 1d ... GCOY 1y",
    known_reader="md.repo_eod_known_at",
    instant=False,
    fixed={"market": "gcc"},
    filters=("secid", "board"),
)
BOND_CLOSE: Final = Spec(
    dataset="bond_close",
    table="md.bond_close_quote",
    keys=(("trade_date", "date"), ("secid", "text"), ("board", "text"), ("basis", "text")),
    columns=_c(
        "trade_date",
        "secid",
        "board",
        "basis",
        "cut_ts",
        "quote_ts",
        "close_bid_pct",
        "close_ask_pct",
        "bid_yield_pct",
        "ask_yield_pct",
        *_PROVENANCE,
    ),
    units="close_bid_pct/close_ask_pct % of face: the latest stored quote at or before cut_ts "
    "(quote_ts); basis bonds_marketdata (per board) or zcyc_securities (board ZCYC, with yields "
    "% p.a.)",
    known_reader="md.bond_close_quote_known_at",
    instant=False,
    filters=("secid", "board", "basis"),
)


_UPDATED_AT_TABLES: Final = frozenset(
    {
        "md.bond_quote_intraday",
        "md.zcyc_security_quote",
        "md.index_intraday",
        "md.index_candle",
        "md.index_constituent",
        "md.repo_intraday",
        "md.repo_eod",
        "md.bond_close_quote",
    }
)
"""Tables with ``updated_at`` (revisable: their data version includes revisions)."""


def _serve(
    request: Request,
    spec: Spec,
    *,
    lo: datetime | date,
    hi: datetime | date,
    open_end: bool = False,
    lists: dict[str, list[str] | None],
    fields: str | None,
    limit: int,
    cursor: str | None,
    known_at: datetime | None,
) -> Response:
    known = _known(known_at)
    for name, values in lists.items():
        if values is not None and len(values) > MAX_SECIDS:
            raise bad_request(f"at most {MAX_SECIDS} values of {name} per call")
    key_names = tuple(k for k, _ in spec.keys)
    cols = _select_fields(spec.columns, key_names, fields)
    norm = {
        "lo": lo,
        # without ``to`` the range ends "now": the query (ETag, cursor) stays the same
        "hi": None if open_end else hi,
        **{k: sorted(v) if v else None for k, v in lists.items()},
        "fields": [c.name for c in cols],
        "known_at": known,
    }
    qhash = query_hash(spec.dataset, norm)
    key = decode_cursor(cursor, qhash, len(spec.keys))
    params: dict[str, Any] = {"lo": lo, "hi": hi, "ka": known}
    params.update(_cursor_params("c", key))
    rng = spec.keys[0][0]
    rng_id = sql.Identifier(rng)
    where: list[sql.Composable] = []
    if known is not None and spec.known_reader is not None:
        # instant readers select [lo, hi), date readers [lo, hi] (migrations 0500, 0560)
        src: sql.Composable = sql.SQL("{}(%(ka)s, %(lo)s, %(hi)s) t").format(
            sql.SQL(spec.known_reader)
        )
    else:
        src = sql.SQL("{} t").format(sql.SQL(spec.table))
        if spec.instant:
            where.append(sql.SQL("t.{r} >= %(lo)s AND t.{r} < %(hi)s").format(r=rng_id))
        else:
            where.append(sql.SQL("t.{r} BETWEEN %(lo)s AND %(hi)s").format(r=rng_id))
        if known is not None:
            where.append(sql.SQL("t.ingested_at <= %(ka)s"))
    for col, value in spec.fixed.items():
        params[f"fx_{col}"] = value
        where.append(sql.SQL("t.{} = {}").format(sql.Identifier(col), sql.Placeholder(f"fx_{col}")))
    for col, values in lists.items():
        if values is not None:
            params[f"in_{col}"] = values
            where.append(
                sql.SQL("t.{} = ANY({})").format(sql.Identifier(col), sql.Placeholder(f"in_{col}"))
            )
    base_where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL("")
    key_ids = sql.SQL(", ").join(sql.SQL("t.{}").format(sql.Identifier(k)) for k in key_names)
    page_where = list(where)
    if key is not None:
        typed = sql.SQL(", ").join(
            sql.SQL("{}::{}").format(sql.Placeholder(f"c{i}"), sql.SQL(typ))
            for i, (_, typ) in enumerate(spec.keys)
        )
        page_where.append(sql.SQL("({}) > ({})").format(key_ids, typed))
    page_sql_where = (
        sql.SQL(" WHERE ") + sql.SQL(" AND ").join(page_where) if page_where else sql.SQL("")
    )
    page_q = (
        sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(", json_build_array({k})::text FROM {src}").format(k=key_ids, src=src)
        + page_sql_where
        + sql.SQL(" ORDER BY {k} LIMIT {n}").format(k=key_ids, n=sql.Literal(limit + 1))
    )
    version_q = None
    if known is None:
        stamp = (
            sql.SQL("max(COALESCE(t.updated_at, t.ingested_at))")
            if spec.table in _UPDATED_AT_TABLES
            else sql.SQL("max(t.ingested_at)")
        )
        version_q = sql.SQL("SELECT count(*), {} FROM {}").format(stamp, src) + base_where
    with history_connection(request) as conn:
        version, hit = _version_or_304(
            request,
            conn,
            dataset=spec.dataset,
            qhash=qhash,
            query=version_q,
            params=params,
            cursor_key=key,
            limit=limit,
        )
        if hit is not None:
            return hit
        return _page(
            request,
            conn,
            dataset=spec.dataset,
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": lo,
                "to": hi,
                "range_column": rng,
                "range": "[from, to)" if spec.instant else "[from, to]",
                "order": list(key_names),
                "units": spec.units,
            },
            version=version,
            cursor_key=key,
        )


def _instant_endpoint(
    request: Request,
    spec: Spec,
    date_from: str,
    date_to: str | None,
    lists: dict[str, list[str] | None],
    fields: str | None,
    limit: int,
    cursor: str | None,
    known_at: datetime | None,
) -> Response:
    lo, hi = _instant_range(date_from, date_to)
    return _serve(
        request,
        spec,
        lo=lo,
        hi=hi,
        open_end=date_to is None,
        lists=lists,
        fields=fields,
        limit=limit,
        cursor=cursor,
        known_at=known_at,
    )


def _date_endpoint(
    request: Request,
    spec: Spec,
    date_from: date,
    date_to: date | None,
    lists: dict[str, list[str] | None],
    fields: str | None,
    limit: int,
    cursor: str | None,
    known_at: datetime | None,
) -> Response:
    lo, hi = _date_range(date_from, date_to)
    return _serve(
        request,
        spec,
        lo=lo,
        hi=hi,
        open_end=date_to is None,
        lists=lists,
        fields=fields,
        limit=limit,
        cursor=cursor,
        known_at=known_at,
    )


# ---------------------------------------------------------------------------- intraday


@router.get("/intraday/futures", summary="FORTS futures marketdata, intraday (change-only)")
def intraday_futures(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    source: Annotated[str | None, Query(description="moex_apim, moex_iss")] = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.futures_intraday``: FX and rates contracts (RGBI ``RB*``, ``RGBIF``, ``KEYRATE``,
    ``RUONIA``); ordered by ts, secid, source, source_variant. APIM rows are realtime, public
    ISS fallback rows delayed (``latency``)."""
    lists = {"secid": split_list(secid), "source": split_list(source)}
    return _instant_endpoint(
        request, FUTURES, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/futures_trades", summary="FORTS trades of the rates contracts")
def intraday_futures_trades(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    source: Annotated[str | None, Query(description="moex_apim, moex_iss")] = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.futures_trade``; ordered by trade_ts, secid, source, trade_no. A trade read from
    both feeds appears once per ``source``: filter one source."""
    lists = {"secid": split_list(secid), "source": split_list(source)}
    return _instant_endpoint(
        request, FUTURES_TRADES, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/bonds", summary="Bond quotes intraday (md.bond_quote_intraday)")
def intraday_bonds(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    board: BoardQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """Changed quotes of every board; ordered by ts, secid, board (public ISS: delayed)."""
    lists = {"secid": split_list(secid), "board": split_list(board, upper=True)}
    return _instant_endpoint(
        request, BONDS, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/zcyc_securities", summary="KBD per-OFZ bid/ask (zcyc securities)")
def intraday_zcyc_securities(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.zcyc_security_quote``; ordered by ts, secid."""
    lists = {"secid": split_list(secid)}
    return _instant_endpoint(
        request, ZCYC_SECURITIES, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/index", summary="Index values intraday (RGBI, RGBITR)")
def intraday_index(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.index_intraday`` (changed values); ordered by ts, secid."""
    lists = {"secid": split_list(secid)}
    return _instant_endpoint(
        request, INDEX, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/index_candles", summary="Index 1-minute candles (RGBI, RGBITR)")
def intraday_index_candles(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    secid: SecidQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.index_candle`` by begin_ts; ordered by begin_ts, secid, interval_min."""
    lists = {"secid": split_list(secid)}
    return _instant_endpoint(
        request, INDEX_CANDLES, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/intraday/repo", summary="Repo with the CCP intraday (ccp per OFZ, gcc GC)")
def intraday_repo(
    request: Request,
    date_from: FromIQ,
    date_to: ToIQ = None,
    market: Annotated[str | None, Query(description="ccp, gcc (default both)")] = None,
    secid: SecidQ = None,
    board: BoardQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.repo_intraday`` (changed rows; public ISS delayed); ordered by ts, market, board,
    secid."""
    markets = split_list(market)
    if markets and set(markets) - {"ccp", "gcc"}:
        raise bad_request("market must be ccp or gcc")
    lists = {
        "secid": split_list(secid),
        "board": split_list(board, upper=True),
        "market": markets,
    }
    return _instant_endpoint(
        request, REPO, date_from, date_to, lists, fields, limit, cursor, known_at
    )


# ------------------------------------------------------------------------------- daily


@router.get("/index/constituents", summary="Index composition and weights per day")
def index_constituents(
    request: Request,
    date_from: FromDQ,
    date_to: ToDQ = None,
    index: Annotated[str | None, Query(description="comma list of indices (default all)")] = None,
    secid: SecidQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.index_constituent``; ordered by trade_date, index_id, secid."""
    lists = {"secid": split_list(secid), "index_id": split_list(index, upper=True)}
    return _date_endpoint(
        request, CONSTITUENTS, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/repo/ccp", summary="Repo with the CCP per OFZ: daily history")
def repo_ccp(
    request: Request,
    date_from: FromDQ,
    date_to: ToDQ = None,
    secid: SecidQ = None,
    board: BoardQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.repo_eod`` market ccp; ordered by trade_date, board, secid."""
    lists = {"secid": split_list(secid), "board": split_list(board, upper=True)}
    return _date_endpoint(
        request, REPO_CCP, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/repo/gc", summary="GC repo with the CCP by term: daily history")
def repo_gc(
    request: Request,
    date_from: FromDQ,
    date_to: ToDQ = None,
    secid: SecidQ = None,
    board: BoardQ = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.repo_eod`` market gcc; ordered by trade_date, board, secid."""
    lists = {"secid": split_list(secid), "board": split_list(board, upper=True)}
    return _date_endpoint(
        request, REPO_GC, date_from, date_to, lists, fields, limit, cursor, known_at
    )


@router.get("/history/bond_close", summary="Closing bid/ask of bonds at the close cut")
def history_bond_close(
    request: Request,
    date_from: FromDQ,
    date_to: ToDQ = None,
    secid: SecidQ = None,
    board: BoardQ = None,
    basis: Annotated[
        str | None, Query(description="bonds_marketdata, zcyc_securities (default both)")
    ] = None,
    fields: FieldsQ = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """``md.bond_close_quote``; ordered by trade_date, secid, board, basis."""
    lists = {
        "secid": split_list(secid),
        "board": split_list(board, upper=True),
        "basis": split_list(basis),
    }
    return _date_endpoint(
        request, BOND_CLOSE, date_from, date_to, lists, fields, limit, cursor, known_at
    )


# ----------------------------------------------------------------------------- latency

LATENCY_DATASETS: Final[tuple[tuple[LiteralString, LiteralString, LiteralString], ...]] = (
    ("futures_intraday", "md.futures_intraday", "ts"),
    ("futures_trade", "md.futures_trade", "trade_ts"),
    ("bond_quote_intraday", "md.bond_quote_intraday", "ts"),
    ("zcyc_security_quote", "md.zcyc_security_quote", "ts"),
    ("index_intraday", "md.index_intraday", "ts"),
    ("index_candle", "md.index_candle", "begin_ts"),
    ("repo_intraday", "md.repo_intraday", "ts"),
)
"""(dataset, table, exchange-time column) of the intraday tables with ``source_ts``."""
LATENCY_WINDOW: Final = timedelta(hours=1)


def latency_query() -> sql.Composed:
    """Per dataset and latency label: exchange observation age and ingestion delay.

    SYSTIME can be current even for a delayed quote; use the dataset's observation clock.
    """
    parts = [
        sql.SQL(
            "SELECT {ds}::text AS dataset, t.latency, max(t.source_ts) AS last_source_ts, "
            "max(t.ingested_at) AS last_ingested_at, "
            "round(extract(epoch FROM now() - max(t.{col}))::numeric, 1) AS age_s, "
            "count(*) FILTER (WHERE t.ingested_at >= now() - %(win)s) AS rows_last_hour, "
            "round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM "
            "t.ingested_at - t.{col})) FILTER (WHERE t.ingested_at >= now() - %(win)s)"
            "::numeric, 1) AS lag_p50_s, "
            "round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM "
            "t.ingested_at - t.{col})) FILTER (WHERE t.ingested_at >= now() - %(win)s)"
            "::numeric, 1) AS lag_p95_s, "
            "round(max(extract(epoch FROM t.ingested_at - t.{col})) FILTER (WHERE "
            "t.ingested_at >= now() - %(win)s)::numeric, 1) AS lag_max_s "
            "FROM {tbl} t WHERE t.{col} >= now() - interval '4 days' GROUP BY t.latency"
        ).format(ds=sql.Literal(ds), tbl=sql.SQL(tbl), col=sql.Identifier(col))
        for ds, tbl, col in LATENCY_DATASETS
    ]
    return sql.SQL(" UNION ALL ").join(parts) + sql.SQL(" ORDER BY dataset, latency")


@router.get("/intraday/latency", summary="Actual delay per intraday dataset (ops)")
def intraday_latency(request: Request) -> Response:
    """Per intraday dataset and ``latency`` label (rows of the last 4 days): ``age_s`` = now -
    the freshest observation time (quote/trade time, or candle start), and ``lag_*_s`` =
    ``ingested_at - observation time`` over the rows written in the last hour (how late rows reach the
    hub: ~0-60 s realtime, ~900 s delayed public ISS). Change-only tables write nothing while
    the market is quiet: a large ``age_s`` outside trading hours is expected."""
    with read_connection(request) as conn, conn.cursor() as cur:
        rows = cur.execute(latency_query(), {"win": LATENCY_WINDOW}).fetchall()
    return json_response(
        request,
        {"data": rows, "meta": {"returned": len(rows), "window_s": LATENCY_WINDOW.seconds}},
        etag=False,
    )
