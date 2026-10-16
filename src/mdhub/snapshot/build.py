"""Shared model of a snapshot build: the cut, datasets, provenance, exclusions, document.

A builder (:mod:`mdhub.snapshot.static`, :mod:`mdhub.snapshot.market`) reads the hub tables
**as known at** ``known_at`` and returns a :class:`Build`; :mod:`mdhub.snapshot.publish`
turns it into the canonical bundle document, computes the ID and stores it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from mdhub import __version__
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex, to_wire, utc_str
from mdhub.snapshot.schema import SCHEMA_VERSION

__all__ = [
    "CUT_TYPES",
    "Build",
    "Cut",
    "Dataset",
    "Excluded",
    "Kind",
    "Provenance",
    "json_decimal",
    "sorted_labels",
]

Kind = Literal["static", "market"]

CUT_TYPES: Final[dict[str, tuple[str, ...]]] = {
    "static": ("daily", "change"),
    "market": ("eod_preliminary", "eod_final", "intraday"),
}
PRODUCER: Final = "market-data-hub"


@dataclass(frozen=True, slots=True)
class Cut:
    """Identity and clock of a snapshot. ``(kind, cut_type, business_date, cut_ts)`` is the
    cut; a correction of a cut supersedes the earlier snapshot of the same cut."""

    kind: Kind
    cut_type: str
    business_date: date
    cut_ts: datetime
    known_at: datetime

    def __post_init__(self) -> None:
        if self.cut_type not in CUT_TYPES[self.kind]:
            raise ValueError(f"cut_type {self.cut_type!r} is not valid for kind {self.kind!r}")
        for name in ("cut_ts", "known_at"):
            if getattr(self, name).utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(slots=True)
class Dataset:
    """One ``data.<name>`` array plus its manifest entry facts."""

    name: str
    rows: list[dict[str, Any]]
    coverage: dict[str, Any] = field(default_factory=dict)
    stale_reasons: list[str] = field(default_factory=list)

    def wire_rows(self) -> list[Any]:
        return [to_wire(r) for r in self.rows]

    def entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rows": len(self.rows),
            "sha256": sha256_hex(canonical_bytes(self.rows)),
            "coverage": to_wire(self.coverage),
            "stale": bool(self.stale_reasons),
            "stale_reasons": sorted(set(self.stale_reasons)),
        }


@dataclass(slots=True)
class Excluded:
    """Records left out, with reasons (never dropped silently)."""

    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, dataset: str, key: Mapping[str, Any], reason: str) -> None:
        self.items.append({"dataset": dataset, "key": to_wire(dict(key)), "reason": reason})

    def wire(self) -> list[dict[str, Any]]:
        def sort_key(e: dict[str, Any]) -> tuple[str, str, bytes]:
            return (e["dataset"], e["reason"], canonical_bytes(e["key"]))

        return sorted(self.items, key=sort_key)


@dataclass(slots=True)
class Provenance:
    """Per (dataset, source): run IDs of the included rows and their version-time range."""

    _runs: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    _times: dict[tuple[str, str], list[datetime]] = field(default_factory=dict)

    def add(
        self, dataset: str, source: str | None, run_id: int | None, at: datetime | None
    ) -> None:
        key = (dataset, source or "unknown")
        runs = self._runs.setdefault(key, set())
        if run_id is not None:
            runs.add(int(run_id))
        times = self._times.setdefault(key, [])
        if at is not None:
            if not times:
                times.extend([at, at])
            else:
                times[0] = min(times[0], at)
                times[1] = max(times[1], at)

    def add_rows(
        self,
        dataset: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        source: str | None = None,
        source_col: str = "source",
    ) -> None:
        for r in rows:
            at = r.get("updated_at") or r.get("ingested_at")
            self.add(dataset, source or r.get(source_col), r.get("run_id"), at)

    def wire(self) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self._runs):
            times = self._times.get(key)
            first, last = (times[0], times[1]) if times else (None, None)
            out.append(
                {
                    "dataset": key[0],
                    "source": key[1],
                    "run_ids": sorted(self._runs[key]),
                    "ingested_from": utc_str(first),
                    "ingested_to": utc_str(last),
                }
            )
        return out


@dataclass(slots=True)
class Build:
    """Everything needed to render one snapshot document."""

    cut: Cut
    datasets: list[Dataset]
    calendar: dict[str, Any]
    parameters: dict[str, Any]
    provenance: Provenance
    excluded: Excluded
    static_snapshot_id: str | None = None
    universe: frozenset[str] = frozenset()
    """SECIDs of the static universe (static builds; used to filter market quotes)."""

    def data(self) -> dict[str, list[Any]]:
        return {d.name: d.rows for d in self.datasets}

    def dataset(self, name: str) -> Dataset:
        for d in self.datasets:
            if d.name == name:
                return d
        raise KeyError(name)

    def content_sha256(self) -> str:
        """Hash of the data (and the static reference): equal content = nothing to publish."""
        return sha256_hex(
            canonical_bytes({"data": self.data(), "static_snapshot_id": self.static_snapshot_id})
        )

    def envelope(self, supersedes: str | None) -> dict[str, Any]:
        """The manifest fields (wire form), without ``snapshot_id`` and ``data``."""
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": self.cut.kind,
            "cut_type": self.cut.cut_type,
            "business_date": self.cut.business_date.isoformat(),
            "cut_ts": utc_str(self.cut.cut_ts),
            "known_at": utc_str(self.cut.known_at),
            "static_snapshot_id": self.static_snapshot_id,
            "supersedes": supersedes,
            "producer": {"name": PRODUCER, "version": __version__},
            "parameters": to_wire(self.parameters),
            "calendar": to_wire(self.calendar),
            "sources": self.provenance.wire(),
            "datasets": [d.entry() for d in self.datasets],
            "excluded": self.excluded.wire(),
        }

    def document(self, supersedes: str | None) -> dict[str, Any]:
        """The bundle document (wire form): envelope plus data."""
        doc = self.envelope(supersedes)
        doc["data"] = {d.name: d.wire_rows() for d in self.datasets}
        return doc


def sorted_labels(*labels: str | None) -> list[str]:
    return sorted({lbl for lbl in labels if lbl})


def json_decimal(text: str | None) -> Any:
    """Parse JSON text from a ``jsonb::text`` column with every number as :class:`Decimal`."""
    if text is None:
        return None
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)
