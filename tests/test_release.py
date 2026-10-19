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
    build_manifest,
    default_keys_dir,
    default_signature_path,
    load_pinned_keys,
    parse_manifest,
    parse_public_key,
    parse_signature,
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
    assert wf[True] == {"push": {"tags": ["v*"]}}
    assert wf["permissions"] == {}
    jobs = wf["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
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
