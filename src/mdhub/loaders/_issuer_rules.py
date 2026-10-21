"""Issuer sector rules (WEB-13, TDM brief R9 HS3): pure functions over the versioned package
file ``mdhub/data/sector_map.json`` (no DB access). See the file's ``description`` for the
precedence; :func:`assign_sector` implements it. A sector is never guessed: no matching rule
means ``None``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any, Final

__all__ = [
    "GROUPS_RESOURCE",
    "GROUP_RELATIONS",
    "SECTOR_RESOURCE",
    "GroupAssignment",
    "SectorMap",
    "SectorResult",
    "agency_sector",
    "assign_sector",
    "groups_version",
    "issuer_groups",
    "load_groups",
    "sector_map",
]

SECTOR_RESOURCE: Final = "sector_map.json"


@dataclass(frozen=True, slots=True)
class SectorResult:
    sector: str
    source: str
    """``curated`` | ``group`` | ``okved`` | ``rule`` | ``agency``
    (``ref.issuer.sector_source``)."""
    basis: str
    """Human-readable reason, e.g. ``name_rule:banks`` or ``class:subfederal``."""


@dataclass(frozen=True, slots=True)
class SectorMap:
    version: str
    codes: frozenset[str]
    issuers: Mapping[str, str]
    okved_prefixes: tuple[tuple[str, str], ...]
    class_rules: tuple[tuple[frozenset[str], str, str], ...]
    name_rules: tuple[tuple[re.Pattern[str], str], ...]
    agency_rules: tuple[tuple[str, re.Pattern[str], str], ...] = ()
    """``(agency, pattern, sector)`` on the industry an agency publishes (WEB-18)."""


def _load(doc: Mapping[str, Any]) -> SectorMap:
    codes = frozenset(str(t["code"]) for t in doc["taxonomy"])

    def check(sector: str, where: str) -> str:
        if sector not in codes:
            raise ValueError(f"sector_map: {where}: sector {sector!r} not in the taxonomy")
        return sector

    issuers = {
        str(inn): check(str(v["sector"]), f"issuer {inn}") for inn, v in doc["issuers"].items()
    }
    okved = tuple(
        sorted(
            ((str(r["prefix"]), check(str(r["sector"]), f"okved {r['prefix']}"))
             for r in doc["okved_prefixes"]),
            key=lambda t: -len(t[0]),
        )
    )  # fmt: skip
    classes = []
    for r in doc["class_rules"]:
        when = str(r["when"])
        if when not in ("any", "all"):
            raise ValueError(f"sector_map: class rule when={when!r}")
        classes.append((frozenset(r["bond_classes"]), check(str(r["sector"]), "class"), when))
    names = tuple(
        (re.compile(str(r["pattern"]), re.IGNORECASE), check(str(r["sector"]), "name rule"))
        for r in doc["name_rules"]
    )
    agency = tuple(
        (str(r["agency"]), re.compile(str(r["pattern"]), re.IGNORECASE),
         check(str(r["sector"]), f"agency rule {r['agency']}"))
        for r in doc.get("agency_industry_rules", ())
    )  # fmt: skip
    return SectorMap(str(doc["version"]), codes, issuers, okved, tuple(classes), names, agency)


def agency_sector(industries: Iterable[tuple[str, str]], m: SectorMap) -> tuple[str, str] | None:
    """``(sector, basis)`` from the agencies' published industries ``(agency, industry)``:
    per agency the first matching rule; ``None`` without a match or when agencies disagree."""
    hits: dict[str, tuple[str, str]] = {}
    for agency, industry in sorted(set(industries)):
        if agency in hits:
            continue
        for rule_agency, pattern, sector in m.agency_rules:
            if rule_agency == agency and pattern.search(industry):
                hits[agency] = (sector, f"agency:{agency}:{industry}")
                break
    sectors = {sector for sector, _ in hits.values()}
    if len(sectors) != 1:
        return None
    return min(hits.values(), key=lambda t: t[1])


@cache
def sector_map() -> SectorMap:
    """The packaged, validated sector map (every sector is a taxonomy code)."""
    return _load(json.loads(files("mdhub.data").joinpath(SECTOR_RESOURCE).read_text("utf-8")))


def assign_sector(
    *,
    inn: str | None,
    name: str | None,
    short_name: str | None,
    bond_classes: Iterable[str],
    okved: str | None = None,
    group_sector: str | None = None,
    group_relation: str | None = None,
    agency_industries: Iterable[tuple[str, str]] = (),
    smap: SectorMap | None = None,
) -> SectorResult | None:
    """Sector of one issuer by the precedence of ``sector_map.json`` (``None``: no rule)."""
    m = smap or sector_map()
    if inn and inn in m.issuers:
        return SectorResult(m.issuers[inn], "curated", f"inn:{inn}")
    if group_sector is not None and group_relation in ("spv", "securitization_spv"):
        sector = "securitization_spv" if group_relation == "securitization_spv" else group_sector
        return SectorResult(sector, "group", f"group_{group_relation}")
    if okved:
        for prefix, sector in m.okved_prefixes:
            if okved.startswith(prefix):
                return SectorResult(sector, "okved", f"okved:{prefix}")
    classes = frozenset(bond_classes)
    if classes:
        for rule_classes, sector, when in m.class_rules:
            hit = classes <= rule_classes if when == "all" else bool(classes & rule_classes)
            if hit:
                return SectorResult(sector, "rule", f"class:{sector}")
    by_agency = agency_sector(agency_industries, m)
    if by_agency is not None:
        return SectorResult(by_agency[0], "agency", by_agency[1])
    text = " | ".join(t for t in (name, short_name) if t)
    if text:
        for pattern, sector in m.name_rules:
            if pattern.search(text):
                return SectorResult(sector, "rule", f"name_rule:{sector}")
    if group_sector is not None:
        return SectorResult(group_sector, "group", f"group_{group_relation}")
    return None


# ------------------------------------------------------------------------ issuer groups (HS4)

GROUPS_RESOURCE: Final = "issuer_groups_curated.json"
GROUP_RELATIONS: Final = frozenset(
    {"parent", "subsidiary", "spv", "securitization_spv", "affiliate"}
)


@dataclass(frozen=True, slots=True)
class GroupAssignment:
    inn: str
    emitter_id: int | None
    group_id: str
    group_name: str
    group_relation: str
    is_state_owned: bool | None
    sector: str | None
    evidence: Mapping[str, Any]


def load_groups(
    doc: Mapping[str, Any], smap: SectorMap | None = None
) -> dict[str, GroupAssignment]:
    """Validate the curated group file: every row names a known group and relation and carries
    evidence; group sectors are taxonomy codes."""
    m = smap or sector_map()
    version = str(doc["version"])
    groups = doc["groups"]
    out: dict[str, GroupAssignment] = {}
    for gid, g in groups.items():
        if g.get("sector") is not None and g["sector"] not in m.codes:
            raise ValueError(f"issuer groups: group {gid}: sector {g['sector']!r} not in taxonomy")
    for inn, row in doc["issuers"].items():
        gid = str(row["group_id"])
        if gid not in groups:
            raise ValueError(f"issuer groups: INN {inn}: unknown group {gid!r}")
        rel = str(row["group_relation"])
        if rel not in GROUP_RELATIONS:
            raise ValueError(f"issuer groups: INN {inn}: bad relation {rel!r}")
        evidence = row.get("evidence")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValueError(f"issuer groups: INN {inn}: evidence missing")
        g = groups[gid]
        out[str(inn)] = GroupAssignment(
            inn=str(inn),
            emitter_id=int(row["emitter_id"]) if row.get("emitter_id") is not None else None,
            group_id=gid,
            group_name=str(g["group_name"]),
            group_relation=rel,
            is_state_owned=g.get("is_state_owned"),
            sector=g.get("sector"),
            evidence={"file": GROUPS_RESOURCE, "version": version, **evidence},
        )
    return out


@cache
def issuer_groups() -> dict[str, GroupAssignment]:
    """The packaged curated issuer groups, by INN."""
    return load_groups(json.loads(files("mdhub.data").joinpath(GROUPS_RESOURCE).read_text("utf-8")))


def groups_version() -> str:
    doc = json.loads(files("mdhub.data").joinpath(GROUPS_RESOURCE).read_text("utf-8"))
    return str(doc["version"])
