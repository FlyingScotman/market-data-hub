-- 0630_ops_job_request: "run this job now" requests from the admin console (ADMIN_DESIGN §4).
-- The console only inserts rows; the worker (mdhub scheduler, mdhub.job_requests) claims them
-- with FOR UPDATE SKIP LOCKED and runs them through run_job with trigger 'admin', so the
-- console needs neither source credentials nor network access to sources.

CREATE TABLE ops.job_request (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job             text NOT NULL,
    params          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(params) = 'object'),
    requested_by    bigint,
    requested_login text,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    worker_instance uuid,
    picked_at       timestamptz,
    finished_at     timestamptz,
    job_run_id      bigint REFERENCES ops.job_run (id) ON DELETE SET NULL,
    error           text,
    CONSTRAINT job_request_finished_iff_final
        CHECK ((state IN ('done', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);
-- One open request per job: a second click while one is queued or running is refused.
CREATE UNIQUE INDEX job_request_open_job_key ON ops.job_request (job)
    WHERE state IN ('queued', 'running');
CREATE INDEX job_request_queued_idx ON ops.job_request (requested_at, id) WHERE state = 'queued';
CREATE INDEX job_request_requested_at_idx ON ops.job_request (requested_at DESC);
COMMENT ON TABLE ops.job_request IS 'Job runs requested in the admin console, executed by the worker (mdhub.job_requests)';
COMMENT ON COLUMN ops.job_request.requested_by IS 'admin.user_account.id of the requesting user (no FK: ops is readable by the read-only role, admin is not)';
COMMENT ON COLUMN ops.job_request.state IS 'queued -> running (claimed by a worker) -> done (the run finished; its status is in ops.job_run, also skipped with lock_busy) / failed (no run or the run failed) / cancelled (withdrawn while queued)';
COMMENT ON COLUMN ops.job_request.worker_instance IS 'ops.worker_heartbeat.instance_id of the claiming worker; a running request whose worker stopped or went silent is requeued (at most 2 attempts)';
COMMENT ON COLUMN ops.job_request.job_run_id IS 'The ops.job_run row of the execution';
