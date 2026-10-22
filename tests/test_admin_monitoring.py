"""Operator pages distinguish execution, authentication and observation freshness."""

import re
from collections.abc import Iterator

import pytest

from admin_helpers import Console
from test_admin import Conn, _client, _enrolled, fast_hasher  # noqa: F401
from test_credentials import _publish


@pytest.fixture
def console(db_url: str) -> Iterator[Console]:
    with _client(db_url) as client:
        yield Console(client)


def _row(html: str, text: str) -> str:
    return next(row for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html, re.S) if text in row)


def test_monitoring_partial_and_unchanged_loads(console: Console, conn: Conn) -> None:
    conn.execute(
        """INSERT INTO ops.job_run (job, status, finished_at, rows_inserted, rows_updated)
           VALUES ('cbonds_enrich', 'partial', now(), 31, 0),
                  ('swaps_ratescalcs', 'succeeded', now(), 0, 0)"""
    )
    conn.commit()
    _enrolled(console, conn)
    sources = console.get("/sources").text
    cbonds = _row(sources, "cbonds")
    assert 'class="pill warn">partial' in cbonds
    assert "31 / 0" in cbonds
    assert "no changed rows" in _row(sources, "ratescalcs")
    jobs = console.get("/jobs").text
    assert "Last succeeded / partial" in jobs
    assert 'class="pill warn">partial' in _row(jobs, "/jobs?job=cbonds_enrich")
    assert "no changed rows" in jobs
    assert "does not prove provider access" in jobs
    assert "Job cbonds_enrich: last run was partial" in console.get("/").text


def test_monitoring_transport_and_unconfigured_auth(console: Console, conn: Conn) -> None:
    conn.execute(
        """INSERT INTO ops.source_probe (source, endpoint, ok, http_status, auth, error)
           VALUES ('cbonds', 'test', false, NULL, 'ok', 'TLS connection failed'),
                  ('moex_apim', 'test', true, 401, 'not_configured', NULL),
                  ('ratescalcs', 'test', false, 503, 'n/a', 'HTTP 503')"""
    )
    conn.commit()
    _enrolled(console, conn)
    sources = console.get("/sources").text
    assert "configured; unverified" in _row(sources, "cbonds")
    assert "no HTTP response" in _row(sources, "cbonds")
    assert "not configured" in _row(sources, "moex_apim")
    assert 'class="pill ok"' not in _row(sources, "moex_apim")
    assert "HTTP response received" in _row(sources, "ratescalcs")
    assert "failed" in _row(sources, "ratescalcs")
    credentials = console.get("/credentials").text
    assert "configured; unverified" in _row(credentials, "CBONDS_LOGIN")
    assert "Unused by current collectors; no probe" in _row(credentials, "MOEX_PASSPORT_LOGIN")
    assert "not configured" in _row(credentials, "MOEX_API_KEY")


def test_monitoring_quality_snapshot_is_dated(console: Console, conn: Conn) -> None:
    conn.execute(
        """INSERT INTO ops.dataset_status
               (dataset, table_name, frequency, last_date, is_stale, checked_at)
           VALUES ('test_old', 'md.test', 'daily', '2026-10-15', true,
                   '2026-10-21 07:00:00+00')"""
    )
    conn.commit()
    _enrolled(console, conn)
    data = console.get("/data").text
    assert "Quality snapshot" in data
    assert "not live table contents" in data
    assert "2026-10-21 07:00:00 UTC" in data
    assert "2026-10-15" in data
    assert "stale at check" in data
    assert "Quality snapshot" in console.get("/").text


def test_monitoring_replaced_credential_needs_new_check(
    console: Console, conn: Conn, db_url: str
) -> None:
    _publish(db_url)
    conn.execute(
        """INSERT INTO ops.source_probe (source, endpoint, ok, http_status, auth, ts)
           VALUES ('cbonds', 'test', true, 200, 'ok', now() - interval '1 hour')"""
    )
    conn.commit()
    _enrolled(console, conn)
    console.get("/credentials")
    response = console.post("/credentials/CBONDS_LOGIN", {"value": "test-credential"})
    assert response.status_code == 200
    row = _row(response.text, "CBONDS_LOGIN")
    assert "not checked" in row and "probe accepted" not in row


@pytest.mark.parametrize(
    ("ok", "http_status", "auth", "label"),
    [
        (True, 200, "ok", "probe accepted"),
        (False, 403, "ok", "configured; unverified"),
        (False, 401, "rejected", "rejected"),
    ],
)
def test_monitoring_probe_outcomes(
    console: Console, conn: Conn, ok: bool, http_status: int, auth: str, label: str
) -> None:
    conn.execute(
        """INSERT INTO ops.source_probe (source, endpoint, ok, http_status, auth)
           VALUES ('cbonds', 'test', %s, %s, %s)""",
        (ok, http_status, auth),
    )
    conn.commit()
    _enrolled(console, conn)
    for path in ("/sources", "/credentials"):
        row = _row(console.get(path).text, "CBONDS_LOGIN" if path == "/credentials" else "cbonds")
        assert label in row
        if not ok:
            assert 'class="pill ok"' not in row
