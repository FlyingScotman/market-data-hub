-- 0610_ops_observability: state shown by the admin console (ADMIN_DESIGN §3.2).
--   ops.api_request       read-API request log (batch writer mdhub.api.requestlog)
--   ops.worker_heartbeat  one row per worker/updater process, beat every 30 s (mdhub.core.heartbeat)
--   ops.source_probe      reachability / credential probes (job source_probe, every 5 min)
--   ops.job_source        which source each loader job reads (for "last successful load")
--   ops.v_source_status   last probe per source + last successful load
-- Rows of api_request, source_probe and worker_heartbeat older than MDHUB_OPS_LOG_RETENTION_DAYS
-- (default 90) are deleted by maintenance mode=retention.

-- ------------------------------------------------------------------------ ops.api_request
CREATE TABLE ops.api_request (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts             timestamptz NOT NULL,
    token_id       bigint,
    token_name     text,
    client_ip      inet,
    method         text NOT NULL,
    path           text NOT NULL,
    query          text,
    status         smallint NOT NULL,
    duration_ms    real NOT NULL CHECK (duration_ms >= 0),
    response_bytes bigint CHECK (response_bytes >= 0),
    request_id     text,
    user_agent     text
);
CREATE INDEX api_request_ts_idx ON ops.api_request (ts);
CREATE INDEX api_request_token_ts_idx ON ops.api_request (token_name, ts DESC);
CREATE INDEX api_request_ip_ts_idx ON ops.api_request (client_ip, ts DESC);
COMMENT ON TABLE ops.api_request IS 'Read-API requests, written in batches off the request path (ADMIN_DESIGN §3.2)';
COMMENT ON COLUMN ops.api_request.token_id IS 'admin.api_token.id; NULL for MDHUB_API_TOKENS entries, unknown tokens and unauthenticated requests (no FK: tokens are revoked, never deleted)';
COMMENT ON COLUMN ops.api_request.token_name IS 'Token name (env:N or the name of a name=token entry for MDHUB_API_TOKENS); NULL when not authenticated';
COMMENT ON COLUMN ops.api_request.path IS 'Route template (e.g. /v1/bonds/{secid}), never the raw path; <unmatched> when no route matched';
COMMENT ON COLUMN ops.api_request.query IS 'Query string with values of credential-like parameters replaced by ***';

-- ------------------------------------------------------------------- ops.worker_heartbeat
CREATE TABLE ops.worker_heartbeat (
    instance_id  uuid PRIMARY KEY,
    process      text NOT NULL CHECK (process ~ '^[a-z][a-z0-9_-]*$'),
    host         text NOT NULL,
    pid          integer NOT NULL,
    version      text NOT NULL,
    started_at   timestamptz NOT NULL,
    beat_at      timestamptz NOT NULL,
    stopped_at   timestamptz,
    running_jobs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(running_jobs) = 'array')
);
CREATE INDEX worker_heartbeat_process_beat_idx ON ops.worker_heartbeat (process, beat_at DESC);
COMMENT ON TABLE ops.worker_heartbeat IS 'One row per process instance (worker, later updater), upserted every 30 s';
COMMENT ON COLUMN ops.worker_heartbeat.running_jobs IS 'Jobs running in the process at beat_at: [{"job", "run_id", "started_at"}]';
COMMENT ON COLUMN ops.worker_heartbeat.stopped_at IS 'Set on a clean shutdown; a stale beat_at without stopped_at means the process died';

-- ----------------------------------------------------------------------- ops.source_probe
CREATE TABLE ops.source_probe (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         bigint REFERENCES ops.job_run (id) ON DELETE SET NULL,
    source         text NOT NULL REFERENCES ops.source (code),
    endpoint       text NOT NULL,
    ts             timestamptz NOT NULL DEFAULT clock_timestamp(),
    ok             boolean NOT NULL,
    http_status    integer,
    latency_ms     real CHECK (latency_ms >= 0),
    auth           text NOT NULL CHECK (auth IN ('ok', 'rejected', 'not_configured', 'n/a')),
    tls_expires_at timestamptz,
    error          text
);
CREATE INDEX source_probe_source_ts_idx ON ops.source_probe (source, ts DESC);
CREATE INDEX source_probe_ts_idx ON ops.source_probe (ts);
CREATE INDEX source_probe_run_idx ON ops.source_probe (run_id);
COMMENT ON TABLE ops.source_probe IS 'One lightweight request per source (job source_probe); no raw capture';
COMMENT ON COLUMN ops.source_probe.endpoint IS 'Redacted URL of the probe request';
COMMENT ON COLUMN ops.source_probe.ok IS 'Source answered, not 5xx, credentials not rejected';
COMMENT ON COLUMN ops.source_probe.auth IS 'ok / rejected (401, 403) / not_configured (no credentials in env) / n/a (public source)';
COMMENT ON COLUMN ops.source_probe.tls_expires_at IS 'notAfter of the server certificate seen by the probe';
COMMENT ON COLUMN ops.source_probe.error IS 'Sanitized error text (no URLs with secrets, no credential values)';

-- -------------------------------------------------------------------------- ops.job_source
CREATE TABLE ops.job_source (
    job    text NOT NULL,
    source text NOT NULL REFERENCES ops.source (code),
    PRIMARY KEY (job, source)
);
CREATE INDEX job_source_source_idx ON ops.job_source (source);
COMMENT ON TABLE ops.job_source IS 'Loader job -> source it fetches from (tests/test_observability.py keeps it in line with mdhub.schedule.KNOWN_JOBS)';

INSERT INTO ops.job_source (job, source) VALUES
    ('bonds_static',        'moex_iss'),
    ('bonds_static_probe',  'moex_iss'),
    ('bonds_eod',           'moex_iss'),
    ('bonds_intraday',      'moex_iss'),
    ('fx_moex_intraday',    'moex_iss'),
    ('fx_moex_eod',         'moex_iss'),
    ('fx_indicative',       'moex_iss'),
    ('futures_intraday',    'moex_apim'),
    ('futures_intraday',    'moex_iss'),
    ('futures_eod',         'moex_iss'),
    ('algopack_fx_futures', 'moex_apim'),
    ('indices_eod',         'moex_iss'),
    ('zcyc',                'moex_iss'),
    ('cbr_daily',           'cbr'),
    ('cbr_meetings',        'cbr'),
    ('ecb_fx',              'ecb'),
    ('rosstat_cpi',         'rosstat'),
    ('swaps_ratescalcs',    'ratescalcs'),
    ('swaps_otc_monitor',   'otc_monitor'),
    ('cbonds_enrich',       'cbonds')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------- ops.v_source_status
CREATE VIEW ops.v_source_status AS
SELECT
    s.code               AS source,
    s.name,
    p.ts                 AS probed_at,
    p.endpoint,
    p.ok,
    p.http_status,
    p.latency_ms,
    p.auth,
    p.tls_expires_at,
    p.error,
    last_ok.ts           AS last_ok_probe_at,
    ld.job               AS last_load_job,
    ld.run_id            AS last_load_run_id,
    ld.finished_at       AS last_load_at
FROM ops.source s
LEFT JOIN LATERAL (
    SELECT sp.ts, sp.endpoint, sp.ok, sp.http_status, sp.latency_ms, sp.auth, sp.tls_expires_at,
           sp.error
    FROM ops.source_probe sp
    WHERE sp.source = s.code
    ORDER BY sp.ts DESC
    LIMIT 1
) p ON true
LEFT JOIN LATERAL (
    SELECT sp.ts FROM ops.source_probe sp
    WHERE sp.source = s.code AND sp.ok
    ORDER BY sp.ts DESC
    LIMIT 1
) last_ok ON true
LEFT JOIN LATERAL (
    SELECT r.job, r.id AS run_id, r.finished_at
    FROM ops.job_source js
    CROSS JOIN LATERAL (
        SELECT jr.job, jr.id, jr.finished_at FROM ops.job_run jr
        WHERE jr.job = js.job AND jr.status IN ('succeeded', 'partial')
        ORDER BY jr.started_at DESC
        LIMIT 1
    ) r
    WHERE js.source = s.code
    ORDER BY r.finished_at DESC
    LIMIT 1
) ld ON true
WHERE p.ts IS NOT NULL OR EXISTS (SELECT 1 FROM ops.job_source js WHERE js.source = s.code);
COMMENT ON VIEW ops.v_source_status IS 'Per source: last probe, last ok probe, last succeeded/partial run of a job reading it (ADMIN_DESIGN §4 Sources)';
