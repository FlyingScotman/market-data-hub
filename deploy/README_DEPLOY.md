# Deploying market-data-hub on a VPS

Target: one Ubuntu 24.04 VPS (4 GB RAM, 2-4 vCPU, SSD >= 80 GB) that runs PostgreSQL 16, the
scheduler (`mdhub scheduler`: all loaders, `quality`, `backfill_worker`, `maintenance`) and
the read API. Two variants: **docker compose** (recommended) and **bare metal** (systemd).

| file | purpose |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | image: Python 3.12, deps from `uv.lock`, `postgresql-client-16`, user `mdhub` (uid 10001) |
| `docker-compose.yml` | `postgres` (16, tuned, healthcheck), `migrate` (one-shot), `worker`, `api`, `admin` (console, not published), optional `caddy` (profile `tls`) |
| `env.example` | every environment variable, documented, no values |
| `postgres/postgresql.conf` | tuning for 4 GB RAM |
| `postgres/initdb/10-mdhub-roles.sh` | first start: read-only role + default privileges |
| `sql/readonly_defaults.sql`, `sql/grant_readonly.sql` | read-only consumer role setup / re-grant |
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
operators can run a job, retry a failed backfill task and mark a gap not expected.

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
under systemd with the same environment and `MDHUB_ADMIN_TRUSTED_PROXIES=127.0.0.1`, and add
the console site of the `Caddyfile` with `MDHUB_ADMIN_UPSTREAM=127.0.0.1:8081`.

Local try-out (no TLS): `MDHUB_ADMIN_DOMAIN=localhost MDHUB_ADMIN_ORIGIN=http://localhost:8081
MDHUB_ADMIN_ALLOW_CIDRS=127.0.0.1/32 uvicorn mdhub.admin.app:app --port 8081`, then open
`http://localhost:8081` (Chromium-based browsers and Firefox treat localhost as secure, so the
`__Host-` cookie and passkeys work).
