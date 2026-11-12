INSERT INTO ops.job_source(job, source) VALUES ('spb_bonds_daily', 'spb');
DO $$
BEGIN
    IF to_regprocedure('admin.grant_service_roles()') IS NOT NULL THEN
        CALL admin.grant_service_roles();
    END IF;
END
$$;
