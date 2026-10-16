# Deploying market-data-hub on a VPS

Target: one Ubuntu 24.04 VPS (4 GB RAM, 2-4 vCPU, SSD >= 80 GB) that runs PostgreSQL 16, the
scheduler (`mdhub scheduler`: all loaders, `quality`, `backfill_worker`, `maintenance`) and
the read API. Two variants: **docker compose** (recommended) and **bare metal** (systemd).

| file | purpose |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | image: Python 3.12, deps from `uv.lock`, `postgresql-client-16`, user `mdhub` (uid 10001) |
| `docker-compose.yml` | `postgres` (16, tuned, healthcheck), `migrate` (one-shot), `worker`, `api`, optional `caddy` (profile `tls`) |
| `env.example` | every environment variable, documented, no values |
| `postgres/postgresql.conf` | tuning for 4 GB RAM |
| `postgres/initdb/10-mdhub-roles.sh` | first start: read-only role + default privileges |
| `sql/readonly_defaults.sql`, `sql/grant_readonly.sql` | read-only consumer role setup / re-grant |
| `make_bundle.sh` | build machine: `dist/market-data-hub-<YYYYMMDD>.tar.gz` = code + seed dump + report + manifest |
| `restore_seed.sh` | restore the bundle's seed dump into the compose database, or any PostgreSQL 16 (`--database-url`) |
| `Caddyfile` | TLS reverse proxy for the API (Let's Encrypt) |
| `install.sh`, `systemd/` | bare-metal alternative |

## 0. Build the bundle (build machine)

One archive carries the code and a pre-seeded database. On the machine that holds the loaded
database (PostgreSQL 16, `pg_dump` 16 - the pgserver binaries in `.venv` are found
automatically):

```bash
git status                                   # commit first: only HEAD goes into the bundle
deploy/make_bundle.sh --database-url 'postgresql://postgres:@/mdhub?host=/path/to/socket'
# -> dist/market-data-hub-<YYYYMMDD>.tar.gz (+ .sha256)
```

It refuses a dirty work tree (`--allow-dirty` bundles HEAD anyway and says so in the manifest),
exports one snapshot and reads it for `pg_dump -Fc --no-owner --no-acl`, `mdhub report seed`
and the manifest (loaders may keep running), refuses if a migration checksum in the database
differs from HEAD, writes `SHA256SUMS` and checks the archive listing for forbidden paths
(`.env`, `data/`, `.handoff/`, `.venv/`, `.git/`, caches, SQLite files). Options: `--out-dir`,
`--date YYYYMMDD`; env `PG_BIN`, `MDHUB_CMD`, `MDHUB_BUNDLE_DATABASE_URL`.

Archive layout (one top-level directory `mdhub/`):

| path | content |
|---|---|
| `mdhub/` (`src/`, `deploy/`, `docs/`, `tests/`, `pyproject.toml`, `uv.lock`, ...) | `git archive HEAD` |
| `mdhub/db/mdhub.dump`, `mdhub/db/mdhub.dump.sha256` | `pg_dump -Fc --no-owner --no-acl` (pg_dump 16) and its SHA-256 |
| `mdhub/SEED_REPORT.md` | `mdhub report seed`: table counts/sizes, per-dataset coverage and missing days, gaps, backfill, last runs, bond static coverage, legacy verification |
| `mdhub/MANIFEST.txt` | git commit, migrations applied in the dump (version, checksum), pg_dump version, sizes |
| `mdhub/SHA256SUMS` | SHA-256 of every file above (`sha256sum -c`) |

## 1. Prepare the server

1. Create the VPS, add your SSH key, `apt update && apt upgrade`, set a hostname.
2. Install Docker Engine + compose plugin (docs.docker.com/engine/install/ubuntu).
3. Firewall - **only 22 and 443 open** (80 too if Caddy obtains certificates by HTTP challenge):

   ```bash
   ufw default deny incoming && ufw default allow outgoing
   ufw allow 22/tcp && ufw allow 443/tcp && ufw allow 80/tcp
   ufw enable
   ```

   PostgreSQL is **never** exposed publicly: compose publishes 5432 and 8080 on
   `127.0.0.1` only (note: Docker-published ports bypass ufw, which is why nothing else is
   published). Backend consumers on other hosts use the HTTPS API or an SSH tunnel
   (`ssh -L 5432:127.0.0.1:5432 vps`) with the read-only role.
4. Optional egress proxy for sources that need a fixed IP: set `MDHUB_HTTP_PROXY`.

## 2. Copy the archive and configure

```bash
scp dist/market-data-hub-<YYYYMMDD>.tar.gz* vps:/opt/   # archive + .sha256
ssh vps
cd /opt && sha256sum -c market-data-hub-<YYYYMMDD>.tar.gz.sha256
tar xzf market-data-hub-<YYYYMMDD>.tar.gz              # -> /opt/mdhub
cd mdhub && sha256sum -c --quiet SHA256SUMS && cat MANIFEST.txt
cd deploy
cp env.example .env && chmod 600 .env
$EDITOR .env
```

Fill in at least `POSTGRES_DB=mdhub`, `POSTGRES_USER=mdhub`, `POSTGRES_PASSWORD`
(`openssl rand -hex 24` - URL-safe, it is embedded in `MDHUB_DATABASE_URL`),
`MDHUB_RO_PASSWORD`, `MDHUB_API_TOKENS` and the source credentials you have
(`MOEX_API_KEY`, `RATESCALC_*`, ...). Leave optional knobs commented out. Backups go to
`./backups` (or `MDHUB_BACKUP_HOST_DIR`), which must be writable by uid 10001:

```bash
mkdir -p backups && sudo chown 10001:10001 backups && chmod 750 backups
```

## 3. Start

```bash
docker compose build
docker compose up -d                   # postgres -> migrate -> worker + api
docker compose ps                      # migrate: exited (0); others: running/healthy
docker compose logs -f worker
```

## 4. Restore the seed (initial history)

The archive's `db/mdhub.dump` is a `pg_dump -Fc` (made with **pg_dump 16**) of the fully
loaded database; `SEED_REPORT.md` describes what is in it. Restore it into the compose database
(drops and re-creates it):

```bash
./restore_seed.sh                  # default ../db/mdhub.dump; asks for confirmation, --yes to skip
```

It checks the dump against `db/mdhub.dump.sha256`, stops worker/api, re-creates the database,
runs `pg_restore --no-owner --no-privileges --single-transaction`, `mdhub migrate` (applies
migrations newer than the dump; refuses if an applied migration's checksum differs from the
code), verifies that `ops.schema_migration` lists every migration file of the code with the
same checksum, re-applies read-only grants, analyzes and starts everything again.

## 5. Verify

```bash
docker compose run --rm --no-deps worker mdhub db-info      # version, migrations, runs (24h)
docker compose run --rm --no-deps worker mdhub jobs list    # registered jobs + schedule
docker compose run --rm --no-deps worker mdhub run quality  # freshness / gaps now
docker compose run --rm --no-deps worker mdhub ops coverage # per-dataset first/last/rows/stale
docker compose run --rm --no-deps worker mdhub ops gaps --summary
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8080/v1/coverage
```

`ops coverage` reads `ops.v_coverage`, refreshed by the `quality` job (08:30 and 20:30 MSK on
business days). Open gaps are queued as `ops.backfill_task` and processed every 10 minutes by
`backfill_worker`. Manual backfill: `mdhub ops backfill bonds_eod --from 2026-01-05 --to
2026-01-30` (queue) or add `--now` to run inline.

## 6. TLS for the API

Point a DNS record (e.g. `mdhub.example.com`) at the VPS, set `MDHUB_API_DOMAIN` and
`ACME_EMAIL` in `.env`, then:

```bash
docker compose --profile tls up -d caddy
curl -s https://mdhub.example.com/health/live
```

Only `/health/*` and `/v1/*` are proxied; the API requires a bearer token from
`MDHUB_API_TOKENS`.

## 7. Backups

- The worker runs `maintenance mode=backup keep=14` at 02:00 MSK: `pg_dump -Fc` into
  `/var/backups/mdhub` in the container = `./backups` on the host, newest 14 kept.
  Manual: `docker compose run --rm --no-deps worker mdhub ops backup`.
- Copy backups off the server (e.g. nightly `rsync`/`restic` of `deploy/backups/` to another
  host or object storage). A backup on the same disk is not a backup.
- Restore test (do it once): `./restore_seed.sh backups/mdhub-mdhub-<stamp>.dump` on a spare
  VPS (no `.sha256` next to backups: integrity is not pre-checked), then `mdhub ops coverage`.
- Retention: `maintenance mode=retention` at 03:00 deletes intraday raw captures
  (`ops.raw_capture.endpoint` starting with `intraday:`) older than
  `MDHUB_RAW_RETENTION_DAYS_INTRADAY` (90) days and runs `VACUUM (ANALYZE)`.

## 8. Upgrading

```bash
cd /opt && tar xzf market-data-hub-<new>.tar.gz  # same directory; keeps deploy/.env and backups/
cd mdhub/deploy                                # do NOT restore the new db/mdhub.dump on upgrades
docker compose run --rm --no-deps worker mdhub ops backup   # safety dump first
docker compose build
docker compose up -d                           # migrate runs first, then worker/api restart
docker compose run --rm --no-deps worker mdhub db-info
```

Migrations are forward-only and checksummed; a failed migration stops `migrate` and the
worker/api do not start (`docker compose logs migrate`). To roll back the code, restore the
pre-upgrade dump with `restore_seed.sh` and start the previous image tag
(`MDHUB_IMAGE_TAG`).

PostgreSQL major upgrades (16 -> 17) are a separate procedure (dump/restore with the new
client); keep `postgres:16` pinned until then.

## 9. Bare metal (systemd) alternative

```bash
mkdir -p ~/release && tar xzf market-data-hub-<YYYYMMDD>.tar.gz -C ~/release   # not in /opt/mdhub
sudo ~/release/mdhub/deploy/install.sh     # MDHUB_UFW=1 to set ufw
sudo $EDITOR /etc/mdhub/mdhub.env          # credentials, API tokens
sudo systemctl restart mdhub-worker mdhub-api
```

`install.sh` installs PostgreSQL 16 from the PGDG repository (tuning in
`/etc/postgresql/16/main/conf.d/50-mdhub.conf`, `listen_addresses = 'localhost'`), uv, the
system user `mdhub`, code and venv in `/opt/mdhub` (`uv sync --frozen --no-dev`), database
`mdhub` owned by role `mdhub` (peer authentication over the unix socket:
`MDHUB_DATABASE_URL=postgresql:///mdhub?host=/var/run/postgresql`), the read-only role
`mdhub_ro` (credentials in `/etc/mdhub/readonly.credentials`, root only), `/var/backups/mdhub`
and the units `mdhub-migrate` (one-shot), `mdhub-worker` (scheduler), `mdhub-api`
(127.0.0.1:8080). `mdhub-backup.service/.timer` are an optional alternative to the
scheduler's backup rule. Re-run `install.sh` to upgrade.

Seed restore on bare metal:

```bash
sudo systemctl stop mdhub-worker mdhub-api
sudo -u postgres dropdb --force mdhub && sudo -u postgres createdb -O mdhub mdhub
sudo -u postgres psql -d mdhub -v owner=mdhub -v ro_user=mdhub_ro -v dbname=mdhub \
    -f /opt/mdhub/deploy/sql/readonly_defaults.sql
sudo install -o mdhub -g mdhub -m 0640 ~/release/mdhub/db/mdhub.dump \
    ~/release/mdhub/db/mdhub.dump.sha256 /var/backups/mdhub/
sudo -u mdhub env MDHUB_CMD=/opt/mdhub/.venv/bin/mdhub /opt/mdhub/deploy/restore_seed.sh --yes \
    --database-url 'postgresql:///mdhub?host=/var/run/postgresql' /var/backups/mdhub/mdhub.dump
sudo systemctl start mdhub-worker mdhub-api
```

`restore_seed.sh --database-url` needs an existing, empty database (it refuses otherwise) and
`pg_restore`/`psql` 16 on `PATH` (or `PG_BIN`); it runs the same restore, `mdhub migrate`,
`ops.schema_migration` check, read-only grants (when the role exists), `ANALYZE` and
`mdhub db-info` as the compose variant, without docker.

TLS: `apt install caddy`, copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, set
`MDHUB_API_DOMAIN`, `ACME_EMAIL` and `MDHUB_API_UPSTREAM=127.0.0.1:8080` for the caddy
service (`systemctl edit caddy` -> `[Service] Environment=...`), `systemctl reload caddy`.

## 10. Operations cheat sheet

| task | command (prefix with `docker compose run --rm --no-deps worker` on compose) |
|---|---|
| status | `mdhub db-info`, `mdhub ops coverage`, `mdhub ops gaps --summary` |
| run a job now | `mdhub run bonds_eod -p mode=final -p days_back=1` |
| backfill | `mdhub ops backfill <dataset> --from YYYY-MM-DD --to YYYY-MM-DD [--now]` |
| job health | `SELECT * FROM ops.v_job_health;` / latest `ops.quality_report.report` |
| backup / restore | `mdhub ops backup [--keep N]`, `mdhub ops restore <file> [--clean] --yes` |
| seed report | `mdhub report seed [--out FILE] [--as-of YYYY-MM-DD]` (read-only, Markdown) |
| psql | `docker compose exec postgres psql -U mdhub mdhub` |
