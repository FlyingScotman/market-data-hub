"""Bond terms coverage of historical cuts (market-data-hub#101).

For each business date the static linked to its current ``eod_final`` cut is compared with a
reference static (by default the newest one): every bond of the reference that has contract 1.4
floater terms (:data:`TERM_FIELDS`) and is alive on the date should carry the same fields in the
static of that date. A bond is counted under the first reason that applies:

* ``not_in_static`` - the date's static has no such instrument;
* ``no_formula`` - the instrument is there but has no ``floater_formulas`` row;
* per field - the row lacks the field (static older than 1.4) or has it null.

Read only: the snapshots are read from ``ops.published_snapshot``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from mdhub.db.pool import DictConnection
from mdhub.snapshot.canonical import gunzip
from mdhub.snapshot.publish import load_bundle

__all__ = [
    "TERM_FIELDS",
    "DateCoverage",
    "date_coverage",
    "reference_terms",
    "terms_coverage_report",
]

TERM_FIELDS: Final = (
    "benchmark_tenor_years",
    "fixing_anchor",
    "fixing_offset",
    "fixing_offset_unit",
    "rate_rounding_decimals",
    "fixing_basis",
    "spread_schedule",
    "reproduction",
)
"""FloaterFormula fields added by contract ``market_snapshot/1.4`` (static part)."""


@dataclass(frozen=True, slots=True)
class Reference:
    """Bonds of the reference static with 1.4 terms: the fields they have and their life."""

    static_id: str
    terms: Mapping[str, frozenset[str]]
    issue_date: Mapping[str, date | None]
    maturity_date: Mapping[str, date | None]

    def alive_on(self, secid: str, day: date) -> bool:
        issued, matures = self.issue_date.get(secid), self.maturity_date.get(secid)
        return (issued is None or issued <= day) and (matures is None or matures >= day)


@dataclass(slots=True)
class DateCoverage:
    business_date: date
    market_id: str
    static_id: str
    static_schema_version: str
    static_basis: str | None
    bonds: int = 0
    complete: int = 0
    not_in_static: int = 0
    no_formula: int = 0
    missing_by_field: Counter[str] = field(default_factory=Counter)
    missing_secids: list[str] = field(default_factory=list)

    def wire(self) -> dict[str, Any]:
        return {
            "business_date": self.business_date.isoformat(),
            "market_id": self.market_id,
            "static_id": self.static_id,
            "static_schema_version": self.static_schema_version,
            "static_basis": self.static_basis,
            "bonds": self.bonds,
            "complete": self.complete,
            "not_in_static": self.not_in_static,
            "no_formula": self.no_formula,
            "missing_by_field": {f: self.missing_by_field[f] for f in TERM_FIELDS},
            "missing_secids": self.missing_secids,
        }


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def reference_terms(static_id: str, doc: Mapping[str, Any]) -> Reference:
    """The bonds of the static bundle ``doc`` whose floater formula has any 1.4 term."""
    terms = {
        f["secid"]: frozenset(k for k in TERM_FIELDS if f.get(k) is not None)
        for f in doc["data"]["floater_formulas"]
    }
    terms = {s: t for s, t in terms.items() if t}
    instruments = {i["secid"]: i for i in doc["data"]["instruments"] if i["secid"] in terms}
    return Reference(
        static_id,
        terms,
        {s: _date(i.get("issue_date")) for s, i in instruments.items()},
        {s: _date(i.get("maturity_date")) for s, i in instruments.items()},
    )


def date_coverage(
    business_date: date, market_id: str, static_id: str, doc: Mapping[str, Any], ref: Reference
) -> DateCoverage:
    """Coverage of the reference terms in the static bundle ``doc`` of ``business_date``."""
    out = DateCoverage(
        business_date,
        market_id,
        static_id,
        doc["schema_version"],
        doc.get("parameters", {}).get("static_basis"),
    )
    instruments = {i["secid"] for i in doc["data"]["instruments"]}
    formulas = {f["secid"]: f for f in doc["data"]["floater_formulas"]}
    for secid in sorted(ref.terms):
        if not ref.alive_on(secid, business_date):
            continue
        out.bonds += 1
        if secid not in instruments:
            out.not_in_static += 1
        elif secid not in formulas:
            out.no_formula += 1
        else:
            missing = [k for k in sorted(ref.terms[secid]) if formulas[secid].get(k) is None]
            out.missing_by_field.update(missing)
            if not missing:
                out.complete += 1
                continue
        out.missing_secids.append(secid)
    return out


def _bundle(conn: DictConnection, snapshot_id: str) -> dict[str, Any]:
    raw = load_bundle(conn, snapshot_id)
    if raw is None:
        raise LookupError(f"snapshot {snapshot_id} not found")
    doc: dict[str, Any] = json.loads(gunzip(raw))
    return doc


def terms_coverage_report(
    conn: DictConnection, date_from: date, date_to: date, *, reference_id: str | None = None
) -> tuple[Reference, list[DateCoverage]]:
    """Per business date with a current ``eod_final`` cut in ``[date_from, date_to]``: the
    coverage of the reference terms (``reference_id``, default the newest static snapshot) in
    the static that cut links."""
    if reference_id is None:
        row = conn.execute(
            "SELECT id FROM ops.published_snapshot WHERE kind = 'static' "
            "ORDER BY known_at DESC, published_at DESC, id LIMIT 1"
        ).fetchone()
        if row is None:
            raise LookupError("no static snapshot published")
        reference_id = row["id"]
    ref = reference_terms(reference_id, _bundle(conn, reference_id))
    cuts = conn.execute(
        """
        SELECT p.business_date, p.id, p.static_snapshot_id FROM ops.published_snapshot p
        WHERE p.kind = 'market' AND p.cut_type = 'eod_final'
          AND p.business_date BETWEEN %s AND %s
          AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
        ORDER BY p.business_date, p.cut_ts, p.id
        """,
        (date_from, date_to),
    ).fetchall()
    out = []
    sid, doc = "", {}  # consecutive dates mostly share a static: keep one bundle in memory
    for c in cuts:
        if c["static_snapshot_id"] != sid:
            sid, doc = c["static_snapshot_id"], _bundle(conn, c["static_snapshot_id"])
        out.append(date_coverage(c["business_date"], c["id"], sid, doc, ref))
    return ref, out
