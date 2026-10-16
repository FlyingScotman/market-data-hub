-- 0400_ops_views: data-quality state and operational views (DESIGN §6 quality/backfill).
-- Owned by mdhub.quality. Tables written by the `quality` and `backfill_worker` jobs:
--   ops.dataset_status   one row per registered dataset (latest check)
--   ops.quality_report   one structured report per `quality` run
-- Views: ops.v_coverage, ops.v_job_health, ops.v_open_gaps, ops.v_backfill_queue.

-- ------------------------------------------------------- ops.backfill_task: worker columns
ALTER TABLE ops.backfill_task
    ADD COLUMN IF NOT EXISTS not_before timestamptz,
    ADD COLUMN IF NOT EXISTS started_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_run_id bigint,
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'quality';
COMMENT ON COLUMN ops.backfill_task.not_before IS 'Retry backoff: the worker ignores the task until this time';
COMMENT ON COLUMN ops.backfill_task.started_at IS 'When the current/last attempt started';
COMMENT ON COLUMN ops.backfill_task.last_run_id IS 'ops.job_run.id of the last backfill job run';
COMMENT ON COLUMN ops.backfill_task.source IS 'Who queued the task: quality (gap scan) or cli (manual)';
CREATE INDEX IF NOT EXISTS backfill_task_pending_idx ON ops.backfill_task (created_at, id)
    WHERE status = 'pending';

-- ----------------------------------------------------------------- ops.dataset_status
CREATE TABLE ops.dataset_status (
    dataset         text PRIMARY KEY,
    table_name      text NOT NULL,
    frequency       text NOT NULL,
    calendar        text,
    backfill_job    text,
    first_date      date,
    last_date       date,
    row_count       bigint,
    expected_latest date,
    is_stale        boolean NOT NULL DEFAULT false,
    gap_days        integer NOT NULL DEFAULT 0,
    problem         text,
    checked_at      timestamptz NOT NULL DEFAULT now(),
    run_id          bigint
);
COMMENT ON TABLE ops.dataset_status IS 'Latest freshness/coverage check per registered dataset (written by the quality job)';
COMMENT ON COLUMN ops.dataset_status.expected_latest IS 'Latest date the dataset should contain given its calendar and max_lag_days';
COMMENT ON COLUMN ops.dataset_status.gap_days IS 'Missing (dataset, key, day) combinations found by the last scan';

-- ----------------------------------------------------------------- ops.quality_report
CREATE TABLE ops.quality_report (
    run_id        bigint PRIMARY KEY REFERENCES ops.job_run (id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    as_of         date NOT NULL,
    stale_count   integer NOT NULL DEFAULT 0,
    gap_count     integer NOT NULL DEFAULT 0,
    tasks_queued  integer NOT NULL DEFAULT 0,
    job_problems  integer NOT NULL DEFAULT 0,
    report        jsonb NOT NULL
);
CREATE INDEX quality_report_created_idx ON ops.quality_report (created_at DESC);
COMMENT ON TABLE ops.quality_report IS 'Structured report of each quality run (datasets, gaps, job health)';

-- ------------------------------------------------------------------------ ops.v_coverage
CREATE VIEW ops.v_coverage AS
SELECT
    s.dataset,
    s.table_name,
    s.frequency,
    s.calendar,
    s.first_date,
    s.last_date,
    s.row_count,
    s.expected_latest,
    s.is_stale,
    (SELECT count(*) FROM ops.data_gap g
      WHERE g.dataset = s.dataset AND g.status = 'open') AS open_gaps,
    (SELECT count(*) FROM ops.data_gap g
      WHERE g.dataset = s.dataset AND g.status = 'unfillable') AS unfillable_gaps,
    (SELECT count(*) FROM ops.backfill_task t
      WHERE t.dataset = s.dataset AND t.status IN ('pending', 'running')) AS open_tasks,
    s.backfill_job,
    lr.status AS last_run_status,
    lr.started_at AS last_run_at,
    ls.last_success_at,
    s.problem,
    s.checked_at
FROM ops.dataset_status s
LEFT JOIN LATERAL (
    SELECT r.status, r.started_at FROM ops.job_run r
    WHERE r.job = s.backfill_job
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) lr ON true
LEFT JOIN LATERAL (
    SELECT max(r.started_at) AS last_success_at FROM ops.job_run r
    WHERE r.job = s.backfill_job AND r.status IN ('succeeded', 'partial')
) ls ON true;
COMMENT ON VIEW ops.v_coverage IS 'Per dataset: first/last date, rows, staleness, gaps, tasks and last run of its job';

-- ---------------------------------------------------------------------- ops.v_job_health
-- Distinct job names via a loose index scan on job_run_job_started_idx.
CREATE VIEW ops.v_job_health AS
WITH RECURSIVE jobs AS (
    (SELECT r.job FROM ops.job_run r ORDER BY r.job LIMIT 1)
    UNION ALL
    SELECT (SELECT r.job FROM ops.job_run r WHERE r.job > jobs.job ORDER BY r.job LIMIT 1)
    FROM jobs WHERE jobs.job IS NOT NULL
)
SELECT
    j.job,
    lr.id AS last_run_id,
    lr.status AS last_status,
    lr.started_at AS last_started_at,
    lr.finished_at AS last_finished_at,
    lr.error AS last_error,
    ok.last_success_at,
    (SELECT count(*) FROM ops.job_run f
      WHERE f.job = j.job AND f.status = 'failed'
        AND f.started_at > COALESCE(ok.last_success_at, '-infinity'::timestamptz)
    ) AS consecutive_failures,
    d.runs_24h,
    d.failed_24h,
    d.skipped_24h
FROM jobs j
CROSS JOIN LATERAL (
    SELECT r.id, r.status, r.started_at, r.finished_at, r.error FROM ops.job_run r
    WHERE r.job = j.job
    ORDER BY r.started_at DESC, r.id DESC
    LIMIT 1
) lr
CROSS JOIN LATERAL (
    SELECT max(r.started_at) AS last_success_at FROM ops.job_run r
    WHERE r.job = j.job AND r.status IN ('succeeded', 'partial')
) ok
CROSS JOIN LATERAL (
    SELECT count(*) AS runs_24h,
           count(*) FILTER (WHERE r.status = 'failed') AS failed_24h,
           count(*) FILTER (WHERE r.status = 'skipped') AS skipped_24h
    FROM ops.job_run r
    WHERE r.job = j.job AND r.started_at > now() - interval '24 hours'
) d
WHERE j.job IS NOT NULL;
COMMENT ON VIEW ops.v_job_health IS 'Per job: last run, last success, consecutive failures since last success, 24h counts';

-- ----------------------------------------------------------------------- ops.v_open_gaps
CREATE VIEW ops.v_open_gaps AS
SELECT
    g.dataset,
    g.key,
    g.gap_date,
    g.detected_at,
    g.note,
    t.id AS task_id,
    t.status AS task_status,
    t.attempts AS task_attempts,
    t.not_before AS task_not_before
FROM ops.data_gap g
LEFT JOIN LATERAL (
    SELECT bt.id, bt.status, bt.attempts, bt.not_before FROM ops.backfill_task bt
    WHERE bt.dataset = g.dataset
      AND bt.params ? 'date_from' AND bt.params ? 'date_to'
      AND (bt.params ->> 'date_from') <= g.gap_date::text
      AND (bt.params ->> 'date_to') >= g.gap_date::text
    ORDER BY bt.created_at DESC, bt.id DESC
    LIMIT 1
) t ON true
WHERE g.status = 'open';
COMMENT ON VIEW ops.v_open_gaps IS 'Open data gaps with the latest backfill task covering each day (ISO date params)';

-- ------------------------------------------------------------------ ops.v_backfill_queue
CREATE VIEW ops.v_backfill_queue AS
SELECT
    t.id,
    t.dataset,
    t.params ->> 'date_from' AS date_from,
    t.params ->> 'date_to' AS date_to,
    t.status,
    t.attempts,
    t.not_before,
    t.started_at,
    t.done_at,
    t.last_run_id,
    t.last_error,
    t.source,
    t.created_at
FROM ops.backfill_task t
WHERE t.status IN ('pending', 'running', 'failed');
COMMENT ON VIEW ops.v_backfill_queue IS 'Backfill tasks that are queued, running or exhausted (failed)';
