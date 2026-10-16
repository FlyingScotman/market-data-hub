"""Phase 2: map the verbatim ``legacy_bc`` (bond catalog) copy into ``ref.*``.

``mdhub legacy map-bc`` runs :func:`map_bc` (one ``ops.job_run``, job ``legacy_map_bc``):

* ``bonds`` - only SECIDs **absent from ref.bond after a live bonds_static run** (matured /
  delisted history that ISS no longer lists), in one transaction: ``ref.issuer`` (missing
  issuers only), ``ref.bond``, ``ref.bond_board``, ``ref.bond_coupon``,
  ``ref.bond_amortization``, ``ref.bond_offer`` and ``ref.bond_rate_formula`` (MOEX
  benchmark fields as the live loader derives them, ``source = 'legacy_bc'``). Rows use the
  live loader's own derivations (:mod:`mdhub.sources.moex_static`: bond class, coupon type,
  coupon numbering/status, offer keys/status).
* ``curated_formulas`` - the curated legacy formula models (``exact_curated`` /
  ``partial_public_formula`` with a benchmark) as ``source = 'curated'`` rows of
  ``ref.bond_rate_formula`` for **every** bond they apply to (active ones included).

The export blanked every raw payload (``raw_json``, ``board_json``), so fields that exist only
there (coupon start/record dates, offer type, per-event face value, lot sizes ...) are NULL /
``unknown`` - never inferred. Rows owned by a live loader (``run_id`` of another job) are never
touched (``live_overlap`` counts); re-runs are no-ops.
"""

from __future__ import annotations

import json
import logging
import time as _time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg.types.json import Jsonb

from mdhub.core.capture import job_run
from mdhub.core.timeutil import msk_today
from mdhub.core.upsert import UpsertResult
from mdhub.db.pool import Database
from mdhub.legacy.import_sqlite import LegacyImportError
from mdhub.legacy.map_md import TargetReport, _existing, _split_live, _upsert, dec
from mdhub.sources import moex_static as ms

__all__ = ["CURATED_STATUSES", "JOB", "TARGETS", "BcMapResult", "map_bc"]

log = logging.getLogger(__name__)

JOB: Final = "legacy_map_bc"
SCHEMA: Final = "legacy_bc"
SOURCE: Final = "legacy_bc"
TARGETS: Final = ("bonds", "curated_formulas")
ZERO_DATE: Final = "0000-00-00"
CURATED_STATUSES: Final = {"exact_curated": "curated", "partial_public_formula": "partial"}
CURATED_BENCHMARKS: Final = {
    "RUONIA": "RUONIA",
    "MOEX G-curve 5Y": "ZCYC",
    "RUONIA compounded index": "RUONIA_INDEX",
}
"""Legacy curated benchmark labels -> hub benchmark codes (unknown labels -> ``OTHER``)."""


def _legacy_date(value: str | None) -> date | None:
    """ISO date; ``None`` for NULL / '' / ``0000-00-00`` (MOEX placeholder)."""
    if value is None or value == "" or value == ZERO_DATE:
        return None
    return date.fromisoformat(value)


def _run_time(run_id: str | None) -> datetime | None:
    """Legacy run id ``YYYYMMDDTHHMMSSZ`` -> aware UTC datetime."""
    if not run_id:
        return None
    return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


class BcMapResult:
    """Outcome of :func:`map_bc`."""

    def __init__(self, run_id: int, targets: list[TargetReport]) -> None:
        self.run_id = run_id
        self.targets = targets

    @property
    def ok(self) -> bool:
        return all(t.error is None for t in self.targets)


def _legacy_runs(conn: psycopg.Connection[Any]) -> list[int]:
    with conn.cursor(row_factory=tuple_row) as cur:
        return [
            int(r[0])
            for r in cur.execute("SELECT id FROM ops.job_run WHERE job = %s", (JOB,)).fetchall()
        ]


def _rows(
    conn: psycopg.Connection[Any], query: str, params: Sequence[Any] = ()
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


# --------------------------------------------------------------------------------- bonds


def _scope(
    conn: psycopg.Connection[Any], legacy_runs: Sequence[int], rep: TargetReport
) -> list[str]:
    with conn.cursor(row_factory=tuple_row) as cur:
        live = {
            str(r[0])
            for r in cur.execute(
                "SELECT secid FROM ref.bond WHERE run_id IS NULL OR NOT (run_id = ANY(%s))",
                (list(legacy_runs),),
            )
        }
        legacy = [
            str(r[0]) for r in cur.execute("SELECT secid FROM legacy_bc.issues ORDER BY secid")
        ]
    if not live:
        raise LegacyImportError(
            "ref.bond has no rows from a live bonds_static run: map-bc maps only SECIDs absent "
            "from the live load; run `mdhub run bonds_static` first"
        )
    rep.source_rows = len(legacy)
    rep.notes["legacy_secids_in_live_load"] = sum(1 for s in legacy if s in live)
    return [s for s in legacy if s not in live]


def _events(
    conn: psycopg.Connection[Any], secids: Sequence[str]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for e in _rows(
        conn,
        "SELECT secid, event_type, event_date, ordinal, value, rate_pct, currency, event_key "
        "FROM legacy_bc.events WHERE secid = ANY(%s) ORDER BY secid, event_type, event_date, "
        "ordinal, event_key",
        (list(secids),),
    ):
        out[e["secid"]][e["event_type"]].append(e)
    return out


def _bond_and_schedule(
    issue: Mapping[str, Any],
    events: Mapping[str, list[dict[str, Any]]],
    today: date,
    run_id: int,
    rep: TargetReport,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    secid = issue["secid"]
    face_unit = ms.norm_ccy(issue["faceunit"])
    bond_type = issue["bond_type"]
    bond_subtype = issue["bond_subtype"]
    maturity_raw = issue["maturity_date"]
    if maturity_raw == ZERO_DATE:
        rep.note("maturity_0000_00_00")

    coupons_src = []
    for e in events.get("coupon", []):
        pay = _legacy_date(e["event_date"])
        if pay is None:
            rep.note("coupon_without_date_skipped")
            continue
        coupons_src.append(
            ms.CouponRow(
                secid=secid,
                isin=None,
                name=None,
                pay_date=pay,
                start_date=None,
                record_date=None,
                value=dec(e["value"]),
                value_prc=dec(e["rate_pct"]),
                value_rub=None,
                face_value=None,
                initial_face_value=None,
                face_unit=ms.norm_ccy(e["currency"]) or face_unit,
                issue_value=None,
                capture_id=None,
            )
        )
    amort_events = [
        e for e in events.get("amortization", []) if _legacy_date(e["event_date"]) is not None
    ]
    has_amort_rows = bool(amort_events)

    is_perpetual = bond_subtype == ms.PERPETUAL_SUBTYPE or (
        bond_subtype == "До оферты (call)"
        and _legacy_date(maturity_raw) is None
        and not has_amort_rows
    )
    maturity = None if is_perpetual else _legacy_date(maturity_raw)
    is_matured = maturity is not None and maturity < today
    sec_type = None if issue["type"] == "bondization_only" else issue["type"]
    struct_flag = True if str(issue["instrument_class"] or "").startswith("structured") else None
    inp = ms.ClassifyInput(
        secid=secid,
        name=issue["name"],
        short_name=issue["shortname"],
        sec_type=sec_type,
        issuer_inn=issue["emitent_inn"],
        issuer_name=issue["emitent_title"],
        face_unit=face_unit,
        bond_type=bond_type,
        bond_subtype=bond_subtype,
        coupon_details=None,
        facevalue_type=None,
        coupon_benchmark=issue["coupon_benchmark"],
        struct_flag=struct_flag,
        has_description=bool(issue["description_available"]),
        is_matured=is_matured,
        coupon_values=tuple(ms.coupon_has_value(c, "fixed") for c in coupons_src),
    )
    bond_class = ms.classify_bond(inp)
    coupon_type, basis = ms.classify_coupon_type(inp, bond_class)
    benchmark = ms.map_benchmark(issue["coupon_benchmark"])
    face_indexed = "линкер" in (bond_type or "").lower()
    spread = dec(issue["coupon_benchmark_spread"])
    first_seen = _run_time(issue["first_seen_run"])
    bond = {
        "secid": secid,
        "isin": issue["isin"],
        "regnumber": issue["regnumber"],
        "issuer_id": int(issue["emitent_id"]) if issue["emitent_id"] else None,
        "name": issue["name"],
        "short_name": issue["shortname"],
        "sec_type": sec_type,
        "bond_type": bond_type,
        "bond_subtype": bond_subtype,
        "bond_class": bond_class,
        "coupon_type": coupon_type,
        "coupon_type_basis": basis,
        "coupon_benchmark": issue["coupon_benchmark"],
        "coupon_benchmark_spread_pct": spread,
        "face_value": dec(issue["facevalue"]),
        "initial_face_value": dec(issue["initial_facevalue"]),
        "face_unit": face_unit,
        "currency_id": ms.norm_ccy(issue["currency"]),
        "issue_date": _legacy_date(issue["issue_date"]),
        "maturity_date": maturity,
        "is_perpetual": is_perpetual,
        "coupon_frequency": issue["coupon_frequency"],
        "coupon_period_days": issue["coupon_period_days"],
        "listing_level": issue["list_level"],
        "issue_size": dec(issue["issue_size"]),
        "issue_size_placed": dec(issue["issue_size_placed"]),
        "is_amortizing": None,  # set from the mapped amortization rows below
        "is_structured": True if struct_flag else None,
        "is_convertible": (bond_type == ms.CONVERTIBLE_BOND_TYPE) if bond_type else None,
        "is_face_indexed": face_indexed if bond_type else None,
        "is_fx_face": (face_unit != "RUB") if face_unit else None,
        "qualified_only": bool(issue["qualified_investors"])
        if issue["qualified_investors"] is not None
        else None,
        "primary_board": issue["primary_boardid"],
        "marketprice_board": issue["marketprice_boardid"],
        "is_traded": False,
        "in_directory": False,
        "description_source": None,
        "status": "matured" if is_matured else "delisted",
        "source": SOURCE,
        "first_seen_at": first_seen,
        "run_id": run_id,
        "capture_id": None,
    }
    if bond["isin"] == "RU000A0D05B7" and secid != "RU000A0D05B7":
        # duplicate ISIN (audit 04 §2.3): the ISIN stays on the SECID equal to it
        bond["isin"] = None
        rep.note("duplicate_isin_cleared")

    numbered = ms.number_coupons(coupons_src)
    current = ms.current_coupon_index(numbered, today)
    coupons = []
    for no, c in numbered:
        status = ms.coupon_status(
            c,
            coupon_type=coupon_type,
            benchmark=benchmark,
            is_current=no == current,
            face_indexed=face_indexed,
            today=today,
        )
        has = status != "unknown"
        coupons.append(
            {
                "secid": secid,
                "coupon_no": no,
                "start_date": None,
                "pay_date": c.pay_date,
                "record_date": None,
                "amount": c.value if has else None,
                "value_rub": c.value if has and c.face_unit == "RUB" else None,
                "value_prc": c.value_prc if has else None,
                "face_value": None,
                "face_unit": c.face_unit,
                "status": status,
                "is_deleted": False,
                "source": SOURCE,
                "run_id": run_id,
                "capture_id": None,
            }
        )

    amorts: dict[tuple[date, str], dict[str, Any]] = {}
    for e in amort_events:
        d = _legacy_date(e["event_date"])
        assert d is not None
        value, prc = dec(e["value"]), dec(e["rate_pct"])
        if (value is None or value == 0) and (prc is None or prc == 0):
            rep.note("zero_amortization_skipped")
            continue
        source_kind = "maturity" if maturity is not None and d == maturity else "amortization"
        key = (d, source_kind)
        if key in amorts:
            rep.note("duplicate_amortization_skipped")
            continue
        unit = ms.norm_ccy(e["currency"]) or face_unit
        amorts[key] = {
            "secid": secid,
            "amort_date": d,
            "data_source": source_kind,
            "value_prc": prc,
            "value": value,
            "value_rub": value if unit == "RUB" else None,
            "face_value": None,
            "initial_face_value": None,
            "face_unit": unit,
            "is_deleted": False,
            "source": SOURCE,
            "run_id": run_id,
            "capture_id": None,
        }

    if amorts:
        bond["is_amortizing"] = any(k[1] == "amortization" for k in amorts)

    offer_rows = [
        ms.OfferRow(
            secid=secid,
            offer_date_raw=e["event_date"],
            offer_date=_legacy_date(e["event_date"]),
            offer_date_start=None,
            offer_date_end=None,
            price=None,
            value=dec(e["value"]),
            face_value=None,
            face_unit=ms.norm_ccy(e["currency"]) or face_unit,
            agent=None,
            offer_type_raw=None,
            capture_id=None,
        )
        for e in events.get("offer", [])
    ]
    options = ms.OptionDates(
        put=_legacy_date(issue["put_option_date"]),
        call=_legacy_date(issue["call_option_date"]),
        buyback=None,
        bond_subtype=bond_subtype,
    )
    offers = [
        {"secid": secid, "source": SOURCE, **o, "is_deleted": False, "run_id": run_id}
        for o in ms.normalize_offers(offer_rows, options, today)
    ]
    rep.note("offers_undated", sum(1 for o in offers if o["date_missing"]))

    formula = None
    if benchmark is not None or coupon_type == "floating":
        spread_bp = spread * 100 if spread is not None else None
        formula = {
            "secid": secid,
            "source": SOURCE,
            "benchmark": benchmark,
            "benchmark_raw": issue["coupon_benchmark"],
            "spread_bp": spread_bp,
            "multiplier": None,
            "lag_days": None,
            "averaging": None,
            "floor_pct": None,
            "cap_pct": None,
            "variable_rate_type": None,
            "fixing_day": None,
            "status": "inferred" if benchmark is not None and spread_bp is not None else "partial",
            "evidence": {
                "legacy_table": f"{SCHEMA}.issues",
                "COUPON_BENCHMARK": issue["coupon_benchmark"],
                "COUPON_BENCHMARK_SPREAD": str(spread) if spread is not None else None,
                "BOND_TYPE": bond_type,
            },
            "is_deleted": False,
            "run_id": run_id,
            "capture_id": None,
        }
    return bond, coupons, list(amorts.values()), offers, formula


def _map_bonds(
    conn: psycopg.Connection[Any],
    run_id: int,
    legacy_runs: Sequence[int],
    rep: TargetReport,
    today: date,
) -> None:
    secids = _scope(conn, legacy_runs, rep)
    rep.candidates = len(secids)
    if not secids:
        return
    issues = _rows(
        conn, "SELECT * FROM legacy_bc.issues WHERE secid = ANY(%s) ORDER BY secid", (secids,)
    )
    events = _events(conn, secids)
    bonds, coupons, amorts, offers, formulas = [], [], [], [], []
    for issue in issues:
        b, c, a, o, f = _bond_and_schedule(
            issue, events.get(issue["secid"], {}), today, run_id, rep
        )
        bonds.append(b)
        coupons += c
        amorts += a
        offers += o
        if f is not None:
            formulas.append(f)
    by_status: dict[str, int] = defaultdict(int)
    for b in bonds:
        by_status[f"{b['status']}/{b['bond_class']}"] += 1
    rep.notes["bonds_by_status_class"] = dict(sorted(by_status.items()))

    # issuers referenced by the mapped bonds and missing from ref.issuer
    ids = sorted({b["issuer_id"] for b in bonds if b["issuer_id"] is not None})
    with conn.cursor(row_factory=tuple_row) as cur:
        present = {
            int(r[0])
            for r in cur.execute(
                "SELECT issuer_id FROM ref.issuer WHERE issuer_id = ANY(%s) "
                "AND (run_id IS NULL OR NOT (run_id = ANY(%s)))",
                (ids, list(legacy_runs)),
            )
        }
    cbr = {
        i["emitent_id"]: i
        for i in _rows(
            conn,
            "SELECT DISTINCT ON (emitent_id) emitent_id, issuer_ogrn, issuer_country, "
            "issuer_short_name_cbr FROM legacy_bc.issues WHERE emitent_id IS NOT NULL "
            "AND issuer_ogrn IS NOT NULL ORDER BY emitent_id, secid",
        )
    }
    issuers = []
    for i in _rows(
        conn,
        "SELECT emitent_id, title, inn, okpo, first_seen_run FROM legacy_bc.issuers "
        "WHERE emitent_id = ANY(%s)",
        ([str(x) for x in ids if x not in present],),
    ):
        extra = cbr.get(i["emitent_id"], {})
        issuers.append(
            {
                "issuer_id": int(i["emitent_id"]),
                "name": i["title"],
                "short_name": extra.get("issuer_short_name_cbr"),
                "inn": i["inn"],
                "ogrn": extra.get("issuer_ogrn"),
                "okpo": i["okpo"],
                "country": extra.get("issuer_country"),
                "is_sovereign": i["inn"] == ms.MINFIN_INN,
                "source": SOURCE,
                "run_id": run_id,
                "capture_id": None,
            }
        )
    rep.notes["issuers_already_live"] = len(present)

    boards = []
    for r in _rows(
        conn,
        "SELECT secid, boardid, is_primary, first_seen_run, last_seen_run FROM legacy_bc.boards "
        "WHERE secid = ANY(%s) ORDER BY secid, boardid",
        (secids,),
    ):
        first, last = _run_time(r["first_seen_run"]), _run_time(r["last_seen_run"])
        boards.append(
            {
                "secid": r["secid"],
                "board": r["boardid"],
                "is_primary": bool(r["is_primary"]),
                "listed": False,
                "listing_source": "description",
                "source": SOURCE,
                "first_seen": first.date() if first else None,
                "last_seen": last.date() if last else None,
                "run_id": run_id,
                "capture_id": None,
            }
        )

    results: dict[str, Any] = {}
    total = UpsertResult()
    for table, rows, key in (
        ("ref.issuer", issuers, ["issuer_id"]),
        ("ref.bond", bonds, ["secid"]),
        ("ref.bond_board", boards, ["secid", "board"]),
        ("ref.bond_coupon", coupons, ["secid", "coupon_no"]),
        ("ref.bond_amortization", amorts, ["secid", "amort_date", "data_source"]),
        ("ref.bond_offer", offers, ["secid", "source", "offer_key"]),
        ("ref.bond_rate_formula", formulas, ["secid", "source"]),
    ):
        for r in rows:
            for k, v in r.items():
                if isinstance(v, dict):
                    r[k] = Jsonb(v)
        # keys of SECIDs in scope are never owned by a live run (scope excludes them); the
        # check still protects schedule rows a live loader may have written meanwhile
        where = "issuer_id = ANY(%s)" if table == "ref.issuer" else "secid = ANY(%s)"
        scope_keys: list[Any] = list(ids) if table == "ref.issuer" else list(secids)
        live = _existing(conn, table, key, key[:1], legacy_runs, where, [scope_keys])
        write = _split_live(rows, key, key[:1], live, rep)
        res = _upsert(conn, table, write, key, run_id)
        total += res
        results[table] = {
            "rows": len(rows),
            "inserted": res.inserted,
            "updated": res.updated,
            "unchanged": res.unchanged,
        }
    rep.counts = total
    rep.notes["tables"] = results


# ------------------------------------------------------------------------ formulas


def _curated_row(m: Mapping[str, Any], run_id: int) -> dict[str, Any]:
    model = json.loads(m["model_json"])
    formula = model.get("formula") or {}
    label = m["benchmark"]
    lag = next(
        (
            formula[k]
            for k in (
                "lag_calendar_days",
                "fixing_calendar_days_before_start",
                "index_lag_calendar_days",
            )
            if formula.get(k) is not None
        ),
        None,
    )
    spread = formula.get("spread_pp", m["spread_pp"])
    averaging = "arithmetic" if str(m["model_type"]).startswith("arithmetic") else None
    return {
        "secid": m["secid"],
        "source": "curated",
        "benchmark": CURATED_BENCHMARKS.get(label, "OTHER"),
        "benchmark_raw": label,
        "spread_bp": Decimal(str(spread)) * 100 if spread is not None else None,
        "multiplier": None,
        "lag_days": int(lag) if lag is not None else None,
        "averaging": averaging,
        "floor_pct": None,
        "cap_pct": None,
        "variable_rate_type": None,
        "fixing_day": None,
        "status": CURATED_STATUSES[m["status"]],
        "evidence": {
            "legacy_table": f"{SCHEMA}.formula_models",
            "model_type": m["model_type"],
            "legacy_status": m["status"],
            "legacy_source": m["source"],
            "llm_used": bool(m["llm_used"]),
            "missing_terms": json.loads(m["missing_terms_json"] or "[]"),
            "formula": formula,
        },
        "is_deleted": False,
        "run_id": run_id,
        "capture_id": None,
    }


def _map_curated(
    conn: psycopg.Connection[Any],
    run_id: int,
    legacy_runs: Sequence[int],
    rep: TargetReport,
    today: date,
) -> None:
    models = _rows(
        conn,
        "SELECT * FROM legacy_bc.formula_models WHERE status = ANY(%s) ORDER BY secid",
        (list(CURATED_STATUSES),),
    )
    rep.source_rows = len(models)
    with conn.cursor(row_factory=tuple_row) as cur:
        known = {
            str(r[0])
            for r in cur.execute(
                "SELECT secid FROM ref.bond WHERE secid = ANY(%s)", ([m["secid"] for m in models],)
            )
        }
    rows = []
    for m in models:
        if m["benchmark"] is None:
            rep.note(f"skipped_without_benchmark:{m['secid']}/{m['model_type']}")
            continue
        if m["secid"] not in known:
            rep.note(f"skipped_not_in_ref_bond:{m['secid']}")
            continue
        rows.append(_curated_row(m, run_id))
    rep.candidates = len(rows)
    key = ["secid", "source"]
    cmp = ["benchmark", "spread_bp", "lag_days"]
    live = _existing(conn, "ref.bond_rate_formula", key, cmp, legacy_runs, "source = 'curated'")
    write = _split_live(rows, key, cmp, live, rep)
    for r in write:
        r["evidence"] = Jsonb(r["evidence"])
    rep.counts = _upsert(conn, "ref.bond_rate_formula", write, key, run_id)
    rep.notes["secids"] = [r["secid"] for r in rows]


# -------------------------------------------------------------------------- runner

_MAPPERS: Final[
    dict[str, Callable[[psycopg.Connection[Any], int, Sequence[int], TargetReport, date], None]]
] = {"bonds": _map_bonds, "curated_formulas": _map_curated}


def map_bc(
    db: Database,
    *,
    targets: Sequence[str] | None = None,
    allow_unverified: bool = False,
    today: date | None = None,
    progress: Callable[[str], None] | None = None,
) -> BcMapResult:
    """Map ``legacy_bc`` into ``ref.*`` (see module docstring).

    :raises LegacyImportError: no (verified) import of ``legacy_bc``; unknown target.
    """
    say = progress or (lambda msg: log.info("%s", msg))
    chosen = list(targets or TARGETS)
    unknown = [t for t in chosen if t not in _MAPPERS]
    if unknown:
        raise LegacyImportError(f"unknown target(s) {unknown}; choose from {list(TARGETS)}")
    with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
        row = cur.execute(
            "SELECT id, verified FROM ops.legacy_import WHERE schema_name = %s "
            "ORDER BY id DESC LIMIT 1",
            (SCHEMA,),
        ).fetchone()
    if row is None:
        raise LegacyImportError(f"no import recorded for {SCHEMA}; run `mdhub legacy import`")
    if not row[1] and not allow_unverified:
        raise LegacyImportError(
            f"{SCHEMA} import #{row[0]} is not verified; run `mdhub legacy verify`"
        )
    day = today or msk_today()
    reports: list[TargetReport] = []
    with job_run(
        db, JOB, {"targets": chosen, "legacy_import_id": int(row[0])}, trigger="cli"
    ) as run:
        for name in chosen:
            rep = TargetReport(target=name)
            t0 = _time.monotonic()
            try:
                with db.connection() as conn:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (JOB,))
                    legacy_runs = sorted({*_legacy_runs(conn), run.id})
                    _MAPPERS[name](conn, run.id, legacy_runs, rep, day)
            except LegacyImportError as exc:
                rep.error = str(exc)
                run.mark_partial(f"{name}: {exc}")
            except (psycopg.Error, ValueError, KeyError, TypeError, AssertionError) as exc:
                rep.error = f"{type(exc).__name__}: {str(exc)[:500]}"
                run.mark_partial(f"{name}: {rep.error}")
                log.exception("legacy map-bc %s failed", name)
            rep.duration_s = _time.monotonic() - t0
            run.add_counts(rep.counts)
            reports.append(rep)
            say(
                f"{name}: {rep.source_rows} legacy rows -> {rep.candidates} candidates, "
                f"inserted {rep.counts.inserted}, updated {rep.counts.updated}, "
                f"unchanged {rep.counts.unchanged}; live overlap {rep.live_overlap} "
                f"in {rep.duration_s:.1f}s" + (f" ERROR {rep.error}" if rep.error else "")
            )
    return BcMapResult(run_id=run.id, targets=reports)
