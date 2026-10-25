# Production upgrade 0.1.0 → 0.1.3

Completed 19 October 2026, approximately 11:26 UTC / 14:26 MSK.

## Installed artifact

- [GitHub Release v0.1.3](https://github.com/FlyingScotman/market-data-hub/releases/tag/v0.1.3),
  commit `911d1feb1c394779ec99cfae114a2c307dc6b51e`.
- Archive SHA-256: `542307f6ec9c83e55861de29e8608a5699e688b9a59e843b8d6eee64746f5c0b`.
- Verified on Mac and VPS with the previously pinned public key `3C9E6DACD133A1D8`;
  368 files, 26 migrations, minimum previous version 0.1.0.
- No working DB, dump or environment/secret files in the code-only archive.

## Changes and checks

The image was built from the verified archive, using the persistent host-nginx Compose override.
Worker drain was enabled, active jobs finished and worker/updater stopped before the backup.
Pre-update dump: `/opt/mdhub/shared/backups/pre-update-0.1.3/mdhub-mdhub-20261019T112340Z.dump`,
771,450,395 bytes; pg_restore read its catalogue successfully. This directory is separate
from the normal nightly backup rotation. The updater sees the same dump at
`/var/backups/mdhub/pre-update-0.1.3/mdhub-mdhub-20261019T112340Z.dump`.

The existing 23 SQL files were byte-identical. The normal migration runner applied:
0500_known_at_readers, 0510_published_snapshot, 0810_snapshot_service_roles.
API/admin/hostmon were stopped during migration; the PostgreSQL volume was retained.
The protected image tag and current symlink were switched to 0.1.3, and all five app services
were recreated, including hostmon. Worker drain was then cleared. ops.release records 0.1.3
active, 0.1.0 previous and the pre-update backup path.

Verified after startup:

- API/admin/PostgreSQL healthy; worker/updater/hostmon heartbeats report 0.1.3.
- External trusted HTTPS readiness: version 0.1.3, 26 applied/26 known migrations,
  zero pending and zero checksum mismatches.
- Login 200 with Secure cookie; all three login assets return 200.
- Coverage 401 without a token and 200 with the existing token; `/v1/snapshots` returns
  200 with that token. This confirms the new API and role grant, not publication of a fresh snapshot.
- 16,063 bonds, one account, one passkey and eight saved source credentials retained.
- API can read published snapshots and still cannot read admin.user_account.
- New updater discovers v0.1.3 from FlyingScotman/market-data-hub. Its pinned key, repository,
  persistent override and external protected configuration read/write access were checked.
- DB/API/admin ports remain on loopback. The host-nginx HTTPS setup is retained.

## Subsequent updates

An admin uses Updates in the console to choose a published signed GitHub release and confirms
with a fresh second-factor step-up. Main pushes alone do not update production.
The next actual UI install/rollback has not yet been rehearsed end to end; track that in WEB-10
in [ROADMAP.md](../ROADMAP.md).

Important 0.1.3 limitation: its updater restarts worker/API/admin and itself, but omits hostmon.
Main already includes the fix after this tag. On the first subsequent upgrade, explicitly
recreate hostmon using the current Compose files and persistent override after the update,
then check its version. Later transitions using the corrected updater include it normally.
No unsigned patch was applied to the installed 0.1.3 image.
