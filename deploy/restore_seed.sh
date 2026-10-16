#!/usr/bin/env bash
# Restore a seed dump (pg_dump -Fc, made by pg_dump 16) into the compose PostgreSQL.
#
#   cd deploy && ./restore_seed.sh [--yes] [seed/mdhub-seed.dump]
#
# Steps: start postgres, stop worker/api, DROP and re-create the database (all data in it is
# lost), pg_restore (--no-owner --no-privileges, single transaction), re-apply read-only
# defaults/grants, run pending migrations, start everything again.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

assume_yes=0
dump=""
for arg in "$@"; do
    case "$arg" in
        -y|--yes) assume_yes=1 ;;
        -h|--help)
            sed -n '2,9p' "$0"
            exit 0
            ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) dump="$arg" ;;
    esac
done
dump="${dump:-seed/mdhub-seed.dump}"

die() { echo "restore_seed: $*" >&2; exit 1; }

[[ -f .env ]] || die "deploy/.env not found (cp env.example .env and fill it in)"
[[ -f "$dump" ]] || die "seed dump not found: $dump"
command -v docker >/dev/null || die "docker is not installed"

compose() { docker compose "$@"; }
in_pg() { compose exec -T postgres "$@"; }

echo "==> starting postgres"
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

echo "==> stopping worker and api"
compose stop worker api >/dev/null 2>&1 || true

echo "==> re-creating database ${db_name}"
in_pg sh -c 'dropdb --if-exists --force -U "$POSTGRES_USER" "$POSTGRES_DB" \
    && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" -E UTF8 "$POSTGRES_DB"'
in_pg sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v owner="$POSTGRES_USER" -v ro_user="${MDHUB_RO_USER:-mdhub_ro}" -v dbname="$POSTGRES_DB" \
    -f /mdhub-sql/readonly_defaults.sql \
    -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'

echo "==> restoring $(du -h "$dump" | cut -f1) from ${dump} (this can take a while)"
in_pg sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges \
    --exit-on-error --single-transaction' < "$dump"

echo "==> applying pending migrations"
compose run --rm migrate

echo "==> re-applying read-only grants"
in_pg sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v ro_user="${MDHUB_RO_USER:-mdhub_ro}" -f /mdhub-sql/grant_readonly.sql' >/dev/null

echo "==> analyzing"
in_pg sh -c 'vacuumdb -U "$POSTGRES_USER" -d "$POSTGRES_DB" --analyze-only --quiet'

echo "==> starting all services"
compose up -d

echo "==> verification"
compose run --rm --no-deps worker mdhub db-info
compose run --rm --no-deps worker mdhub ops coverage || true
echo "restore complete"
