# Pinned release keys

Every `*.pub` file here is a minisign ed25519 public key that `mdhub release verify` (and the
updater) accepts for `SHA256SUMS.minisig` of a release. The list is read from the **installed**
version, never from the archive being checked. A new key reaches servers only inside a release
signed by a key they already trust.

- File name: `<KEYID>.pub`, the key id that `minisign` prints (`mdhub release keys` lists them).
- Content: the `minisign.pub` file exactly as `minisign -G` wrote it (two lines).
- No secret key ever goes into this repository.

Key generation, the protected `release` environment and rotation: `deploy/README_DEPLOY.md` §11.
