"""Observability backend (ADMIN_DESIGN §3.2): worker heartbeat, the source_probe job,
ops.v_source_status, ops.job_source and the retention of the ops log tables."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
import respx
from psycopg.rows import DictRow

import mdhub.loaders.maintenance
import mdhub.quality.source_probe as sp
from mdhub import __version__
from mdhub.config import Settings
from mdhub.core.heartbeat import Heartbeat
from mdhub.core.http import HttpClient, RateLimiter
from mdhub.db.pool import Database
from mdhub.jobs import JobContext, JobRegistry, run_job, running_jobs
from mdhub.schedule import KNOWN_JOBS, RULES

Conn = psycopg.Connection[DictRow]
NOT_LOADERS = {
    "quality", "backfill_worker", "maintenance", "source_probe", "snapshot_static",
    "snapshot_market",
}  # fmt: skip


def _http_factory(**kw: Any) -> HttpClient:
    kw.setdefault("max_retries", 0)
    return HttpClient(limiter=RateLimiter(default_rps=None), sleep=lambda _s: None, **kw)


# ------------------------------------------------------------------------- heartbeat


def test_heartbeat_reports_running_jobs_and_stop(db: Database, conn: Conn) -> None:
    hb = Heartbeat(db, "worker", running=running_jobs)
    registry = JobRegistry()
    seen: list[dict[str, Any]] = []

    def busy(ctx: JobContext) -> None:
        hb.beat()
        with db.connection() as c:
            row = c.execute("SELECT running_jobs FROM ops.worker_heartbeat").fetchone()
        assert row is not None
        seen.extend(row["running_jobs"])

    registry.register("busy", "test", busy)
    out = run_job("busy", settings=Settings(database_url=db.conninfo), db=db, registry=registry)
    assert [(j["job"], j["run_id"], j["trigger"]) for j in seen] == [("busy", out.run_id, "other")]
    assert running_jobs() == []

    hb.beat()
    row = conn.execute("SELECT * FROM ops.worker_heartbeat").fetchone()
    assert row is not None and row["running_jobs"] == [] and row["stopped_at"] is None
    assert row["process"] == "worker" and row["version"] == __version__ and row["pid"] > 0
    conn.commit()
    hb.stop()  # never started: still records the clean stop
    row = conn.execute("SELECT beat_at, stopped_at FROM ops.worker_heartbeat").fetchone()
    assert row is not None and row["stopped_at"] is not None


def test_heartbeat_thread_beats_and_survives_db_errors(db: Database, pg_server: Any) -> None:
    hb = Heartbeat(db, "worker", interval_s=0.05)
    hb.start()
    try:
        first = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with db.connection() as c:
                row = c.execute("SELECT beat_at FROM ops.worker_heartbeat").fetchone()
            if row and first is None:
                first = row["beat_at"]
            elif row and row["beat_at"] > first:
                break
            time.sleep(0.05)
        else:
            pytest.fail("heartbeat did not advance")
    finally:
        hb.stop()
    broken = Heartbeat(Database(pg_server.url("no_such_database")), interval_s=0.01)
    broken.start()
    broken.stop()  # logs, never raises


# ---------------------------------------------------------------------- source probe


def _creds(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        moex_api_key="apim-key-secret",
        ratescalc_username="rc-user",
        ratescalc_password="rc-pass-secret",
        cbonds_login="cb-login",
        cbonds_password="cb-pass-secret",
    )


def _mock_sources(router: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "moex_iss": router.get("https://iss.moex.com/iss/engines.json").respond(
            200, json={"engines": {}}
        ),
        "moex_apim": router.get("https://apim.moex.com/iss/engines.json").respond(401),
        "cbr": router.get("https://www.cbr.ru/scripts/XML_daily.asp").respond(
            200, text="<ValCurs/>"
        ),
        "ecb": router.get(sp.ecb.DAILY_URL).mock(side_effect=httpx.ConnectTimeout("slow")),
        "rosstat": router.get("https://rosstat.gov.ru/statistics/price").respond(503),
        "ratescalcs": router.get("https://ratescalcs.ru/cnyfuturesyields/snapshot").respond(
            200, json={"timestamp": "x"}
        ),
        "otc_monitor": router.get("https://otc-monitor.ru/api/bond/").respond(401),
        "cbonds": router.post("https://ws2.cbonds.info/services/json/get_emissions/").respond(
            200, json={"items": [], "total": 0}
        ),
        "nkr": router.get("https://www.ratings.ru/robots.txt").respond(200, text="User-Agent: *"),
        "nra": router.get("https://www.ra-national.ru/robots.txt").respond(200, text=""),
        "raexpert": router.get("https://raexpert.ru/robots.txt").mock(
            side_effect=httpx.ConnectError("reset")
        ),
    }


def _probe_rows(conn: Conn) -> dict[str, dict[str, Any]]:
    conn.rollback()
    rows = conn.execute("SELECT * FROM ops.source_probe ORDER BY id").fetchall()
    return {r["source"]: r for r in rows}


def test_source_probe_job_records_every_source(
    db_url: str, db: Database, conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _creds(db_url)
    with respx.mock(assert_all_called=False) as router:
        routes = _mock_sources(router)
        out = run_job(
            "source_probe",
            {"force": "true"},
            settings=settings,
            db=db,
            http_factory=_http_factory,
        )
    assert out.status == "succeeded"
    rows = _probe_rows(conn)
    assert set(rows) == {p.source for p in sp.PROBES}
    assert {k: (r["ok"], r["auth"], r["http_status"]) for k, r in rows.items()} == {
        "moex_iss": (True, "n/a", 200),
        "moex_apim": (False, "rejected", 401),
        "cbr": (True, "n/a", 200),
        "ecb": (False, "n/a", None),
        "rosstat": (False, "n/a", 503),
        "ratescalcs": (True, "ok", 200),
        "otc_monitor": (True, "not_configured", 401),  # reachable; no token to check
        "cbonds": (True, "ok", 200),
        "nkr": (True, "n/a", 200),
        "nra": (True, "n/a", 200),
        "raexpert": (False, "n/a", None),
    }
    assert rows["ecb"]["error"].startswith("TransportFailure: ConnectTimeout")
    assert rows["rosstat"]["error"] == "HTTP 503"
    assert all(r["run_id"] == out.run_id and r["latency_ms"] >= 0 for r in rows.values())
    assert rows["moex_iss"]["endpoint"] == "https://iss.moex.com/iss/engines.json?iss.meta=off"
    # credentials only in headers / body, never in the recorded endpoint or errors
    apim_req = routes["moex_apim"].calls.last.request
    assert apim_req.headers["authorization"] == "Bearer apim-key-secret"
    rc_req = routes["ratescalcs"].calls.last.request
    assert rc_req.headers["authorization"] == "Basic " + base64.b64encode(
        b"rc-user:rc-pass-secret"
    ).decode("ascii")
    assert "authorization" not in routes["otc_monitor"].calls.last.request.headers
    assert b"cb-pass-secret" in routes["cbonds"].calls.last.request.content
    dump = repr(rows) + caplog.text
    for secret in ("apim-key-secret", "rc-pass-secret", "cb-pass-secret"):
        assert secret not in dump


def test_source_probe_intervals_hours_and_subset(db_url: str, db: Database, conn: Conn) -> None:
    settings = _creds(db_url)

    def run(params: dict[str, Any]) -> Any:
        with respx.mock(assert_all_called=False) as router:
            _mock_sources(router)
            return run_job(
                "source_probe", params, settings=settings, db=db, http_factory=_http_factory
            )

    first = run({"sources": "cbonds,cbr", "force": "true"})
    assert set(first.result["probed"]) == {"cbonds", "cbr"}
    second = run({"sources": "cbonds,cbr"})  # Cbonds at most hourly
    assert set(second.result["probed"]) == {"cbr"} and second.result["skipped"] == ["cbonds"]
    with pytest.raises(Exception, match="unknown probe source"):
        run({"sources": "nope"})
    assert sp.ratescalcs_hours(datetime(2026, 10, 16, 9, 0, tzinfo=sp.MSK))  # Friday
    assert not sp.ratescalcs_hours(datetime(2026, 10, 16, 23, 0, tzinfo=sp.MSK))
    assert not sp.ratescalcs_hours(datetime(2026, 10, 17, 12, 0, tzinfo=sp.MSK))  # Saturday
    assert not sp.ratescalcs_hours(datetime(2026, 10, 16, 5, 49, tzinfo=UTC))  # 08:49 MSK


def test_tls_expiry_from_network_stream() -> None:
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = {"notAfter": "Jan 15 12:00:00 2027 GMT"}
    stream = MagicMock()
    stream.get_extra_info.return_value = ssl_object
    response = httpx.Response(200, extensions={"network_stream": stream})
    assert sp.tls_expiry(response) == datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
    assert sp.tls_expiry(httpx.Response(200)) is None


def test_source_probe_is_scheduled_every_5_minutes() -> None:
    (rule,) = RULES["source_probe"]
    assert rule.cron == "*/5 * * * *" and rule.days == "all" and not rule.catch_up


# -------------------------------------------------------------------- source status


def test_job_source_covers_every_loader(conn: Conn) -> None:
    rows = conn.execute("SELECT job, source FROM ops.job_source").fetchall()
    jobs = {r["job"] for r in rows}
    assert jobs <= set(KNOWN_JOBS)
    assert jobs == set(KNOWN_JOBS) - NOT_LOADERS
    assert {r["source"] for r in rows} == {p.source for p in sp.PROBES}


def test_v_source_status(conn: Conn) -> None:
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO ops.source_probe (source, endpoint, ts, ok, http_status, auth, error)
        VALUES ('cbr', 'u', %(t1)s, true, 200, 'n/a', NULL),
               ('cbr', 'u', %(t2)s, false, 503, 'n/a', 'HTTP 503'),
               ('ecb', 'u', %(t1)s, true, 200, 'n/a', NULL)
        """,
        {"t1": now - timedelta(minutes=10), "t2": now - timedelta(minutes=5)},
    )
    conn.execute(
        """
        INSERT INTO ops.job_run (job, started_at, finished_at, status)
        VALUES ('cbr_daily', %(a)s, %(a)s + interval '1 minute', 'succeeded'),
               ('cbr_meetings', %(b)s, %(b)s + interval '1 minute', 'succeeded'),
               ('cbr_daily', %(c)s, %(c)s + interval '1 minute', 'failed')
        """,
        {"a": now - timedelta(hours=3), "b": now - timedelta(hours=2), "c": now},
    )
    rows = {r["source"]: r for r in conn.execute("SELECT * FROM ops.v_source_status").fetchall()}
    assert {p.source for p in sp.PROBES} <= set(rows)
    cbr = rows["cbr"]
    assert cbr["ok"] is False and cbr["http_status"] == 503 and cbr["error"] == "HTTP 503"
    assert cbr["last_ok_probe_at"] == now - timedelta(minutes=10)
    assert cbr["last_load_job"] == "cbr_meetings"
    assert cbr["last_load_at"] == now - timedelta(hours=2) + timedelta(minutes=1)
    assert rows["ecb"]["ok"] is True and rows["ecb"]["last_load_at"] is None
    assert rows["moex_iss"]["probed_at"] is None  # loader source, not probed yet
    assert "andromeda" not in rows  # neither probed nor loaded


# ------------------------------------------------------------------------- retention


def test_retention_purges_old_ops_logs(settings: Settings, db: Database, conn: Conn) -> None:
    old, new = datetime.now(UTC) - timedelta(days=91), datetime.now(UTC) - timedelta(days=1)
    for ts in (old, new):
        conn.execute(
            "INSERT INTO ops.api_request (ts, method, path, status, duration_ms) "
            "VALUES (%s, 'GET', '/v1/coverage', 200, 1)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO ops.source_probe (source, endpoint, ts, ok, auth) "
            "VALUES ('cbr', 'u', %s, true, 'n/a')",
            (ts,),
        )
        conn.execute(
            "INSERT INTO ops.worker_heartbeat (instance_id, process, host, pid, version, "
            "started_at, beat_at) VALUES (gen_random_uuid(), 'worker', 'h', 1, 'v', %s, %s)",
            (ts, ts),
        )
    conn.commit()
    out = run_job("maintenance", {"mode": "retention", "vacuum": "false"}, settings=settings, db=db)
    assert out.result["ops_log_retention_days"] == 90
    assert out.result["ops_logs_deleted"] == {
        "ops.api_request": 1,
        "ops.source_probe": 1,
        "ops.worker_heartbeat": 1,
    }
    for table, column in mdhub.loaders.maintenance.OPS_LOG_TABLES:
        row = conn.execute(f"SELECT min({column}) AS m, count(*) AS n FROM {table}").fetchone()
        assert row is not None and row["n"] == 1 and row["m"] > old


# ---------------------------------------------------------------- admin schema access


def test_admin_schema_closed_to_public(conn: Conn) -> None:
    acl = conn.execute(
        """
        SELECT n.nspacl::text AS schema_acl,
               (SELECT array_agg(c.relacl::text) FROM pg_class c
                WHERE c.relnamespace = n.oid AND c.relacl IS NOT NULL) AS rel_acls
        FROM pg_namespace n WHERE n.nspname = 'admin'
        """
    ).fetchone()
    assert acl is not None
    owner = conn.execute("SELECT current_user AS u").fetchone()
    assert owner is not None
    for text in [acl["schema_acl"], *(acl["rel_acls"] or [])]:
        if text is None:
            continue
        grantees = {e.split("=", 1)[0] for e in text.strip("{}").split(",")}
        # the console and the API (token lookup) are granted by admin.grant_service_roles()
        assert grantees <= {owner["u"], "mdhub_admin", "mdhub_api"}, text
