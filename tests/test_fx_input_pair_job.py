"""Point-in-time integration tests use only invented prices and source payloads."""

from __future__ import annotations

import gzip
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core import capture as capture_module
from mdhub.core.capture import job_run, record_capture
from mdhub.core.http import HttpClient
from mdhub.core.timeutil import MSK
from mdhub.db.migrate import MIGRATIONS_DIR, migrate
from mdhub.db.pool import Database, DictConnection
from mdhub.jobs import run_job
from mdhub.loaders import fx_input_pairs as loader
from mdhub.snapshot.build import Build, Cut, Provenance
from mdhub.snapshot.canonical import canonical_bytes, sha256_hex
from mdhub.snapshot.fx_input_pairs import fx_input_pairs
from mdhub.snapshot.market import build_market
from mdhub.snapshot.publish import publish, purge_intraday_snapshots
from mdhub.snapshot.runner import publish_market_snapshot, publish_static_snapshot
from mdhub.snapshot.schema import validate_bundle
from mdhub.snapshot.static import build_static
from mdhub.sources import fx_pair_inputs as inputs
from test_fx_pair_inputs import catalog, cny_payload, futures_payload

SPOT = datetime(2026, 1, 5, 8, 0, 3, tzinfo=UTC)


def seed_contracts(conn: DictConnection) -> None:
    for row in catalog():
        conn.execute(
            """INSERT INTO ref.futures_contract
               (secid, asset_code, is_fx, base_ccy, quote_ccy, contract_type,
                first_trade_date, last_trade_date, last_del_date, spec_source,
                first_seen, ingested_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'rfud', %s, %s)""",
            (
                row["secid"],
                row["asset_code"],
                row["is_fx"],
                row["base_ccy"],
                row["quote_ccy"],
                row["contract_type"],
                row["first_trade_date"],
                row["last_trade_date"],
                row["last_del_date"],
                row["first_trade_date"],
                SPOT - timedelta(days=1),
            ),
        )


def capture_futures(conn: DictConnection, when: datetime) -> int:
    payload = json.dumps(futures_payload(catalog())).encode()
    return record_capture(
        conn,
        None,
        "moex_apim",
        loader.FUTURES_ENDPOINT,
        "https://example.invalid/futures",
        payload,
        "1",
        fetched_at=when,
    )


def capture_cny(conn: DictConnection, when: datetime) -> None:
    payload = json.dumps(cny_payload(), default=str).encode()
    record_capture(
        conn,
        None,
        "moex_iss",
        loader.CNY_ENDPOINT,
        "https://example.invalid/spot",
        payload,
        "1",
        fetched_at=when,
    )


def factory(**kwargs: Any) -> HttpClient:
    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://ntprog.ru/local_api/public/east/"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": [
                    {
                        "name": "USD/RUB_TOM",
                        "bid": "10.00",
                        "offer": "10.02",
                        "creationDateTime": SPOT.isoformat(),
                    },
                    {
                        "name": "EUR/RUB_TOM",
                        "bid": "11.00",
                        "offer": "11.10",
                        "creationDateTime": SPOT.isoformat(),
                    },
                    {
                        "name": "CNH/RUB_TOM",
                        "bid": "1",
                        "offer": "2",
                        "creationDateTime": SPOT.isoformat(),
                    },
                ],
            },
            request=request,
        )

    return HttpClient(transport=httpx.MockTransport(answer), **kwargs)


def test_job_retains_same_group_after_raw_expiry_and_ref_revision(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    capture_cny(conn, datetime.now(UTC) - timedelta(seconds=1))
    conn.commit()
    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    assert outcome.status == "partial"  # EUR/CNY have no matching synthetic futures.
    records = conn.execute("SELECT * FROM md.fx_input_pair_group ORDER BY currency").fetchall()
    assert len(records) == 3
    usd = next(row for row in records if row["currency"] == "USD")
    group = usd["document"]
    assert group["known_at"] == records[0]["document"]["known_at"]
    assert group["spot"]["settle_date"] is None
    assert "spot_value_date_unknown" in group["spot"]["reasons"]
    assert Decimal(group["nodes"][0]["bid_rub_per_unit"]) == Decimal("10.1")
    assert group["coverage"]["observed_nodes"] == 4
    assert sha256_hex(canonical_bytes(group)) == usd["content_sha256"]
    conn.execute("DELETE FROM ops.raw_capture")
    conn.execute(
        "UPDATE ref.futures_contract SET last_trade_date = '2026-03-20' WHERE secid = 'SiH6'"
    )
    conn.commit()
    after = conn.execute(
        "SELECT document, content_sha256 FROM md.fx_input_pair_group "
        "WHERE run_id = %s AND currency = 'USD'",
        (outcome.run_id,),
    ).fetchone()
    assert after == {"document": group, "content_sha256": usd["content_sha256"]}
    now = datetime.now(UTC) + timedelta(seconds=1)
    cut = Cut("market", "intraday", now.astimezone(MSK).date(), now, now)
    ds = fx_input_pairs(conn, cut, Provenance())
    assert len(ds.rows) == 3
    assert (
        next(row for row in ds.rows if row["currency"] == "USD")["content_sha256"]
        == usd["content_sha256"]
    )
    conn.commit()
    later = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    assert later.run_id != outcome.run_id
    new = conn.execute(
        "SELECT document FROM md.fx_input_pair_group WHERE run_id = %s AND currency = 'USD'",
        (later.run_id,),
    ).fetchone()
    assert all(node["reasons"] == ["future_capture_missing"] for node in new["document"]["nodes"])
    old = conn.execute(
        "SELECT document FROM md.fx_input_pair_group WHERE run_id = %s AND currency = 'USD'",
        (outcome.run_id,),
    ).fetchone()
    assert old["document"] == group


def test_late_commit_is_not_visible_in_the_materialization_snapshot(
    conn: DictConnection, db: Database
) -> None:
    seed_contracts(conn)
    conn.commit()
    capture_futures(conn, SPOT - timedelta(seconds=1))  # intentionally uncommitted
    spot = inputs.Spot(
        "USD",
        "ntpro_public",
        "USD/RUB_TOM",
        "indicative_bid_offer",
        quote_ts=SPOT,
        bid=Decimal("10"),
        offer=Decimal("10.1"),
    )
    spot_capture = {"fetched_at": SPOT + timedelta(seconds=1), "sha256": "f" * 64}
    with db.connection() as reader:
        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        known_at = reader.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        before = loader._group(reader, 42, known_at, spot, spot_capture)
        conn.commit()
        after = loader._group(reader, 42, known_at, spot, spot_capture)
        assert before == after
        assert before["nodes"][0]["reasons"] == ["future_capture_missing"]
    with db.connection() as reader:
        visible = loader._group(reader, 43, datetime.now(UTC), spot, spot_capture)
        assert visible["nodes"][0]["bid_rub_per_unit"] == Decimal("10.1")


def test_late_completion_commit_does_not_change_published_fixed_cut(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    """A completion stamp inside an uncommitted transaction is not visibility at the cut."""
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.commit()
    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    conn.commit()
    conn.execute(
        "UPDATE ops.job_run SET status='running', finished_at=NULL WHERE id=%s",
        (outcome.run_id,),
    )
    conn.commit()
    stamped = conn.execute(
        "UPDATE ops.job_run SET status='partial', finished_at=clock_timestamp() "
        "WHERE id=%s RETURNING finished_at",
        (outcome.run_id,),
    ).fetchone()["finished_at"]
    with db.connection() as reader:
        boundary = reader.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        assert boundary >= stamped
        cut = Cut("market", "intraday", boundary.astimezone(MSK).date(), boundary, boundary)
        original = fx_input_pairs(reader, cut, Provenance())
        assert original.rows == []
    first = publish_market_snapshot(
        db,
        cut_type="intraday",
        business_date=cut.business_date,
        cut_ts=cut.cut_ts,
        known_at=cut.known_at,
    )
    conn.commit()
    capture_module._observe_fx_completion(db, outcome.run_id)
    with db.connection() as reader:
        replay = fx_input_pairs(reader, cut, Provenance())
    rebuilt = publish_market_snapshot(
        db,
        cut_type="intraday",
        business_date=cut.business_date,
        cut_ts=cut.cut_ts,
        known_at=cut.known_at,
    )
    assert replay.rows == original.rows
    assert rebuilt.market.id == first.market.id


def test_first_historical_publish_rejects_completion_first_visible_after_cut(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.commit()
    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    conn.commit()
    conn.execute(
        "UPDATE ops.job_run SET status='running', finished_at=NULL WHERE id=%s", (outcome.run_id,)
    )
    conn.commit()
    stamped = conn.execute(
        "UPDATE ops.job_run SET status='partial', finished_at=clock_timestamp() "
        "WHERE id=%s RETURNING finished_at",
        (outcome.run_id,),
    ).fetchone()["finished_at"]
    with db.connection() as reader:
        boundary = reader.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        assert boundary >= stamped
    conn.commit()
    capture_module._observe_fx_completion(db, outcome.run_id)
    cut = Cut("market", "intraday", boundary.astimezone(MSK).date(), boundary, boundary)
    with db.connection() as reader:
        candidate = fx_input_pairs(reader, cut, Provenance())
    assert candidate.rows == []
    assert "completion_visibility_unknown" in candidate.stale_reasons
    result = publish_market_snapshot(
        db,
        cut_type="intraday",
        business_date=cut.business_date,
        cut_ts=cut.cut_ts,
        known_at=cut.known_at,
    )
    with db.connection() as reader:
        row = reader.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (result.market.id,)
        ).fetchone()
    document = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    assert document["data"]["fx_input_pairs"] == []


def _market_build(conn: DictConnection, cut: Cut, static_id: str) -> Build:
    static_cut = Cut("static", "change", cut.business_date, cut.cut_ts, cut.known_at)
    static = build_static(conn, cut=static_cut)
    return build_market(conn, cut=cut, static=static, static_snapshot_id=static_id)


def test_late_evidence_concurrent_publications_retention_and_distinct_known_at(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.commit()
    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    conn.execute(
        "DELETE FROM md.fx_input_pair_completion_evidence WHERE run_id=%s", (outcome.run_id,)
    )
    conn.commit()
    # This transaction has seen the committed completion, but its evidence is not committed.
    conn.execute(
        """INSERT INTO md.fx_input_pair_completion_evidence
           SELECT id, status, finished_at, clock_timestamp() FROM ops.job_run WHERE id=%s""",
        (outcome.run_id,),
    )
    with db.connection() as reader:
        boundary = reader.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    cut = Cut("market", "intraday", boundary.astimezone(MSK).date(), boundary, boundary)
    static_id = publish_static_snapshot(
        db, business_date=cut.business_date, cut_ts=boundary, known_at=boundary, cut_type="change"
    ).id
    with db.connection() as reader:
        first_build = _market_build(reader, cut, static_id)
    assert first_build.dataset("fx_input_pairs").stale_reasons == ["completion_visibility_unknown"]
    conn.commit()  # Evidence becomes visible only after the cut.
    with db.connection() as reader:
        second_build = _market_build(reader, cut, static_id)
    assert len(second_build.dataset("fx_input_pairs").rows) == 3

    # Both provisional builds are real; the second publisher waits on the actual advisory lock.
    first_locked, release_first, second_started = Event(), Event(), Event()
    second_pid: list[int] = []

    def first_publish() -> Any:
        with db.connection() as writer:
            result = publish(writer, first_build)
            first_locked.set()
            assert release_first.wait(10)
        return result

    def second_publish() -> Any:
        assert first_locked.wait(10)
        with db.connection() as writer:
            second_pid.append(writer.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            second_started.set()
            return publish(writer, second_build)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first_publish)
        assert first_locked.wait(10)
        two = pool.submit(second_publish)
        assert second_started.wait(10)
        deadline = time.monotonic() + 5
        waiting = None
        while time.monotonic() < deadline:
            with db.connection() as reader:
                waiting = reader.execute(
                    "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid=%s",
                    (second_pid[0],),
                ).fetchone()
            if (
                waiting
                and waiting["wait_event_type"] == "Lock"
                and waiting["wait_event"] == "advisory"
            ):
                break
            Event().wait(0.01)
        assert (
            waiting and waiting["wait_event_type"] == "Lock" and waiting["wait_event"] == "advisory"
        )
        release_first.set()
        first, second = one.result(timeout=10), two.result(timeout=10)
    assert first.id == second.id
    assert second.status == "unchanged"
    with db.connection() as reader:
        saved = reader.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (first.id,)
        ).fetchone()
    assert json.loads(gzip.decompress(bytes(saved["payload_gz"])))["data"]["fx_input_pairs"] == []
    # Normal retention deletes the snapshot, but not the selection, and rebuilding remains fixed.
    with db.connection() as writer:
        assert purge_intraday_snapshots(writer, 0, now=boundary + timedelta(days=1)) == 1
    rebuilt = publish_market_snapshot(
        db,
        cut_type=cut.cut_type,
        business_date=cut.business_date,
        cut_ts=cut.cut_ts,
        known_at=cut.known_at,
    )
    with db.connection() as reader:
        saved = reader.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (rebuilt.market.id,)
        ).fetchone()
    assert json.loads(gzip.decompress(bytes(saved["payload_gz"])))["data"]["fx_input_pairs"] == []

    # A genuinely different knowledge clock has its own decision, even with the same cut time.
    newer = publish_market_snapshot(
        db,
        cut_type=cut.cut_type,
        business_date=cut.business_date,
        cut_ts=cut.cut_ts,
        known_at=cut.known_at + timedelta(microseconds=1),
    )
    with db.connection() as reader:
        saved = reader.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (newer.market.id,)
        ).fetchone()
        pins = reader.execute("SELECT count(*) AS n FROM md.fx_input_pair_cut_selection").fetchone()
    assert (
        len(json.loads(gzip.decompress(bytes(saved["payload_gz"])))["data"]["fx_input_pairs"]) == 3
    )
    assert pins["n"] == 2


def test_failed_publication_rolls_back_selection(conn: DictConnection, db: Database) -> None:
    now = datetime.now(UTC)
    cut = Cut("market", "intraday", now.astimezone(MSK).date(), now, now)
    static_id = publish_static_snapshot(
        db, business_date=cut.business_date, cut_ts=now, known_at=now, cut_type="change"
    ).id
    with db.connection() as reader:
        build = _market_build(reader, cut, static_id)
    build.static_snapshot_id = None  # The real publish rejects this after attempting the pin.
    with pytest.raises(ValueError, match="static_snapshot_id"), db.connection() as writer:
        publish(writer, build)
    conn.rollback()
    assert (
        conn.execute("SELECT count(*) AS n FROM md.fx_input_pair_cut_selection").fetchone()["n"]
        == 0
    )
    build.static_snapshot_id = static_id
    with db.connection() as writer:
        assert publish(writer, build).status == "published"


def test_distinct_known_at_keeps_different_empty_quality_reasons(
    db: Database,
) -> None:
    first_known = datetime.now(UTC)
    cut_ts = first_known + timedelta(minutes=1)
    day = cut_ts.astimezone(MSK).date()
    first = publish_market_snapshot(
        db, cut_type="intraday", business_date=day, cut_ts=cut_ts, known_at=first_known
    )
    with pytest.raises(RuntimeError, match="failure"), job_run(db, "fx_input_pairs"):
        raise RuntimeError("failure")
    later_known = datetime.now(UTC)
    second = publish_market_snapshot(
        db, cut_type="intraday", business_date=day, cut_ts=cut_ts, known_at=later_known
    )
    assert second.market.id != first.market.id
    with db.connection() as reader:
        row = reader.execute(
            "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (second.market.id,)
        ).fetchone()
    document = json.loads(gzip.decompress(bytes(row["payload_gz"])))
    fx_entry = next(entry for entry in document["datasets"] if entry["name"] == "fx_input_pairs")
    assert fx_entry["stale_reasons"] == ["latest_capture_failed"]


def test_published_groups_and_provenance_survive_snapshot_and_raw_retention(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.commit()
    run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    now = datetime.now(UTC) + timedelta(seconds=1)
    args = {
        "cut_type": "intraday",
        "business_date": now.astimezone(MSK).date(),
        "cut_ts": now,
        "known_at": now,
    }
    first = publish_market_snapshot(db, **args)

    def document(snapshot_id: str) -> dict[str, Any]:
        with db.connection() as reader:
            row = reader.execute(
                "SELECT payload_gz FROM ops.published_snapshot WHERE id=%s", (snapshot_id,)
            ).fetchone()
        return json.loads(gzip.decompress(bytes(row["payload_gz"])))

    before = document(first.market.id)
    assert len(before["data"]["fx_input_pairs"]) == 3
    before_sources = [s for s in before["sources"] if s["dataset"] == "fx_input_pairs"]
    assert before_sources
    with db.connection() as writer:
        writer.execute("DELETE FROM ops.raw_capture")
        assert purge_intraday_snapshots(writer, 0, now=now + timedelta(days=1)) == 1
    rebuilt = document(publish_market_snapshot(db, **args).market.id)
    assert rebuilt["data"]["fx_input_pairs"] == before["data"]["fx_input_pairs"]
    assert [s for s in rebuilt["sources"] if s["dataset"] == "fx_input_pairs"] == before_sources


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "skipped", "running"])
def test_latest_attempt_status_is_not_hidden_by_older_success(
    conn: DictConnection, db: Database, settings: Settings, status: str
) -> None:
    older = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)

    def candidate() -> Any:
        now = datetime.now(UTC) + timedelta(seconds=1)
        cut = Cut("market", "intraday", now.astimezone(MSK).date(), now, now)
        with db.connection() as reader:
            return fx_input_pairs(reader, cut, Provenance())

    if status == "failed":
        with pytest.raises(RuntimeError, match="failure"), job_run(db, "fx_input_pairs"):
            raise RuntimeError("failure")
    else:
        with job_run(db, "fx_input_pairs") as run:
            if status == "running":
                running = candidate()
                assert running.rows == []
                assert running.stale_reasons == ["latest_attempt_unconfirmed"]
            if status in ("succeeded", "partial"):
                with db.connection() as writer:
                    writer.execute(
                        """INSERT INTO md.fx_input_pair_group
                           (run_id, currency, known_at, document, content_sha256, ingested_at)
                           SELECT %s, currency, known_at, document, content_sha256, ingested_at
                           FROM md.fx_input_pair_group WHERE run_id=%s""",
                        (run.id, older.run_id),
                    )
            if status == "partial":
                run.mark_partial("test partial")
            if status == "skipped":
                run.mark_skipped("test skipped")
    if status == "running":
        return
    selected = candidate()
    if status in ("succeeded", "partial"):
        assert len(selected.rows) == 3
    else:
        assert selected.rows == []
        assert selected.stale_reasons == [f"latest_capture_{status}"]


@pytest.mark.parametrize("tamper", ["missing", "status", "finished_at"])
def test_missing_or_mismatched_evidence_does_not_use_older_group(
    conn: DictConnection, db: Database, settings: Settings, tamper: str
) -> None:
    run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    newest = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    if tamper == "missing":
        conn.execute(
            "DELETE FROM md.fx_input_pair_completion_evidence WHERE run_id=%s", (newest.run_id,)
        )
    elif tamper == "status":
        conn.execute(
            "UPDATE md.fx_input_pair_completion_evidence SET status='succeeded' WHERE run_id=%s",
            (newest.run_id,),
        )
    else:
        conn.execute(
            "UPDATE md.fx_input_pair_completion_evidence "
            "SET finished_at=finished_at - interval '1 second' WHERE run_id=%s",
            (newest.run_id,),
        )
    conn.commit()
    now = datetime.now(UTC) + timedelta(seconds=1)
    cut = Cut("market", "intraday", now.astimezone(MSK).date(), now, now)
    selected = fx_input_pairs(conn, cut, Provenance())
    assert selected.rows == []
    assert selected.stale_reasons == ["completion_visibility_unknown"]


def test_evidence_error_preserves_terminal_status_and_original_error(
    conn: DictConnection,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []

    def fail(_db: Database, run_id: int) -> None:
        calls.append(run_id)
        raise RuntimeError("synthetic evidence failure")

    monkeypatch.setattr(capture_module, "_observe_fx_completion", fail)
    with job_run(db, "fx_input_pairs") as successful:
        pass
    with (
        pytest.raises(ValueError, match="original job error"),
        job_run(db, "fx_input_pairs") as failed,
    ):
        raise ValueError("original job error")
    with job_run(db, "another_job"):
        pass
    rows = conn.execute(
        "SELECT id,status FROM ops.job_run WHERE id IN (%s,%s) ORDER BY id",
        (successful.id, failed.id),
    ).fetchall()
    assert [row["status"] for row in rows] == ["succeeded", "failed"]
    assert calls == [successful.id, failed.id]
    assert caplog.text.count("completion evidence failed (RuntimeError)") == 2


def test_late_contract_definition_is_not_visible_to_the_group(
    conn: DictConnection, db: Database
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.commit()
    conn.execute(
        """INSERT INTO ref.futures_contract
           (secid, asset_code, is_fx, base_ccy, quote_ccy, contract_type, first_trade_date,
            last_trade_date, last_del_date, spec_source, first_seen, ingested_at)
           VALUES ('SiH6X', 'Si', true, 'USD', 'RUB', 'dated', '2025-01-01', '2026-03-18',
                   '2026-03-18', 'rfud', '2025-01-01', %s)""",
        (SPOT - timedelta(days=1),),
    )
    spot = inputs.Spot(
        "USD",
        "ntpro_public",
        "USD/RUB_TOM",
        "indicative_bid_offer",
        quote_ts=SPOT,
        bid=Decimal("10"),
        offer=Decimal("10.1"),
    )
    spot_capture = {"fetched_at": SPOT + timedelta(seconds=1), "sha256": "f" * 64}
    with db.connection() as reader:
        reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        known_at = reader.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        before = loader._group(reader, 42, known_at, spot, spot_capture)
        conn.commit()
        after = loader._group(reader, 42, known_at, spot, spot_capture)
        assert before == after
        assert before["nodes"][0]["secid"] == "SiH6"
    with db.connection() as reader:
        revised = loader._group(reader, 43, datetime.now(UTC), spot, spot_capture)
        assert revised["nodes"][0]["secid"] == "SiH6X"


def test_no_lookahead_and_consumer_reads_closed_schema_snapshot(
    conn: DictConnection, db: Database, db_url: str, settings: Settings
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=1))
    capture_cny(conn, datetime.now(UTC) - timedelta(seconds=1))
    for i, offset in enumerate((-60, 60)):
        curve = conn.execute(
            """INSERT INTO md.curve_snapshot
               (source, dataset, quote_basis, fetched_at, source_ts, snapshot_date,
                content_sha256, parser_version, ingested_at)
               VALUES ('otc_monitor', 'ruonia_ois', 'mid_only', %s, %s, %s, %s, '1', %s)
               RETURNING id""",
            (
                SPOT + timedelta(minutes=2, seconds=i),
                SPOT + timedelta(seconds=offset),
                SPOT.date(),
                str(i) * 64,
                SPOT + timedelta(minutes=2),
            ),
        ).fetchone()
        assert curve is not None
        conn.execute(
            "INSERT INTO md.curve_quote (snapshot_id, tenor, position, mid_pct) "
            "VALUES (%s, '1Y', 0, 10)",
            (curve["id"],),
        )
    conn.commit()
    run = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    conn.commit()
    finished = conn.execute(
        "SELECT finished_at FROM ops.job_run WHERE id = %s", (run.run_id,)
    ).fetchone()["finished_at"]
    conn.commit()
    day = finished.astimezone(MSK).date()
    before = Cut(
        "market",
        "intraday",
        day,
        finished - timedelta(microseconds=1),
        finished + timedelta(seconds=1),
    )
    assert fx_input_pairs(conn, before, Provenance()).rows == []
    conn.commit()
    after = finished + timedelta(seconds=1)
    result = publish_market_snapshot(
        db, cut_type="intraday", business_date=day, cut_ts=after, known_at=after
    )
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="test-token")
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/snapshots/{result.market.id}/bundle",
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 200
        doc = json.loads(gzip.decompress(response.content))
        history = client.get(
            "/v1/history/curves/ruonia_ois",
            params={
                "from": SPOT.date().isoformat(),
                "to": SPOT.date().isoformat(),
                "rule": "all",
                "source": "otc_monitor",
                "known_at": after.isoformat(),
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert history.status_code == 200
        body = history.json()
        curves = [dict(zip(body["columns"], item, strict=True)) for item in body["rows"]]
        # The history API does not apply an FX market-time bound: the consumer must do it.
        assert len(curves) == 2
        aligned = [
            item
            for item in curves
            if datetime.fromisoformat(item["source_ts"].replace("Z", "+00:00")) <= SPOT
        ]
        assert len(aligned) == 1
        assert aligned[0]["content_sha256"] == "0" * 64
    validate_bundle(doc)
    assert doc["schema_version"] == "market_snapshot/1.9"
    groups = {group["currency"]: group for group in doc["data"]["fx_input_pairs"]}
    assert set(groups) == {"USD", "EUR", "CNY"}
    assert groups["USD"]["coverage"]["all_four_observed"] is True
    assert groups["EUR"]["coverage"]["all_four_observed"] is False
    assert groups["CNY"]["spot"]["quote_kind"] == "last"
    row = groups["USD"].copy()
    digest = row.pop("content_sha256")
    row.pop("spot_age_seconds_at_cut")
    assert sha256_hex(canonical_bytes(row)) == digest


def test_invalid_latest_capture_does_not_fall_back_to_older_valid_capture(
    conn: DictConnection, db: Database
) -> None:
    seed_contracts(conn)
    capture_futures(conn, SPOT - timedelta(seconds=2))
    bad = capture_futures(conn, SPOT - timedelta(seconds=1))
    conn.execute("UPDATE ops.raw_capture SET sha256 = %s WHERE id = %s", ("0" * 64, bad))
    conn.commit()
    spot = inputs.Spot(
        "USD",
        "ntpro_public",
        "USD/RUB_TOM",
        "indicative_bid_offer",
        quote_ts=SPOT,
        bid=Decimal("10"),
        offer=Decimal("10.1"),
    )
    with db.connection() as reader:
        group = loader._group(
            reader,
            42,
            datetime.now(UTC),
            spot,
            {"fetched_at": SPOT + timedelta(seconds=1), "sha256": "f" * 64},
        )
    assert all(node["reasons"] == ["future_capture_invalid"] for node in group["nodes"])
    assert all(node["capture_sha256"] == "0" * 64 for node in group["nodes"])


def test_ntpro_failure_produces_unavailable_inputs_without_a_second_source(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    requests: list[str] = []

    def failing_factory(**kwargs: Any) -> HttpClient:
        def answer(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(503, request=request)

        return HttpClient(transport=httpx.MockTransport(answer), **kwargs)

    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=failing_factory)
    assert outcome.status == "partial"
    groups = conn.execute(
        "SELECT currency, document FROM md.fx_input_pair_group ORDER BY currency"
    ).fetchall()
    assert requests == ["https://ntprog.ru/local_api/public/east/"]
    assert len(groups) == 3
    usd = next(group["document"] for group in groups if group["currency"] == "USD")
    assert usd["spot"]["reasons"] == ["spot_source_unavailable", "spot_value_date_unknown"]
    assert all(node["reasons"] == ["spot_time_unavailable"] for node in usd["nodes"])


def test_failed_latest_job_is_unavailable_not_an_older_group(
    conn: DictConnection, db: Database, settings: Settings
) -> None:
    outcome = run_job("fx_input_pairs", settings=settings, db=db, http_factory=factory)
    assert outcome.status == "partial"
    with pytest.raises(RuntimeError, match="synthetic failure"), job_run(db, "fx_input_pairs"):
        raise RuntimeError("synthetic failure")
    now = datetime.now(UTC) + timedelta(seconds=1)
    cut = Cut("market", "intraday", now.astimezone(MSK).date(), now, now)
    result = fx_input_pairs(conn, cut, Provenance())
    assert result.rows == []
    assert result.stale_reasons == ["latest_capture_failed"]


def test_upgrade_existing_database_applies_new_migrations_and_api_grant(
    empty_db_url: str, template_db: str, tmp_path: Path
) -> None:
    # Start with the previous migration set, then upgrade exactly as the normal runner does.
    old = tmp_path / "old-migrations"
    old.mkdir()
    added = {"0290_md_fx_input_pairs.sql", "0671_fx_input_pair_source.sql"}
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name not in added:
            shutil.copy(path, old / path.name)
    migrate(empty_db_url, directory=old)
    report = migrate(empty_db_url)
    assert report.applied == ["0290_md_fx_input_pairs", "0671_fx_input_pair_source"]
    read_db = Database(make_conninfo(empty_db_url, user="mdhub_api"))
    with read_db.connection() as reader:
        assert (
            reader.execute("SELECT count(*) AS n FROM md.fx_input_pair_group").fetchone()["n"] == 0
        )
    with Database(empty_db_url).connection() as owner:
        row = owner.execute(
            "SELECT source FROM ops.job_source WHERE job = 'fx_input_pairs'"
        ).fetchone()
        assert row["source"] == "ntpro_public"
