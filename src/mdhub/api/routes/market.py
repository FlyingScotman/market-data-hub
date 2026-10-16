"""FX, futures, fixings, curves, ZCYC, indices and calendar endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from psycopg import sql

from mdhub.api.common import (
    Page,
    Where,
    date_range,
    fetch_all,
    json_response,
    page_params,
    read_connection,
    require_aware,
    split_list,
)
from mdhub.core.timeutil import utc_now

router = APIRouter()

DateQ = Annotated[date | None, Query(alias="date", description="single date")]
FromQ = Annotated[date | None, Query(alias="from", description="range start (inclusive)")]
ToQ = Annotated[date | None, Query(alias="to", description="range end (inclusive)")]
PageDep = Annotated[Page, Depends(page_params)]


def _paged(
    request: Request,
    page: Page,
    select: sql.Composed | sql.SQL,
    where: Where,
    order: sql.SQL,
    **meta: object,
) -> Response:
    query = select + where.sql() + order + page.sql()
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, page.wrap(rows, **meta))


# ------------------------------------------------------------------------------- FX


@router.get("/fx/official", tags=["fx"], summary="CBR official FX by effective date")
def fx_official(
    request: Request,
    page: PageDep,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    ccy: Annotated[str | None, Query(description="comma list, e.g. USD,EUR,CNY,CHF")] = None,
) -> Response:
    """One row per **calendar** day the rate is in force (weekends included).
    ``rate_per_unit`` = RUB per 1 unit (``value`` / ``nominal``); ``set_date`` = day CBR set
    the rate; ``record_date`` = first day in force."""
    d1, d2 = date_range(day, date_from, date_to)
    where = Where().add("effective_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    ccys = split_list(ccy, upper=True)
    if ccys:
        where.add("ccy = ANY(%(ccys)s)", ccys=ccys)
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT effective_date, ccy, rate_per_unit, nominal, value AS value_rub_per_nominal, "
            "vunit_rate, set_date, record_date, cbr_id, source, ingested_at FROM md.fx_official"
        ),
        where,
        sql.SQL(" ORDER BY effective_date, ccy"),
        date_from=d1,
        date_to=d2,
        units="rate_per_unit: RUB per 1 unit of ccy",
    )


@router.get("/fx/eod", tags=["fx"], summary="MOEX CETS (selt) EOD history")
def fx_eod(
    request: Request,
    page: PageDep,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    secid: Annotated[str | None, Query(description="comma list, e.g. CNYRUB_TOM")] = None,
    board: Annotated[str | None, Query(description="comma list, e.g. CETS")] = None,
) -> Response:
    """Prices in quote currency per 1 unit of base (as MOEX publishes them); NULL when there
    were no trades."""
    d1, d2 = date_range(day, date_from, date_to)
    where = Where().add("e.trade_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    secids = split_list(secid)
    if secids:
        where.add("e.secid = ANY(%(secids)s)", secids=secids)
    boards = split_list(board, upper=True)
    if boards:
        where.add("e.board = ANY(%(boards)s)", boards=boards)
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT e.trade_date, e.secid, e.board, i.base_ccy, i.quote_ccy, i.tenor, i.kind, "
            "e.open, e.low, e.high, e.close, e.waprice, e.num_trades, e.ingested_at "
            "FROM md.fx_eod e LEFT JOIN ref.fx_instrument i "
            "ON i.secid = e.secid AND i.board = e.board"
        ),
        where,
        sql.SQL(" ORDER BY e.trade_date, e.secid, e.board"),
        date_from=d1,
        date_to=d2,
    )


@router.get("/fx/indicative", tags=["fx"], summary="MOEX indicative FX (14:00 tc / 19:00 mc)")
def fx_indicative(
    request: Request,
    page: PageDep,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    clearing: Annotated[str | None, Query(description="tc (14:00) or mc (19:00)")] = None,
    pair: Annotated[str | None, Query(description="comma list, e.g. USD/RUB,CNY/RUB")] = None,
) -> Response:
    """``rate`` = quote currency per 1 unit of base currency. The 19:00 (``mc``) rates are the
    hub's labelled candidate for the desk FX mark."""
    d1, d2 = date_range(day, date_from, date_to)
    where = Where().add("trade_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    if clearing:
        where.add("clearing = %(clearing)s", clearing=clearing.lower())
    pairs = split_list(pair, upper=True)
    if pairs:
        where.add("ccy_pair = ANY(%(pairs)s)", pairs=pairs)
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT trade_date, clearing, ccy_pair, base_ccy, quote_ccy, trade_time, rate, source, "
            "ingested_at FROM md.fx_indicative"
        ),
        where,
        sql.SQL(" ORDER BY trade_date, clearing, ccy_pair"),
        date_from=d1,
        date_to=d2,
    )


@router.get("/fx/ecb", tags=["fx"], summary="ECB euro reference rates")
def fx_ecb(
    request: Request,
    page: PageDep,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    ccy: Annotated[str | None, Query(description="comma list")] = None,
) -> Response:
    d1, d2 = date_range(day, date_from, date_to)
    where = Where().add("rate_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    ccys = split_list(ccy, upper=True)
    if ccys:
        where.add("ccy = ANY(%(ccys)s)", ccys=ccys)
    return _paged(
        request,
        page,
        sql.SQL("SELECT rate_date, ccy, rate_per_eur, ingested_at FROM md.ecb_fx"),
        where,
        sql.SQL(" ORDER BY rate_date, ccy"),
        date_from=d1,
        date_to=d2,
        units="rate_per_eur: units of ccy per 1 EUR",
    )


# -------------------------------------------------------------------------- futures


@router.get("/futures/eod", tags=["futures"], summary="FORTS futures EOD (FX by default)")
def futures_eod(
    request: Request,
    page: PageDep,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    secid: Annotated[str | None, Query(description="comma list")] = None,
    asset_code: Annotated[str | None, Query(description="comma list, e.g. Si,Eu,CNY")] = None,
    fx_only: Annotated[bool, Query(description="only currency futures")] = True,
) -> Response:
    """Raw MOEX prices plus ``settle_rub_per_unit`` / ``close_rub_per_unit`` = raw x STEPPRICE /
    MINSTEP / LOTVOLUME (RUB per 1 unit of base currency; RUB-quoted FX contracts only).
    Perpetuals (``contract_type = perpetual``) are futures, never spot."""
    d1, d2 = date_range(day, date_from, date_to)
    where = Where().add("f.trade_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    secids = split_list(secid)
    if secids:
        where.add("f.secid = ANY(%(secids)s)", secids=secids)
    assets = split_list(asset_code)
    if assets:
        where.add("f.asset_code = ANY(%(assets)s)", assets=assets)
    if fx_only:
        where.add("c.is_fx")
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT f.trade_date, f.secid, f.board, f.asset_code, c.contract_type, c.base_ccy, "
            "c.quote_ccy, c.last_trade_date, c.step_price, c.min_step, c.lot_volume, "
            "f.open, f.low, f.high, f.close, f.settle_price, f.waprice, "
            "f.volume AS volume_contracts, "
            "f.value AS value_rub, f.open_position AS open_position_contracts, "
            "f.open_position_value AS open_position_value_rub, f.num_trades, f.swap_rate, "
            "f.settle_rub_per_unit, f.close_rub_per_unit, f.ingested_at "
            "FROM md.futures_eod f LEFT JOIN ref.futures_contract c ON c.secid = f.secid"
        ),
        where,
        sql.SQL(" ORDER BY f.trade_date, f.secid"),
        date_from=d1,
        date_to=d2,
    )


@router.get("/futures/contracts", tags=["futures"], summary="FORTS futures static")
def futures_contracts(
    request: Request,
    page: PageDep,
    asset_code: Annotated[str | None, Query(description="comma list")] = None,
    fx_only: bool = True,
    active_on: Annotated[
        date | None, Query(description="tradable on this date (perpetuals always)")
    ] = None,
) -> Response:
    where = Where()
    assets = split_list(asset_code)
    if assets:
        where.add("asset_code = ANY(%(assets)s)", assets=assets)
    if fx_only:
        where.add("is_fx")
    if active_on is not None:
        where.add(
            "(first_trade_date IS NULL OR first_trade_date <= %(d)s) "
            "AND (last_trade_date IS NULL OR last_trade_date >= %(d)s)",
            d=active_on,
        )
    return _paged(
        request,
        page,
        sql.SQL("SELECT * FROM ref.futures_contract"),
        where,
        sql.SQL(" ORDER BY asset_code, last_trade_date NULLS FIRST, secid"),
    )


# ------------------------------------------------------------------------- fixings


@router.get("/fixings", tags=["rates"], summary="Official fixings (md.fixing)")
def fixings(
    request: Request,
    page: PageDep,
    index: Annotated[
        str | None,
        Query(description="index codes, comma list: KEY_RATE, RUONIA, RUONIA_INDEX, ..."),
    ] = None,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
    source: str | None = None,
    known_at: Annotated[
        datetime | None,
        Query(description="values as known at this time (ISO 8601 with offset)"),
    ] = None,
) -> Response:
    """``fixing_date``: KEY_RATE = the day the rate is in force (= ``effective_date``, one row
    per CBR business day); RUONIA = observation date, published on ``published_on`` (next
    business day); term RUONIA / RUONIA_INDEX per calendar day. ``value`` in % p.a.
    (``unit = pct``) or index level (``unit = index``). ``ingested_at`` = when the hub first
    knew the current value. Default range: last 31 days."""
    known = require_aware(known_at, "known_at")
    d1, d2 = date_range(day, date_from, date_to, default_days=31)
    where = Where()
    codes = split_list(index, upper=True)
    if codes:
        where.add("index_code = ANY(%(codes)s)", codes=codes)
    if source:
        where.add("source = %(source)s", source=source)
    cols = sql.SQL(
        "SELECT index_code, fixing_date, value, unit, effective_date, published_on, published_at, "
        "source, details, ingested_at "
    )
    if known is None:
        select = cols + sql.SQL("FROM md.fixing")
        where.add("fixing_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    else:
        select = cols + sql.SQL("FROM md.fixing_known_at(%(known)s, %(d1)s, %(d2)s)")
        where.params.update(known=known, d1=d1, d2=d2)
    return _paged(
        request,
        page,
        select,
        where,
        sql.SQL(" ORDER BY index_code, fixing_date, source"),
        date_from=d1,
        date_to=d2,
        known_at=known,
    )


# -------------------------------------------------------------------------- curves


@router.get("/curves", tags=["curves"], summary="Curve datasets and sources")
def curves(request: Request) -> Response:
    with read_connection(request) as conn:
        rows = fetch_all(
            conn,
            sql.SQL(
                "SELECT dataset, source, count(*) AS snapshots, "
                "min(fetched_at) AS first_fetched_at, "
                "max(fetched_at) AS last_fetched_at FROM md.curve_snapshot "
                "GROUP BY dataset, source ORDER BY dataset, source"
            ),
        )
    return json_response(request, {"data": rows, "meta": {"returned": len(rows)}}, etag=False)


@router.get("/curves/{dataset}", tags=["curves"], summary="Latest curve snapshot known at as_of")
def curve(
    request: Request,
    dataset: str,
    as_of: Annotated[
        datetime | None,
        Query(description="ISO 8601 with offset; default now. Snapshot fetched_at <= as_of"),
    ] = None,
    source: Annotated[str | None, Query(description="comma list of sources")] = None,
) -> Response:
    """One snapshot per source: the latest whose ``fetched_at`` <= ``as_of`` (snapshots are
    stored only when content changed, so it is the state the source showed at ``as_of``).
    Quotes in % p.a. (``bid_pct``/``ask_pct``/``mid_pct``)."""
    at = require_aware(as_of, "as_of") or utc_now()
    where = Where().add(
        "dataset = %(dataset)s AND fetched_at <= %(at)s", dataset=dataset.lower(), at=at
    )
    sources = split_list(source)
    if sources:
        where.add("source = ANY(%(sources)s)", sources=sources)
    query = (
        sql.SQL(
            "SELECT DISTINCT ON (source) id, source, dataset, quote_basis, fetched_at, "
            "snapshot_date, source_ts, market_closed, content_sha256, parser_version, label, meta "
            "FROM md.curve_snapshot"
        )
        + where.sql()
        + sql.SQL(" ORDER BY source, fetched_at DESC, id DESC")
    )
    with read_connection(request) as conn:
        snaps = fetch_all(conn, query, where.params)
        ids = [s["id"] for s in snaps]
        quotes = fetch_all(
            conn,
            sql.SQL(
                "SELECT snapshot_id, tenor, position, tenor_months, tenor_days, pillar_date, "
                "bid_pct, ask_pct, mid_pct, extra FROM md.curve_quote "
                "WHERE snapshot_id = ANY(%(ids)s) ORDER BY snapshot_id, position"
            ),
            {"ids": ids},
        )
    by_snap: dict[int, list[dict[str, object]]] = {}
    for q in quotes:
        sid = q.pop("snapshot_id")
        by_snap.setdefault(sid, []).append(q)
    data = [{**s, "quotes": by_snap.get(s["id"], [])} for s in snaps]
    return json_response(
        request, {"data": data, "meta": {"dataset": dataset.lower(), "as_of": at}}, etag=False
    )


# ---------------------------------------------------------------------------- ZCYC


@router.get("/zcyc", tags=["curves"], summary="MOEX KBD (zero-coupon curve) for a date")
def zcyc(
    request: Request,
    day: Annotated[date, Query(alias="date", description="trade date")],
    eod_only: Annotated[bool, Query(description="only the EOD publication")] = True,
) -> Response:
    """``params``: B1..B3, T1, G1..G9 (G in bp, continuous compounding); ``standard_yields``:
    ``[{tenor_years, yield_pct}]`` effective annual %. Only snapshots that reproduce their
    published yields within ``check_tolerance_bp`` are stored."""
    where = Where().add("trade_date = %(d)s", d=day)
    if eod_only:
        where.add("is_eod")
    query = (
        sql.SQL(
            "SELECT trade_date, trade_time, source_ts, is_eod, params, standard_yields, "
            "check_max_error_bp, check_tolerance_bp, ingested_at FROM md.zcyc_snapshot"
        )
        + where.sql()
        + sql.SQL(" ORDER BY trade_time")
    )
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, {"data": rows, "meta": {"date": day, "returned": len(rows)}})


# ------------------------------------------------------------------------- indices


@router.get("/indices/eod", tags=["rates"], summary="MOEX indices EOD (RUSFAR, RGBI, ...)")
def indices_eod(
    request: Request,
    page: PageDep,
    secid: Annotated[str | None, Query(description="comma list, e.g. RUSFAR,RUSFARCNY")] = None,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
) -> Response:
    """``close`` = index value; for RUSFAR* the fixing in % p.a. ``basis = marketdata``: same-day
    preliminary value, replaced by ``history`` later. Default range: last 31 days."""
    d1, d2 = date_range(day, date_from, date_to, default_days=31)
    where = Where().add("trade_date BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    secids = split_list(secid)
    if secids:
        where.add("secid = ANY(%(secids)s)", secids=secids)
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT trade_date, secid, board, short_name, open, high, low, close, value, volume, "
            "duration_days, yield_pct, capitalization, currency_id, basis, source_ts, ingested_at "
            "FROM md.index_eod"
        ),
        where,
        sql.SQL(" ORDER BY secid, trade_date"),
        date_from=d1,
        date_to=d2,
    )


# ------------------------------------------------------------------------ calendar


@router.get("/calendar", tags=["calendar"], summary="Business-day calendars (by source priority)")
def calendar(
    request: Request,
    page: PageDep,
    calendar: Annotated[
        str | None, Query(description="MOEX_STOCK, MOEX_FX, MOEX_FORTS, RU_PRODUCTION (comma)")
    ] = None,
    day: DateQ = None,
    date_from: FromQ = None,
    date_to: ToQ = None,
) -> Response:
    """Flag per (calendar, day) from ``ref.calendar_effective`` - the highest-priority source
    (observed > MOEX ISS calendar > curated > curated_unverified; ``known_at`` breaks ties) -
    with its source and version. Days without any information are absent (never assumed)."""
    d1, d2 = date_range(day, date_from, date_to, default_days=366)
    where = Where().add("day BETWEEN %(d1)s AND %(d2)s", d1=d1, d2=d2)
    cals = split_list(calendar, upper=True)
    if cals:
        where.add("calendar = ANY(%(cals)s)", cals=cals)
    return _paged(
        request,
        page,
        sql.SQL(
            "SELECT calendar, day, is_business_day, source, version, known_at "
            "FROM ref.calendar_effective"
        ),
        where,
        sql.SQL(" ORDER BY calendar, day"),
        date_from=d1,
        date_to=d2,
    )
