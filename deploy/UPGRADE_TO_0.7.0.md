# 0.7.0 — snapshot self-heal, carried curves (contract 1.7), history load, Ratescalcs address from settings

Instructions for the person or agent who installs 0.7.0 on the hub server and moves the Ratescalcs
source to its new address. You need no knowledge of the code: every step says what to do, how to
see that it worked and what to do if it did not. Where the result is not what this document
expects, **stop and report to the owner** instead of improvising.

Starting point: the server runs **0.6.2** ([UPGRADE_TO_0.6.2.md](UPGRADE_TO_0.6.2.md)). 0.7.0 can be
installed over any version from 0.1.0 (`min_previous_version`), so there is no intermediate release.
If the server runs something else, write the version down and tell the owner before you continue.

## 0. Before you start

### 0.1 What you need

- A console account with the role **admin** and a fresh second-factor check ("step-up"; the pages
  Updates, Credentials and Tokens ask for it), from an IP that the console allow-list accepts
  ([README_DEPLOY.md](README_DEPLOY.md) §12). The role **operator** is enough for "Run now" on the Jobs page.
- A shell on the hub server with the right to run `docker compose` and to edit the server
  configuration file `.env`.
- From the owner, **separately from this document** (never in a ticket, issue, pull request or
  chat that others can read): the new address of the Ratescalcs source (this document writes
  `<address of the source>`, and `<port>` when it is not the standard https port 443).
  The owner has said: the source now has a bare IP address, is reached by **https**, and its pages
  have **no path prefix** any more.

### 0.2 Rules that apply to every step

1. **The address of the source is written only into the server's `.env`** (step 5.4) and typed
   into the commands that need it on the server itself. It does not go into any file of this
   repository, an issue, a pull request, a log excerpt, a screenshot or your report. Since 0.7.0 the
   hub itself keeps it out of the database and the API (section 1); do not undo that by hand.
2. Likewise no values of secrets (passwords, tokens, the proxy URL, `MDHUB_SECRETS_PRIVATE_KEY`).
   Report **names, states and counts**.
3. Certificate verification cannot be switched off by any setting. Do not look for a way.
4. Do not roll back, delete rows or change settings that this document does not name, unless the
   owner says so.

### 0.3 Where commands run

All `docker compose` commands run in the `deploy` directory of the running release. In the release
layout of [README_DEPLOY.md](README_DEPLOY.md) §13 (default root `/opt/mdhub`) that is
`/opt/mdhub/current/deploy`, and the configuration file is `/opt/mdhub/shared/.env` (linked as
`deploy/.env`). Check the layout:

```bash
ls -l /opt/mdhub/current
```

- `ls` lists files; `-l` long format, so a symbolic link is shown as `current -> releases/X.Y.Z`;
  `/opt/mdhub/current` is the path asked about.
- A line starting with `l` and `-> releases/0.6.2` means the release layout. "No such file" means a
  plain unpacked archive (§2-3 of the README): use the `deploy` directory of that unpack instead of
  `/opt/mdhub/current/deploy` and its `deploy/.env` instead of `/opt/mdhub/shared/.env`. Then the
  console's Updates page may be unavailable: see 4.4.

If `MDHUB_UPDATE_COMPOSE_OVERRIDE` is set in `.env` (a site-specific Compose file, see
[DOMAIN_DEPLOY.md](DOMAIN_DEPLOY.md)), every `docker compose` command below needs the same files the
updater uses: `docker compose -f docker-compose.yml -f <that file> ...`. Check:

```bash
grep -q '^MDHUB_UPDATE_COMPOSE_OVERRIDE=.' /opt/mdhub/shared/.env && echo "override configured" || echo "no override"
```

- `grep` searches a file for a pattern; `-q` prints nothing and only sets the exit status;
  the pattern `'^MDHUB_UPDATE_COMPOSE_OVERRIDE=.'` means: a line that starts (`^`) with that name
  and `=` followed by at least one character (`.`), i.e. a non-empty value; the last argument is
  the file.
- `&&` runs the next command only if the previous one succeeded (pattern found); `||` runs the next
  one only if it failed; `echo "..."` prints the text. Nothing from the file is printed.

### 0.4 Commands that come back several times

Each is shown in full where it is used; this is what its parts mean.

- `docker compose logs --tail 100 worker`: `docker compose` is the Compose plugin of Docker;
  `logs` prints the output of a service's container; `--tail 100` only the last 100 lines;
  `worker` is the service (the scheduler that runs all jobs). The **full texts of errors, including
  the address of the source, are only here**; the database and the API keep a placeholder.
  Container logs are kept in 5 files of 20 MB.
- `docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`: `exec` runs a
  command inside a running container; `postgres` is the database service; `sh -c '...'` starts a
  shell inside the container that runs the quoted text (the single quotes make `$POSTGRES_USER`
  and `$POSTGRES_DB` be read inside the container, where compose has set them, not by your shell);
  `psql` is the PostgreSQL client; `-U` the database user, `-d` the database name. It opens an
  interactive session as the owner role; leave it with `\q`. For one statement without a session
  use `exec -T` (no terminal) and add `-c "<SQL>"` inside the quotes.
- `curl -sS -H "Authorization: Bearer $MDHUB_TOKEN" "http://127.0.0.1:8080<path>"`: `curl` sends an
  HTTP request; `-s` hides the progress bar and `-S` still shows an error; `-H` adds a header, here
  the bearer token that the read API requires for `/v1/...` (the variable is set in 0.5); the
  last argument is the URL: the API listens on the server's loopback port 8080 (compose publishes
  `127.0.0.1:${MDHUB_API_HOST_PORT:-8080}`). `/health/...` needs no token. Append
  `| python3 -m json.tool` to print the answer indented (`|` passes the output of the left command to the
  right one; `python3 -m json.tool` is a JSON formatter).

### 0.5 A read token for the API checks (optional but used below)

Console, page **Tokens**: Name `upgrade-check`, scope `read`, "Expires after (days)" `2`, **Create**. Copy the
token (it is shown once). In the server shell:

```bash
read -rs MDHUB_TOKEN
```

- `read` reads one line from the keyboard into a variable; `-r` takes backslashes literally; `-s`
  does not echo what you type; `MDHUB_TOKEN` is the variable name. Paste the token and press Enter.
  The variable lives only in this shell session (bash).

Revoke the token on the Tokens page at the end (section 9).

## 1. What comes in 0.7.0

Source: the changes since `v0.6.2`, pull request FlyingScotman/market-data-hub#142 and
[SNAPSHOT_HEAL.md](../docs/SNAPSHOT_HEAL.md). Contract: [schemas/market_snapshot/v1/README.md](../schemas/market_snapshot/v1/README.md).

- **Self-healing of the `eod_final` history** (job `snapshot_heal`, on by default). Every 30 minutes
  it finds business days without an `eod_final` market snapshot and cuts that sit on the static of
  their build day, and in the **night window 21:00-06:00 MSK** it publishes the missing ones and
  reissues the others (a correction that `supersedes` the old snapshot, which stays readable), at most
  10 per run with a 20-second pause. See "The first night" below.
- **Contract `market_snapshot/1.7`** (additive). When no source delivered a curve of the carried set
  (default `ruonia_ois`, `key_rate_swap`) on the day of a market cut, the cut takes the curve from the
  latest earlier day that has it and **marks it** (`carried_forward` on the quote set, `carried_curves`
  in the envelope and manifest). A snapshot of a day without such a gap is byte for byte what a
  1.6 hub published, except `schema_version` (now `market_snapshot/1.7` in every new snapshot).
  A carried cut is reissued when real data of a later day is stored afterwards.
- **`GET /v1/carried-curves`**: the curves carried in the current market snapshots (section 6.1).
  The `quality` report lists them too (`summary.carried_curves`).
- **History load of new datasets** (`MDHUB_SNAPSHOT_HEAL_HISTORY`, **off by default**). A dataset that
  is new to the hub and whose earliest stored day is later than its registered start gets its history
  backfilled through the ordinary backfill queue, and the `eod_final` cuts of the last 12 months are
  reissued once afterwards. While it is off the job only **previews** what it would do (section 6.2).
- **Ratescalcs address from settings** (`MDHUB_RATESCALCS_BASE_URL`, `MDHUB_RATESCALCS_TLS_SERVER_NAME`):
  scheme, host, optional port and optional path prefix of the source are settings, not constants.
  A bare IP address can be used with a certificate issued for a name (section 5).
- **The configured address is not stored and not served.** Error texts, capture URLs, probe rows,
  backfill and request errors, gap notes and quality reports show a placeholder:
  `<MDHUB_RATESCALCS_BASE_URL>` for the address and `<server name>` for the TLS name. The full text
  is only in the worker log. Rows written by earlier versions are not rewritten (section 7).
- Small: an unconfigured source now ends a run as `skipped` with a text that starts with
  `source_not_configured:`; the advisory lock of a job is released explicitly; the calendar of a long
  range is read with one query. No action needed.

### Migrations 0840, 0850, 0860

All three are additive; no existing row is rewritten.

| migration | what it does |
|---|---|
| `0840_dataset_history_load` | creates `ops.dataset_history_load` (one row per dataset whose history load was detected: status `loading` / `reissue` / `done`) |
| `0850_dataset_history_load_waiting` | adds the columns `waiting_reason` and `failed_ranges` to it |
| `0860_dataset_history_load_tasks` | adds the column `task_ids` (the backfill tasks that belong to a load) |

0.6.2 has 50 migrations, 0.7.0 has 53. After the install `/health/ready` must say `applied` 53,
`known` 53, `pending` 0, `checksum_mismatch` 0.

### The first night after the install

The job is on (`MDHUB_SNAPSHOT_HEAL=repair`). Nobody has to run anything, but you will see:

- From the first :00 or :30 after the worker starts, `snapshot_heal` runs record what is wrong as gaps
  (datasets `snapshot_cut_missing`, `snapshot_cut_reissue`, `snapshot_cut_carried`). In the daytime the
  run ends `skipped` with a text like `N missing, M to reissue, ...; builds only 21:00-06:00 (MSK)`.
  **That is normal.** N and M are the real sizes.
- In the night window the hub publishes the missing cuts and the corrections. By the estimate in
  [SNAPSHOT_HEAL.md](../docs/SNAPSHOT_HEAL.md) it is about 45 new `eod_final` cuts and about 204
  corrections (business dates 2026-01-05 .. 2026-10-21): a night and a half. Each cut is one read-only
  transaction of about a minute plus a short publish; up to 10 per 30 minutes, so up to 180 per night. Jobs and
  live cuts are not blocked.
- **Consumers download these snapshots again** (about 249 new snapshot ids, new days and replacements
  of existing days). Warn whoever runs a consumer of `eod_final` before the first night.
- Afterwards the runs end `skipped` with `nothing to heal` and publish nothing.

Watch: Jobs page, filter job `snapshot_heal` (`/jobs?job=snapshot_heal`); and the gaps, in an interactive `psql`
session (0.4):

```sql
SELECT dataset, status, count(*) FROM ops.data_gap
WHERE dataset LIKE 'snapshot\_cut\_%' GROUP BY dataset, status ORDER BY dataset, status;
```

- `SELECT ... FROM ops.data_gap` counts the rows of the gap table per dataset and status;
  `LIKE 'snapshot\_cut\_%'` takes the datasets whose name starts with `snapshot_cut_` (`\_` is a
  literal underscore, `%` any text); `GROUP BY` makes one row per dataset and status; `count(*)` is the
  number of gaps. Expected: `open` falls to 0 over the nights, `backfilled` grows. A cut given up after
  repeated failures becomes `unfillable`: report its `note` (`SELECT note FROM ops.data_gap WHERE status = 'unfillable' AND dataset LIKE 'snapshot\_cut\_%'`).

## 2. All new and changed settings since 0.6.2

Set in the server's `.env` (see `deploy/env.example`, which lists them commented out). A setting
that is unset or empty uses its default. The worker reads settings **when it starts**
(`docker compose up -d worker` applies a change, see 5.5). A value that cannot be parsed stops the
worker (and the API, which reads the same file) at the next start; 5.5 shows how to check first.
**Nothing in this table has to be changed for the installation itself.** The right-hand column says
what is expected of you in this upgrade.

| name | default | what it does | when to change | in this upgrade |
|---|---|---|---|---|
| `MDHUB_RATESCALCS_BASE_URL` | unset = the address the code used before 0.7.0 | the full base URL of the Ratescalcs source: scheme `http` or `https`, host (a name or an IP address), optional `:port`, optional path prefix. Pages are requested from `<base>/<page>`. No user name, password, query or fragment (the process refuses to start with such a value) | when the source moves | **set** (section 5) |
| `MDHUB_RATESCALCS_TLS_SERVER_NAME` | unset | only for a base URL that is a bare IP address whose certificate is issued for a **name** (does not list the IP): that name. The hub still connects to the base URL but sends the name as TLS server name (SNI) and `Host` and checks the certificate against it. Verification is never switched off. Must be a host name, not an IP address or URL. Needs an `https` base URL and **no `MDHUB_HTTP_PROXY`** (see below) | only if the Check in 5.6 says "not valid for ..." | **maybe** (5.3, 5.4) |
| `MDHUB_RATESCALCS_ALLOW_PLAINTEXT_CREDENTIALS` | `false` | with an `http` base URL and Ratescalcs credentials set, the worker refuses to start unless this is `true` (then it starts with a WARNING and every probe says "credentials are sent unencrypted"). Not combinable with the TLS server name | never without a decision of the owner | **leave unset** |
| `MDHUB_HTTP_PROXY` (existing; changed rule) | unset | egress proxy for **all** sources; may also be set on the console page Credentials (a console value wins over `.env`). Since 0.7.0 it cannot be combined with `MDHUB_RATESCALCS_TLS_SERVER_NAME` (a proxy tunnel presents the host of the URL, not the name) | - | **check** it is empty if a TLS name is needed (3.5) |
| `MDHUB_SNAPSHOT_HEAL` | `repair` | `repair`: record gaps and build/reissue cuts; `detect`: only record gaps; `off`: the job does nothing | `detect` or `off` to stop the first-night wave | none (the wave is wanted) |
| `MDHUB_SNAPSHOT_HEAL_FROM` | `2025-10-29` | first business date the job looks at | when the 12-month window moves | none |
| `MDHUB_SNAPSHOT_HEAL_WINDOW` | `21:00-06:00` | MSK hours (zone of `MDHUB_TZ`) in which cuts are built, `HH:MM-HH:MM`, may wrap midnight | to move the load | none |
| `MDHUB_SNAPSHOT_HEAL_MAX_PER_RUN` | `10` (1-200) | cuts built per run (every 30 minutes) | slower or faster repair | none |
| `MDHUB_SNAPSHOT_HEAL_PAUSE_S` | `20` (0-600) | seconds between two cuts | as above | none |
| `MDHUB_SNAPSHOT_HEAL_HISTORY` | `false` | history load of new datasets and the one-off reissue after it (works only with `repair` and only in the night window). Off: preview only | **switch on in section 6.2** | **set later** |
| `MDHUB_HISTORY_LOAD_WINDOW` | `00:30-06:00` | MSK hours in which the backfill worker takes the tasks of a history load; not the window of the cuts | to move the load | none |
| `MDHUB_HISTORY_LOAD_MAX_TASKS_PER_NIGHT` | `30` (1-1000) | history-load tasks taken per window (a task is up to 31 days of a daily dataset: 30 tasks are about 2.5 years) | slower or faster load | none |
| `MDHUB_CURVE_CARRY_DATASETS` | `key_rate_swap,ruonia_ois` | curves that are carried forward when no source delivered them on a cut's day; empty = no carry. Valid names: `cny_futures_yields`, `cny_ruonia_xccy`, `cny_rusfar_ois`, `key_rate_swap`, `ruonia_ois`, `usd_futures_yields`; an unknown name stops the start | to carry fewer or more curves | none |
| `MDHUB_CURVE_CARRY_MAX_AGE_DAYS` | `5` (0-3650) | working days after which a carried curve's marker says `age_limit_exceeded` (the curve is **still served**: it is a marker, not a cut-off) | - | none |

New settings of 0.7.0 are only these; nothing else in `src/mdhub/config.py` or `deploy/env.example` changed
since 0.6.2. The compose file does not need new variables: the worker and the API read `.env` as a whole
(the console reads only the variables compose lists for it, none of the new ones).

## 3. Pre-flight (on 0.6.2, before the install)

### 3.1 Version and health

Console, page **Updates**: the block "Running" shows `Version 0.6.2` and the table below it lists the heartbeats of
`worker`, `hostmon` and `updater` with their versions. Page **Overview**: no red lights you
cannot explain; write down what is red so you can tell it from anything the upgrade causes. The
Updates page must show no queued or running request.

```bash
curl -sS http://127.0.0.1:8080/health/ready
```

- `curl -sS` as in 0.4, no token (`/health/*` is open); the URL is the API on the server's loopback port.

Expected (formatting may differ): `"status":"ok"`, `"version":"0.6.2"`, `"migrations":{"applied":50,"known":50,"pending":0,"checksum_mismatch":0}`.
Anything else: stop and report.

### 3.2 Backup

```bash
cd /opt/mdhub/current/deploy
docker compose run --rm --no-deps worker mdhub ops backup
```

- `cd` changes into the directory (see 0.3). `docker compose run` starts a one-off container of a
  service; `--rm` deletes it afterwards; `--no-deps` does not start the services it depends on (the database
  already runs); `worker` is the service whose image has the `mdhub` command line and the database access;
  `mdhub ops backup` runs a `pg_dump` now (keeps the newest 14 dumps).

Expected: a last line like `backup succeeded: /var/backups/mdhub/<file> (<n> bytes)`. The directory is
`backups/` next to the compose file or `shared/backups/` in the release layout ([README_DEPLOY.md](README_DEPLOY.md) §7).
The install in step 4 takes its own `pre-update-*.dump` as well. Also make sure that a copy of `.env` is
kept off the server (it holds `MDHUB_SECRETS_PRIVATE_KEY`; README §7, §14); ask the owner if you do not know.
If the backup fails: stop and report.

### 3.3 Ratescalcs before the move

Console, page **Credentials**: for `RATESCALC_USERNAME` and `RATESCALC_PASSWORD` the state must be
`console`, `.env fallback` or `both`, not `not set` (otherwise the job is skipped with
`source_not_configured:` and there is nothing to move). Page **Sources**, row `ratescalcs`: write down
what it shows now (the source has moved, so a failure of the old address is expected).

### 3.4 Is the compose override configured?

See 0.3. Write down yes or no.

### 3.5 Is an egress proxy configured?

```bash
grep -q '^MDHUB_HTTP_PROXY=.' /opt/mdhub/shared/.env && echo "proxy set in .env" || echo "no proxy in .env"
```

- Same construction as in 0.3. The proxy URL may contain a password, so the command prints only the
  answer, never the line.

Then look at the console page **Credentials**, row `MDHUB_HTTP_PROXY`: state `not set` means no proxy anywhere;
`console`, `.env fallback` or `both` means a proxy is configured (a console value wins). **A proxy
is configured if either place says so.** Write down yes or no. It matters only if step 5.3 shows that a TLS
server name is needed: that combination is refused.

### 3.6 Was the new address used before 0.7.0?

On 0.6.2 the address of the source was a constant of the code, so it can be in the database only if this
server (or a copy of it) once ran with the new address in some way (a locally patched build, an address
typed into a note). Ask the owner. If the answer is "never", you still run the counts of section 7 now:
**the baseline** (expected all 0) that you compare with after the move. If the answer is "yes" or
"not sure", the baseline is the number of old rows (section 7.2).

### 3.7 Tell the owner

Before you install, make sure the owner knows about the first-night wave and the re-download by consumers
(section 1) and the time you intend to install. Write down the time.

## 4. Install

### 4.1 Where the release comes from

The owner publishes release `v0.7.0` on GitHub ([README_DEPLOY.md](README_DEPLOY.md) §11): the archive
`market-data-hub-0.7.0.tar.gz`, `market-data-hub-0.7.0.SHA256SUMS` and `market-data-hub-0.7.0.SHA256SUMS.minisig`.
Only signed releases (signature by a key pinned in the running version) can be installed.

### 4.2 Install through the console (the normal path; README_DEPLOY §13 describes what happens)

1. Console, page **Updates**, step-up if asked. In "Releases on GitHub" the updater lists releases every
   10 minutes and downloads and verifies newer ones. Wait for the row `0.7.0` with the pill **verified**.
   If it shows **refused**, or is still missing after two polling intervals (20 minutes) although the release
   is published: stop and report (with the text of the pill).
2. Click **Review and install** on that row. The confirmation page must list **exactly three new
   migrations**: `0840_dataset_history_load`, `0850_dataset_history_load_waiting`,
   `0860_dataset_history_load_tasks`. Different migrations: stop and report. Read the release notes
   shown there.
3. Click **Install 0.7.0**. The page shows the log of the steps (refreshes every 2 seconds): verifying,
   backing up (a `pre-update-*.dump` in the backup directory), staging (the image is built), draining
   (the worker finishes running jobs, at most 10 minutes), migrating, starting, checking (up to 3
   minutes), done.
4. The request ends **done**. If it ends **failed** (nothing changed) or **rolled_back** (the updater went
   back to 0.6.2 by itself), copy the error text from the Requests table and stop: report it. Do not
   retry before the owner has seen it.

### 4.3 What must be true afterwards

- Updates: "Running" says `Version 0.7.0`; "Installed versions" lists `0.7.0` as `active`;
  the heartbeat table shows 0.7.0 for worker and hostmon (updater a few seconds later).
- ```bash
  curl -sS http://127.0.0.1:8080/health/ready
  ```
  gives `"status":"ok"`, `"version":"0.7.0"`, `"migrations":{"applied":53,"known":53,"pending":0,"checksum_mismatch":0}`.
- ```bash
  cd /opt/mdhub/current/deploy && docker compose ps -a
  ```
  (`ps` lists the containers of the project and their state; `-a` includes stopped ones; `&&` runs the
  second command only if `cd` worked) shows `migrate` exited with code 0, the others running or healthy,
  images `mdhub:0.7.0`.
- Overview shows nothing new in red. The worker log (0.4) contains `scheduler 0.7.0 starting with ... trigger(s)` and no `invalid configuration`.
- Within about 30-40 minutes Jobs shows a `snapshot_heal` run (`skipped` in the daytime, section 1).

### 4.4 When the console path is not available

- GitHub cannot be reached or the release is not listed: download the three release files on another
  machine and use **Upload a release** on the same page (the archive and its `.minisig`; 200 MB at most),
  then continue with step 2 above.
- The console itself is down: [README_DEPLOY.md](README_DEPLOY.md) §8 ("Upgrading") is the manual path
  for the plain layout, and §13 "Manual recovery" for the release layout. Check the archive first with
  `mdhub release verify` (§11). Do not restore the `db/mdhub.dump` of a bundle on an upgrade. If you
  get here, tell the owner before you do anything: the manual path of the release layout is not
  spelled out in the README.

## 5. Move Ratescalcs to the new address

Do this **after** 4.3 is satisfied (0.6.2 ignores these settings). You have the address and, if it is not
443, the port.

### 5.1 What you will set

| in `.env` | value |
|---|---|
| `MDHUB_RATESCALCS_BASE_URL` | `https://<address of the source>` (or `https://<address of the source>:<port>`); an IPv6 address goes in brackets: `https://[<address>]`. **No path after it**: the old address had a prefix, the new one has none, and the hub appends `/snapshot`, `/key-rate-editor/ois`, `/key-rate-editor/krs`, `/cny-xccy-curve` itself |
| `MDHUB_RATESCALCS_TLS_SERVER_NAME` | only if 5.3 says so |
| `RATESCALC_USERNAME`, `RATESCALC_PASSWORD` | **unchanged**: in `.env` or on the Credentials page (a console value wins). Change them only if the owner says the source now wants other ones |
| `MDHUB_HTTP_PROXY` | must be **empty** when a TLS server name is set |

### 5.2 First try with the base URL alone

Do 5.4 and 5.5 with only `MDHUB_RATESCALCS_BASE_URL`, then 5.6. The hub's own check is the judge of the
certificate: if the Check passes, 5.3 is not needed at all; it applies only when 5.6 fails with a
certificate error.

### 5.3 If the Check says the certificate is not valid for the address

The Sources/Credentials error then reads like `TransportFailure: TLS certificate check failed for
<MDHUB_RATESCALCS_BASE_URL>/snapshot: IP address mismatch, certificate is not valid for ... (if the certificate
is issued for a name and not for the IP address of the base URL, set MDHUB_RATESCALCS_TLS_SERVER_NAME to that
name)`. Find out which names the source's certificate lists:

```bash
openssl s_client -connect <address of the source>:<port or 443> -showcerts </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```

- `openssl s_client` is a TLS test client; `-connect HOST:PORT` the server to connect to (use the
  standard port 443 unless the owner gave another); `-showcerts` prints all certificates the server sends,
  the server's own first; `</dev/null` gives it empty input so it ends after the handshake instead of
  waiting for the keyboard; `2>/dev/null` throws away its progress messages (stderr).
- `|` hands the output to `openssl x509`, which reads one certificate (the first); `-noout` does not print
  the certificate itself; `-subject`, `-issuer`, `-dates` print who it was issued to, by whom and the validity
  period; `-ext subjectAltName` prints the extension with the names it is valid for (OpenSSL 1.1.1 or newer).
- To see what the server presents for a given name (a server may hold several certificates and choose by
  the name the client announces), add `-servername <name>` after `-connect ...`. Without it no name is
  announced, as when the hub connects to a bare IP.

How to read the output:

| you see | meaning | do |
|---|---|---|
| `IP Address:<address of the source>` among the names | the certificate lists the IP | no TLS name is needed. If the Check still fails with "not valid for", the certificate the hub got is not this one: report |
| only `DNS:` names | the certificate is issued for names | take the name the owner confirms (a name from the list, or a host name that a wildcard `DNS:*.example.org` covers) as `MDHUB_RATESCALCS_TLS_SERVER_NAME`; the value is a host name only (no `https://`, port or IP) |
| no `subjectAltName` at all | the names are unknown to this check | ask the owner which name the certificate is issued for; the hub's Check decides |
| `subject` equals `issuer` (self-signed), or the issuer is unknown to you | untrusted issuer | see below |

**Self-signed or untrusted issuer: stop and return it to the owner.** The hub trusts the certifi root bundle
and the Russian Trusted Root and Sub CA, which are built into the image
(`src/mdhub/certs`); the HTTP client ignores the environment (`trust_env=False`), so `SSL_CERT_FILE` and
the system store of the container are not read; there is no setting to add a certificate authority or to
skip verification. The only way to trust another authority is a change of the image (code). Report the
`issuer` line (not the address) and the Check text (`self-signed certificate`, `unable to get local issuer
certificate`).

If a TLS name is needed and a proxy is configured (3.5), the two cannot be combined either: report both
facts. The owner's options, which follow from the code, are to give the source a certificate that lists
the IP (then no name is needed and the proxy may stay), or to unset the proxy for all sources.

### 5.4 Edit the configuration file

```bash
sudoedit /opt/mdhub/shared/.env
```

- `sudoedit` edits the file with your editor (`$SUDO_EDITOR`, `$VISUAL` or `$EDITOR`) through a temporary copy and
  writes it back with root rights. If it refuses because the path is a symbolic link, edit the real
  file: `readlink -f /opt/mdhub/shared/.env` prints its target (`readlink` shows where a link points;
  `-f` follows all links to the final path).

Add (or fill in) these lines; type the address into the editor, do not put it on a command line:

```
MDHUB_RATESCALCS_BASE_URL=https://<address of the source>
# only when 5.3 showed that a name is needed:
MDHUB_RATESCALCS_TLS_SERVER_NAME=<certificate name>
```

Lines that start with `#` are comments. A line with an empty value counts as unset. If a TLS name is
set, `MDHUB_HTTP_PROXY` must be empty (5.3). Do not change any other line.

### 5.5 Check, then apply

Check the values first. Start a one-off container of the new image that only loads the settings and the
Ratescalcs rules (the same ones the worker applies at its start):

```bash
cd /opt/mdhub/current/deploy
docker compose run --rm --no-deps worker python -c 'from mdhub.config import Settings; Settings().require_ratescalcs_config(); print("settings ok")'
```

- `docker compose run --rm --no-deps worker` as in 3.2; `python -c '...'` runs the quoted Python text with
  the image's Python; it builds the `Settings` from the environment compose passes (all of `.env`) and
  runs the Ratescalcs consistency check. The error messages name the setting and never print its value.

Expected: `settings ok`. Otherwise the last lines of the output (above pydantic's "For further information"
line) name the setting and the reason (`MDHUB_RATESCALCS_BASE_URL: not a valid URL (the port must be 1-65535 or left out)`, `the scheme must be http or
https`, `no user name or password in the URL`, `no query string or fragment in the URL`,
`MDHUB_RATESCALCS_TLS_SERVER_NAME: a host name is expected, not an IP address`, or one of the rows in the
table of 5.8). Fix the line and repeat. **A proxy set on the console page Credentials is not seen by this
check**; it is checked at every run (5.8).

Then apply. The worker reads settings only at its start. Do it when the Jobs page shows nothing under "Running
now" (a run cut off by the restart is cleaned up at the next start, but a night build of a cut would be lost):

```bash
docker compose up -d worker
docker compose ps worker
docker compose logs --tail 50 worker
```

- `up -d worker` creates or re-creates the service's container with the current configuration and starts it
  in the background (`-d` = detached); changed `.env` values make compose re-create it. The worker finishes
  running jobs for up to 120 seconds before it stops. `ps worker` shows its state; `logs --tail 50 worker`
  as in 0.4.

Expected: the output of `up` says the container was **Recreated** / **Started** (if it only says `Running`,
nothing was re-read: use `docker compose up -d --force-recreate worker`); `ps` shows `running` (not `restarting`);
the log shows `scheduler 0.7.0 starting with ... trigger(s)` and no `invalid configuration`.
The API does not use these settings; it needs no restart now (it reads the same file at its next start, which is
why a malformed value must be fixed, not left).

### 5.6 Check the source

1. Console, page **Credentials**, row `RATESCALC_USERNAME`, button **Check** (it queues a probe for the
   worker; the result appears after a few seconds in "Last check"; reload the page). Expected: pill **probe
   accepted**. On page **Sources**, row `ratescalcs`: "HTTP response received", **passed**, HTTP `200`, "TLS expiry"
   filled, "Error" empty. The scheduled probe runs every 5 minutes, but only Mon-Fri 08:50-23:00 MSK, when
   the service answers; a Check is forced at any time, yet the source itself may not answer outside those hours.
2. Console, page **Jobs**, row `swaps_ratescalcs`, button **Run now** (operator). Under "Requested runs" the
   request goes `queued` -> `running` -> `done`, with a run id. Open `/jobs?job=swaps_ratescalcs`: the run must be
   `succeeded` with an empty error. The first successful run of a business day stores the day's curves, so
   `rows_inserted` is above 0. The job also runs by itself every 30 minutes 09:00-23:00 MSK on business
   days.
3. Same with the API:
   ```bash
   curl -sS -H "Authorization: Bearer $MDHUB_TOKEN" "http://127.0.0.1:8080/v1/runs?job=swaps_ratescalcs&limit=3"
   curl -sS -H "Authorization: Bearer $MDHUB_TOKEN" "http://127.0.0.1:8080/v1/runs/health"
   ```
   (as in 0.4; `job=` filters the run log, `limit=` the page size; `/v1/runs/health` is the per-job health).
   Expected: `"status":"succeeded"`, `"error":null`; in `/v1/runs/health` the job `swaps_ratescalcs` has no
   failures in a row.
4. The stored text must show the placeholder, not the address. In an interactive `psql` session (0.4):
   ```sql
   SELECT url_redacted FROM ops.raw_capture WHERE source = 'ratescalcs' ORDER BY fetched_at DESC LIMIT 3;
   ```
   (the three newest captures of the source). Expected: `<MDHUB_RATESCALCS_BASE_URL>/...` and **never the
   address**. That is normal, not a fault. The real text of any error is in the worker log:
   `docker compose logs --tail 200 worker | grep -i ratescalcs` (`grep -i` keeps the lines containing the
   word, ignoring case).
5. Repeat the counts of section 7 now. They must equal the baseline from 3.6; **any increase means the address
   reached the database: report it at once**.

### 5.7 If the source works

Write down: the time of the first `succeeded` run, whether a TLS name was needed (yes/no; the name is not a
secret but leave it out of the report anyway), and whether the proxy was involved. Go on with section 6.

### 5.8 Symptom -> cause -> action

"Where" tells you where the text appears: **worker** = last lines of the worker log when it does not start
(`restarting` in `docker compose ps`), **run** = the error of a job run (Jobs page, `/v1/runs`), **probe** = the
Credentials/Sources page. Texts are shortened. The placeholder `<MDHUB_RATESCALCS_BASE_URL>` stands for the address in
stored texts; the log has the real one.

| where | text | cause | action |
|---|---|---|---|
| worker | `invalid configuration: ValidationError` (nothing else) | some value in `.env` cannot be parsed (the command line prints no detail) | run the check of 5.5: it prints the setting and the reason |
| worker, 5.5 | `... BASE_URL: not a valid URL (the port must be 1-65535 or left out)` | port `0`, or `:` with no number, or another bad host/port | write the port as 1-65535 or leave it out |
| worker, 5.5 | `... BASE_URL: the scheme must be http or https` | the value has no `https://` (a bare `host` or `ftp://`) | add `https://` |
| worker, 5.5 | `invalid configuration: MDHUB_RATESCALCS_BASE_URL is plain http and the Ratescalcs credentials are set: they would be sent unencrypted (use an https base URL, or accept it on a trusted network with MDHUB_RATESCALCS_ALLOW_PLAINTEXT_CREDENTIALS=true)` | the base URL starts with `http://` and credentials are set | use `https://`. **Do not** set the opt-in; it needs a decision of the owner |
| worker | `invalid configuration: MDHUB_RATESCALCS_TLS_SERVER_NAME is set but MDHUB_RATESCALCS_BASE_URL is plain http: no certificate would be checked and no name sent (use an https base URL or unset the server name)` | TLS name with an `http` base URL | `https://`, or remove the name line |
| worker | `invalid configuration: MDHUB_RATESCALCS_TLS_SERVER_NAME is set while MDHUB_HTTP_PROXY is set: the proxy tunnel presents the host of the base URL, not the server name ...` | a TLS name and a proxy in `.env` | remove the proxy line **only if the owner agrees** (it serves every source); else report (5.3) |
| run, probe | `ConfigError: MDHUB_RATESCALCS_TLS_SERVER_NAME is set while MDHUB_HTTP_PROXY is set ...` (the worker did start) | the proxy is set on the console page Credentials (the start check does not see it) | same decision; the owner or an admin removes `MDHUB_HTTP_PROXY` on the Credentials page (Remove) |
| run, probe | `ConfigError: ... plain http and the Ratescalcs credentials are set ...` | credentials were set on the console after an `http` base URL | `https://` |
| run, probe | `TLS certificate check failed for <MDHUB_RATESCALCS_BASE_URL>/...: ... certificate is not valid for ...` (also "IP address mismatch" or "Hostname mismatch") | the certificate does not list the IP, or does not list the TLS name that is set | 5.3: set or correct `MDHUB_RATESCALCS_TLS_SERVER_NAME` (the name the certificate lists) |
| run, probe | `TLS certificate check failed ...: self-signed certificate` / `unable to get local issuer certificate` | issuer not trusted by the hub | **stop, return to the owner** (5.3) |
| run, probe | `TLS certificate check failed ...: certificate has expired` | the source's certificate expired | nothing to change on the hub; tell the owner |
| probe | pill **rejected**, error `HTTP 401` or `HTTP 403`; run: `HttpStatusError: HTTP 401 for <MDHUB_RATESCALCS_BASE_URL>/snapshot` | the new address does not accept the credentials | confirm with the owner that the credentials are the right ones for the new address; replace them on the Credentials page (Set); then Check again |
| run, probe | `HTTP 404 for <MDHUB_RATESCALCS_BASE_URL>/...` | the base URL carries a prefix the new source does not have, or the pages are elsewhere | make sure the base URL has no path; otherwise report |
| run, probe | `TransportFailure: ConnectTimeout for <MDHUB_RATESCALCS_BASE_URL>/snapshot after N attempt(s)` (or `ConnectError`, `ReadTimeout`) | the address cannot be reached from the server (network, firewall, wrong address or port) or the service is down (it answers Mon-Fri 08:50-23:00 MSK only) | retry in service hours; test reachability from the server (`openssl s_client`, 5.3); report if the server cannot connect |
| run | `... refusing cross-host redirect from <MDHUB_RATESCALCS_BASE_URL>/...` | the source answers with a redirect to **another host**, or from https to a non-https URL; the hub never follows these | the target is only in the worker log (`grep -i redirect`); give it to the owner privately; the owner decides the base URL |
| run | `EntitlementError: HTML instead of JSON from <MDHUB_RATESCALCS_BASE_URL>/snapshot (entitlement missing?)` | a page of HTML (login, error) instead of JSON | credentials or permissions at the source: report |
| run | `FormatDriftError: Ratescalcs ...` | the new server's pages differ from what the parser expects | report; no fix on the server |
| run | `swaps_ratescalcs: all pages failed: ...` / status `partial` | every page failed / some pages failed; the text lists each reason as above | use the rows above for the listed reasons |
| run | `skipped`, `source_not_configured: RATESCALC_USERNAME/RATESCALC_PASSWORD not configured` | no credentials | set them on the Credentials page or in `.env` (3.3) |

## 6. Carried curves and the history load

### 6.1 Carried curves

```bash
curl -sS -H "Authorization: Bearer $MDHUB_TOKEN" "http://127.0.0.1:8080/v1/carried-curves?days=30"
```

- As in 0.4. `days=` is how many days back from today the business dates go (0-400, default 30);
  `cut_type=eod_final` (or `eod_preliminary`, `intraday`) narrows it.

The answer has `data` and `meta` (`returned`, `since`). One entry per carried curve of a **current** (not
superseded) market snapshot: `snapshot_id`, `cut_type`, `business_date`, `cut_ts`, `dataset`, `source`,
`source_day` (the day it was taken from), `age_working_days`, `age_limit_exceeded` (true after 5 working days,
`MDHUB_CURVE_CARRY_MAX_AGE_DAYS`), `reason` (`no_quote_set_on_business_date`). An empty `data` means nothing
is carried.

What to expect: while the source was unreachable (and no other source delivered `ruonia_ois` / `key_rate_swap`),
the cuts of those days carry the curves, with growing ages. That is by design and visible, not a fault.
Cuts of days after the move hold the real curves. Cuts of the outage days stay carried unless data of a later day up
to their business date is stored afterwards (then `snapshot_heal` reissues them). The missing days also remain
gaps of the datasets `curves_ruonia_ois` / `curves_key_rate_swap` ([SNAPSHOT_HEAL.md](../docs/SNAPSHOT_HEAL.md),
"Not covered here"). Record the count and the oldest `source_day` in the report.

### 6.2 History load (separate explicit step; the owner has decided that the history is loaded and the cuts reissued once)

Do this only after 5.7 and 4.3. Nothing is loaded until you switch it on.

**Step 1 - look at the preview.** While the load is off, every `snapshot_heal` run (every 30 minutes, after the
worker started on 0.7.0, so within about 30-40 minutes) keeps one `open` gap per new dataset in the dataset
`snapshot_history_load`, with a note that starts `preview, nothing queued` and says the range and the
number of backfill tasks, for example `would load 2010-01-01..2026-10-09 in 12 backfill task(s) (job X, shared by 2
dataset(s), 12 task(s) in all)`. Read it:

```bash
curl -sS -H "Authorization: Bearer $MDHUB_TOKEN" "http://127.0.0.1:8080/v1/gaps?dataset=snapshot_history_load"
```

- As in 0.4; `/v1/gaps` lists open gaps, `dataset=` selects this one. The same rows without a token, in an
  interactive `psql` session (0.4):

```sql
SELECT gap_date, status, note FROM ops.data_gap WHERE dataset = 'snapshot_history_load' ORDER BY gap_date;
```

- It takes the rows of the gap table for that dataset, ordered by date.

The console page **Data**, section "Open and unfillable gaps", shows the same rows, but it lists only the first
300 gaps ordered by status and dataset, so while hundreds of `snapshot_cut_*` gaps are open (the first night) the rows of
`snapshot_history_load` may be cut off: use the API or the SQL then.

Work out the size: **nights = the sum of "task(s) in all" over the *distinct jobs*, divided by 30, rounded up**
(30 = `MDHUB_HISTORY_LOAD_MAX_TASKS_PER_NIGHT`; datasets that share a job share its tasks, so do not add them
twice). The reissue that follows is up to about 250 corrected cuts (every `eod_final` cut of the last 12 months),
published at 10 cuts per run in the night window; consumers download them again. No preview rows: nothing is new
to the hub, there is nothing to load - write that down and skip the rest of 6.2.

**Step 2 - switch it on.** Edit `.env` as in 5.4 and add:

```
MDHUB_SNAPSHOT_HEAL_HISTORY=true
```

and apply it as in 5.5 (`docker compose up -d worker`, then `ps` and the log). There is no console screen for
this setting. It acts only with `MDHUB_SNAPSHOT_HEAL=repair` (the default).

**What then happens.** In the next night window (21:00-06:00 MSK) `snapshot_heal` queues the backfill tasks
(`ops.backfill_task`, source `history_load`) and writes `ops.dataset_history_load` rows. The backfill worker
(every 10 minutes) takes them after the tasks of every other source, **only 00:30-06:00 MSK, at most 30 per
night** (a task whose source has no credentials stays `pending` and is looked at again every night; a failed task is
queued again after 6 hours, 1 day, 3 days and 7 days, five rounds in all). When no dataset is still loading, one
pass reissues the `eod_final` cuts of the last 365 days once (gaps `snapshot_cut_new_series`, 10 per run); then the
datasets become `done` and are never loaded again.

**Step 3 - watch.** In an interactive `psql` session (0.4):

```sql
SELECT dataset, status, load_round, history_from, head_to, first_before, first_after, failed_ranges, waiting_reason, note
FROM ops.dataset_history_load ORDER BY dataset;
SELECT status, count(*) FROM ops.backfill_task WHERE source = 'history_load' GROUP BY status ORDER BY status;
SELECT status, count(*) FROM ops.data_gap WHERE dataset = 'snapshot_cut_new_series' GROUP BY status;
```

- The first lists one row per dataset with its state (`loading`, `reissue`, `done`), the loaded range and the number
  of ranges that could not be loaded; the second counts the load's backfill tasks per status (`pending`,
  `running`, `done`, `failed`); the third counts the cuts of the reissue pass per gap status.

Also: Jobs page, `snapshot_heal` and `backfill_worker` runs; Data page, "Tasks" and the queue; the gap
`snapshot_history_load` of each dataset (`open` while loading, `backfilled` when done, `unfillable` when done
with ranges that could not be loaded - report the note).

**Done** when every row of `ops.dataset_history_load` is `done`, the `snapshot_cut_new_series` gaps are
`backfilled`, and `snapshot_heal` ends `skipped` with `nothing to heal` again. Record the nights it took, the
number of reissued cuts and any `unfillable` note.

**Stop it.** Set `MDHUB_SNAPSHOT_HEAL_HISTORY=false` (or remove the line) and apply it as in 5.5. The backfill worker
then takes no history-load task and the job queues nothing; tasks already queued stay `pending`, the state of the
loads stays as it is, and a later switch-on continues from there. For the first-night repair use
`MDHUB_SNAPSHOT_HEAL=detect` (records gaps only) or `off`.

## 7. Old rows that may hold the address

Applies **only if the new address was used on a version before 0.7.0** (3.6). Since 0.7.0 the hub replaces the
configured address by `<MDHUB_RATESCALCS_BASE_URL>` (and the TLS name by `<server name>`) in everything it
writes to these places; rows written before are not rewritten:

| table.column | what it holds |
|---|---|
| `ops.raw_capture.url_redacted` | URL of a stored page |
| `ops.source_probe.endpoint`, `ops.source_probe.error` | URL and error of a probe |
| `ops.job_run.error` | error of a run |
| `ops.job_request.error` | error of a console request |
| `ops.backfill_task.last_error` | error of a backfill task |
| `ops.data_gap.note` | note of a gap |
| `ops.quality_report.report` | the JSON report of a `quality` run (cast to text below) |

`admin.audit_log` cannot be changed (append-only by design) and is not part of this.

### 7.1 Count the rows (a parameter, so the address is never in the SQL text)

Open an interactive session (0.4). First enter this single psql command, type the address when asked and press
Enter (do not paste it together with the statement: the next line you paste would be taken as the answer):

```
\prompt 'address (host or IP only): ' addr
```

Then paste the statement. It uses the variable as `:'addr'` (psql quotes it as a text literal):

```sql
SELECT 'ops.raw_capture.url_redacted' AS "column", count(*) AS rows_with_address
  FROM ops.raw_capture WHERE strpos(lower(url_redacted), lower(:'addr')) > 0
UNION ALL SELECT 'ops.source_probe.endpoint', count(*)
  FROM ops.source_probe WHERE strpos(lower(endpoint), lower(:'addr')) > 0
UNION ALL SELECT 'ops.source_probe.error', count(*)
  FROM ops.source_probe WHERE strpos(lower(coalesce(error, '')), lower(:'addr')) > 0
UNION ALL SELECT 'ops.job_run.error', count(*)
  FROM ops.job_run WHERE strpos(lower(coalesce(error, '')), lower(:'addr')) > 0
UNION ALL SELECT 'ops.job_request.error', count(*)
  FROM ops.job_request WHERE strpos(lower(coalesce(error, '')), lower(:'addr')) > 0
UNION ALL SELECT 'ops.backfill_task.last_error', count(*)
  FROM ops.backfill_task WHERE strpos(lower(coalesce(last_error, '')), lower(:'addr')) > 0
UNION ALL SELECT 'ops.data_gap.note', count(*)
  FROM ops.data_gap WHERE strpos(lower(coalesce(note, '')), lower(:'addr')) > 0
UNION ALL SELECT 'ops.quality_report.report', count(*)
  FROM ops.quality_report WHERE strpos(lower(report::text), lower(:'addr')) > 0
ORDER BY 1;
```

- `\prompt 'text' name` is a psql command (not SQL): it shows the text and stores what you type in the variable `name`,
  so the address is on no command line. Type the host or IP only, as the owner gave it, without `https://`, port or
  path.
- Each `SELECT` counts the rows of one column that contain the text: `strpos(a, b)` is the position of `b` in `a`
  (0 = not found), so `> 0` means "contains"; `lower(...)` on both sides ignores case; `coalesce(x, '')` turns a
  missing value into an empty text; `report::text` casts the JSON to text. `UNION ALL` glues the rows of the
  statements into one result; `ORDER BY 1` sorts by the first column; `AS` names columns.
- The result has eight rows and **no address**. A count is an upper bound: a short address such as `192.0.2.1` also
  matches `192.0.2.10` in unrelated text. Leave the session with `\q`.

### 7.2 What to do with the numbers

All eight counts 0: nothing to do (and the baseline for 5.6 step 5). Otherwise **do not edit or delete rows**:
there is no ready procedure yet to clean old rows (issue FlyingScotman/market-data-hub#148 asks for one: a console
action or a migration that replaces the address by the placeholder). Report the eight counts to the owner.
Two more facts about the limit of the replacement are in that issue: the compressed page text in
`ops.raw_capture.payload_gz` is not scanned (the pages hold relative links only), and the matching is by substring.

## 8. Rollback

Decide with the owner; do not roll back on your own unless the update did it by itself.

- **During the install** the updater rolls back by itself when the post-install check fails (request state
  `rolled_back`): the services go back to 0.6.2. If the migrations had already run (they run before the check),
  it restores the pre-update dump (the database is dropped and re-created from it); only without migrations it
  just switches the image tag back.
- **Later**, Updates page, "Installed versions": **Roll back to 0.6.2** and confirm. The confirmation page lists the
  migrations that 0.6.2 does not know (0840, 0850, 0860; or says they are not recorded) and warns that the dump
  taken before 0.7.0 was installed is restored: **everything written after that dump is lost** until backfill reloads it, including
  the snapshots and corrections the self-heal published. A consumer that downloaded such snapshots holds
  ids the hub no longer has: tell the owner before you roll back if a night repair has already run.
- The three migrations are additive (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, no row is
  changed), so the old schema is intact; but the updater does not skip the restore when the database
  has migrations the target version does not know ([README_DEPLOY.md](README_DEPLOY.md) §8 says to roll back the code by
  restoring the pre-upgrade dump, §13 describes the updater doing it). A way to run 0.6.2 over the migrated database without a restore is
  not documented; do not improvise one.
- **Settings after the rollback**: 0.6.2 ignores variables it does not know, so the new lines may stay in `.env`;
  remove them if you want a clean file (`MDHUB_RATESCALCS_*`, `MDHUB_SNAPSHOT_HEAL*`, `MDHUB_HISTORY_LOAD_*`,
  `MDHUB_CURVE_CARRY_*`). **0.6.2 requests the old address** (a constant of that code): after a rollback the
  moved source delivers nothing until 0.7.0 is installed again. Tell the owner.
- If the update ends `failed` ("rollback failed"), follow [README_DEPLOY.md](README_DEPLOY.md) §13 "Manual
  recovery" only with the owner's agreement.

## 9. Result to return to the owner

Send this list, filled in. **No secret values, no address, no proxy URL, no token.**

| item | result |
|---|---|
| layout (release layout / plain), compose override (yes/no) | |
| version before / after (`/health/ready`) | 0.6.2 / 0.7.0 |
| migrations after: applied / known / pending / checksum_mismatch | 53 / 53 / 0 / 0 expected |
| backup taken before the install (file name, size) | |
| install request: state, start and end time (UTC) | |
| proxy configured anywhere before the move (yes/no) | |
| address used before 0.7.0 (answer of the owner) | |
| TLS server name needed (yes/no); certificate issuer trusted by the hub (yes/no) | |
| Check on Credentials after the move: pill and HTTP status; time | |
| first `swaps_ratescalcs` run that `succeeded`: run id, time, `rows_inserted` | |
| any error text seen (placeholder form only) and what fixed it | |
| `/v1/carried-curves`: number of entries, oldest `source_day`, any `age_limit_exceeded` | |
| history-load preview: datasets, ranges, "task(s) in all" per job, nights (6.2 step 1) | |
| history load switched on (date) / done (date) / nights it took / `unfillable` notes | |
| first-night repair: missing / reissue counts from the first run; cuts published; `unfillable` | |
| counts of section 7.1 before and after the move (eight numbers each) | |
| API token `upgrade-check` revoked (yes/no) | |
| everything that did not match this document | |

After sending it: console page **Tokens**, **Revoke** for `upgrade-check`.
