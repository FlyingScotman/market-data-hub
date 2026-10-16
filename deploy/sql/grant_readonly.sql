-- Re-apply read-only grants for backend consumers (idempotent). Run as the owner role after
-- `mdhub migrate` or a seed restore (pg_restore --no-privileges drops grants):
--   psql -v ON_ERROR_STOP=1 -v ro_user=mdhub_ro -d mdhub -f deploy/sql/grant_readonly.sql
-- Grants USAGE on every non-system schema and SELECT on all its tables, views and sequences.
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
  AND pg_get_userbyid(nspowner) = current_user
\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
  AND pg_get_userbyid(nspowner) = current_user
\gexec
SELECT format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', nspname, :'ro_user')
FROM pg_namespace
WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'
  AND pg_get_userbyid(nspowner) = current_user
\gexec
