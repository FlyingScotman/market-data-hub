-- 0750_ops_host_metric: machine state for the Machine page of the admin console (ADMIN_DESIGN §10).
--   ops.host_metric          raw samples: hostmon (host, every 30 s) and the worker (pg.*, every minute)
--   ops.host_metric_5m       5-minute rollup (min/avg/max), filled by ops.host_metric_rollup()
--   ops.v_host_status        latest value per series (the states come from mdhub.hostmon.status,
--                            because the thresholds are settings, MDHUB_HOSTMON_*)
--   ops.container_status     one row per container, written by the updater (phase D)
-- Retention (maintenance mode=retention): raw 7 days, rollup 90 days.

-- ----------------------------------------------------------------------- ops.host_metric
CREATE TABLE ops.host_metric (
    ts     timestamptz NOT NULL,
    metric text NOT NULL CHECK (metric ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$'),
    labels jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(labels) = 'object'),
    value  double precision NOT NULL
);
CREATE INDEX host_metric_metric_ts_idx ON ops.host_metric (metric, ts DESC);
CREATE INDEX host_metric_ts_idx ON ops.host_metric (ts);
COMMENT ON TABLE ops.host_metric IS 'Raw machine samples (narrow): hostmon every 30 s, PostgreSQL statistics (pg.*) every minute; kept 7 days';
COMMENT ON COLUMN ops.host_metric.metric IS 'Dotted name, e.g. cpu.steal_pct, disk.used_pct, pg.connections (catalogue in mdhub.hostmon.status)';
COMMENT ON COLUMN ops.host_metric.labels IS 'Series labels, e.g. {"mount": "/"}, {"iface": "eth0"}, {"device": "sda"}, {"table": "md.bond_eod"}';

-- -------------------------------------------------------------------- ops.host_metric_5m
CREATE TABLE ops.host_metric_5m (
    bucket timestamptz NOT NULL,
    metric text NOT NULL,
    labels jsonb NOT NULL,
    n      integer NOT NULL CHECK (n > 0),
    min    double precision NOT NULL,
    avg    double precision NOT NULL,
    max    double precision NOT NULL,
    PRIMARY KEY (metric, bucket, labels)
);
CREATE INDEX host_metric_5m_bucket_idx ON ops.host_metric_5m (bucket);
COMMENT ON TABLE ops.host_metric_5m IS '5-minute rollup of ops.host_metric (bucket = start of the 5 minutes); kept 90 days';

CREATE FUNCTION ops.host_metric_rollup() RETURNS bigint
LANGUAGE sql AS $$
    -- Re-aggregates the newest rolled bucket (it may have been incomplete) and every complete
    -- bucket after it; the current, still filling bucket is left for the next call.
    WITH src AS (
        SELECT date_bin('5 minutes', ts, timestamptz '2000-01-01 00:00+00') AS bucket,
               metric, labels, count(*)::integer AS n,
               min(value) AS min, avg(value) AS avg, max(value) AS max
        FROM ops.host_metric
        WHERE ts >= coalesce((SELECT max(bucket) FROM ops.host_metric_5m), '-infinity')
          AND ts < date_bin('5 minutes', now(), timestamptz '2000-01-01 00:00+00')
        GROUP BY 1, 2, 3
    ), up AS (
        INSERT INTO ops.host_metric_5m (bucket, metric, labels, n, min, avg, max)
        SELECT bucket, metric, labels, n, min, avg, max FROM src
        ON CONFLICT (metric, bucket, labels) DO UPDATE
            SET n = EXCLUDED.n, min = EXCLUDED.min, avg = EXCLUDED.avg, max = EXCLUDED.max
        RETURNING 1
    )
    SELECT count(*) FROM up
$$;
COMMENT ON FUNCTION ops.host_metric_rollup() IS
    'Fill ops.host_metric_5m up to the last complete 5 minutes; returns buckets written (hostmon every 5 min, maintenance before retention)';

-- ---------------------------------------------------------------------- ops.v_host_status
CREATE VIEW ops.v_host_status AS
SELECT DISTINCT ON (metric, labels) metric, labels, ts, value
FROM ops.host_metric
WHERE ts > now() - interval '10 minutes'
ORDER BY metric, labels, ts DESC;
COMMENT ON VIEW ops.v_host_status IS
    'Latest value per series of the last 10 minutes; ok/warn/crit states are computed by mdhub.hostmon.status from the MDHUB_HOSTMON_* thresholds';

-- -------------------------------------------------------------------- ops.container_status
CREATE TABLE ops.container_status (
    name            text PRIMARY KEY,
    service         text,
    image           text,
    state           text NOT NULL,
    health          text,
    restart_count   integer NOT NULL DEFAULT 0 CHECK (restart_count >= 0),
    started_at      timestamptz,
    cpu_pct         real,
    mem_bytes       bigint,
    mem_limit_bytes bigint,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ops.container_status IS 'Current state per container, upserted every 30 s by the updater (the only process with the docker socket)';
COMMENT ON COLUMN ops.container_status.state IS 'docker state: running, restarting, exited, paused, dead, created';
COMMENT ON COLUMN ops.container_status.health IS 'healthcheck status (healthy, unhealthy, starting) or NULL without a healthcheck';
COMMENT ON COLUMN ops.container_status.image IS 'Image reference with tag, e.g. mdhub:v1.4.0';
COMMENT ON COLUMN ops.container_status.updated_at IS 'Time of the last report; the console shows "updater not reporting" when it is older than 2 minutes';
