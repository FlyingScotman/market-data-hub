"""JSON Schema of the Market Snapshot Contract v1 (``schemas/market_snapshot/v1``).

The schema files live at the repository root (the hub owns the contract); wheels carry a copy
at ``mdhub/schemas`` (``tool.hatch.build.targets.wheel.force-include``). :func:`schema_dir`
finds either. Validation uses JSON Schema draft 2020-12 with a local registry (no network).
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from mdhub.core.errors import MdhubError

__all__ = [
    "SCHEMA_VERSION",
    "SnapshotSchemaError",
    "schema_dir",
    "validate_bundle",
    "validate_manifest",
    "validator",
]

SCHEMA_VERSION: Final = "market_snapshot/1.0"
"""Value of ``schema_version`` written by this hub release."""

BASE_URI: Final = "https://schemas.market-data-hub.invalid/market_snapshot/v1/"
_RELATIVE: Final = Path("market_snapshot") / "v1"


class SnapshotSchemaError(MdhubError, ValueError):
    """A snapshot document does not conform to the contract schema."""


def schema_dir() -> Path:
    """Directory holding ``*.schema.json`` (packaged copy first, then the source tree)."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "schemas" / _RELATIVE,  # wheel: mdhub/schemas/...
        here.parents[3] / "schemas" / _RELATIVE,  # source tree: <repo>/schemas/...
    ]
    for path in candidates:
        if (path / "snapshot.schema.json").is_file():
            return path
    raise FileNotFoundError(
        "Market Snapshot Contract schema not found (looked in "
        + ", ".join(str(c) for c in candidates)
        + ")"
    )


@cache
def _registry() -> Registry[Any]:
    resources = []
    for path in sorted(schema_dir().glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resources.append((str(doc["$id"]), Resource.from_contents(doc)))
    registry: Registry[Any] = Registry().with_resources(resources)
    return registry


@cache
def validator(name: str = "snapshot") -> Draft202012Validator:
    """Validator of ``<name>.schema.json`` (snapshot, static, market, manifest)."""
    schema = _registry().contents(f"{BASE_URI}{name}.schema.json")
    return Draft202012Validator(
        schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def _check(name: str, document: Any) -> None:
    errors = sorted(validator(name).iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first: ValidationError = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise SnapshotSchemaError(
            f"{name} schema: {len(errors)} error(s); first at {where}: {first.message[:300]}"
        )


def validate_bundle(document: Any) -> None:
    """Raise :class:`SnapshotSchemaError` unless ``document`` (wire form) is a valid bundle."""
    kind = document.get("kind") if isinstance(document, dict) else None
    _check(kind if kind in ("static", "market") else "snapshot", document)


def validate_manifest(document: Any) -> None:
    """Raise :class:`SnapshotSchemaError` unless ``document`` is a valid manifest."""
    _check("manifest", document)
