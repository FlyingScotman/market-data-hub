-- 0510_published_snapshot: immutable published snapshots (Market Snapshot Contract v1,
-- system ADR-002; DESIGN §4 "ops", §11). Distinct from ref.static_snapshot, the per-SECID
-- source payload cache of the static loader.
--
-- * id = sha256 of the canonical bundle document (sorted keys, UTF-8, decimals as strings);
--   payload_gz = deterministic gzip of exactly those bytes, so sha256(gunzip(payload_gz)) = id.
-- * Immutable: no UPDATE ever; a correction is a new row whose supersedes names the earlier
--   snapshot of the same cut (kind, cut_type, business_date, cut_ts). Only intraday market
--   snapshots may be deleted (retention, maintenance mode=retention); static and EOD
--   snapshots are kept forever.
-- * The manifest columns repeat fields of the bundle for listing and filtering; manifest =
--   the bundle's envelope (everything but data), served by /v1/snapshots/{id}/manifest.

CREATE TABLE ops.published_snapshot (
    id                   text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    schema_version       text NOT NULL,
    kind                 text NOT NULL CHECK (kind IN ('static', 'market')),
    cut_type             text NOT NULL,
    business_date        date NOT NULL,
    cut_ts               timestamptz NOT NULL,
    known_at             timestamptz NOT NULL,
    static_snapshot_id   text REFERENCES ops.published_snapshot (id),
    supersedes           text REFERENCES ops.published_snapshot (id),
    content_sha256       text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    calendar_fingerprint text NOT NULL CHECK (calendar_fingerprint ~ '^[0-9a-f]{64}$'),
    sources              jsonb NOT NULL CHECK (jsonb_typeof(sources) = 'array'),
    datasets             jsonb NOT NULL CHECK (jsonb_typeof(datasets) = 'array'),
    excluded             jsonb NOT NULL CHECK (jsonb_typeof(excluded) = 'array'),
    excluded_count       integer NOT NULL CHECK (excluded_count >= 0),
    manifest             jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    payload_gz           bytea NOT NULL,
    size_bytes           bigint NOT NULL CHECK (size_bytes > 0),
    gzip_bytes           bigint NOT NULL CHECK (gzip_bytes > 0),
    run_id               bigint,
    published_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT published_snapshot_cut_type_chk CHECK (
        (kind = 'market' AND cut_type IN ('eod_preliminary', 'eod_final', 'intraday'))
        OR (kind = 'static' AND cut_type IN ('daily', 'change'))),
    CONSTRAINT published_snapshot_static_ref_chk CHECK (
        (kind = 'market') = (static_snapshot_id IS NOT NULL)),
    CONSTRAINT published_snapshot_supersedes_chk CHECK (supersedes IS DISTINCT FROM id)
);
CREATE INDEX published_snapshot_cut_idx
    ON ops.published_snapshot (kind, cut_type, business_date, cut_ts, published_at);
CREATE INDEX published_snapshot_latest_idx
    ON ops.published_snapshot (kind, known_at DESC, published_at DESC);
CREATE INDEX published_snapshot_static_idx ON ops.published_snapshot (static_snapshot_id);
CREATE UNIQUE INDEX published_snapshot_supersedes_idx
    ON ops.published_snapshot (supersedes) WHERE supersedes IS NOT NULL;
COMMENT ON TABLE ops.published_snapshot IS
    'Immutable published snapshots (Market Snapshot Contract v1): id = sha256 of the canonical '
    'bundle, payload_gz = its deterministic gzip. Corrections are new rows (supersedes); only '
    'intraday market snapshots are deleted by retention. Not ref.static_snapshot (source cache)';
COMMENT ON COLUMN ops.published_snapshot.content_sha256 IS
    'sha256 of the canonical data part (plus static_snapshot_id for market): change detection, '
    'a rebuild with identical content publishes nothing';
COMMENT ON COLUMN ops.published_snapshot.cut_ts IS 'Nominal instant of the cut';
COMMENT ON COLUMN ops.published_snapshot.known_at IS
    'No record the hub learned after this instant is included (as-known-at readers, 0500)';

-- At most one superseding snapshot per snapshot (unique index above) and corrections stay
-- within their cut.
CREATE FUNCTION ops.published_snapshot_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prev ops.published_snapshot%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'ops.published_snapshot is immutable (snapshot %): publish a correction '
                        'that supersedes it', OLD.id;
    ELSIF TG_OP = 'DELETE' THEN
        IF NOT (OLD.kind = 'market' AND OLD.cut_type = 'intraday') THEN
            RAISE EXCEPTION 'ops.published_snapshot %: only intraday market snapshots may be '
                            'deleted (retention)', OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.supersedes IS NOT NULL THEN
        SELECT * INTO prev FROM ops.published_snapshot WHERE id = NEW.supersedes;
        IF FOUND AND (prev.kind, prev.cut_type, prev.business_date, prev.cut_ts)
                     IS DISTINCT FROM (NEW.kind, NEW.cut_type, NEW.business_date, NEW.cut_ts) THEN
            RAISE EXCEPTION 'snapshot % may only supersede a snapshot of the same cut', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER published_snapshot_guard
    BEFORE INSERT OR UPDATE OR DELETE ON ops.published_snapshot
    FOR EACH ROW EXECUTE FUNCTION ops.published_snapshot_guard();
