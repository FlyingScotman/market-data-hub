-- 0800_service_roles: least-privilege database roles for the internet-facing and sampling
-- services (README_DEPLOY §16). worker, migrate and updater keep the owner role.
--
--   mdhub_api      read API: market/reference data and the ops views it serves, its request
--                  log, token lookup and last use
--   mdhub_admin    admin console: what its pages read and its actions write, nothing more
--   mdhub_hostmon  host sampler: its samples, the rollup, its heartbeat
--
-- The roles themselves (LOGIN, password, pg_read_all_stats) are created by
-- deploy/sql/service_roles.sql, run as a superuser. This migration only grants, and only to
-- roles that exist, so installs without them are unaffected.
--
-- admin.grant_service_roles() is the single source of the grants and is idempotent: it first
-- revokes everything the three roles hold outside schema admin, then grants the exact set.
-- admin.restrict_access() (re-defined below) strips schema admin from every role and then
-- calls it, so every path that already re-locks admin - each admin migration, `mdhub migrate`,
-- deploy/sql/grant_readonly.sql and readonly_defaults.sql, a seed restore, the updater's
-- restore - re-grants the service roles too. A migration that adds a table a service reads or
-- writes must CREATE OR REPLACE this procedure (tests/test_service_roles.py runs the apps as
-- these roles).

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

-- ------------------------------------------------------------------- admin.restrict_access
CREATE OR REPLACE PROCEDURE admin.restrict_access()
LANGUAGE plpgsql AS $$
DECLARE
    owner_oid oid;
    grantee   text;
BEGIN
    SELECT nspowner INTO owner_oid FROM pg_namespace WHERE nspname = 'admin';
    REVOKE ALL ON SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL TABLES IN SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA admin FROM PUBLIC;
    REVOKE ALL ON ALL ROUTINES IN SCHEMA admin FROM PUBLIC;
    FOR grantee IN
        SELECT DISTINCT pg_get_userbyid(a.grantee)
        FROM (
            SELECT (aclexplode(n.nspacl)).grantee FROM pg_namespace n WHERE n.nspname = 'admin'
            UNION ALL
            SELECT (aclexplode(c.relacl)).grantee
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'admin'
            UNION ALL
            SELECT (aclexplode(p.proacl)).grantee
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'admin'
        ) a
        WHERE a.grantee <> 0 AND a.grantee <> owner_oid
    LOOP
        EXECUTE format('REVOKE ALL ON SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA admin FROM %I', grantee);
        EXECUTE format('REVOKE ALL ON ALL ROUTINES IN SCHEMA admin FROM %I', grantee);
    END LOOP;
    CALL admin.grant_service_roles();
END
$$;
COMMENT ON PROCEDURE admin.restrict_access() IS
    'Revoke every privilege in schema admin from PUBLIC and all roles but the owner, then re-grant the service roles; call at the end of each admin migration and after grant scripts';

-- ------------------------------------------------------------- ops.host_metric_rollup
-- hostmon rolls up its own samples without SELECT on ops.host_metric or any right on
-- ops.host_metric_5m: the function runs as its owner and only hostmon (and the owner) may call it.
ALTER FUNCTION ops.host_metric_rollup() SECURITY DEFINER SET search_path = pg_catalog, pg_temp;
REVOKE ALL ON FUNCTION ops.host_metric_rollup() FROM PUBLIC;

CALL admin.restrict_access();
