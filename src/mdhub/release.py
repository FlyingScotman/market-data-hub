"""Release bundles: manifest, minisign signatures and verification (ADMIN_DESIGN §6.1, §6.4).

A code-only release (``deploy/make_bundle.sh --code-only``) is published as three files:

* ``market-data-hub-X.Y.Z.tar.gz`` - one top-level directory ``mdhub/``: ``git archive`` of the
  tagged commit, ``MANIFEST.json`` (:func:`build_manifest`) and ``SHA256SUMS`` (SHA-256 of every
  other file in ``mdhub/``, ``sha256sum -c`` format);
* ``market-data-hub-X.Y.Z.SHA256SUMS`` - a byte-identical copy of ``mdhub/SHA256SUMS``;
* ``market-data-hub-X.Y.Z.SHA256SUMS.minisig`` - an ed25519 signature over it in minisign format.

:func:`verify_archive` fails closed and checks, in this order:

1. the archive layout: regular files and directories under ``mdhub/`` only (no links, devices,
   absolute paths, ``..``, duplicates or setuid bits), within :data:`MAX_TOTAL_BYTES`;
2. the signature over the archive's own ``mdhub/SHA256SUMS`` against the pinned public keys
   (``deploy/release-keys/*.pub`` of the *installed* version, :func:`load_pinned_keys`). A missing
   signature or an unknown key is refused unless ``allow_unsigned`` (the CLI flag
   ``--allow-unsigned``); a pinned key whose signature does not verify is always refused;
3. every file's hash: none missing, none unlisted, none different;
4. the manifest: format, version, commit, ``min_previous_version`` and the migrations with
   checksums, which must match ``src/mdhub/db/migrations/*.sql`` in the archive exactly.

Signing is done by the ``minisign`` tool (``minisign -S``), never by this module, so the secret
key never meets the Python environment (README_DEPLOY §11). Both minisign signature algorithms
verify: ``ED`` (BLAKE2b-512 prehashed, the default since minisign 0.10) and legacy ``Ed``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tarfile
import tomllib
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mdhub.db.migrate import MigrationError, discover_migrations

PROJECT_NAME: Final = "market-data-hub"
TOP: Final = "mdhub"
"""The archive's single top-level directory (same as the full seed bundle)."""
SUMS_NAME: Final = "SHA256SUMS"
MANIFEST_NAME: Final = "MANIFEST.json"
MANIFEST_FORMAT: Final = 1
MIGRATIONS_PREFIX: Final = "src/mdhub/db/migrations/"
SIGNATURE_SUFFIX: Final = ".SHA256SUMS.minisig"
KEYS_DIR_ENV: Final = "MDHUB_RELEASE_KEYS_DIR"

MAX_MEMBERS: Final = 50_000
MAX_TOTAL_BYTES: Final = 1 << 30
"""Uncompressed size limit of an archive (the upload limit is 200 MB compressed)."""
MAX_UPLOAD_BYTES: Final = 200 << 20
"""Size limit of an archive uploaded in the console or downloaded from GitHub (200 MiB)."""
MAX_SMALL_FILE: Final = 4 << 20
"""Limit for files read into memory: SHA256SUMS, MANIFEST.json, signatures, keys."""

_PUBKEY_ALG: Final = b"Ed"
_SIG_ALG_PREHASHED: Final = b"ED"
_SIG_ALG_LEGACY: Final = b"Ed"
_TRUSTED_PREFIX: Final = b"trusted comment: "
_UNTRUSTED_PREFIX: Final = b"untrusted comment:"

_VERSION_RE: Final = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?$"
)
_PRE_RANK: Final = {"a": 0, "b": 1, "rc": 2}
_COMMIT_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SUM_LINE_RE: Final = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
_CREATED_AT_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class ReleaseError(Exception):
    """A release archive, signature, key or manifest is invalid (always refuse the archive)."""


# ----------------------------------------------------------------------------------- versions


def version_key(version: str) -> tuple[int, int, int, int, int]:
    """Sort key of a release version ``X.Y.Z`` or ``X.Y.Z{a|b|rc}N`` (PEP 440 subset).

    :raises ReleaseError: on any other form (dev and local versions are never released).
    """
    m = _VERSION_RE.match(version)
    if m is None:
        raise ReleaseError(f"bad release version {version!r} (want X.Y.Z or X.Y.Z{{a|b|rc}}N)")
    major, minor, patch, pre, num = m.groups()
    if pre is None:
        return int(major), int(minor), int(patch), 3, 0
    return int(major), int(minor), int(patch), _PRE_RANK[pre], int(num)


# ------------------------------------------------------------------------------------ minisign


@dataclass(frozen=True, slots=True)
class PublicKey:
    """A minisign ed25519 public key."""

    key_id: bytes
    """The 8-byte key number as stored in key and signature files."""
    key: Ed25519PublicKey
    source: str

    @property
    def key_id_hex(self) -> str:
        """The key id as minisign prints it (little-endian, upper-case hex)."""
        return self.key_id[::-1].hex().upper()


@dataclass(frozen=True, slots=True)
class Signature:
    """A parsed ``.minisig`` file."""

    algorithm: bytes
    key_id: bytes
    signature: bytes
    trusted_comment: bytes
    global_signature: bytes

    @property
    def key_id_hex(self) -> str:
        return self.key_id[::-1].hex().upper()


def _b64(value: bytes, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ReleaseError(f"{what}: not valid base64") from None


def _lines(data: bytes) -> list[bytes]:
    return [line.rstrip(b"\r") for line in data.split(b"\n")]


def parse_public_key(data: bytes, source: str = "<key>") -> PublicKey:
    """Parse a minisign public key file (comment line + base64 line) or a bare base64 key."""
    lines = [line.strip() for line in _lines(data) if line.strip()]
    if lines and lines[0].startswith(_UNTRUSTED_PREFIX):
        lines = lines[1:]
    if len(lines) != 1:
        raise ReleaseError(f"{source}: expected one base64 key line")
    raw = _b64(lines[0], source)
    if len(raw) != 42 or raw[:2] != _PUBKEY_ALG:
        raise ReleaseError(f"{source}: not a minisign ed25519 public key")
    return PublicKey(
        key_id=raw[2:10], key=Ed25519PublicKey.from_public_bytes(raw[10:]), source=source
    )


def parse_signature(data: bytes, source: str = "<signature>") -> Signature:
    """Parse a minisign signature file (4 lines: comment, signature, trusted comment, global)."""
    lines = _lines(data)
    while lines and lines[-1] == b"":
        lines.pop()
    if (
        len(lines) != 4
        or not lines[0].startswith(_UNTRUSTED_PREFIX)
        or not lines[2].startswith(_TRUSTED_PREFIX)
    ):
        raise ReleaseError(f"{source}: not a minisign signature file")
    raw = _b64(lines[1].strip(), source)
    if len(raw) != 74 or raw[:2] not in (_SIG_ALG_PREHASHED, _SIG_ALG_LEGACY):
        raise ReleaseError(f"{source}: unsupported signature algorithm or length")
    global_sig = _b64(lines[3].strip(), source)
    if len(global_sig) != 64:
        raise ReleaseError(f"{source}: bad trusted comment signature length")
    return Signature(
        algorithm=raw[:2],
        key_id=raw[2:10],
        signature=raw[10:],
        trusted_comment=lines[2][len(_TRUSTED_PREFIX) :],
        global_signature=global_sig,
    )


def verify_signature(data: bytes, sig: Signature, key: PublicKey) -> None:
    """Check ``sig`` over ``data`` and over its trusted comment, as ``minisign -V`` does.

    :raises ReleaseError: on a key id mismatch or a signature that does not verify.
    """
    if sig.key_id != key.key_id:
        raise ReleaseError(f"signature key {sig.key_id_hex} is not key {key.key_id_hex}")
    message = (
        hashlib.blake2b(data, digest_size=64).digest()
        if sig.algorithm == _SIG_ALG_PREHASHED
        else data
    )
    try:
        key.key.verify(sig.signature, message)
    except InvalidSignature:
        raise ReleaseError(
            f"signature by pinned key {key.key_id_hex} does not verify: "
            f"{SUMS_NAME} or the signature was modified"
        ) from None
    try:
        key.key.verify(sig.global_signature, sig.signature + sig.trusted_comment)
    except InvalidSignature:
        raise ReleaseError(
            f"trusted comment signature by key {key.key_id_hex} does not verify"
        ) from None


def default_keys_dir() -> Path:
    """``$MDHUB_RELEASE_KEYS_DIR``, else ``deploy/release-keys`` of this source checkout."""
    env = os.environ.get(KEYS_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "deploy" / "release-keys"


def load_pinned_keys(directory: Path) -> dict[bytes, PublicKey]:
    """Load every ``*.pub`` in ``directory``, keyed by key id (may be empty).

    :raises ReleaseError: if the directory is missing, a key file is malformed or two files
        hold the same key id (fail closed: a broken key list is a configuration error).
    """
    if not directory.is_dir():
        raise ReleaseError(f"release keys directory {directory} not found (set {KEYS_DIR_ENV})")
    keys: dict[bytes, PublicKey] = {}
    for path in sorted(directory.glob("*.pub")):
        key = parse_public_key(_read_small(path), source=path.name)
        if key.key_id in keys:
            raise ReleaseError(
                f"{path.name}: key {key.key_id_hex} is also in {keys[key.key_id].source}"
            )
        keys[key.key_id] = key
    return keys


def _read_small(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_SMALL_FILE:
            raise ReleaseError(f"{path}: {size} bytes, too large")
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"{path}: {exc.strerror or exc}") from None


# ------------------------------------------------------------------------------------ manifest


@dataclass(frozen=True, slots=True)
class Manifest:
    """``mdhub/MANIFEST.json`` of a code-only release."""

    version: str
    commit: str
    created_at: str
    min_previous_version: str
    """The oldest installed version this release can be installed over directly."""
    migrations: tuple[tuple[str, str], ...]
    """``(version, sha256)`` per migration file, sorted; checksums as ``mdhub migrate`` uses."""

    def to_json(self) -> dict[str, Any]:
        return {
            "format": MANIFEST_FORMAT,
            "name": PROJECT_NAME,
            "version": self.version,
            "commit": self.commit,
            "created_at": self.created_at,
            "min_previous_version": self.min_previous_version,
            "migrations": [{"version": v, "sha256": s} for v, s in self.migrations],
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2) + "\n"


def _check_manifest(m: Manifest) -> Manifest:
    if version_key(m.min_previous_version) > version_key(m.version):
        raise ReleaseError(
            f"min_previous_version {m.min_previous_version} is newer than version {m.version}"
        )
    if not _COMMIT_RE.match(m.commit):
        raise ReleaseError(f"bad commit {m.commit!r} (want 40 hex digits)")
    if not _CREATED_AT_RE.match(m.created_at):
        raise ReleaseError(f"bad created_at {m.created_at!r} (want YYYY-MM-DDTHH:MM:SSZ)")
    versions = [v for v, _ in m.migrations]
    if versions != sorted(set(versions)):
        raise ReleaseError("manifest migrations are not sorted and unique")
    for v, s in m.migrations:
        if not v or "/" in v or not _SHA256_RE.match(s):
            raise ReleaseError(f"bad manifest migration entry {v!r}")
    return m


def build_manifest(
    root: Path, *, version: str, commit: str, created_at: datetime | None = None
) -> Manifest:
    """Manifest of the release tree ``root`` (the unpacked ``git archive``).

    ``min_previous_version`` comes from ``[tool.mdhub.release]`` in ``root/pyproject.toml``;
    migrations and checksums from :func:`mdhub.db.migrate.discover_migrations`.
    """
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"{root / 'pyproject.toml'}: {exc}") from None
    min_previous = (
        pyproject.get("tool", {}).get("mdhub", {}).get("release", {}).get("min_previous_version")
    )
    if not isinstance(min_previous, str):
        raise ReleaseError("pyproject.toml: [tool.mdhub.release] min_previous_version not set")
    try:
        migrations = discover_migrations(root / MIGRATIONS_PREFIX.rstrip("/"))
    except MigrationError as exc:
        raise ReleaseError(str(exc)) from None
    stamp = (created_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _check_manifest(
        Manifest(
            version=version,
            commit=commit,
            created_at=stamp,
            min_previous_version=min_previous,
            migrations=tuple((m.version, m.checksum) for m in migrations),
        )
    )


def parse_manifest(data: bytes) -> Manifest:
    """Parse and sanity-check ``MANIFEST.json``.

    :raises ReleaseError: on malformed JSON, unknown format or project, bad fields.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"{MANIFEST_NAME}: not valid JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise ReleaseError(f"{MANIFEST_NAME}: not a JSON object")
    if raw.get("format") != MANIFEST_FORMAT:
        raise ReleaseError(f"{MANIFEST_NAME}: unsupported format {raw.get('format')!r}")
    if raw.get("name") != PROJECT_NAME:
        raise ReleaseError(f"{MANIFEST_NAME}: not a {PROJECT_NAME} release ({raw.get('name')!r})")
    fields = ("version", "commit", "created_at", "min_previous_version")
    values = {f: raw.get(f) for f in fields}
    for f, v in values.items():
        if not isinstance(v, str):
            raise ReleaseError(f"{MANIFEST_NAME}: {f} missing or not a string")
    entries = raw.get("migrations")
    if not isinstance(entries, list):
        raise ReleaseError(f"{MANIFEST_NAME}: migrations missing or not a list")
    migrations: list[tuple[str, str]] = []
    for entry in entries:
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("version"), str)
            and isinstance(entry.get("sha256"), str)
        ):
            raise ReleaseError(f"{MANIFEST_NAME}: bad migration entry {entry!r}")
        migrations.append((entry["version"], entry["sha256"]))
    try:
        return _check_manifest(
            Manifest(
                version=str(values["version"]),
                commit=str(values["commit"]),
                created_at=str(values["created_at"]),
                min_previous_version=str(values["min_previous_version"]),
                migrations=tuple(migrations),
            )
        )
    except ReleaseError as exc:
        raise ReleaseError(f"{MANIFEST_NAME}: {exc}") from None


# ------------------------------------------------------------------------------------- archive


def parse_sha256sums(data: bytes) -> dict[str, str]:
    """``{relative path: sha256}`` from a ``sha256sum`` listing (strict; no escaped names)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseError(f"{SUMS_NAME}: not UTF-8") from None
    sums: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _SUM_LINE_RE.match(line)
        if m is None:
            raise ReleaseError(f"{SUMS_NAME}:{lineno}: malformed line")
        path = _relative(m.group(2), f"{SUMS_NAME}:{lineno}")
        if path in sums:
            raise ReleaseError(f"{SUMS_NAME}:{lineno}: duplicate entry {path}")
        sums[path] = m.group(1)
    if not sums:
        raise ReleaseError(f"{SUMS_NAME}: empty")
    return sums


def _relative(path: str, where: str) -> str:
    parts = path.split("/")
    if "\\" in path or "\x00" in path or any(p in ("", ".", "..") for p in parts):
        raise ReleaseError(f"{where}: unsafe path {path!r}")
    return path


@dataclass(slots=True)
class _Scan:
    hashes: dict[str, str]
    small: dict[str, bytes]
    total_bytes: int


def _scan_archive(archive: Path) -> _Scan:
    """Hash every regular file of a ``.tar.gz`` in one streaming pass; enforce the layout."""
    hashes: dict[str, str] = {}
    small: dict[str, bytes] = {}
    total = 0
    members = 0
    keep = {SUMS_NAME, MANIFEST_NAME}
    try:
        with tarfile.open(archive, mode="r|gz") as tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise ReleaseError(f"more than {MAX_MEMBERS} archive members")
                name = member.name
                if member.isdir():
                    stripped = name.rstrip("/")
                    if stripped != TOP:
                        _member_path(stripped)
                    continue
                if not member.isreg():
                    raise ReleaseError(f"{name}: not a regular file or directory")
                if member.mode & 0o7000:
                    raise ReleaseError(f"{name}: setuid, setgid or sticky bit")
                rel = _member_path(name)
                if rel in hashes:
                    raise ReleaseError(f"{name}: duplicate archive member")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ReleaseError(f"archive expands to more than {MAX_TOTAL_BYTES} bytes")
                if rel in keep and member.size > MAX_SMALL_FILE:
                    raise ReleaseError(f"{name}: {member.size} bytes, too large")
                fileobj = tar.extractfile(member)
                if fileobj is None:  # pragma: no cover - isreg() members always have data
                    raise ReleaseError(f"{name}: unreadable")
                digest = hashlib.sha256()
                buf = bytearray()
                read = 0
                while chunk := fileobj.read(1 << 20):
                    read += len(chunk)
                    digest.update(chunk)
                    if rel in keep:
                        buf += chunk
                if read != member.size:
                    raise ReleaseError(f"{name}: truncated")
                hashes[rel] = digest.hexdigest()
                if rel in keep:
                    small[rel] = bytes(buf)
    except ReleaseError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise ReleaseError(f"{archive.name}: not a readable .tar.gz ({exc})") from None
    return _Scan(hashes=hashes, small=small, total_bytes=total)


def _member_path(name: str) -> str:
    prefix = f"{TOP}/"
    if not name.startswith(prefix):
        raise ReleaseError(f"{name}: outside the top-level directory {TOP}/")
    return _relative(name[len(prefix) :], "archive")


def default_signature_path(archive: Path) -> Path:
    """``market-data-hub-X.Y.Z.tar.gz`` -> ``market-data-hub-X.Y.Z.SHA256SUMS.minisig``."""
    name = archive.name
    stem = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else name
    return archive.with_name(stem + SIGNATURE_SUFFIX)


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    """Outcome of a successful :func:`verify_archive`."""

    manifest: Manifest
    files: int
    total_bytes: int
    key: PublicKey | None
    """The pinned key that signed it; ``None`` only when accepted with ``allow_unsigned``."""
    trusted_comment: str | None
    warnings: tuple[str, ...]


def _check_signature(
    sums: bytes,
    sig_path: Path,
    keys: Mapping[bytes, PublicKey],
    *,
    explicit: bool,
    allow_unsigned: bool,
    warnings: list[str],
) -> tuple[PublicKey | None, str | None]:
    if not sig_path.is_file():
        if explicit:
            raise ReleaseError(f"signature file {sig_path} not found")
        if not allow_unsigned:
            raise ReleaseError(
                f"unsigned: no {sig_path.name} next to the archive "
                "(refused; only the CLI flag --allow-unsigned overrides)"
            )
        warnings.append("UNSIGNED: no signature, accepted because of --allow-unsigned")
        return None, None
    sig = parse_signature(_read_small(sig_path), source=sig_path.name)
    comment = sig.trusted_comment.decode("utf-8", "replace")
    key = keys.get(sig.key_id)
    if key is None:
        pinned = ", ".join(k.key_id_hex for k in keys.values()) or "none"
        message = f"signed by unknown key {sig.key_id_hex} (pinned: {pinned})"
        if not allow_unsigned:
            raise ReleaseError(f"{message}; refused, only --allow-unsigned overrides")
        warnings.append(f"UNTRUSTED: {message}, accepted because of --allow-unsigned")
        return None, comment
    verify_signature(sums, sig, key)
    return key, comment


def verify_archive(
    archive: Path,
    keys: Mapping[bytes, PublicKey],
    *,
    signature: Path | None = None,
    allow_unsigned: bool = False,
) -> VerifiedRelease:
    """Verify a code-only release archive (see the module docstring for the checks).

    ``signature`` defaults to :func:`default_signature_path`. ``allow_unsigned`` accepts a
    missing signature or one by an unknown key (with a warning), never a pinned key's signature
    that fails, and never skips the hash and manifest checks.

    :raises ReleaseError: with the first reason to refuse the archive.
    """
    scan = _scan_archive(archive)
    sums = scan.small.get(SUMS_NAME)
    if sums is None:
        raise ReleaseError(f"no {TOP}/{SUMS_NAME} in the archive")
    warnings: list[str] = []
    key, comment = _check_signature(
        sums,
        signature if signature is not None else default_signature_path(archive),
        keys,
        explicit=signature is not None,
        allow_unsigned=allow_unsigned,
        warnings=warnings,
    )

    listed = parse_sha256sums(sums)
    actual = {p: h for p, h in scan.hashes.items() if p != SUMS_NAME}
    mismatched = sorted(p for p in listed.keys() & actual.keys() if listed[p] != actual[p])
    problems = []
    for label, paths in (
        ("missing from the archive", sorted(listed.keys() - actual.keys())),
        ("not in SHA256SUMS", sorted(actual.keys() - listed.keys())),
        ("hash mismatch", mismatched),
    ):
        if paths:
            more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
            problems.append(f"{label}: {', '.join(paths[:5])}{more}")
    if problems:
        raise ReleaseError("; ".join(problems))

    data = scan.small.get(MANIFEST_NAME)
    if data is None:
        raise ReleaseError(f"no {TOP}/{MANIFEST_NAME} in the archive (not a code-only release)")
    manifest = parse_manifest(data)
    in_archive = tuple(
        sorted(
            (p[len(MIGRATIONS_PREFIX) : -len(".sql")], h)
            for p, h in actual.items()
            if p.startswith(MIGRATIONS_PREFIX)
            and p.endswith(".sql")
            and "/" not in p[len(MIGRATIONS_PREFIX) :]
        )
    )
    if in_archive != manifest.migrations:
        listed_versions = {v for v, _ in manifest.migrations}
        found_versions = {v for v, _ in in_archive}
        differ = sorted(listed_versions ^ found_versions) or sorted(
            v for (v, s) in manifest.migrations if (v, s) not in in_archive
        )
        raise ReleaseError(
            f"{MANIFEST_NAME} migrations differ from {MIGRATIONS_PREFIX}*.sql: {', '.join(differ)}"
        )
    return VerifiedRelease(
        manifest=manifest,
        files=len(scan.hashes),
        total_bytes=scan.total_bytes,
        key=key,
        trusted_comment=comment,
        warnings=tuple(warnings),
    )


__all__ = [
    "KEYS_DIR_ENV",
    "MAX_UPLOAD_BYTES",
    "MIGRATIONS_PREFIX",
    "Manifest",
    "PublicKey",
    "ReleaseError",
    "Signature",
    "VerifiedRelease",
    "build_manifest",
    "default_keys_dir",
    "default_signature_path",
    "load_pinned_keys",
    "parse_manifest",
    "parse_public_key",
    "parse_sha256sums",
    "parse_signature",
    "verify_archive",
    "verify_signature",
    "version_key",
]
