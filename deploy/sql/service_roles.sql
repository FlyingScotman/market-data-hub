-- Login roles of the least-privilege services (idempotent; README_DEPLOY §16). Run as a
-- superuser (compose: the owner role POSTGRES_USER) in the mdhub database:
--   psql -v ON_ERROR_STOP=1 -v dbname=mdhub -v api_password=... -v admin_password=... \
--        -v hostmon_password=... -f service_roles.sql
-- mdhub_api (read API), mdhub_admin (console), mdhub_hostmon (host sampler). A missing role is
-- created NOLOGIN; a non-empty password makes it LOGIN with that password (so a re-run rotates
-- it); an empty one leaves an existing role as it is. The grants themselves come from
-- admin.grant_service_roles() (migration 0800), which admin.restrict_access() calls: run here
-- when the schema exists, and by `mdhub migrate` and grant_readonly.sql every time.
\getenv api_password MDHUB_API_DB_PASSWORD
\getenv admin_password MDHUB_ADMIN_DB_PASSWORD
\getenv hostmon_password MDHUB_HOSTMON_DB_PASSWORD
SELECT format('CREATE ROLE %I NOLOGIN', r)
FROM unnest(ARRAY['mdhub_api', 'mdhub_admin', 'mdhub_hostmon']) r
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r)
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', r, pw)
FROM (VALUES ('mdhub_api', :'api_password'), ('mdhub_admin', :'admin_password'),
             ('mdhub_hostmon', :'hostmon_password')) v (r, pw)
WHERE pw <> ''
\gexec
GRANT CONNECT ON DATABASE :"dbname" TO mdhub_api, mdhub_admin, mdhub_hostmon;
SELECT 'CALL admin.restrict_access()'
WHERE to_regprocedure('admin.restrict_access()') IS NOT NULL
\gexec
