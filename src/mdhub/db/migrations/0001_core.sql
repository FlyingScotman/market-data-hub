-- 0001_core: schemas, provenance/operations tables (ops.*) and the generic row-history trigger.
-- DESIGN §4 "ops". Migration number ranges: 0001-0099 core/ops, 0100-0199 ref,
-- 0200-0299 md, 0300-0399 legacy, 0400+ api/views (DESIGN §5).
-- Applied by mdhub.db.migrate in a single transaction. Never edit after it has been applied
-- anywhere: add a new migration instead (the runner refuses changed checksums).

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS md;
CREATE SCHEMA IF NOT EXISTS legacy_md;
CREATE SCHEMA IF NOT EXISTS legacy_bc;

COMMENT ON SCHEMA ops IS 'Provenance and operations: sources, job runs, raw captures, row history, gaps, backfill';
COMMENT ON SCHEMA ref IS 'Static / reference data (instruments, calendars, issuers)';
COMMENT ON SCHEMA md IS 'Normalized market data';
COMMENT ON SCHEMA legacy_md IS 'Verbatim copy of legacy market-data.sqlite3';
COMMENT ON SCHEMA legacy_bc IS 'Verbatim copy of legacy bond_catalog.sqlite3';

-- Created by the migration runner before any migration runs; repeated here for completeness.
CREATE TABLE IF NOT EXISTS ops.schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now(),
    checksum   text NOT NULL
);

-- ---------------------------------------------------------------------------- ops.source
CREATE TABLE ops.source (
    code         text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]*$'),
    name         text NOT NULL,
    base_url     text,
    licence_note text
);
COMMENT ON TABLE ops.source IS 'Data providers; ops.raw_capture.source references code';

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('moex_iss',      'MOEX ISS (public)',           'https://iss.moex.com/iss',   'Public ISS; marketdata 15-min delayed'),
    ('moex_apim',     'MOEX APIM / AlgoPack',        'https://apim.moex.com/iss',  'MOEX_API_KEY bearer; partial realtime entitlement'),
    ('moex_passport', 'MOEX Passport',               'https://passport.moex.com',  'Login only; not realtime-entitled'),
    ('moex_sdfi',     'MOEX SDFI swap curves',       'https://iss.moex.com/iss/sdfi', 'Subscription required for values'),
    ('ratescalcs',    'Ratescalcs',                  'https://ratescalcs.ru',      'Basic auth, GET only; dealer runs'),
    ('cbr',           'Bank of Russia',              'https://www.cbr.ru',         'Public'),
    ('ecb',           'European Central Bank',       'https://www.ecb.europa.eu',  'Public eurofxref'),
    ('cbonds',        'Cbonds JSON API',             'https://ws2.cbonds.info',    'Licensed; optional enrichment'),
    ('otc_monitor',   'OTC Monitor',                 NULL,                         'Legacy history only'),
    ('andromeda',     'Andromeda terminal',          NULL,                         'Legacy history only')
ON CONFLICT (code) DO NOTHING;

-- --------------------------------------------------------------------------- ops.job_run
CREATE TABLE ops.job_run (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job            text NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at    timestamptz,
    status         text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'skipped')),
    params         jsonb NOT NULL DEFAULT '{}'::jsonb,
    rows_inserted  bigint NOT NULL DEFAULT 0 CHECK (rows_inserted >= 0),
    rows_updated   bigint NOT NULL DEFAULT 0 CHECK (rows_updated >= 0),
    rows_unchanged bigint NOT NULL DEFAULT 0 CHECK (rows_unchanged >= 0),
    error          text,
    host           text,
    CONSTRAINT job_run_finished_iff_done CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX job_run_job_started_idx ON ops.job_run (job, started_at DESC);
CREATE INDEX job_run_started_idx ON ops.job_run (started_at DESC);
CREATE INDEX job_run_running_idx ON ops.job_run (job) WHERE status = 'running';
COMMENT ON TABLE ops.job_run IS 'One row per job execution (mdhub.core.capture.job_run)';
COMMENT ON COLUMN ops.job_run.error IS 'Sanitized error text (failed) or reasons (partial/skipped)';

-- ----------------------------------------------------------------------- ops.raw_capture
CREATE TABLE ops.raw_capture (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         bigint REFERENCES ops.job_run (id),
    source         text NOT NULL REFERENCES ops.source (code),
    endpoint       text NOT NULL,
    url_redacted   text NOT NULL,
    http_status    integer,
    content_type   text,
    fetched_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_ts      timestamptz,
    parser_version text NOT NULL,
    sha256         text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    payload_gz     bytea NOT NULL,
    size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
    CONSTRAINT raw_capture_sha256_endpoint_fetched_at_key UNIQUE (sha256, endpoint, fetched_at)
);
-- Payload is already gzip-compressed: store out of line without TOAST compression.
ALTER TABLE ops.raw_capture ALTER COLUMN payload_gz SET STORAGE EXTERNAL;
CREATE INDEX raw_capture_source_endpoint_idx ON ops.raw_capture (source, endpoint, fetched_at DESC);
CREATE INDEX raw_capture_run_idx ON ops.raw_capture (run_id);
CREATE INDEX raw_capture_fetched_at_idx ON ops.raw_capture (fetched_at);
COMMENT ON TABLE ops.raw_capture IS 'Every fetched payload (gzip), SHA-256 of the uncompressed bytes, URL without secrets';
COMMENT ON COLUMN ops.raw_capture.size_bytes IS 'Uncompressed payload size';
COMMENT ON COLUMN ops.raw_capture.source_ts IS 'Source''s own clock (e.g. ISS SYSTIME/UPDATETIME) when available';

-- ----------------------------------------------------------------------- ops.row_history
CREATE TABLE ops.row_history (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name      text NOT NULL,
    row_key         jsonb NOT NULL,
    old_row         jsonb NOT NULL,
    replaced_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    replaced_by_run bigint
);
CREATE INDEX row_history_key_idx ON ops.row_history (table_name, row_key, replaced_at);
CREATE INDEX row_history_replaced_at_idx ON ops.row_history (replaced_at);
COMMENT ON TABLE ops.row_history IS 'Previous row images of tables with ops.enable_history(); written by ops.track_changes()';

-- -------------------------------------------------------------------------- ops.data_gap
CREATE TABLE ops.data_gap (
    dataset     text NOT NULL,
    key         jsonb NOT NULL DEFAULT '{}'::jsonb,
    gap_date    date NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'backfilled', 'unfillable')),
    note        text,
    PRIMARY KEY (dataset, key, gap_date)
);
CREATE INDEX data_gap_open_idx ON ops.data_gap (dataset, gap_date) WHERE status = 'open';

-- --------------------------------------------------------------------- ops.backfill_task
CREATE TABLE ops.backfill_task (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset    text NOT NULL,
    params     jsonb NOT NULL DEFAULT '{}'::jsonb,
    status     text NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    attempts   integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz
);
-- At most one open task per (dataset, params).
CREATE UNIQUE INDEX backfill_task_open_uniq ON ops.backfill_task (dataset, params)
    WHERE status IN ('pending', 'running');
CREATE INDEX backfill_task_status_idx ON ops.backfill_task (status, created_at);

-- ---------------------------------------------------------- generic row-history trigger
-- Trigger arguments (TG_ARGV): the natural-key column names, optionally followed by the
-- marker '--meta' and extra meta column names. Meta columns never cause a history row:
-- ingested_at, updated_at, run_id, capture_id, last_seen_run, last_seen (+ extras).
-- replaced_by_run = session setting mdhub.run_id (set by mdhub.core.upsert) if set, else
-- NEW.run_id / NEW.last_seen_run when they changed.
CREATE OR REPLACE FUNCTION ops.track_changes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    meta_cols text[] := ARRAY['ingested_at', 'updated_at', 'run_id', 'capture_id',
                              'last_seen_run', 'last_seen'];
    key_cols  text[] := ARRAY[]::text[];
    in_meta   boolean := false;
    arg       text;
    old_j     jsonb;
    new_j     jsonb;
    run_txt   text;
BEGIN
    IF TG_OP <> 'UPDATE' OR TG_LEVEL <> 'ROW' THEN
        RAISE EXCEPTION 'ops.track_changes() must be used as a row-level UPDATE trigger';
    END IF;
    FOREACH arg IN ARRAY COALESCE(TG_ARGV, ARRAY[]::text[]) LOOP
        IF arg = '--meta' THEN
            in_meta := true;
        ELSIF in_meta THEN
            meta_cols := meta_cols || arg;
        ELSE
            key_cols := key_cols || arg;
        END IF;
    END LOOP;

    old_j := to_jsonb(OLD);
    new_j := to_jsonb(NEW);
    IF (old_j - meta_cols) IS NOT DISTINCT FROM (new_j - meta_cols) THEN
        RETURN NULL;  -- only meta columns changed: no history
    END IF;

    run_txt := NULLIF(current_setting('mdhub.run_id', true), '');
    IF run_txt IS NULL AND (new_j -> 'run_id') IS DISTINCT FROM (old_j -> 'run_id') THEN
        run_txt := new_j ->> 'run_id';
    END IF;
    IF run_txt IS NULL AND (new_j -> 'last_seen_run') IS DISTINCT FROM (old_j -> 'last_seen_run') THEN
        run_txt := new_j ->> 'last_seen_run';
    END IF;

    INSERT INTO ops.row_history (table_name, row_key, old_row, replaced_by_run)
    VALUES (
        TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
        (SELECT COALESCE(jsonb_object_agg(k, old_j -> k), '{}'::jsonb) FROM unnest(key_cols) AS k),
        old_j,
        CASE WHEN run_txt ~ '^-?[0-9]+$' THEN run_txt::bigint END
    );
    RETURN NULL;
END;
$$;
COMMENT ON FUNCTION ops.track_changes() IS
    'AFTER UPDATE row trigger: stores OLD row image in ops.row_history when a non-meta column changed';

-- Attach (or re-attach) the history trigger to a table.
--   CALL ops.enable_history('md.bond_eod', ARRAY['trade_date','secid','board']);
--   CALL ops.enable_history('ref.calendar_day', ARRAY['calendar','day'], ARRAY['known_at']);
CREATE OR REPLACE PROCEDURE ops.enable_history(
    tbl regclass,
    key_cols text[],
    extra_meta_cols text[] DEFAULT ARRAY[]::text[]
)
LANGUAGE plpgsql AS $$
DECLARE
    c    text;
    args text;
BEGIN
    IF key_cols IS NULL OR cardinality(key_cols) = 0 THEN
        RAISE EXCEPTION 'ops.enable_history(%): key_cols must not be empty', tbl;
    END IF;
    FOREACH c IN ARRAY key_cols || COALESCE(extra_meta_cols, ARRAY[]::text[]) LOOP
        IF c = '--meta' OR NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = tbl AND attname = c AND attnum > 0 AND NOT attisdropped
        ) THEN
            RAISE EXCEPTION 'ops.enable_history(%): column % does not exist', tbl, c;
        END IF;
    END LOOP;
    SELECT string_agg(quote_literal(a), ', ' ORDER BY ord) INTO args
    FROM unnest(
        key_cols || CASE WHEN cardinality(COALESCE(extra_meta_cols, ARRAY[]::text[])) > 0
                         THEN ARRAY['--meta'] || extra_meta_cols
                         ELSE ARRAY[]::text[] END
    ) WITH ORDINALITY AS t(a, ord);
    EXECUTE format('DROP TRIGGER IF EXISTS mdhub_row_history ON %s', tbl);
    EXECUTE format(
        'CREATE TRIGGER mdhub_row_history AFTER UPDATE ON %s '
        'FOR EACH ROW EXECUTE FUNCTION ops.track_changes(%s)',
        tbl, args
    );
END;
$$;
COMMENT ON PROCEDURE ops.enable_history(regclass, text[], text[]) IS
    'Attach ops.track_changes() as trigger mdhub_row_history (idempotent)';

CREATE OR REPLACE PROCEDURE ops.disable_history(tbl regclass)
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('DROP TRIGGER IF EXISTS mdhub_row_history ON %s', tbl);
END;
$$;
