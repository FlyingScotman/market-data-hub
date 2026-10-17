# Admin console, observability and self-update: design

Status: proposed, 17 October 2026. Owner decisions are in §0. [DESIGN.md](DESIGN.md) stays authoritative for
the loaders, storage and the read API; this document adds the admin side.

## 0. Owner decisions (2026-10-17)

| Question | Decision |
|---|---|
| Where the console is reachable | Internet over HTTPS on its own host name, behind Caddy, with an IP allow-list |
| Users | Several users with roles: `viewer`, `operator`, `admin` |
| Second factor | Passkeys (WebAuthn) first; TOTP and one-time recovery codes as the fallback |
| Updater targets in v1 | VPS with docker compose. Bare-metal systemd and the Windows inside instance come later behind the same interface |

## 1. Goals and non-goals

The console must answer these questions on one screen, without SSH:

- Is anything broken? Failed or missed jobs, stale datasets, open or unfillable gaps, a scheduler that is down.
- What was loaded, what is loading now, and what is pending? Last runs, running jobs, the backfill queue, the next scheduled fires.
- Can the hub reach every source, and do the source credentials still work?
- Who called the API, from where, with which token, and with what result?
- Which version is running? Install a new one from GitHub or from an uploaded file, and roll back.

Non-goals for v1:

- Alerting (system doc 06 §9). It will read the same tables later. The console only shows state.
- Editing source credentials from the browser. They stay in `.env`, and the console shows only "set" or "not set".
- Running arbitrary SQL or shell commands from the browser.

## 2. Processes and network

```
Internet ──443──> Caddy ──┬── api.<domain>   /health/*, /v1/*  ──> api      127.0.0.1:8080  (existing, bearer tokens)
                          └── admin.<domain> /*  (IP allow-list) ──> admin  127.0.0.1:8081  (new, sessions + MFA)
worker   (mdhub scheduler; existing + source probes + heartbeat)
updater  (new: mdhub-updater; no listening port; the only process with the docker socket)
postgres 127.0.0.1:5432
```

- **The admin console is a separate ASGI app** (`mdhub.admin.app:app`) in its own container. The public read API loads
  none of its code, so a flaw in the console does not widen the API's attack surface, and the reverse also holds.
- **The IP allow-list is enforced twice.** Caddy checks `remote_ip` against `MDHUB_ADMIN_ALLOW_CIDRS`. The app checks
  the client IP again, taking `X-Forwarded-For` only from Caddy's address. An empty list means the console refuses
  every request: it fails closed.
- **The updater has no inbound port.** It polls `ops.update_request` and writes progress back. The console never
  holds the docker socket, root, or the GitHub token.
- Both the console and the updater use the same image and code version as the worker and the API.

## 3. Data model (migrations `0600`–`0699`)

The `0500` range belongs to the snapshot-layer work (session hub-snapshot-layer).

### 3.1 New schema `admin`: secrets and identities

The `admin` schema is **excluded** from the read-only consumer grants. Today
`deploy/sql/grant_readonly.sql` and the default privileges grant `SELECT` on every schema, so both must
skip `admin`. A test asserts that `mdhub_ro` can read nothing in `admin`.

| Table | Content |
|---|---|
| `admin.user` | id, login (citext, unique), display name, role ∈ {viewer, operator, admin}, argon2id password hash, `disabled_at`, `password_changed_at`, created_by, created_at |
| `admin.webauthn_credential` | user, credential id, public key (COSE), sign count, transports, AAGUID, name ("MacBook Touch ID"), created_at, last_used_at |
| `admin.totp_secret` | user, secret (encrypted with `MDHUB_ADMIN_SECRET_KEY`; AES-GCM), confirmed_at, last used step (replay guard) |
| `admin.recovery_code` | user, argon2id hash, used_at (10 codes, each usable once) |
| `admin.enrollment` | one-time invite or reset link: sha256 of the token, user, purpose, expires_at (15 min), used_at |
| `admin.session` | sha256 of the session id, user, created_at, last_seen_at, expires_at, ip, user agent, `mfa_at`, `step_up_at`, revoked_at |
| `admin.login_attempt` | ts, login (as typed), ip, outcome, used by throttling (kept 30 days) |
| `admin.audit_log` | append-only: ts, user, session, ip, action, target, details jsonb, outcome. Holds no secrets. `UPDATE`/`DELETE` are revoked, even for the app role. |
| `admin.api_token` | consumer tokens: id, name ("tdm-backend", "inside-replica"), sha256 of the token, prefix (first 8 characters, for display), scopes, created_by, created_at, expires_at, revoked_at, last_used_at, last_used_ip |

### 3.2 `ops` additions: state shown by the console

| Table / view | Writer | Content |
|---|---|---|
| `ops.api_request` | api | ts, token_id (NULL for an unknown token or unauthenticated), token_name, client_ip, method, path template, query (redacted), status, duration_ms, response bytes, request_id, user agent. The api writes in batches every second or every 200 rows, off the request path. Retention is 90 days in `maintenance`. |
| `ops.worker_heartbeat` | worker, updater | process, host, pid, version, started_at, beat_at (every 30 s), running jobs |
| `ops.source_probe` | worker job `source_probe` (every 5 min) | source, endpoint, ts, ok, http status, latency_ms, auth ∈ {ok, rejected, not_configured, n/a}, TLS certificate expiry, error (sanitized) |
| `ops.v_source_status` | view | last probe per source, plus the last successful load per source from `ops.job_run` |
| `ops.release` | updater | installed versions: version, git commit, source (github, upload), sha256, signature key id, installed_at, installed_by, status ∈ {active, previous, failed, rolled_back} |
| `ops.update_request` | admin (insert), updater (state) | requested version or uploaded file, requested_by, state ∈ {queued, verifying, backing_up, staging, migrating, starting, checking, done, failed, rolling_back, rolled_back}, timestamps, error |
| `ops.update_log` | updater | request, ts, step, line: the progress feed on the Updates page |

### 3.3 Named consumer tokens

- The API checks the presented token against `admin.api_token`: an indexed lookup by sha256, then
  `hmac.compare_digest`. Tokens are cached in memory for 30 s, so revocation takes effect within 30 s.
- `MDHUB_API_TOKENS` keeps working. Its entries appear as `env:1`, `env:2`, …, or as `name=token` when written that way.
  The console offers to migrate them into the database.
- Token format: `mdh_<base32 of 32 random bytes>`. The prefix makes leaked tokens easy to find with secret scanners.
  The full token is shown once, at creation.
- Scopes: `read` (all of `/v1`), `snapshots` (only `/v1/snapshots*`, for the inside replica), `export` (the TDM CSV).

## 4. Console pages

| Page | Shows | Source |
|---|---|---|
| **Overview** | Traffic lights for the scheduler (heartbeat age), database, disk, last backup age, TLS certificate, sources, jobs, data, and the version. Lists the top problems. | heartbeat, `v_job_health`, `v_coverage`, `v_source_status`, `ops.release` |
| **Jobs** | Running now (with elapsed time), failed and partial runs in the last 24 h with errors, missed fires, the next fire of each job, run history with filters | `job_run`, `v_job_health`, `schedule.RULES` |
| **Data** | Per dataset: first and last date, rows, stale flag, open gaps, unfillable gaps. Backfill queue: pending, running, failed. | `v_coverage`, `v_open_gaps`, `backfill_task` |
| **Sources** | Per source: reachable or not, latency, credentials accepted or rejected or not configured, certificate expiry, last successful load | `v_source_status` |
| **API traffic** | Requests per consumer, IP and endpoint. Error rates (401/403/429/5xx). Unknown-token attempts by IP. Slow requests. | `api_request` |
| **Tokens** | Consumer tokens: create, revoke, set expiry. Shows the last use and IP. | `admin.api_token` |
| **Updates** | Running version and the available GitHub releases with their notes. Upload a bundle. Install, and roll back to the previous version. Live progress log. | `ops.release`, `update_request`, `update_log` |
| **Users** and **Audit** | Users, roles, credentials and sessions. The audit trail. | `admin.*` |

Operator actions are limited to: start a job now, retry a failed backfill task, and mark a gap
`not_expected` with a reason. Each one goes through the existing job and queue code and is audited.

Rendering: server-side Jinja2 with htmx for partial refreshes (every 15 s on the Overview). There is no Node
build. Static assets are vendored and served with hashes. The CSP is strict: `default-src 'self'`, no inline scripts,
`frame-ancestors 'none'`.

### Permissions by role

| | viewer | operator | admin |
|---|---|---|---|
| All read pages | ✓ | ✓ | ✓ |
| Run a job, retry a backfill, mark a gap | | ✓ | ✓ |
| Tokens, users, updates, rollback | | | ✓ (with step-up) |

## 5. Authentication and sessions

Baselines: OWASP ASVS 4.0 level 2 (V2 authentication, V3 sessions, V4 access control) and NIST SP 800-63B
(AAL2).

**Bootstrap.** No user and no password exist after installation. On the server, run
`mdhub admin create-user <login> --role admin`. It prints a one-time enrollment link that expires
after 15 minutes. On that link the user sets a password and registers a passkey. Further users are invited from the console with the same
kind of link.

**Login.** The login takes a password plus a passkey, or a passkey on its own when the user turns that on. TOTP or a recovery code is the fallback.

- Passwords are hashed with argon2id (`argon2-cffi`: m=64 MiB, t=3, p=1) and rehashed when the parameters change.
  Passwords must be 12 to 128 characters and are checked against a bundled list of the top 100,000 common passwords.
  There are no composition rules and no forced periodic changes.
- WebAuthn uses `webauthn` (py_webauthn). The RP ID is the admin host name. User verification is required and
  the origin is checked. A decreasing sign counter marks the credential as suspect and is audited.
- TOTP follows RFC 6238: 30 s steps, ±1 step tolerance, replay protection by last used step. The secret is encrypted at rest.
- Every login failure returns the same response and takes the same time, whether the login exists or not.

**Throttling.** Failed logins are counted per login and per IP over a sliding window, with exponential delays. After
10 failures in 15 minutes the account locks for 15 minutes; an admin can unlock it. A 429 response carries
`Retry-After`. Lockouts are audited.

**Sessions.**

- The session id has 256 random bits. The database stores only its sha256. The cookie is `__Host-mdhub_admin` with
  `Secure`, `HttpOnly`, `SameSite=Strict` and `Path=/`.
- The id rotates on login and on privilege changes. Sessions expire after 30 minutes idle and after 12 hours in total.
- Changing a password or removing a credential revokes the user's other sessions. Users can see and revoke their sessions.
- **CSRF protection:** every unsafe method needs a synchronizer token tied to the session, plus an `Origin` or
  `Sec-Fetch-Site` check.
- **Step-up:** installing an update, rolling back, creating or revoking tokens and managing users all need a fresh
  passkey or TOTP check within the last 5 minutes.

**Headers.** `Strict-Transport-Security` (2 years, preload-ready), the CSP above, `X-Content-Type-Options`,
`Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, `Permissions-Policy` (WebAuthn only), and
`Cache-Control: no-store` on every page.

**Secrets.** `MDHUB_ADMIN_SECRET_KEY` (32 random bytes, in `.env`) encrypts the TOTP secrets. Session and CSRF
tokens are random values, not signed blobs. Rotating the key re-encrypts the secrets with `mdhub admin rotate-key`.

## 6. Self-update (docker compose)

### 6.1 Release artifacts

- A tag `vX.Y.Z` triggers a GitHub Actions workflow on the private repository. It runs the tests, `ruff` and `mypy`,
  then builds a **code-only bundle** `market-data-hub-X.Y.Z.tar.gz`. The bundle is `git archive` plus a `MANIFEST.json`
  (version, commit, migrations with checksums, minimum previous version) and `SHA256SUMS`. It contains no seed dump.
  `make_bundle.sh` gains `--code-only`.
- **Signature:** `SHA256SUMS` is signed with an ed25519 key in minisign format, giving `SHA256SUMS.minisig`.
  The private key lives in a protected GitHub environment that needs approval. The public key is pinned on the server
  in `deploy/release-keys/`, and a key list allows rotation. GitHub uploads the archive, `SHA256SUMS` and
  `.minisig` to the Release.
- **Uploaded files** go through exactly the same check. An unsigned archive or a signature from an unknown key is refused.
  The signature is detached, so an upload is the archive plus its `market-data-hub-X.Y.Z.SHA256SUMS.minisig`.
  Implemented in Phase C: `mdhub.release.verify_archive` and `mdhub release verify`, documented in README_DEPLOY §11.
  There is no override switch in the UI; the only override is a CLI flag on the server.
- The version comes from git tags (`hatch-vcs`). It replaces the hard-coded `0.1.0` and shows up in `/health/ready`, the
  console and every heartbeat.

### 6.2 Fetching

- **From GitHub:** the updater lists releases through the API with `MDHUB_UPDATE_GITHUB_TOKEN`, a fine-grained token
  with `contents:read` on this one repository. The token is held only by the updater container and goes through `MDHUB_HTTP_PROXY` if one is set.
- **Upload:** the console streams the file (up to 200 MB) into a staging volume shared with the updater, then queues
  an `update_request` for that file. Caddy's `request_body max_size` applies to that one path only.

### 6.3 The update procedure (updater state machine)

1. **verify.** Check the signature over `SHA256SUMS` against a pinned key, then every file's hash. Refuse a downgrade unless the request is a rollback. Refuse if the
   new manifest's migration checksums differ from already applied versions, the same rule `mdhub migrate` uses.
2. **backup.** Run `mdhub ops backup` with a pre-update label. The backup is kept outside the normal retention of the last 14.
3. **stage.** Unpack into `/opt/mdhub/releases/X.Y.Z` and run `docker compose build` with image tag `mdhub:X.Y.Z`.
   The running services are untouched at this point.
4. **drain.** Tell the worker to finish its running jobs and start no new ones: set the flag `ops.worker_control.drain`.
   Wait at most 10 minutes. After that, stop the worker anyway; the job locks and catch-up recover it.
5. **migrate.** Run `docker compose run migrate` with the new tag.
6. **start.** Switch `MDHUB_IMAGE_TAG` and the `current` symlink, then run `docker compose up -d worker api admin`. The updater
   updates itself last: it starts its successor and exits.
7. **check.** Within 3 minutes: `/health/ready` is 200, the worker heartbeat reports the new version, and a smoke
   request with an internal token succeeds.
8. **done,** or **automatic rollback:**
   - No migration was applied: switch back to the previous tag. This takes seconds.
   - A migration was applied: restore the pre-update dump, then switch back to the previous tag. Data
     loaded since the backup is lost, but backfill restores it. The console says so before the user confirms
     an update that brings new migrations.

Every step writes `ops.update_log` and the audit log. A crash of the updater mid-way resumes from the recorded state when it starts again.
Only one update can run at a time, enforced by an advisory lock.

### 6.4 Threat model

| Threat | Mitigation |
|---|---|
| An attacker with a stolen admin session uploads a malicious bundle | The signature check uses a key the console does not hold. Step-up is required. The upload is audited. |
| The GitHub account or the CI is compromised | The signing key sits in a protected environment with required reviewers. Moving it to an offline key is an option. |
| The console is compromised (RCE) | It has no docker socket, no root and no GitHub token. It can only queue requests the updater verifies. |
| A token leaks from a consumer | Per-consumer tokens can be revoked within 30 s, with last-use IP. Unknown-token attempts are visible by IP. |
| Brute-force or credential stuffing on the login | The IP allow-list, throttling with lockout and mandatory MFA (WebAuthn is phishing-resistant). |
| The read-only database role reads password or session hashes | The `admin` schema is excluded from the RO grants. A test enforces this. |

## 7. Implementation phases

| Phase | Content | Depends on | Done when |
|---|---|---|---|
| **A. Observability backend** | Named tokens (§3.3), `ops.api_request` with batch writer and retention, `ops.worker_heartbeat`, the `source_probe` job and `v_source_status`, version from git tags, `admin` schema excluded from RO grants | none | migrations 06xx, tests (pgserver), mypy strict, `/v1/*` behaviour unchanged for env tokens |
| **B. Admin auth + console** | `mdhub.admin` app, users and roles, passkeys, TOTP and recovery codes, sessions, CSRF, throttling, audit log, all pages in §4, CLI `mdhub admin …`, compose service plus Caddy host with allow-list | A (for data); auth can start in parallel | the ASVS L2 checklist items for V2–V4 have tests; the login flows are tested end to end with a virtual authenticator (Playwright) |
| **C. Release pipeline** | `make_bundle.sh --code-only`, GitHub Actions on tag (tests, bundle, minisign), key management in `deploy/release-keys/`, `mdhub release verify` | none | a test tag produces a release whose bundle verifies on a clean machine |
| **D. Updater** | `mdhub-updater` service, state machine §6.3, worker drain flag, Updates page | B (page), C (artifacts) | update and both rollback paths rehearsed on a throw-away VPS |

A and C can run in parallel. The authentication part of B can also start in parallel with A.

**Phase A status (2026-10-17):** implemented. Migrations `0600_admin_api_token` (schema `admin`,
`admin.api_token`, `admin.restrict_access()`) and `0610_ops_observability` (`ops.api_request`,
`ops.worker_heartbeat`, `ops.source_probe`, `ops.job_source`, `ops.v_source_status`); code in
`mdhub.api.auth`, `mdhub.api.requestlog`, `mdhub.core.heartbeat`, `mdhub.quality.source_probe`,
`mdhub tokens …`; new env `MDHUB_OPS_LOG_RETENTION_DAYS` (and the compose build arg
`MDHUB_VERSION`). Deviations from §3: `ops.job_source` maps loader jobs to sources (the job run
table has no source column); `/health/*` requests are not logged; Cbonds is probed at most hourly
and Ratescalcs only in its service hours.

**Phase B status (2026-10-17):** implemented. Migration `0620_admin_auth` (`admin.user_account`,
`webauthn_credential`, `totp_secret`, `recovery_code`, `enrollment`, `session`, `login_attempt`,
append-only `audit_log`; job trigger `admin`); code in `mdhub.admin` (app, auth, pages, factors,
store, security), `mdhub.job_requests` (worker side of "run now") and `mdhub admin …` (create-user, list, reset, unlock, disable, enable,
rotate-key); compose service `admin` (not published; Caddy reaches it on network `admin_edge`
from a fixed address, the only trusted proxy) and the Caddy site with `remote_ip` allow-list;
new env `MDHUB_ADMIN_DOMAIN`, `MDHUB_ADMIN_ORIGIN`, `MDHUB_ADMIN_ALLOW_CIDRS`,
`MDHUB_ADMIN_TRUSTED_PROXIES`, `MDHUB_ADMIN_SECRET_KEY`. Deviations: the user table is
`admin.user_account` with a lower-case `text` login (no citext); sessions carry a stage
(anonymous login page, password done, enrolling, full) and the pending WebAuthn challenge; the
lockout is derived from `login_attempt` per typed login (an unlock is an `unlocked` row), so it
behaves the same for logins that do not exist; the common-password list holds the 12+ character
entries of the NCSC top-100k list (shorter ones fail the length rule); "run a job now" inserts a
row into `ops.job_request` (migration `0630_ops_job_request`, one open request per job) that the
worker (`mdhub.job_requests`, polled every 5 s, `FOR UPDATE SKIP LOCKED`) runs through
`run_job` with trigger `admin` and default parameters; running requests of a worker whose
heartbeat stopped or went stale are requeued (2 attempts at most). The `admin` compose service
therefore gets no `env_file`, only `MDHUB_DATABASE_URL`, `MDHUB_ADMIN_*`, `MDHUB_LOG_LEVEL` and
`MDHUB_TZ` (no source credentials, no `MDHUB_API_TOKENS`; the Tokens page lists only named
tokens);
`Origin: null` (sent by browsers on form posts under `Referrer-Policy: no-referrer`) counts as
absent, so `Sec-Fetch-Site: same-origin` must then be present; the overview's TLS light shows
the sources' certificates (the console's own certificate is Caddy's). Login flows are tested
end to end with a software authenticator (`tests/admin_helpers.py`), not Playwright.

**Phase D status (2026-10-17):** implemented, not yet rehearsed on a VPS (README_DEPLOY §13 has
the procedure). Migration `0640_ops_update` (`ops.release`, `ops.release_available`,
`ops.update_request`, `ops.update_log`, `ops.worker_control`); code in `mdhub.updater`
(`mdhub updater`), the worker side of the drain flag in `mdhub.schedule` / `mdhub.job_requests`
(`mdhub.jobs.worker_draining`), the Updates page in `mdhub.admin.pages`; compose service
`updater`, volume `uploads`, Caddy body limit 201 MB on `/updates/upload` only; new env
`MDHUB_UPDATE_GITHUB_TOKEN` (in `updater.env`), `MDHUB_UPDATE_GITHUB_REPO`, `MDHUB_UPDATE_ROOT`,
`MDHUB_UPDATE_UPLOAD_DIR`, `MDHUB_UPDATE_API_URL`. Deviations: states `draining` and `cancelled`
(queued requests can be withdrawn); release statuses add `superseded`, and `ops.release` keeps
the release's migrations, the version it replaced and its pre-update dump; GitHub releases are
listed every 10 minutes into `ops.release_available` and the newest three above the running
version are downloaded and verified ahead of time, so the console can show their migrations
without the token (no "check now" button); uploads are verified in the console too (same
`verify_archive`, same pinned keys) before the confirmation page, and again by the updater from
its own copy; a rollback to the previous version does not re-verify a signature (its release
directory and image were verified when it was installed) and, across migrations, restores the
dump taken before the current version was installed; a restore drops and re-creates the
database (`pg_restore --create`) and puts back `ops.update_request`, `ops.update_log`,
`ops.release` and the missing `admin.audit_log` rows from a journal file, so the update's trail
survives and an interrupted restore is redone; the GitHub token lives in `updater.env` because
worker, api and migrate read all of `.env`; the updater runs as root (the docker socket is root
anyway); it replaces itself through a one-off container of the new image; the Updates page is
admin-only (it was a viewer page in phase B); the host layout is `releases/`, `current`,
`archives/`, `shared/` under `MDHUB_UPDATE_ROOT`, mounted at the same path in the updater.

## 8. Open points

- The admin host name and the initial allow-list: the owner's office or home IPs. A dynamic home IP needs either a wider
  CIDR or a WireGuard peer.
- The signing key policy: the protected GitHub environment (proposed) or an offline key on the owner's machine.
- The inside Windows instance will need a console too: the same app behind IIS, or Windows Integrated Authentication
  instead of passwords (system doc 05 §6). This is decided when the inside instance is built.
