"""Release commands: ``mdhub release verify | keys | manifest`` (ADMIN_DESIGN §6.1).

* ``verify ARCHIVE [--signature FILE] [--keys-dir DIR] [--allow-unsigned] [--json]`` - check a
  code-only release against the pinned keys (:func:`mdhub.release.verify_archive`). Exit 0 when
  it verifies, 1 when it is refused, 2 on a usage or key-list error. ``--allow-unsigned`` is the
  only override and exists on the CLI only: it accepts a missing signature or an unknown key,
  never a pinned key's signature that fails, and still checks every hash and the manifest.
* ``keys [--keys-dir DIR]`` - list the pinned public keys.
* ``manifest --root DIR --commit SHA [--version V] [--out FILE]`` - write ``MANIFEST.json`` for an
  unpacked ``git archive`` (used by ``deploy/make_bundle.sh --code-only``); prints the version.
  The version defaults to the installed package's (``importlib.metadata``).

The key directory defaults to ``$MDHUB_RELEASE_KEYS_DIR``, else ``deploy/release-keys`` of the
source checkout. No database or ``.env`` is needed.
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from typing import Annotated

import typer

from mdhub.release import (
    PROJECT_NAME,
    PublicKey,
    ReleaseError,
    build_manifest,
    default_keys_dir,
    load_pinned_keys,
    verify_archive,
)

name = "release"
app = typer.Typer(help="Release bundles: verify, pinned keys, manifest.", no_args_is_help=True)

KeysDir = Annotated[
    Path | None,
    typer.Option(
        "--keys-dir",
        help="Pinned public keys (*.pub). Default: $MDHUB_RELEASE_KEYS_DIR or deploy/release-keys.",
    ),
]


def _keys(keys_dir: Path | None) -> dict[bytes, PublicKey]:
    try:
        return load_pinned_keys(keys_dir or default_keys_dir())
    except ReleaseError as exc:
        typer.echo(f"release keys: {exc}", err=True)
        raise typer.Exit(2) from None


@app.command("verify")
def verify_cmd(
    archive: Annotated[Path, typer.Argument(help="market-data-hub-X.Y.Z.tar.gz")],
    signature: Annotated[
        Path | None,
        typer.Option(
            "--signature",
            "-s",
            help="Signature over SHA256SUMS (default: <archive>.SHA256SUMS.minisig alongside).",
        ),
    ] = None,
    keys_dir: KeysDir = None,
    allow_unsigned: Annotated[
        bool,
        typer.Option(
            "--allow-unsigned",
            help="Accept a missing signature or an unknown key (hashes, manifest still checked).",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Verify a release archive: signature by a pinned key, every file hash, the manifest."""
    keys = _keys(keys_dir)
    if not archive.is_file():
        typer.echo(f"{archive}: not a file", err=True)
        raise typer.Exit(2)
    try:
        result = verify_archive(archive, keys, signature=signature, allow_unsigned=allow_unsigned)
    except ReleaseError as exc:
        if as_json:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}))
        typer.echo(f"REFUSED {archive.name}: {exc}", err=True)
        raise typer.Exit(1) from None
    for warning in result.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    m = result.manifest
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": True,
                    "signed_by": result.key.key_id_hex if result.key else None,
                    "trusted_comment": result.trusted_comment,
                    "files": result.files,
                    "bytes": result.total_bytes,
                    "warnings": list(result.warnings),
                    "manifest": m.to_json(),
                },
                indent=2,
            )
        )
        return
    signer = (
        f"signed by pinned key {result.key.key_id_hex} ({result.key.source})"
        if result.key
        else "NOT signed by a pinned key"
    )
    typer.echo(f"OK {PROJECT_NAME} {m.version} (commit {m.commit[:12]}): {signer}")
    if result.trusted_comment is not None:
        typer.echo(f"   trusted comment: {result.trusted_comment}")
    typer.echo(
        f"   {result.files} files, {result.total_bytes} bytes; {len(m.migrations)} migrations"
        f" (last {m.migrations[-1][0] if m.migrations else 'none'});"
        f" min_previous_version {m.min_previous_version}; built {m.created_at}"
    )


@app.command("keys")
def keys_cmd(keys_dir: KeysDir = None) -> None:
    """List the pinned release public keys."""
    keys = _keys(keys_dir)
    if not keys:
        typer.echo(f"no pinned release keys in {keys_dir or default_keys_dir()}")
        return
    for key in keys.values():
        typer.echo(f"{key.key_id_hex}  {key.source}")


@app.command("manifest")
def manifest_cmd(
    root: Annotated[Path, typer.Option("--root", help="Unpacked git archive of the release.")],
    commit: Annotated[str, typer.Option("--commit", help="Full git commit SHA.")],
    version: Annotated[
        str | None,
        typer.Option("--version", help="Release version (default: installed package version)."),
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output file (default: ROOT/MANIFEST.json).")
    ] = None,
) -> None:
    """Write MANIFEST.json for a release tree and print its version."""
    if version is None:
        try:
            version = importlib.metadata.version(PROJECT_NAME)
        except importlib.metadata.PackageNotFoundError:
            typer.echo(f"{PROJECT_NAME} is not installed: pass --version", err=True)
            raise typer.Exit(2) from None
    try:
        manifest = build_manifest(root, version=version, commit=commit)
    except ReleaseError as exc:
        typer.echo(f"manifest: {exc}", err=True)
        raise typer.Exit(2) from None
    (out or root / "MANIFEST.json").write_text(manifest.dumps(), encoding="utf-8")
    typer.echo(manifest.version)
