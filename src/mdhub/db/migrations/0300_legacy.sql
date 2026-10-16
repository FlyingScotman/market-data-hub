-- 0300_legacy: bookkeeping for the verbatim import of the legacy SQLite stores
-- (DESIGN §4 "legacy_md, legacy_bc"). The copied tables themselves are created by
-- `mdhub legacy import` (mdhub.legacy.import_sqlite), not by migrations, because their
-- shape is read from the SQLite file. Each import appends one row here; `mdhub legacy
-- verify` updates the latest row of a schema with checksums and the verification outcome.

CREATE SCHEMA IF NOT EXISTS legacy_md;
CREATE SCHEMA IF NOT EXISTS legacy_bc;

CREATE TABLE ops.legacy_import (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    schema_name       text NOT NULL CHECK (schema_name ~ '^legacy_[a-z0-9_]+$'),
    source_path       text NOT NULL,
    source_sha256     text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_size_bytes bigint NOT NULL CHECK (source_size_bytes >= 0),
    snapshot_json     jsonb,
    sqlite_meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
    imported_at       timestamptz NOT NULL DEFAULT now(),
    duration_s        double precision,
    table_counts      jsonb NOT NULL,
    verified          boolean NOT NULL DEFAULT false,
    verified_at       timestamptz,
    table_checksums   jsonb,
    verify_report     jsonb
);
CREATE INDEX legacy_import_schema_idx ON ops.legacy_import (schema_name, id DESC);

COMMENT ON TABLE ops.legacy_import IS
    'One row per verbatim SQLite -> legacy_* import (mdhub legacy import); verified by mdhub legacy verify';
COMMENT ON COLUMN ops.legacy_import.snapshot_json IS 'SNAPSHOT.json shipped next to the SQLite file (verbatim)';
COMMENT ON COLUMN ops.legacy_import.sqlite_meta IS 'SQLite pragmas, MANIFEST.sha256 entry, constraint/index outcomes';
COMMENT ON COLUMN ops.legacy_import.table_counts IS '{table: row count} as imported';
COMMENT ON COLUMN ops.legacy_import.table_checksums IS '{table: sha256} content checksum, identical on SQLite and PostgreSQL';
