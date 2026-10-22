"""Terms of the floaters on the MOEX KBD, checked against the hub's own ZCYC history
(contract market_snapshot/1.4): job ``zcyc_floater_fit`` (daily 07:20, after ``zcyc`` final,
``bonds_static`` and ``bond_terms_curated``).

MOEX ISS publishes only the benchmark of these floaters (``ZR_YLD_CRV``) and the current margin;
the KBD tenor, the fixing day and the margin of past periods are in the issue documents. For
every bond whose resolved formula has ``benchmark = ZCYC`` the job reads the coupons whose rate
is a fact (``ref.bond_coupon`` ``known`` / ``current_published`` with ``value_prc > 0``), the
dated offers (margin resets) and the end-of-day KBD parameters (``md.zcyc_snapshot``
``is_eod``), and writes one ``ref.bond_rate_formula`` row with ``source = 'zcyc_fit'``
(``status = 'inferred'``):

* curated terms (``source = 'curated'`` with a tenor, from an issue document): they are
  **checked** (:func:`mdhub.verify.zcycfloat.check_terms`); the row carries the reproduction
  and, when the curated row has no margin schedule, the schedule the coupons imply;
* otherwise the terms are **fitted** (:func:`mdhub.verify.zcycfloat.fit`); tenor, fixing rule,
  rounding and the margin schedule are written only when the fit is accepted, the
  reproduction statistics always.

``ref.bond_rate_formula_resolved`` takes the terms curated first, then from this row
(``fixing_basis``). Rows of bonds no longer on ZCYC are soft-deleted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from mdhub.core.upsert import upsert_rows
from mdhub.jobs import JobContext, job
from mdhub.verify.static import BusinessCalendar
from mdhub.verify.zcycfloat import (
    CouponObs,
    KbdHistory,
    Reproduction,
    SpreadStep,
    ZcycTerms,
    check_terms,
    fit,
    implied_schedule,
)

__all__ = ["SOURCE", "fit_bond", "load_inputs", "zcyc_floater_fit"]

SOURCE: Final = "zcyc_fit"
BENCHMARK: Final = "ZCYC"
RESET_TYPES: Final = ("put", "call", "unknown")
PARAM_KEYS: Final = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))

Conn = psycopg.Connection[Any]


def load_history(conn: Conn) -> KbdHistory:
    rows = conn.execute(
        "SELECT trade_date, params FROM md.zcyc_snapshot WHERE is_eod ORDER BY trade_date"
    ).fetchall()
    params = {r["trade_date"]: {k: float(r["params"][k]) for k in PARAM_KEYS} for r in rows}
    return KbdHistory(params, BusinessCalendar.load(conn))


def load_inputs(conn: Conn) -> dict[str, dict[str, Any]]:
    """Per ZCYC floater: current spread, curated row, coupons, reset dates."""
    bonds: dict[str, dict[str, Any]] = {
        r["secid"]: {"spread_bp": r["spread_bp"], "curated": None, "coupons": [], "resets": []}
        for r in conn.execute(
            "SELECT secid, spread_bp FROM ref.bond_rate_formula_resolved WHERE benchmark = %s",
            (BENCHMARK,),
        ).fetchall()
    }
    secids = sorted(bonds)
    for r in conn.execute(
        "SELECT * FROM ref.bond_rate_formula WHERE source = 'curated' AND NOT is_deleted "
        "AND secid = ANY(%s)",
        (secids,),
    ).fetchall():
        bonds[r["secid"]]["curated"] = r
    for r in conn.execute(
        """
        SELECT secid, coupon_no, start_date, value_prc FROM ref.bond_coupon
        WHERE NOT is_deleted AND secid = ANY(%s) AND status IN ('known', 'current_published')
          AND start_date IS NOT NULL AND value_prc > 0
        ORDER BY secid, coupon_no
        """,
        (secids,),
    ).fetchall():
        bonds[r["secid"]]["coupons"].append(
            CouponObs(r["coupon_no"], r["start_date"], Decimal(r["value_prc"]))
        )
    resets: dict[str, set[date]] = defaultdict(set)
    for r in conn.execute(
        "SELECT secid, offer_date FROM ref.bond_offer WHERE NOT is_deleted AND secid = ANY(%s) "
        "AND offer_date IS NOT NULL AND offer_type = ANY(%s)",
        (secids, list(RESET_TYPES)),
    ).fetchall():
        resets[r["secid"]].add(r["offer_date"])
    for s, b in bonds.items():
        b["resets"] = sorted(resets[s])
    return bonds


def _curated_terms(c: dict[str, Any] | None) -> ZcycTerms | None:
    if c is None or c["benchmark_tenor_years"] is None or c["fixing_offset"] is None:
        return None
    return ZcycTerms(
        tenor_years=Decimal(c["benchmark_tenor_years"]),
        fixing_offset=int(c["fixing_offset"]),
        fixing_offset_unit=c["fixing_offset_unit"] or "zcyc_publication_days",
        fixing_anchor=c["fixing_anchor"] or "accrual_start",
        rate_rounding_decimals=2
        if c["rate_rounding_decimals"] is None
        else int(c["rate_rounding_decimals"]),
    )


def fit_bond(
    h: KbdHistory, b: dict[str, Any]
) -> tuple[ZcycTerms | None, list[SpreadStep], Reproduction, dict[str, Any]]:
    """``(terms to publish, margin schedule to publish, reproduction, evidence)``."""
    spread = None if b["spread_bp"] is None else Decimal(b["spread_bp"])
    curated = b["curated"]
    documented = _curated_terms(curated)
    if documented is not None:
        own = [SpreadStep.from_json(s) for s in (curated or {}).get("spread_schedule") or []]
        implied = [] if own else implied_schedule(h, b["coupons"], documented, b["resets"], spread)
        rep = check_terms(h, b["coupons"], documented, own or implied)
        return None, implied, rep, {"method": "check", "terms_source": "curated"}
    res = fit(h, b["coupons"], b["resets"], spread)
    evidence = {"method": "fit", "candidates": res.candidates}
    return (
        (res.reproduction.terms if res.accepted else None),
        res.schedule,
        res.reproduction,
        (evidence),
    )


@job("zcyc_floater_fit", "KBD floater terms checked against the hub's ZCYC history")
def zcyc_floater_fit(ctx: JobContext) -> dict[str, Any]:
    """Fit / check the ZCYC floater terms (see module docstring)."""
    run_id = ctx.run.id
    with ctx.connection() as conn:
        h = load_history(conn)
        bonds = load_inputs(conn)
        history = {
            "first_day": h.days[0].isoformat() if h.days else None,
            "last_day": h.days[-1].isoformat() if h.days else None,
            "publication_days": len(h.days),
        }
        rows = []
        accepted = 0
        for secid, b in sorted(bonds.items()):
            terms, schedule, rep, evidence = fit_bond(h, b)
            accepted += rep.accepted
            rows.append(
                {
                    "secid": secid,
                    "source": SOURCE,
                    "benchmark_tenor_years": terms.tenor_years if terms else None,
                    "fixing_anchor": terms.fixing_anchor if terms else None,
                    "fixing_offset": terms.fixing_offset if terms else None,
                    "fixing_offset_unit": terms.fixing_offset_unit if terms else None,
                    "rate_rounding_decimals": terms.rate_rounding_decimals if terms else None,
                    "spread_schedule": Jsonb([s.as_json() for s in schedule]) if schedule else None,
                    "reproduction": Jsonb(rep.as_json()),
                    "status": "inferred",
                    "evidence": Jsonb(
                        {
                            "job": "zcyc_floater_fit",
                            **evidence,
                            "resets": [d.isoformat() for d in b["resets"]],
                            "zcyc_history": history,
                        }
                    ),
                    "is_deleted": False,
                    "run_id": run_id,
                }
            )
        ctx.run.add_counts(
            upsert_rows(conn, "ref.bond_rate_formula", rows, ["secid", "source"], run_id=run_id)
        )
        gone = conn.execute(
            "UPDATE ref.bond_rate_formula SET is_deleted = true, updated_at = now() "
            "WHERE source = %s AND NOT is_deleted AND NOT secid = ANY(%s) RETURNING secid",
            (SOURCE, sorted(bonds)),
        ).fetchall()
    if not h.days and bonds:
        ctx.run.mark_partial("md.zcyc_snapshot has no end-of-day publication")
    return {
        "zcyc_floaters": len(bonds),
        "accepted": accepted,
        "published_terms": sum(1 for r in rows if r["benchmark_tenor_years"] is not None),
        "soft_deleted": len(gone),
        **history,
    }
