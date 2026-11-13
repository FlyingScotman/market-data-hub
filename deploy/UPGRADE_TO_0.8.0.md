# 0.8.0 — contract 1.8: the last intraday futures quotes in the evening cut (`eod_preliminary`)

Instructions for the operator (a person or an agent) to whom the owner has given access to the hub
server, who installs 0.8.0. Changes on the production server are the owner's: you act for the owner,
only as far as this document and the owner's answers allow. You need no knowledge of the code: every
step says what to do, how to see that it worked and what to do if it did not. Where the result is not
what this document expects, **stop and report to the owner** instead of improvising. "Stop" also covers
any `Permission denied`: do not change file modes or owners to get around it.

Starting point: the server runs **0.7.0** ([UPGRADE_TO_0.7.0.md](UPGRADE_TO_0.7.0.md)). 0.8.0 can be
installed over any version from 0.1.0 (`min_previous_version`), so there is no intermediate release.
If the server runs something else, write the version down and tell the owner before you continue. In
particular, **a server that still runs 0.6.2 or older is not covered by this document**: 0.7.0 has
its own steps (migrations, the move of the Ratescalcs address, the first-night repair); the owner
decides how to get there.

**This release has no database migrations, no new or changed settings and no new environment
variables** (sections 1 and 2 show how that was established). The only change in behaviour is the
content of one evening snapshot and the contract version it is published with. The install is
therefore short, and the rollback (section 6) only switches the image tag back.

## 0. Before you start

### 0.1 What you need

- A console account with the role **admin** and a fresh second-factor check ("step-up"; the pages
  Updates and Tokens ask for it), from an IP that the console allow-list accepts
  ([README_DEPLOY.md](README_DEPLOY.md) §12). The role **operator** is enough to look at the Jobs page.
- A shell on the hub server **as root**: log in and run `sudo -i` (`sudo` runs a command with root rights; `-i`
  starts a login shell for root), then stay in that shell for the whole upgrade (the server configuration
  file `.env` is mode 0600 in a 0700 directory and the docker socket is root-equivalent). Every command
  below runs there. Do not put `sudo` in front of single commands instead: `sudo` normally drops the shell
  variables that 0.3 and 0.5 set.
- From the owner: the day and hour of the install (asked in 3.4), and whether the desk monitor on the
  desk machine already runs 0.2.28 or newer (asked in 3.3).

### 0.2 Rules that apply to every step

1. No values of secrets (passwords, tokens, the proxy URL, `MDHUB_SECRETS_PRIVATE_KEY`) go into a ticket, an
   issue, a pull request, a log excerpt, a screenshot or your report. Report **names, states and counts**.
2. Do not roll back, delete rows, re-run jobs or change settings that this document does not name, unless the
   owner says so. In particular **do not run `snapshot_market` for `eod_preliminary` by hand** to "test" the
   change (Run now on the Jobs page): it publishes a real snapshot (section 5.5).
3. Snapshots that are already published are immutable and stay as they are. Nothing in this release rewrites,
   reissues or deletes one (section 1).

### 0.3 Layout and Compose files

All `docker compose` commands below must use **exactly the Compose files with which the running
services were started**. A site may have an extra file next to `docker-compose.yml` (the production
layout has one, see [DOMAIN_DEPLOY.md](DOMAIN_DEPLOY.md)). It carries, among other things, the network
settings, the console's published port and `MDHUB_ADMIN_TRUSTED_PROXIES`. A command that runs `up` or `run`
without it computes a different configuration: Compose may then recreate the network `mdhub_default` and with it
stop and restart everything attached to it, and the console may no longer trust the web proxy in front
of it (its client-IP checks and allow-list break, `MDHUB_ADMIN_TRUSTED_PROXIES`). Do not guess from `.env`: the
`MDHUB_UPDATE_COMPOSE_OVERRIDE` setting is normally written in the extra file itself, not in `.env`. Ask the
running project instead.

**Step 1 — the layout.**

```bash
ls -l /opt/mdhub/current
ls -d /opt/mdhub/releases/0.7.0
```

- `ls` lists files; `-l` long format, so a symbolic link is shown as `current -> releases/X.Y.Z`;
  `-d` shows a directory itself, not its content. `/opt/mdhub/current` and `/opt/mdhub/releases/0.7.0` are
  the paths asked about.
- Expected: `current -> releases/0.7.0` and the second command prints `/opt/mdhub/releases/0.7.0`.
  This is the release layout of [README_DEPLOY.md](README_DEPLOY.md) §13 (default root `/opt/mdhub`); the
  configuration file is `/opt/mdhub/shared/.env` (linked as `deploy/.env`). "No such file" for the first
  command means a plain unpacked archive (§2-3 of the README): the console's Updates page may then be
  unavailable (see 4.4); tell the owner before you go on. The second command is the rollback
  prerequisite (3.2): the console can only go back to a release directory that exists.

**Step 2 — which files are in use.** The source is the label that Compose put on the running worker
container; `docker compose ls` is the cross-check.

```bash
docker inspect mdhub-worker-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker compose ls
```

- `docker inspect` prints the details of a container; `--format '...'` is a Go template; `index .Config.Labels "..."`
  takes one label, the one that holds the list of Compose files the project was started with (Compose writes
  it on every container it creates). The project is named `mdhub` by `name:` in `docker-compose.yml`, so the worker
  container is normally `mdhub-worker-1`; if that name does not exist (`docker ps` shows the container names), use
  the name shown there for the `worker` service.
- `docker compose ls` lists the running Compose projects: the project name, its state and the column
  `CONFIG FILES`, the files with which it was started.
- Expected: the label is a comma-separated list such as
  `/opt/mdhub/current/deploy/docker-compose.yml,/opt/mdhub/shared/compose.local.yaml`, and `docker compose ls` shows
  one project `mdhub`, state `running(N)`, with **the same list**. The first file is the release's `docker-compose.yml`;
  every file after it is a site file. Write the list down (section 7).
- **Stop and ask the owner** if the label is empty or the container does not exist, if the list holds **more than one**
  `docker-compose.yml` (two releases mixed), if the first file belongs to a release other than 0.7.0, or if the two
  sources differ in any way (a different file, a different order, a cut-off list).

**Step 3 — fix the files for this shell.** Join the paths with a colon, **in the same order**, and use the
release's `docker-compose.yml` through `current` (it follows the running release) as the first one:

```bash
export COMPOSE_FILE=/opt/mdhub/current/deploy/docker-compose.yml:/opt/mdhub/shared/compose.local.yaml
```

- `export` sets a variable for this shell and the programs it starts; `COMPOSE_FILE` is the variable that
  Docker Compose reads for the list of files, separated by `:`; the value above is the example of the
  production layout, **use the files of your list** (a base file only: just the first path). Later files
  override earlier ones, so keep the order. With `COMPOSE_FILE` set, the directory you are in does not matter
  and no `-f` is needed; `.env` is read from the directory of the first file.
- If a `caddy` container is running (the `tls` profile, [README_DEPLOY.md](README_DEPLOY.md) §6), also run
  `export COMPOSE_PROFILES=tls`; the production layout with the web proxy on the host has none.
- Set it again whenever you open a new shell (it is not remembered).

**Step 4 — check that Compose sees the same project.**

```bash
docker compose version
docker compose config --services | sort
docker ps --filter label=com.docker.compose.project=mdhub --format '{{.Label "com.docker.compose.service"}}' | sort
docker compose up -d --dry-run --no-deps worker
```

- `docker compose version` prints the version of the Compose plugin. **`--dry-run` needs 2.17 or newer.** If it is
  older, or the last command answers `unknown flag: --dry-run`: **stop and ask the owner. Do not "try the
  command without the flag".**
- **Never run the last command without `--dry-run`**: without it `up` really creates, re-creates and starts containers.
- `docker compose config --services` prints the service names of the configuration Compose computes from
  `COMPOSE_FILE`; `| sort` sorts them (`|` passes the output of the left command to the right one). `docker ps` lists the running containers; `--filter label=...` keeps those of
  the project `mdhub`; `--format '{{.Label "..."}}'` prints one label of each, the service name.
- Expected: every service of the second list is in the first; the first has also `migrate` (a one-shot
  that has finished) and nothing else. Differences: stop.
- `docker compose up -d --dry-run --no-deps worker` only **shows** what a start of the worker would do and
  changes nothing (`--dry-run`); `--no-deps` leaves out the services the worker depends on. Read every line it prints:
  - **Not a reason to stop:** lines with `Waiting`, `Healthy` or `Exited` about `postgres` or `migrate` (Compose prints
    the dependency check even with `--no-deps`; the one-shot `migrate` has exited by design), and lines about the
    `worker` itself (`Running`, or `Recreate`/`Recreated`/`Started`: a re-created worker alone means `.env` changed
    since its last start; write it down and tell the owner).
  - **Stop and ask the owner:** any line `Create`, `Recreate` or `Remove` for a container **other than the worker**,
    and any line at all about a **network or a volume**. It means that the files are wrong.

### 0.4 Commands that come back several times

Each is shown in full where it is used; this is what its parts mean.

- `docker compose logs --tail 100 worker`: `docker compose` is the Compose plugin of Docker;
  `logs` prints the output of a service's container; `--tail 100` only the last 100 lines;
  `worker` is the service (the scheduler that runs all jobs). Container logs are kept in 5 files of 20 MB.
- `curl -sS -K "$CURLRC" "http://127.0.0.1:8080<path>"`: `curl` sends an HTTP request; `-s` hides the progress
  bar and `-S` still shows an error; `-K FILE` (`--config`) reads more options from a file: the file made in 0.5
  holds one line `header = "Authorization: Bearer ..."`, the bearer token that the read API requires for
  `/v1/...`. The token is in a file and not in the command because every user of the server can read a
  command line with `ps`. The last argument is the URL: the API listens on the server's loopback port 8080
  (compose publishes `127.0.0.1:${MDHUB_API_HOST_PORT:-8080}`). `/health/...` needs no token.
- `python3 -c '...'`: `python3` runs Python 3; `-c` takes the program from the quoted text instead of a file.
  Where it is used here, the program reads the JSON answer of `curl` from its standard input (`json.load(sys.stdin)`,
  fed by `|`) and prints a few fields of it. It changes nothing.

### 0.5 A read token for the API checks (used in section 5)

Console, page **Tokens**, form "New token": Name `upgrade-check`, scope `read`, "Expires after (days)" `3`,
**Create**. Copy the token (it is shown once). In the server shell:

```bash
CURLRC=$(mktemp)
read -rs MDHUB_TOKEN && printf 'header = "Authorization: Bearer %s"\n' "$MDHUB_TOKEN" > "$CURLRC"; unset MDHUB_TOKEN
```

- `mktemp` creates an empty file with a unique name in `/tmp`, readable by its owner only (mode 0600), and prints
  its name; `$(...)` puts that name into the variable `CURLRC`.
- `read` reads one line from the keyboard into a variable; `-r` takes backslashes literally; `-s` does not echo
  what you type; `MDHUB_TOKEN` is the variable name. Paste the token and press Enter.
- `&&` runs the next command only if the previous one succeeded; `printf 'format' value` prints text by the
  format (`%s` is replaced by the value, `\n` is a line break) and `> "$CURLRC"` writes it into the file instead
  of the screen (`printf` is built into the shell, so the token never appears in a process list). `;`
  starts the next command; `unset MDHUB_TOKEN` forgets the variable.
- The variable `CURLRC` lives only in this shell session. The first check of section 5 comes the evening
  after the install, so the token lives for 3 days and the shell must be kept (or the file made again from a
  new token).

Revoke the token on the Tokens page and delete the file at the end (section 7).

## 1. What comes in 0.8.0

Source: the changes since `v0.7.0` (`git log v0.7.0..main`), pull requests FlyingScotman/market-data-hub#155 and
FlyingScotman/market-data-hub#157, issue FlyingScotman/market-data-hub#154. Contract:
[schemas/market_snapshot/v1/README.md](../schemas/market_snapshot/v1/README.md) (row 1.8), design:
[DESIGN.md](../docs/DESIGN.md) §11.

- **`eod_preliminary` carries the last intraday futures quotes** (contract `market_snapshot/1.8`, additive).
  At the evening cut (default 19:45 MSK, `MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME`) MOEX ISS has not yet published
  the FORTS history of the day, so `futures_prices` (and `fx_swaps`) of the cut are empty, and a consumer
  could not build an FX-implied curve before `eod_final` (08:00 the next business day). Since 0.8.0 the cut
  holds `futures_prices_intraday` instead of `not_in_cut: eod`: per contract the last quote of the business date
  whose quote time (`ts`) and source time (`source_ts`) are **at or before the cut** (`cut_ts`), the same row
  shape as in an intraday cut. Owner decision of 2026-11-13 (option A of #154).
- **The rows are labelled as what they are.** The coverage of the dataset in the manifest says
  `basis: intraday_last_at_cut`, with `contracts`, `latest_ts` and `latest_source_ts` (the newest quote time).
  They are intraday quotes, not end-of-day data. With no quote before the cut the dataset is empty and
  `stale` with the reason `no intraday FORTS futures quotes on <date> at or before the cut`.
- **The cut can be rebuilt with the same result.** The rows are the row versions *known at the cut*, so a
  retry or a catch-up run of the same cut reads the same rows: a poll after the cut or a later update of a row
  does not change it (publishing identical content publishes nothing: `unchanged`). The price of it: quotes
  stored **after** the cut (a legacy import, a backfill of a historical day) count as "not known at the cut",
  so a manual rebuild of such an old day gives an empty, stale set. No job of the hub rebuilds an
  already published `eod_preliminary` cut (the autocorrection and `snapshot_heal` work on `eod_final` only).
- **Everything else in the snapshots is unchanged.** `intraday` cuts and `eod_final` cuts (`futures_prices_intraday`
  there is still `not_in_cut: eod`) have the same content as with 0.7.0; the only difference is the version
  string: **every new snapshot (static, intraday, `eod_preliminary`, `eod_final`, including corrections that
  `snapshot_heal` publishes) says `schema_version: market_snapshot/1.8`.** The identifier of a snapshot is a
  hash of its bundle, so ids of new snapshots differ from what 0.7.0 would have produced; no old snapshot is
  touched. A new market cut can point (`static_snapshot_id`) to an existing static snapshot that is still
  `1.7`: static snapshots with the same content are reused, and a version string does not count as content. That
  is not an error.
- **Already published snapshots stay as they are.** They are immutable. The evening cuts of earlier days
  (`eod_preliminary` of 0.7.0 and before) keep no futures, and no job adds them later.
- **Test only, no code:** #155 adds a regression test that the USD and EUR futures reach the end-of-day
  snapshot in the shape the monitor's FX-implied curve reads. Nothing to do.
- Not part of 0.8.0: FlyingScotman/market-data-hub#156 (a sub-second commit race in as-known-at reads; a quote
  committed in the very fraction of a second of the cut).

**Consumer.** The desk monitor takes these quotes on `eod_preliminary` from its release **0.2.28**
(FlyingScotman/trading-desk-monitor#704, consumer side of FlyingScotman/trading-desk-monitor#695). Monitor 0.2.28 is
released (tag `v0.2.28` in FlyingScotman/trading-desk-monitor). Before that
it took intraday futures only from `intraday` cuts and ignores the new rows in an evening cut, which does no harm
but gives it no benefit. **Roll the monitor 0.2.28 out first, then the hub** (3.3). Which version runs on the desk
machine is the owner's to confirm: this document cannot see it.

### No migrations, no new settings (how that was established)

- Migrations: `src/mdhub/db/migrations/` has 53 files in `v0.7.0` and the same 53 in 0.8.0 (the last is
  `0860_dataset_history_load_tasks`); the diff of the directory between the two is empty. The database function
  that the new read uses, `md.futures_intraday_known_at`, comes from migration `0560_intraday_known_at`, which
  0.7.0 already has. After the install `/health/ready` must say `applied` 53, `known` 53, `pending` 0,
  `checksum_mismatch` 0, and the confirmation page of the console must say that there are **no new migrations**
  (4.2).
- Code: since `v0.7.0` only `src/mdhub/snapshot/market.py` (the dataset builder) and `src/mdhub/snapshot/schema.py`
  (the version constant) changed under `src/`; `deploy/`, `pyproject.toml` and `uv.lock` did not (so no new
  dependency, no change of the compose file). The rest of the diff is the contract documents, the golden
  fixtures (only the version string and ids derived from it), tests and `AGENTS.md`.
- Nothing runs once or at the install: no job, schedule, backfill or "history load" is added or switched on. The
  schedule of `snapshot_market` (the `eod_preliminary` cut at 19:45 MSK on business days, `eod_final` at 08:00, intraday
  cuts every 30 minutes 10:00-19:00) is as before. The job `futures_intraday` that supplies the quotes already
  polls every minute 07:00-23:50 on FORTS trading days.

## 2. All new and changed settings since 0.7.0

**None.** `src/mdhub/config.py`, `deploy/env.example` and `deploy/docker-compose.yml` did not change since 0.7.0;
`.env` stays as it is, and the worker, the API and the console need no new variable. Do not edit `.env` for this
upgrade. (The existing `MDHUB_SNAPSHOT_EOD_PRELIMINARY_TIME`, default `19:45`, decides when the evening cut is
taken; leave it as it is.)

## 3. Pre-flight (on 0.7.0, before the install)

### 3.1 Version and health

Console, page **Updates**: the block "Running" shows `Version 0.7.0` and the table below it lists the heartbeats of
`worker`, `hostmon` and `updater` with their versions. Page **Overview**: no red lights you
cannot explain; write down what is red so you can tell it from anything the upgrade causes. The
Updates page must show no queued or running request.

```bash
curl -sS http://127.0.0.1:8080/health/ready
```

- `curl -sS` as in 0.4, no token (`/health/*` is open); the URL is the API on the server's loopback port.

Expected (formatting may differ): `"status":"ok"`, `"version":"0.7.0"`, `"migrations":{"applied":53,"known":53,"pending":0,"checksum_mismatch":0}`.
Anything else: stop and report.

### 3.2 Backup, and can the console roll back to 0.7.0?

```bash
docker compose run --rm --no-deps worker mdhub ops backup
```

- `docker compose run` starts a one-off container of a service; `--rm` deletes it afterwards; `--no-deps` does
  not start the services it depends on (the database already runs); `worker` is the service whose image
  has the `mdhub` command line and the database access; `mdhub ops backup` runs a `pg_dump` now (keeps the
  newest 14 dumps). It uses the Compose files of 0.3 (`COMPOSE_FILE`).

Expected: a last line like `backup succeeded: /var/backups/mdhub/<file> (<n> bytes)`. The directory is
`backups/` next to the compose file or `shared/backups/` in the release layout ([README_DEPLOY.md](README_DEPLOY.md) §7).
The install in step 4 takes its own `pre-update-*.dump` as well. If the backup fails: stop and report.

The Updates page offers "Roll back to 0.7.0" once 0.8.0 has been installed through the console (the install
records 0.7.0 as the previous version). The rollback needs the directory `/opt/mdhub/releases/0.7.0` on disk
(checked in 0.3). If it is missing the console cannot roll back (the request ends `failed`, nothing changes):
**stop and tell the owner before you install.**

### 3.3 Is the monitor ready? (the owner answers)

Monitor 0.2.28 is released. Ask the owner: "Does the desk monitor already run 0.2.28 or newer?" and write the
answer down.

- **Yes**: go on.
- **No, not yet**: tell the owner that the monitor has to go first so that the first evening cut after the install
  is already used (1, "Consumer"). The owner decides: wait for the monitor, or install the hub now. An older monitor
  ignores the new rows in the evening cut; the hub install itself does not break it. Do not install the hub before
  the owner has answered.

### 3.4 Agree the time

Ask the owner for the day and hour, and write down the answer. The rules:

- Install on a business day, best **Monday to Thursday**, and start between **10:00 and 17:00 MSK**. MSK is
  UTC+3 all year; the console and the database show UTC, so this is 07:00-14:00 UTC. A business day, because the
  evening cut of the same day (19:45 MSK = 16:45 UTC) is then the first one with the new content and you can do the
  check of 5.3 the same evening; Monday to Thursday, because then a working day is left to react to what you find.
- **Do not install between 19:00 and 20:00 MSK** (16:00-17:00 UTC). The updater drains the worker (it waits up to
  10 minutes for running jobs) and restarts it; the EOD loads run at 19:05-19:30 and the cut at 19:45. If the install
  is not finished by **19:30 MSK (16:30 UTC)**, tell the owner at once: the cut at 19:45 needs a running worker.
- An install after 19:45 MSK is possible. Then the first cut with the new content is the one of the next business
  day (5.3 moves to that evening); the cuts in between are published as before, with `1.8` as the version string.

## 4. Install

### 4.1 Where the release comes from

The owner publishes release `v0.8.0` on GitHub ([README_DEPLOY.md](README_DEPLOY.md) §11): the archive
`market-data-hub-0.8.0.tar.gz`, `market-data-hub-0.8.0.SHA256SUMS` and `market-data-hub-0.8.0.SHA256SUMS.minisig`.
Only signed releases (signature by a key pinned in the running version) can be installed.

### 4.2 Install through the console (the normal path; README_DEPLOY §13 describes what happens)

1. Console, page **Updates**, step-up if asked. In "Releases on GitHub" the updater lists releases every
   10 minutes and downloads and verifies newer ones. Wait for the row `0.8.0` with the pill **verified**; its
   column "New migrations" must show `0`. If the pill is **refused**, or the row is still missing after two
   polling intervals (20 minutes) although the release is published: stop and report (with the text of the pill).
2. Click **Review and install** on that row. The confirmation page must say **"No new migrations: a rollback
   only switches the image tag back."** If it lists any migration: stop and report, this document does not cover
   that build. Read the release notes shown there.
3. Click **Install 0.8.0**. The page shows the log of the steps (refreshes every 2 seconds): verifying (the line
   `0.7.0 -> 0.8.0; new migrations: none`), backing up (a `pre-update-*.dump` in the backup directory), staging
   (the image is built), draining (the worker finishes running jobs, at most 10 minutes), migrating (nothing to
   apply), starting, checking (up to 3 minutes), done.
4. The request ends **done**. If it ends **failed** (nothing changed) or **rolled_back** (the updater went
   back to 0.7.0 by itself), copy the error text from the "Requests" table and stop: report it. Do not
   retry before the owner has seen it.

### 4.3 What must be true afterwards

- Updates: "Running" says `Version 0.8.0`; "Installed versions" lists `0.8.0` as `active`;
  the heartbeat table shows 0.8.0 for worker and hostmon (updater a few seconds later). The link
  "Roll back to 0.7.0" is under "Installed versions".
- ```bash
  curl -sS http://127.0.0.1:8080/health/ready
  ```
  gives `"status":"ok"`, `"version":"0.8.0"`, `"migrations":{"applied":53,"known":53,"pending":0,"checksum_mismatch":0}`.
- ```bash
  docker compose ps -a
  ```
  (`ps` lists the containers of the project and their state; `-a` includes stopped ones) shows the services
  `worker`, `api`, `admin` and `hostmon` on image `mdhub:0.8.0` and running or healthy, and `updater` on
  `mdhub:0.8.0` a few seconds later. `postgres` (and `caddy`, if present) are not images of ours and keep their own images.
  `migrate` is listed as `exited (0)`: **its image is an older tag, and that is not an error**: the updater
  runs the migrations in a one-off container that it deletes, and does not touch the old `migrate` container.
- ```bash
  ls -l /opt/mdhub/shared/backups/
  ```
  lists a `pre-update-...-0.7.0-to-0.8.0-....dump`. The directory is `shared/backups/` in the release layout.
- Overview shows nothing new in red. The worker log (0.4) contains `scheduler 0.8.0 starting with ... trigger(s)`
  and no `invalid configuration`.

### 4.4 When the console path is not available

- GitHub cannot be reached or the release is not listed: download the three release files on another
  machine and use **Upload a release** on the same page (the archive and its `.minisig`; 200 MB at most),
  then continue with step 2 above.
- The console itself is down: [README_DEPLOY.md](README_DEPLOY.md) §8 ("Upgrading") is the manual path
  for the plain layout, and §13 "Manual recovery" for the release layout. Check the archive first with
  `mdhub release verify` (§11). Do not restore the `db/mdhub.dump` of a bundle on an upgrade. If you
  get here, tell the owner before you do anything: the manual path of the release layout is not
  spelled out in the README.

## 5. After the install: look at the first evening cut

Nothing has to be run or switched on. What you do is look.

### 5.1 Right after the install (any time between 10:00 and 19:00 MSK): the contract version

Intraday cuts are published every 30 minutes from 10:00 to 19:00 MSK. Wait for the first one after the install
(at most about 30 minutes) and look at it:

```bash
SNAP=$(curl -sS -K "$CURLRC" "http://127.0.0.1:8080/v1/snapshots?kind=market&cut_type=intraday&latest=true" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["snapshot_id"])')
curl -sS -K "$CURLRC" "http://127.0.0.1:8080/v1/snapshots/$SNAP/manifest" | python3 -c '
import json, sys
m = json.load(sys.stdin)["data"]
print("cut_type", m["cut_type"], "| business_date", m["business_date"], "| cut_ts", m["cut_ts"])
print("schema_version", m["schema_version"], "| produced by", m["producer"]["version"])
'
```

- The first command: `SNAP=$(...)` puts the output of the command inside into the shell variable `SNAP`. Inside,
  `curl` (0.4) asks `/v1/snapshots` for the snapshots of `kind=market` and `cut_type=intraday` (`&` joins the
  query parameters; the URL is quoted so that the shell does not read the `&`) with `latest=true` = only the newest
  one that no correction supersedes. The Python program reads the answer, takes the first element of `data` and prints its
  `snapshot_id` (a 64-character hash, not a secret).
- The second command asks for the manifest of that snapshot (`/v1/snapshots/<id>/manifest`) and prints the cut,
  the contract version and the version of the hub that built it. The `'` on the first line opens the Python text,
  the `'` on the last line closes it.
- Expected: `schema_version market_snapshot/1.8` and `produced by 0.8.0`, and a `cut_ts` after the install. If the newest
  snapshot is still from before the install (`1.7`, `produced by 0.7.0`), wait for the next 30-minute cut; if it has not appeared
  within an hour, look at the Jobs page (filter job `snapshot_market`) and report the status and error of the last runs.
- Outside 10:00-19:00 MSK there is no new intraday cut; do 5.3 instead.

### 5.2 What to expect in the evening, and when

On a business day the job `snapshot_market` publishes the `eod_preliminary` cut at **19:45 MSK = 16:45 UTC** (the
run is short). Look at it any time after 19:50 MSK (16:50 UTC) on the day of the install if it
was installed before 19:30 (3.4), otherwise on the evening of the next business day.

### 5.3 The first `eod_preliminary` on 0.8.0

```bash
SNAP=$(curl -sS -K "$CURLRC" "http://127.0.0.1:8080/v1/snapshots?kind=market&cut_type=eod_preliminary&latest=true" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["snapshot_id"])')
curl -sS -K "$CURLRC" "http://127.0.0.1:8080/v1/snapshots/$SNAP/manifest" | python3 -c '
import json, sys
m = json.load(sys.stdin)["data"]
print("cut_type", m["cut_type"], "| business_date", m["business_date"], "| cut_ts", m["cut_ts"])
print("schema_version", m["schema_version"], "| produced by", m["producer"]["version"])
for x in m["datasets"]:
    if x["name"] in ("futures_prices_intraday", "futures_prices", "fx_swaps"):
        print(json.dumps(x, indent=2))
'
```

- The same two commands as in 5.1, with `cut_type=eod_preliminary`; the program now also prints the entries of
  three datasets of the manifest (`json.dumps(..., indent=2)` prints an entry as indented JSON; `for x in ...` and
  `if x["name"] in (...)` select the entries by name). Indentation in the Python text matters: copy it as shown.
- Expected for the cut of the day you look at (`business_date` is that day, `cut_ts` is 16:45 UTC):
  - `schema_version market_snapshot/1.8`, `produced by 0.8.0`;
  - the entry `futures_prices_intraday` has `"rows"` **greater than 0** (an evening cut of 0.7.0 and before had no such rows),
    `"stale": false`, and
    in `"coverage"`: **`"basis": "intraday_last_at_cut"`**, `"contracts"` equal to `"rows"`, and `"latest_ts"` and
    `"latest_source_ts"` **not later than `cut_ts`** (typically a minute or a few before it; both are UTC);
  - the entries `futures_prices` and `fx_swaps` still have `"rows": 0` and `"stale": true` with reasons like
    `no FORTS futures history on <date>` and `no MOEX FX swap rows on <date>`: ISS publishes the history of the day
    only for `eod_final`. **That is expected and is not a fault**; it is the reason for this change.
- Next morning (after 08:00 MSK) the `eod_final` cut of that day (`cut_type=eod_final`, the same commands) has
  `schema_version market_snapshot/1.8`, `futures_prices` with rows and `futures_prices_intraday` with
  `"coverage": {"not_in_cut": "eod"}`: unchanged from 0.7.0 except the version.
- Already published evening cuts of earlier days are not changed: you can see this with
  `business_date=<an earlier day>` added to the URL of the first command (`&business_date=<that day>`): its old version string (`1.7` or older), no futures rows. That is correct.

### 5.4 Symptom -> cause -> action

| what you see | probable cause | action |
|---|---|---|
| after 19:55 MSK the newest `eod_preliminary` is still of the previous day or says `1.7` | the cut has not run yet or failed; or the server was installed after 19:45 (3.4) | Jobs page, filter job `snapshot_market`: the run of 16:45 UTC, its status and Error. If it is `failed`, copy the text and stop: report it. Do not run it by hand |
| `futures_prices_intraday` has `"rows": 0`, `"stale": true`, reason `no intraday FORTS futures quotes on <date> at or before the cut` | no quote was stored before the cut: the job `futures_intraday` did not work that evening | Jobs page, filter job `futures_intraday`: statuses and Error of the runs before 16:45 UTC; page Sources for the futures sources. Report what you see. **Do not rebuild the cut by hand to fix it**: rows stored after the cut are "not known at the cut", so a rebuild gives the same empty set |
| rows are there, but `coverage` has no `basis` | you looked at an `intraday` cut, not at `eod_preliminary` (intraday cuts have no `basis`) | repeat the first command of 5.3 with `cut_type=eod_preliminary` |
| `latest_ts` or `latest_source_ts` later than `cut_ts` | not expected: the cut must not hold a quote from after it | stop and report, with the two values and the `snapshot_id` |
| Python error `IndexError` or `KeyError` | the answer holds no snapshot of that kind, or an error text instead of data | run the `curl` part alone and read the answer: `401` = the token of 0.5 is missing, expired or not `read`; an empty `data` = no such snapshot yet |

### 5.5 What not to do

- Do not use "Run now" with `eod_preliminary` on the Jobs page to see the new content earlier. It publishes a real
  snapshot: before 19:45 MSK of the day it would publish a cut with fewer quotes than the scheduled one, and
  the scheduled run would then follow it with a correction (`supersedes`); for an earlier day it publishes a
  correction of a cut that consumers already hold. Consumers download both.
- Do not try to give the old evening cuts the futures. They stay as published (1).

### 5.6 For the owner: what the monitor shows

Tell the owner that after the first evening cut on 0.8.0 the monitor (0.2.28 or newer) is expected to build the
USD and EUR FX-implied curves from the evening cut as well, not only from `eod_final`. The symptom that went away
is the monitor's message `no USD swap point or future in the snapshot` between about 19:45 and 08:00 MSK (it is the
text quoted in FlyingScotman/market-data-hub#154). What the monitor's screens call it is the monitor's business and
is described in its own notes; this document does not name them. Write down what the owner says.

## 6. Rollback

Decide with the owner; do not roll back on your own unless the update did it by itself.

- **During the install** the updater rolls back by itself when the post-install check fails (request state
  `rolled_back`): the services go back to 0.7.0. There are no migrations to undo, so it does not restore the
  database: it only switches the image tag and the `current` link back.
- **Later**, Updates page, "Installed versions": **Roll back to 0.7.0**; the confirmation page must say "No
  migration has to be undone: the rollback switches the image tag back and takes seconds." Then the red
  button. The link exists because the install recorded 0.7.0 as the previous version; the rollback needs the
  release directory `/opt/mdhub/releases/0.7.0` (3.2). If it is missing, the request ends `failed` with nothing
  changed: **stop and ask the owner**; do not look for another way. If the confirmation page instead lists
  migrations that 0.7.0 does not know, or warns that a dump will be restored, **stop**: something that this
  document does not expect has been applied to the database.
- **What is lost.** Nothing in the database: no dump is restored. Snapshots that 0.8.0 published (`1.8`, the evening
  cut with its futures rows, new `eod_final` and corrections) stay in the database and stay readable by the same
  ids; a consumer that already holds them keeps valid ids. After the rollback 0.7.0 publishes `1.7` again and its evening cut
  has no futures, so the monitor is again without the evening FX-implied curves. Tell the owner.
- **Settings after the rollback**: none were added, nothing to clean.
- If the update ends `failed` ("rollback failed"), follow [README_DEPLOY.md](README_DEPLOY.md) §13 "Manual
  recovery" only with the owner's agreement.

## 7. Result to return to the owner

Send this list, filled in. **No secret values, no proxy URL, no token.** Before you send it, go through
the checklist: every box below must be either filled in or explained.

| item | result |
|---|---|
| shell: root (`sudo -i`) used throughout (yes/no) | |
| layout (release layout / plain); `releases/0.7.0` present (yes/no) | |
| Compose files in use (the container label and `docker compose ls` of 0.3 step 2, which agreed, file names), `COMPOSE_FILE` set and the check of 0.3 step 4 passed (yes/no) | |
| owner's answer: monitor on 0.2.28 or newer (yes / no, installed anyway by the owner's decision / waited) | |
| agreed install time; real start and end of the install (MSK), weekday | |
| version before / after (`/health/ready`) | 0.7.0 / 0.8.0 |
| migrations after: applied / known / pending / checksum_mismatch | 53 / 53 / 0 / 0 expected |
| the confirmation page said "No new migrations" (yes/no); the log line `0.7.0 -> 0.8.0; new migrations: none` (yes/no) | |
| backup taken before the install (file name, size); `pre-update-*.dump` present after the install (yes/no) | |
| install request: state, start and end time (UTC) | |
| first intraday cut after the install (5.1): `cut_ts`, `schema_version`, producer version | expected `market_snapshot/1.8`, 0.8.0 |
| first `eod_preliminary` on 0.8.0 (5.3): `business_date`, `schema_version`, producer version | expected `market_snapshot/1.8`, 0.8.0 |
| `futures_prices_intraday` of that cut: `rows`, `stale`, `basis`, `contracts`, `latest_ts`, `latest_source_ts` (all not later than `cut_ts`: yes/no) | `rows` > 0, `false`, `intraday_last_at_cut` |
| `futures_prices` and `fx_swaps` of that cut still empty (yes/no; expected yes) | |
| next morning's `eod_final` of that day: `schema_version`, `futures_prices` rows, `futures_prices_intraday` coverage `not_in_cut` (yes/no) | |
| what the owner said about the monitor on the evening cut (5.6) | |
| API token `upgrade-check` revoked (yes/no); temporary file `$CURLRC` deleted (yes/no) | |
| everything that did not match this document | |

After sending it: console page **Tokens**, **Revoke** for `upgrade-check`, and in the shell

```bash
rm -f "$CURLRC"; unset CURLRC
```

- `rm` removes files; `-f` ignores a missing file; `"$CURLRC"` is the file of 0.5 that holds the token; `unset CURLRC`
  forgets the variable. Also `unset COMPOSE_FILE COMPOSE_PROFILES` or close the root shell.
