"""``/v1/history/*``: columnar history of bonds, curves, ZCYC, fixings and FX (DESIGN §8).

Thin, indexed reads of the stored tables (or, with ``known_at``, of the same as-known-at readers
the snapshot builders use), paged with a keyset cursor and cached with ETag / Last-Modified.
Response envelope, paging, caching and gzip: :mod:`mdhub.api.columnar`. Scope ``read``.

Endpoints:

* ``GET /v1/history/index`` - datasets and series with depth and row counts.
* ``GET /v1/history/bonds`` - EOD prices, accrued, volumes and MOEX analytics per (date, bond)
  on the bond's main board (``board=primary``) or on all / chosen boards.
* ``GET /v1/history/curves/{dataset}`` - per business day and source the end-of-day quote set
  chosen by the ``eod_final`` rule (``rule=eod``), or every stored snapshot (``rule=all``).
* ``GET /v1/history/zcyc`` - MOEX KBD parameters and standard yields per day.
* ``GET /v1/history/fixings`` - ``md.fixing`` plus the RUSFAR family (correct units).
* ``GET /v1/history/fx`` - CBR official, MOEX CETS EOD outrights, MOEX indicative 14:00/19:00.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, LiteralString

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response
from psycopg import sql
from psycopg.rows import tuple_row

from mdhub.api.columnar import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Columnar,
    Version,
    decode_cursor,
    encode_cursor,
    fetch_page,
    fetch_version,
    history_connection,
    not_modified,
    query_hash,
    read_as_of,
    respond,
    version_etag,
)
from mdhub.api.common import bad_request, require_aware, split_list
from mdhub.core.timeutil import msk_today
from mdhub.db.pool import DictConnection
from mdhub.snapshot.market import RUSFAR_INDEX_SERIES, RUSFAR_RATE_SERIES, rusfar_unit

router = APIRouter(prefix="/history", tags=["history"])

MAX_SECIDS: Final = 500
MAX_CODES: Final = 200
MAX_PAIRS: Final = 100
INDEX_CACHE_TTL_S: Final = 300.0
_DATASET_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

FromQ = Annotated[date, Query(alias="from", description="first date (inclusive, MSK)")]
ToQ = Annotated[
    date | None, Query(alias="to", description="last date (inclusive); default today (MSK)")
]
LimitQ = Annotated[int, Query(ge=1, le=MAX_LIMIT, description=f"rows per page (max {MAX_LIMIT})")]
CursorQ = Annotated[
    str | None,
    Query(max_length=2048, description="opaque cursor from `next` of the previous page"),
]
KnownAtQ = Annotated[
    datetime | None,
    Query(
        description="read as known at this instant (ISO 8601 with offset), with the readers "
        "the snapshot builders use; default: the current state"
    ),
]


@dataclass(frozen=True, slots=True)
class Col:
    """An output column: name and SQL expression (constant, written here)."""

    name: str
    expr: LiteralString


def _json_array(cols: list[Col]) -> sql.Composed:
    return sql.SQL("json_build_array({})::text").format(
        sql.SQL(", ").join(sql.SQL(c.expr) for c in cols)
    )


def _known(value: datetime | None) -> datetime | None:
    """``known_at`` (offset required), normalized to UTC."""
    known = require_aware(value, "known_at")
    return known.astimezone(UTC) if known is not None else None


def _range(date_from: date, date_to: date | None) -> tuple[date, date]:
    end = date_to or msk_today()
    if date_from > end:
        raise bad_request("from must not be after to")
    return date_from, end


def _select_fields(all_cols: list[Col], keys: tuple[str, ...], fields: str | None) -> list[Col]:
    """Key columns first (always), then the requested fields in the canonical order."""
    wanted = split_list(fields)
    if wanted is None:
        return all_cols
    names = {c.name for c in all_cols}
    unknown = sorted(set(wanted) - names)
    if unknown:
        raise bad_request(
            f"unknown fields: {', '.join(unknown)}; valid: {', '.join(c.name for c in all_cols)}"
        )
    keep = set(wanted) | set(keys)
    return [c for c in all_cols if c.name in keep]


def _page(
    request: Request,
    conn: DictConnection,
    *,
    dataset: str,
    qhash: str,
    cols: list[Col],
    page_query: sql.Composed,
    params: dict[str, Any],
    limit: int,
    known_at: datetime | None,
    meta: dict[str, Any],
    version: Version | None,
    cursor_key: list[Any] | None,
) -> Response:
    rows, last_key = fetch_page(conn, page_query, params, limit)
    page = Columnar(
        dataset=dataset,
        columns=[c.name for c in cols],
        rows=rows,
        as_of=read_as_of(conn),
        known_at=known_at,
        next=encode_cursor(qhash, last_key) if last_key is not None else None,
        meta={**meta, "limit": limit, "returned": len(rows)},
        data_version=version.data_version if version is not None else None,
    )
    etag = (
        version_etag(dataset, qhash, [cursor_key, limit], version) if version is not None else None
    )
    return respond(request, page, etag)


def _version_or_304(
    request: Request,
    conn: DictConnection,
    *,
    dataset: str,
    qhash: str,
    query: sql.Composed | sql.SQL | None,
    params: dict[str, Any],
    cursor_key: list[Any] | None,
    limit: int,
) -> tuple[Version | None, Response | None]:
    """Version of the selection (``None`` for ``known_at`` reads) and a 304 when it matches."""
    if query is None:
        return None, None
    version = fetch_version(conn, query, params)
    etag = version_etag(dataset, qhash, [cursor_key, limit], version)
    return version, not_modified(request, etag, version.data_version)


def _cursor_params(prefix: str, key: list[Any] | None) -> dict[str, Any]:
    if key is None:
        return {}
    return {f"{prefix}{i}": v for i, v in enumerate(key)}


# ------------------------------------------------------------------------------- bonds

BOND_COLUMNS: Final = [
    Col("trade_date", "k.trade_date"),
    Col("secid", "k.secid"),
    Col("board", "k.board"),
    Col("open_pct", "e.open"),
    Col("low_pct", "e.low"),
    Col("high_pct", "e.high"),
    Col("close_pct", "e.close"),
    Col("legal_close_pct", "e.legal_close"),
    Col("waprice_pct", "e.waprice"),
    Col("market_price2_pct", "e.market_price2"),
    Col("market_price3_pct", "e.market_price3"),
    Col("admitted_quote_pct", "e.admitted_quote"),
    Col("accrued_per_bond", "e.accrued_int"),
    Col("face_value", "e.face_value"),
    Col("face_unit", "e.face_unit"),
    Col("currency_id", "e.currency_id"),
    Col("volume_bonds", "e.volume"),
    Col("value_ccy", "e.value"),
    Col("num_trades", "e.num_trades"),
    Col("trading_session", "e.trading_session"),
    Col("yield_close_pct", "e.yield_close_pct"),
    Col("yield_at_waprice_pct", "e.yield_at_waprice_pct"),
    Col("effective_yield_pct", "y.effective_yield_pct"),
    Col("effective_yield_waprice_pct", "y.effective_yield_waprice_pct"),
    Col("yield_to_offer_pct", "COALESCE(y.yield_to_offer_pct, e.yield_to_offer_pct)"),
    Col("yield_date", "y.yield_date"),
    Col("yield_date_type", "y.yield_date_type"),
    Col("duration_days", "COALESCE(y.duration_days, e.duration_days)"),
    Col("duration_waprice_days", "y.duration_waprice_days"),
    Col("zspread_bp", "COALESCE(y.zspread_bp, e.zspread_bp)"),
    Col("gspread_bp", "y.gspread_bp"),
    Col("yield_last_coupon_pct", "COALESCE(y.yield_last_coupon_pct, e.yield_last_coupon_pct)"),
    Col("icpi", "y.icpi"),
    Col("ir", "y.ir"),
    Col("bei", "y.bei"),
    Col("cbr", "y.cbr"),
]
BOND_KEYS: Final = ("trade_date", "secid", "board")
BOND_UNITS: Final = (
    "same fields as the snapshot BondQuote (contract 1.2): *_pct prices in % of face as MOEX "
    "publishes them, yields in % p.a.; accrued_per_bond per bond in currency_id; value_ccy "
    "turnover in currency_id (SUR = RUB); volume_bonds in bonds; *_bp basis points; *_days days; "
    "icpi/ir/bei/cbr MOEX yields fields verbatim. yield_to_offer_pct, duration_days, zspread_bp, "
    "yield_last_coupon_pct: MOEX yields endpoint, else the history row"
)


def _bond_sources(known: bool) -> tuple[sql.SQL, sql.SQL, sql.SQL]:
    if known:
        return (
            sql.SQL("md.bond_eod_known_at(%(ka)s, %(d1)s, %(d2)s)"),
            sql.SQL("md.bond_eod_yield_known_at(%(ka)s, %(d1)s, %(d2)s)"),
            sql.SQL("ref.bond_known_at(%(ka)s)"),
        )
    return sql.SQL("md.bond_eod"), sql.SQL("md.bond_eod_yield"), sql.SQL("ref.bond")


def _bond_filter(
    alias: str, *, secids: bool, boards: bool, primary: bool, cursor: bool
) -> sql.Composed:
    a = sql.Identifier(alias)
    parts: list[sql.Composable] = [sql.SQL("{a}.trade_date BETWEEN %(d1)s AND %(d2)s").format(a=a)]
    if secids:
        parts.append(sql.SQL("{a}.secid = ANY(%(secids)s)").format(a=a))
    if boards:
        parts.append(sql.SQL("{a}.board = ANY(%(boards)s)").format(a=a))
    if primary:
        parts.append(
            sql.SQL("{a}.board = COALESCE(b.marketprice_board, b.primary_board)").format(a=a)
        )
    if cursor:
        parts.append(
            sql.SQL(
                "({a}.secid, {a}.trade_date, {a}.board) > (%(c0)s, %(c1)s::date, %(c2)s)"
            ).format(a=a)
        )
    return sql.SQL(" AND ").join(parts)


def _bond_cte(
    known: bool, *, secids: bool, boards: bool, primary: bool, cursor: bool
) -> sql.Composed:
    eod, yld, ref = _bond_sources(known)
    join = sql.SQL(" JOIN {ref} b ON b.secid = t.secid").format(ref=ref) if primary else sql.SQL("")
    flt = _bond_filter("t", secids=secids, boards=boards, primary=primary, cursor=cursor)
    return sql.SQL(
        "WITH e AS (SELECT t.* FROM {eod} t{join} WHERE {flt}), "
        "y AS (SELECT t.* FROM {yld} t{join} WHERE {flt}), "
        "k AS (SELECT trade_date, secid, board FROM e "
        "UNION SELECT trade_date, secid, board FROM y) "
    ).format(eod=eod, yld=yld, join=join, flt=flt)


@router.get("/bonds", summary="Bond EOD history, columnar (many bonds per call)")
def history_bonds(
    request: Request,
    date_from: FromQ,
    date_to: ToQ = None,
    secid: Annotated[
        str | None,
        Query(
            description=f"comma list of SECIDs (case-sensitive), at most {MAX_SECIDS}; "
            "default: every bond"
        ),
    ] = None,
    board: Annotated[
        str,
        Query(
            description="primary (default: the bond's main board, ref.bond marketprice_board, "
            "else primary_board, as the TDM export), all, or a comma list of boards"
        ),
    ] = "primary",
    fields: Annotated[
        str | None,
        Query(description="comma list of columns (trade_date, secid, board always included)"),
    ] = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """One row per (trade_date, secid, board) with a history or yields row; ordered by secid,
    trade_date, board. No forward fill: a day without a row on the board is absent."""
    known = _known(known_at)
    d1, d2 = _range(date_from, date_to)
    secids = split_list(secid)
    if secids is not None and len(secids) > MAX_SECIDS:
        raise bad_request(f"at most {MAX_SECIDS} secids per call")
    mode = board.strip()
    primary = mode.lower() == "primary"
    boards = None if primary or mode.lower() == "all" else split_list(mode, upper=True)
    if not primary and mode.lower() != "all" and not boards:
        raise bad_request("board must be primary, all or a comma list of boards")
    cols = _select_fields(BOND_COLUMNS, BOND_KEYS, fields)
    norm = {
        "from": d1,
        "to": d2,
        "secids": sorted(secids) if secids else None,
        "board": "primary" if primary else (sorted(boards) if boards else "all"),
        "fields": [c.name for c in cols],
        "known_at": known,
    }
    qhash = query_hash("bonds", norm)
    key = decode_cursor(cursor, qhash, 3)
    params: dict[str, Any] = {"d1": d1, "d2": d2, "secids": secids, "boards": boards, "ka": known}
    params.update(_cursor_params("c", key))
    flags = {"secids": secids is not None, "boards": boards is not None, "primary": primary}
    version_q = None
    if known is None:
        cte = _bond_cte(False, cursor=False, **flags)
        version_q = cte + sql.SQL(
            "SELECT (SELECT count(*) FROM e), (SELECT max(COALESCE(updated_at, ingested_at)) "
            "FROM e), (SELECT count(*) FROM y), "
            "(SELECT max(COALESCE(updated_at, ingested_at)) FROM y)"
        )
        if primary:
            # the main board comes from ref.bond: its changes invalidate the ETag but are not
            # a write of the selection's rows (text, so not part of data_version)
            version_q = version_q + (
                sql.SQL(
                    ", (SELECT max(updated_at)::text FROM ref.bond WHERE secid = ANY(%(secids)s))"
                )
                if secids is not None
                else sql.SQL(", (SELECT max(updated_at)::text FROM ref.bond)")
            )
    page_q = (
        _bond_cte(known is not None, cursor=key is not None, **flags)
        + sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(
            ", json_build_array(k.secid, k.trade_date, k.board)::text FROM k "
            "LEFT JOIN e ON e.trade_date = k.trade_date AND e.secid = k.secid "
            "AND e.board = k.board "
            "LEFT JOIN y ON y.trade_date = k.trade_date AND y.secid = k.secid "
            "AND y.board = k.board "
            "ORDER BY k.secid, k.trade_date, k.board LIMIT {}"
        ).format(sql.Literal(limit + 1))
    )
    with history_connection(request) as conn:
        version, hit = _version_or_304(
            request,
            conn,
            dataset="bonds",
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
            dataset="bonds",
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": d1,
                "to": d2,
                "board": norm["board"],
                "secids_requested": len(secids) if secids else None,
                "order": ["secid", "trade_date", "board"],
                "units": BOND_UNITS,
            },
            version=version,
            cursor_key=key,
        )


# ------------------------------------------------------------------------------ curves

CURVE_COLUMNS: Final = [
    Col("snapshot_date", "s.snapshot_date"),
    Col("source", "s.source"),
    Col("quote_set_id", "s.id"),
    Col("fetched_at", "s.fetched_at"),
    Col("source_ts", "s.source_ts"),
    Col("quote_basis", "s.quote_basis"),
    Col("market_closed", "s.market_closed"),
    Col("label", "s.label"),
    Col("content_sha256", "s.content_sha256"),
    Col(
        "labels",
        "to_json(array_remove(ARRAY["
        "CASE WHEN s.market_closed THEN 'market_closed' END, "
        "CASE WHEN s.quote_basis = 'mid_only' THEN 'mid_only' END, "
        "CASE WHEN (s.source_ts AT TIME ZONE 'Europe/Moscow')::date <> s.snapshot_date "
        "THEN 'source_clock_not_on_business_date' END]::text[], NULL))",
    ),
    Col("meta", "s.meta"),
    Col("hub_ingested_at", "s.ingested_at"),
    Col("tenor", "q.tenor"),
    Col("position", "q.position"),
    Col("tenor_months", "q.tenor_months"),
    Col("tenor_days", "q.tenor_days"),
    Col("pillar_date", "q.pillar_date"),
    Col("bid_pct", "q.bid_pct"),
    Col("mid_pct", "q.mid_pct"),
    Col("ask_pct", "q.ask_pct"),
    Col("extra", "q.extra"),
]
CURVE_KEYS: Final = ("snapshot_date", "source", "quote_set_id", "position", "tenor")


@router.get("/curves/{dataset}", summary="Curve history: end-of-day quote set per day and source")
def history_curves(
    request: Request,
    dataset: Annotated[str, Path(description="ruonia_ois, key_rate_swap, cny_ruonia_xccy, ...")],
    date_from: FromQ,
    date_to: ToQ = None,
    source: Annotated[
        str | None, Query(description="comma list (ratescalcs, otc_monitor, andromeda, ...)")
    ] = None,
    rule: Annotated[
        str,
        Query(
            description="eod (default): per (snapshot_date, source) the latest snapshot of that "
            "Moscow day - the eod_final rule; all: every stored snapshot"
        ),
    ] = "eod",
    fields: Annotated[str | None, Query(description="comma list of columns")] = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """One row per quote (tenor) of each selected snapshot; ordered by snapshot_date, source,
    quote_set_id, position. Sources are never merged (DESIGN §8 "Curve sources")."""
    known = _known(known_at)
    d1, d2 = _range(date_from, date_to)
    ds = dataset.lower()
    if not _DATASET_RE.match(ds):
        raise bad_request("dataset must match [a-z][a-z0-9_]*")
    if rule not in ("eod", "all"):
        raise bad_request("rule must be eod or all")
    sources = split_list(source)
    cols = _select_fields(CURVE_COLUMNS, CURVE_KEYS, fields)
    norm = {
        "dataset": ds,
        "from": d1,
        "to": d2,
        "sources": sorted(sources) if sources else None,
        "rule": rule,
        "fields": [c.name for c in cols],
        "known_at": known,
    }
    qhash = query_hash("curves", norm)
    key = decode_cursor(cursor, qhash, 5)
    params: dict[str, Any] = {"ds": ds, "d1": d1, "d2": d2, "sources": sources, "ka": known}
    params.update(_cursor_params("c", key))
    where: list[sql.Composable] = [
        sql.SQL("s.dataset = %(ds)s AND s.snapshot_date BETWEEN %(d1)s AND %(d2)s")
    ]
    if sources:
        where.append(sql.SQL("s.source = ANY(%(sources)s)"))
    if known is not None:
        where.append(sql.SQL("s.fetched_at <= %(ka)s AND s.ingested_at <= %(ka)s"))
    cond = sql.SQL(" AND ").join(where)
    distinct = sql.SQL("DISTINCT ON (s.snapshot_date, s.source) ") if rule == "eod" else sql.SQL("")
    order = (
        sql.SQL(" ORDER BY s.snapshot_date, s.source, s.fetched_at DESC, s.id DESC")
        if rule == "eod"
        else sql.SQL("")
    )
    snaps = sql.SQL("WITH s AS (SELECT {d}s.* FROM md.curve_snapshot s WHERE {c}{o}) ").format(
        d=distinct, c=cond, o=order
    )
    cursor_sql = (
        sql.SQL(
            " WHERE (s.snapshot_date, s.source, s.id, q.position, q.tenor) > "
            "(%(c0)s::date, %(c1)s, %(c2)s::bigint, %(c3)s::integer, %(c4)s)"
        )
        if key is not None
        else sql.SQL("")
    )
    page_q = (
        snaps
        + sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(
            ", json_build_array(s.snapshot_date, s.source, s.id, q.position, q.tenor)::text "
            "FROM s JOIN md.curve_quote q ON q.snapshot_id = s.id"
        )
        + cursor_sql
        + sql.SQL(" ORDER BY s.snapshot_date, s.source, s.id, q.position, q.tenor LIMIT {}").format(
            sql.Literal(limit + 1)
        )
    )
    version_q = None
    if known is None:
        vwhere = [sql.SQL("s.dataset = %(ds)s AND s.snapshot_date BETWEEN %(d1)s AND %(d2)s")]
        if sources:
            vwhere.append(sql.SQL("s.source = ANY(%(sources)s)"))
        version_q = sql.SQL(
            "SELECT count(*), max(s.ingested_at), "
            "md5(string_agg(s.id::text || ':' || s.content_sha256, ',' ORDER BY s.id)) "
            "FROM md.curve_snapshot s WHERE {}"
        ).format(sql.SQL(" AND ").join(vwhere))
    with history_connection(request) as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            exists = cur.execute(
                "SELECT 1 FROM md.curve_snapshot WHERE dataset = %s LIMIT 1", (ds,)
            ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"unknown curve dataset {ds!r}")
        version, hit = _version_or_304(
            request,
            conn,
            dataset=f"curves/{ds}",
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
            dataset=f"curves/{ds}",
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": d1,
                "to": d2,
                "rule": rule,
                "sources": sorted(sources) if sources else None,
                "order": ["snapshot_date", "source", "quote_set_id", "position"],
                "units": "bid_pct / mid_pct / ask_pct in % p.a. (mid-only quotes: bid/ask null); "
                "tenor codes as the source states them",
            },
            version=version,
            cursor_key=key,
        )


# -------------------------------------------------------------------------------- ZCYC

_ZCYC_PARAM_NAMES: Final = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))
ZCYC_COLUMNS: Final = [
    Col("trade_date", "z.trade_date"),
    Col("trade_time", "z.trade_time"),
    Col("source_ts", "z.source_ts"),
    Col("is_eod", "z.is_eod"),
    Col(
        "labels",
        "CASE WHEN z.is_eod THEN '[]'::json ELSE '[\"not_end_of_day_publication\"]'::json END",
    ),
    Col("B1", "z.params -> 'B1'"),
    Col("B2", "z.params -> 'B2'"),
    Col("B3", "z.params -> 'B3'"),
    Col("T1", "z.params -> 'T1'"),
    Col("G1", "z.params -> 'G1'"),
    Col("G2", "z.params -> 'G2'"),
    Col("G3", "z.params -> 'G3'"),
    Col("G4", "z.params -> 'G4'"),
    Col("G5", "z.params -> 'G5'"),
    Col("G6", "z.params -> 'G6'"),
    Col("G7", "z.params -> 'G7'"),
    Col("G8", "z.params -> 'G8'"),
    Col("G9", "z.params -> 'G9'"),
    Col("standard_yields", "z.standard_yields"),
    Col("check_max_error_bp", "z.check_max_error_bp"),
    Col("check_tolerance_bp", "z.check_tolerance_bp"),
    Col("hub_ingested_at", "COALESCE(z.updated_at, z.ingested_at)"),
]
ZCYC_KEYS: Final = ("trade_date", "trade_time")


@router.get("/zcyc", summary="MOEX KBD (ZCYC) history: parameters and standard yields per day")
def history_zcyc(
    request: Request,
    date_from: FromQ,
    date_to: ToQ = None,
    rule: Annotated[
        str,
        Query(
            description="eod (default): one publication per day - the end-of-day one, else the "
            "latest (label not_end_of_day_publication), as EOD snapshots; all: every publication"
        ),
    ] = "eod",
    fields: Annotated[str | None, Query(description="comma list of columns")] = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """Ordered by trade_date, trade_time. ``B1..B3, T1`` and ``G1..G9`` (G in bp) as stored;
    ``standard_yields`` = ``[{tenor_years, yield_pct}]`` effective annual %."""
    known = _known(known_at)
    d1, d2 = _range(date_from, date_to)
    if rule not in ("eod", "all"):
        raise bad_request("rule must be eod or all")
    cols = _select_fields(ZCYC_COLUMNS, ZCYC_KEYS, fields)
    norm = {"from": d1, "to": d2, "rule": rule, "fields": [c.name for c in cols], "known_at": known}
    qhash = query_hash("zcyc", norm)
    key = decode_cursor(cursor, qhash, 2)
    params: dict[str, Any] = {"d1": d1, "d2": d2, "ka": known}
    params.update(_cursor_params("c", key))
    src = (
        sql.SQL("md.zcyc_snapshot_known_at(%(ka)s, %(d1)s, %(d2)s) z")
        if known is not None
        else sql.SQL("md.zcyc_snapshot z WHERE z.trade_date BETWEEN %(d1)s AND %(d2)s")
    )
    base = (
        sql.SQL(
            "SELECT DISTINCT ON (z.trade_date) z.* FROM {} ORDER BY z.trade_date, "
            "z.is_eod DESC, z.trade_time DESC"
        ).format(src)
        if rule == "eod"
        else sql.SQL("SELECT z.* FROM {}").format(src)
    )
    cursor_sql = (
        sql.SQL(" WHERE (z.trade_date, z.trade_time) > (%(c0)s::date, %(c1)s::time)")
        if key is not None
        else sql.SQL("")
    )
    page_q = (
        sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(", json_build_array(z.trade_date, z.trade_time)::text FROM ({}) z").format(base)
        + cursor_sql
        + sql.SQL(" ORDER BY z.trade_date, z.trade_time LIMIT {}").format(sql.Literal(limit + 1))
    )
    version_q = (
        sql.SQL(
            "SELECT count(*), count(*) FILTER (WHERE is_eod), "
            "max(COALESCE(updated_at, ingested_at)) FROM md.zcyc_snapshot "
            "WHERE trade_date BETWEEN %(d1)s AND %(d2)s"
        )
        if known is None
        else None
    )
    with history_connection(request) as conn:
        version, hit = _version_or_304(
            request,
            conn,
            dataset="zcyc",
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
            dataset="zcyc",
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": d1,
                "to": d2,
                "rule": rule,
                "order": ["trade_date", "trade_time"],
                "units": "B1..B3, T1 and G1..G9 (G in bp) as MOEX publishes them (continuous "
                "compounding); standard_yields yield_pct effective annual %; trade_time MSK",
            },
            version=version,
            cursor_key=key,
        )


# ----------------------------------------------------------------------------- fixings

FIXING_COLUMNS: Final = [
    Col("index_code", "u.index_code"),
    Col("source", "u.source"),
    Col("fixing_date", "u.fixing_date"),
    Col("value", "u.value"),
    Col("unit", "u.unit"),
    Col("effective_date", "u.effective_date"),
    Col("published_on", "u.published_on"),
    Col("published_at", "u.published_at"),
    Col("hub_ingested_at", "u.hub_ingested_at"),
    Col("origin", "u.origin"),
    Col("labels", "u.labels"),
    Col("details", "u.details"),
]
FIXING_KEYS: Final = ("index_code", "source", "fixing_date")


@router.get("/fixings", summary="Fixing history (md.fixing and the RUSFAR family)")
def history_fixings(
    request: Request,
    date_from: FromQ,
    date_to: ToQ = None,
    code: Annotated[
        str | None,
        Query(
            description="comma list of index codes (KEY_RATE, RUONIA, RUONIA_INDEX, "
            "TERM_RUONIA_1M, CPI_RU_MOM, RUSFAR, RUSFARIND, ...); default: all"
        ),
    ] = None,
    source: Annotated[str | None, Query(description="comma list (cbr, rosstat, moex_iss)")] = None,
    fields: Annotated[str | None, Query(description="comma list of columns")] = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """Ordered by index_code, source, fixing_date. ``fixing_date`` semantics as ``/v1/fixings``
    (KEY_RATE: day in force; RUONIA: observation date, ``published_on`` next business day).
    RUSFAR rates (explicit list) have ``unit = pct``, RUSFARIND ``unit = index``; other RUSFAR*
    indices are not served (unit unknown, ``meta.not_served``). ``hub_ingested_at`` = when the
    hub first stored the value (not a publication time)."""
    known = _known(known_at)
    d1, d2 = _range(date_from, date_to)
    codes = split_list(code, upper=True)
    if codes is not None and len(codes) > MAX_CODES:
        raise bad_request(f"at most {MAX_CODES} codes per call")
    sources = split_list(source)
    cols = _select_fields(FIXING_COLUMNS, FIXING_KEYS, fields)
    norm = {
        "from": d1,
        "to": d2,
        "codes": sorted(codes) if codes else None,
        "sources": sorted(sources) if sources else None,
        "fields": [c.name for c in cols],
        "known_at": known,
    }
    qhash = query_hash("fixings", norm)
    key = decode_cursor(cursor, qhash, 3)
    rusfar = [*RUSFAR_RATE_SERIES, *RUSFAR_INDEX_SERIES]
    want_rusfar = (codes is None or any(c in rusfar for c in codes)) and (
        sources is None or "moex_iss" in sources
    )
    params: dict[str, Any] = {
        "d1": d1,
        "d2": d2,
        "codes": codes,
        "sources": sources,
        "ka": known,
        "rusfar": rusfar if codes is None else [c for c in codes if c in rusfar],
        "rates": list(RUSFAR_RATE_SERIES),
    }
    params.update(_cursor_params("c", key))
    fsrc = (
        sql.SQL("md.fixing_known_at(%(ka)s, %(d1)s, %(d2)s) f WHERE true")
        if known is not None
        else sql.SQL("md.fixing f WHERE f.fixing_date BETWEEN %(d1)s AND %(d2)s")
    )
    isrc = (
        sql.SQL("md.index_eod_known_at(%(ka)s, %(d1)s, %(d2)s) i WHERE true")
        if known is not None
        else sql.SQL("md.index_eod i WHERE i.trade_date BETWEEN %(d1)s AND %(d2)s")
    )
    fextra: sql.Composable = sql.SQL("")
    if codes:
        fextra = fextra + sql.SQL(" AND f.index_code = ANY(%(codes)s)")
    if sources:
        fextra = fextra + sql.SQL(" AND f.source = ANY(%(sources)s)")
    parts: list[sql.Composable] = [
        sql.SQL(
            "SELECT f.index_code, f.source, f.fixing_date, f.value, f.unit, f.effective_date, "
            "f.published_on, f.published_at, f.ingested_at AS hub_ingested_at, "
            "'md.fixing'::text AS origin, '[]'::json AS labels, f.details FROM {}{}"
        ).format(fsrc, fextra)
    ]
    if want_rusfar:
        parts.append(
            sql.SQL(
                "SELECT i.secid, 'moex_iss'::text, i.trade_date, i.close, "
                "CASE WHEN i.secid = ANY(%(rates)s) THEN 'pct' ELSE 'index' END, "
                "NULL::date, NULL::date, NULL::timestamptz, COALESCE(i.updated_at, i.ingested_at), "
                "'md.index_eod'::text, CASE WHEN i.basis = 'marketdata' "
                "THEN '[\"moex_marketdata_preliminary\"]'::json ELSE '[]'::json END, NULL::jsonb "
                "FROM {} AND i.secid = ANY(%(rusfar)s) AND i.close IS NOT NULL"
            ).format(isrc)
        )
    union = sql.SQL(" UNION ALL ").join(parts)
    cursor_sql = (
        sql.SQL(" WHERE (u.index_code, u.source, u.fixing_date) > (%(c0)s, %(c1)s, %(c2)s::date)")
        if key is not None
        else sql.SQL("")
    )
    page_q = (
        sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(
            ", json_build_array(u.index_code, u.source, u.fixing_date)::text FROM ({}) u"
        ).format(union)
        + cursor_sql
        + sql.SQL(" ORDER BY u.index_code, u.source, u.fixing_date LIMIT {}").format(
            sql.Literal(limit + 1)
        )
    )
    version_q = None
    if known is None:
        vparts = [
            sql.SQL("(SELECT count(*) FROM {}{})").format(fsrc, fextra),
            sql.SQL("(SELECT max(f.ingested_at) FROM {}{})").format(fsrc, fextra),
        ]
        if want_rusfar:
            irest = sql.SQL(" AND i.secid = ANY(%(rusfar)s)")
            vparts += [
                sql.SQL("(SELECT count(*) FROM {}{})").format(isrc, irest),
                sql.SQL("(SELECT max(COALESCE(i.updated_at, i.ingested_at)) FROM {}{})").format(
                    isrc, irest
                ),
            ]
        version_q = sql.SQL("SELECT ") + sql.SQL(", ").join(vparts)
    not_served = [
        {"code": c, "reason": "rusfar_series_unit_unknown"}
        for c in codes or []
        if c.startswith("RUSFAR") and rusfar_unit(c) is None
    ]
    with history_connection(request) as conn:
        version, hit = _version_or_304(
            request,
            conn,
            dataset="fixings",
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
            dataset="fixings",
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": d1,
                "to": d2,
                "not_served": not_served,
                "order": ["index_code", "source", "fixing_date"],
                "units": "value in % p.a. (unit pct) or index level (unit index; CPI_RU_MOM = "
                "CPI of the month to the previous month in %, fixing_date = first day of month)",
            },
            version=version,
            cursor_key=key,
        )


# ---------------------------------------------------------------------------------- FX

FX_KINDS: Final = ("cbr_official", "moex_eod", "moex_indicative_14", "moex_indicative_19")
FX_COLUMNS: Final = [
    Col("kind", "u.kind"),
    Col("pair", "u.pair"),
    Col("base_ccy", "u.base_ccy"),
    Col("quote_ccy", "u.quote_ccy"),
    Col("date", "u.d"),
    Col("rate", "u.rate"),
    Col("secid", "u.secid"),
    Col("board", "u.board"),
    Col("tenor", "u.tenor"),
    Col("close", "u.close"),
    Col("waprice", "u.waprice"),
    Col("quote_units", "u.quote_units"),
    Col("close_per_unit", "u.close_per_unit"),
    Col("waprice_per_unit", "u.waprice_per_unit"),
    Col("num_trades", "u.num_trades"),
    Col("nominal", "u.nominal"),
    Col("set_date", "u.set_date"),
    Col("trade_time", "u.trade_time"),
    Col("hub_ingested_at", "u.hub_ingested_at"),
]
FX_KEYS: Final = ("kind", "pair", "date", "secid", "board")
_PAIR_RE: Final = re.compile(r"^[A-Z]{3}(/[A-Z]{3})?$")

# exact per-unit value: price / quote units when the units are a power of ten (as the snapshot)
_CLOSE_PER_UNIT: Final = sql.SQL(
    "CASE WHEN trim_scale(i.face_value)::text ~ '^10*$' THEN round(e.close / i.face_value, "
    "scale(e.close) + length(trim_scale(i.face_value)::text) - 1) END"
)
_WAPRICE_PER_UNIT: Final = sql.SQL(
    "CASE WHEN trim_scale(i.face_value)::text ~ '^10*$' THEN round(e.waprice / i.face_value, "
    "scale(e.waprice) + length(trim_scale(i.face_value)::text) - 1) END"
)


@router.get("/fx", summary="FX history: CBR official, MOEX CETS EOD, MOEX indicative")
def history_fx(
    request: Request,
    date_from: FromQ,
    date_to: ToQ = None,
    pair: Annotated[
        str | None,
        Query(
            description=f"comma list of BASE/QUOTE pairs (USD/RUB, CNY/RUB, EUR/USD) or bare "
            f"base currencies (USD = USD/RUB), at most {MAX_PAIRS}; default: all"
        ),
    ] = None,
    kind: Annotated[
        str | None, Query(description=f"comma list of {', '.join(FX_KINDS)}; default: all")
    ] = None,
    tenor: Annotated[
        str | None, Query(description="moex_eod only: comma list of tenors (TOD, TOM)")
    ] = None,
    fields: Annotated[str | None, Query(description="comma list of columns")] = None,
    limit: LimitQ = DEFAULT_LIMIT,
    cursor: CursorQ = None,
    known_at: KnownAtQ = None,
) -> Response:
    """One row per (kind, pair, date, secid, board); ordered by those. ``rate`` = quote currency
    per 1 unit of base: ``cbr_official`` = CBR rate per unit by **effective date** (every
    calendar day in force), ``moex_indicative_14/19`` = MOEX indicative rate (14:00 tc / 19:00
    mc). ``moex_eod`` = CETS outrights (swaps excluded): raw ``close`` / ``waprice`` per
    ``quote_units`` and the exact ``*_per_unit`` (``null`` when the units are not a power of
    ten); ``rate`` is null."""
    known = _known(known_at)
    d1, d2 = _range(date_from, date_to)
    pairs_raw = split_list(pair, upper=True)
    if pairs_raw is not None:
        if len(pairs_raw) > MAX_PAIRS:
            raise bad_request(f"at most {MAX_PAIRS} pairs per call")
        bad = [p for p in pairs_raw if not _PAIR_RE.match(p)]
        if bad:
            raise bad_request(f"bad pair(s): {', '.join(bad)} (use BASE/QUOTE, e.g. CNY/RUB)")
    pairs = sorted({p if "/" in p else f"{p}/RUB" for p in pairs_raw}) if pairs_raw else None
    kinds = split_list(kind)
    if kinds is not None:
        unknown = sorted(set(kinds) - set(FX_KINDS))
        if unknown:
            raise bad_request(f"unknown kind(s): {', '.join(unknown)}")
    kinds_set = set(kinds or FX_KINDS)
    tenors = split_list(tenor, upper=True)
    cols = _select_fields(FX_COLUMNS, FX_KEYS, fields)
    norm = {
        "from": d1,
        "to": d2,
        "pairs": pairs,
        "kinds": sorted(kinds_set),
        "tenors": sorted(tenors) if tenors else None,
        "fields": [c.name for c in cols],
        "known_at": known,
    }
    qhash = query_hash("fx", norm)
    key = decode_cursor(cursor, qhash, 5)
    params: dict[str, Any] = {"d1": d1, "d2": d2, "pairs": pairs, "tenors": tenors, "ka": known}
    params.update(_cursor_params("c", key))
    k = known is not None
    off = (
        sql.SQL("md.fx_official_known_at(%(ka)s, %(d1)s, %(d2)s) o WHERE true")
        if k
        else sql.SQL("md.fx_official o WHERE o.effective_date BETWEEN %(d1)s AND %(d2)s")
    )
    eod = sql.SQL("md.fx_eod_known_at(%(ka)s, %(d1)s, %(d2)s) e") if k else sql.SQL("md.fx_eod e")
    inst = sql.SQL("ref.fx_instrument_known_at(%(ka)s) i") if k else sql.SQL("ref.fx_instrument i")
    ind = (
        sql.SQL("md.fx_indicative_known_at(%(ka)s, %(d1)s, %(d2)s) n WHERE true")
        if k
        else sql.SQL("md.fx_indicative n WHERE n.trade_date BETWEEN %(d1)s AND %(d2)s")
    )
    pf_off = sql.SQL(" AND (o.ccy || '/RUB') = ANY(%(pairs)s)") if pairs else sql.SQL("")
    pf_eod = (
        sql.SQL(" AND (i.base_ccy || '/' || i.quote_ccy) = ANY(%(pairs)s)")
        if pairs
        else sql.SQL("")
    )
    pf_ind = sql.SQL(" AND n.ccy_pair = ANY(%(pairs)s)") if pairs else sql.SQL("")
    tf = sql.SQL(" AND i.tenor = ANY(%(tenors)s)") if tenors else sql.SQL("")
    eod_where = sql.SQL(" WHERE i.kind = 'outright'{d}{p}{t}").format(
        d=sql.SQL("") if k else sql.SQL(" AND e.trade_date BETWEEN %(d1)s AND %(d2)s"),
        p=pf_eod,
        t=tf,
    )
    parts: list[sql.Composable] = []
    if "cbr_official" in kinds_set:
        parts.append(
            sql.SQL(
                "SELECT 'cbr_official'::text AS kind, o.ccy || '/RUB' AS pair, o.ccy AS base_ccy, "
                "'RUB'::text AS quote_ccy, o.effective_date AS d, o.rate_per_unit AS rate, "
                "NULL::text AS secid, NULL::text AS board, NULL::text AS tenor, "
                "NULL::numeric AS close, NULL::numeric AS waprice, NULL::numeric AS quote_units, "
                "NULL::numeric AS close_per_unit, NULL::numeric AS waprice_per_unit, "
                "NULL::integer AS num_trades, o.nominal, o.set_date, NULL::text AS trade_time, "
                "o.ingested_at AS hub_ingested_at FROM {}{}"
            ).format(off, pf_off)
        )
    if "moex_eod" in kinds_set:
        parts.append(
            sql.SQL(
                "SELECT 'moex_eod'::text AS kind, i.base_ccy || '/' || i.quote_ccy AS pair, "
                "i.base_ccy, i.quote_ccy, e.trade_date AS d, NULL::numeric AS rate, e.secid, "
                "e.board, i.tenor, e.close, e.waprice, i.face_value AS quote_units, "
                "{cu} AS close_per_unit, {wu} AS waprice_per_unit, e.num_trades, "
                "NULL::numeric AS nominal, NULL::date AS set_date, NULL::text AS trade_time, "
                "e.ingested_at AS hub_ingested_at "
                "FROM {eod} JOIN {inst} ON i.secid = e.secid AND i.board = e.board{w}"
            ).format(cu=_CLOSE_PER_UNIT, wu=_WAPRICE_PER_UNIT, eod=eod, inst=inst, w=eod_where)
        )
    for kname, clearing in (("moex_indicative_14", "tc"), ("moex_indicative_19", "mc")):
        if kname in kinds_set:
            parts.append(
                sql.SQL(
                    "SELECT {kn}::text AS kind, n.ccy_pair AS pair, n.base_ccy, n.quote_ccy, "
                    "n.trade_date AS d, n.rate, NULL::text AS secid, NULL::text AS board, "
                    "NULL::text AS tenor, NULL::numeric AS close, NULL::numeric AS waprice, "
                    "NULL::numeric AS quote_units, NULL::numeric AS close_per_unit, "
                    "NULL::numeric AS waprice_per_unit, NULL::integer AS num_trades, "
                    "NULL::numeric AS nominal, NULL::date AS set_date, "
                    "to_char(n.trade_time, 'HH24:MI') AS trade_time, "
                    "n.ingested_at AS hub_ingested_at "
                    "FROM {ind} AND n.clearing = {cl}{p}"
                ).format(kn=sql.Literal(kname), ind=ind, cl=sql.Literal(clearing), p=pf_ind)
            )
    union = sql.SQL(" UNION ALL ").join(parts)
    cursor_sql = (
        sql.SQL(
            " WHERE (u.kind, u.pair, u.d, COALESCE(u.secid, ''), COALESCE(u.board, '')) > "
            "(%(c0)s, %(c1)s, %(c2)s::date, %(c3)s, %(c4)s)"
        )
        if key is not None
        else sql.SQL("")
    )
    page_q = (
        sql.SQL("SELECT ")
        + _json_array(cols)
        + sql.SQL(
            ", json_build_array(u.kind, u.pair, u.d, COALESCE(u.secid, ''), "
            "COALESCE(u.board, ''))::text FROM ({}) u"
        ).format(union)
        + cursor_sql
        + sql.SQL(
            " ORDER BY u.kind, u.pair, u.d, COALESCE(u.secid, ''), COALESCE(u.board, '') LIMIT {}"
        ).format(sql.Literal(limit + 1))
    )
    version_q = None
    if not k:
        vparts: list[sql.Composable] = []
        if "cbr_official" in kinds_set:
            vparts.append(
                sql.SQL(
                    "(SELECT count(*) FROM {}{}), (SELECT max(o.ingested_at) FROM {}{})"
                ).format(off, pf_off, off, pf_off)
            )
        if "moex_eod" in kinds_set:
            join = sql.SQL("{} JOIN {} ON i.secid = e.secid AND i.board = e.board{}").format(
                eod, inst, eod_where
            )
            vparts.append(
                sql.SQL("(SELECT count(*) FROM {j}), (SELECT max(e.ingested_at) FROM {j})").format(
                    j=join
                )
            )
        if kinds_set & {"moex_indicative_14", "moex_indicative_19"}:
            vparts.append(
                sql.SQL(
                    "(SELECT count(*) FROM {}{}), (SELECT max(n.ingested_at) FROM {}{})"
                ).format(ind, pf_ind, ind, pf_ind)
            )
        version_q = sql.SQL("SELECT ") + sql.SQL(", ").join(vparts)
    with history_connection(request) as conn:
        version, hit = _version_or_304(
            request,
            conn,
            dataset="fx",
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
            dataset="fx",
            qhash=qhash,
            cols=cols,
            page_query=page_q,
            params=params,
            limit=limit,
            known_at=known,
            meta={
                "from": d1,
                "to": d2,
                "kinds": sorted(kinds_set),
                "pairs": pairs,
                "order": ["kind", "pair", "date", "secid", "board"],
                "units": "rate / *_per_unit: quote currency per 1 unit of base; close / waprice "
                "per quote_units units of base (MOEX raw); trade_time MSK",
            },
            version=version,
            cursor_key=key,
        )


# ------------------------------------------------------------------------------- index

INDEX_COLUMNS: Final = (
    "dataset",
    "series",
    "source",
    "unit",
    "date_from",
    "date_to",
    "rows",
    "days",
    "series_count",
    "endpoint",
)
_INDEX_SQL: Final = """
SELECT json_build_array('bonds', NULL, 'moex_iss', NULL, min(trade_date), max(trade_date),
       count(*), count(DISTINCT trade_date), count(DISTINCT secid), '/v1/history/bonds')::text
FROM md.bond_eod
UNION ALL
SELECT json_build_array('bond_yields', NULL, 'moex_iss', NULL, min(trade_date), max(trade_date),
       count(*), count(DISTINCT trade_date), count(DISTINCT secid), '/v1/history/bonds')::text
FROM md.bond_eod_yield
UNION ALL
SELECT j FROM (
  SELECT json_build_array('curves', dataset, source, 'pct', min(snapshot_date),
         max(snapshot_date), count(*), count(DISTINCT snapshot_date), NULL,
         '/v1/history/curves/' || dataset)::text AS j, dataset, source
  FROM md.curve_snapshot GROUP BY dataset, source ORDER BY dataset, source) c
UNION ALL
SELECT json_build_array('zcyc', NULL, 'moex_iss', NULL, min(trade_date), max(trade_date),
       count(*), count(DISTINCT trade_date), NULL, '/v1/history/zcyc')::text
FROM md.zcyc_snapshot
UNION ALL
SELECT j FROM (
  SELECT json_build_array('fixings', index_code, source, min(unit), min(fixing_date),
         max(fixing_date), count(*), count(DISTINCT fixing_date), NULL,
         '/v1/history/fixings?code=' || index_code)::text AS j, index_code, source
  FROM md.fixing GROUP BY index_code, source ORDER BY index_code, source) f
UNION ALL
SELECT j FROM (
  SELECT json_build_array('fixings', secid, 'moex_iss',
         CASE WHEN secid = ANY(%(rates)s) THEN 'pct' ELSE 'index' END, min(trade_date),
         max(trade_date), count(*), count(DISTINCT trade_date), NULL,
         '/v1/history/fixings?code=' || secid)::text AS j, secid
  FROM md.index_eod WHERE secid = ANY(%(rusfar)s) AND close IS NOT NULL
  GROUP BY secid ORDER BY secid) r
UNION ALL
SELECT j FROM (
  SELECT json_build_array('fx', ccy || '/RUB', 'cbr', 'cbr_official', min(effective_date),
         max(effective_date), count(*), count(DISTINCT effective_date), NULL,
         '/v1/history/fx?kind=cbr_official&pair=' || ccy || '/RUB')::text AS j, ccy
  FROM md.fx_official GROUP BY ccy ORDER BY ccy) o
UNION ALL
SELECT j FROM (
  SELECT json_build_array('fx', i.base_ccy || '/' || i.quote_ccy, 'moex_iss',
         'moex_eod:' || e.secid || ':' || e.board, min(e.trade_date), max(e.trade_date),
         count(*), count(DISTINCT e.trade_date), NULL,
         '/v1/history/fx?kind=moex_eod&pair=' || i.base_ccy || '/' || i.quote_ccy)::text AS j,
         e.secid, e.board
  FROM md.fx_eod e JOIN ref.fx_instrument i ON i.secid = e.secid AND i.board = e.board
  WHERE i.kind = 'outright' GROUP BY i.base_ccy, i.quote_ccy, e.secid, e.board
  ORDER BY e.secid, e.board) m
UNION ALL
SELECT j FROM (
  SELECT json_build_array('fx', ccy_pair, 'moex_iss',
         CASE clearing WHEN 'tc' THEN 'moex_indicative_14' ELSE 'moex_indicative_19' END,
         min(trade_date), max(trade_date), count(*), count(DISTINCT trade_date), NULL,
         '/v1/history/fx?kind=' ||
         CASE clearing WHEN 'tc' THEN 'moex_indicative_14' ELSE 'moex_indicative_19' END ||
         '&pair=' || ccy_pair)::text AS j, ccy_pair, clearing
  FROM md.fx_indicative GROUP BY ccy_pair, clearing ORDER BY ccy_pair, clearing) n
"""


@router.get("/index", summary="History datasets and series: depth and row counts")
def history_index(request: Request) -> Response:
    """One row per dataset / series: first and last date, rows, distinct days, number of bonds
    (bonds), the endpoint to read it. ``unit`` of fx rows names the kind (``cbr_official``,
    ``moex_eod:<secid>:<board>``, ``moex_indicative_14/19``). Cached for 5 minutes."""
    state = request.app.state
    cached: tuple[float, Columnar] | None = getattr(state, "history_index_cache", None)
    now = time.monotonic()
    if cached is not None and now - cached[0] < INDEX_CACHE_TTL_S:
        return respond(request, cached[1])
    params = {
        "rates": list(RUSFAR_RATE_SERIES),
        "rusfar": [*RUSFAR_RATE_SERIES, *RUSFAR_INDEX_SERIES],
    }
    with history_connection(request) as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            rows = [str(r[0]) for r in cur.execute(_INDEX_SQL, params).fetchall()]
        as_of = read_as_of(conn)
    page = Columnar(
        dataset="index",
        columns=list(INDEX_COLUMNS),
        rows=rows,
        as_of=as_of,
        known_at=None,
        next=None,
        meta={
            "returned": len(rows),
            "cache_ttl_s": int(INDEX_CACHE_TTL_S),
            "limits": {
                "max_limit": MAX_LIMIT,
                "default_limit": DEFAULT_LIMIT,
                "max_secids": MAX_SECIDS,
                "max_codes": MAX_CODES,
                "max_pairs": MAX_PAIRS,
            },
        },
    )
    state.history_index_cache = (now, page)
    return respond(request, page)
