"""Optional Cbonds enrichment of bond static (job ``cbonds_enrich``, daily 07:30).

1. Credentials missing -> run ``skipped``.
2. One tiny probe query (``get_emissions``, 1 ISIN, 2 fields). HTTP 401 -> run ``skipped``
   with the reason (credentials rejected); nothing else
   is requested, so a rejected account is never hammered.
3. For active MOEX bonds with an ISIN (floaters first, ``max_isins``), in batches of 50 ISINs
   (``in`` filter): ``get_emissions``, ``get_offert``, ``get_rating_emission_maxdate``; for
   floaters (``flow_limit``) ``get_flow_new`` per ISIN.
4. Writes with ``source = 'cbonds'`` and field-level precedence:

   * conventions - Cbonds wins: ``ref.bond.day_count`` (+ ``day_count_source``), a
     ``ref.bond_rate_formula`` row (benchmark, spread, floor/cap, variable rate type, fixing
     day; lag/averaging stay NULL - not stated by these fields);
   * exchange schedule data - MOEX wins: Cbonds offers are stored as separate rows
     (``ref.bond_offer`` source ``cbonds``), coupon dates/maturity are only compared;
   * ratings -> ``ref.bond_rating``;
   * every disagreement -> ``ref.static_conflict`` (``resolved = true`` once it disappears).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from typing import Any, Final

import psycopg
from psycopg.rows import tuple_row

from mdhub.core.capture import record_capture
from mdhub.core.errors import FormatDriftError
from mdhub.core.http import HttpClient, HttpError
from mdhub.core.upsert import UpsertResult, touch_last_seen, upsert_rows
from mdhub.jobs import JobContext, job
from mdhub.sources import cbonds as cb
from mdhub.sources.moex_static import canonical_sha256, map_benchmark

__all__ = ["cbonds_enrich"]

SOURCE: Final = "cbonds"


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


@job(
    "cbonds_enrich", "Cbonds enrichment of bond static (day count, floater terms, offers, ratings)"
)
def cbonds_enrich(
    ctx: JobContext,
    *,
    max_isins: int = 5000,
    batch_size: int = cb.MAX_BATCH,
    flow_limit: int = 200,
) -> None:
    login = ctx.settings.cbonds_login
    password = ctx.settings.cbonds_password
    if login is None or password is None:
        ctx.run.mark_skipped("CBONDS_LOGIN / CBONDS_PASSWORD not configured")
        return
    batch_size = max(1, min(batch_size, cb.MAX_BATCH))
    creds = cb.CbondsCredentials(login.get_secret_value(), password.get_secret_value())
    with ctx.http(base_url=cb.CBONDS_BASE_URL) as http:
        try:
            probe = cb.query(
                http,
                "get_emissions",
                filters=[{"field": "isin_code", "operator": "eq", "value": cb.PROBE_ISIN}],
                fields=["id", "isin_code"],
                limit=1,
                creds=creds,
            )
        except cb.CbondsAuthError as exc:
            ctx.run.mark_skipped(f"{exc}; Cbonds enrichment disabled until access is restored")
            return
        with ctx.connection() as conn:
            _capture(ctx, conn, "cbonds_probe", probe)
            bonds = _target_bonds(conn, max_isins)
            if not bonds:
                ctx.run.mark_skipped("no active bonds with ISIN in ref.bond")
                return
            fetched = _fetch_all(ctx, conn, http, creds, bonds, batch_size, flow_limit)
            _write(ctx, conn, bonds, fetched)


# --------------------------------------------------------------------------- fetching


def _capture(
    ctx: JobContext, conn: psycopg.Connection[Any], endpoint: str, res: cb.CbondsResult
) -> int:
    return record_capture(conn, ctx.run.id, SOURCE, endpoint, None, res.response, cb.PARSER_VERSION)


def _target_bonds(conn: psycopg.Connection[Any], limit: int) -> dict[str, dict[str, Any]]:
    """isin -> bond facts for active bonds (floaters first)."""
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT b.isin, b.secid, b.coupon_type, b.maturity_date, b.coupon_frequency,
                   b.coupon_benchmark, b.coupon_details, b.coupon_benchmark_spread_pct,
                   b.day_count, b.day_count_source
            FROM ref.bond b
            WHERE b.status = 'active' AND b.isin IS NOT NULL
            ORDER BY (b.coupon_type = 'floating') DESC, b.secid
            """
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for isin, secid, ctype, mat, freq, bench, details, spread, dc, dcs in rows:
        isin = str(isin).upper()
        if not cb.valid_isin(isin) or isin in out:
            continue  # duplicate ISIN across SECIDs: first (floater-first, secid order) wins
        out[isin] = {
            "secid": secid,
            "coupon_type": ctype,
            "maturity_date": mat,
            "coupon_frequency": freq,
            "benchmark": map_benchmark(bench, details),
            "spread_pct": spread,
            "day_count": dc,
            "day_count_source": dcs,
        }
        if len(out) >= limit:
            break
    return out


def _paged(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    creds: cb.CbondsCredentials,
    operation: str,
    filters: list[dict[str, Any]],
    fields: Sequence[str],
    *,
    limit: int,
    sort_field: str = "id",
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        res = cb.query(
            http,
            operation,
            filters=filters,
            fields=fields,
            limit=limit,
            offset=offset,
            sorting=[{"field": sort_field, "order": "asc"}],
            creds=creds,
        )
        _capture(ctx, conn, f"cbonds_{operation}", res)
        items.extend(res.items)
        offset += limit
        if not res.items or res.total is None or offset >= res.total:
            return items
    raise FormatDriftError(f"Cbonds {operation}: more than {max_pages} pages")


def _fetch_all(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    http: HttpClient,
    creds: cb.CbondsCredentials,
    bonds: Mapping[str, Mapping[str, Any]],
    batch_size: int,
    flow_limit: int,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {
        "emissions": [],
        "offers": [],
        "ratings": [],
        "flows": [],
    }
    isins = list(bonds)
    denied: set[str] = set()
    try:
        for batch in _chunks(isins, batch_size):
            joined = ";".join(batch)
            out["emissions"] += [
                cb.parse_emission(r)
                for r in _paged(
                    ctx,
                    conn,
                    http,
                    creds,
                    "get_emissions",
                    [{"field": "isin_code", "operator": "in", "value": joined}],
                    cb.EMISSION_FIELDS,
                    limit=len(batch),
                )
            ]
            for kind, operation, fields, parse in (
                ("offers", "get_offert", cb.OFFER_FIELDS, cb.parse_offer),
                ("ratings", "get_rating_emission_maxdate", cb.RATING_FIELDS, cb.parse_rating),
            ):
                if operation in denied:
                    continue
                try:
                    out[kind] += [
                        parse(r)
                        for r in _paged(
                            ctx,
                            conn,
                            http,
                            creds,
                            operation,
                            [{"field": "emission_isin_code", "operator": "in", "value": joined}],
                            fields,
                            limit=250,
                        )
                    ]
                except cb.CbondsAccessError as exc:
                    # Do not repeat a denied method for every batch, or block accessible data.
                    denied.add(operation)
                    ctx.run.mark_partial(str(exc))
        floaters = [i for i in isins if bonds[i]["coupon_type"] == "floating"][: max(0, flow_limit)]
        for isin in floaters:
            out["flows"] += [
                cb.parse_flow(r)
                for r in _paged(
                    ctx,
                    conn,
                    http,
                    creds,
                    "get_flow_new",
                    [{"field": "emission_isin_code", "operator": "eq", "value": isin}],
                    cb.FLOW_FIELDS,
                    limit=250,
                    sort_field="date",
                )
            ]
    except cb.CbondsAuthError as exc:
        ctx.run.mark_partial(f"Cbonds access lost mid-run: {exc}")
    except (cb.CbondsError, HttpError) as exc:
        ctx.run.mark_partial(f"Cbonds request failed: {exc}")
    return out


# ---------------------------------------------------------------------------- writing


def _add(ctx: JobContext, result: UpsertResult) -> None:
    ctx.run.add_counts(result)


def _write(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    bonds: Mapping[str, Mapping[str, Any]],
    fetched: Mapping[str, list[dict[str, Any]]],
) -> None:
    run_id = ctx.run.id
    today = ctx.today()
    conflicts: list[dict[str, Any]] = []
    checked: dict[str, set[str]] = {}

    def conflict(
        secid: str, field: str, moex: Any, cbonds: Any, selected: str, reason: str
    ) -> None:
        conflicts.append(
            {
                "secid": secid,
                "field": field,
                "source_a": "moex_iss",
                "source_b": SOURCE,
                "value_a": None if moex is None else str(moex),
                "value_b": None if cbonds is None else str(cbonds),
                "selected_source": selected,
                "reason": reason,
                "resolved": False,
                "run_id": run_id,
            }
        )

    # emissions: day count (Cbonds wins), formula row, conflicts
    day_counts: list[tuple[str, str]] = []
    formulas: list[dict[str, Any]] = []
    for e in fetched["emissions"]:
        bond = bonds.get(e["isin"])
        if bond is None:
            continue
        secid = bond["secid"]
        checked.setdefault(secid, set()).update(
            {"maturity_date", "coupon_frequency", "benchmark", "spread"}
        )
        if e["day_count"]:
            day_counts.append((secid, e["day_count"]))
        if (
            e["maturity_date"]
            and bond["maturity_date"]
            and e["maturity_date"] != bond["maturity_date"]
        ):
            conflict(
                secid,
                "maturity_date",
                bond["maturity_date"],
                e["maturity_date"],
                "moex_iss",
                "exchange data: MOEX wins",
            )
        if (
            e["coupon_frequency"]
            and bond["coupon_frequency"]
            and e["coupon_frequency"] != bond["coupon_frequency"]
        ):
            conflict(
                secid,
                "coupon_frequency",
                bond["coupon_frequency"],
                e["coupon_frequency"],
                SOURCE,
                "convention: Cbonds wins",
            )
        if e["floating"]:
            spread_bp = e["margin_pct"] * 100 if e["margin_pct"] is not None else None
            if e["benchmark"] and bond["benchmark"] and e["benchmark"] != bond["benchmark"]:
                conflict(
                    secid,
                    "benchmark",
                    bond["benchmark"],
                    e["benchmark"],
                    SOURCE,
                    "convention: Cbonds wins",
                )
            if (
                e["margin_pct"] is not None
                and bond["spread_pct"] is not None
                and e["margin_pct"] != bond["spread_pct"]
            ):
                conflict(
                    secid,
                    "spread",
                    bond["spread_pct"],
                    e["margin_pct"],
                    SOURCE,
                    "convention: Cbonds wins",
                )
            formulas.append(
                {
                    "secid": secid,
                    "source": SOURCE,
                    "benchmark": e["benchmark"],
                    "benchmark_raw": e["reference_rate"],
                    "spread_bp": spread_bp,
                    "multiplier": None,
                    "lag_days": None,
                    "averaging": None,
                    "floor_pct": e["floor_pct"],
                    "cap_pct": e["cap_pct"],
                    "variable_rate_type": e["variable_rate_type"],
                    "fixing_day": e["fixing_day"],
                    "status": "inferred" if e["benchmark"] and spread_bp is not None else "partial",
                    "evidence": json.loads(json.dumps(e, default=str)),
                    "is_deleted": False,
                    "run_id": run_id,
                }
            )
    _set_day_counts(ctx, conn, day_counts)
    _add(
        ctx,
        upsert_rows(conn, "ref.bond_rate_formula", formulas, ["secid", "source"], run_id=run_id),
    )

    # offers (separate rows; MOEX stays authoritative for the exchange schedule)
    offers: list[dict[str, Any]] = []
    per_secid: dict[str, list[dict[str, Any]]] = {}
    for o in fetched["offers"]:
        bond = bonds.get(o["isin"])
        if bond is not None:
            per_secid.setdefault(bond["secid"], []).append(o)
    moex_offers = _moex_offer_dates(conn, list(per_secid))
    for secid, rows in per_secid.items():
        counts: dict[str, int] = {}
        for o in sorted(
            rows, key=lambda r: (r["offer_date"] or date.max, r["offer_type_raw"] or "")
        ):
            base = o["offer_date"].isoformat() if o["offer_date"] else "undated"
            n = counts.get(base, 0) + 1
            counts[base] = n
            key = f"undated-{n}" if o["offer_date"] is None else (base if n == 1 else f"{base}#{n}")
            status = (
                "undated"
                if o["offer_date"] is None
                else ("scheduled" if o["offer_date"] >= today else "past")
            )
            offers.append(
                {
                    "secid": secid,
                    "source": SOURCE,
                    "offer_key": key,
                    "offer_date": o["offer_date"],
                    "date_missing": o["offer_date"] is None,
                    "offer_type": o["offer_type"],
                    "offer_type_basis": "source_type",
                    "offer_type_raw": o["offer_type_raw"],
                    "status": status,
                    "is_offer_or_redemption": False,
                    "offer_date_start": o["offer_date_start"],
                    "offer_date_end": o["offer_date_end"],
                    "price": o["price"],
                    "value": None,
                    "face_value": None,
                    "face_unit": None,
                    "agent": None,
                    "is_deleted": False,
                    "run_id": run_id,
                }
            )
        future_cb = {o["offer_date"] for o in rows if o["offer_date"] and o["offer_date"] >= today}
        future_mx = {d for d in moex_offers.get(secid, set()) if d >= today}
        checked.setdefault(secid, set()).add("offer_dates")
        if future_cb != future_mx:
            conflict(
                secid,
                "offer_dates",
                sorted(future_mx),
                sorted(future_cb),
                "moex_iss",
                "exchange schedule: MOEX wins",
            )
    _add(
        ctx,
        upsert_rows(
            conn, "ref.bond_offer", offers, ["secid", "source", "offer_key"], run_id=run_id
        ),
    )

    # ratings
    ratings: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in fetched["ratings"]:
        bond = bonds.get(r["isin"])
        if bond is None:
            continue
        key = (bond["secid"], r["agency"], r["scale"])
        prev = ratings.get(key)
        if prev is None or (r["rating_date"] or date.min) > (prev["rating_date"] or date.min):
            ratings[key] = {
                "secid": bond["secid"],
                "agency": r["agency"],
                "scale": r["scale"],
                "source": SOURCE,
                "rating": r["rating"],
                "outlook": r["outlook"],
                "rating_date": r["rating_date"],
                "run_id": run_id,
            }
    _add(
        ctx,
        upsert_rows(
            conn,
            "ref.bond_rating",
            list(ratings.values()),
            ["secid", "agency", "scale", "source"],
            run_id=run_id,
        ),
    )

    # flows: compare coupon payment dates (MOEX wins)
    flow_dates: dict[str, set[date]] = {}
    for f in fetched["flows"]:
        bond = bonds.get(f["isin"])
        if bond is not None and f["pay_date"] is not None and f["coupon_no"] is not None:
            flow_dates.setdefault(bond["secid"], set()).add(f["pay_date"])
    moex_coupons = _moex_coupon_dates(conn, list(flow_dates))
    for secid, dates in flow_dates.items():
        checked.setdefault(secid, set()).add("coupon_dates")
        mx = moex_coupons.get(secid, set())
        if dates != mx:
            only_cb = sorted(dates - mx)[:10]
            only_mx = sorted(mx - dates)[:10]
            conflict(
                secid, "coupon_dates", only_mx, only_cb, "moex_iss", "exchange schedule: MOEX wins"
            )

    # conflicts: upsert current, resolve vanished ones among checked fields
    _add(
        ctx,
        upsert_rows(
            conn,
            "ref.static_conflict",
            conflicts,
            ["secid", "field", "source_a", "source_b"],
            run_id=run_id,
        ),
    )
    _resolve_conflicts(ctx, conn, checked, {(c["secid"], c["field"]) for c in conflicts})

    # observation log
    snaps: list[dict[str, Any]] = []
    for kind in ("emissions", "offers", "ratings", "flows"):
        grouped: dict[str, list[Any]] = {}
        for item in fetched[kind]:
            bond = bonds.get(item["isin"])
            if bond is not None:
                grouped.setdefault(bond["secid"], []).append(item)
        for secid, items in grouped.items():
            snaps.append(
                {
                    "secid": secid,
                    "source": SOURCE,
                    "endpoint": f"cbonds_{kind}",
                    "payload_sha256": canonical_sha256(
                        sorted(json.dumps(i, sort_keys=True, default=str) for i in items)
                    ),
                    "run_id": run_id,
                }
            )
    key = ["secid", "source", "endpoint", "payload_sha256"]
    _add(ctx, upsert_rows(conn, "ref.static_snapshot", snaps, key, update_cols=[], run_id=run_id))
    touch_last_seen(conn, "ref.static_snapshot", key, snaps, run_id)


def _set_day_counts(
    ctx: JobContext, conn: psycopg.Connection[Any], rows: Sequence[tuple[str, str]]
) -> None:
    if not rows:
        return
    conn.execute("SELECT set_config('mdhub.run_id', %s, true)", (str(ctx.run.id),))
    secids = [r[0] for r in rows]
    values = [r[1] for r in rows]
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ref.bond b SET day_count = v.dc, day_count_source = 'cbonds', run_id = %s,
                                  updated_at = now()
            FROM unnest(%s::text[], %s::text[]) AS v(secid, dc)
            WHERE b.secid = v.secid
              AND (b.day_count, b.day_count_source) IS DISTINCT FROM (v.dc, 'cbonds')
            """,
            (ctx.run.id, secids, values),
        )
        ctx.run.add_counts(UpsertResult(updated=max(cur.rowcount, 0)))


def _moex_offer_dates(conn: psycopg.Connection[Any], secids: list[str]) -> dict[str, set[date]]:
    if not secids:
        return {}
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            """
            SELECT secid, offer_date FROM ref.bond_offer
            WHERE source = 'moex_iss' AND NOT is_deleted AND offer_date IS NOT NULL
              AND status NOT IN ('cancelled') AND secid = ANY(%s)
            """,
            (secids,),
        ).fetchall()
    out: dict[str, set[date]] = {}
    for s, d in rows:
        out.setdefault(str(s), set()).add(d)
    return out


def _moex_coupon_dates(conn: psycopg.Connection[Any], secids: list[str]) -> dict[str, set[date]]:
    if not secids:
        return {}
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(
            "SELECT secid, pay_date FROM ref.bond_coupon WHERE NOT is_deleted AND secid = ANY(%s)",
            (secids,),
        ).fetchall()
    out: dict[str, set[date]] = {}
    for s, d in rows:
        out.setdefault(str(s), set()).add(d)
    return out


def _resolve_conflicts(
    ctx: JobContext,
    conn: psycopg.Connection[Any],
    checked: Mapping[str, set[str]],
    current: set[tuple[str, str]],
) -> None:
    if not checked:
        return
    with conn.cursor(row_factory=tuple_row) as cur:
        open_rows = cur.execute(
            """
            SELECT secid, field FROM ref.static_conflict
            WHERE source_b = %s AND NOT resolved AND secid = ANY(%s)
            """,
            (SOURCE, list(checked)),
        ).fetchall()
    to_resolve = [
        (str(s), str(f))
        for s, f in open_rows
        if f in checked.get(str(s), set()) and (s, f) not in current
    ]
    if not to_resolve:
        return
    conn.execute("SELECT set_config('mdhub.run_id', %s, true)", (str(ctx.run.id),))
    with conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE ref.static_conflict SET resolved = true, run_id = %s
            WHERE secid = %s AND field = %s AND source_a = 'moex_iss' AND source_b = %s
            """,
            [(ctx.run.id, s, f, SOURCE) for s, f in to_resolve],
        )
    ctx.run.add_counts(UpsertResult(updated=len(to_resolve)))
