"""Legacy SQLite stores -> PostgreSQL.

Phase 1 (this package): verbatim, lossless copy of ``market-data.sqlite3`` into schema
``legacy_md`` and ``bond_catalog.sqlite3`` into ``legacy_bc``
(:func:`mdhub.legacy.import_sqlite.import_sqlite`), and an exact content verification on
both sides (:func:`mdhub.legacy.verify.verify_schema`). Phase 2 maps the copies into
``md`` / ``ref``: :func:`mdhub.legacy.map_md.map_md` and :func:`mdhub.legacy.map_bc.map_bc`
(rules in ``MAPPING.md`` in this directory).

CLI: ``mdhub legacy import --md <sqlite> --bc <sqlite> [--drop-existing]``,
``mdhub legacy verify``, ``mdhub legacy map-md``, ``mdhub legacy map-bc``.
"""

from mdhub.legacy.import_sqlite import ImportResult, LegacyImportError, import_sqlite
from mdhub.legacy.verify import TableCheck, VerifyResult, verify_schema

LEGACY_SCHEMAS = {"md": "legacy_md", "bc": "legacy_bc"}
"""Source key -> target schema."""

__all__ = [
    "LEGACY_SCHEMAS",
    "ImportResult",
    "LegacyImportError",
    "TableCheck",
    "VerifyResult",
    "import_sqlite",
    "verify_schema",
]
