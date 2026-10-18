#!/usr/bin/env bash
# Restore the bundle's seed dump (pg_dump -Fc, pg_dump 16) and bring it to the code's schema.
#
#   deploy/restore_seed.sh [--yes] [DUMP]                         # docker compose (deploy/.env)
#   deploy/restore_seed.sh --database-url URL [--yes] [DUMP]      # any PostgreSQL 16, no docker
#
# DUMP defaults to db/mdhub.dump of the unpacked bundle (deploy/make_bundle.sh); DUMP.sha256 is
# checked when present. Both modes: pg_restore --no-owner --no-privileges --single-transaction,
# then `mdhub migrate` (applies newer migrations, refuses if an applied one's checksum differs),
# then ops.schema_migration is compared with the code's migration files (all applied, checksums
# equal), read-only grants are re-applied, ANALYZE, `mdhub db-info`. The service roles
# (mdhub_api, mdhub_admin, mdhub_hostmon) get their grants back from `mdhub migrate` and the
# read-only grant script (both call admin.restrict_access(), migration 0800); compose also
# re-runs sql/service_roles.sql (creates missing roles, README_DEPLOY §16).
#
# compose: starts postgres, stops worker/api, DROPS and re-creates the database (all data in it
# is lost; asks unless --yes), restores, starts everything again.
# --database-url: the target database must exist and be empty (re-create it first - see
# README_DEPLOY.md "Bare metal"); pg_restore/psql from PG_BIN or PATH, mdhub from MDHUB_CMD
# (default: mdhub on PATH, else .venv/bin/mdhub of this checkout). Grants are re-applied when
# the read-only role (MDHUB_RO_USER, default mdhub_ro) exists. The URL reaches psql/pg_restore
# as an argument (visible in ps): prefer peer authentication or .pgpass.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"

die() { echo "restore_seed: $*" >&2; exit 1; }
log() { echo "==> $*"; }

assume_yes=0
dump=""
url=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) assume_yes=1; shift ;;
        --database-url) [[ $# -ge 2 ]] || die "--database-url needs a value"; url="$2"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) dump="$1"; shift ;;
    esac
done
dump="${dump:-$SRC_DIR/db/mdhub.dump}"
[[ -f "$dump" ]] || die "seed dump not found: $dump"
dump="$(cd "$(dirname "$dump")" && pwd)/$(basename "$dump")"

if command -v sha256sum >/dev/null; then SHA256="sha256sum"; else SHA256="shasum -a 256"; fi
sha_of() { $SHA256 "$1" | cut -d' ' -f1; }

if [[ -f "$dump.sha256" ]]; then
    log "checking $(basename "$dump") against $(basename "$dump").sha256"
    [[ "$(sha_of "$dump")" == "$(cut -d' ' -f1 "$dump.sha256")" ]] \
        || die "SHA-256 of $dump does not match $dump.sha256 (incomplete copy?)"
else
    echo "note: no $(basename "$dump").sha256 next to the dump - integrity not checked" >&2
fi

# verify_migrations "version,checksum lines": every migration file of the code is applied with
# the same checksum (mdhub migrate already refuses on a mismatch; this double-checks the result).
verify_migrations() {
    local applied="$1" f v sum db_sum n=0 missing="" bad=""
    local dir="$SRC_DIR/src/mdhub/db/migrations"
    if ! ls "$dir"/*.sql >/dev/null 2>&1; then
        echo "note: $dir not found - schema_migration not compared with the code" >&2
        return 0
    fi
    for f in "$dir"/*.sql; do
        v="$(basename "$f" .sql)"
        sum="$(sha_of "$f")"
        db_sum="$(awk -F, -v v="$v" '$1 == v {print $2}' <<<"$applied")"
        n=$((n + 1))
        if [[ -z "$db_sum" ]]; then
            missing="$missing $v"
        elif [[ "$db_sum" != "$sum" ]]; then
            bad="$bad $v"
        fi
    done
    [[ -z "$bad" ]] || die "ops.schema_migration checksum differs from the code:$bad"
    [[ -z "$missing" ]] || die "migrations not applied:$missing"
    echo "schema_migration: all $n code migrations applied, checksums match"
}

ro_user="${MDHUB_RO_USER:-mdhub_ro}"
restore_flags=(--no-owner --no-privileges --exit-on-error --single-transaction)
migrations_sql="SELECT version || ',' || checksum FROM ops.schema_migration ORDER BY version"

# ------------------------------------------------------------------- direct (--database-url)
if [[ -n "$url" ]]; then
    if [[ -z "${PG_BIN:-}" ]]; then
        command -v pg_restore >/dev/null || die "pg_restore not found (install postgresql-client-16 or set PG_BIN)"
        PG_BIN="$(dirname "$(command -v pg_restore)")"
    fi
    PSQL=("$PG_BIN/psql" -X -q -A -t -v ON_ERROR_STOP=1 -d "$url")
    if [[ -z "${MDHUB_CMD:-}" ]]; then
        if command -v mdhub >/dev/null; then
            MDHUB_CMD="mdhub"
        elif [[ -x "$SRC_DIR/.venv/bin/mdhub" ]]; then
            MDHUB_CMD="$SRC_DIR/.venv/bin/mdhub"
        else
            die "mdhub not found (set MDHUB_CMD, e.g. /opt/mdhub/.venv/bin/mdhub)"
        fi
    fi
    run_mdhub() { MDHUB_DATABASE_URL="$url" $MDHUB_CMD "$@"; }

    log "pg_restore: $("$PG_BIN/pg_restore" --version)"
    objects="$("${PSQL[@]}" -c "
        SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\_toast%' AND n.nspname NOT LIKE 'pg\_temp%'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                            AND d.deptype = 'e')")" || die "cannot connect to the target database"
    [[ "$objects" == 0 ]] \
        || die "target database is not empty ($objects relations): drop and re-create it first"
    encoding="$("${PSQL[@]}" -c "SELECT pg_encoding_to_char(encoding) FROM pg_database
                                  WHERE datname = current_database()")"
    [[ "$encoding" == UTF8 ]] \
        || die "target database encoding is $encoding, need UTF8: re-create it with createdb -E UTF8 -T template0"
    db_name="$("${PSQL[@]}" -c "SELECT current_database()")"
    if [[ "$assume_yes" != 1 && -t 0 ]]; then
        read -r -p "Restore ${dump} into the empty database '${db_name}'? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || die "aborted"
    fi

    log "restoring $(du -h "$dump" | cut -f1) into ${db_name} (this can take a while)"
    "$PG_BIN/pg_restore" "${restore_flags[@]}" -d "$url" "$dump"

    log "applying pending migrations"
    run_mdhub migrate

    log "verifying ops.schema_migration"
    verify_migrations "$("${PSQL[@]}" -c "$migrations_sql")"

    ro_exists="$("${PSQL[@]}" -v ro_user="$ro_user" <<<"SELECT count(*) FROM pg_roles WHERE rolname = :'ro_user'")"
    if [[ "$ro_exists" == 1 ]]; then
        log "re-applying read-only grants for ${ro_user}"
        "${PSQL[@]}" -v ro_user="$ro_user" -f "$DEPLOY_DIR/sql/grant_readonly.sql" >/dev/null
    else
        echo "note: role ${ro_user} does not exist - read-only grants skipped" >&2
    fi

    log "analyzing"
    "${PSQL[@]}" -c "SET client_min_messages = error" -c "ANALYZE"

    log "verification"
    run_mdhub db-info
    echo "restore complete"
    exit 0
fi

# ------------------------------------------------------------------- docker compose
cd "$DEPLOY_DIR"
[[ -f .env ]] || die "deploy/.env not found (cp env.example .env and fill it in)"
command -v docker >/dev/null || die "docker is not installed (or use --database-url URL)"

compose() { docker compose "$@"; }
in_pg() { compose exec -T postgres "$@"; }

log "starting postgres"
compose up -d postgres
for _ in $(seq 1 60); do
    if in_pg sh -c 'pg_isready -q -U "$POSTGRES_USER" -d postgres -h 127.0.0.1'; then
        break
    fi
    sleep 2
done
in_pg sh -c 'pg_isready -q -U "$POSTGRES_USER" -d postgres -h 127.0.0.1' \
    || die "postgres did not become ready"

db_name="$(in_pg sh -c 'printf %s "$POSTGRES_DB"')"
if [[ "$assume_yes" != 1 ]]; then
    read -r -p "Drop and re-create database '${db_name}' and restore ${dump}? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || die "aborted"
fi

log "stopping worker and api"
compose stop worker api >/dev/null 2>&1 || true

log "re-creating database ${db_name}"
in_pg sh -c 'dropdb --if-exists --force -U "$POSTGRES_USER" "$POSTGRES_DB" \
    && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" -E UTF8 "$POSTGRES_DB"'
in_pg sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v owner="$POSTGRES_USER" -v ro_user="${MDHUB_RO_USER:-mdhub_ro}" -v dbname="$POSTGRES_DB" \
    -f /mdhub-sql/readonly_defaults.sql \
    -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'

log "restoring $(du -h "$dump" | cut -f1) from ${dump} (this can take a while)"
in_pg sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" '"${restore_flags[*]}" <"$dump"

log "applying pending migrations"
compose run --rm migrate

log "verifying ops.schema_migration"
verify_migrations "$(in_pg sh -c 'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" -c "'"$migrations_sql"'"')"

log "re-applying read-only grants"
in_pg sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ro_user="${MDHUB_RO_USER:-mdhub_ro}" -f /mdhub-sql/grant_readonly.sql' >/dev/null

log "re-applying service roles and their grants"
in_pg sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v dbname="$POSTGRES_DB" -v api_password="${MDHUB_API_DB_PASSWORD:-}" \
    -v admin_password="${MDHUB_ADMIN_DB_PASSWORD:-}" \
    -v hostmon_password="${MDHUB_HOSTMON_DB_PASSWORD:-}" \
    -f /mdhub-sql/service_roles.sql' >/dev/null

log "analyzing"
in_pg sh -c 'vacuumdb -U "$POSTGRES_USER" -d "$POSTGRES_DB" --analyze-only --quiet'

log "starting all services"
compose up -d

log "verification"
compose run --rm --no-deps worker mdhub db-info
compose run --rm --no-deps worker mdhub ops coverage || true
echo "restore complete"
