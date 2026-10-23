"""JSON Schema of the Market Snapshot Contract v1 (``schemas/market_snapshot/v1``).

The schema files live at the repository root (the hub owns the contract); wheels carry a copy
at ``mdhub/schemas`` (``tool.hatch.build.targets.wheel.force-include``). :func:`schema_dir`
finds either. Validation uses JSON Schema draft 2020-12 with a local registry (no network).
"""

from __future__ import annotations

import json
import re
from functools import cache, partial
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator as PythonValidator
from jsonschema_rs import Draft202012Validator, Registry, ValidationError

from mdhub.core.errors import MdhubError

__all__ = [
    "SCHEMA_VERSION",
    "SnapshotSchemaError",
    "schema_dir",
    "validate_bundle",
    "validate_manifest",
    "validator",
]

SCHEMA_VERSION: Final = "market_snapshot/1.6"
"""Value of ``schema_version`` written by this hub release."""

BASE_URI: Final = "https://schemas.market-data-hub.invalid/market_snapshot/v1/"
_RELATIVE: Final = Path("market_snapshot") / "v1"


class SnapshotSchemaError(MdhubError, ValueError):
    """A snapshot document does not conform to the contract schema."""


class _PythonType:
    """Keep Python jsonschema's types (notably lists, not tuples, for arrays)."""

    def __init__(
        self, parent_schema: dict[str, Any], value: Any, schema_path: list[str | int]
    ) -> None:
        self.types: tuple[str, ...] = (value,) if isinstance(value, str) else tuple(value)

    def validate(self, instance: Any) -> None:
        if not any(PythonValidator.TYPE_CHECKER.is_type(instance, t) for t in self.types):
            raise ValueError(f"expected type {' or '.join(self.types)}")


class _PythonPattern:
    """Keep Python regex semantics, including $ matching before a final newline."""

    def __init__(
        self, parent_schema: dict[str, Any], value: Any, schema_path: list[str | int]
    ) -> None:
        self.pattern: re.Pattern[str] = re.compile(value)

    def validate(self, instance: Any) -> None:
        if isinstance(instance, str) and self.pattern.search(instance) is None:
            raise ValueError(f"does not match {self.pattern.pattern!r}")


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
def _schemas() -> dict[str, Any]:
    schemas = {}
    for path in sorted(schema_dir().glob("*.schema.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        schemas[str(doc["$id"])] = doc
    return schemas


@cache
def _registry() -> Registry:
    return Registry(list(_schemas().items()))


@cache
def validator(name: str = "snapshot") -> Draft202012Validator:
    """Validator of ``<name>.schema.json`` (snapshot, static, market, manifest)."""
    schema = _schemas()[f"{BASE_URI}{name}.schema.json"]
    return Draft202012Validator(
        schema,
        registry=_registry(),
        offline=True,
        keywords={"type": _PythonType, "pattern": _PythonPattern},
        # These are v1's formats. Keep the old checker, including its optional date-time
        # dependency, rather than silently changing acceptance with native format checks.
        formats={
            name: partial(PythonValidator.FORMAT_CHECKER.conforms, format=name)
            for name in ("date", "date-time")
        },
        validate_formats=True,
    )


def _check(name: str, document: Any) -> None:
    errors = sorted(validator(name).iter_errors(document), key=lambda e: e.instance_path)
    if errors:
        first: ValidationError = errors[0]
        where = "/".join(str(p) for p in first.instance_path) or "<root>"
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
