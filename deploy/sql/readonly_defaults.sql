-- Per-database setup for the read-only consumer role (idempotent). Run as a superuser or the
-- owner role in the mdhub database after it is (re)created:
--   psql -v ON_ERROR_STOP=1 -v owner=mdhub -v ro_user=mdhub_ro -v dbname=mdhub -f readonly_defaults.sql
-- Default privileges make every schema/table/sequence the owner creates later (migrations,
-- seed restore) readable by ro_user. The role itself must already exist.
--
-- Schema `admin` (secrets and identities, ADMIN_DESIGN §3.1) is excluded: PostgreSQL cannot
-- subtract one schema from global default privileges, so every migration that creates objects
-- in admin ends with CALL admin.restrict_access(), and this script calls it too when the
-- schema already exists (re-run after migrations).
GRANT CONNECT ON DATABASE :"dbname" TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT USAGE ON SCHEMAS TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT SELECT ON TABLES TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT SELECT ON SEQUENCES TO :"ro_user";
SELECT 'CALL admin.restrict_access()'
WHERE to_regprocedure('admin.restrict_access()') IS NOT NULL
\gexec
