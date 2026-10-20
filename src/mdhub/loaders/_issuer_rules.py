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
    "SECTOR_RESOURCE",
    "SectorMap",
    "SectorResult",
    "assign_sector",
    "sector_map",
]

SECTOR_RESOURCE: Final = "sector_map.json"


@dataclass(frozen=True, slots=True)
class SectorResult:
    sector: str
    source: str
    """``curated`` | ``group`` | ``okved`` | ``rule`` (``ref.issuer.sector_source``)."""
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
    return SectorMap(str(doc["version"]), codes, issuers, okved, tuple(classes), names)


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
    text = " | ".join(t for t in (name, short_name) if t)
    if text:
        for pattern, sector in m.name_rules:
            if pattern.search(text):
                return SectorResult(sector, "rule", f"name_rule:{sector}")
    if group_sector is not None:
        return SectorResult(group_sector, "group", f"group_{group_relation}")
    return None
