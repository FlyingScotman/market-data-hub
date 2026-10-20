"""Static snapshot builder: instruments, schedules, formulas, issuers, calendars, IdMap.

Everything is read **as known at** ``known_at`` through the readers of migration 0500
(``ref.<table>_known_at``, ``ref.bond_rate_formula_resolved_known_at``,
``ref.calendar_effective_known_at``), so rebuilding a past static snapshot with the same
``known_at`` reproduces it byte for byte (except for writes whose transaction was still open
at ``known_at``: their rows carry an earlier ``ingested_at``).

Universe: bonds whose status (as known) is ``active``, plus ``matured`` bonds with a maturity
date within :data:`MATURED_LOOKBACK_DAYS` before the business date (late EOD rows and final
redemptions stay quotable), plus **alive bonds that are not traded** (``delisted`` with a
maturity date on or after the business date, or perpetual; label ``not_traded``): desk
positions resolve by ISIN against the instruments, also when MOEX lists no board for the
bond. A ``delisted`` bond without a maturity date that is not perpetual is not included (its
life cannot be established). For a linked EOD market cut, ``quoted_secids`` also retains any
known instrument with a price or yield on that date, including older matured and delisted
bonds (label ``retained_for_market_quote``). Missing reference instruments are not invented.
Inside the universe nothing is excluded: unknown coupon types, perpetuals, missing day
counts and partial floater formulas are published with **labels** (contract rule
"coverage labelling instead of exclusion").

Calendars come from ``ref.calendar_effective`` only (never ``ref.weekend_session``): per
calendar the covered range ``[valid_from, valid_to]`` of its rows, holidays (weekdays that are
not business days), working weekends, ``provisional_from`` (first day decided by an unverified
curated row), a version (latest row change time) and a fingerprint; the snapshot carries one
calendar version and fingerprint for the whole set.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Final, LiteralString

from psycopg import sql

from mdhub.db.pool import DictConnection
from mdhub.snapshot.build import Build, Cut, Dataset, Excluded, Provenance, sorted_labels
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex, utc_str

__all__ = [
    "BOND_CALENDAR",
    "MATURED_LOOKBACK_DAYS",
    "build_static",
    "calendar_objects",
]

MATURED_LOOKBACK_DAYS: Final = 31
BOND_CALENDAR: Final = "MOEX_STOCK"
"""Settlement calendar of MOEX bonds (the hub's bond datasets use it)."""

_PROVISIONAL_SOURCES: Final = frozenset({"curated_unverified"})


def _alive_not_traded(b: dict[str, Any], day: date) -> bool:
    """``delisted`` but alive on ``day``: perpetual or maturing on/after it (HS2)."""
    return b["status"] == "delisted" and bool(
        b["is_perpetual"] or (b["maturity_date"] is not None and b["maturity_date"] >= day)
    )


def _universe(
    conn: DictConnection,
    known_at: datetime,
    since: date,
    day: date,
    quoted_secids: frozenset[str],
) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT * FROM ref.bond_known_at(%(t)s) b
        WHERE b.status = 'active' OR (b.status = 'matured' AND b.maturity_date >= %(since)s)
           OR (b.status = 'delisted' AND (b.is_perpetual OR b.maturity_date >= %(day)s))
           OR b.secid = ANY(%(quoted)s)
        ORDER BY b.secid
        """,
        {"t": known_at, "since": since, "day": day, "quoted": sorted(quoted_secids)},
    ).fetchall()


def _by_secid(
    conn: DictConnection, func: str, known_at: datetime, secids: list[str], order: LiteralString
) -> list[dict[str, Any]]:
    """Non-deleted rows of the as-known-at reader ``ref.<func>`` for ``secids``."""
    query = sql.SQL(
        "SELECT * FROM {f}(%(t)s) x WHERE x.secid = ANY(%(s)s) "
        "AND NOT COALESCE(x.is_deleted, false) ORDER BY "
    ).format(f=sql.Identifier("ref", func)) + sql.SQL(order)
    return conn.execute(query, {"t": known_at, "s": secids}).fetchall()


def _boards(conn: DictConnection, known_at: datetime, secids: list[str]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM ref.bond_board_known_at(%(t)s) x WHERE x.secid = ANY(%(s)s) "
        "ORDER BY x.secid, x.board",
        {"t": known_at, "s": secids},
    ).fetchall()


def _instrument(
    b: dict[str, Any], boards: list[dict[str, Any]], labels: list[str]
) -> dict[str, Any]:
    return {
        "secid": b["secid"],
        "isin": b["isin"],
        "regnumber": b["regnumber"],
        "issuer_id": b["issuer_id"],
        "name": b["name"],
        "short_name": b["short_name"],
        "sec_type": b["sec_type"],
        "bond_type": b["bond_type"],
        "bond_subtype": b["bond_subtype"],
        "coupon_details": b["coupon_details"],
        "bond_class": b["bond_class"],
        "coupon_type": b["coupon_type"],
        "coupon_type_basis": b["coupon_type_basis"],
        "coupon_benchmark": b["coupon_benchmark"],
        "coupon_benchmark_spread_pct": b["coupon_benchmark_spread_pct"],
        "face_unit": b["face_unit"],
        "currency_id": b["currency_id"],
        "face_value": b["face_value"],
        "initial_face_value": b["initial_face_value"],
        "issue_date": b["issue_date"],
        "maturity_date": b["maturity_date"],
        "is_perpetual": bool(b["is_perpetual"]),
        "is_amortizing": b["is_amortizing"],
        "is_subordinated": b["is_subordinated"],
        "is_structured": b["is_structured"],
        "is_face_indexed": b["is_face_indexed"],
        "is_fx_face": b["is_fx_face"],
        "qualified_only": b["qualified_only"],
        "high_risk": b["high_risk"],
        "coupon_frequency": b["coupon_frequency"],
        "coupon_period_days": b["coupon_period_days"],
        "day_count": b["day_count"],
        "day_count_source": b["day_count_source"],
        "listing_level": b["listing_level"],
        "issue_size": b["issue_size"],
        "primary_board": b["primary_board"],
        "status": b["status"],
        "calendar": BOND_CALENDAR,
        "boards": [
            {
                "board": r["board"],
                "is_primary": bool(r["is_primary"]),
                "listed": bool(r["listed"]),
                "listing_source": r["listing_source"],
                "lot_size": r["lot_size"],
                "min_step": r["min_step"],
                "decimals": r["decimals"],
                "currency_id": r["currency_id"],
                "face_unit": r["face_unit"],
                "settle_code": r["settle_code"],
                "settle_lag_days": r["settle_lag_days"],
                "listed_from": r["listed_from"],
                "listed_till": r["listed_till"],
            }
            for r in boards
        ],
        "labels": labels,
    }


def calendar_objects(
    conn: DictConnection, known_at: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """``(calendars, snapshot calendar {version, fingerprint}, effective rows)`` as known at
    ``known_at`` from ``ref.calendar_effective_known_at`` (never ``ref.weekend_session``)."""
    eff = conn.execute(
        "SELECT * FROM ref.calendar_effective_known_at(%(t)s) ORDER BY calendar, day",
        {"t": known_at},
    ).fetchall()
    versions = {
        r["calendar"]: r["v"]
        for r in conn.execute(
            "SELECT calendar, max(COALESCE(ingested_at, known_at)) AS v "
            "FROM ref.calendar_day_known_at(%(t)s) GROUP BY calendar",
            {"t": known_at},
        ).fetchall()
    }
    by_cal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in eff:
        by_cal[r["calendar"]].append(r)
    out: list[dict[str, Any]] = []
    for code in sorted(by_cal):
        rows = by_cal[code]
        flags = {r["day"]: bool(r["is_business_day"]) for r in rows}
        valid_from, valid_to = rows[0]["day"], rows[-1]["day"]
        holidays: list[date] = []
        working_weekends: list[date] = []
        d = valid_from
        while d <= valid_to:
            weekday = d.weekday() < 5
            flag = flags.get(d, weekday)
            if weekday and not flag:
                holidays.append(d)
            elif not weekday and flag:
                working_weekends.append(d)
            d += timedelta(days=1)
        provisional = [r["day"] for r in rows if r["source"] in _PROVISIONAL_SOURCES]
        body = {
            "calendar": code,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "provisional_from": min(provisional) if provisional else None,
            "holidays": holidays,
            "working_weekends": working_weekends,
        }
        out.append(
            {
                **body,
                "sources": sorted({r["source"] for r in rows}),
                "version": utc_str(versions.get(code)),
                "fingerprint": sha256_hex(canonical_bytes(body)),
            }
        )
    all_versions = [v for v in versions.values() if v is not None]
    summary = {
        "version": utc_str(max(all_versions)) if all_versions else None,
        "fingerprint": sha256_hex(
            canonical_bytes(
                [{"calendar": c["calendar"], "fingerprint": c["fingerprint"]} for c in out]
            )
        ),
    }
    return out, summary, eff


def _issuer_coverage(
    issuers: list[dict[str, Any]], bonds: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fill rates of the issuer reference (HS1/HS3): over all published issuers and over the
    issuers of ``active`` non-structured bonds (the desk's acceptance base)."""
    active = {
        b["issuer_id"]
        for b in bonds
        if b["status"] == "active" and not b["is_structured"] and b["issuer_id"] is not None
    }

    def rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "issuers": len(rows),
            "with_short_name": sum(1 for i in rows if i["short_name"]),
            "with_ogrn": sum(1 for i in rows if i["ogrn"]),
            "with_country": sum(1 for i in rows if i["country"]),
            "with_sector": sum(1 for i in rows if i["sector"]),
        }

    return {
        **rates(issuers),
        "of_active_non_structured": rates([i for i in issuers if i["issuer_id"] in active]),
        "by_sector": dict(sorted(Counter(i["sector"] or "none" for i in issuers).items())),
        "by_sector_source": dict(
            sorted(Counter(r.get("sector_source") or "none" for r in rows).items())
        ),
        "without_sector": sorted(i["issuer_id"] for i in issuers if not i["sector"]),
    }


def build_static(
    conn: DictConnection,
    *,
    cut: Cut,
    matured_lookback_days: int = MATURED_LOOKBACK_DAYS,
    quoted_secids: frozenset[str] = frozenset(),
) -> Build:
    """Build the static snapshot of ``cut`` (kind ``static``) as known at ``cut.known_at``."""
    if cut.kind != "static":
        raise ValueError("build_static needs a static cut")
    t = cut.known_at
    since = cut.business_date - timedelta(days=matured_lookback_days)
    prov = Provenance()
    bonds = _universe(conn, t, since, cut.business_date, quoted_secids)
    secids = [b["secid"] for b in bonds]
    universe = frozenset(secids)

    boards = _boards(conn, t, secids)
    coupons = _by_secid(conn, "bond_coupon_known_at", t, secids, "x.secid, x.coupon_no")
    amort = _by_secid(
        conn, "bond_amortization_known_at", t, secids, "x.secid, x.amort_date, x.data_source"
    )
    offers = _by_secid(conn, "bond_offer_known_at", t, secids, "x.secid, x.source, x.offer_key")
    formula_rows = _by_secid(conn, "bond_rate_formula_known_at", t, secids, "x.secid, x.source")
    formulas = conn.execute(
        "SELECT * FROM ref.bond_rate_formula_resolved_known_at(%(t)s) x "
        "WHERE x.secid = ANY(%(s)s) ORDER BY x.secid",
        {"t": t, "s": secids},
    ).fetchall()
    issuer_ids = sorted({b["issuer_id"] for b in bonds if b["issuer_id"] is not None})
    issuers = conn.execute(
        "SELECT * FROM ref.issuer_known_at(%(t)s) x WHERE x.issuer_id = ANY(%(i)s) "
        "ORDER BY x.issuer_id",
        {"t": t, "i": issuer_ids},
    ).fetchall()
    isin_rows = conn.execute(
        "SELECT secid, isin FROM ref.bond_known_at(%(t)s) WHERE isin IS NOT NULL",
        {"t": t},
    ).fetchall()
    calendars, cal_summary, cal_rows = calendar_objects(conn, t)

    # ---------------------------------------------------------------- instruments
    boards_of: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in boards:
        boards_of[r["secid"]].append(r)
    coupon_count = Counter(c["secid"] for c in coupons)
    formula_of = {f["secid"]: f for f in formulas}
    issuer_set = {i["issuer_id"] for i in issuers}
    scheduled_calls = {
        o["secid"] for o in offers if o["offer_type"] == "call" and o["status"] == "scheduled"
    }
    instruments = []
    label_counts: Counter[str] = Counter()
    for b in bonds:
        f = formula_of.get(b["secid"])
        labels = sorted_labels(
            "retained_for_market_quote"
            if b["secid"] in quoted_secids
            and not (
                b["status"] == "active"
                or (
                    b["status"] == "matured"
                    and b["maturity_date"] is not None
                    and b["maturity_date"] >= since
                )
                or _alive_not_traded(b, cut.business_date)
            )
            else None,
            "not_traded" if _alive_not_traded(b, cut.business_date) else None,
            "coupon_type_unknown" if b["coupon_type"] == "unknown" else None,
            "perpetual" if b["is_perpetual"] else None,
            "perpetual_no_call_date"
            if b["is_perpetual"] and b["secid"] not in scheduled_calls
            else None,
            "day_count_not_published" if b["day_count"] is None else None,
            "floater_formula_missing" if b["coupon_type"] == "floating" and f is None else None,
            "floater_formula_partial" if f is not None and f["status"] == "partial" else None,
            "coupon_schedule_empty"
            if coupon_count[b["secid"]] == 0 and b["coupon_type"] != "zero"
            else None,
            "no_issuer" if b["issuer_id"] is None or b["issuer_id"] not in issuer_set else None,
            "structured" if b["is_structured"] else None,
            "matured" if b["status"] == "matured" else None,
        )
        label_counts.update(labels)
        instruments.append(_instrument(b, boards_of[b["secid"]], labels))
    prov.add_rows("instruments", bonds)

    coupon_rows = [
        {
            "secid": c["secid"],
            "coupon_no": c["coupon_no"],
            "start_date": c["start_date"],
            "pay_date": c["pay_date"],
            "record_date": c["record_date"],
            "amount_per_bond": c["amount"],
            "amount_rub_per_bond": c["value_rub"],
            "rate_pct": c["value_prc"],
            "face_value": c["face_value"],
            "face_unit": c["face_unit"],
            "status": c["status"],
        }
        for c in coupons
    ]
    prov.add_rows("coupon_periods", coupons)
    redemptions = [
        {
            "secid": a["secid"],
            "date": a["amort_date"],
            "kind": "maturity" if a["data_source"] == "maturity" else "amortization",
            "value_pct_of_initial": a["value_prc"],
            "value_per_bond": a["value"],
            "value_rub_per_bond": a["value_rub"],
            "face_value": a["face_value"],
            "initial_face_value": a["initial_face_value"],
            "face_unit": a["face_unit"],
        }
        for a in amort
    ]
    prov.add_rows("redemptions", amort)
    offer_rows = [
        {
            "secid": o["secid"],
            "source": o["source"],
            "offer_key": o["offer_key"],
            "offer_date": o["offer_date"],
            "type": o["offer_type"],
            "type_basis": o["offer_type_basis"],
            "type_raw": o["offer_type_raw"],
            "status": o["status"],
            "is_offer_or_redemption": bool(o["is_offer_or_redemption"]),
            "window_start": o["offer_date_start"],
            "window_end": o["offer_date_end"],
            "price_pct": o["price"],
            "value_per_bond": o["value"],
            "face_value": o["face_value"],
            "face_unit": o["face_unit"],
            "agent": o["agent"],
            "labels": sorted_labels("date_missing" if o["date_missing"] else None),
        }
        for o in offers
    ]
    prov.add_rows("offers", offers)
    formula_out = [
        {
            "secid": f["secid"],
            "benchmark": f["benchmark"],
            "benchmark_raw": f["benchmark_raw"],
            "benchmark_source": f["benchmark_source"],
            "spread_bp": f["spread_bp"],
            "multiplier": f["multiplier"],
            "lag_days": f["lag_days"],
            "averaging": f["averaging"],
            "floor_pct": f["floor_pct"],
            "cap_pct": f["cap_pct"],
            "variable_rate_type": f["variable_rate_type"],
            "fixing_day": f["fixing_day"],
            "status": f["status"],
            "sources": list(f["sources"] or []),
        }
        for f in formulas
    ]
    prov.add_rows("floater_formulas", formula_rows)
    issuer_out = [
        {
            "issuer_id": i["issuer_id"],
            "inn": i["inn"],
            "ogrn": i["ogrn"],
            "name": i["name"],
            "short_name": i["short_name"],
            "sector": i["sector"],
            "country": i["country"],
            "is_sovereign": bool(i["is_sovereign"]),
        }
        for i in issuers
    ]
    prov.add_rows("issuers", issuers)
    for r in cal_rows:
        prov.add("calendars", r["source"], r["run_id"], r["ingested_at"])

    secids_of_isin: dict[str, set[str]] = defaultdict(set)
    for r in isin_rows:
        secids_of_isin[r["isin"]].add(r["secid"])
    id_map = [
        {
            "secid": b["secid"],
            "isin": b["isin"],
            "regnumber": b["regnumber"],
            "isin_shared_with": sorted(secids_of_isin.get(b["isin"], set()) - {b["secid"]})
            if b["isin"]
            else [],
        }
        for b in bonds
    ]
    prov.add_rows("id_map", bonds)

    missing_cals = sorted(
        {"MOEX_STOCK", "MOEX_FX", "MOEX_FORTS", "RU_PRODUCTION", "TARGET"}
        - {c["calendar"] for c in calendars}
    )
    datasets = [
        Dataset(
            "instruments",
            instruments,
            {
                "universe": "status active, or matured with maturity_date >= since, or "
                "delisted and alive (perpetual or maturity_date >= business_date)"
                + (", or quoted in the linked market cut" if quoted_secids else ""),
                "since": since,
                "by_coupon_type": dict(sorted(Counter(b["coupon_type"] for b in bonds).items())),
                "by_status": dict(sorted(Counter(b["status"] for b in bonds).items())),
                "labels": dict(sorted(label_counts.items())),
            },
        ),
        Dataset(
            "coupon_periods",
            coupon_rows,
            {"by_status": dict(sorted(Counter(c["status"] for c in coupons).items()))},
        ),
        Dataset("redemptions", redemptions),
        Dataset("offers", offer_rows),
        Dataset(
            "floater_formulas",
            formula_out,
            {
                "by_status": dict(sorted(Counter(f["status"] for f in formulas).items())),
                "floaters_without_formula": label_counts.get("floater_formula_missing", 0),
            },
        ),
        Dataset("issuers", issuer_out, _issuer_coverage(issuer_out, bonds, issuers)),
        Dataset(
            "calendars",
            calendars,
            {"missing": missing_cals},
            [f"calendar {c} has no rows" for c in missing_cals],
        ),
        Dataset(
            "id_map",
            id_map,
            {"ambiguous_isin": sum(1 for r in id_map if r["isin_shared_with"])},
        ),
    ]
    return Build(
        cut=cut,
        datasets=datasets,
        calendar=cal_summary,
        parameters={
            "universe": "active_or_recently_matured_or_alive_or_quoted"
            if quoted_secids
            else "active_or_recently_matured_or_alive",
            "matured_lookback_days": matured_lookback_days,
            "bond_calendar": BOND_CALENDAR,
        },
        provenance=prov,
        excluded=Excluded(),
        universe=universe,
    )
