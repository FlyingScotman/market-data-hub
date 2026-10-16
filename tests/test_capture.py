from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.capture import (
    job_run,
    last_capture_sha256,
    load_capture_payload,
    record_capture,
)
from mdhub.core.errors import JobSkipped
from mdhub.core.upsert import UpsertResult
from mdhub.db.pool import Database

Conn = psycopg.Connection[DictRow]


def _run_row(conn: Conn, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ops.job_run WHERE id = %s", (run_id,)).fetchone()
    assert row is not None
    return row


def test_job_run_success(db: Database, conn: Conn, settings: Settings) -> None:
    with job_run(
        db, "unit_job", {"date": datetime(2026, 10, 15, tzinfo=UTC)}, settings=settings
    ) as run:
        running = _run_row(conn, run.id)
        conn.commit()
        assert running["status"] == "running" and running["finished_at"] is None
        run.add_counts(UpsertResult(2, 1, 3))
        run.add_counts(UpsertResult(1, 0, 0))
    row = _run_row(conn, run.id)
    assert row["status"] == "succeeded"
    assert (row["rows_inserted"], row["rows_updated"], row["rows_unchanged"]) == (3, 1, 3)
    assert row["finished_at"] is not None and row["error"] is None
    assert row["params"] == {"date": "2026-10-15T00:00:00+00:00"}
    assert row["host"]


def test_job_run_failure_is_recorded_and_sanitized(db: Database, conn: Conn) -> None:
    settings = Settings(database_url="postgresql://x", moex_api_key="sup3r-secret-key")
    with pytest.raises(RuntimeError), job_run(db, "unit_job", settings=settings) as run:
        raise RuntimeError(
            "boom sup3r-secret-key at https://user:pw@iss.moex.com/x.json?token=abc&date=1"
        )
    row = _run_row(conn, run.id)
    assert row["status"] == "failed"
    err = row["error"]
    assert err.startswith("RuntimeError: boom")
    assert "sup3r-secret-key" not in err and "token" not in err and "pw@" not in err
    assert "https://iss.moex.com/x.json?date=1" in err


def test_job_run_data_rolls_back_but_run_is_recorded(db: Database, conn: Conn) -> None:
    def load(run_id: int) -> None:
        with db.connection() as c:
            record_capture(c, run_id, "moex_iss", "ep", "https://iss.moex.com/x", b"{}", "1")
            raise ValueError("parse failed")

    with pytest.raises(ValueError, match="parse"), job_run(db, "unit_job") as run:
        load(run.id)
    assert _run_row(conn, run.id)["status"] == "failed"
    assert conn.execute("SELECT count(*) AS n FROM ops.raw_capture").fetchone() == {"n": 0}


def test_job_run_skipped_and_partial(db: Database, conn: Conn) -> None:
    with job_run(db, "unit_job") as run:
        raise JobSkipped("cbonds auth 401")
    row = _run_row(conn, run.id)
    assert (row["status"], row["error"]) == ("skipped", "cbonds auth 401")

    with job_run(db, "unit_job") as run2:
        run2.mark_partial("SU1 missing")
        run2.mark_partial("SU2 missing")
    row = _run_row(conn, run2.id)
    assert (row["status"], row["error"]) == ("partial", "SU1 missing; SU2 missing")

    with job_run(db, "unit_job") as run3:
        run3.mark_skipped("nothing to do")
    assert _run_row(conn, run3.id)["status"] == "skipped"


def test_record_capture_from_response(db: Database, conn: Conn) -> None:
    body = b'{"history": {"columns": [], "data": []}}' * 50
    request = httpx.Request(
        "GET",
        "https://user:pw@iss.moex.com/iss/history.json?date=2026-10-15&apikey=SECRET&password=x",
    )
    response = httpx.Response(
        200, content=body, headers={"content-type": "application/json"}, request=request
    )
    with job_run(db, "unit_job") as run, db.connection() as c:
        src_ts = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
        cap_id = record_capture(
            c, run.id, "moex_iss", "bonds_history", None, response, "v1", source_ts=src_ts
        )
    row = conn.execute("SELECT * FROM ops.raw_capture WHERE id = %s", (cap_id,)).fetchone()
    assert row is not None
    assert row["url_redacted"] == "https://iss.moex.com/iss/history.json?date=2026-10-15"
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert gzip.decompress(row["payload_gz"]) == body
    assert len(row["payload_gz"]) < len(body)
    assert row["size_bytes"] == len(body)
    assert (row["http_status"], row["content_type"]) == (200, "application/json")
    assert row["run_id"] == run.id and row["parser_version"] == "v1"
    assert row["source_ts"] == src_ts
    assert load_capture_payload(conn, cap_id) == body
    assert last_capture_sha256(conn, "moex_iss", "bonds_history") == row["sha256"]
    assert last_capture_sha256(conn, "moex_iss", "other") is None


def test_record_capture_bytes_and_str(conn: Conn) -> None:
    fetched = datetime(2026, 10, 15, 12, tzinfo=UTC)
    a = record_capture(
        conn,
        None,
        "cbr",
        "key_rate",
        "https://www.cbr.ru/x?token=1",
        "текст",
        "1",
        content_type="text/html",
        http_status=200,
        fetched_at=fetched,
    )
    # Same payload, endpoint and fetch time: same capture row (unique key), no error.
    b = record_capture(
        conn, None, "cbr", "key_rate", "https://www.cbr.ru/x", "текст", "1", fetched_at=fetched
    )
    assert a == b
    assert load_capture_payload(conn, a) == "текст".encode()
    with pytest.raises(ValueError, match="url is required"):
        record_capture(conn, None, "cbr", "key_rate", None, b"x", "1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_capture(conn, None, "no_such_source", "ep", "https://x", b"x", "1")
    with pytest.raises(KeyError):
        load_capture_payload(conn, 999_999)
