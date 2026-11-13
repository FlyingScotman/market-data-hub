"""Expose only committed, retained FX input groups through the market snapshot."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from mdhub.core.timeutil import MSK
from mdhub.db.pool import DictConnection
from mdhub.snapshot.build import Build, Cut, Dataset, Provenance
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex, to_wire


def _selection(conn: DictConnection, cut: Cut) -> dict[str, Any] | None:
    record = conn.execute(
        """SELECT document, content_sha256 FROM md.fx_input_pair_cut_selection
           WHERE business_date=%s AND cut_type=%s AND cut_ts=%s AND known_at=%s""",
        (cut.business_date, cut.cut_type, cut.cut_ts, cut.known_at),
    ).fetchone()
    if record is None:
        return None
    document: dict[str, Any] = record["document"]
    if sha256_hex(canonical_bytes(document)) != record["content_sha256"]:
        raise ValueError("fx_input_pairs cut selection hash mismatch")
    return document


def _restore(document: dict[str, Any], prov: Provenance) -> Dataset:
    prov.discard_dataset("fx_input_pairs")
    for source in document["sources"]:
        for run_id in source["run_ids"]:
            prov.add("fx_input_pairs", source["source"], run_id, None)
        for name in ("ingested_from", "ingested_to"):
            if source[name] is not None:
                at = datetime.fromisoformat(source[name].replace("Z", "+00:00"))
                prov.add("fx_input_pairs", source["source"], None, at)
        if not source["run_ids"] and source["ingested_from"] is None:
            prov.add("fx_input_pairs", source["source"], None, None)
    return Dataset(
        "fx_input_pairs", document["rows"], document["coverage"], document["stale_reasons"]
    )


def pin_for_publication(conn: DictConnection, build: Build) -> None:
    """Freeze the first decision under the caller's publish lock and transaction."""
    datasets = [dataset for dataset in build.datasets if dataset.name == "fx_input_pairs"]
    if not datasets:
        return
    dataset = datasets[0]
    document = {
        "rows": dataset.wire_rows(),
        "coverage": to_wire(dataset.coverage),
        "stale_reasons": sorted(set(dataset.stale_reasons)),
        "sources": [s for s in build.provenance.wire() if s["dataset"] == dataset.name],
    }
    c = build.cut
    conn.execute(
        """INSERT INTO md.fx_input_pair_cut_selection
           (business_date, cut_type, cut_ts, known_at, document, content_sha256)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (business_date, cut_type, cut_ts, known_at) DO NOTHING""",
        (
            c.business_date,
            c.cut_type,
            c.cut_ts,
            c.known_at,
            Jsonb(document),
            sha256_hex(canonical_bytes(document)),
        ),
    )
    pinned = _selection(conn, c)
    assert pinned is not None
    build.datasets[build.datasets.index(dataset)] = _restore(pinned, build.provenance)


def fx_input_pairs(conn: DictConnection, cut: Cut, prov: Provenance) -> Dataset:
    """Select a certified latest attempt, or restore the immutable publication decision."""
    pinned = _selection(conn, cut)
    if pinned is not None:
        return _restore(pinned, prov)
    start = datetime.combine(cut.business_date, time.min, tzinfo=MSK)
    end = start + timedelta(days=1)
    boundary = min(cut.cut_ts, cut.known_at)
    run = conn.execute(
        """SELECT id, status, finished_at FROM ops.job_run
           WHERE job = 'fx_input_pairs' AND started_at >= %s AND started_at < %s
             AND started_at <= %s
           ORDER BY started_at DESC, id DESC LIMIT 1""",
        (start, end, boundary),
    ).fetchone()
    if not run:
        return Dataset(
            "fx_input_pairs", [], {"groups": 0, "complete_four": 0}, ["no_completed_capture"]
        )
    if run["finished_at"] is None or run["finished_at"] > boundary:
        return Dataset(
            "fx_input_pairs",
            [],
            {"groups": 0, "complete_four": 0},
            ["latest_attempt_unconfirmed"],
        )
    evidence = conn.execute(
        """SELECT 1 FROM md.fx_input_pair_completion_evidence
           WHERE run_id=%s AND status=%s AND finished_at=%s AND observed_at <= %s""",
        (run["id"], run["status"], run["finished_at"], boundary),
    ).fetchone()
    if evidence is None:
        return Dataset(
            "fx_input_pairs",
            [],
            {"groups": 0, "complete_four": 0},
            ["completion_visibility_unknown"],
        )
    if run["status"] not in ("succeeded", "partial"):
        return Dataset(
            "fx_input_pairs",
            [],
            {"groups": 0, "complete_four": 0},
            [f"latest_capture_{run['status']}"],
        )
    records = conn.execute(
        """SELECT currency, document, content_sha256, ingested_at FROM md.fx_input_pair_group
           WHERE run_id = %s AND known_at <= %s AND ingested_at <= %s ORDER BY currency""",
        (run["id"], boundary, boundary),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for record in records:
        document = record["document"]
        if sha256_hex(canonical_bytes(document)) != record["content_sha256"]:
            errors.append("group_hash_mismatch")
            continue
        row = dict(document)
        row["content_sha256"] = record["content_sha256"]
        quote_ts = row["spot"]["quote_ts"]
        if quote_ts is None:
            row["spot_age_seconds_at_cut"] = None
        else:
            delta = cut.cut_ts - datetime.fromisoformat(quote_ts.replace("Z", "+00:00"))
            row["spot_age_seconds_at_cut"] = Decimal(delta.days * 86400 + delta.seconds) + Decimal(
                delta.microseconds
            ) / Decimal(1_000_000)
        rows.append(row)
        prov.add("fx_input_pairs", row["spot"]["source"], run["id"], record["ingested_at"])
    if len(records) != 3:
        errors.append("group_missing")
    return Dataset(
        "fx_input_pairs",
        rows,
        {
            "groups": len(rows),
            "complete_four": sum(row["coverage"]["all_four_observed"] for row in rows),
        },
        errors,
    )
