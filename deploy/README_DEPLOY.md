# Deploying market-data-hub on a VPS

Target: one Ubuntu 24.04 VPS (4 GB RAM, 2-4 vCPU, SSD >= 80 GB) that runs PostgreSQL 16, the
scheduler (`mdhub scheduler`: all loaders, `quality`, `backfill_worker`, `maintenance`) and
the read API. Two variants: **docker compose** (recommended) and **bare metal** (systemd).

| file | purpose |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | image: Python 3.12, deps from `uv.lock`, `postgresql-client-16`, user `mdhub` (uid 10001) |
| `docker-compose.yml` | `postgres` (16, tuned, healthcheck), `migrate` (one-shot), `worker`, `api`, `admin` (console, not published), `updater` (self-update, §13), `hostmon` (host sampler, §15), optional `caddy` (profile `tls`) |
| `env.example` | every environment variable, documented, no values |
| `postgres/postgresql.conf` | tuning for 4 GB RAM |
| `postgres/initdb/10-mdhub-roles.sh` | first start: read-only role + default privileges, service roles (§16) |
| `sql/readonly_defaults.sql`, `sql/grant_readonly.sql` | read-only consumer role setup / re-grant |
| `sql/service_roles.sql` | login roles of api, admin, hostmon (§16) |
| `make_bundle.sh` | build machine: `dist/market-data-hub-<YYYYMMDD>.tar.gz` = code + seed dump + report + manifest; `--code-only`: the signed release bundle (§11) |
| `release-keys/` | pinned minisign public keys that `mdhub release verify` accepts (§11) |
| `restore_seed.sh` | restore the bundle's seed dump into the compose database, or any PostgreSQL 16 (`--database-url`) |
| `Caddyfile` | TLS reverse proxy for the API and the admin console with its IP allow-list (Let's Encrypt) |
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
- Back up `deploy/.env` separately (it holds `MDHUB_SECRETS_PRIVATE_KEY`, §14), e.g. in the
  owner's password manager, never next to the database backups. Database backups contain the
  source credentials only as ciphertext; they open only with that key.
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

With the release layout of §13 the console does all of this (Updates page): verify, backup,
build, drain, migrate, restart, check and automatic rollback.

Upgrading to a release with migration `0800` changes nothing until you set the service role
passwords (§16): api, admin and hostmon keep the owner role and log a warning.

Check a release before unpacking it: `mdhub release verify market-data-hub-X.Y.Z.tar.gz` (§11).
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
sudo -u postgres dropdb --force mdhub && sudo -u postgres createdb -O mdhub -E UTF8 -T template0 mdhub
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
| console users | `mdhub admin create-user LOGIN --role admin`, `mdhub admin list / reset / unlock / disable / enable LOGIN` (§12) |

## 11. Release bundles and signing

A release is a **code-only bundle** signed with an ed25519 key in minisign format
(ADMIN_DESIGN §6.1). The tag `vX.Y.Z` triggers `.github/workflows/release.yml`, which publishes
three files on the GitHub Release:

| file | content |
|---|---|
| `market-data-hub-X.Y.Z.tar.gz` | `mdhub/`: `git archive` of the tag, `MANIFEST.json`, `SHA256SUMS`; no seed dump |
| `market-data-hub-X.Y.Z.SHA256SUMS` | copy of `mdhub/SHA256SUMS` (SHA-256 of every other file in the archive) |
| `market-data-hub-X.Y.Z.SHA256SUMS.minisig` | minisign signature over it, trusted comment `market-data-hub X.Y.Z <commit>` |

`MANIFEST.json` holds `version`, `commit`, `created_at`, `min_previous_version` (from
`[tool.mdhub.release]` in `pyproject.toml`: the oldest installed version this release can be
installed over) and every migration with the checksum `mdhub migrate` records.

### Build locally

```bash
deploy/make_bundle.sh --code-only --version 0.2.0    # or tag HEAD v0.2.0 first
# -> dist/market-data-hub-0.2.0.tar.gz, dist/market-data-hub-0.2.0.SHA256SUMS
```

No database is needed. The version is `--version`, else the `v*` tag on HEAD, else the installed
package version (`importlib.metadata`); it must be `X.Y.Z` or `X.Y.Z{a|b|rc}N`. The dirty-tree
rule and the forbidden-path check are the same as for the full bundle, and the archive must not
contain `db/`, `SEED_REPORT.md` or `MANIFEST.txt`. A rebuild deletes a stale `.minisig`.

### Verify

```bash
mdhub release verify market-data-hub-X.Y.Z.tar.gz   # .SHA256SUMS.minisig next to it
mdhub release verify ARCHIVE --signature FILE.minisig --keys-dir deploy/release-keys --json
mdhub release keys                                  # the pinned keys
```

`mdhub release verify` is pure Python (no minisign binary on the server). It checks, failing
closed:

1. the archive layout: only regular files and directories under `mdhub/`; no links, devices,
   absolute paths, `..`, duplicate members or setuid bits; at most 1 GiB unpacked;
2. the signature over the archive's own `mdhub/SHA256SUMS` against the pinned keys;
3. every file's hash: none missing, none unlisted, none different;
4. the manifest: format, project name, version, commit, `min_previous_version <= version`, and
   migrations that match `src/mdhub/db/migrations/*.sql` in the archive exactly.

Exit status 0 means verified, 1 refused, 2 a usage or key-list error. An unsigned archive or a
signature by an unknown key is refused. `--allow-unsigned` is the only override, it exists on
the CLI only, and it prints a warning. It still checks every hash and the manifest, and never
accepts a signature by a pinned key that does not verify, because that means tampering.

The pinned keys are `deploy/release-keys/*.pub` of the **installed** version, never the keys
inside the archive under test. The image carries them in `/app/release-keys`
(`MDHUB_RELEASE_KEYS_DIR`). In a source checkout the default is `deploy/release-keys/`. On
compose:

```bash
docker compose run --rm --no-deps -v "$HOME/dl:/dl:ro" worker \
    mdhub release verify /dl/market-data-hub-X.Y.Z.tar.gz
```

Without mdhub (any machine with minisign):

```bash
minisign -V -p deploy/release-keys/<KEYID>.pub -m market-data-hub-X.Y.Z.SHA256SUMS
mkdir x && tar xzf market-data-hub-X.Y.Z.tar.gz -C x
cmp x/mdhub/SHA256SUMS market-data-hub-X.Y.Z.SHA256SUMS && (cd x/mdhub && sha256sum -c --quiet SHA256SUMS)
```

### Generate the signing key (once, on the owner's machine)

```bash
brew install minisign                       # or: apt install minisign
minisign -G -p mdhub-release.pub -s mdhub-release.key   # asks for a strong password
id="$(sed -n 's/^untrusted comment: minisign public key //p' mdhub-release.pub)"
cp mdhub-release.pub "deploy/release-keys/$id.pub"   # commit the public key only
```

Keep `mdhub-release.key` and its password offline, for example in a password manager and on an
encrypted USB stick. Never commit them, and never sign with `minisign -W` (an unencrypted key).

### The protected environment `release`

Repository **Settings -> Environments -> New environment** `release`:

- **Required reviewers**: the owner (and a second maintainer when there is one). The `sign` job
  waits for an approval in the Actions tab before it can read the secrets.
- **Deployment branches and tags**: *Selected branches and tags*, add the tag pattern `v*`.
  Nothing else may deploy to `release`.
- **Environment secrets** (environment secrets, not repository secrets):

```bash
gh secret set MINISIGN_SECRET_KEY --env release < mdhub-release.key
gh secret set MINISIGN_PASSWORD --env release          # prompts; paste the key password
```

Also add a tag ruleset (**Settings -> Rules -> Rulesets**, target tags `v*`) so that only admins
can create, update or delete release tags.

Only the `sign` job reads the secrets. It downloads the build artifact, installs `minisign` from
Ubuntu and runs `minisign -S`. It has no checkout, no Python, no project dependencies and no
token permissions. `verify` then checks the result against the pinned keys of the tagged
commit, and only `publish` can write, to create the Release. The workflow pins every action
to a commit SHA.

Plan limits: required reviewers on a **private** repository need a plan that offers them
(GitHub Enterprise at the time of writing). Without them, keep the environment with its tag rule
and the tag ruleset, or sign offline:

```bash
gh run download <run-id> -n bundle -D dist           # from the release run, after `build`
minisign -S -s mdhub-release.key -m dist/market-data-hub-X.Y.Z.SHA256SUMS -t "market-data-hub X.Y.Z $(git rev-parse vX.Y.Z)"
mdhub release verify dist/market-data-hub-X.Y.Z.tar.gz
gh release create vX.Y.Z --verify-tag dist/market-data-hub-X.Y.Z.tar.gz \
    dist/market-data-hub-X.Y.Z.SHA256SUMS dist/market-data-hub-X.Y.Z.SHA256SUMS.minisig
```

### Rotating the key

Servers trust only the keys of the version they run, so a new key reaches them inside a release
signed by a key they already trust:

1. Generate the new key pair as above and commit its `.pub` **next to** the old one.
2. Release N, still signed with the old key. Servers that install N trust both keys.
3. Replace `MINISIGN_SECRET_KEY` and `MINISIGN_PASSWORD` in the `release` environment with the
   new key. Release N+1 is signed with the new key.
4. Once every server runs N or later, delete the old `.pub` in a later release.

If the secret key leaks, delete its `.pub` and release immediately with a trusted key. On
servers that cannot be updated in time, point `MDHUB_RELEASE_KEYS_DIR` at a directory without
the leaked key. A lost key without a second pinned key cannot be rotated through the updater:
install the next release by hand (§8) after checking it with minisign and the new public key.

## 12. Admin console

The console (ADMIN_DESIGN §4-5) is the `admin` service: `uvicorn mdhub.admin.app:app` on
port 8081 inside the compose network, reachable only through Caddy on its own host name. It
shows jobs, data coverage and gaps, sources, API traffic, tokens, users and the audit trail;
operators can request a job run, retry a failed backfill task and mark a gap not expected.
The console holds no source credentials: compose gives it an explicit environment (database
URL, `MDHUB_ADMIN_*`, log level, time zone) instead of `.env`, and a requested run is a row in
`ops.job_request` that the worker picks up within about 5 seconds and runs like any other run
(trigger `admin` in `ops.job_run`). A worker that dies mid-run has its requests requeued once
by the next worker.

1. DNS: point `admin.example.com` at the VPS.
2. `.env`: `MDHUB_ADMIN_DOMAIN=admin.example.com`, `MDHUB_ADMIN_ALLOW_CIDRS="203.0.113.7/32"`
   (your office / home IPs, space-separated; a dynamic home IP needs a wider range or a
   WireGuard peer), `MDHUB_ADMIN_SECRET_KEY=$(openssl rand -hex 32)`. Compose sets
   `MDHUB_ADMIN_TRUSTED_PROXIES` to Caddy's fixed address on the `admin_edge` network.
3. Start: `docker compose up -d admin && docker compose --profile tls up -d caddy`.
4. First user (no user or password exists after installation):

   ```bash
   docker compose run --rm --no-deps admin mdhub admin create-user alice --role admin --name "Alice"
   ```

   It prints a one-time link valid for 15 minutes. Open it from an allowed IP, set a password
   (12-128 characters, common passwords refused) and register a passkey. Then add an
   authenticator app and recovery codes on the Account page as a fallback. Invite further
   users from the Users page.

Security model: the allow-list is checked by Caddy and again by the app (an empty list
refuses everything); passwords are argon2id; login needs password + passkey (or TOTP or a
recovery code), or a passkey alone when the user turns that on; sessions are server-side
(cookie `__Host-mdhub_admin`, idle 30 min, absolute 12 h); 10 failed attempts lock a login
for 15 minutes (`mdhub admin unlock LOGIN`); tokens and users need a fresh passkey/TOTP check
(step-up, 5 min). Every action lands in `admin.audit_log`, which cannot be updated or deleted.

Lost factors: `mdhub admin reset LOGIN` prints a reset link (new password and passkey; old
factors are removed when the link is used). Key rotation: `mdhub admin rotate-key` re-encrypts
the TOTP secrets and prints the new `MDHUB_ADMIN_SECRET_KEY`; put it into `.env` and
`docker compose up -d admin`. Changing `MDHUB_ADMIN_DOMAIN` invalidates every passkey
(they are bound to the host name): reset each user afterwards.

Bare metal: run `uvicorn mdhub.admin.app:app --host 127.0.0.1 --port 8081 --no-proxy-headers`
under systemd with its own minimal environment file (not `/etc/mdhub/mdhub.env`: no source
credentials) and `MDHUB_ADMIN_TRUSTED_PROXIES=127.0.0.1`, and add
the console site of the `Caddyfile` with `MDHUB_ADMIN_UPSTREAM=127.0.0.1:8081`.

Local try-out (no TLS): `MDHUB_ADMIN_DOMAIN=localhost MDHUB_ADMIN_ORIGIN=http://localhost:8081
MDHUB_ADMIN_ALLOW_CIDRS=127.0.0.1/32 uvicorn mdhub.admin.app:app --port 8081`, then open
`http://localhost:8081` (Chromium-based browsers and Firefox treat localhost as secure, so the
`__Host-` cookie and passkeys work).

## 13. Self-update from the console (updater)

The `updater` service (`mdhub updater`, ADMIN_DESIGN §6) installs signed releases from GitHub
or from a file uploaded on the console's Updates page, and rolls back. The console only queues
requests (`ops.update_request`); the updater is the only container with the docker socket and
the GitHub token, has no port, and verifies every archive against the keys pinned in the
running image (§11) before it touches anything.

### Layout

The updater needs every release in its own directory and the shared files outside them:

| path (`MDHUB_UPDATE_ROOT`, default `/opt/mdhub`) | content |
|---|---|
| `releases/X.Y.Z/` | unpacked bundles (the `mdhub/` directory of each archive) |
| `current` | symlink to the running release; always run compose from `current/deploy` |
| `archives/` | verified bundles and signatures (downloads and copied uploads) |
| `shared/.env` | the `.env`, linked as `deploy/.env` into every release |
| `shared/updater.env` | `MDHUB_UPDATE_GITHUB_TOKEN=...` (optional; only the updater reads it) |
| `shared/backups/` | nightly dumps and the `pre-update-*.dump` files (never rotated: delete old ones by hand) |
| `shared/updater-journal.json` | exists only while a restore runs (see recovery) |

Move an existing installation (§2-3) to it once, with the first release that has the updater
(`$V`, verified as in §11):

```bash
cd /opt/mdhub/deploy && docker compose run --rm --no-deps worker mdhub ops backup
docker compose --profile tls down                      # volumes stay (project name mdhub)
sudo mv /opt/mdhub /opt/mdhub-old
sudo mkdir -p /opt/mdhub/releases /opt/mdhub/archives /opt/mdhub/shared
sudo tar xzf /opt/market-data-hub-$V.tar.gz -C /opt/mdhub/releases
sudo mv /opt/mdhub/releases/mdhub /opt/mdhub/releases/$V
sudo cp /opt/market-data-hub-$V.* /opt/mdhub/archives/
sudo mv /opt/mdhub-old/deploy/.env /opt/mdhub/shared/.env
sudo mv /opt/mdhub-old/deploy/backups /opt/mdhub/shared/backups
sudo ln -s /opt/mdhub/shared/.env /opt/mdhub/releases/$V/deploy/.env
sudo ln -s releases/$V /opt/mdhub/current
sudoedit /opt/mdhub/shared/.env   # MDHUB_IMAGE_TAG=$V, MDHUB_BACKUP_HOST_DIR=/opt/mdhub/shared/backups
cd /opt/mdhub/current/deploy && docker compose build && docker compose up -d
docker compose --profile tls up -d caddy               # the Caddyfile with the upload limit
```

GitHub releases (optional; uploads work without): create a fine-grained token with
**Contents: read** on this one repository only, then

```bash
echo 'MDHUB_UPDATE_GITHUB_TOKEN=github_pat_...' | sudo tee /opt/mdhub/shared/updater.env
sudo chmod 600 /opt/mdhub/shared/updater.env
sudo ln -s /opt/mdhub/shared/updater.env /opt/mdhub/current/deploy/updater.env
docker compose up -d updater
```

`env_file` entries with `required: false` need docker compose 2.24 or newer on the host.
The token goes through `MDHUB_HTTP_PROXY` when set, and only to `api.github.com` (the asset
download redirect does not carry it). Never put it into `.env`: worker, api and migrate read
all of `.env`.

### What an update does

An admin with a fresh step-up picks a verified GitHub release ("Review and install") or uploads
`market-data-hub-X.Y.Z.tar.gz` plus its `.SHA256SUMS.minisig` (200 MB at most; Caddy allows
that size on `/updates/upload` only). The confirmation page lists the release's new
migrations: with migrations, any rollback means restoring the pre-update dump. The updater then
runs, logging each step on the page (refreshed every 2 s) and in `admin.audit_log`:

1. **verifying**: signature, every file hash, manifest; newer than the running version and at
   least its `min_previous_version`; migration checksums equal the applied ones;
2. **backing_up**: `pg_dump` to `shared/backups/pre-update-<db>-<from>-to-<to>-<stamp>.dump`;
3. **staging**: unpack to `releases/X.Y.Z`, `docker compose build` as `mdhub:X.Y.Z`;
4. **draining**: `ops.worker_control.drain`: the worker finishes running jobs (at most 10
   minutes) and starts no scheduled run or console request; then the worker is stopped.
   Skipped scheduled fires are recorded as `lock_busy` and caught up by `quality`;
5. **migrating**: `docker compose run --rm migrate` with the new image;
6. **starting**: `MDHUB_IMAGE_TAG=X.Y.Z` in `shared/.env`, `current` -> `releases/X.Y.Z`,
   `docker compose up -d --no-deps worker api admin`;
7. **checking** (3 minutes at most): `/health/ready` reports X.Y.Z, a worker heartbeat reports
   X.Y.Z and `/v1/coverage` answers a short-lived read token;
8. **done**: the updater starts its successor (a one-off container recreates the `updater`
   service) and exits.

A failure in steps 1-3 changes nothing (`failed`). A later one rolls back automatically
(`rolled_back`): when no migration ran, the previous tag and symlink come back within seconds;
when one did, the services stop, the pre-update dump is restored (the database is dropped and
re-created; the update and audit trail are kept), then the previous version starts. Data
loaded after the backup is lost; backfill reloads it. "Roll back to X" on the page does the
same for the version before the current one; across migrations it restores the dump taken
before the current version was installed.

The updater resumes an interrupted request at the recorded step when it starts again. Only one
update runs at a time (an advisory lock, and the console refuses a second request).

### Manual recovery

- **Request `failed` with "rollback failed"**: the message says which step failed. Put the
  previous version back by hand: `ln -sfn releases/<prev> /opt/mdhub/current`,
  `MDHUB_IMAGE_TAG=<prev>` in `shared/.env`, restore the `pre-update-*.dump` named in the log
  when migrations ran (`docker compose run --rm --no-deps worker mdhub ops restore <file>
  --clean --yes` or §4), `UPDATE ops.worker_control SET drain = false;`, then
  `cd /opt/mdhub/current/deploy && docker compose up -d`.
- **`shared/updater-journal.json` exists**: a restore was interrupted; the updater redoes it
  at start (logs: `docker compose logs updater`). Delete the file only after restoring the
  database yourself.
- **Updater down**: requests stay `queued` (the page warns when no updater heartbeat exists);
  cancel them on the page.

### Rehearsal on a throw-away VPS

Do this once before relying on it in production, and again when the updater changes. Never on
the production server, and never with the production signing key.

1. Generate a rehearsal key (`minisign -G -p rehearsal.pub -s rehearsal.key`) and commit
   `rehearsal.pub` as `deploy/release-keys/<KEYID>.pub` on a throw-away branch. From that branch
   build and sign (`make_bundle.sh --code-only --version ...`, `minisign -S -s rehearsal.key -m
   dist/market-data-hub-X.Y.Z.SHA256SUMS`) five bundles:
   - **A** `9.0.0`: the branch as is;
   - **B** `9.1.0`: plus a migration `0690_rehearsal.sql` (`CREATE TABLE ops.rehearsal (x int);`);
   - **C** `9.2.0`: A plus a broken API (`raise RuntimeError` at the top of
     `src/mdhub/api/app.py`), no new migration;
   - **D** `9.3.0`: B plus the broken API (new migration *and* failing check);
   - **E** `9.4.0`: B with one file changed after signing (repack the archive: tampered).
2. Fresh Ubuntu VPS: §1, then §2-3 with bundle A directly in the §13 layout, load a little
   data (`mdhub run cbr`, `mdhub run quality`), create an admin (§12), allow-list your IP.
3. **Install with a migration**: upload B; the confirmation shows `0690_rehearsal`; install.
   Expect `done`; `ops.release`: 9.1.0 active, 9.0.0 previous; `docker compose ps` shows
   `mdhub:9.1.0` for worker, api, admin and (after a few seconds) updater; the Overview shows
   no version mismatch.
4. **Drain**: before step 3, start a long job from the Jobs page ("Run now"); the update log
   must show "waiting for running jobs" and continue only after it finished.
5. **Rollback across a migration**: "Roll back to 9.0.0". Expect `done`, `ops.rehearsal` gone,
   `ops.update_log` and `admin.audit_log` still holding the 9.1.0 install.
6. **Automatic rollback without migration**: install C. Expect `rolled_back` within about
   3 minutes, back on 9.0.0 (tag switch only, no restore in the log).
7. **Automatic rollback with restore**: install B again, then D. Expect D `rolled_back` with
   "restoring the pre-update dump", back on 9.1.0 with `ops.rehearsal` present and data
   loaded before the D backup intact.
8. **Refusals** (each ends at verifying, nothing restarts): upload E (hash mismatch), A's archive
   without its signature (unsigned), A signed with another key (unknown key), and A while 9.1.0
   runs (older version).
9. **Crash and resume**: install C again and run `docker compose kill updater` while the log
   shows `staging`, then `docker compose up -d updater`: the log shows "resuming at staging"
   and the request completes. Repeat with D, killing the updater while it restores; on restart
   it redoes the restore (journal) and finishes `rolled_back`.
10. **GitHub**: push the rehearsal bundles as releases of a private test repository, point
    `MDHUB_UPDATE_GITHUB_REPO` at it with a token for that repository, and check that the page
    lists them with notes, verified, and installs one. Confirm with `tcpdump`/proxy logs that
    the token only goes to `api.github.com` (and through `MDHUB_HTTP_PROXY` when set).
11. **Upload limit**: a 250 MB file is refused by Caddy (413) before it reaches the console.
12. Destroy the VPS and the rehearsal key.

## 14. Source credentials store

Source credentials (`MOEX_API_KEY`, `MOEX_PASSPORT_*`, `RATESCALC_*`, `CBONDS_*`,
`OTC_MONITOR_TOKEN`, `MDHUB_HTTP_PROXY`) can be set, replaced and removed on the console page
**Credentials** (admin role and step-up) instead of `.env` (ADMIN_DESIGN §9). The console seals
each value to the worker's public key and stores only the ciphertext in
`admin.source_credential`: neither the console nor a database backup can read a value. The
worker decrypts at the start of each job run; a console value wins over `.env`, and a change is
used from the next run (NOTIFY plus a 60 s poll, no restart). Nothing is ever shown back.

Set up once:

```bash
docker compose run --rm --no-deps worker mdhub secrets init-key   # prints MDHUB_SECRETS_PRIVATE_KEY=...
```

Put the printed line into `deploy/.env` and `docker compose up -d worker`. Compose passes the
key to the worker only (it is blanked for `migrate` and `api`; `admin` gets no `.env` at all).
On start the worker publishes its public key; the Credentials page then accepts values.

Move from `.env` to the store:

```bash
docker compose exec worker mdhub secrets import-env   # seals every credential set in .env
docker compose exec worker mdhub secrets list         # names and state, never values
```

Then empty those lines in `.env` and `docker compose up -d worker api` (the API does not use
them). `list` and the page show per name: not set, console, .env fallback, or both (console
wins).

- **Back up the key.** Keep a copy of `deploy/.env` (or at least the
  `MDHUB_SECRETS_PRIVATE_KEY` line) outside the server, separate from the database backups.
  If the key is lost, run `init-key` again and enter the credentials again; data and history
  are unaffected. Rows sealed to an old key show "re-enter" and are never used.
- **Rotate the key:** `docker compose exec worker mdhub secrets rotate-key` re-encrypts every
  stored value and prints the new key line. Replace it in `.env` and restart the worker right
  away (until then jobs fall back to `.env` values).
- **Check** on the page queues a `source_probe` of that source for the worker; the result
  appears in the "Last check" column.
- Bundles never contain credentials: the code is `git archive`, `.env` is a forbidden path, and
  the seed dump of `make_bundle.sh` excludes the data of every `admin.*` table (the bundle is
  refused if its dump has any; check an existing dump with
  `deploy/make_bundle.sh --check-dump FILE`).
- Bare metal (systemd): give the key to the worker only. Write the line into
  `/etc/mdhub/worker.env` (`root:root`, mode `0600`) and add it with `systemctl edit
  mdhub-worker`: `[Service]` `EnvironmentFile=/etc/mdhub/worker.env`. Not into
  `/etc/mdhub/mdhub.env` (the API reads that file too), and not as `Environment=` in a
  drop-in (drop-in files are world-readable).

## 15. Machine monitoring

The console's **Machine** page (ADMIN_DESIGN §10) shows CPU (including steal and iowait),
memory, disks, network, uptime, the clock offset, PostgreSQL statistics and containers, with
ok/warn/crit states that also feed the Overview lights.

- **`hostmon`** (`mdhub hostmon`) samples the host every 30 s into `ops.host_metric` and rolls
  it up to `ops.host_metric_5m` every 5 minutes. It reads the host through read-only mounts
  (`/proc` -> `/host/proc`, `/sys` -> `/host/sys`, `/` -> `/rootfs`), runs on the host network
  (so it sees the host's interfaces and reaches PostgreSQL on `127.0.0.1:${MDHUB_PG_HOST_PORT}`),
  with a read-only root file system and no capabilities. It has no docker socket, no `env_file`
  and no port: its environment is `MDHUB_DATABASE_URL`, `MDHUB_HOSTMON_*` and
  `MDHUB_NTP_SERVER` only.
- The **worker** samples `pg_stat_*` every minute (sizes, connections, longest transaction
  and query, lock waits, cache hit ratio, dead tuples, autovacuum, WAL rate, checkpoints).
- **Containers** come from the updater (phase D), the only process with the docker socket;
  until it writes `ops.container_status` the page says "updater not reporting".
- Retention (nightly `maintenance mode=retention`): raw samples 7 days, rollup 90 days.

Settings (`.env`, see `env.example`): `MDHUB_HOSTMON_MOUNTS` lists the file systems to show as
host paths (default `/`; add the docker data root and the backup directory when they are
separate file systems), `MDHUB_NTP_SERVER` (default `pool.ntp.org`, `off` disables the clock
check; UDP 123 outbound must be allowed) and the `MDHUB_HOSTMON_*` thresholds. After a change:
`docker compose up -d hostmon admin`.

Check what the sampler sees (prints one sample, writes nothing):

```bash
docker compose run --rm --no-deps hostmon mdhub hostmon --once
```

Reboot-required and pending security updates are read from `/run/reboot-required` and
`/var/lib/update-notifier/updates-available` (Debian/Ubuntu with `update-notifier-common`);
on other hosts those rows are absent. Bare metal: run `mdhub hostmon` under systemd with the
same minimal environment; it then reads `/proc` and `/` directly.

## 16. Database roles

The services that face the internet or only sample do not connect as the owner role: a
compromised console, API or sampler can then write only what its code writes (migration
`0800_service_roles`, `admin.grant_service_roles()`). migrate, worker and updater stay on the
owner role (migrations, every loader, `pg_dump`/`pg_restore`).

| role | service | may |
|---|---|---|
| `mdhub_api` | `api` | read `md.*`, `ref.*`, `ops.row_history` (known_at reads), `ops.v_coverage`, `ops.v_job_health`, `ops.v_open_gaps`, `ops.job_run`, `ops.schema_migration`; insert `ops.api_request`; read `admin.api_token` and update its `last_used_at`/`last_used_ip` |
| `mdhub_admin` | `admin` | the `admin` tables the console uses (`audit_log` insert-only; `secret_key_public` read-only; `source_credential` set/replace/remove; no deletes of users or tokens); read the `ops` tables behind its pages and `ref.calendar_day`/`ref.weekend_session`; insert `ops.job_request` and `ops.update_request` and cancel them (`state`, `state_at`, `finished_at`); retry a backfill task (`status`, `attempts`, `not_before`, `done_at`); mark a gap (`status`, `note`). No `md.*` |
| `mdhub_hostmon` | `hostmon` | insert `ops.host_metric`; run `ops.host_metric_rollup()` (security definer: no other right on the metric tables); upsert its `ops.worker_heartbeat` row |

The grants are re-applied, exactly, every time schema `admin` is locked: by `mdhub migrate`
(each start of the `migrate` service), `sql/grant_readonly.sql` / `readonly_defaults.sql`,
`restore_seed.sh` and the updater's restore. Anything granted to these roles by hand is taken
back then. The roles themselves (LOGIN, password, `CONNECT`) come from `sql/service_roles.sql`,
run as the owner (a superuser in compose).

Compose gives each of the three services `MDHUB_DATABASE_URL` with its own role once its
password is set in `.env`: `MDHUB_API_DB_PASSWORD`, `MDHUB_ADMIN_DB_PASSWORD`,
`MDHUB_HOSTMON_DB_PASSWORD` (long random URL-safe values, `openssl rand -hex 24`). **Until a
password is set, that service falls back to the owner role** and logs at every start:
`<service> connects to PostgreSQL as <owner>, the owner role: it can write every table`. An
install that has not set them keeps working as before, including after a self-update. The `api`
reads `.env` (`env_file`), so compose blanks `POSTGRES_PASSWORD`, `MDHUB_RO_PASSWORD` and the
three role passwords in its environment; `admin` and `hostmon` never see them.

**New install:** set the three passwords in `.env` before the first `docker compose up -d`;
`initdb/10-mdhub-roles.sh` creates the roles, `migrate` grants them.

**Existing install** (or to rotate a password): set the passwords in `.env`, then

```bash
docker compose up -d postgres            # recreated with the new passwords in its environment
docker compose exec postgres sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v dbname="$POSTGRES_DB" -v api_password="$MDHUB_API_DB_PASSWORD" \
    -v admin_password="$MDHUB_ADMIN_DB_PASSWORD" -v hostmon_password="$MDHUB_HOSTMON_DB_PASSWORD" \
    -f /mdhub-sql/service_roles.sql'
docker compose up -d                     # api, admin, hostmon reconnect with their roles
docker compose logs api admin hostmon | grep 'owner role' || echo "all on their own roles"
```

Setting a password without running `service_roles.sql` makes that service fail to connect
(`password authentication failed for user "mdhub_admin"`); run it and restart the service.
`restore_seed.sh` (compose) re-runs it after a restore.

Bare metal (§9) runs only the worker and the API, both as `mdhub` over peer authentication;
the service roles are not set up there.
