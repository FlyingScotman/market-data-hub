-- 0640_ops_update: self-update through the updater agent (ADMIN_DESIGN §3.2, §6).
--   ops.release            installed versions (written by the updater)
--   ops.release_available  GitHub releases seen by the updater, with their verification result
--   ops.update_request     install / rollback requests: inserted by the console, driven by the
--                          updater through the §6.3 state machine
--   ops.update_log         progress lines of a request (the live log on the Updates page)
--   ops.worker_control     one row; drain = true tells the worker to start no new job
-- The console only inserts requests; it holds neither the docker socket nor the GitHub token.

CREATE TABLE ops.release (
    version          text PRIMARY KEY,
    git_commit       text,
    source           text NOT NULL CHECK (source IN ('github', 'upload', 'manual')),
    sha256           text,
    key_id           text,
    migrations       jsonb CHECK (migrations IS NULL OR jsonb_typeof(migrations) = 'object'),
    previous_version text,
    backup_path      text,
    installed_at     timestamptz NOT NULL DEFAULT now(),
    installed_by     text,
    status           text NOT NULL
                     CHECK (status IN ('active', 'previous', 'superseded', 'failed', 'rolled_back'))
);
CREATE UNIQUE INDEX release_one_active_key ON ops.release ((true)) WHERE status = 'active';
COMMENT ON TABLE ops.release IS 'Versions installed by the updater (mdhub.updater); a manually installed version appears as source manual when the first update replaces it';
COMMENT ON COLUMN ops.release.migrations IS '{version: sha256} of the release''s migration files';
COMMENT ON COLUMN ops.release.previous_version IS 'The version this one replaced: the target of "roll back to the previous version"';
COMMENT ON COLUMN ops.release.backup_path IS 'Pre-update dump taken before this version was installed (kept outside the backup rotation); restored by a rollback across migrations';
COMMENT ON COLUMN ops.release.status IS 'active (running) / previous (the one before) / superseded (older) / failed (its update was rolled back automatically) / rolled_back (rolled back by an admin)';

CREATE TABLE ops.release_available (
    version              text PRIMARY KEY,
    tag                  text NOT NULL,
    name                 text,
    notes                text,
    html_url             text,
    published_at         timestamptz,
    prerelease           boolean NOT NULL DEFAULT false,
    verified             boolean,
    error                text,
    archive_sha256       text,
    key_id               text,
    git_commit           text,
    min_previous_version text,
    migrations           jsonb CHECK (migrations IS NULL OR jsonb_typeof(migrations) = 'object'),
    checked_at           timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE ops.release_available IS 'GitHub releases listed by the updater; newer ones are downloaded and verified ahead of an install so the console can show their migrations';
COMMENT ON COLUMN ops.release_available.verified IS 'NULL: not downloaded (older than the running version); true: signature, hashes and manifest verified; false: refused (see error)';

CREATE TABLE ops.update_request (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('install', 'rollback')),
    source          text NOT NULL CHECK (source IN ('github', 'upload', 'release')),
    version         text NOT NULL,
    upload_sha256   text CHECK (upload_sha256 ~ '^[0-9a-f]{64}$'),
    requested_by    bigint,
    requested_login text,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued', 'verifying', 'backing_up', 'staging', 'draining',
                                     'migrating', 'starting', 'checking', 'done', 'failed',
                                     'rolling_back', 'rolled_back', 'cancelled')),
    state_at        timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    error           text,
    from_version    text,
    from_tag        text,
    from_link       text,
    archive_sha256  text,
    key_id          text,
    git_commit      text,
    applied_before  jsonb,
    restore_path    text,
    backup_path     text,
    CONSTRAINT update_request_upload_sha CHECK ((source = 'upload') = (upload_sha256 IS NOT NULL)),
    CONSTRAINT update_request_finished_iff_final
        CHECK ((state IN ('done', 'failed', 'rolled_back', 'cancelled')) = (finished_at IS NOT NULL))
);
-- Only one update at a time: a second request while one is open is refused.
CREATE UNIQUE INDEX update_request_one_open_key ON ops.update_request ((true))
    WHERE state NOT IN ('done', 'failed', 'rolled_back', 'cancelled');
CREATE INDEX update_request_requested_at_idx ON ops.update_request (requested_at DESC);
COMMENT ON TABLE ops.update_request IS 'Install and rollback requests from the console, executed by the updater (ADMIN_DESIGN §6.3)';
COMMENT ON COLUMN ops.update_request.source IS 'github (release asset), upload (file in the shared upload volume, named by its sha256), release (an installed release directory: rollback)';
COMMENT ON COLUMN ops.update_request.requested_by IS 'admin.user_account.id (no FK: ops is readable by the read-only role, admin is not)';
COMMENT ON COLUMN ops.update_request.state IS 'queued -> verifying -> backing_up -> staging -> draining -> migrating -> starting -> checking -> done; a failure before draining ends in failed, a later one in rolling_back -> rolled_back (failed when the rollback fails too)';
COMMENT ON COLUMN ops.update_request.applied_before IS '{version: checksum} of ops.schema_migration at verify: a rollback restores the pre-update dump when the applied migrations differ from it';
COMMENT ON COLUMN ops.update_request.restore_path IS 'Rollback requests: the dump restored before migrating when the database has migrations the target does not know';

CREATE TABLE ops.update_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id bigint NOT NULL REFERENCES ops.update_request (id) ON DELETE CASCADE,
    ts         timestamptz NOT NULL DEFAULT clock_timestamp(),
    step       text NOT NULL,
    level      text NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'error')),
    line       text NOT NULL
);
CREATE INDEX update_log_request_idx ON ops.update_log (request_id, id);
COMMENT ON TABLE ops.update_log IS 'Progress lines of an update request (no secrets)';

CREATE TABLE ops.worker_control (
    id         boolean PRIMARY KEY DEFAULT true CHECK (id),
    drain      boolean NOT NULL DEFAULT false,
    reason     text,
    changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ops.worker_control DEFAULT VALUES;
COMMENT ON TABLE ops.worker_control IS 'Single row. drain = true: the worker finishes running jobs and starts no new scheduled run or job request (set by the updater before migrating)';
