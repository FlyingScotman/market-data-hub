"""Opt-in synthetic history benchmark: run this file explicitly with pytest -s.

Uses the usual disposable database; no production data or credentials. Captures both SQL
statements from the real route, then explains them with ANALYZE/BUFFERS. The file name keeps
this multi-million-row benchmark outside the default test collection.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from psycopg import sql

from mdhub.api.routes import history
from test_history_api import AUTH, Conn, TestClient
from test_history_api import client as client


@pytest.mark.timeout(600)
def test_history_benchmark(client: TestClient, conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = conn.execute(
        "SELECT version(), current_setting('work_mem') AS work_mem, "
        "current_setting('shared_buffers') AS shared_buffers, "
        "current_setting('max_parallel_workers_per_gather') AS parallel_workers"
    ).fetchone()
    print(f"benchmark_environment: {environment}")
    conn.execute(
        """
        INSERT INTO ref.bond (secid, primary_board, marketprice_board, bond_class,
                              coupon_type, coupon_type_basis, status)
        SELECT 'B' || lpad(i::text, 5, '0'), 'TQCB', 'TQCB', 'corporate', 'fixed', 'moex_label',
               'active'
        FROM generate_series(1, 4000) i;
        WITH days AS (
            SELECT d::date AS d FROM generate_series('2024-11-01'::date, '2026-10-01', '1 day') d
            WHERE extract(isodow FROM d) < 6 LIMIT 500)
        INSERT INTO md.bond_eod (trade_date, secid, board, close, waprice, accrued_int,
               face_value, face_unit, currency_id, volume, value, num_trades, ingested_at)
        SELECT days.d, 'B' || lpad(i::text, 5, '0'), 'TQCB', 95 + i / 10000.0,
               95.1, 12.34, 1000, 'SUR', 'SUR', 1000, 950000, 10, '2026-10-21Z'
        FROM days, generate_series(1, 4000) i;
        INSERT INTO md.bond_eod_yield (trade_date, secid, board, effective_yield_pct,
               duration_days, gspread_bp, ingested_at)
        SELECT trade_date, secid, board, 15.5, 700, 120, ingested_at
        FROM md.bond_eod WHERE right(secid, 1) IN ('0', '1', '2', '3', '4', '5');
        ANALYZE ref.bond; ANALYZE md.bond_eod; ANALYZE md.bond_eod_yield;
        """
    )
    conn.commit()
    captured: list[tuple[str, Any, Any, float]] = []

    def record(name: str, fn: Any) -> Any:
        def wrapped(connection: Any, query: Any, params: Any, *args: Any) -> Any:
            started = time.perf_counter()
            result = fn(connection, query, params, *args)
            captured.append((name, query, params, time.perf_counter() - started))
            return result

        return wrapped

    monkeypatch.setattr(history, "fetch_version", record("version", history.fetch_version))
    monkeypatch.setattr(history, "fetch_page", record("page", history.fetch_page))
    base = {"from": "2026-01-01", "to": "2026-10-20", "limit": 2}
    first = client.get("/v1/history/bonds", params=base, headers=AUTH)
    assert first.status_code == 200, first.text
    cases = {
        "broad_first": base,
        "broad_next": {**base, "cursor": first.json()["next"]},
        "narrow": {**base, "from": "2026-09-01", "secid": "B02000"},
    }
    for name, params in cases.items():
        captured.clear()
        started = time.perf_counter()
        response = client.get("/v1/history/bonds", params=params, headers=AUTH)
        elapsed = time.perf_counter() - started
        assert response.status_code == 200, response.text
        assert len(response.json()["rows"]) == 2
        print(f"\n{name}: http_ms={elapsed * 1000:.2f}")
        for kind, query, binds, duration in captured:
            plan = conn.execute(
                sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + query, binds
            ).fetchone()
            assert plan is not None
            print(f"{kind}: route_ms={duration * 1000:.2f}")
            print(json.dumps(plan, default=str))
    started = time.perf_counter()
    index = client.get("/v1/history/index", headers=AUTH)
    assert index.status_code == 200, index.text
    print(f"\nindex_cold: http_ms={(time.perf_counter() - started) * 1000:.2f}")
    plan = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + history._INDEX_SQL,
        {
            "rusfar": [*history.RUSFAR_RATE_SERIES, *history.RUSFAR_INDEX_SERIES],
            "rates": list(history.RUSFAR_RATE_SERIES),
        },
    ).fetchone()
    print(json.dumps(plan, default=str))
    started = time.perf_counter()
    cached = client.get("/v1/history/index", headers=AUTH)
    assert cached.status_code == 200 and cached.content == index.content
    print(f"index_cached: http_ms={(time.perf_counter() - started) * 1000:.2f}")
