-- Per-database setup for the read-only consumer role (idempotent). Run as a superuser or the
-- owner role in the mdhub database after it is (re)created:
--   psql -v ON_ERROR_STOP=1 -v owner=mdhub -v ro_user=mdhub_ro -v dbname=mdhub -f readonly_defaults.sql
-- Default privileges make every schema/table/sequence the owner creates later (migrations,
-- seed restore) readable by ro_user. The role itself must already exist.
GRANT CONNECT ON DATABASE :"dbname" TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT USAGE ON SCHEMAS TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT SELECT ON TABLES TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" GRANT SELECT ON SEQUENCES TO :"ro_user";
