"""Release bundles: minisign verification, archive checks, manifest, CLI, make_bundle --code-only.

Keys are generated per test (never committed). The interop tests with the ``minisign`` binary
run when it is installed (CI installs it).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from typer.testing import CliRunner

from mdhub.cli import app
from mdhub.db.migrate import discover_migrations
from mdhub.release import (
    KEYS_DIR_ENV,
    ReleaseError,
    allowed_next_versions,
    build_manifest,
    check_next_version,
    default_keys_dir,
    default_signature_path,
    load_pinned_keys,
    parse_manifest,
    parse_public_key,
    parse_signature,
    release_tags,
    verify_archive,
    verify_signature,
    version_key,
)

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
WORKFLOWS = REPO / ".github" / "workflows"
MINISIGN = shutil.which("minisign")
COMMIT = "0123456789abcdef0123456789abcdef01234567"


# ------------------------------------------------------------------------------------- helpers


@dataclass
class SigningKey:
    sk: Ed25519PrivateKey
    key_id: bytes

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate(), os.urandom(8))

    @property
    def key_id_hex(self) -> str:
        return self.key_id[::-1].hex().upper()

    def public_file(self) -> bytes:
        pk = self.sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        raw = b"Ed" + self.key_id + pk
        return (
            f"untrusted comment: minisign public key {self.key_id_hex}\n".encode()
            + base64.b64encode(raw)
            + b"\n"
        )

    def sign(self, data: bytes, comment: bytes = b"test release", *, legacy: bool = False) -> bytes:
        """A minisign signature file (what ``minisign -S`` writes)."""
        alg = b"Ed" if legacy else b"ED"
        message = data if legacy else hashlib.blake2b(data, digest_size=64).digest()
        sig = self.sk.sign(message)
        return (
            b"untrusted comment: signature from test key\n"
            + base64.b64encode(alg + self.key_id + sig)
            + b"\ntrusted comment: "
            + comment
            + b"\n"
            + base64.b64encode(self.sk.sign(sig + comment))
            + b"\n"
        )


@pytest.fixture
def key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def keys_dir(tmp_path: Path, key: SigningKey) -> Path:
    d = tmp_path / "release-keys"
    d.mkdir()
    (d / f"{key.key_id_hex}.pub").write_bytes(key.public_file())
    (d / "README.md").write_text("not a key\n")
    return d


def _sums(root: Path) -> bytes:
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        rel = path.relative_to(root).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n")
    return "".join(sorted(lines, key=lambda line: line.split("  ", 1)[1])).encode()


def _pack(
    stage: Path, archive: Path, extra: Callable[[tarfile.TarFile], None] | None = None
) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage / "mdhub", arcname="mdhub")
        if extra is not None:
            extra(tar)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, **attrs: Any) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for k, v in attrs.items():
        setattr(info, k, v)
    tar.addfile(info, io.BytesIO(data))


def make_release(
    tmp_path: Path,
    key: SigningKey | None,
    *,
    version: str = "1.2.0",
    manifest_patch: dict[str, Any] | None = None,
    after_sign: Callable[[Path], object] | None = None,
    extra: Callable[[tarfile.TarFile], None] | None = None,
) -> Path:
    """Build a small signed code-only release in tmp_path; returns the archive path."""
    stage = tmp_path / "stage"
    root = stage / "mdhub"
    migrations = root / "src" / "mdhub" / "db" / "migrations"
    migrations.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "market-data-hub"\n\n[tool.mdhub.release]\n'
        'min_previous_version = "1.0.0"\n'
    )
    (root / "src" / "mdhub" / "cli.py").write_text("print('hello')\n")
    (migrations / "0001_core.sql").write_text("CREATE SCHEMA md;\n")
    (migrations / "0002_more.sql").write_text("CREATE TABLE md.t (x int);\n")
    (root / "deploy").mkdir()
    (root / "deploy" / "install.sh").write_text("#!/usr/bin/env bash\n")
    manifest = build_manifest(root, version=version, commit=COMMIT)
    data = manifest.to_json()
    data.update(manifest_patch or {})
    (root / "MANIFEST.json").write_text(json.dumps(data, indent=2) + "\n")
    sums = _sums(root)
    (root / "SHA256SUMS").write_bytes(sums)
    archive = tmp_path / f"market-data-hub-{version}.tar.gz"
    if key is not None:
        default_signature_path(archive).write_bytes(key.sign(sums))
    if after_sign is not None:
        after_sign(root)
    _pack(stage, archive, extra)
    return archive


# -------------------------------------------------------------------------------- happy paths


def test_signed_release_verifies(tmp_path: Path, key: SigningKey, keys_dir: Path) -> None:
    archive = make_release(tmp_path, key)
    assert default_signature_path(archive).name == "market-data-hub-1.2.0.SHA256SUMS.minisig"
    result = verify_archive(archive, load_pinned_keys(keys_dir))
    assert result.key is not None and result.key.key_id == key.key_id
    assert result.warnings == ()
    assert result.trusted_comment == "test release"
    m = result.manifest
    assert (m.version, m.commit, m.min_previous_version) == ("1.2.0", COMMIT, "1.0.0")
    assert [v for v, _ in m.migrations] == ["0001_core", "0002_more"]
    assert m.migrations[0][1] == hashlib.sha256(b"CREATE SCHEMA md;\n").hexdigest()
    assert result.files == 7  # 5 tree files + MANIFEST.json + SHA256SUMS


def test_detached_signature_option_and_legacy_algorithm(
    tmp_path: Path, key: SigningKey, keys_dir: Path
) -> None:
    archive = make_release(tmp_path, None)
    with tarfile.open(archive) as tar:
        member = tar.extractfile("mdhub/SHA256SUMS")
        assert member is not None
        sums = member.read()
    sig = tmp_path / "elsewhere.minisig"
    sig.write_bytes(key.sign(sums, legacy=True))
    result = verify_archive(archive, load_pinned_keys(keys_dir), signature=sig)
    assert result.key is not None
    with pytest.raises(ReleaseError, match="not found"):
        verify_archive(archive, load_pinned_keys(keys_dir), signature=tmp_path / "nope.minisig")


def test_key_rotation_any_pinned_key(tmp_path: Path, key: SigningKey, keys_dir: Path) -> None:
    new = SigningKey.generate()
    (keys_dir / f"{new.key_id_hex}.pub").write_bytes(new.public_file())
    keys = load_pinned_keys(keys_dir)
    assert len(keys) == 2
    archive = make_release(tmp_path, new)
    result = verify_archive(archive, keys)
    assert result.key is not None and result.key.key_id == new.key_id


# ------------------------------------------------------------------------------ signature


def test_unsigned_refused_unless_allowed(tmp_path: Path, keys_dir: Path) -> None:
    archive = make_release(tmp_path, None)
    keys = load_pinned_keys(keys_dir)
    with pytest.raises(ReleaseError, match="unsigned"):
        verify_archive(archive, keys)
    result = verify_archive(archive, keys, allow_unsigned=True)
    assert result.key is None and result.warnings and "UNSIGNED" in result.warnings[0]


def test_unknown_key_refused_unless_allowed(tmp_path: Path, keys_dir: Path) -> None:
    stranger = SigningKey.generate()
    archive = make_release(tmp_path, stranger)
    keys = load_pinned_keys(keys_dir)
    with pytest.raises(ReleaseError, match=f"unknown key {stranger.key_id_hex}"):
        verify_archive(archive, keys)
    with pytest.raises(ReleaseError, match="pinned: none"):
        verify_archive(archive, {})
    result = verify_archive(archive, keys, allow_unsigned=True)
    assert result.key is None and "UNTRUSTED" in result.warnings[0]


def test_tampered_sums_refused_even_with_allow_unsigned(
    tmp_path: Path, key: SigningKey, keys_dir: Path
) -> None:
    def tamper(root: Path) -> None:
        (root / "src" / "mdhub" / "cli.py").write_text("import os; os.system('evil')\n")
        (root / "SHA256SUMS").write_bytes(_sums(root))

    archive = make_release(tmp_path, key, after_sign=tamper)
    for allow in (False, True):
        with pytest.raises(ReleaseError, match="does not verify"):
            verify_archive(archive, load_pinned_keys(keys_dir), allow_unsigned=allow)


def test_tampered_trusted_comment_refused(tmp_path: Path, key: SigningKey, keys_dir: Path) -> None:
    archive = make_release(tmp_path, key)
    sig_path = default_signature_path(archive)
    sig_path.write_bytes(sig_path.read_bytes().replace(b"test release", b"other release"))
    with pytest.raises(ReleaseError, match="trusted comment signature"):
        verify_archive(archive, load_pinned_keys(keys_dir))


def test_malformed_signature_refused(tmp_path: Path, key: SigningKey, keys_dir: Path) -> None:
    archive = make_release(tmp_path, key)
    default_signature_path(archive).write_text("untrusted comment: x\nnot base64!\n")
    with pytest.raises(ReleaseError, match="not a minisign signature"):
        verify_archive(archive, load_pinned_keys(keys_dir), allow_unsigned=True)


# ------------------------------------------------------------------------------ file hashes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: (r / "deploy" / "install.sh").write_text("rm -rf /\n"), "hash mismatch"),
        (lambda r: (r / "deploy" / "extra.sh").write_text("x\n"), "not in SHA256SUMS"),
        (lambda r: (r / "deploy" / "install.sh").unlink(), "missing from the archive"),
    ],
    ids=["modified", "added", "removed"],
)
def test_tampered_files_refused(
    tmp_path: Path,
    key: SigningKey,
    keys_dir: Path,
    change: Callable[[Path], object],
    message: str,
) -> None:
    archive = make_release(tmp_path, key, after_sign=change)
    with pytest.raises(ReleaseError, match=message):
        verify_archive(archive, load_pinned_keys(keys_dir), allow_unsigned=True)


def _symlink(tar: tarfile.TarFile) -> None:
    info = tarfile.TarInfo("mdhub/deploy/link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    tar.addfile(info)


def _hardlink(tar: tarfile.TarFile) -> None:
    info = tarfile.TarInfo("mdhub/deploy/hard")
    info.type = tarfile.LNKTYPE
    info.linkname = "mdhub/deploy/install.sh"
    tar.addfile(info)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (_symlink, "not a regular file"),
        (_hardlink, "not a regular file"),
        (lambda t: _add_bytes(t, "mdhub/../evil.py", b"x"), "unsafe path"),
        (lambda t: _add_bytes(t, "/etc/cron.d/evil", b"x"), "outside the top-level"),
        (lambda t: _add_bytes(t, "other/evil.py", b"x"), "outside the top-level"),
        (lambda t: _add_bytes(t, "mdhub/deploy/install.sh", b"x"), "duplicate archive member"),
        (lambda t: _add_bytes(t, "mdhub/deploy/su", b"x", mode=0o4755), "setuid"),
    ],
    ids=["symlink", "hardlink", "dotdot", "absolute", "other-top", "duplicate", "setuid"],
)
def test_unsafe_members_refused(
    tmp_path: Path,
    key: SigningKey,
    keys_dir: Path,
    extra: Callable[[tarfile.TarFile], None],
    message: str,
) -> None:
    archive = make_release(tmp_path, key, extra=extra)
    with pytest.raises(ReleaseError, match=message):
        verify_archive(archive, load_pinned_keys(keys_dir), allow_unsigned=True)


def test_not_a_tarball_refused(tmp_path: Path, keys_dir: Path) -> None:
    bogus = tmp_path / "market-data-hub-1.0.0.tar.gz"
    bogus.write_bytes(b"PK\x03\x04 this is a zip")
    with pytest.raises(ReleaseError, match=r"not a readable \.tar\.gz"):
        verify_archive(bogus, load_pinned_keys(keys_dir))


# --------------------------------------------------------------------------------- manifest


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"migrations": [{"version": "0001_core", "sha256": "0" * 64}]}, "migrations differ"),
        ({"min_previous_version": "1.3.0"}, "newer than version"),
        ({"version": "1.2.0.dev3+g0123456"}, "bad release version"),
        ({"format": 2}, "unsupported format"),
        ({"name": "other"}, "not a market-data-hub release"),
        ({"commit": "abc"}, "bad commit"),
    ],
    ids=["migrations", "min-previous", "dev-version", "format", "name", "commit"],
)
def test_manifest_sanity(
    tmp_path: Path, key: SigningKey, keys_dir: Path, patch: dict[str, Any], message: str
) -> None:
    archive = make_release(tmp_path, key, manifest_patch=patch)
    with pytest.raises(ReleaseError, match=message):
        verify_archive(archive, load_pinned_keys(keys_dir))


def test_manifest_roundtrip_and_missing_policy(tmp_path: Path) -> None:
    manifest = build_manifest(REPO, version="0.2.0", commit=COMMIT)
    assert parse_manifest(manifest.dumps().encode()) == manifest
    assert manifest.migrations == tuple((m.version, m.checksum) for m in discover_migrations())
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "market-data-hub"\n')
    with pytest.raises(ReleaseError, match="min_previous_version not set"):
        build_manifest(tmp_path, version="0.2.0", commit=COMMIT)


def test_version_key_order() -> None:
    order = ["0.9.9", "1.0.0a1", "1.0.0a2", "1.0.0b1", "1.0.0rc1", "1.0.0", "1.0.1", "1.10.0"]
    assert sorted(order, key=version_key) == order
    for bad in ("1.0", "v1.0.0", "1.0.0.dev1", "1.0.0+local", "01.0.0"):
        with pytest.raises(ReleaseError):
            version_key(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "0.7.0\n",  # a `$` would accept a trailing newline
        "0.7.0 ",
        " 0.7.0",
        "0.7.0\r\n",
        "0.7.0\n0.7.1",
        "\u0660.\u0667.\u0660",  # Arabic-Indic digits: int() reads them, a release cannot have them
        "0.7.0rc",
        "0.7.0rc01",
        "",
    ],
)
def test_version_key_is_strict(bad: str) -> None:
    with pytest.raises(ReleaseError):
        version_key(bad)


def test_allowed_next_versions_and_check() -> None:
    assert allowed_next_versions("0.6.2") == ("0.6.3", "0.7.0", "1.0.0")
    assert allowed_next_versions("1.9.9") == ("1.9.10", "1.10.0", "2.0.0")
    # after a pre-release only its own X.Y.Z (the final, or a later pre-release): a step from it
    # would skip a version whose final was never released, and nobody can release it afterwards
    assert allowed_next_versions("0.7.0rc1") == ("0.7.0",)
    assert allowed_next_versions("1.0.0a3") == ("1.0.0",)
    for new in ("0.6.3", "0.7.0", "1.0.0", "0.6.3a1", "0.7.0b2", "1.0.0rc1"):
        check_next_version(new, "0.6.2")
    for new in ("0.7.0rc2", "0.7.0"):
        check_next_version(new, "0.7.0rc1")
    for new in ("0.7.1", "0.8.0", "1.0.0", "0.7.1rc1"):
        with pytest.raises(ReleaseError, match=r"not the next one.*allowed are 0\.7\.0,"):
            check_next_version(new, "0.7.0rc1")
    check_next_version("3.4.5", None)  # no earlier tag: any well-formed version
    for new in ("0.6.4", "0.8.0", "0.9.0", "2.0.0", "1.1.0"):
        with pytest.raises(
            ReleaseError, match=r"not the next one.*allowed are 0\.6\.3, 0\.7\.0, 1\.0\.0"
        ):
            check_next_version(new, "0.6.2")
    for new in ("0.6.2", "0.6.1", "0.6.2rc1", "0.5.0"):
        with pytest.raises(ReleaseError, match=r"not greater than the latest tag v0\.6\.2"):
            check_next_version(new, "0.6.2")
    with pytest.raises(ReleaseError):
        check_next_version("0.7.0\n", "0.6.2")
    with pytest.raises(ReleaseError):
        check_next_version("0.7.0rc1", "0.7.0rc1")
    tags = ["v0.6.2", "v0.10.0", "v0.6.10", "latest", "v1", "v0.7.0rc1", "x0.1.0", "v0.7.0\n"]
    assert [t for _, t in release_tags(tags)] == ["v0.6.2", "v0.6.10", "v0.7.0rc1", "v0.10.0"]


# -------------------------------------------------------------------------------- key files


def test_pinned_key_list_errors(tmp_path: Path, key: SigningKey) -> None:
    with pytest.raises(ReleaseError, match="not found"):
        load_pinned_keys(tmp_path / "missing")
    d = tmp_path / "keys"
    d.mkdir()
    assert load_pinned_keys(d) == {}
    (d / "a.pub").write_bytes(key.public_file())
    (d / "b.pub").write_bytes(key.public_file())
    with pytest.raises(ReleaseError, match=r"also in a\.pub"):
        load_pinned_keys(d)
    (d / "b.pub").write_text("untrusted comment: x\nAAAA\n")
    with pytest.raises(ReleaseError, match="not a minisign ed25519 public key"):
        load_pinned_keys(d)


def test_repo_keys_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(KEYS_DIR_ENV, raising=False)
    assert default_keys_dir() == DEPLOY / "release-keys"
    keys = load_pinned_keys(default_keys_dir())  # every committed key parses
    assert all(k.source == f"{k.key_id_hex}.pub" for k in keys.values())
    names = [p.name for p in (DEPLOY / "release-keys").iterdir()]
    assert not [n for n in names if "sec" in n or n.endswith(".key")], names
    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path))
    assert default_keys_dir() == tmp_path
    dockerfile = (DEPLOY / "Dockerfile").read_text()
    assert "COPY deploy/release-keys /app/release-keys" in dockerfile
    assert f"{KEYS_DIR_ENV}=/app/release-keys" in dockerfile
    assert "!deploy/release-keys/" in (DEPLOY / "Dockerfile.dockerignore").read_text()


# ------------------------------------------------------------------------------------- CLI


def test_cli_verify(
    tmp_path: Path, key: SigningKey, keys_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    archive = make_release(tmp_path, key)
    ok = runner.invoke(app, ["release", "verify", str(archive), "--keys-dir", str(keys_dir)])
    assert ok.exit_code == 0, ok.output
    assert f"OK market-data-hub 1.2.0 (commit {COMMIT[:12]})" in ok.output
    assert key.key_id_hex in ok.output

    as_json = runner.invoke(
        app, ["release", "verify", str(archive), "--keys-dir", str(keys_dir), "--json"]
    )
    data = json.loads(as_json.stdout)
    assert data["ok"] is True and data["signed_by"] == key.key_id_hex
    assert data["manifest"]["version"] == "1.2.0"

    default_signature_path(archive).unlink()
    refused = runner.invoke(app, ["release", "verify", str(archive), "--keys-dir", str(keys_dir)])
    assert refused.exit_code == 1 and "REFUSED" in refused.output and "unsigned" in refused.output
    allowed = runner.invoke(
        app,
        ["release", "verify", str(archive), "--keys-dir", str(keys_dir), "--allow-unsigned"],
    )
    assert allowed.exit_code == 0 and "WARNING: UNSIGNED" in allowed.output
    assert "NOT signed by a pinned key" in allowed.output

    monkeypatch.setenv(KEYS_DIR_ENV, str(tmp_path / "missing"))
    no_keys = runner.invoke(app, ["release", "verify", str(archive)])
    assert no_keys.exit_code == 2 and "release keys directory" in no_keys.output

    listed = runner.invoke(app, ["release", "keys", "--keys-dir", str(keys_dir)])
    assert listed.exit_code == 0 and f"{key.key_id_hex}  {key.key_id_hex}.pub" in listed.output


# --------------------------------------------------------------------- make_bundle.sh --code-only


def _run_bundle(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MDHUB_CMD": f"{sys.executable} -m mdhub.cli", "TMPDIR": str(tmp_path)}
    return subprocess.run(
        [str(DEPLOY / "make_bundle.sh"), "--code-only", "--allow-dirty", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_make_bundle_code_only(tmp_path: Path, key: SigningKey, keys_dir: Path) -> None:
    dist = tmp_path / "dist"
    built = _run_bundle(tmp_path, "--version", "9.8.7", "--out-dir", str(dist))
    assert built.returncode == 0, built.stderr
    archive = dist / "market-data-hub-9.8.7.tar.gz"
    sums_copy = dist / "market-data-hub-9.8.7.SHA256SUMS"
    assert sorted(p.name for p in dist.iterdir()) == [sums_copy.name, archive.name]
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert all(m.uid == 0 and m.gid == 0 for m in tar.getmembers())
        inner_sums = tar.extractfile("mdhub/SHA256SUMS")
        manifest_file = tar.extractfile("mdhub/MANIFEST.json")
        assert inner_sums is not None and manifest_file is not None
        assert inner_sums.read() == sums_copy.read_bytes()
        manifest = parse_manifest(manifest_file.read())
    assert not [n for n in names if n.startswith("mdhub/db/") or n.endswith(".dump")]
    assert "mdhub/SEED_REPORT.md" not in names and "mdhub/MANIFEST.txt" not in names
    assert not [n for n in names if "/.git/" in n or n.endswith((".pyc", ".env"))]
    assert manifest.version == "9.8.7"
    assert (
        manifest.commit
        == subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    # Unsigned until signed; then a signature by a pinned key verifies.
    with pytest.raises(ReleaseError, match="unsigned"):
        verify_archive(archive, load_pinned_keys(keys_dir))
    default_signature_path(archive).write_bytes(key.sign(sums_copy.read_bytes()))
    result = verify_archive(archive, load_pinned_keys(keys_dir))
    assert result.key is not None and result.manifest == manifest
    assert len(manifest.migrations) == len(
        [n for n in names if n.startswith("mdhub/src/mdhub/db/migrations/") and n.endswith(".sql")]
    )

    # A rebuild drops the stale signature next to the archive.
    again = _run_bundle(tmp_path, "--version", "9.8.7", "--out-dir", str(dist))
    assert again.returncode == 0, again.stderr
    assert not default_signature_path(archive).exists()


def test_make_bundle_code_only_rejects_bad_args(tmp_path: Path) -> None:
    bad = _run_bundle(tmp_path, "--version", "1.0", "--out-dir", str(tmp_path / "d"))
    assert bad.returncode != 0 and "--version: expected X.Y.Z" in bad.stderr
    env = {**os.environ}
    env.pop("MDHUB_DATABASE_URL", None)
    env.pop("MDHUB_BUNDLE_DATABASE_URL", None)
    full = subprocess.run(
        [str(DEPLOY / "make_bundle.sh"), "--version", "1.0.0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert full.returncode != 0 and "--code-only only" in full.stderr


# --------------------------------------------------------------------------- minisign interop


@pytest.mark.skipif(MINISIGN is None, reason="minisign not installed")
def test_minisign_interop(tmp_path: Path, key: SigningKey) -> None:
    assert MINISIGN is not None
    gen = subprocess.run(
        [MINISIGN, "-G", "-W", "-p", str(tmp_path / "k.pub"), "-s", str(tmp_path / "k.key")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    assert gen.returncode == 0, gen.stderr
    pinned = tmp_path / "keys"
    pinned.mkdir()
    shutil.copy(tmp_path / "k.pub", pinned / "k.pub")

    # minisign signs, we verify (prehashed default and legacy -l).
    archive = make_release(tmp_path, None)
    sums = tmp_path / "SHA256SUMS"
    with tarfile.open(archive) as tar:
        member = tar.extractfile("mdhub/SHA256SUMS")
        assert member is not None
        sums.write_bytes(member.read())
    for legacy in ([], ["-l"]):
        signed = subprocess.run(
            [
                *(MINISIGN, "-S", *legacy, "-s", str(tmp_path / "k.key"), "-m", str(sums)),
                *("-x", str(default_signature_path(archive)), "-t", "market-data-hub 1.2.0"),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
        assert signed.returncode == 0, signed.stderr
        result = verify_archive(archive, load_pinned_keys(pinned))
        assert result.trusted_comment == "market-data-hub 1.2.0"
        sig = parse_signature(default_signature_path(archive).read_bytes())
        assert sig.algorithm == (b"Ed" if legacy else b"ED")

    # We parse minisign's public key with the id it prints; minisign verifies our format.
    pub = parse_public_key((tmp_path / "k.pub").read_bytes())
    # minisign prints the id with %X, dropping leading zeros (1 key in 16)
    assert pub.key_id_hex.lstrip("0") in (tmp_path / "k.pub").read_text()
    data = b"payload\n"
    (tmp_path / "ours.pub").write_bytes(key.public_file())
    (tmp_path / "payload").write_bytes(data)
    (tmp_path / "payload.minisig").write_bytes(key.sign(data, b"ours"))
    checked = subprocess.run(
        [MINISIGN, "-V", "-p", str(tmp_path / "ours.pub"), "-m", str(tmp_path / "payload")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, checked.stderr
    verify_signature(data, parse_signature(key.sign(data)), parse_public_key(key.public_file()))


# ------------------------------------------------------------------------------- workflows


def _workflow(name: str) -> dict[Any, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(data, dict)
    return data


def test_ci_workflow() -> None:
    wf = _workflow("ci.yml")
    triggers = wf[True]  # YAML 1.1 reads the key `on` as True
    assert {"push", "pull_request", "workflow_call"} <= set(triggers)
    assert wf["permissions"] == {"contents": "read"}
    # a call from release.yml gets a group of its own (the run id), so that no other run replaces
    # or cancels the tests of a release; push and pull_request keep the plain group
    call = triggers["workflow_call"]["inputs"]["isolated"]
    assert call["type"] == "boolean" and call["default"] is False
    group = wf["concurrency"]["group"]
    assert "inputs.isolated" in group and "github.run_id" in group
    assert group.startswith("ci-${{ github.workflow }}-${{ github.ref }}")
    text = (WORKFLOWS / "ci.yml").read_text()
    for needle in (
        "uv sync --frozen",
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run mypy",
        "uv run pytest",
    ):
        assert needle in text
    assert "secrets." not in text


def test_release_workflow() -> None:
    wf = _workflow("release.yml")
    triggers = wf[True]
    # a pushed tag and "Run workflow" with the version (main only, checked in the job `check`)
    assert triggers["push"] == {"tags": ["v*"]}
    version_input = triggers["workflow_dispatch"]["inputs"]["version"]
    assert version_input["required"] is True and version_input["type"] == "string"
    assert wf["permissions"] == {}
    jobs = wf["jobs"]
    assert list(jobs) == ["check", "ci", "build", "sign", "verify", "publish"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml" and jobs["ci"]["needs"] == "check"
    assert jobs["build"]["needs"] == "ci"
    sign = jobs["sign"]
    assert sign["environment"] == "release" and sign["needs"] == "build"
    assert sign.get("permissions") == {}
    sign_text = yaml.safe_dump(sign)
    assert "secrets.MINISIGN_SECRET_KEY" in sign_text and "secrets.MINISIGN_PASSWORD" in sign_text
    assert "uv " not in sign_text and "checkout" not in sign_text  # the key never meets Python
    for name, job in jobs.items():
        if name != "sign":
            assert "secrets.MINISIGN" not in yaml.safe_dump(job), name
    verify = jobs["verify"]
    assert verify["permissions"] == {"contents": "read"} and "sign" in verify["needs"]
    verify_text = yaml.safe_dump(verify)
    assert "mdhub release verify" in verify_text and "--allow-unsigned" not in verify_text
    assert "--keys-dir deploy/release-keys" in verify_text
    publish = jobs["publish"]
    assert publish["permissions"] == {"contents": "write"} and "verify" in publish["needs"]
    assert "gh release create" in yaml.safe_dump(publish)
    assert [n for n, j in jobs.items() if "write" in str(j.get("permissions"))] == ["publish"]
    for job in jobs.values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert "@" in uses and len(uses.split("@")[1]) == 40, uses  # pinned by SHA


def test_release_workflow_run_workflow_path() -> None:
    """ "Run workflow" (a session may start it): main only, a version that is the next one, the tag
    made by the release after sign and verify, no input text in any script, nothing that can cancel
    a release."""
    text = (WORKFLOWS / "release.yml").read_text()
    wf = _workflow("release.yml")
    jobs = wf["jobs"]
    # the version input reaches the scripts only as an environment value, never in script text:
    # not in a `run`, not in a `with`, only in run-name (display), env and the job-level values
    for name, job in jobs.items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert "${{" not in run, (name, step.get("name"))
            assert "git push" not in run, (name, step.get("name"))  # a local tag is never pushed
            assert "inputs." not in str(step.get("with", "")), (name, step.get("name"))
        assert "inputs." not in str(job.get("with", {})), name
    assert "inputs.version" in wf["run-name"] and "github.sha" in wf["run-name"]
    assert "github.ref_name" in wf["run-name"]
    assert "inputs.version" in wf["env"]["RELEASE_TAG"]
    # one release at a time, none cancelled by another run; ci cannot be replaced either
    assert wf["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert jobs["ci"]["with"] == {"isolated": True}
    # check: the summary for the approval on both ways; main only, the next version, no tag and no
    # release yet on Run workflow only
    check = jobs["check"]
    assert check["permissions"] == {"contents": "read"}
    steps = {step["name"]: step for step in check["steps"] if "name" in step}
    main_only = steps["Run workflow releases main only"]
    assert "workflow_dispatch" in main_only["if"] and "refs/heads/main" in main_only["run"]
    version_step = steps["version is the next one after the latest tag; summary for the approval"]
    assert "if" not in version_step  # the summary is written for a pushed tag too
    assert 'check-next --version "${RELEASE_TAG#v}" "$enforce"' in version_step["run"]
    assert '--summary "$GITHUB_STEP_SUMMARY"' in version_step["run"]
    assert "--enforce" in version_step["run"] and "--no-enforce" in version_step["run"]
    exists = steps["tag and release do not exist yet"]
    assert "workflow_dispatch" in exists["if"]
    for needle in ("(HTTP 404)", "exists already", "releases/tags/$RELEASE_TAG"):
        assert needle in exists["run"], needle
    tags = steps["the release tags seen by this check"]
    assert "workflow_dispatch" in tags["if"] and tags["id"] == "tags"
    assert check["outputs"] == {"tags": "${{ steps.tags.outputs.tags }}"}
    # build: the commit gets the tag v<version> in this checkout only, so that hatch-vcs,
    # make_bundle.sh and .git_archival.txt see what a tag build sees; it is never pushed
    build_text = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert 'git tag "$RELEASE_TAG"' in build_text
    assert "ref: ${{ github.event_name == 'workflow_dispatch' && github.sha || '' }}" in text
    # the tag is made by publish only, on the built commit, after verify (which needs sign) and
    # before any release exists; the release then only uses the existing tag
    publish = jobs["publish"]
    assert set(publish["needs"]) == {"check", "build", "verify"}
    assert publish["env"]["TAGS_AT_CHECK"] == "${{ needs.check.outputs.tags }}"
    publish_text = "\n".join(step.get("run", "") for step in publish["steps"])
    assert '-f ref="refs/tags/$RELEASE_TAG" -f sha="$GITHUB_SHA"' in publish_text
    assert "--verify-tag" in publish_text and "--target" not in publish_text
    assert publish_text.index("git/refs") < publish_text.index("gh release create")
    assert "gh release delete" not in publish_text and "git/refs/tags" not in publish_text
    # gh prints the body of an HTTP error to stdout, so an error must never be swallowed (`|| true`,
    # `2>/dev/null`) around a lookup: the tag is looked up with matching-refs (200 and [] for "no
    # tag") by the exact name, and the lookup of one ref (a 404 body in stdout) is not used
    assert "matching-refs/tags/$RELEASE_TAG" in publish_text and "git/ref/tags" not in publish_text
    for name, job in jobs.items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "gh " in run:
                assert "|| true" not in run and "2>/dev/null" not in run, (name, step.get("name"))
    assert [n for n, j in jobs.items() if "git/refs" in str(j)] == ["publish"]
    assert "sign" in jobs["verify"]["needs"] and "verify" in jobs["publish"]["needs"]


# ----------------------------------------------------- the scripts of the steps, run with a fake gh


def _step_script(job: str, name: str) -> str:
    for step in _workflow("release.yml")["jobs"][job]["steps"]:
        if step.get("name") == name:
            return str(step["run"])
    raise AssertionError(f"no step {name!r} in {job}")


FAKE_GH = r"""#!/usr/bin/env bash
# A stand-in for `gh`, with the behaviour of the real one (checked against gh 2.89 on a missing and
# an existing tag and release, GET only): on an HTTP error the BODY of the error goes to STDOUT
# ("--jq" is not applied to it), "gh: <message> (HTTP <code>)" goes to stderr, the exit status is 1.
# A missing ref or release is a 404; matching-refs answers 200 with [] and matches by PREFIX.
# State in $FAKE_GH_DIR (tags: "<name> <sha>" per line, releases: one tag per line).
set -u
d="$FAKE_GH_DIR"
echo "gh $*" >>"$d/calls.log"
http_error() {  # code, message
  printf '{"message":"%s","documentation_url":"https://docs.github.com","status":"%s"}' "$2" "$1"
  echo "gh: $2 (HTTP $1)" >&2
  exit 1
}
if [[ "$1" == "api" ]]; then
  shift
  method=GET; jq=""; ref=""; sha=""; path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --method) method="$2"; shift 2;;
      --paginate) shift;;
      --jq) jq="$2"; shift 2;;
      -f) case "$2" in ref=*) ref="${2#ref=}";; sha=*) sha="${2#sha=}";; esac; shift 2;;
      *) path="$1"; shift;;
    esac
  done
  case "$path" in
    */git/matching-refs/tags/*)
      prefix="${path##*/git/matching-refs/tags/}"
      if [[ "$prefix" == "v" ]]; then
        [[ -n "${FAKE_GH_LIST_ERROR:-}" ]] && http_error 502 "Bad Gateway"
      else
        [[ -n "${FAKE_GH_REF_ERROR:-}" ]] && http_error 502 "Bad Gateway"
      fi
      found=()
      while read -r name sha_of; do
        [[ "$name" == "$prefix"* ]] && found+=("$name $sha_of")
      done <"$d/tags"
      case "$jq" in
        '.[].ref') for row in "${found[@]}"; do echo "refs/tags/${row% *}"; done;;
        '.[] | "\(.ref) \(.object.sha)"') for row in "${found[@]}"; do echo "refs/tags/$row"; done;;
        "") echo '[]';;
        *) echo "unexpected jq: $jq" >&2; exit 2;;
      esac
      exit 0;;
    */git/ref/tags/*)  # one ref: a missing one is a 404 (the exact-ref lookup is not used any more)
      name="${path##*/git/ref/tags/}"
      line="$(grep "^$name " "$d/tags" || true)"
      [[ -z "$line" ]] && http_error 404 "Not Found"
      echo "${line#* }"; exit 0;;
    */git/refs)
      name="${ref#refs/tags/}"
      if grep -q "^$name " "$d/tags"; then http_error 422 "Reference already exists"; fi
      echo "$name ${FAKE_GH_TAG_AT:-$sha}" >>"$d/tags"; echo '{}'; exit 0;;
    */releases/tags/*)
      name="${path##*/releases/tags/}"
      [[ -n "${FAKE_GH_RELEASE_ERROR:-}" ]] && http_error 502 "Bad Gateway"
      if grep -qx "$name" "$d/releases"; then echo "{\"tag_name\":\"$name\"}"; exit 0; fi
      http_error 404 "Not Found";;
  esac
fi
if [[ "$1" == "release" && "$2" == "create" ]]; then
  tag="$3"
  case " $* " in
    *" --verify-tag "*) ;;
    *) echo "release create without --verify-tag" >&2; exit 1;;
  esac
  if ! grep -q "^$tag " "$d/tags"; then echo "tag $tag does not exist" >&2; exit 1; fi
  echo "$tag" >>"$d/releases"; exit 0
fi
echo "unexpected gh call: $*" >&2; exit 2
"""

FAKE_UV = r"""#!/usr/bin/env bash
# uv sync: nothing; uv run --frozen mdhub ...: this checkout's mdhub
if [[ "$1" == "sync" ]]; then exit 0; fi
if [[ "$1" == "run" ]]; then
  shift; [[ "$1" == "--frozen" ]] && shift; shift
  exec "$MDHUB_PYTHON" -m mdhub.cli "$@"
fi
echo "unexpected uv call: $*" >&2; exit 2
"""

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER_SHA = "89abcdef0123456789abcdef0123456789abcdef"


@dataclass
class StepEnv:
    root: Path
    bin: Path
    state: Path
    repo: Path

    def run(
        self, script: str, *, cwd: Path | None = None, **env: str
    ) -> subprocess.CompletedProcess[str]:
        file = self.root / "step.sh"
        file.write_text(script)
        base = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_SHA": SHA,
            "GH_TOKEN": "x",
            "FAKE_GH_DIR": str(self.state),
            "MDHUB_PYTHON": sys.executable,
            "GITHUB_STEP_SUMMARY": str(self.root / "summary.md"),
            "GITHUB_OUTPUT": str(self.root / "output.txt"),
        }
        return subprocess.run(
            ["bash", "-e", str(file)],  # what the runner does for a step without `shell:`
            cwd=cwd or self.repo,
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=300,
        )

    def tags(self) -> list[str]:
        return [line.split()[0] for line in (self.state / "tags").read_text().splitlines()]

    def calls(self) -> list[str]:
        file = self.state / "calls.log"
        return file.read_text().splitlines() if file.exists() else []


@pytest.fixture
def step_env(tmp_path: Path) -> StepEnv:
    bin_dir, state, repo = tmp_path / "bin", tmp_path / "state", tmp_path / "repo"
    for d in (bin_dir, state, repo):
        d.mkdir()
    for name, body in (("gh", FAKE_GH), ("uv", FAKE_UV)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (state / "tags").write_text("")
    (state / "releases").write_text("")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )

    git("init", "-q", "-b", "main")
    for i, tag in enumerate(["v0.5.0", "v0.6.0", "v0.6.2"]):
        git("commit", "-q", "--allow-empty", "-m", f"change `{i}` before {tag}")
        git("tag", tag)
    git("tag", "not-a-release")
    for subject in ("Fix a thing", "Add `code` thing"):
        git("commit", "-q", "--allow-empty", "-m", subject)
    for tag in ("v0.5.0", "v0.6.0", "v0.6.2"):
        with (state / "tags").open("a") as out:
            out.write(f"{tag} {OTHER_SHA}\n")
    return StepEnv(tmp_path, bin_dir, state, repo)


CHECK_VERSION = "version is the next one after the latest tag; summary for the approval"


def test_step_main_only(step_env: StepEnv) -> None:
    script = _step_script("check", "Run workflow releases main only")
    assert step_env.run(script, GITHUB_REF="refs/heads/main").returncode == 0
    for ref in ("refs/heads/feature", "refs/tags/v0.7.0", "refs/pull/1/merge"):
        refused = step_env.run(script, GITHUB_REF=ref)
        assert refused.returncode == 1 and "releases main only" in refused.stdout


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("0.6.3", True),
        ("0.7.0", True),
        ("1.0.0", True),
        ("0.6.3rc1", True),
        ("0.7.0a1", True),
        ("0.9.0", False),  # skips ahead: a server would never see 0.6.3 or 0.7.0 again
        ("0.6.4", False),
        ("1.1.0", False),
        ("0.6.2", False),
        ("0.6.1", False),
        ("0.7", False),
        ("v0.7.0", False),
        ("0.7.0\n", False),
    ],
)
def test_step_check_next_version(step_env: StepEnv, version: str, ok: bool) -> None:
    script = _step_script("check", CHECK_VERSION)
    out = step_env.run(
        script,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_ACTIONS="true",
        RELEASE_TAG=f"v{version}",
    )
    file = step_env.root / "summary.md"
    summary = file.read_text() if file.exists() else ""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=step_env.repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    if ok:
        assert out.returncode == 0, out.stderr
        assert f"## Release v{version}" in summary
        assert f"- commit: `{head}`" in summary
        assert "- previous tag: `v0.6.2`" in summary
        assert "- version check: ok - the next one after v0.6.2" in summary
        # the commits since the previous tag, one line each, no backquote of a subject survives
        assert "### Commits since v0.6.2: 2" in summary
        assert "Fix a thing" in summary and "Add 'code' thing" in summary
    else:
        assert out.returncode == 1 and "::error::" in out.stderr, (out.stdout, out.stderr)
        assert "REFUSED" in summary or "bad release version" in out.stderr


def test_step_check_pushed_tag_is_not_enforced(step_env: StepEnv) -> None:
    """A pushed tag is the owner's act: the summary says that it is odd, the run goes on."""
    subprocess.run(["git", "tag", "v0.9.0"], cwd=step_env.repo, check=True)
    out = step_env.run(
        _step_script("check", CHECK_VERSION),
        GITHUB_EVENT_NAME="push",
        GITHUB_ACTIONS="true",
        RELEASE_TAG="v0.9.0",
    )
    assert out.returncode == 0, out.stderr
    # the odd version is an annotation on the run page too: a constant text and the checked version
    warnings = [line for line in out.stderr.splitlines() if line.startswith("::warning::")]
    assert warnings == [
        "::warning::release version 0.9.0 is not the next one after the latest release tag: "
        "the tag was pushed by the owner and the run goes on (see the summary of the job check)"
    ]
    summary = (step_env.root / "summary.md").read_text()
    assert "## Release v0.9.0" in summary and "- previous tag: `v0.6.2`" in summary
    assert "**REFUSED** - version 0.9.0 is not the next one after the latest tag v0.6.2" in summary
    # a malformed tag fails even there
    bad = step_env.run(
        _step_script("check", CHECK_VERSION), GITHUB_EVENT_NAME="push", RELEASE_TAG="vnext"
    )
    assert bad.returncode == 1


def test_step_check_pushed_tag_in_order_has_no_warning(step_env: StepEnv) -> None:
    subprocess.run(["git", "tag", "v0.6.3"], cwd=step_env.repo, check=True)
    out = step_env.run(
        _step_script("check", CHECK_VERSION),
        GITHUB_EVENT_NAME="push",
        GITHUB_ACTIONS="true",
        RELEASE_TAG="v0.6.3",
    )
    assert out.returncode == 0, out.stderr
    assert "::warning::" not in out.stderr


def test_step_check_existing_version_is_refused_on_run_workflow(step_env: StepEnv) -> None:
    """Run workflow makes the tag itself: a version whose tag exists is refused with a clear text,
    not reported as "the next one after" the tag before it (the own tag is left out only for a
    pushed tag)."""
    subprocess.run(
        ["git", "tag", "v0.6.1"], cwd=step_env.repo, check=True
    )  # v0.6.0, v0.6.1, v0.6.2
    script = _step_script("check", CHECK_VERSION)
    for version in ("0.6.2", "0.6.1"):
        out = step_env.run(
            script,
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_ACTIONS="true",
            RELEASE_TAG=f"v{version}",
        )
        assert out.returncode == 1, (out.stdout, out.stderr)
        assert f"::error::the version {version} exists already (tag v{version})" in out.stderr
        assert "choose a greater version" in out.stderr
    summary = (step_env.root / "summary.md").read_text()
    assert "**REFUSED** - the version 0.6.2 exists already" in summary
    assert "the next one after v0.6.1" not in summary
    # the pushed-tag path still leaves its own tag out of the comparison
    pushed = step_env.run(script, GITHUB_EVENT_NAME="push", RELEASE_TAG="v0.6.2")
    assert pushed.returncode == 0, pushed.stderr


def test_step_check_after_a_pre_release_only_its_own_version(step_env: StepEnv) -> None:
    subprocess.run(["git", "tag", "v0.7.0rc1"], cwd=step_env.repo, check=True)
    script = _step_script("check", CHECK_VERSION)
    for version, ok in (("0.7.0", True), ("0.7.0rc2", True), ("0.7.1", False), ("0.8.0", False)):
        out = step_env.run(
            script,
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_ACTIONS="true",
            RELEASE_TAG=f"v{version}",
        )
        assert (out.returncode == 0) is ok, (version, out.stderr)


def test_step_tag_and_release_do_not_exist(step_env: StepEnv) -> None:
    script = _step_script("check", "tag and release do not exist yet")
    ok = step_env.run(script, RELEASE_TAG="v0.6.3")
    assert ok.returncode == 0, ok.stderr
    subprocess.run(["git", "tag", "v0.6.3"], cwd=step_env.repo, check=True)
    tag = step_env.run(script, RELEASE_TAG="v0.6.3")
    assert tag.returncode == 1 and "the tag v0.6.3 exists already" in tag.stdout
    (step_env.state / "releases").write_text("v0.7.0\n")
    release = step_env.run(script, RELEASE_TAG="v0.7.0")
    assert release.returncode == 1 and "a release v0.7.0 exists already" in release.stdout
    unknown = step_env.run(script, RELEASE_TAG="v0.7.1", FAKE_GH_RELEASE_ERROR="1")
    assert unknown.returncode == 1 and "could not tell whether the release exists" in unknown.stdout


def _tags_digest(step_env: StepEnv) -> str:
    out = step_env.run(_step_script("check", "the release tags seen by this check"))
    assert out.returncode == 0, out.stderr
    line = (step_env.root / "output.txt").read_text().splitlines()[-1]
    assert line.startswith("tags=") and len(line) == len("tags=") + 64
    return line[len("tags=") :]


def _publish(step_env: StepEnv, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "RELEASE_TAG": "v0.7.0",
        "VERSION": "0.7.0",
    }
    return step_env.run(
        _step_script("publish", "tag on the built commit, then gh release create"),
        **{**base, **env},
    )


def test_step_publish_makes_the_tag_then_the_release(step_env: StepEnv) -> None:
    digest = _tags_digest(step_env)
    out = _publish(step_env, TAGS_AT_CHECK=digest)
    assert out.returncode == 0, out.stderr
    calls = step_env.calls()
    post = next(i for i, c in enumerate(calls) if "--method POST" in c)
    create = next(i for i, c in enumerate(calls) if c.startswith("gh release create"))
    assert post < create  # the tag exists, and was read back, before any release
    assert "-f ref=refs/tags/v0.7.0 -f sha=" + SHA in calls[post]
    assert "--verify-tag" in calls[create] and "--prerelease" not in calls[create]
    assert "--target" not in calls[create]
    assert f"v0.7.0 {SHA}" in (step_env.state / "tags").read_text().splitlines()
    assert (step_env.state / "releases").read_text().split() == ["v0.7.0"]
    # a pre-release is marked (a later run: its check sees the tag the first one made)
    subprocess.run(["git", "tag", "v0.7.0"], cwd=step_env.repo, check=True)
    pre = _publish(
        step_env,
        TAGS_AT_CHECK=_tags_digest(step_env),
        RELEASE_TAG="v1.0.0rc1",
        VERSION="1.0.0rc1",
    )
    assert pre.returncode == 0, pre.stderr
    assert "--prerelease" in [c for c in step_env.calls() if c.startswith("gh release create")][-1]


def test_step_publish_refuses_when_the_tags_changed(step_env: StepEnv) -> None:
    digest = _tags_digest(step_env)
    with (step_env.state / "tags").open("a") as out:
        out.write(f"v0.6.3 {OTHER_SHA}\n")  # somebody tagged while the release ran
    out = _publish(step_env, TAGS_AT_CHECK=digest)
    assert out.returncode == 1 and "release tags changed" in out.stdout
    assert not [c for c in step_env.calls() if "POST" in c or "release create" in c]
    assert "v0.7.0" not in step_env.tags()
    # no digest from the check, a listing that fails: also nothing is published
    assert _publish(step_env, TAGS_AT_CHECK="").returncode == 1
    failing = _publish(step_env, TAGS_AT_CHECK=digest, FAKE_GH_LIST_ERROR="1")
    assert failing.returncode == 1
    assert not [c for c in step_env.calls() if "POST" in c or "release create" in c]


def test_step_publish_no_tag_is_not_an_error_but_a_failed_lookup_is(step_env: StepEnv) -> None:
    """The real gh prints the body of an HTTP error to stdout (rc 1), so "no tag" must come from a
    200 answer (matching-refs: []), and a failing lookup must stop the run, not look like a tag."""
    digest = _tags_digest(step_env)
    # the lookup fails with HTTP 502 (body in stdout, like the real gh): no tag, no release
    broken = _publish(step_env, TAGS_AT_CHECK=digest, FAKE_GH_REF_ERROR="1")
    assert broken.returncode != 0
    assert not [c for c in step_env.calls() if "POST" in c or "release create" in c]
    assert "v0.7.0" not in step_env.tags()
    # a tag of a longer name that starts with ours is not our tag
    with (step_env.state / "tags").open("a") as out:
        out.write(f"v0.7.0rc1 {OTHER_SHA}\n")
    subprocess.run(["git", "tag", "v0.7.0rc1"], cwd=step_env.repo, check=True)
    digest = _tags_digest(step_env)
    ok = _publish(step_env, TAGS_AT_CHECK=digest)
    assert ok.returncode == 0, ok.stderr
    assert f"v0.7.0 {SHA}" in (step_env.state / "tags").read_text().splitlines()


def test_fake_gh_behaves_like_the_real_one(step_env: StepEnv) -> None:
    """The stand-in is only as good as its likeness: these facts were seen on the real gh 2.89."""
    env = {
        **os.environ,
        "PATH": f"{step_env.bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DIR": str(step_env.state),
    }

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["gh", *args], env=env, capture_output=True, text=True, check=False)

    missing = gh("api", "repos/o/r/git/ref/tags/v9.9.9", "--jq", ".object.sha")
    assert missing.returncode == 1
    assert missing.stdout.startswith('{"message":"Not Found"')  # the body, --jq not applied
    assert missing.stderr.strip() == "gh: Not Found (HTTP 404)"
    none = gh("api", "--paginate", "repos/o/r/git/matching-refs/tags/v9.9.9", "--jq", ".[].ref")
    assert (none.returncode, none.stdout) == (0, "")
    prefix = gh("api", "--paginate", "repos/o/r/git/matching-refs/tags/v0.6", "--jq", ".[].ref")
    assert prefix.stdout.split() == ["refs/tags/v0.6.0", "refs/tags/v0.6.2"]


def test_step_publish_tag_elsewhere_is_never_released(step_env: StepEnv) -> None:
    digest = _tags_digest(step_env)
    with (step_env.state / "tags").open("a") as out:
        out.write(f"v0.7.0 {OTHER_SHA}\n")  # the tag exists on another commit
    out = _publish(step_env, TAGS_AT_CHECK=digest)
    assert out.returncode == 1 and "not to the built commit" in out.stdout
    # the ruleset is named in the message: backticks in a double-quoted echo would run it
    assert "release tags: no change" in out.stdout and "command not found" not in out.stderr
    assert not [c for c in step_env.calls() if "release create" in c or "POST" in c]
    assert (step_env.state / "releases").read_text() == ""


def test_step_publish_tag_created_on_another_commit_is_not_released(step_env: StepEnv) -> None:
    """The read-back is the proof: a tag that did not land on the built commit stops the run
    before a release exists (the workflow cannot delete a tag: the rulesets forbid it)."""
    digest = _tags_digest(step_env)
    out = _publish(step_env, TAGS_AT_CHECK=digest, FAKE_GH_TAG_AT=OTHER_SHA)
    assert out.returncode == 1 and "not to the built commit" in out.stdout
    assert not [c for c in step_env.calls() if "release create" in c]
    assert (step_env.state / "releases").read_text() == ""


def test_step_publish_rerun_after_the_tag_was_made(step_env: StepEnv) -> None:
    """A re-run of a publish that made the tag and failed before the release: the tag is already on
    the built commit, so the release is created for it (the tag set differs from the check's)."""
    digest = _tags_digest(step_env)
    with (step_env.state / "tags").open("a") as out:
        out.write(f"v0.7.0 {SHA}\n")
    out = _publish(step_env, TAGS_AT_CHECK=digest)
    assert out.returncode == 0, out.stderr
    assert not [c for c in step_env.calls() if "POST" in c]
    assert (step_env.state / "releases").read_text().split() == ["v0.7.0"]


def test_step_publish_pushed_tag_path(step_env: StepEnv) -> None:
    """A pushed tag: no tag is made, nothing about the tags is compared; the release uses it."""
    with (step_env.state / "tags").open("a") as out:
        out.write(f"v0.7.0 {SHA}\n")
    out2 = _publish(step_env, GITHUB_EVENT_NAME="push", TAGS_AT_CHECK="")
    assert out2.returncode == 0, out2.stderr
    assert [c for c in step_env.calls() if not c.startswith("gh release create")] == []
    # no tag at all: gh release create --verify-tag refuses
    (step_env.state / "tags").write_text("")
    missing = _publish(step_env, GITHUB_EVENT_NAME="push", RELEASE_TAG="v0.8.0", VERSION="0.8.0")
    assert missing.returncode != 0 and "does not exist" in missing.stderr


def test_deploy_readme_documents_release_keys() -> None:
    readme = (DEPLOY / "README_DEPLOY.md").read_text()
    for needle in (
        "--code-only",
        "minisign -G",
        "deploy/release-keys/",
        "MINISIGN_SECRET_KEY",
        "MINISIGN_PASSWORD",
        "Required reviewers",
        "mdhub release verify",
        "--allow-unsigned",
        "Rotating the key",
    ):
        assert needle in readme, needle
