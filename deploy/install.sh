#!/usr/bin/env bash
# Bare-metal install of market-data-hub on Ubuntu 24.04 (alternative to docker compose).
#
#   sudo ./deploy/install.sh            # run from the unpacked archive / repository root
#   sudo MDHUB_UFW=1 ./deploy/install.sh   # also configure ufw: allow 22, 80, 443 only
#
# Idempotent: re-running upgrades the code in /opt/mdhub, re-syncs the venv from uv.lock,
# applies migrations and restarts the services. It never overwrites /etc/mdhub/mdhub.env.
#
# Installs: PostgreSQL 16 (PGDG apt repo; tuned via conf.d, listening on localhost only),
# uv, system user `mdhub`, code + venv in /opt/mdhub, database `mdhub` owned by role `mdhub`
# (peer authentication over the unix socket - no password), read-only role `mdhub_ro`
# for backend consumers (password in /etc/mdhub/readonly.credentials, root-only),
# /var/backups/mdhub, systemd units mdhub-migrate/mdhub-worker/mdhub-api.
set -euo pipefail

UV_VERSION="${UV_VERSION:-0.12.3}"
PG_MAJOR=16
APP_USER=mdhub
APP_HOME=/opt/mdhub
DB_NAME="${MDHUB_DB_NAME:-mdhub}"
RO_USER="${MDHUB_RO_USER:-mdhub_ro}"
ETC_DIR=/etc/mdhub
BACKUP_DIR=/var/backups/mdhub
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"
[[ -f "$SRC_DIR/pyproject.toml" && -f "$SRC_DIR/uv.lock" ]] || die "run from the mdhub archive"
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    echo "warning: tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}" >&2
fi

export DEBIAN_FRONTEND=noninteractive

log "base packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
    ca-certificates curl gnupg rsync openssl python3.12 python3.12-venv tzdata

log "PostgreSQL ${PG_MAJOR} (PGDG)"
if [[ ! -f /etc/apt/sources.list.d/pgdg.list ]]; then
    install -d -m 0755 /usr/share/postgresql-common/pgdg
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    apt-get update -q
fi
apt-get install -y -q "postgresql-${PG_MAJOR}" "postgresql-client-${PG_MAJOR}"

conf_dir="/etc/postgresql/${PG_MAJOR}/main/conf.d"
install -d -m 0755 "$conf_dir"
install -m 0644 "$SRC_DIR/deploy/postgres/postgresql.conf" "$conf_dir/50-mdhub.conf"
cat > "$conf_dir/60-mdhub-local.conf" <<'EOF'
# Bare metal: never listen on public interfaces (consumers use SSH tunnels or the API).
listen_addresses = 'localhost'
EOF
systemctl enable postgresql >/dev/null
systemctl restart postgresql

log "uv ${UV_VERSION}"
if ! command -v uv >/dev/null || [[ "$(uv --version | awk '{print $2}')" != "$UV_VERSION" ]]; then
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
        | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
fi

log "user ${APP_USER} and directories"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$APP_HOME"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$BACKUP_DIR"
install -d -o root -g "$APP_USER" -m 0750 "$ETC_DIR"

log "code -> ${APP_HOME}"
# The bundle's db/ (seed dump) and dist/ are not copied; unpack new bundles outside ${APP_HOME}
# (re-running ${APP_HOME}/deploy/install.sh itself only re-syncs venv, migrations and units).
[[ "$SRC_DIR" == "$APP_HOME" ]] || rsync -a --delete \
    --exclude '/.venv/' --exclude '/.cache/' --exclude '.git/' --exclude '/data/' \
    --exclude '.handoff/' --exclude 'deploy/.env' --exclude 'deploy/backups/' \
    --exclude 'deploy/seed/' --exclude '__pycache__/' \
    --exclude '/db/' --exclude '/dist/' \
    "$SRC_DIR/" "$APP_HOME/"
chown -R "$APP_USER:$APP_USER" "$APP_HOME"

log "virtualenv (uv sync --frozen --no-dev)"
(
    cd "$APP_HOME"
    sudo -u "$APP_USER" env HOME="$APP_HOME" UV_CACHE_DIR="$APP_HOME/.cache/uv" \
        UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
        uv sync --frozen --no-dev --no-editable --python /usr/bin/python3.12
)

log "database ${DB_NAME}, roles ${APP_USER} (owner) and ${RO_USER} (read-only)"
psql_admin() { sudo -u postgres psql -q -v ON_ERROR_STOP=1 "$@"; }
psql_admin -v owner="$APP_USER" -v ro_user="$RO_USER" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'owner')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner')
\gexec
SELECT format('CREATE ROLE %I NOLOGIN', :'ro_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user')
\gexec
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = '120s';
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1; then
    sudo -u postgres createdb -O "$APP_USER" -E UTF8 -T template0 "$DB_NAME"
fi
psql_admin -d "$DB_NAME" -v owner="$APP_USER" -v ro_user="$RO_USER" -v dbname="$DB_NAME" \
    -f "$APP_HOME/deploy/sql/readonly_defaults.sql"

ro_cred="$ETC_DIR/readonly.credentials"
if [[ ! -f "$ro_cred" ]]; then
    ro_password="$(openssl rand -hex 24)"
    psql_admin -v ro_user="$RO_USER" -v ro_password="$ro_password" <<'SQL'
ALTER ROLE :"ro_user" LOGIN PASSWORD :'ro_password';
SQL
    umask 077
    printf 'PGHOST=127.0.0.1\nPGPORT=5432\nPGDATABASE=%s\nPGUSER=%s\nPGPASSWORD=%s\n' \
        "$DB_NAME" "$RO_USER" "$ro_password" > "$ro_cred"
    chmod 0600 "$ro_cred"
    unset ro_password
    echo "read-only credentials written to $ro_cred (root only)"
fi

env_file="$ETC_DIR/mdhub.env"
if [[ ! -f "$env_file" ]]; then
    log "creating ${env_file} from env.example - fill in credentials"
    sed -e "s|^MDHUB_DATABASE_URL=.*|MDHUB_DATABASE_URL=postgresql:///${DB_NAME}?host=/var/run/postgresql|" \
        -e "s|^MDHUB_BACKUP_DIR=.*|MDHUB_BACKUP_DIR=${BACKUP_DIR}|" \
        "$APP_HOME/deploy/env.example" > "$env_file"
fi
chown root:"$APP_USER" "$env_file"
chmod 0640 "$env_file"

log "migrations"
sudo -u "$APP_USER" bash -c "set -a; . '$env_file'; set +a; '$APP_HOME/.venv/bin/mdhub' migrate"
psql_admin -d "$DB_NAME" -v ro_user="$RO_USER" -c "SET ROLE ${APP_USER}" \
    -f "$APP_HOME/deploy/sql/grant_readonly.sql" >/dev/null

log "systemd units"
install -m 0644 "$APP_HOME"/deploy/systemd/*.service "$APP_HOME"/deploy/systemd/*.timer \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable mdhub-migrate.service mdhub-worker.service >/dev/null
systemctl restart mdhub-migrate.service mdhub-worker.service
if sudo -u "$APP_USER" "$APP_HOME/.venv/bin/python" -c "import mdhub.api.app" 2>/dev/null; then
    systemctl enable mdhub-api.service >/dev/null
    systemctl restart mdhub-api.service
else
    echo "note: mdhub.api.app not importable yet - mdhub-api.service not started" >&2
fi

if [[ "${MDHUB_UFW:-0}" == 1 ]]; then
    log "ufw: allow 22, 80, 443"
    apt-get install -y -q ufw
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
fi

log "done"
sudo -u "$APP_USER" bash -c "set -a; . '$env_file'; set +a; '$APP_HOME/.venv/bin/mdhub' db-info" || true
cat <<EOF

Next steps:
  1. Fill in credentials in ${env_file}, then: systemctl restart mdhub-worker mdhub-api
  2. Optional seed restore (db/mdhub.dump of the bundle): see README_DEPLOY.md, section
     "Bare metal" (re-create the database, then deploy/restore_seed.sh --database-url ...)
  3. TLS: install caddy and use ${APP_HOME}/deploy/Caddyfile (see README_DEPLOY.md)
EOF
