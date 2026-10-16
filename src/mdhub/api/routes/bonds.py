"""Bond static (``ref.bond*``) and bond EOD market data (``md.v_bond_eod``)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from mdhub.db.pool import DictConnection

router = APIRouter(tags=["bonds"])

_UNITS = (
    "*_pct: prices in % of face as MOEX publishes them, yields in % p.a.; *_per_bond: per one "
    "bond (accrued in currency_id, coupon in face_unit); value_ccy: turnover in currency_id "
    "(SUR = RUB); *_bp: basis points; *_days: days. y_*: MOEX yields endpoint (effective yield "
    "to y_yield_date of y_yield_date_type)."
)


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/bonds", summary="Bond static list (ref.bond) with filters")
def bonds(
    request: Request,
    page: Annotated[Page, Depends(page_params)],
    board: Annotated[str | None, Query(description="listed on board(s), comma list")] = None,
    bond_class: Annotated[str | None, Query(description="ofz_pd, corporate, ... (comma)")] = None,
    status: Annotated[str | None, Query(description="active|matured|delisted (comma)")] = None,
    coupon_type: Annotated[str | None, Query(description="fixed|floating|... (comma)")] = None,
    q: Annotated[
        str | None, Query(max_length=100, description="substring of secid/isin/name")
    ] = None,
) -> Response:
    """Ordered by secid. Every ``ref.bond`` column except provenance ids."""
    where = Where()
    boards = split_list(board, upper=True)
    if boards:
        where.add(
            "EXISTS (SELECT 1 FROM ref.bond_board bb WHERE bb.secid = b.secid "
            "AND bb.board = ANY(%(boards)s) AND bb.listed)",
            boards=boards,
        )
    classes = split_list(bond_class)
    if classes:
        where.add("b.bond_class = ANY(%(classes)s)", classes=classes)
    statuses = split_list(status)
    if statuses:
        where.add("b.status = ANY(%(statuses)s)", statuses=statuses)
    ctypes = split_list(coupon_type)
    if ctypes:
        where.add("b.coupon_type = ANY(%(ctypes)s)", ctypes=ctypes)
    if q and q.strip():
        where.add(
            "(b.secid ILIKE %(q)s OR b.isin ILIKE %(q)s OR b.short_name ILIKE %(q)s "
            "OR b.name ILIKE %(q)s OR b.regnumber ILIKE %(q)s)",
            q=_like(q.strip()),
        )
    query = (
        sql.SQL("SELECT b.* FROM ref.bond b")
        + where.sql()
        + sql.SQL(" ORDER BY b.secid")
        + page.sql()
    )
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, page.wrap(rows))


@router.get("/bonds/eod", summary="Bonds EOD for one trade date (all boards, joined yields)")
def bonds_eod(
    request: Request,
    page: Annotated[Page, Depends(page_params)],
    day: Annotated[date, Query(alias="date", description="trade date (MSK)")],
    secid: Annotated[str | None, Query(description="comma list")] = None,
    board: Annotated[str | None, Query(description="comma list")] = None,
    known_at: Annotated[
        datetime | None,
        Query(description="reconstruct values as known at this time (ISO 8601 with offset)"),
    ] = None,
) -> Response:
    known = require_aware(known_at, "known_at")
    where = Where()
    secids = split_list(secid)
    if secids:
        where.add("secid = ANY(%(secids)s)", secids=secids)
    boards = split_list(board, upper=True)
    if boards:
        where.add("board = ANY(%(boards)s)", boards=boards)
    if known is None:
        source = sql.SQL("SELECT * FROM md.v_bond_eod")
        where.add("trade_date = %(day)s", day=day)
    else:
        source = sql.SQL("SELECT * FROM md.v_bond_eod_known_at(%(known)s, %(day)s, %(day)s)")
        where.params.update(known=known, day=day)
    query = source + where.sql() + sql.SQL(" ORDER BY secid, board") + page.sql()
    with read_connection(request) as conn:
        rows = fetch_all(conn, query, where.params)
    return json_response(request, page.wrap(rows, date=day, known_at=known, units=_UNITS))


def _one(conn: DictConnection, query: sql.SQL, params: dict[str, Any]) -> dict[str, Any] | None:
    rows = fetch_all(conn, query, params)
    return rows[0] if rows else None


def _resolve_secid(conn: DictConnection, secid: str) -> str | None:
    """The stored SECID for a path parameter: exact match (SECIDs are case-sensitive - FORTS
    and index codes are mixed case), else a case-insensitive match only when it is unique."""
    exact = _one(conn, sql.SQL("SELECT secid FROM ref.bond WHERE secid = %(s)s"), {"s": secid})
    if exact is not None:
        return str(exact["secid"])
    rows = fetch_all(
        conn, sql.SQL("SELECT secid FROM ref.bond WHERE upper(secid) = upper(%(s)s)"), {"s": secid}
    )
    return str(rows[0]["secid"]) if len(rows) == 1 else None


@router.get("/bonds/{secid}", summary="One bond: static, issuer, boards, schedules, formula")
def bond(
    request: Request,
    secid: str,
    include_deleted: Annotated[
        bool, Query(description="include schedule rows flagged is_deleted")
    ] = False,
) -> Response:
    """Coupons (``amount`` per bond in face_unit, ``value_prc`` % p.a.), amortizations
    (``value_prc`` % of initial face), offers, floater formula (resolved + per source),
    boards and ratings."""
    with read_connection(request) as conn:
        resolved_secid = _resolve_secid(conn, secid)
        if resolved_secid is None:
            raise HTTPException(status_code=404, detail="bond not found")
        p = {"secid": resolved_secid, "del": include_deleted}
        row = _one(conn, sql.SQL("SELECT * FROM ref.bond WHERE secid = %(secid)s"), p)
        if row is None:
            raise HTTPException(status_code=404, detail="bond not found")
        issuer = None
        if row.get("issuer_id") is not None:
            issuer = _one(
                conn,
                sql.SQL("SELECT * FROM ref.issuer WHERE issuer_id = %(iid)s"),
                {"iid": row["issuer_id"]},
            )
        boards = fetch_all(
            conn,
            sql.SQL(
                "SELECT * FROM ref.bond_board WHERE secid = %(secid)s "
                "ORDER BY is_primary DESC, board"
            ),
            p,
        )
        coupons = fetch_all(
            conn,
            sql.SQL(
                "SELECT * FROM ref.bond_coupon WHERE secid = %(secid)s "
                "AND (%(del)s OR NOT is_deleted) ORDER BY coupon_no"
            ),
            p,
        )
        amortizations = fetch_all(
            conn,
            sql.SQL(
                "SELECT * FROM ref.bond_amortization WHERE secid = %(secid)s "
                "AND (%(del)s OR NOT is_deleted) ORDER BY amort_date, data_source"
            ),
            p,
        )
        offers = fetch_all(
            conn,
            sql.SQL(
                "SELECT * FROM ref.bond_offer WHERE secid = %(secid)s "
                "AND (%(del)s OR NOT is_deleted) ORDER BY offer_date NULLS LAST, source, offer_key"
            ),
            p,
        )
        resolved = _one(
            conn,
            sql.SQL("SELECT * FROM ref.bond_rate_formula_resolved WHERE secid = %(secid)s"),
            p,
        )
        formula_sources = fetch_all(
            conn,
            sql.SQL(
                "SELECT * FROM ref.bond_rate_formula WHERE secid = %(secid)s "
                "AND (%(del)s OR NOT is_deleted) ORDER BY source"
            ),
            p,
        )
        ratings = fetch_all(
            conn,
            sql.SQL("SELECT * FROM ref.bond_rating WHERE secid = %(secid)s ORDER BY agency, scale"),
            p,
        )
    payload = {
        "data": {
            **row,
            "issuer": issuer,
            "boards": boards,
            "coupons": coupons,
            "amortizations": amortizations,
            "offers": offers,
            "formula": {"resolved": resolved, "sources": formula_sources},
            "ratings": ratings,
        }
    }
    return json_response(request, payload)


@router.get("/bonds/{secid}/history", summary="EOD history of one bond (all or chosen boards)")
def bond_history(
    request: Request,
    secid: str,
    page: Annotated[Page, Depends(page_params)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    board: Annotated[str | None, Query(description="comma list")] = None,
) -> Response:
    """Default range: last 366 days. Same fields as ``/v1/bonds/eod``. The SECID is matched
    exactly (case-insensitively only when that is unique in ``ref.bond``)."""
    d1, d2 = date_range(None, date_from, date_to, default_days=366)
    with read_connection(request) as conn:
        secid = _resolve_secid(conn, secid) or secid
        where = Where().add(
            "secid = %(secid)s AND trade_date BETWEEN %(d1)s AND %(d2)s",
            secid=secid,
            d1=d1,
            d2=d2,
        )
        boards = split_list(board, upper=True)
        if boards:
            where.add("board = ANY(%(boards)s)", boards=boards)
        query = (
            sql.SQL("SELECT * FROM md.v_bond_eod")
            + where.sql()
            + sql.SQL(" ORDER BY trade_date, board")
            + page.sql()
        )
        rows = fetch_all(conn, query, where.params)
    return json_response(
        request, page.wrap(rows, secid=secid, date_from=d1, date_to=d2, units=_UNITS)
    )
