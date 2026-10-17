-- Re-apply read-only grants for backend consumers (idempotent). Run as the owner role after
-- `mdhub migrate` or a seed restore (pg_restore --no-privileges drops grants):
--   psql -v ON_ERROR_STOP=1 -v ro_user=mdhub_ro -d mdhub -f deploy/sql/grant_readonly.sql
-- Grants USAGE on every non-system schema except `admin` (secrets and identities,
-- ADMIN_DESIGN §3.1) and SELECT on all its tables, views and sequences; then strips any
-- privilege in `admin` from every role but its owner (admin.restrict_access()).
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'admin')
  AND pg_get_userbyid(nspowner) = current_user
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'admin')
  AND pg_get_userbyid(nspowner) = current_user
\gexec
SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname NOT IN ('information_schema', 'admin')
  AND pg_get_userbyid(nspowner) = current_user
\gexec
SELECT 'CALL admin.restrict_access()'
WHERE to_regprocedure('admin.restrict_access()') IS NOT NULL
\gexec
