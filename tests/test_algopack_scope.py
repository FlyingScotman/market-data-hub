"""AlgoPack scope "option b" (0423): obstats dropped; tradestats + FUTOI kept for a rolling
window (``MDHUB_ALGOPACK_RETENTION_DAYS``) - retention, gap scan, gap/task handling, backfill
clamp and the migration on top of 0422."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

import mdhub.loaders.futures as fut
import mdhub.quality.job  # noqa: F401 - registers the `quality` job
from mdhub.config import Settings
from mdhub.core.bizday import weekday_calendar
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.core.timeutil import MSK, msk_today
from mdhub.datasets import DatasetSpec, get_dataset, rolling_window_start
from mdhub.db.migrate import MIGRATIONS_DIR, discover_migrations, migrate
from mdhub.db.pool import Database
from mdhub.jobs import JobParamError, run_job
from mdhub.loaders import maintenance as mt
from mdhub.quality.checks import (
    apply_rolling_window,
    queue_backfill,
    reconcile_gaps,
    scan_dataset,
    window_start,
)

Conn = psycopg.Connection[DictRow]
FIX = Path(__file__).parent / "fixtures" / "moex_fx"
TODAY = date(2026, 10, 16)
TS = "algopack_fo_tradestats"
FUTOI = "algopack_futoi"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIX / name).read_text("utf-8"))
    return data


def http_factory(**kw: Any) -> HttpClient:
    return HttpClient(
        limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, max_retries=0, **kw
    )


def capture(conn: Conn, endpoint: str, fetched_at: datetime, tag: str, source: str) -> int:
    row = conn.execute(
        """
        INSERT INTO ops.raw_capture (source, endpoint, url_redacted, fetched_at, parser_version,
            sha256, payload_gz, size_bytes)
        VALUES (%s, %s, 'https://x', %s, '1', encode(sha256(%s::bytea), 'hex'), '\\x00', 1)
        RETURNING id
        """,
        (source, endpoint, fetched_at, tag.encode()),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def msk(d: date, hh: int = 0, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=MSK)


# ------------------------------------------------------------------------- registry


def test_registry_has_no_obstats_and_window_specs() -> None:
    with pytest.raises(KeyError):
        get_dataset("algopack_fo_obstats")
    s30 = Settings(algopack_retention_days=30)  # type: ignore[call-arg]
    for name in (TS, FUTOI):
        spec = get_dataset(name)
        assert spec.rolling_window_setting == "algopack_retention_days"
        assert spec.rolling_window_days(Settings()) == 90
        assert spec.window_start(TODAY, s30) == date(2026, 9, 17)
    assert get_dataset("futures_eod").window_start(TODAY, s30) is None
    assert rolling_window_start(TODAY, 90) == date(2026, 7, 19)
    assert rolling_window_start(TODAY, 1) == TODAY
    with pytest.raises(ValueError, match="int Settings field"):
        DatasetSpec(
            name="x",
            table="md.x",
            date_column="d",
            calendar=None,
            frequency="intraday",
            max_lag_days=1,
            description="x",
            rolling_window_setting="no_such_setting",
        )
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Settings(algopack_retention_days=0)  # type: ignore[call-arg]
    assert {e for e in fut.ALGOPACK_RAW_ENDPOINTS if "obstats" in e} == set()


# ------------------------------------------------------------------------ retention


def _bar(conn: Conn, table: str, day: date, capture_id: int | None, tag: str) -> None:
    if table == "md.algopack_fo_tradestats":
        conn.execute(
            "INSERT INTO md.algopack_fo_tradestats (secid, interval_ts, trade_date, pr_close, "
            "capture_id) VALUES (%s, %s, %s, 1, %s)",
            (tag, msk(day, 10), day, capture_id),
        )
    else:
        conn.execute(
            "INSERT INTO md.algopack_futoi (ticker, interval_ts, clgroup, trade_date, pos, "
            "capture_id) VALUES (%s, %s, 'YUR', %s, 1, %s)",
            (tag, msk(day, 10), day, capture_id),
        )


def test_retention_deletes_only_rows_and_captures_before_the_window(
    conn: Conn, db: Database, settings: Settings
) -> None:
    today = msk_today()  # the job's "today"; intraday raw retention uses now() as well
    start = rolling_window_start(today, settings.algopack_retention_days)
    old, new = start - timedelta(days=1), start
    old_fetch, new_fetch = msk(old, 12), msk(new, 0, 30)
    caps = {
        "old_hist": capture(conn, "algopack_fo_tradestats_history", old_fetch, "a", "moex_apim"),
        "new_hist": capture(conn, "algopack_fo_tradestats_history", new_fetch, "b", "moex_apim"),
        "old_futoi": capture(conn, "futoi_history", old_fetch, "c", "moex_apim"),
        "new_poll": capture(conn, "intraday:algopack_fo_tradestats", new_fetch, "d", "moex_apim"),
        "old_other_src": capture(conn, "futoi_history", old_fetch, "e", "moex_iss"),
        "old_eod": capture(conn, "forts_history", old_fetch, "f", "moex_iss"),
    }
    for table in ("md.algopack_fo_tradestats", "md.algopack_futoi"):
        _bar(conn, table, old, caps["old_hist"], "OLD")
        _bar(conn, table, new, caps["new_hist"], "NEW")
        # a row inside the window loaded by a capture fetched before it (e.g. a backfill)
        _bar(conn, table, new, caps["old_futoi"], "NEWOLDCAP")
    # revisions -> one ops.row_history image per table and day
    conn.execute("UPDATE md.algopack_fo_tradestats SET pr_close = 2")
    conn.execute("UPDATE md.algopack_futoi SET pos = 2")
    conn.commit()
    history_before = conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone()
    assert history_before == {"n": 6}

    out = run_job("maintenance", {"mode": "retention", "vacuum": "false"}, settings=settings, db=db)
    assert out.status == "succeeded"
    algo = out.result["algopack"]
    assert algo["window_start"] == start.isoformat()
    assert algo["rows_deleted"] == {"md.algopack_fo_tradestats": 1, "md.algopack_futoi": 1}
    assert algo["history_deleted"] == 2
    assert algo["raw_deleted"] == 2
    assert out.result["algopack_retention_days"] == 90

    for table in ("md.algopack_fo_tradestats", "md.algopack_futoi"):
        key = "secid" if "tradestats" in table else "ticker"
        rows = {
            r[key]: r["capture_id"] for r in conn.execute(f"SELECT {key}, capture_id FROM {table}")
        }
        assert rows == {"NEW": caps["new_hist"], "NEWOLDCAP": None}  # FK: ON DELETE SET NULL
    left = {r["id"] for r in conn.execute("SELECT id FROM ops.raw_capture")}
    assert left == {caps["new_hist"], caps["new_poll"], caps["old_other_src"], caps["old_eod"]}
    # DELETEs wrote no history; only the images of the in-window rows remain
    hist = conn.execute(
        "SELECT table_name, old_row ->> 'trade_date' AS d FROM ops.row_history ORDER BY 1, 2"
    ).fetchall()
    assert len(hist) == 4 and {h["d"] for h in hist} == {new.isoformat()}
    triggers = {
        r["tgrelid"]
        for r in conn.execute(
            "SELECT tgrelid::regclass::text AS tgrelid FROM pg_trigger "
            "WHERE tgname = 'mdhub_row_history' AND tgrelid::regclass::text LIKE 'md.algopack%%'"
        )
    }
    assert triggers == {"md.algopack_fo_tradestats", "md.algopack_futoi"}

    again = run_job(
        "maintenance", {"mode": "retention", "vacuum": "false"}, settings=settings, db=db
    )
    assert again.result["algopack"]["rows_deleted"] == {
        "md.algopack_fo_tradestats": 0,
        "md.algopack_futoi": 0,
    }
    assert conn.execute("SELECT count(*) AS n FROM ops.row_history").fetchone() == {"n": 4}


def test_purge_algopack_window_parameter(conn: Conn, db: Database) -> None:
    _bar(conn, "md.algopack_fo_tradestats", date(2026, 10, 1), None, "A")
    _bar(conn, "md.algopack_fo_tradestats", date(2026, 10, 7), None, "B")
    conn.commit()
    out = mt.purge_algopack_window(db, TODAY, 10)  # window from 2026-10-07
    assert out["window_start"] == "2026-10-07"
    assert out["rows_deleted"]["md.algopack_fo_tradestats"] == 1
    with pytest.raises(JobParamError):
        mt.purge_algopack_window(db, TODAY, 0)


# ------------------------------------------------------------------ quality / gaps


def test_gap_scan_is_limited_to_the_window(conn: Conn) -> None:
    spec = get_dataset(TS)
    s30 = Settings(algopack_retention_days=30)  # type: ignore[call-arg]
    _bar(conn, "md.algopack_fo_tradestats", date(2025, 1, 10), None, "OLD")
    _bar(conn, "md.algopack_fo_tradestats", TODAY, None, "NOW")
    conn.commit()
    check = scan_dataset(
        conn, spec, today=TODAY, calendar=weekday_calendar, lookback_days=1000, settings=s30
    )
    assert check.window_start == check.scan_from == date(2026, 9, 17)
    assert check.gaps and min(g.day for g in check.gaps) >= date(2026, 9, 17)
    assert check.to_dict()["window_start"] == "2026-09-17"
    unlimited = scan_dataset(
        conn, get_dataset("futures_eod"), today=TODAY, calendar=weekday_calendar, settings=s30
    )
    assert unlimited.window_start is None
    assert window_start(spec, TODAY, s30) == date(2026, 9, 17)


def _gap(conn: Conn, dataset: str, day: date, status: str = "open") -> None:
    conn.execute(
        "INSERT INTO ops.data_gap (dataset, gap_date, status, note) VALUES (%s, %s, %s, 'x')",
        (dataset, day, status),
    )


def _task(conn: Conn, dataset: str, d1: date, d2: date) -> int:
    row = conn.execute(
        "INSERT INTO ops.backfill_task (dataset, params, source) VALUES (%s, %s, 'quality') "
        "RETURNING id",
        (dataset, Jsonb({"date_from": d1.isoformat(), "date_to": d2.isoformat()})),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_out_of_window_gaps_closed_and_not_requeued(conn: Conn) -> None:
    spec = get_dataset(TS)
    s90 = Settings(algopack_retention_days=90)  # type: ignore[call-arg]
    _gap(conn, TS, date(2026, 7, 1))
    _gap(conn, TS, date(2026, 7, 2), "unfillable")
    _gap(conn, TS, date(2026, 10, 1))
    _gap(conn, "futures_eod", date(2026, 7, 1))  # no window: untouched
    old_task = _task(conn, TS, date(2026, 7, 1), date(2026, 7, 3))
    straddle = _task(conn, TS, date(2026, 7, 17), date(2026, 7, 21))
    conn.commit()
    # queueing alone never looks before the window
    assert queue_backfill(conn, spec, weekday_calendar, today=TODAY, settings=s90) == [
        (date(2026, 10, 1), date(2026, 10, 1))
    ]
    closed, cancelled = apply_rolling_window(conn, spec, today=TODAY, settings=s90, run_id=5)
    assert (closed, cancelled) == (1, 1)
    gaps = {
        (r["dataset"], r["gap_date"]): r
        for r in conn.execute("SELECT dataset, gap_date, status, note FROM ops.data_gap")
    }
    assert gaps[(TS, date(2026, 7, 1))]["status"] == "not_expected"
    assert "rolling 90-day window" in gaps[(TS, date(2026, 7, 1))]["note"]
    assert gaps[(TS, date(2026, 7, 2))]["status"] == "unfillable"
    assert gaps[(TS, date(2026, 10, 1))]["status"] == "open"
    assert gaps[("futures_eod", date(2026, 7, 1))]["status"] == "open"
    tasks = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM ops.backfill_task")}
    assert tasks[old_task] == "cancelled" and tasks[straddle] == "pending"
    assert apply_rolling_window(conn, get_dataset("futures_eod"), today=TODAY) == (0, 0)


def test_quality_job_applies_the_window(
    conn: Conn, db: Database, settings: Settings, today_is: Callable[[date], None]
) -> None:
    today_is(TODAY)
    _gap(conn, FUTOI, date(2026, 6, 30))
    _task(conn, FUTOI, date(2026, 6, 29), date(2026, 6, 30))
    conn.commit()
    out = run_job(
        "quality",
        {"datasets": FUTOI, "check_jobs": "false", "catch_up": "false"},
        settings=settings,
        db=db,
    )
    assert out.status == "succeeded"
    ds = out.result["datasets"][0]
    assert ds["window_start"] is None  # no rows: freshness only, no scan
    assert ds["gaps_closed"] == 1
    status = conn.execute("SELECT status FROM ops.data_gap WHERE dataset = %s", (FUTOI,))
    assert status.fetchone() == {"status": "not_expected"}
    tasks = conn.execute("SELECT status FROM ops.backfill_task WHERE dataset = %s", (FUTOI,))
    assert [r["status"] for r in tasks] == ["cancelled"]


def test_reconcile_closes_pre_window_gaps_as_not_expected(conn: Conn) -> None:
    spec = get_dataset(TS)
    s10 = Settings(algopack_retention_days=10)  # type: ignore[call-arg]  # from 2026-10-07
    _gap(conn, TS, date(2026, 10, 5))
    _gap(conn, TS, date(2026, 10, 8))
    conn.commit()
    filled, unfillable = reconcile_gaps(
        conn,
        spec,
        date(2026, 10, 5),
        date(2026, 10, 8),
        run_id=1,
        mark_unfillable=True,
        today=TODAY,
        settings=s10,
    )
    assert (filled, unfillable) == (0, 1)
    got = {r["gap_date"]: r["status"] for r in conn.execute("SELECT * FROM ops.data_gap")}
    assert got == {date(2026, 10, 5): "not_expected", date(2026, 10, 8): "unfillable"}


# ------------------------------------------------------------------------ backfill


def _empty_bars(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": {"columns": payload["data"]["columns"], "data": []},
        "data.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 0, 1000]]},
    }


@respx.mock
def test_backfill_clamps_to_the_window_and_refuses_older_ranges(
    db_url: str, db: Database, conn: Conn, today_is: Callable[[date], None]
) -> None:
    from test_futures import seed_ref

    today_is(TODAY)
    seed_ref(conn)
    settings = Settings(  # type: ignore[call-arg]
        database_url=db_url, iss_rps=1000.0, moex_api_key="k", algopack_retention_days=12
    )  # window from 2026-10-05
    p0, p1 = load("algopack_tradestats_SiZ6_p0.json"), load("algopack_tradestats_SiZ6_p1.json")
    futoi_day = load("futoi_si_day.json")
    futoi_empty = {"futoi": {"columns": futoi_day["futoi"]["columns"], "data": []}}

    def tradestats(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/SiZ6.json"):
            return httpx.Response(200, json=p0 if request.url.params["start"] == "0" else p1)
        return httpx.Response(200, json=_empty_bars(p0))

    def futoi(request: httpx.Request) -> httpx.Response:
        hit = request.url.path.endswith("/si.json") and request.url.params["from"] == "2026-10-15"
        return httpx.Response(200, json=futoi_day if hit else futoi_empty)

    ts_route = respx.get(url__regex=r".*/datashop/algopack/fo/tradestats/\w+\.json").mock(
        side_effect=tradestats
    )
    futoi_route = respx.get(url__regex=r".*/analyticalproducts/futoi/securities/\w+\.json").mock(
        side_effect=futoi
    )
    out = run_job(
        "algopack_fx_futures",
        {"date_from": "2026-10-01", "date_to": "2026-10-15"},
        settings=settings,
        db=db,
        http_factory=http_factory,
    )
    assert out.status == "succeeded"
    assert out.result == {
        "mode": "backfill",
        "date_from": "2026-10-05",
        "date_to": "2026-10-15",
        "requested_from": "2026-10-01",
    }
    assert {c.request.url.params["from"] for c in ts_route.calls} == {"2026-10-05"}
    assert min(c.request.url.params["from"] for c in futoi_route.calls) >= "2026-10-05"
    days = {
        r["trade_date"]
        for r in conn.execute("SELECT DISTINCT trade_date FROM md.algopack_fo_tradestats")
    }
    assert days == {date(2026, 10, 6)}  # the 10-01 bars of the answer are outside the window
    assert conn.execute("SELECT count(*) AS n FROM md.algopack_futoi").fetchone() == {"n": 6}

    calls = len(ts_route.calls)
    with pytest.raises(JobParamError, match="before the rolling window"):
        run_job(
            "algopack_fx_futures",
            {"date_from": "2026-09-01", "date_to": "2026-10-04"},
            settings=settings,
            db=db,
            http_factory=http_factory,
        )
    assert len(ts_route.calls) == calls
    last = conn.execute(
        "SELECT status FROM ops.job_run WHERE job = 'algopack_fx_futures' ORDER BY id DESC"
    ).fetchone()
    assert last == {"status": "failed"}


# ----------------------------------------------------------------------- migration


def test_migration_0423_on_top_of_0422(tmp_path: Path, empty_db_url: str) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for m in discover_migrations():
        if m.number <= 422:
            shutil.copy(MIGRATIONS_DIR / f"{m.version}.sql", mig_dir / f"{m.version}.sql")
    migrate(empty_db_url, directory=mig_dir)

    with psycopg.connect(empty_db_url, row_factory=dict_row) as conn:
        fetched = msk(TODAY, 10)
        ob_caps = [
            capture(conn, ep, fetched, ep, "moex_apim")
            for ep in (
                "intraday:algopack_fo_obstats",
                "intraday:reread:algopack_fo_obstats",
                "algopack_fo_obstats_history",
            )
        ]
        ts_cap = capture(conn, "intraday:algopack_fo_tradestats", fetched, "ts", "moex_apim")
        conn.execute(
            "INSERT INTO md.algopack_fo_obstats (secid, interval_ts, trade_date, mid_price, "
            "capture_id) VALUES ('SiZ6', %s, %s, 1, %s)",
            (msk(TODAY, 10), TODAY, ob_caps[0]),
        )
        conn.execute("UPDATE md.algopack_fo_obstats SET mid_price = 2")  # history image
        _bar(conn, "md.algopack_fo_tradestats", TODAY, ts_cap, "SiZ6")
        conn.execute("UPDATE md.algopack_fo_tradestats SET pr_close = 2")
        _gap(conn, "algopack_fo_obstats", date(2026, 10, 1))
        _gap(conn, TS, date(2026, 10, 1))
        _task(conn, "algopack_fo_obstats", date(2026, 10, 1), date(2026, 10, 1))
        conn.execute(
            "INSERT INTO ops.dataset_status (dataset, table_name, frequency) "
            "VALUES ('algopack_fo_obstats', 'md.algopack_fo_obstats', 'intraday'), "
            "('algopack_fo_tradestats', 'md.algopack_fo_tradestats', 'intraday')"
        )
        conn.commit()

    report = migrate(empty_db_url, directory=mig_dir)
    assert report.applied == []
    shutil.copy(MIGRATIONS_DIR / "0423_algopack_scope.sql", mig_dir / "0423_algopack_scope.sql")
    report = migrate(empty_db_url, directory=mig_dir)
    assert report.applied == ["0423_algopack_scope"]

    with psycopg.connect(empty_db_url, row_factory=dict_row) as conn:
        gone = conn.execute("SELECT to_regclass('md.algopack_fo_obstats') AS t").fetchone()
        assert gone == {"t": None}
        idx = conn.execute(
            "SELECT count(*) AS n FROM pg_class WHERE relname LIKE 'algopack_fo_obstats%%'"
        ).fetchone()
        assert idx == {"n": 0}
        caps = {r["id"] for r in conn.execute("SELECT id FROM ops.raw_capture")}
        assert caps == {ts_cap}
        hist = [r["table_name"] for r in conn.execute("SELECT table_name FROM ops.row_history")]
        assert hist == ["md.algopack_fo_tradestats"]
        gaps = {
            r["dataset"]: r["status"]
            for r in conn.execute("SELECT dataset, status FROM ops.data_gap")
        }
        assert gaps == {"algopack_fo_obstats": "not_expected", TS: "open"}
        task = conn.execute("SELECT status FROM ops.backfill_task").fetchone()
        assert task == {"status": "cancelled"}
        status = [r["dataset"] for r in conn.execute("SELECT dataset FROM ops.dataset_status")]
        assert status == [TS]
        ts = conn.execute("SELECT pr_close, capture_id FROM md.algopack_fo_tradestats").fetchone()
        assert ts == {"pr_close": Decimal("2"), "capture_id": ts_cap}
        comment = conn.execute(
            "SELECT obj_description('md.algopack_futoi'::regclass) AS c"
        ).fetchone()
        assert comment is not None and "MDHUB_ALGOPACK_RETENTION_DAYS" in comment["c"]
    # the packaged migrations (incl. any later ones) still apply on top
    assert "0423_algopack_scope" in migrate(empty_db_url).already_applied
