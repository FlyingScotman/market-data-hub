"""Native validation must keep the existing Python validator's v1 acceptance."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator as PythonValidator
from jsonschema import FormatChecker
from jsonschema_rs import Registry as NativeRegistry
from jsonschema_rs import ValidationError
from referencing import Registry, Resource

from mdhub.snapshot import schema
from mdhub.snapshot.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas" / "market_snapshot" / "v1"
PathParts = tuple[str | int, ...]


@cache
def _reference(name: str) -> PythonValidator:
    documents = [json.loads(path.read_text()) for path in SCHEMAS.glob("*.schema.json")]
    registry: Registry[Any] = Registry().with_resources(
        (doc["$id"], Resource.from_contents(doc)) for doc in documents
    )
    return PythonValidator(
        registry.contents(f"{schema.BASE_URI}{name}.schema.json"),
        registry=registry,
        format_checker=PythonValidator.FORMAT_CHECKER,
    )


def _document(name: str) -> dict[str, Any]:
    file = "market_eod_final" if name == "market" else "static_daily"
    doc: dict[str, Any] = json.loads((SCHEMAS / "fixtures" / f"{file}.json").read_text())
    if name == "manifest":
        del doc["data"]
        doc["snapshot_id"] = "a" * 64
    else:
        # One row per dataset retains every dataset's shape while keeping mutations cheap.
        doc["data"] = {key: rows[:1] for key, rows in doc["data"].items()}
    return doc


def _at(document: Any, path: PathParts) -> Any:
    for part in path:
        document = document[part]
    return document


def _assert_same(name: str, document: Any) -> bool:
    expected = _reference(name).is_valid(document)
    assert schema.validator(name).is_valid(document) == expected
    validate = schema.validate_manifest if name == "manifest" else schema.validate_bundle
    if expected:
        validate(document)
    else:
        with pytest.raises(schema.SnapshotSchemaError):
            validate(document)
    return expected


def _objects(document: Any, path: PathParts = ()) -> Iterator[tuple[PathParts, dict[str, Any]]]:
    if isinstance(document, dict):
        yield path, document
        for key, value in document.items():
            yield from _objects(value, (*path, key))
    elif isinstance(document, list) and document:
        yield from _objects(document[0], (*path, 0))


@pytest.mark.parametrize("name", ["static", "market", "manifest"])
def test_valid_documents_are_not_modified(name: str) -> None:
    doc = _document(name)
    before = canonical_bytes(doc)
    assert _assert_same(name, doc)
    assert canonical_bytes(doc) == before


@pytest.mark.parametrize("document", [None, True, 1, 1.5, "invalid", [], ()])
def test_nonobject_documents_are_rejected(document: Any) -> None:
    assert not _assert_same("snapshot", document)


@pytest.mark.parametrize("name", ["static", "market", "manifest"])
def test_missing_and_extra_fields_match_python(name: str) -> None:
    original = _document(name)
    for path, obj in _objects(original):
        for key in obj:
            doc = copy.deepcopy(original)
            del _at(doc, path)[key]
            _assert_same(name, doc)
        doc = copy.deepcopy(original)
        _at(doc, path)["unexpected"] = "x"
        _assert_same(name, doc)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_per_bond", "14.500"),
        ("amount_per_bond", "-0.00000000000000000000000000001"),
        ("amount_per_bond", "1\n"),
        ("amount_per_bond", "1\r\n"),
        ("amount_per_bond", "01"),
        ("amount_per_bond", "1e2"),
        ("amount_per_bond", "١"),
        ("amount_per_bond", 14.5),
        ("amount_per_bond", None),
        ("coupon_no", 2**1024),
        ("coupon_no", -(2**1024)),
        ("coupon_no", True),
        ("coupon_no", 1.0),
        ("coupon_no", 1.5),
        ("coupon_no", Decimal("1")),
        ("coupon_no", 0),
        ("pay_date", "2024-02-29"),
        ("pay_date", "2026-02-29"),
        ("pay_date", "0000-01-01"),
        ("pay_date", "2026-01-01\n"),
        ("pay_date", None),
        ("start_date", None),
        ("start_date", "2026-13-01"),
        ("secid", ""),
        ("status", "invalid"),
    ],
)
def test_coupon_value_acceptance_matches_python(field: str, value: Any) -> None:
    doc = _document("static")
    doc["data"]["coupon_periods"][0][field] = value
    _assert_same("static", doc)


@pytest.mark.parametrize("name", ["static", "market", "manifest"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "market_snapshot/2.0"),
        ("kind", "invalid"),
        ("cut_type", "invalid"),
        ("business_date", "2026-02-29"),
        ("known_at", "2026-99-99T99:99:99Z"),
        ("known_at", "2026-12-31T23:59:60Z"),
        ("known_at", "2026-01-01T00:00:00.123456Z"),
        ("known_at", "2026-01-01T00:00:00.1234567Z"),
        ("known_at", "2026-01-01T03:00:00+03:00"),
        ("supersedes", None),
        ("supersedes", "a" * 64),
        ("supersedes", "a" * 64 + "\n"),
        ("supersedes", "A" * 64),
        ("static_snapshot_id", None),
        ("static_snapshot_id", "a" * 64),
    ],
)
def test_envelope_value_acceptance_matches_python(name: str, field: str, value: Any) -> None:
    doc = _document(name)
    doc[field] = value
    _assert_same(name, doc)


@pytest.mark.parametrize("name", ["static", "market"])
def test_arrays_nulls_labels_and_wrong_dataset_types_match_python(name: str) -> None:
    original = _document(name)
    for dataset, rows in original["data"].items():
        for replacement in (None, {}, tuple(rows), []):
            doc = copy.deepcopy(original)
            doc["data"][dataset] = replacement
            _assert_same(name, doc)
    dataset = "instruments" if name == "static" else "bond_quotes"
    for labels in (["valid_label"], ["valid_label", "valid_label"], ["Invalid"], ["valid\n"]):
        doc = copy.deepcopy(original)
        doc["data"][dataset][0]["labels"] = labels
        _assert_same(name, doc)


@pytest.fixture
def isolated_schemas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    def clear() -> None:
        schema.validator.cache_clear()
        schema._registry.cache_clear()
        schema._schemas.cache_clear()

    clear()
    directory = tmp_path / "schemas"
    directory.mkdir()
    monkeypatch.setattr(schema, "schema_dir", lambda: directory)
    try:
        yield directory
    finally:
        clear()


def test_local_relative_fragment_refs_and_siblings(isolated_schemas: Path) -> None:
    (isolated_schemas / "static.schema.json").write_text(
        json.dumps(
            {
                "$id": f"{schema.BASE_URI}static.schema.json",
                "$ref": "common.schema.json#/$defs/value",
                "minimum": 1,
            }
        )
    )
    (isolated_schemas / "common.schema.json").write_text(
        json.dumps(
            {
                "$id": f"{schema.BASE_URI}common.schema.json",
                "$defs": {"value": {"$ref": "#/$defs/integer"}, "integer": {"type": "integer"}},
            }
        )
    )
    compiled = schema.validator("static")
    assert compiled.is_valid(2)
    assert not compiled.is_valid(0)
    assert not compiled.is_valid(True)
    assert not compiled.is_valid("2")


@pytest.mark.parametrize("scheme", ["https", "file"])
def test_unknown_refs_cannot_fetch_network_or_files(
    isolated_schemas: Path, scheme: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = isolated_schemas.parent / "unregistered.schema.json"
    other.write_text(json.dumps({"type": "integer"}))
    reference = (
        "https://example.invalid/missing.schema.json" if scheme == "https" else other.as_uri()
    )
    (isolated_schemas / "static.schema.json").write_text(
        json.dumps({"$id": f"{schema.BASE_URI}static.schema.json", "$ref": reference})
    )
    # Registry construction itself rejects unresolved references without retrieving them.
    with pytest.raises(ValueError, match="Default retriever does not fetch resources"):
        schema.validator("static")
    # The validator must stay offline even for references absent from its input registry.
    with monkeypatch.context() as patch:
        patch.setattr(schema, "_registry", lambda: NativeRegistry([]))
        with pytest.raises(ValidationError, match="Retrieval is disabled"):
            schema.validator("static")


@pytest.mark.parametrize("enabled", [False, True])
def test_date_time_checker_remains_optional(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    checker = FormatChecker()
    checker.checkers.pop("date-time", None)
    if enabled:
        checker.checks("date-time")(lambda value: value != "2026-99-99T99:99:99Z")
    monkeypatch.setattr(PythonValidator, "FORMAT_CHECKER", checker)
    schema.validator.cache_clear()
    try:
        doc = _document("static")
        doc["known_at"] = "2026-99-99T99:99:99Z"
        assert schema.validator("static").is_valid(doc) is not enabled
    finally:
        schema.validator.cache_clear()


def test_schema_error_identifies_nested_location() -> None:
    doc = _document("static")
    doc["data"]["coupon_periods"][0]["amount_per_bond"] = 14.5
    with pytest.raises(schema.SnapshotSchemaError, match="data/coupon_periods/0/amount_per_bond"):
        schema.validate_bundle(doc)
