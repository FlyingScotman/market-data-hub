#!/usr/bin/env bash
# Runs once, when the postgres container initialises an empty data volume.
# Creates the read-only role for backend consumers (MDHUB_RO_USER, default mdhub_ro) and the
# default privileges that make everything the owner role (POSTGRES_USER) creates later -
# `mdhub migrate`, seed restore - readable by it (/mdhub-sql/readonly_defaults.sql), except
# schema `admin` (secrets and identities): its migrations revoke every privilege there from
# all roles but the owner (admin.restrict_access(), ADMIN_DESIGN §3.1).
# If MDHUB_RO_PASSWORD is empty the role is created without LOGIN.
# Also creates the service roles mdhub_api, mdhub_admin, mdhub_hostmon
# (/mdhub-sql/service_roles.sql; LOGIN once their MDHUB_*_DB_PASSWORD is set). `mdhub migrate`
# grants them their privileges (migration 0800, README_DEPLOY §16).
set -euo pipefail

ro_user="${MDHUB_RO_USER:-mdhub_ro}"
ro_password="${MDHUB_RO_PASSWORD:-}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v ro_user="$ro_user" -v ro_password="$ro_password" <<'SQL'
SELECT format('CREATE ROLE %I %s', :'ro_user',
              CASE WHEN :'ro_password' = '' THEN 'NOLOGIN'
                   ELSE format('LOGIN PASSWORD %L', :'ro_password') END)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = '120s';
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner="$POSTGRES_USER" -v ro_user="$ro_user" -v dbname="$POSTGRES_DB" \
    -f /mdhub-sql/readonly_defaults.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v dbname="$POSTGRES_DB" -v api_password="${MDHUB_API_DB_PASSWORD:-}" \
    -v admin_password="${MDHUB_ADMIN_DB_PASSWORD:-}" \
    -v hostmon_password="${MDHUB_HOSTMON_DB_PASSWORD:-}" \
    -f /mdhub-sql/service_roles.sql
