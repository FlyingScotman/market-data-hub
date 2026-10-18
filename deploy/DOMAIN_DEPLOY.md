# Production: marketdatahub.cc

The release repository is `FlyingScotman/market-data-hub`. GitHub Releases contain **code only**:
the archive, its checksum list and its minisign signature. The seed database is transferred privately
for the first installation. An update preserves the existing PostgreSQL volume and never imports a seed.

On this VPS, host nginx serves HTTPS for `marketdatahub.cc`. `/v1/` and `/health/` go to the API on
`127.0.0.1:8080`; the other paths go to the console on `127.0.0.1:8081`. PostgreSQL listens only on
loopback. Only SSH22, HTTP80 (ACME and HTTPS redirect), and HTTPS443 are public. Let's Encrypt renewal
reloads nginx automatically. Request URLs, cookies and authorization must not be written to access logs.

The site-specific Compose configuration lives outside releases at
`/opt/mdhub/shared/compose.local.yaml`. Set `MDHUB_UPDATE_COMPOSE_OVERRIDE` to that absolute path in the
shared deployment environment. The updater applies it when building, migrating, starting, rolling back
and replacing itself. If an explicitly configured override is missing, the update fails rather than
starting with different network settings.

The shared `.env` symlink resolves into `/root/.config/lima/secrets/market-data-hub/mdhub.env`.
Mount that protected directory at the same path in the updater only: it needs to read deployment values
and atomically change the image tag. A symlink into an unmounted secret directory cannot be used from
the updater container. The directory is 0700 and its configuration file is 0600.

Set the console domain to `marketdatahub.cc`, origin to `https://marketdatahub.cc`, and trust only the
Docker host gateway as the nginx proxy. nginx replaces forwarded client-IP headers. The owner chose
access from any network, protected by password plus passkey; the app's CIDR list therefore includes
all IPv4 and IPv6 clients. The application retains its CSRF checks, throttling, roles and second factor.
Passkeys registered for `localhost` do not work on this domain and must be enrolled again.

## Publishing and installing updates

A release tag matching the package version starts the existing GitHub release workflow: tests, code-only
bundle, signature in the protected `release` environment, independent signature verification, publication.
Only the signing job can access the signing secrets; only the publish job has write access to releases.
The public verification key is committed under `deploy/release-keys/`; private signing material never
belongs in Git, a bundle, or on the VPS. Approve signing in GitHub when the protected environment asks.

The public repository does not require a GitHub token on the VPS. A private repository instead requires
a token limited to Contents:read in the updater-only environment, never in the shared application env.

A push to the main branch alone does not deploy. The console lists verified releases in Updates.
An admin reviews a version and confirms installation with a fresh second-factor check. The updater
verifies the signature and migration checksums, backs up the database, builds the new Docker image,
waits for active jobs, applies migrations, restarts services and checks API readiness and worker heartbeat.
An error after services stop triggers the existing rollback procedure. Updates do not renew passkeys or
reset users; the one-time account setup is needed only for initial setup or a domain change.
