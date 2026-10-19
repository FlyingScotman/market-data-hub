# One-time manual upgrade 0.1.0 -> 0.1.3 (production VPS)

Production was installed on 2026-10-18 from the 0.1.0 bundle (FIRST_DEPLOY.md), before the repository
moved to `FlyingScotman/market-data-hub`. The Updates page cannot install 0.1.3 itself, because the
0.1.0 updater:

- pins **no** release key (`git show v0.1.0:deploy/release-keys/` holds only `README.md`), so it
  refuses every signed bundle, uploads included;
- creates its GitHub client only when `MDHUB_UPDATE_GITHUB_TOKEN` is set
  (`git show v0.1.0:src/mdhub/updater.py`, `run()`), and its default repository is
  `FiveLimaGolf/market-data-hub` (`git show v0.1.0:src/mdhub/config.py`);
- ignores `MDHUB_UPDATE_COMPOSE_OVERRIDE`, so it would restart services without
  `shared/compose.local.yaml` (host nginx, loopback ports).

This file repeats by hand what the 0.1.3 updater does (`src/mdhub/updater.py`: verifying ->
backing_up -> staging -> draining -> migrating -> starting -> checking). Once it is done, the Updates
page lists and installs later releases. Run everything as root (`sudo -i`): the shared `.env` is a
symlink into `/root/.config/lima/secrets/market-data-hub/` (DOMAIN_DEPLOY.md). Never print `.env`,
`docker compose config`, tokens or auth headers.

## 1. Preconditions and what changes

What 0.1.3 brings over 0.1.0 (`git diff --stat v0.1.0 v0.1.3`):

- Published snapshots (Market Snapshot Contract v1): `schemas/market_snapshot/v1/` (copied into the image),
  `src/mdhub/snapshot/`, jobs `snapshot_static` and `snapshot_market` (`src/mdhub/loaders/snapshots.py`),
  `GET /v1/snapshots`, `/v1/snapshots/{id}/manifest` and `/v1/snapshots/{id}/bundle`. New dependency
  `jsonschema`: the build needs PyPI, just as the 0.1.0 build did.
- Updater: public GitHub releases are listed without a token, the default repository is FlyingScotman,
  `MDHUB_UPDATE_COMPOSE_OVERRIDE` is honoured, the key `3C9E6DACD133A1D8` is pinned in `/app/release-keys`.
- Three new migrations (23 -> 26), additive only (no DROP TABLE/COLUMN, TRUNCATE or DELETE):
  - `0500_known_at_readers`: an index on `ops.row_history`; `is_deleted` and `ingested_at` columns
    on `ref.calendar_day` and `ref.weekend_session` (backfilled by one UPDATE, then NOT NULL);
    `CREATE OR REPLACE` of `ref.calendar_effective` and the new `*_known_at` functions.
  - `0510_published_snapshot`: new table `ops.published_snapshot` with an immutability trigger.
  - `0810_snapshot_service_roles`: re-creates `admin.grant_service_roles()` and adds
    `SELECT ON ops.published_snapshot` for `mdhub_api`, then `CALL admin.restrict_access()`. The
    procedure REVOKEs and re-GRANTs the service-role privileges, so the grants are briefly reset.
- `.env`: nothing new is **required**. All new variables have defaults (`deploy/env.example`, `src/mdhub/config.py`):
  `MDHUB_SNAPSHOT_STATIC_TIME` (07:45), `_EOD_PRELIMINARY_TIME` (19:45), `_EOD_FINAL_TIME` (08:00),
  `_INTRADAY_MINUTES` (30; `0` = off), `_INTRADAY_WINDOW` (10:00-19:00), `_RETENTION_DAYS_INTRADAY` (30);
  `MDHUB_UPDATE_GITHUB_REPO` (default FlyingScotman now); `MDHUB_UPDATE_COMPOSE_OVERRIDE` (no default; see the checks).
  The cut times are owner decision DEC04. Set them before step 5 if the defaults are wrong. By default,
  intraday cuts start the day after the upgrade.

Helpers used below (define them again in every new shell). `dc` = the running release, `dcn` = the
new one, both with the persistent override exactly as the updater passes it (`_compose_files()`):

```bash
dc() { docker compose -f /opt/mdhub/current/deploy/docker-compose.yml -f /opt/mdhub/shared/compose.local.yaml "$@"; }
dcn() { MDHUB_IMAGE_TAG=0.1.3 MDHUB_VERSION=0.1.3 docker compose -f /opt/mdhub/releases/0.1.3/deploy/docker-compose.yml -f /opt/mdhub/shared/compose.local.yaml "$@"; }
pg() { dc exec -T postgres sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' sh "$@"; }
```

Check the live state before you change anything (ROADMAP "Как продолжить"):

```bash
readlink /opt/mdhub/current                      # expect releases/0.1.0
```
```bash
docker compose ls                                # project "mdhub"; CONFIG FILES must include /opt/mdhub/shared/compose.local.yaml
```
```bash
docker image ls mdhub                            # mdhub:0.1.0 present (used for verification in step 2)
```
```bash
docker volume ls | grep pgdata                   # mdhub_pgdata: live data, never removed
```
```bash
ls -l /opt/mdhub/shared/                         # .env -> /root/.config/lima/secrets/..., compose.local.yaml, backups/, updater.env?
```
```bash
grep -E '^(MDHUB_IMAGE_TAG|MDHUB_BACKUP_HOST_DIR)=' /opt/mdhub/shared/.env   # expect 0.1.0 and /opt/mdhub/shared/backups
```
```bash
grep -E '^(MDHUB_UPDATE_[A-Z_]+|MDHUB_HTTP_PROXY|MDHUB_SNAPSHOT_[A-Z_]+)=' /opt/mdhub/shared/.env | cut -d= -f1   # names only
```
```bash
grep -n -E 'MDHUB_UPDATE_|/root/.config/lima|127.0.0.1:8081' /opt/mdhub/shared/compose.local.yaml
```
```bash
curl -s http://127.0.0.1:8080/health/ready       # version 0.1.0, migrations applied 23, pending 0
```
```bash
df -h /opt /var/lib/docker                       # room for one more full dump and one image
```

If `.env` pins `MDHUB_UPDATE_GITHUB_REPO=FiveLimaGolf/...`, remove that line. The default is correct.
The updater needs `MDHUB_UPDATE_COMPOSE_OVERRIDE=/opt/mdhub/shared/compose.local.yaml` and the secrets
directory mounted at the same path. Both are in the `updater:` block of `deploy/compose.host-nginx.yaml`.
If `compose.local.yaml` lacks them, merge that block into it before step 5. Without the mount the updater
cannot see `shared/.env` and refuses every update (`_verifying`: "not found").

## 2. Download and verify on the VPS

The repository is public, so no token is needed. If outbound HTTPS from the VPS goes through a proxy,
check that `curl` can reach github.com.

```bash
mkdir -p /opt/mdhub/archives && cd /opt/mdhub/archives
```
```bash
for f in market-data-hub-0.1.3.tar.gz market-data-hub-0.1.3.SHA256SUMS market-data-hub-0.1.3.SHA256SUMS.minisig; do curl -fsSLO "https://github.com/FlyingScotman/market-data-hub/releases/download/v0.1.3/$f" || break; done
```

Get the pinned public key from the owner's checkout, not from the release. Run this on the Mac:

```bash
ssh <vps> 'mkdir -p /tmp/mdhub-release-keys' && scp deploy/release-keys/3C9E6DACD133A1D8.pub <vps>:/tmp/mdhub-release-keys/
```

On the VPS, run 0.1.0's `mdhub release verify` (`src/mdhub/release.py` is unchanged since 0.1.0)
with that key. It checks the signature, every file hash and the manifest. The container has no network:

```bash
chmod 755 /tmp/mdhub-release-keys && chmod 644 /tmp/mdhub-release-keys/*.pub /opt/mdhub/archives/market-data-hub-0.1.3.*
```
```bash
docker run --rm --network none -v /opt/mdhub/archives:/dl:ro -v /tmp/mdhub-release-keys:/keys:ro mdhub:0.1.0 mdhub release verify /dl/market-data-hub-0.1.3.tar.gz --keys-dir /keys
```

Expect `OK market-data-hub 0.1.3 (commit ...): signed by pinned key 3C9E6DACD133A1D8`, `26 migrations
(last 0810_snapshot_service_roles)` and `min_previous_version 0.1.0`. Anything other than `OK` means stop.
Record the full commit and compare it on the Mac with `git rev-parse 'v0.1.3^{commit}'`:

```bash
docker run --rm --network none -v /opt/mdhub/archives:/dl:ro -v /tmp/mdhub-release-keys:/keys:ro mdhub:0.1.0 mdhub release verify /dl/market-data-hub-0.1.3.tar.gz --keys-dir /keys --json | grep -m1 '"commit"'
```

Optional second check with minisign, if it is installed (README_DEPLOY §11):

```bash
minisign -V -p /tmp/mdhub-release-keys/3C9E6DACD133A1D8.pub -m /opt/mdhub/archives/market-data-hub-0.1.3.SHA256SUMS
```

## 3. Backup

Use the documented mechanism (`mdhub ops backup` = `maintenance mode=backup`, README_DEPLOY §7). It
writes `mdhub-<db>-<stamp>.dump` to `/var/backups/mdhub` in the container, which is
`/opt/mdhub/shared/backups` on the host:

```bash
dc run --rm --no-deps -T worker mdhub ops backup
```

Note the printed file name. The nightly rotation keeps only 14 `mdhub-*.dump` files, so add a second,
non-rotated name in the updater's `pre-update-*` convention (`_backing_up`; same file, hard link):

```bash
DUMP=pre-update-mdhub-0.1.0-to-0.1.3-$(date -u +%Y%m%dT%H%M%SZ).dump
```
```bash
ln /opt/mdhub/shared/backups/<printed mdhub-mdhub-....dump> "/opt/mdhub/shared/backups/$DUMP" && echo "$DUMP"
```
```bash
dc run --rm --no-deps -T worker pg_restore -l "/var/backups/mdhub/$DUMP" | grep -c 'TABLE DATA'   # > 0, no error
```

Keep `$DUMP` for steps 6 and 8. Copy it off the VPS if possible: there is no off-site backup yet (ROADMAP WEB-6).

## 4. Drain the worker and wait for active jobs

Use the same flag the updater sets (`ops.worker_control.drain`, migration 0640; `Updater._drain`). The
worker then starts no new scheduled run and no console request. Skipped fires are recorded as
`lock_busy`, and `quality` catches them up later.

```bash
pg -c "UPDATE ops.worker_control SET drain = true, reason = 'manual upgrade 0.1.0 -> 0.1.3', changed_at = now();"
```

Wait until this query returns no rows (`Updater._busy`). The updater gives up after 10 minutes. A row that
stays `running` for much longer is probably a stale run: check it on the Jobs page.

```bash
until [ -z "$(pg -At -c "SELECT job FROM ops.job_run WHERE status = 'running' UNION ALL SELECT job FROM ops.job_request WHERE state = 'running';")" ]; do sleep 15; done; echo idle
```
```bash
dc stop worker
```

Do not use the Updates page or "Run now" until step 6. `mdhub run` on the CLI ignores the drain flag.

## 5. Stage, build, migrate, switch

Unpack the release the way the updater does it (`_staging`: extract to a temporary directory, then
rename `mdhub/` to `releases/<ver>`):

```bash
mkdir /opt/mdhub/releases/.staging-0.1.3
```
```bash
tar xzf /opt/mdhub/archives/market-data-hub-0.1.3.tar.gz --no-same-owner -C /opt/mdhub/releases/.staging-0.1.3
```
```bash
mv /opt/mdhub/releases/.staging-0.1.3/mdhub /opt/mdhub/releases/0.1.3 && rmdir /opt/mdhub/releases/.staging-0.1.3
```
```bash
(cd /opt/mdhub/releases/0.1.3 && sha256sum -c --quiet SHA256SUMS) && echo hashes-ok
```

Link the shared files the way the updater does (`_staging`: `deploy/.env` and `deploy/updater.env` ->
`/opt/mdhub/shared/<name>`). `compose.local.yaml` stays in `shared/` and is passed with `-f`, so it is not linked:

```bash
ln -s /opt/mdhub/shared/.env /opt/mdhub/releases/0.1.3/deploy/.env
```
```bash
[ -f /opt/mdhub/shared/updater.env ] && ln -s /opt/mdhub/shared/updater.env /opt/mdhub/releases/0.1.3/deploy/updater.env
```

Build `mdhub:0.1.3`. The running services are not touched. The updater passes the same
`MDHUB_IMAGE_TAG`/`MDHUB_VERSION` (`ComposeHost.compose`):

```bash
dcn build
```
```bash
docker run --rm --network none mdhub:0.1.3 mdhub --version          # 0.1.3
```
```bash
docker run --rm --network none mdhub:0.1.3 mdhub release keys       # 3C9E6DACD133A1D8  /app/release-keys/...
```
```bash
dcn run --rm --no-deps -T worker mdhub db-info                      # 23 applied, 3 pending: 0500, 0510, 0810
```

Run the migrations with the new image (`_migrating`: `run --rm --no-deps migrate`). `mdhub migrate`
refuses to start if an applied checksum differs, before it applies anything:

```bash
dcn run --rm --no-deps migrate
```

Switch the tag and `current` (`_switch`). The `.env` is a symlink, so edit the real file. GNU `sed -i`
on the symlink itself would replace the link with a plain file:

```bash
sed -i 's/^MDHUB_IMAGE_TAG=.*/MDHUB_IMAGE_TAG=0.1.3/' "$(readlink -f /opt/mdhub/shared/.env)"
```
```bash
grep '^MDHUB_IMAGE_TAG=' /opt/mdhub/shared/.env                     # MDHUB_IMAGE_TAG=0.1.3
```
```bash
ln -sfn releases/0.1.3 /opt/mdhub/current && readlink /opt/mdhub/current
```

Start the new version with the override. The updater runs `up -d --no-deps worker api admin` and then
replaces itself. Here hostmon is included too, because the updater never restarts it. `--no-deps`
leaves the `postgres` container (and `mdhub_pgdata`) alone:

```bash
dc up -d --no-deps worker api admin hostmon updater
```

Do **not** run `restore_seed.sh` and never `docker compose down -v`. Keep `releases/0.1.0`: it is the
rollback target, and the running `postgres` container still bind-mounts its `deploy/postgres` and `deploy/sql`.

## 6. Undrain and check

```bash
pg -c "UPDATE ops.worker_control SET drain = false, reason = NULL, changed_at = now();"
```
```bash
dc ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'        # mdhub:0.1.3 for worker/api/admin/hostmon/updater; healthy
```
```bash
curl -s http://127.0.0.1:8080/health/ready                          # "version":"0.1.3", migrations applied 26 known 26 pending 0 checksum_mismatch 0
```
```bash
curl -s https://marketdatahub.cc/health/ready                       # same, through host nginx
```
```bash
ss -ltn | grep -E ':(5432|8080|8081) '                              # 127.0.0.1 only
```
```bash
pg -c "SELECT process, host, version, beat_at FROM ops.worker_heartbeat WHERE stopped_at IS NULL ORDER BY process;"   # worker, updater, hostmon: 0.1.3
```
```bash
dc run --rm --no-deps -T worker mdhub db-info                       # 26 applied, 0 pending; recent job runs
```
```bash
dc run --rm --no-deps -T worker mdhub jobs list | grep snapshot_    # snapshot_static, snapshot_market with their cuts
```

In the browser: log in at <https://marketdatahub.cc/login> with password and passkey. Overview should show
no version mismatch; Updates should show 0.1.3 and the heartbeats; Jobs should list `snapshot_static` and
`snapshot_market`.

API without a token: expect `401`.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://marketdatahub.cc/v1/snapshots
```

API with a token. Existing `read` tokens such as `tdm-backend` already cover `/v1/snapshots`
(`required_scopes()` in `src/mdhub/api/auth.py`). For the snapshot consumer (the inside replica / TDM),
create a dedicated least-privilege token with scope `snapshots` (DESIGN §8: `snapshots` =
`/v1/snapshots*` only). The token is printed once on stdout, so write it straight to a 0600 file
(`-T`: no TTY, so stderr stays out of the file):

```bash
(umask 077; dc run --rm --no-deps -T worker mdhub tokens create tdm-snapshots -s snapshots > /root/tdm-snapshots.token)
```
```bash
(umask 077; printf 'Authorization: Bearer %s\n' "$(cat /root/tdm-snapshots.token)" > /root/tdm-snapshots.hdr)
```
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H @/root/tdm-snapshots.hdr https://marketdatahub.cc/v1/snapshots   # 200 (empty list until step 9)
```
```bash
curl -s -o /dev/null -w '%{http_code}\n' -H @/root/tdm-snapshots.hdr https://marketdatahub.cc/v1/coverage    # 403: scope limited
```

Hand the token to its consumer through the usual secret channel, then delete both files.

Record the manual install in the release history, as `Updater._done` would. This lets the Updates page
show 0.1.0 -> 0.1.3, and "Roll back" finds the pre-update dump. `backup_path` is the path inside the
updater container. Use the full commit from step 2:

```bash
COMMIT=<full commit from step 2>; SHA=$(sha256sum /opt/mdhub/archives/market-data-hub-0.1.3.tar.gz | cut -d' ' -f1)
```
```bash
pg -1 -f - <<SQL
UPDATE ops.release SET status = 'superseded' WHERE status IN ('active', 'previous') AND version NOT IN ('0.1.0', '0.1.3');
INSERT INTO ops.release (version, source, status, installed_by) VALUES ('0.1.0', 'manual', 'previous', 'manual')
    ON CONFLICT (version) DO UPDATE SET status = 'previous';
INSERT INTO ops.release (version, git_commit, source, sha256, key_id, migrations, previous_version, backup_path, installed_by, status)
SELECT '0.1.3', '$COMMIT', 'manual', '$SHA', '3C9E6DACD133A1D8', jsonb_object_agg(version, checksum), '0.1.0',
       '/var/backups/mdhub/$DUMP', 'manual', 'active' FROM ops.schema_migration;
SQL
```

## 7. Future updates from the panel

0.1.3 always creates the GitHub client (commit `5b3d3df`, `updater.run()`): public releases are listed
without a token, and a token is needed only for a private repository. The repository defaults to
`FlyingScotman/market-data-hub` (compose default, `config.py`, and `compose.host-nginx.yaml`). Check:

```bash
dc exec updater mdhub release keys                                  # 3C9E6DACD133A1D8
```
```bash
dc exec updater printenv MDHUB_UPDATE_GITHUB_REPO MDHUB_UPDATE_COMPOSE_OVERRIDE   # FlyingScotman/market-data-hub, /opt/mdhub/shared/compose.local.yaml
```
```bash
dc exec updater sh -c 'test -f /opt/mdhub/shared/.env && test -f /opt/mdhub/shared/compose.local.yaml && echo layout-ok'
```

Releases are refreshed every 10 minutes (`GITHUB_INTERVAL_S`). After that, the list should contain
0.1.3 (and 0.1.2, if it was published), with `verified` empty because they are not newer:

```bash
pg -c "SELECT version, verified, error, checked_at FROM ops.release_available ORDER BY version;"
```
```bash
dc logs --since 15m updater | grep -i -E 'github|error|warn'
```

If `/opt/mdhub/shared/updater.env` holds an old FiveLimaGolf token, remove it. A revoked token makes
the listing fail. If you remove it, delete the `deploy/updater.env` link and run `dc up -d --no-deps updater`.
`MDHUB_HTTP_PROXY`, when set, also carries the GitHub traffic.

After a panel update, run `dc up -d --no-deps hostmon`. The updater restarts only worker, api, admin
and itself (`SERVICES`).

## 8. Rollback

Failure before the switch in step 5 (build or migrate): `current` and the tag are unchanged. Undrain
(step 6, first command) and start the old worker:

```bash
dc up -d --no-deps worker
```

Failure after the switch: put the 0.1.0 tag and link back, then start with the same override
(README_DEPLOY §13 "Manual recovery"):

```bash
sed -i 's/^MDHUB_IMAGE_TAG=.*/MDHUB_IMAGE_TAG=0.1.0/' "$(readlink -f /opt/mdhub/shared/.env)"
```
```bash
ln -sfn releases/0.1.0 /opt/mdhub/current && readlink /opt/mdhub/current
```
```bash
dc up -d --no-deps worker api admin hostmon updater
```
```bash
pg -c "UPDATE ops.worker_control SET drain = false, reason = NULL, changed_at = now();"
```

0.1.0 runs on the migrated schema. The three migrations are additive: 0.1.0's `/health/ready` checks
only its own 23 migrations, and its `mdhub migrate` only warns about unknown applied ones. If you recorded
`ops.release` in step 6, fix it:
`pg -c "UPDATE ops.release SET status = CASE version WHEN '0.1.3' THEN 'rolled_back' ELSE 'active' END WHERE version IN ('0.1.0','0.1.3');"`.
Avoid "Roll back" in the panel for this hop: it would leave a 0.1.0 updater without a pinned key or override support.

Restore the dump **only** if the migrations broke data. Everything loaded after step 3 is lost, and
backfill reloads it. Run 0.1.0's own script with the override so nginx and the loopback ports survive
(`restore_seed.sh` runs plain `docker compose`, which honours `COMPOSE_FILE`). It drops and re-creates the
database from the given dump, which is the pre-update dump and not the seed:

```bash
cd /opt/mdhub/releases/0.1.0/deploy && COMPOSE_FILE=docker-compose.yml:/opt/mdhub/shared/compose.local.yaml ./restore_seed.sh "/opt/mdhub/shared/backups/$DUMP"
```

## 9. First snapshots

"Run now" on the Jobs page runs with **default parameters** (`templates/jobs.html`). For
`snapshot_static` that means `mode=daily` for today, which is fine. For `snapshot_market` it means
`eod_preliminary`, so use the CLI for `eod_final`. Parameters (`src/mdhub/loaders/snapshots.py`): `mode`;
`day` (YYYY-MM-DD business date); `days_back` (business-day offset; `eod_final` default 1); `cut` (HH:MM
MSK; default is the configured cut time, for `eod_final` on the business day after `day`); `known_at`
(default now; leave it).

```bash
dc run --rm --no-deps -T worker mdhub run snapshot_static
```
```bash
dc run --rm --no-deps -T worker mdhub run snapshot_market -p mode=eod_final      # D = previous business day
```

Back-date about 10 `eod_final` cuts, oldest first. `days_back` counts business days on the bond
calendar, so holidays are skipped. Each market cut also publishes the static snapshot it needs. Keep
the default `cut` so later scheduled rebuilds hit the same cut and supersede rather than duplicate.
`skipped` (lock busy) means the scheduler holds the job: rerun that day.

```bash
for n in $(seq 10 -1 2); do dc run --rm --no-deps -T worker mdhub run snapshot_market -p mode=eod_final -p days_back=$n || break; done
```

The same for one explicit date (it must be a business day):

```bash
dc run --rm --no-deps -T worker mdhub run snapshot_market -p mode=eod_final -p day=2026-10-06 -p cut=08:00
```
```bash
curl -s -H @/root/tdm-snapshots.hdr 'https://marketdatahub.cc/v1/snapshots?kind=market&cut_type=eod_final' | head -c 600
```

From now on the scheduler publishes `snapshot_static` at 07:45, `eod_final` at 08:00 (for D-1),
`eod_preliminary` at 19:45 and intraday cuts every 30 minutes from 10:00 to 19:00, unless `MDHUB_SNAPSHOT_*`
says otherwise. Check the size of `ops.published_snapshot` after the first full day.
