"""Issue terms curated from issue documents, with evidence (WEB-17; TDM brief R9 HS8 / HS9 /
HS10, review P1-6 / P1-9): job ``bond_terms_curated`` (daily 06:55, after ``bonds_static``).

Source: the versioned package file ``mdhub/data/bond_terms_curated.json`` (its
``description`` is the format). MOEX ISS does not publish floater conventions (lag,
averaging, floor / cap), call schedules of perpetuals or the dates of many offers; Cbonds
covers part of it under licence. The documents themselves (decision on issue, prospectus)
are public on e-disclosure / NSD but not machine-readable from the hub (e-disclosure answers
automated clients with an anti-bot page), so terms enter through this file, one entry per
clause, each quoting the clause (``evidence.extracted_text``) with the document URL and
page. :func:`load_terms` rejects an entry without evidence, with an unknown term, a bad value
or a duplicate key (fail closed: nothing of the file is applied).

The job writes, for every entry whose SECID is in ``ref.bond`` (an ``isin`` given in the
entry must match):

* ``ref.bond_curated_term`` - the entry verbatim (value + evidence + file version);
* ``floater_formula`` -> ``ref.bond_rate_formula`` (``source = 'curated'``, ``status =
  'curated'``, evidence) - the resolved formula then reports ``status = curated`` instead of
  ``inferred``;
* ``call`` / ``offer`` -> ``ref.bond_offer`` (``source = 'curated'``, ``offer_type_basis =
  'document'``, status ``scheduled`` / ``past`` by the date);
* ``replacement_bond`` is read by the static snapshot builder (basis ``document``).

Rows of this file that disappear from it are soft-deleted (``is_deleted``), never removed;
rows of other writers with ``source = 'curated'`` (the legacy mapping) are not touched.
Entries whose SECID is unknown are skipped (run ``partial``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib.resources import files
from typing import Any, Final, LiteralString

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from mdhub.core.upsert import upsert_rows
from mdhub.jobs import JobContext, job

__all__ = [
    "TERMS",
    "TERMS_RESOURCE",
    "CuratedTerm",
    "bond_terms_curated",
    "load_terms",
    "packaged_terms",
]

TERMS_RESOURCE: Final = "bond_terms_curated.json"
TERMS: Final = frozenset({"floater_formula", "call", "offer", "replacement_bond"})
EVIDENCE_REQUIRED: Final = ("document_url", "document_title", "extracted_text", "retrieved_on")
FORMULA_DECIMALS: Final = ("spread_bp", "multiplier", "floor_pct", "cap_pct")
FORMULA_INTS: Final = ("lag_days", "fixing_day")
FORMULA_TEXT: Final = ("benchmark", "benchmark_raw", "averaging", "variable_rate_type")
OFFER_TYPES: Final = frozenset({"call", "put"})


@dataclass(frozen=True, slots=True)
class CuratedTerm:
    secid: str
    isin: str | None
    term: str
    term_key: str
    value: Mapping[str, Any]
    evidence: Mapping[str, Any]


def _dec(v: Any, where: str) -> Decimal | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{where}: decimals are strings, got {v!r}")
    try:
        return Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{where}: not a decimal: {v!r}") from None


def _day(v: Any, where: str) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{where}: not an ISO date: {v!r}") from None


def _check_value(term: str, value: Mapping[str, Any], where: str) -> str:
    """Validate ``value`` of ``term``; returns the term key (date of a call / offer)."""
    if term == "floater_formula":
        allowed = {*FORMULA_DECIMALS, *FORMULA_INTS, *FORMULA_TEXT}
        if set(value) - allowed:
            raise ValueError(f"{where}: unknown formula fields {sorted(set(value) - allowed)}")
        if not value.get("benchmark"):
            raise ValueError(f"{where}: formula without benchmark")
        for k in FORMULA_DECIMALS:
            _dec(value.get(k), f"{where}.{k}")
        for k in FORMULA_INTS:
            if value.get(k) is not None and not isinstance(value[k], int):
                raise ValueError(f"{where}.{k}: not an integer")
        return ""
    if term in ("call", "offer"):
        d = _day(value.get("date"), f"{where}.date")
        if d is None:
            raise ValueError(f"{where}: {term} without date")
        kind = value.get("type", "call" if term == "call" else None)
        if kind not in OFFER_TYPES or (term == "call" and kind != "call"):
            raise ValueError(f"{where}: bad {term} type {kind!r}")
        _dec(value.get("price_pct"), f"{where}.price_pct")
        _day(value.get("window_start"), f"{where}.window_start")
        _day(value.get("window_end"), f"{where}.window_end")
        return d.isoformat()
    if term == "replacement_bond":
        if not isinstance(value.get("replacement_bond"), bool):
            raise ValueError(f"{where}: replacement_bond must be true/false")
        return ""
    raise ValueError(f"{where}: unknown term {term!r} (known: {sorted(TERMS)})")


def load_terms(doc: Mapping[str, Any]) -> tuple[str, list[CuratedTerm]]:
    """Validate the curated file; ``(version, entries)``. Raises ``ValueError``."""
    version = str(doc["version"])
    out: dict[tuple[str, str, str], CuratedTerm] = {}
    for n, e in enumerate(doc["entries"]):
        where = f"bond_terms_curated entry {n}"
        secid = str(e.get("secid") or "")
        term = str(e.get("term") or "")
        if not secid:
            raise ValueError(f"{where}: secid missing")
        value = e.get("value")
        if not isinstance(value, Mapping):
            raise ValueError(f"{where}: value must be an object")
        evidence = e.get("evidence")
        if not isinstance(evidence, Mapping) or any(not evidence.get(k) for k in EVIDENCE_REQUIRED):
            raise ValueError(f"{where} ({secid}): evidence needs {list(EVIDENCE_REQUIRED)}")
        _day(evidence["retrieved_on"], f"{where}.evidence.retrieved_on")
        key = _check_value(term, value, f"{where} ({secid} {term})")
        if (secid, term, key) in out:
            raise ValueError(f"{where}: duplicate {secid} {term} {key}")
        out[(secid, term, key)] = CuratedTerm(
            secid=secid,
            isin=str(e["isin"]) if e.get("isin") else None,
            term=term,
            term_key=key,
            value=dict(value),
            evidence=dict(evidence),
        )
    return version, [out[k] for k in sorted(out)]


@cache
def packaged_terms() -> tuple[str, list[CuratedTerm]]:
    doc = json.loads(files("mdhub.data").joinpath(TERMS_RESOURCE).read_text("utf-8"))
    return load_terms(doc)


def _evidence(t: CuratedTerm, version: str) -> Jsonb:
    return Jsonb({"file": TERMS_RESOURCE, "version": version, **t.evidence})


def _formula_row(t: CuratedTerm, version: str, run_id: int) -> dict[str, Any]:
    v = t.value
    return {
        "secid": t.secid,
        "source": "curated",
        "benchmark": v.get("benchmark"),
        "benchmark_raw": v.get("benchmark_raw"),
        "spread_bp": _dec(v.get("spread_bp"), "spread_bp"),
        "multiplier": _dec(v.get("multiplier"), "multiplier"),
        "lag_days": v.get("lag_days"),
        "averaging": v.get("averaging"),
        "floor_pct": _dec(v.get("floor_pct"), "floor_pct"),
        "cap_pct": _dec(v.get("cap_pct"), "cap_pct"),
        "variable_rate_type": v.get("variable_rate_type"),
        "fixing_day": v.get("fixing_day"),
        "status": "curated",
        "evidence": _evidence(t, version),
        "is_deleted": False,
        "run_id": run_id,
    }


def _offer_row(t: CuratedTerm, version: str, today: date, run_id: int) -> dict[str, Any]:
    v = t.value
    d = date.fromisoformat(t.term_key)
    return {
        "secid": t.secid,
        "source": "curated",
        "offer_key": t.term_key,
        "offer_date": d,
        "date_missing": False,
        "offer_type": v.get("type", "call"),
        "offer_type_basis": "document",
        "offer_type_raw": t.evidence.get("document_title"),
        "status": "scheduled" if d >= today else "past",
        "is_offer_or_redemption": False,
        "offer_date_start": _day(v.get("window_start"), "window_start"),
        "offer_date_end": _day(v.get("window_end"), "window_end"),
        "price": _dec(v.get("price_pct"), "price_pct"),
        "is_deleted": False,
        "run_id": run_id,
    }


def _soft_delete(
    conn: psycopg.Connection[Any],
    table: tuple[str, str],
    keep: list[tuple[str, ...]],
    key: tuple[str, ...],
    where: LiteralString,
) -> int:
    """Soft-delete this file's rows of ``table`` (rows matching ``where``) not in ``keep``."""
    tbl = sql.Identifier(*table)
    cols = sql.SQL(", ").join(map(sql.Identifier, key))
    rows = conn.execute(
        sql.SQL("SELECT {} FROM {} WHERE NOT is_deleted AND ").format(cols, tbl) + sql.SQL(where)
    ).fetchall()
    keep_set = set(keep)
    gone = [tuple(r[k] for k in key) for r in rows if tuple(r[k] for k in key) not in keep_set]
    match = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key)
    for g in gone:
        conn.execute(
            sql.SQL("UPDATE {} SET is_deleted = true, updated_at = now() WHERE {}").format(
                tbl, match
            ),
            g,
        )
    return len(gone)


@job("bond_terms_curated", "Issue terms curated from issue documents (with evidence)")
def bond_terms_curated(ctx: JobContext) -> dict[str, Any]:
    """Apply ``mdhub/data/bond_terms_curated.json`` (see module docstring)."""
    version, terms = packaged_terms()
    run_id = ctx.run.id
    today = ctx.today()
    with ctx.connection() as conn:
        known = {
            r["secid"]: r["isin"]
            for r in conn.execute(
                "SELECT secid, isin FROM ref.bond WHERE secid = ANY(%s)",
                (sorted({t.secid for t in terms}),),
            ).fetchall()
        }
        skipped = [t for t in terms if t.secid not in known]
        mismatched = [t for t in terms if t.secid in known and t.isin and t.isin != known[t.secid]]
        if mismatched:
            raise ValueError(
                "bond_terms_curated: ISIN does not match ref.bond for "
                + ", ".join(f"{t.secid} ({t.isin} vs {known[t.secid]})" for t in mismatched)
            )
        use = [t for t in terms if t.secid in known]
        term_rows = [
            {
                "secid": t.secid,
                "term": t.term,
                "term_key": t.term_key,
                "value": Jsonb(dict(t.value)),
                "evidence": Jsonb(dict(t.evidence)),
                "file_version": version,
                "is_deleted": False,
                "run_id": run_id,
            }
            for t in use
        ]
        formulas = [_formula_row(t, version, run_id) for t in use if t.term == "floater_formula"]
        offers = [_offer_row(t, version, today, run_id) for t in use if t.term in ("call", "offer")]
        ctx.run.add_counts(
            upsert_rows(
                conn,
                "ref.bond_curated_term",
                term_rows,
                ["secid", "term", "term_key"],
                run_id=run_id,
            )
        )
        ctx.run.add_counts(
            upsert_rows(conn, "ref.bond_rate_formula", formulas, ["secid", "source"], run_id=run_id)
        )
        ctx.run.add_counts(
            upsert_rows(
                conn, "ref.bond_offer", offers, ["secid", "source", "offer_key"], run_id=run_id
            )
        )
        deleted = (
            _soft_delete(
                conn,
                ("ref", "bond_curated_term"),
                [(t.secid, t.term, t.term_key) for t in use],
                ("secid", "term", "term_key"),
                "true",
            )
            + _soft_delete(
                conn,
                ("ref", "bond_rate_formula"),
                [(r["secid"], "curated") for r in formulas],
                ("secid", "source"),
                "source = 'curated' AND evidence ->> 'file' = 'bond_terms_curated.json'",
            )
            + _soft_delete(
                conn,
                ("ref", "bond_offer"),
                [(r["secid"], "curated", r["offer_key"]) for r in offers],
                ("secid", "source", "offer_key"),
                "source = 'curated' AND offer_type_basis = 'document'",
            )
        )
    if skipped:
        ctx.run.mark_partial(
            "unknown SECIDs in bond_terms_curated.json: "
            + ", ".join(sorted({t.secid for t in skipped}))[:300]
        )
    return {
        "version": version,
        "entries": len(terms),
        "applied": len(use),
        "skipped_unknown_secid": len(skipped),
        "formulas": len(formulas),
        "offers": len(offers),
        "soft_deleted": deleted,
    }
