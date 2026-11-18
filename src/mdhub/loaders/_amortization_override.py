"""Manual redemption amounts (market-data-hub#165): the part of job ``bond_terms_curated`` that
applies ``mdhub/data/bond_amortization_overrides.json``.

MOEX ISS rounds the principal schedule (``value`` to 0.01, ``valueprc`` to whole percent), so
the future redemptions of a low-face bond do not add up to its face and the yields MOEX derives
from them are wrong. There is no source with the exact future amounts, so a person confirms them
and they enter through this reviewed, versioned file: one entry per bond, with the amounts, the
face currency, a note on where they come from, who confirmed them and when. :func:`load_overrides`
rejects a bad entry (fail closed: nothing of the file is applied); :func:`apply_overrides`
writes ``ref.bond_amortization_override`` for the SECIDs known to ``ref.bond`` (an ``isin`` in
the entry must match; an entry for a SECID the database does not hold is skipped with a warning,
unless its ISIN belongs to another SECID: that is a typo), soft-deletes the rows that left the
file and reports every override that does not agree with what MOEX publishes now
(:func:`mdhub.snapshot.redemptions.plan_schedule`: same dates, amounts within rounding, same
currency, sum exactly equal to the face). The snapshot builder
and the TDM export make the same check and do not apply a divergent override; the report here
is the early warning (run ``partial``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib.resources import files
from typing import Any, Final

import psycopg

from mdhub.core.upsert import UpsertResult, upsert_rows
from mdhub.snapshot.redemptions import plan_schedule

__all__ = [
    "OVERRIDES_RESOURCE",
    "AmortizationOverride",
    "ApplyResult",
    "apply_overrides",
    "load_overrides",
    "packaged_overrides",
]

OVERRIDES_RESOURCE: Final = "bond_amortization_overrides.json"


@dataclass(frozen=True, slots=True)
class AmortizationOverride:
    secid: str
    isin: str | None
    face_unit: str
    redemptions: tuple[tuple[date, Decimal], ...]
    source_note: str
    confirmed_by: str
    confirmed_on: date


@dataclass(frozen=True, slots=True)
class ApplyResult:
    counts: UpsertResult
    applied: int
    skipped_unknown_secid: list[str]
    """Entries whose SECID is not in ``ref.bond`` (a development seed, a fresh install, a bond
    MOEX no longer lists): nothing to correct, so only a warning."""
    secid_typo: dict[str, str]
    """Entries whose SECID is unknown but whose ISIN belongs to another SECID of ``ref.bond``
    (entry SECID -> the SECID that has the ISIN): a mistake in the file, the job is ``partial``."""
    soft_deleted: int
    stale: dict[str, str]
    """SECID -> why the override disagrees with MOEX now (not applied by the builders)."""


def _text(e: Mapping[str, Any], key: str, where: str) -> str:
    v = e.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where}: {key} must be a non-empty string")
    return v.strip()


def _day(v: Any, where: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ValueError(f"{where}: not an ISO date: {v!r}") from None


def _amount(v: Any, where: str) -> Decimal:
    if not isinstance(v, str):
        raise ValueError(f"{where}: decimals are strings, got {v!r}")
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"{where}: not a decimal: {v!r}") from None
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{where}: an amount must be positive, got {v!r}")
    return d


def _check_isin(isin: str, where: str) -> str:
    """The ISIN as given, if it is 12 characters with a valid check digit (ISO 6166, Luhn)."""
    if len(isin) != 12 or not isin.isascii() or not isin.isalnum() or isin != isin.upper():
        raise ValueError(f"{where}: not an ISIN: {isin!r}")
    digits = "".join(str(int(c, 36)) for c in isin)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    if total % 10:
        raise ValueError(f"{where}: ISIN check digit is wrong: {isin!r}")
    return isin


def load_overrides(doc: Mapping[str, Any]) -> tuple[str, list[AmortizationOverride]]:
    """Validate the overrides file; ``(version, entries)``. Raises ``ValueError``."""
    version = str(doc["version"])
    out: dict[str, AmortizationOverride] = {}
    for n, e in enumerate(doc["entries"]):
        where = f"bond_amortization_overrides entry {n}"
        secid = _text(e, "secid", where)
        where = f"{where} ({secid})"
        if secid in out:
            raise ValueError(f"{where}: duplicate secid")
        rows = e.get("redemptions")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{where}: redemptions must be a non-empty list")
        by_date: dict[date, Decimal] = {}
        for i, r in enumerate(rows):
            if not isinstance(r, Mapping) or set(r) != {"date", "value"}:
                raise ValueError(f"{where}: redemption {i} must be {{date, value}}")
            d = _day(r["date"], f"{where}: redemption {i}.date")
            if d in by_date:
                raise ValueError(f"{where}: duplicate redemption date {d.isoformat()}")
            by_date[d] = _amount(r["value"], f"{where}: redemption {i}.value")
        unit = _text(e, "face_unit", where).upper()
        out[secid] = AmortizationOverride(
            secid=secid,
            isin=_check_isin(str(e["isin"]), f"{where}.isin") if e.get("isin") else None,
            face_unit=unit,
            redemptions=tuple(sorted(by_date.items())),
            source_note=_text(e, "source_note", where),
            confirmed_by=_text(e, "confirmed_by", where),
            confirmed_on=_day(e.get("confirmed_on"), f"{where}.confirmed_on"),
        )
    return version, [out[k] for k in sorted(out)]


@cache
def packaged_overrides() -> tuple[str, list[AmortizationOverride]]:
    doc = json.loads(files("mdhub.data").joinpath(OVERRIDES_RESOURCE).read_text("utf-8"))
    return load_overrides(doc)


def apply_overrides(
    conn: psycopg.Connection[Any],
    version: str,
    entries: list[AmortizationOverride],
    *,
    run_id: int,
    today: date,
) -> ApplyResult:
    """Write ``entries`` to ``ref.bond_amortization_override`` and check them against MOEX."""
    bonds = {
        r["secid"]: r
        for r in conn.execute(
            "SELECT * FROM ref.bond WHERE secid = ANY(%s)", (sorted(e.secid for e in entries),)
        ).fetchall()
    }
    skipped = sorted(e.secid for e in entries if e.secid not in bonds)
    typo: dict[str, str] = {}
    by_isin = {e.isin: e.secid for e in entries if e.secid not in bonds and e.isin}
    if by_isin:
        for r in conn.execute(
            "SELECT secid, isin FROM ref.bond WHERE isin = ANY(%s) ORDER BY secid",
            (sorted(by_isin),),
        ).fetchall():
            typo.setdefault(by_isin[r["isin"]], r["secid"])
    mismatched = [
        e for e in entries if e.secid in bonds and e.isin and e.isin != bonds[e.secid]["isin"]
    ]
    if mismatched:
        raise ValueError(
            "bond_amortization_overrides: ISIN does not match ref.bond for "
            + ", ".join(f"{e.secid} ({e.isin} vs {bonds[e.secid]['isin']})" for e in mismatched)
        )
    use = [e for e in entries if e.secid in bonds]
    rows = [
        {
            "secid": e.secid,
            "amort_date": d,
            "value": v,
            "face_unit": e.face_unit,
            "source_note": e.source_note,
            "confirmed_by": e.confirmed_by,
            "confirmed_on": e.confirmed_on,
            "file_version": version,
            "is_deleted": False,
            "run_id": run_id,
        }
        for e in use
        for d, v in e.redemptions
    ]
    counts = upsert_rows(
        conn, "ref.bond_amortization_override", rows, ["secid", "amort_date"], run_id=run_id
    )
    keep = {(r["secid"], r["amort_date"]) for r in rows}
    gone = [
        (r["secid"], r["amort_date"])
        for r in conn.execute(
            "SELECT secid, amort_date FROM ref.bond_amortization_override WHERE NOT is_deleted"
        ).fetchall()
        if (r["secid"], r["amort_date"]) not in keep
    ]
    for g in gone:
        conn.execute(
            "UPDATE ref.bond_amortization_override SET is_deleted = true, updated_at = now() "
            "WHERE secid = %s AND amort_date = %s",
            g,
        )
    return ApplyResult(
        counts,
        len(use),
        skipped,
        dict(sorted(typo.items())),
        len(gone),
        _stale(conn, bonds, [e.secid for e in use], today),
    )


def _stale(
    conn: psycopg.Connection[Any], bonds: Mapping[str, Any], secids: list[str], today: date
) -> dict[str, str]:
    """Overrides that the builders will not apply today, with the reason."""
    if not secids:
        return {}
    moex: dict[str, list[Any]] = {s: [] for s in secids}
    for r in conn.execute(
        "SELECT * FROM ref.bond_amortization WHERE NOT is_deleted AND secid = ANY(%s) "
        "ORDER BY secid, amort_date, data_source",
        (secids,),
    ).fetchall():
        moex[r["secid"]].append(r)
    mine: dict[str, list[Any]] = {s: [] for s in secids}
    for r in conn.execute(
        "SELECT * FROM ref.bond_amortization_override WHERE NOT is_deleted AND secid = ANY(%s) "
        "ORDER BY secid, amort_date",
        (secids,),
    ).fetchall():
        mine[r["secid"]].append(r)
    out: dict[str, str] = {}
    for s in secids:
        plan = plan_schedule(bonds[s], moex[s], mine[s], today)
        if plan.override == "stale" and plan.stale_reason:
            out[s] = plan.stale_reason
    return out
