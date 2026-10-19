"""Publish builds as immutable snapshots in ``ops.published_snapshot`` (migration 0510).

* The bundle is the canonical JSON of :meth:`Build.document`; ``id = sha256(bundle)``;
  ``payload_gz`` is its deterministic gzip. Every bundle is validated against the contract
  schema before it is stored (fail closed).
* A cut is ``(kind, cut_type, business_date, cut_ts)``. Publishing a build whose content
  (``content_sha256``: data plus static reference) equals the current (not superseded)
  snapshot of the same cut publishes nothing (``unchanged``); a different content publishes a
  correction that ``supersedes`` it. The old snapshot stays readable.
* :func:`ensure_static` reuses any static snapshot with the same content and
  ``known_at <= `` the build's (``reused``), else publishes the build.
* All of this runs under one transaction-level advisory lock, so concurrent jobs cannot
  publish two snapshots of one cut or two static snapshots with the same content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

from psycopg.types.json import Jsonb

from mdhub.db.pool import DictConnection
from mdhub.snapshot.build import Build
from mdhub.snapshot.canonical import canonical_bytes, gzip_bytes, sha256_hex
from mdhub.snapshot.schema import validate_bundle

__all__ = [
    "PUBLISH_LOCK",
    "PublishResult",
    "ensure_static",
    "load_bundle",
    "publish",
    "purge_intraday_snapshots",
]

log = logging.getLogger(__name__)

PUBLISH_LOCK: Final = "mdhub.snapshot.publish"

Status = Literal["published", "unchanged", "exists", "reused"]


@dataclass(frozen=True, slots=True)
class PublishResult:
    id: str
    status: Status
    kind: str
    cut_type: str
    business_date: date
    supersedes: str | None = None
    size_bytes: int | None = None
    gzip_bytes: int | None = None
    excluded: int | None = None


def _lock(conn: DictConnection) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (PUBLISH_LOCK,))


def _current_of_cut(conn: DictConnection, build: Build) -> dict[str, Any] | None:
    """The newest snapshot of the build's cut that nothing supersedes."""
    c = build.cut
    row: dict[str, Any] | None = conn.execute(
        """
        SELECT p.id, p.content_sha256 FROM ops.published_snapshot p
        WHERE p.kind = %(k)s AND p.cut_type = %(ct)s AND p.business_date = %(d)s
          AND p.cut_ts = %(ts)s
          AND NOT EXISTS (SELECT 1 FROM ops.published_snapshot n WHERE n.supersedes = p.id)
        ORDER BY p.published_at DESC, p.id
        LIMIT 1
        """,
        {"k": c.kind, "ct": c.cut_type, "d": c.business_date, "ts": c.cut_ts},
    ).fetchone()
    return row


def _insert(
    conn: DictConnection,
    build: Build,
    *,
    supersedes: str | None,
    content: str,
    run_id: int | None,
    validate: bool,
) -> PublishResult:
    if (build.cut.kind == "market") != (build.static_snapshot_id is not None):
        raise ValueError("a market snapshot needs a static_snapshot_id, a static one none")
    doc = build.document(supersedes)
    if validate:
        validate_bundle(doc)
    raw = canonical_bytes(doc)
    sid = sha256_hex(raw)
    c = build.cut
    exists = conn.execute("SELECT 1 FROM ops.published_snapshot WHERE id = %s", (sid,)).fetchone()
    if exists:
        return PublishResult(sid, "exists", c.kind, c.cut_type, c.business_date, supersedes)
    gz = gzip_bytes(raw)
    manifest = {k: v for k, v in doc.items() if k != "data"}
    manifest["snapshot_id"] = sid
    conn.execute(
        """
        INSERT INTO ops.published_snapshot (
            id, schema_version, kind, cut_type, business_date, cut_ts, known_at,
            static_snapshot_id, supersedes, content_sha256, calendar_fingerprint, sources,
            datasets, excluded, excluded_count, manifest, payload_gz, size_bytes, gzip_bytes,
            run_id)
        VALUES (%(id)s, %(sv)s, %(kind)s, %(ct)s, %(d)s, %(ts)s, %(ka)s, %(st)s, %(sup)s,
                %(content)s, %(cal)s, %(src)s, %(ds)s, %(ex)s, %(exn)s, %(man)s, %(gz)s,
                %(size)s, %(gzs)s, %(run)s)
        """,
        {
            "id": sid,
            "sv": doc["schema_version"],
            "kind": c.kind,
            "ct": c.cut_type,
            "d": c.business_date,
            "ts": c.cut_ts,
            "ka": c.known_at,
            "st": build.static_snapshot_id,
            "sup": supersedes,
            "content": content,
            "cal": doc["calendar"]["fingerprint"],
            "src": Jsonb(doc["sources"]),
            "ds": Jsonb(doc["datasets"]),
            "ex": Jsonb(doc["excluded"]),
            "exn": len(doc["excluded"]),
            "man": Jsonb(manifest),
            "gz": gz,
            "size": len(raw),
            "gzs": len(gz),
            "run": run_id,
        },
    )
    log.info(
        "published %s snapshot %s (%s %s, %d bytes, gzip %d)%s",
        c.kind,
        sid,
        c.cut_type,
        c.business_date,
        len(raw),
        len(gz),
        f", supersedes {supersedes}" if supersedes else "",
    )
    return PublishResult(
        sid,
        "published",
        c.kind,
        c.cut_type,
        c.business_date,
        supersedes,
        len(raw),
        len(gz),
        len(doc["excluded"]),
    )


def publish(
    conn: DictConnection, build: Build, *, run_id: int | None = None, validate: bool = True
) -> PublishResult:
    """Publish ``build`` for its cut (``unchanged`` / correction rules in the module doc).

    Runs in the caller's transaction (the caller commits)."""
    _lock(conn)
    content = build.content_sha256()
    current = _current_of_cut(conn, build)
    c = build.cut
    if current is not None and current["content_sha256"] == content:
        return PublishResult(current["id"], "unchanged", c.kind, c.cut_type, c.business_date)
    supersedes = current["id"] if current is not None else None
    return _insert(
        conn, build, supersedes=supersedes, content=content, run_id=run_id, validate=validate
    )


def ensure_static(
    conn: DictConnection, build: Build, *, run_id: int | None = None, validate: bool = True
) -> PublishResult:
    """A static snapshot with ``build``'s content: an existing one known at or before the
    build (``reused``), else ``build`` published (``cut_type`` as built, e.g. ``change``)."""
    if build.cut.kind != "static":
        raise ValueError("ensure_static needs a static build")
    _lock(conn)
    content = build.content_sha256()
    row = conn.execute(
        """
        SELECT id FROM ops.published_snapshot
        WHERE kind = 'static' AND content_sha256 = %(c)s AND known_at <= %(t)s
        ORDER BY known_at DESC, published_at DESC, id LIMIT 1
        """,
        {"c": content, "t": build.cut.known_at},
    ).fetchone()
    c = build.cut
    if row is not None:
        return PublishResult(row["id"], "reused", c.kind, c.cut_type, c.business_date)
    current = _current_of_cut(conn, build)
    supersedes = current["id"] if current is not None else None
    return _insert(
        conn, build, supersedes=supersedes, content=content, run_id=run_id, validate=validate
    )


def load_bundle(conn: DictConnection, snapshot_id: str) -> bytes | None:
    """The gzipped bundle of ``snapshot_id`` (``None`` if unknown)."""
    row = conn.execute(
        "SELECT payload_gz FROM ops.published_snapshot WHERE id = %s", (snapshot_id,)
    ).fetchone()
    return bytes(row["payload_gz"]) if row else None


def purge_intraday_snapshots(conn: DictConnection, days: int, *, now: datetime) -> int:
    """Delete intraday market snapshots whose cut is older than ``days`` (retention rule;
    static and EOD snapshots are kept forever)."""
    cur = conn.execute(
        "DELETE FROM ops.published_snapshot "
        "WHERE kind = 'market' AND cut_type = 'intraday' AND cut_ts < %(cutoff)s",
        {"cutoff": now - timedelta(days=days)},
    )
    return max(cur.rowcount, 0)
