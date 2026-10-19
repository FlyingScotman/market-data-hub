-- 0810_snapshot_service_roles: the read API serves /v1/snapshots from ops.published_snapshot
-- (0510), so mdhub_api gets SELECT on it. Re-creates admin.grant_service_roles() (0800 is the
-- single source of the grants; a table a service reads needs a new CREATE OR REPLACE) with
-- that one grant added. The worker builds and publishes snapshots as the owner role (0800), and
-- the API reads no as-known-at reader (0500): no other grant.

-- -------------------------------------------------------------- admin.grant_service_roles
CREATE OR REPLACE PROCEDURE admin.grant_service_roles()
LANGUAGE plpgsql AS $$
DECLARE
    r text;
    s text;
BEGIN
    FOREACH r IN ARRAY ARRAY['mdhub_api', 'mdhub_admin', 'mdhub_hostmon'] LOOP
        CONTINUE WHEN to_regrole(r) IS NULL;
        FOR s IN
            SELECT nspname FROM pg_namespace
            WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'admin')
        LOOP
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', s, r);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, r);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, r);
            EXECUTE format('REVOKE ALL ON ALL ROUTINES IN SCHEMA %I FROM %I', s, r);
        END LOOP;
    END LOOP;

    IF to_regrole('mdhub_api') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA md, ref, ops, admin TO mdhub_api;
        -- Everything in md and ref is what /v1 serves (routes, TDM export).
        GRANT SELECT ON ALL TABLES IN SCHEMA md, ref TO mdhub_api;
        GRANT SELECT ON ops.v_coverage, ops.v_job_health, ops.v_open_gaps, ops.job_run,
            ops.schema_migration TO mdhub_api;
        GRANT SELECT ON ops.published_snapshot TO mdhub_api;  -- /v1/snapshots (0510)
        GRANT SELECT ON ops.row_history TO mdhub_api;  -- known_at reads of md/ref history
        GRANT INSERT ON ops.api_request TO mdhub_api;  -- COPY of the request log
        GRANT SELECT ON admin.api_token TO mdhub_api;
        GRANT UPDATE (last_used_at, last_used_ip) ON admin.api_token TO mdhub_api;
    END IF;

    IF to_regrole('mdhub_admin') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA admin, ops, ref TO mdhub_admin;
        GRANT SELECT, INSERT, UPDATE ON admin.user_account, admin.enrollment TO mdhub_admin;
        GRANT SELECT, INSERT, UPDATE, DELETE ON admin.session, admin.webauthn_credential,
            admin.totp_secret, admin.recovery_code, admin.source_credential TO mdhub_admin;
        GRANT SELECT, INSERT, DELETE ON admin.login_attempt TO mdhub_admin;
        GRANT SELECT, INSERT ON admin.audit_log TO mdhub_admin;  -- append-only
        GRANT SELECT, INSERT, UPDATE ON admin.api_token TO mdhub_admin;
        -- The worker publishes its key; a console able to write it could swap in its own and
        -- open every credential sealed afterwards.
        GRANT SELECT ON admin.secret_key_public TO mdhub_admin;
        GRANT SELECT ON ops.worker_heartbeat, ops.job_run, ops.v_job_health, ops.v_source_status,
            ops.v_coverage, ops.v_backfill_queue, ops.backfill_task, ops.data_gap,
            ops.job_request, ops.api_request, ops.release, ops.release_available,
            ops.update_request, ops.update_log, ops.schema_migration, ops.host_metric,
            ops.host_metric_5m, ops.v_host_status, ops.container_status TO mdhub_admin;
        GRANT INSERT, UPDATE (state, finished_at) ON ops.job_request TO mdhub_admin;
        GRANT INSERT, UPDATE (state, state_at, finished_at) ON ops.update_request TO mdhub_admin;
        GRANT UPDATE (status, attempts, not_before, done_at) ON ops.backfill_task TO mdhub_admin;
        GRANT UPDATE (status, note) ON ops.data_gap TO mdhub_admin;
        GRANT SELECT ON ref.calendar_day, ref.weekend_session TO mdhub_admin;  -- next fire
    END IF;

    IF to_regrole('mdhub_hostmon') IS NOT NULL THEN
        GRANT USAGE ON SCHEMA ops TO mdhub_hostmon;
        GRANT INSERT ON ops.host_metric TO mdhub_hostmon;
        GRANT EXECUTE ON FUNCTION ops.host_metric_rollup() TO mdhub_hostmon;
        -- its heartbeat upsert; ON CONFLICT reads the conflict key and the EXCLUDED columns
        GRANT INSERT, UPDATE (beat_at, version, stopped_at, running_jobs),
            SELECT (instance_id, beat_at, version, stopped_at, running_jobs)
            ON ops.worker_heartbeat TO mdhub_hostmon;
    END IF;
END
$$;
COMMENT ON PROCEDURE admin.grant_service_roles() IS
    'Re-grant the exact privileges of mdhub_api, mdhub_admin, mdhub_hostmon (those that exist); called by admin.restrict_access()';

CALL admin.restrict_access();
