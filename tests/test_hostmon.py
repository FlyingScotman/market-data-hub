"""Host monitoring (ADMIN_DESIGN §10): sampler, SNTP, PostgreSQL stats, states, Machine page."""

from __future__ import annotations

import logging
import struct
from collections import namedtuple
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow

from admin_helpers import CLIENT_IP, ORIGIN, RP_ID, Console, SoftAuthenticator
from mdhub.admin import store
from mdhub.admin.app import CSP, create_app
from mdhub.admin.machine import svg_chart
from mdhub.config import Settings
from mdhub.db.pool import Database
from mdhub.hostmon.pgstats import PgStatsSampler
from mdhub.hostmon.sampler import (
    NTP_EPOCH_OFFSET,
    HostPaths,
    Sample,
    Sampler,
    parse_sntp,
    parse_updates,
    rollup,
    write_samples,
)
from mdhub.hostmon.status import above, machine_status, project_full, worst
from mdhub.loaders.maintenance import purge_host_metrics
from mdhub.updater import ContainerReporter
from test_updater import FakeDocker

Conn = psycopg.Connection[DictRow]

# ----------------------------------------------------------------------------- thresholds


def test_above_and_worst() -> None:
    assert above(79.9, 80, 90) == "ok"
    assert above(80, 80, 90) == "warn"
    assert above(95, 80, 90) == "crit"
    assert above(3, 2) == "warn" and above(1, 2) == "ok"
    assert worst(["ok", "crit", "warn"]) == "crit" and worst([]) == "ok"


def test_threshold_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().hostmon_disk_warn_pct == 80 and Settings().hostmon_pg_xact_max_minutes == 30
    monkeypatch.setenv("MDHUB_HOSTMON_DISK_WARN_PCT", "70")
    monkeypatch.setenv("MDHUB_NTP_SERVER", "off")
    s = Settings()
    assert s.hostmon_disk_warn_pct == 70 and s.ntp_server is None
    monkeypatch.setenv("MDHUB_HOSTMON_SWAP_MAX_PCT", "0")
    with pytest.raises(ValueError, match="greater than 0"):
        Settings()


def test_disk_projection() -> None:
    day = 86400.0
    t0 = 1_800_000_000.0
    # 1 GB/day from 50 GB on a 100 GB disk: full 50 days after t0
    growing = [(t0 + i * 3600, 50e9 + i * 1e9 / 24) for i in range(7 * 24)]
    full = project_full(growing, 100e9)
    assert full is not None and abs(full - (t0 + 50 * day)) < 60
    assert project_full([(t0 + i * 3600, 50e9) for i in range(48)], 100e9) is None  # flat
    assert project_full([(t0 - i * 3600, 50e9 + i) for i in range(48)][::-1], 100e9) is None
    assert project_full(growing[:5], 100e9) is None  # too few points
    assert project_full([(t0 + i * 60, i * 1e9) for i in range(30)], 100e9) is None  # 30 min


# -------------------------------------------------------------------------------- sampler

CpuTimes = namedtuple("CpuTimes", "user system idle iowait steal")
Vm = namedtuple("Vm", "total available")
Swap = namedtuple("Swap", "total used percent")
Usage = namedtuple("Usage", "total used percent")
DiskIo = namedtuple("DiskIo", "read_count write_count read_time write_time busy_time")
NetIo = namedtuple("NetIo", "bytes_recv bytes_sent errin errout dropin dropout")


class FakePsutil:
    PROCFS_PATH = "/proc"

    def __init__(self) -> None:
        self.n = 0
        self.net_fails = False

    def cpu_times(self) -> CpuTimes:
        self.n += 1
        k = self.n
        return CpuTimes(user=60 * k, system=10 * k, idle=10 * k, iowait=5 * k, steal=15 * k)

    def getloadavg(self) -> tuple[float, float, float]:
        return (1.0, 5.0, 3.0)

    def cpu_count(self) -> int:
        return 2

    def virtual_memory(self) -> Vm:
        return Vm(total=8 << 30, available=512 << 20)

    def swap_memory(self) -> Swap:
        return Swap(total=1 << 30, used=1 << 29, percent=50.0)

    def disk_usage(self, path: str) -> Usage:
        assert path.startswith(self.root)
        return Usage(total=100 << 30, used=91 << 30, percent=91.0)

    def disk_io_counters(self, perdisk: bool) -> dict[str, DiskIo]:
        k = self.n
        return {
            "sda": DiskIo(100 * k, 100 * k, 400 * k, 600 * k, 15000 * k),
            "sda1": DiskIo(1, 1, 1, 1, 1),  # a partition: not in /sys/block
            "loop0": DiskIo(1, 1, 1, 1, 1),
        }

    def net_io_counters(self, pernic: bool) -> dict[str, NetIo]:
        if self.net_fails:
            raise OSError("no /proc/net/dev")
        k = self.n
        return {
            "eth0": NetIo(30000 * k, 3000 * k, k, 0, 2 * k, 0),
            "lo": NetIo(1, 1, 0, 0, 0, 0),
            "veth1a2b": NetIo(1, 1, 0, 0, 0, 0),
        }

    def boot_time(self) -> float:
        return 1_000_000.0


@pytest.fixture
def fake_host(tmp_path: Path) -> tuple[HostPaths, FakePsutil]:
    proc, sys_, root = tmp_path / "proc", tmp_path / "sys", tmp_path / "rootfs"
    proc.mkdir()
    (proc / "vmstat").write_text("nr_free_pages 1000\noom_kill 3\npgfault 7\n")
    (sys_ / "block" / "sda").mkdir(parents=True)
    (sys_ / "block" / "loop0").mkdir(parents=True)
    (root / "var" / "lib" / "dpkg").mkdir(parents=True)
    (root / "run").mkdir()
    (root / "run" / "reboot-required").write_text("*** System restart required ***\n")
    notifier = root / "var" / "lib" / "update-notifier"
    notifier.mkdir(parents=True)
    (notifier / "updates-available").write_text(
        "\n12 updates can be applied immediately.\n5 of these updates are standard security "
        "updates.\nTo see these additional updates run: apt list --upgradable\n"
    )
    ps = FakePsutil()
    ps.root = str(root)  # type: ignore[attr-defined]
    return HostPaths(proc=str(proc), sys=str(sys_), root=str(root)), ps


def _by_name(samples: list[Sample]) -> dict[tuple[str, str], float]:
    return {(s.metric, ",".join(s.labels.values())): s.value for s in samples}


def test_sampler_with_fake_psutil_and_procfs(
    fake_host: tuple[HostPaths, FakePsutil], caplog: pytest.LogCaptureFixture
) -> None:
    paths, ps = fake_host
    t = [100.0]
    sampler = Sampler(paths=paths, mounts=("/",), ntp_server=None, ps=ps, clock=lambda: t[0])
    assert paths.proc == ps.PROCFS_PATH
    first = _by_name(sampler.sample())
    assert ("cpu.util_pct", "") not in first and ("net.rx_bps", "eth0") not in first  # no rates
    assert first[("mem.oom_kills", "")] == 3 and first[("system.reboot_required", "")] == 1
    assert first[("system.updates_pending", "")] == 12
    assert first[("system.security_updates", "")] == 5
    t[0] += 30
    ps.net_fails = True
    with caplog.at_level(logging.WARNING):
        got = _by_name(sampler.sample())
        sampler.sample()
    # cpu deltas: user 60, system 10, idle 10, iowait 5, steal 15 of 100
    assert got[("cpu.util_pct", "")] == pytest.approx(70)
    assert got[("cpu.steal_pct", "")] == pytest.approx(15)
    assert got[("cpu.iowait_pct", "")] == pytest.approx(5)
    assert got[("cpu.load5", "")] == 5 and got[("cpu.cores", "")] == 2
    assert got[("mem.available_pct", "")] == pytest.approx(6.25)
    assert got[("swap.used_pct", "")] == 50
    assert got[("disk.used_pct", "/")] == 91
    assert 0 <= got[("disk.inodes_used_pct", "/")] <= 100
    # sda: busy 15000 ms in 30 s = 50 %, 1000 ms for 200 operations = 5 ms
    assert got[("disk.io_util_pct", "sda")] == pytest.approx(50)
    assert got[("disk.io_latency_ms", "sda")] == pytest.approx(5)
    assert not any(k[1] in ("sda1", "loop0") for k in got)
    # the failing collector is skipped and logged once; the others still report
    assert not any(k[0].startswith("net.") for k in got)
    assert [r.message for r in caplog.records].count(
        "hostmon: network failed: OSError: no /proc/net/dev"
    ) == 1
    ps.net_fails = False
    sampler.sample()
    t[0] += 30
    net = _by_name(sampler.sample())
    assert net[("net.rx_bps", "eth0")] == pytest.approx(1000)
    assert net[("net.errors", "eth0")] == 1 and net[("net.drops", "eth0")] == 2
    assert ("net.rx_bps", "lo") not in net and ("net.rx_bps", "veth1a2b") not in net


def test_sampler_falls_back_without_host_files(tmp_path: Path) -> None:
    ps = FakePsutil()
    ps.root = ""  # type: ignore[attr-defined]
    sampler = Sampler(paths=HostPaths(proc=str(tmp_path), sys=str(tmp_path)), ps=ps)
    got = _by_name(sampler.sample())  # no vmstat, no /sys/block, no host root
    assert ("mem.oom_kills", "") not in got and ("system.reboot_required", "") not in got
    assert ("mem.available_pct", "") in got and ("system.uptime_s", "") in got


def test_parse_updates() -> None:
    assert parse_updates("0 updates can be applied immediately.\n") == (0, 0)
    text = "3 updates can be applied immediately.\n1 update is a security update"
    assert parse_updates(text) == (3, 1)
    assert parse_updates("") == (None, None)


def _ntp(t: float) -> bytes:
    return struct.pack("!II", int(t) + NTP_EPOCH_OFFSET, int((t % 1) * 2**32))


def _reply(sent: bytes, t2: float, t3: float, *, first: int = 0x24, stratum: int = 2) -> bytes:
    return bytes([first, stratum, 6, 0xEC]) + bytes(20) + sent + _ntp(t2) + _ntp(t3)


def test_sntp_offset_parsing() -> None:
    t1, t4 = 1_800_000_000.0, 1_800_000_000.100
    sent = _ntp(t1)
    # server clock 2.5 s ahead, 40 ms network each way, 20 ms processing
    offset, delay = parse_sntp(_reply(sent, t1 + 2.54, t1 + 2.56), sent, t1, t4)
    assert offset == pytest.approx(2.5, abs=1e-6) and delay == pytest.approx(0.08, abs=1e-6)
    with pytest.raises(ValueError, match="does not answer"):
        parse_sntp(_reply(_ntp(t1 - 5), t1, t1), sent, t1, t4)
    with pytest.raises(ValueError, match="stratum 0"):  # kiss-o'-death
        parse_sntp(_reply(sent, t1, t1, stratum=0), sent, t1, t4)
    with pytest.raises(ValueError, match="leap 3"):  # unsynchronised server
        parse_sntp(_reply(sent, t1, t1, first=0xE4), sent, t1, t4)
    with pytest.raises(ValueError, match="short"):
        parse_sntp(b"\x24", sent, t1, t4)


# ------------------------------------------------------------------- storage and rollup


def test_write_rollup_and_retention(db: Database, conn: Conn, db_url: str) -> None:
    hostmon = Database(make_conninfo(db_url, user="mdhub_hostmon"))
    conn.execute(
        """
        INSERT INTO ops.host_metric (ts, metric, labels, value)
        SELECT date_bin('5 minutes', now(), timestamptz '2000-01-01') - interval '1 hour'
               + make_interval(secs => 30 * i), 'cpu.util_pct', '{}'::jsonb, i
        FROM generate_series(0, 9) i
        UNION ALL
        SELECT now() - interval '8 days', 'disk.used_pct', '{"mount": "/"}'::jsonb, 42
        """
    )
    conn.execute(
        "INSERT INTO ops.host_metric_5m VALUES "
        "(now() - interval '91 days', 'cpu.util_pct', '{}', 1, 1, 1, 1)"
    )
    conn.commit()
    write_samples(hostmon, [Sample("cpu.util_pct", {}, 99.0)])  # current bucket: not rolled up
    deleted = purge_host_metrics(db)
    assert deleted == {"ops.host_metric": 1, "ops.host_metric_5m": 1}
    rows = conn.execute(
        "SELECT metric, n, min, avg, max FROM ops.host_metric_5m ORDER BY bucket"
    ).fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("disk.used_pct", 1, 42, 42, 42),  # rolled up before the raw row was deleted
        ("cpu.util_pct", 10, 0, 4.5, 9),  # 10 samples 30 s apart in one bucket
    ]
    # idempotent: a second run rewrites the newest bucket, adds nothing
    rollup(hostmon)
    assert conn.execute("SELECT count(*) AS n FROM ops.host_metric_5m").fetchone() == {"n": 2}
    latest = conn.execute("SELECT * FROM ops.v_host_status").fetchall()
    assert [(r["metric"], r["value"]) for r in latest] == [("cpu.util_pct", 99.0)]


# ------------------------------------------------------------------------ PostgreSQL stats


def test_pg_stats_against_postgres(
    db: Database, conn: Conn, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("CREATE TABLE public.churn (id int)")
    conn.execute("INSERT INTO public.churn SELECT generate_series(1, 500)")
    conn.commit()
    conn.execute("DELETE FROM public.churn WHERE id > 100")
    conn.execute("SELECT pg_stat_force_next_flush()")
    conn.commit()
    sampler = PgStatsSampler(db)
    with db.connection() as c:
        first = _by_name(sampler.sample(c))
    assert first[("pg.db_size_bytes", "")] > 1e6
    assert first[("pg.max_connections", "")] >= first[("pg.connections", "")] >= 1
    assert ("pg.checkpoints_req", "") in first and ("pg.waiting_locks", "") in first
    assert first[("pg.dead_tuples", "public.churn")] == 400
    assert ("pg.cache_hit_pct", "") not in first  # a rate: needs the previous sample
    with psycopg.connect(db_url) as idle_in_xact:
        idle_in_xact.execute("SELECT 1")  # leaves a transaction open
        conn.execute("SELECT pg_sleep(1.1)")
        sampler.run_once()
    conn.rollback()
    got = {
        r["metric"]: r["value"]
        for r in conn.execute("SELECT metric, value FROM ops.host_metric").fetchall()
    }
    assert got["pg.longest_xact_s"] >= 1
    assert 0 <= got["pg.cache_hit_pct"] <= 100 and got["pg.wal_bps"] >= 0

    def missing_view(c: Any) -> list[Sample]:
        c.execute("SELECT x FROM pg_stat_does_not_exist")
        return []

    monkeypatch.setattr("mdhub.hostmon.pgstats._dead_tuples", missing_view)
    with db.connection() as c:
        degraded = _by_name(sampler.sample(c))
    assert ("pg.db_size_bytes", "") in degraded and ("pg.checkpoints_req", "") in degraded
    assert not any(k[0] == "pg.dead_tuples" for k in degraded)


# ------------------------------------------------------------------------------ console

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fast_hasher(monkeypatch: pytest.MonkeyPatch) -> None:
    from argon2 import PasswordHasher

    from mdhub.admin import security

    monkeypatch.setattr(
        security, "HASHER", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    security._dummy_hash.cache_clear()


@pytest.fixture
def viewer(db_url: str, conn: Conn) -> Iterator[Console]:
    settings = Settings(
        database_url=make_conninfo(db_url, user="mdhub_admin"),
        admin_domain=RP_ID,
        admin_allow_cidrs="203.0.113.0/24",
        admin_secret_key="11" * 32,
        ntp_server="ntp.example",
    )
    with TestClient(
        create_app(settings), base_url=ORIGIN, client=(CLIENT_IP, 50000), follow_redirects=True
    ) as client:
        console = Console(client)
        user = store.create_user(conn, "vera", "viewer", created_by="test")
        token = store.create_enrollment(conn, user.id, "invite", "test")
        conn.commit()
        r = console.enroll(f"/enroll/{token}", PASSWORD, SoftAuthenticator())
        assert r.status_code == 200
        yield console


def _seed_machine(conn: Conn) -> None:
    latest = [
        ("cpu.util_pct", {}, 35.0),
        ("cpu.steal_pct", {}, 12.0),
        ("cpu.iowait_pct", {}, 1.0),
        ("cpu.load1", {}, 1.0),
        ("cpu.load5", {}, 1.5),
        ("cpu.load15", {}, 1.2),
        ("cpu.cores", {}, 2.0),
        ("mem.total_bytes", {}, 8e9),
        ("mem.available_bytes", {}, 4e9),
        ("mem.available_pct", {}, 50.0),
        ("swap.total_bytes", {}, 0.0),
        ("swap.used_bytes", {}, 0.0),
        ("swap.used_pct", {}, 0.0),
        ("mem.oom_kills", {}, 4.0),
        ("disk.total_bytes", {"mount": "/"}, 100e9),
        ("disk.used_bytes", {"mount": "/"}, 91e9),
        ("disk.used_pct", {"mount": "/"}, 91.0),
        ("disk.inodes_used_pct", {"mount": "/"}, 10.0),
        ("disk.io_util_pct", {"device": "vda"}, 3.0),
        ("disk.io_latency_ms", {"device": "vda"}, 0.7),
        ("net.rx_bps", {"iface": "eth0"}, 2048.0),
        ("net.tx_bps", {"iface": "eth0"}, 1024.0),
        ("net.errors", {"iface": "eth0"}, 0.0),
        ("net.drops", {"iface": "eth0"}, 3.0),
        ("system.uptime_s", {}, 10 * 86400.0),
        ("clock.offset_s", {}, -1.7),
        ("system.reboot_required", {}, 0.0),
        ("pg.db_size_bytes", {}, 2e9),
        ("pg.connections", {}, 90.0),
        ("pg.max_connections", {}, 100.0),
        ("pg.connections_pct", {}, 90.0),
        ("pg.longest_xact_s", {}, 60.0),
        ("pg.dead_tuples", {"table": "md.bond_eod"}, 1234.0),
    ]
    for metric, labels, value in latest:
        conn.execute(
            "INSERT INTO ops.host_metric (ts, metric, labels, value) "
            "VALUES (now() - interval '20 seconds', %s, %s, %s)",
            (metric, psycopg.types.json.Jsonb(labels), value),
        )
    # OOM kills: 2 an hour ago -> 2 new ones in the last hour
    conn.execute(
        "INSERT INTO ops.host_metric "
        "VALUES (now() - interval '50 minutes', 'mem.oom_kills', '{}', 2)"
    )
    # 7 days of disk growth (1 GB/day to 91 GB of 100 GB) and CPU history for the charts
    conn.execute(
        """
        INSERT INTO ops.host_metric_5m (bucket, metric, labels, n, min, avg, max)
        SELECT now() - make_interval(hours => h), 'disk.used_bytes', '{"mount": "/"}'::jsonb,
               10, v, v, v
        FROM generate_series(0, 167) h, LATERAL (SELECT 91e9 - h * 1e9 / 24 AS v) x
        UNION ALL
        SELECT now() - make_interval(mins => 5 * i), 'cpu.util_pct', '{}'::jsonb,
               10, 10, 30 + (i % 7), 80
        FROM generate_series(1, 280) i
        """
    )
    conn.commit()


def test_machine_page_renders_states_and_charts(viewer: Console, conn: Conn) -> None:
    _seed_machine(conn)
    page = viewer.get("/machine")
    assert page.status_code == 200
    assert page.headers["content-security-policy"] == CSP
    text = page.text
    assert '<a href="/machine" aria-current="page">Machine</a>' in text
    assert "<svg" in text and "polyline" in text and " style=" not in text
    assert "/ used" in text and "91.0 %" in text
    assert "projected" in text and "9 d" in text  # 9 GB left at 1 GB/day: warn (< 14 days)
    assert "Steal (5 min average)" in text and "12.0 %" in text
    assert "2 in the last hour" in text and "no swap configured" in text
    assert "against ntp.example" in text and "-1.70 s" in text
    assert "90 of 100 max_connections" in text and "Dead tuples md.bond_eod" in text
    assert "Updater not reporting" in text
    for state in ("crit", "warn"):
        assert f'<span class="pill {state}">{state}</span>' in text
    assert viewer.get("/machine?range=7d").status_code == 200
    assert viewer.get("/machine?range=1y").status_code == 422

    m = machine_status(conn, Settings())
    states = {c.label: c.state for c in m.checks}
    assert states["/ used"] == "crit" and states["/ full in"] == "warn"
    assert states["Steal (5 min average)"] == "warn" and states["OOM kills since boot"] == "crit"
    assert states["Clock offset (SNTP)"] == "crit" and states["Connections"] == "warn"
    assert states["Available"] == "ok" and states["Load (5 min)"] == "ok"
    assert states["Longest transaction"] == "ok" and states["Uptime"] == "ok"
    assert m.area_state("Containers") == "warn"
    relaxed = machine_status(
        conn,
        Settings(hostmon_disk_warn_pct=95, hostmon_disk_crit_pct=99, hostmon_disk_full_days=5),
    )
    assert {c.label: c.state for c in relaxed.checks}["/ used"] == "ok"
    assert {c.label: c.state for c in relaxed.checks}["/ full in"] == "ok"

    overview = viewer.get("/").text
    assert "<strong>Disks</strong>" in overview
    assert "Machine: Disks / used 91.0 % (crit)." in overview
    assert "the updater is not reporting container status" in overview


def test_machine_page_containers_from_updater(viewer: Console, conn: Conn) -> None:
    conn.execute(
        """
        INSERT INTO ops.container_status (name, service, image, state, health, restart_count,
                                          started_at, cpu_pct, mem_bytes, mem_limit_bytes)
        VALUES ('mdhub-worker-1', 'worker', 'mdhub:v1.2.0', 'running', NULL, 0,
                now() - interval '3 days', 2.5, 300e6, NULL),
               ('mdhub-api-1', 'api', 'mdhub:v1.2.0', 'running', 'healthy', 2,
                now() - interval '10 minutes', 1.0, 120e6, 512e6),
               ('mdhub-admin-1', 'admin', 'mdhub:v1.2.0', 'exited', NULL, 0, NULL, NULL, NULL,
                NULL)
        """
    )
    conn.commit()
    text = viewer.get("/machine").text
    assert "Updater not reporting" not in text and "mdhub:v1.2.0" in text
    m = machine_status(conn, Settings())
    assert m.containers_reporting
    assert m.container_states == {
        "mdhub-admin-1": "crit",
        "mdhub-api-1": "warn",
        "mdhub-worker-1": "ok",
    }
    conn.execute("UPDATE ops.container_status SET updated_at = now() - interval '5 minutes'")
    conn.commit()
    assert "Updater not reporting" in viewer.get("/machine").text


def test_machine_page_renders_the_updaters_report(viewer: Console, conn: Conn, db_url: str) -> None:
    """Rows written by the updater's ContainerReporter (fake docker) render with their states."""
    reporter = ContainerReporter(Database(db_url), docker=FakeDocker())
    reporter.write(reporter.sample())
    text = viewer.get("/machine").text
    assert "Updater not reporting" not in text
    for name in ("mdhub-worker-1", "mdhub-api-1", "mdhub-admin-1", "mdhub-postgres-1"):
        assert name in text
    assert "mdhub:1.2.0" in text and "postgres:16" in text
    assert "120.5 MB / 512.0 MB" in text and "2.5 %" in text
    conn.rollback()
    m = machine_status(conn, Settings())
    assert m.containers_reporting
    assert m.container_states == {
        "mdhub-admin-1": "crit",  # exited
        "mdhub-api-1": "warn",  # restarted 10 minutes ago
        "mdhub-postgres-1": "warn",  # health starting
        "mdhub-worker-1": "ok",
    }


def test_svg_chart_breaks_gaps_and_escapes() -> None:
    pts = [(0.0, 1.0, 2.0), (300.0, 2.0, 3.0), (3000.0, 1.0, 1.0), (3300.0, 5.0, 6.0)]
    svg = str(svg_chart(pts, t0=0, t1=3600, step=300, unit="pct", label="<x>", axes=True, limit=4))
    assert svg.count('class="avg"') == 2 and svg.count('class="peak"') == 2
    assert 'class="limit"' in svg and "&lt;x&gt;" in svg and "<x>" not in svg
    assert "no data" in str(svg_chart([], t0=0, t1=1, step=1, unit="pct", label="cpu"))
