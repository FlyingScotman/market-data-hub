"""Dataset-level gap scan of the required curves (review §6.2): a MOEX_STOCK business day
without a ``ruonia_ois`` / ``key_rate_swap`` snapshot from any source opens ``ops.data_gap``
and shows in ``/v1/gaps``; a snapshot stored later closes it as ``backfilled``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

import mdhub.loaders.swaps as swaps
import mdhub.quality.job  # noqa: F401  (registers the `quality` job)
from mdhub.api.app import create_app
from mdhub.config import Settings
from mdhub.core.timeutil import MSK, date_range
from mdhub.datasets import get_dataset
from mdhub.db.pool import Database
from mdhub.jobs import run_job

Conn = psycopg.Connection[DictRow]
FIRST, LAST = date(2026, 2, 2), date(2026, 2, 13)  # two full weeks (Mon..Fri), no holidays
REMOVED = date(2026, 2, 10)
AS_OF = date(2026, 2, 16)  # Monday: D-1 (Friday 13th) is the expected latest day


def _snapshot(conn: Conn, source: str, dataset: str, day: date, n: int) -> None:
    fetched = datetime.combine(day, time(12, 0), tzinfo=MSK)
    row = conn.execute(
        "INSERT INTO md.curve_snapshot (source, dataset, quote_basis, fetched_at, snapshot_date,"
        " content_sha256, parser_version)"
        " VALUES (%s, %s, 'mid_only', %s, %s, %s, '1') RETURNING id",
        (source, dataset, fetched, day, f"{n:064x}"),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO md.curve_quote (snapshot_id, tenor, position, mid_pct)"
        " VALUES (%s, '1Y', 0, 15)",
        (row["id"],),
    )


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    app = create_app(
        Settings(database_url=make_conninfo(db_url, user="mdhub_api"), api_tokens="tok-gap")
    )
    with TestClient(app) as c:
        yield c


def test_specs_registered() -> None:
    for ds in ("ruonia_ois", "key_rate_swap"):
        spec = get_dataset(f"curves_{ds}")
        assert spec.frequency == "business_daily" and spec.calendar == "MOEX_STOCK"
        assert spec.table == "md.curve_snapshot" and spec.where == f"dataset = '{ds}'"
        assert spec.backfill_job is None and spec.history_start == swaps.CURVE_GAP_SCAN_START


def test_missing_curve_day_opens_gap_and_shows_in_api(
    conn: Conn, db: Database, settings: Settings, client: TestClient
) -> None:
    n = 0
    for d in date_range(FIRST, LAST):
        if d.weekday() >= 5:
            continue
        n += 1
        _snapshot(conn, "andromeda", "key_rate_swap", d, n)  # any source counts
        if d != REMOVED:
            source = "otc_monitor" if n % 2 else "ratescalcs"
            _snapshot(conn, source, "ruonia_ois", d, 1000 + n)
    conn.commit()

    params = {
        "as_of": AS_OF.isoformat(),
        "datasets": ["curves_ruonia_ois", "curves_key_rate_swap"],
        "check_jobs": False,
        "lookback_days": (AS_OF - FIRST).days,
    }
    out = run_job("quality", params, settings=settings, db=db)
    assert out.status == "succeeded", out
    checks = {c["dataset"]: c for c in out.result["datasets"]}
    assert checks["curves_ruonia_ois"]["gap_days"] == 1
    assert checks["curves_ruonia_ois"]["gap_first"] == REMOVED.isoformat()
    assert checks["curves_key_rate_swap"]["gap_count"] == 0
    assert not checks["curves_ruonia_ois"]["is_stale"]
    gaps = conn.execute(
        "SELECT dataset, gap_date, status FROM ops.data_gap WHERE dataset LIKE 'curves_%'"
    ).fetchall()
    assert [(g["dataset"], g["gap_date"], g["status"]) for g in gaps] == [
        ("curves_ruonia_ois", REMOVED, "open")
    ]
    assert conn.execute("SELECT count(*) AS n FROM ops.backfill_task").fetchone() == {"n": 0}

    r = client.get(
        "/v1/gaps",
        params={"dataset": "curves_ruonia_ois"},
        headers={"Authorization": "Bearer tok-gap"},
    )
    assert r.status_code == 200
    assert [(g["dataset"], g["gap_date"]) for g in r.json()["data"]] == [
        ("curves_ruonia_ois", REMOVED.isoformat())
    ]

    # a snapshot of that day stored later (e.g. a history import) closes the gap
    _snapshot(conn, "andromeda", "ruonia_ois", REMOVED, 9999)
    conn.commit()
    out = run_job("quality", params, settings=settings, db=db)
    assert out.result["datasets"][0]["gap_count"] == 0
    st = conn.execute(
        "SELECT status FROM ops.data_gap WHERE dataset = 'curves_ruonia_ois'"
    ).fetchone()
    assert st == {"status": "backfilled"}


def test_missing_latest_day_is_stale(conn: Conn, db: Database, settings: Settings) -> None:
    for i, d in enumerate(date_range(FIRST, LAST - timedelta(days=1))):
        if d.weekday() < 5:
            _snapshot(conn, "otc_monitor", "ruonia_ois", d, i)
    conn.commit()
    out = run_job(
        "quality",
        {"as_of": AS_OF.isoformat(), "datasets": ["curves_ruonia_ois"], "check_jobs": False},
        settings=settings,
        db=db,
    )
    check = out.result["datasets"][0]
    assert check["is_stale"] and check["last_date"] == (LAST - timedelta(days=1)).isoformat()
    assert check["gap_last"] == LAST.isoformat()
