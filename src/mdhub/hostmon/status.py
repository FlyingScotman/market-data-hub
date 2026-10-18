"""States of the machine for the Machine page and the Overview lights (ADMIN_DESIGN §10).

Reads ``ops.v_host_status`` (latest value per series), a few windows of ``ops.host_metric``
and ``ops.host_metric_5m``, and ``ops.container_status``; classifies against the
``MDHUB_HOSTMON_*`` thresholds of :class:`~mdhub.config.Settings`:

========================== ===================================================== =========
check                      rule (defaults)                                       state
========================== ===================================================== =========
disk / inodes used         >= 80 % warn, >= 90 % crit                            warn/crit
disk full                  linear fit over 7 days reaches 100 % within 14 days   warn
memory available           below 10 %                                            crit
swap in use                above 50 %                                            warn
CPU steal                  5-minute average above 10 %                           warn
load                       5-minute load above 2 x cores                         warn
clock offset               more than 1 s from MDHUB_NTP_SERVER                   crit
OOM kills                  any in the last hour                                  crit
uptime                     rebooted in the last hour                             warn
reboot required / security pending (Debian/Ubuntu host files)                    warn
PostgreSQL connections     above 80 % of max_connections                         warn
PostgreSQL transaction     open longer than 30 minutes                           warn
container                  not running or unhealthy: crit; restarted in the last
                           hour or health starting: warn
samplers                   hostmon silent > 2 min, pg stats silent > 3 min       warn
========================== ===================================================== =========
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from mdhub.config import Settings
from mdhub.db.pool import DictConnection

__all__ = [
    "AREAS",
    "Check",
    "Machine",
    "State",
    "above",
    "format_value",
    "machine_status",
    "project_full",
    "series_key",
    "worst",
]

State = Literal["ok", "warn", "crit"]
Unit = Literal["pct", "bytes", "bps", "s", "ms", "count", "load", "days"]
AREAS: Final = ("CPU", "Memory", "Disks", "Network", "System", "PostgreSQL")
HOSTMON_STALE_S: Final = 120
PGSTATS_STALE_S: Final = 180
CONTAINERS_STALE_S: Final = 120
_RANK: Final = {"ok": 0, "warn": 1, "crit": 2}


def worst(states: Iterable[State]) -> State:
    return max(states, key=_RANK.__getitem__, default="ok")


def above(value: float, warn: float, crit: float | None = None) -> State:
    """``crit`` at or above ``crit``, ``warn`` at or above ``warn``, else ``ok``."""
    if crit is not None and value >= crit:
        return "crit"
    return "warn" if value >= warn else "ok"


def project_full(points: Sequence[tuple[float, float]], total: float) -> float | None:
    """Epoch seconds at which a linear fit of ``(epoch, used_bytes)`` reaches ``total``.

    ``None`` when the disk is not growing or the data are too thin for a trend (fewer than
    12 points or less than 6 hours).
    """
    if len(points) < 12 or points[-1][0] - points[0][0] < 6 * 3600:
        return None
    xs, ys = zip(*points, strict=True)
    slope, intercept = statistics.linear_regression(xs, ys)
    if slope <= 0:
        return None
    return (total - intercept) / slope


@dataclass(frozen=True, slots=True)
class Check:
    area: str
    label: str
    value: float | None
    unit: Unit
    state: State = "ok"
    note: str = ""
    series: tuple[str, str] | None = None
    """``(metric, labels as sorted JSON)`` of the sparkline, if any."""
    limit: float | None = None
    """Threshold drawn on the chart."""


@dataclass(slots=True)
class Machine:
    now: datetime
    checks: list[Check] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=list)
    container_states: dict[str, State] = field(default_factory=dict)
    containers_reporting: bool = False

    def area_state(self, area: str) -> State:
        if area == "Containers":
            return worst(self.container_states.values()) if self.containers_reporting else "warn"
        return worst(c.state for c in self.checks if c.area == area)

    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.state != "ok"]


def series_key(labels: dict[str, Any]) -> str:
    return json.dumps(labels, sort_keys=True, separators=(",", ":"))


def format_value(value: float | None, unit: str) -> str:
    """Human text of a metric value in ``unit``."""
    if value is None:
        return "-"
    if unit in ("bytes", "bps"):
        size = float(value)
        for prefix in ("", "K", "M", "G", "T"):
            if abs(size) < 1024 or prefix == "T":
                break
            size /= 1024
        text = f"{size:.0f} B" if not prefix else f"{size:.1f} {prefix}B"
        return text + ("/s" if unit == "bps" else "")
    if unit == "s":
        s = abs(value)
        if s < 60:
            return f"{value:.2f} s"
        if s < 5400:
            return f"{s / 60:.0f} min"
        return f"{s / 3600:.1f} h" if s < 172800 else f"{s / 86400:.0f} d"
    if unit == "pct":
        return f"{value:.1f} %"
    if unit == "ms":
        return f"{value:.1f} ms"
    if unit == "load":
        return f"{value:.2f}"
    if unit == "days":
        return f"{value:.0f} d"
    return f"{value:,.0f}".replace(",", " ")


def machine_status(conn: DictConnection, settings: Settings) -> Machine:
    """Current values and states of every check (one read-only transaction)."""
    s = settings
    now: datetime = conn.execute("SELECT now() AS v").fetchone()["v"]  # type: ignore[index]
    m = Machine(now=now)
    latest: dict[tuple[str, str], tuple[datetime, float]] = {}
    labelsets: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("SELECT metric, labels, ts, value FROM ops.v_host_status"):
        latest[(r["metric"], series_key(r["labels"]))] = (r["ts"], r["value"])
        labelsets.setdefault(r["metric"], []).append(r["labels"])

    def v(metric: str, **labels: str) -> float | None:
        hit = latest.get((metric, series_key(labels)))
        return hit[1] if hit else None

    def add(
        area: str,
        label: str,
        metric: str | None,
        unit: Unit,
        state: State = "ok",
        note: str = "",
        *,
        value: float | None = None,
        labels: dict[str, str] | None = None,
        limit: float | None = None,
    ) -> None:
        lab = labels or {}
        val = value if value is not None or metric is None else v(metric, **lab)
        series = (metric, series_key(lab)) if metric else None
        m.checks.append(Check(area, label, val, unit, state, note, series, limit))

    def scalar(query: str, *params: Any) -> Any:
        row = conn.execute(query, params).fetchone()
        return row["v"] if row else None

    # --- samplers
    host_ts = max(
        (ts for (k, _), (ts, _v) in latest.items() if not k.startswith("pg.")), default=None
    )
    pg_ts = max((ts for (k, _), (ts, _v) in latest.items() if k.startswith("pg.")), default=None)

    def age(ts: datetime | None) -> float | None:
        return (now - ts).total_seconds() if ts else None

    host_age, pg_age = age(host_ts), age(pg_ts)

    # --- CPU
    add("CPU", "Utilisation", "cpu.util_pct", "pct")
    steal5 = scalar(
        "SELECT avg(value) AS v FROM ops.host_metric "
        "WHERE metric = 'cpu.steal_pct' AND ts > now() - interval '5 minutes'"
    )
    add(
        "CPU",
        "Steal (5 min average)",
        "cpu.steal_pct",
        "pct",
        above(steal5, s.hostmon_steal_max_pct) if steal5 is not None else "ok",
        "time taken by other guests on the VPS host",
        value=steal5,
        limit=s.hostmon_steal_max_pct,
    )
    add("CPU", "IO wait", "cpu.iowait_pct", "pct")
    load5, cores = v("cpu.load5"), v("cpu.cores")
    if load5 is not None and cores:
        limit = s.hostmon_load_per_core_max * cores
        add(
            "CPU",
            "Load (5 min)",
            "cpu.load5",
            "load",
            above(load5, limit),
            f"1/5/15 min {v('cpu.load1') or 0:.2f} / {load5:.2f} / {v('cpu.load15') or 0:.2f} "
            f"on {cores:.0f} cores",
            limit=limit,
        )

    # --- memory
    avail = v("mem.available_pct")
    if avail is not None:
        add(
            "Memory",
            "Available",
            "mem.available_pct",
            "pct",
            "crit" if avail < s.hostmon_mem_available_min_pct else "ok",
            f"{format_value(v('mem.available_bytes'), 'bytes')} of "
            f"{format_value(v('mem.total_bytes'), 'bytes')}",
            limit=s.hostmon_mem_available_min_pct,
        )
    swap = v("swap.used_pct")
    if swap is not None:
        no_swap = not v("swap.total_bytes")
        add(
            "Memory",
            "Swap in use",
            "swap.used_pct",
            "pct",
            "ok" if no_swap else above(swap, s.hostmon_swap_max_pct),
            "no swap configured"
            if no_swap
            else f"{format_value(v('swap.used_bytes'), 'bytes')} of "
            f"{format_value(v('swap.total_bytes'), 'bytes')}",
            limit=s.hostmon_swap_max_pct,
        )
    oom = v("mem.oom_kills")
    if oom is not None:
        oom_min = scalar(
            "SELECT min(value) AS v FROM ops.host_metric "
            "WHERE metric = 'mem.oom_kills' AND ts > now() - interval '1 hour'"
        )
        recent = max(0.0, oom - (oom_min if oom_min is not None else oom))
        add(
            "Memory",
            "OOM kills since boot",
            "mem.oom_kills",
            "count",
            "crit" if recent > 0 else "ok",
            f"{recent:.0f} in the last hour",
        )

    # --- disks
    totals = {
        r["labels"].get("mount", ""): r["value"]
        for r in conn.execute(
            "SELECT labels, value FROM ops.v_host_status WHERE metric = 'disk.total_bytes'"
        )
    }
    trend: dict[str, list[tuple[float, float]]] = {}
    for r in conn.execute(
        """
        SELECT labels ->> 'mount' AS mount, extract(epoch FROM bucket)::float8 AS t, avg
        FROM ops.host_metric_5m
        WHERE metric = 'disk.used_bytes' AND bucket > now() - interval '7 days'
        ORDER BY bucket
        """
    ):
        trend.setdefault(r["mount"], []).append((r["t"], r["avg"]))
    for lab in sorted(labelsets.get("disk.used_pct", []), key=lambda x: str(x.get("mount"))):
        mount = str(lab.get("mount", ""))
        pct = v("disk.used_pct", mount=mount) or 0.0
        add(
            "Disks",
            f"{mount} used",
            "disk.used_pct",
            "pct",
            above(pct, s.hostmon_disk_warn_pct, s.hostmon_disk_crit_pct),
            f"{format_value(v('disk.used_bytes', mount=mount), 'bytes')} of "
            f"{format_value(totals.get(mount), 'bytes')}",
            labels={"mount": mount},
            limit=s.hostmon_disk_warn_pct,
        )
        inodes = v("disk.inodes_used_pct", mount=mount)
        if inodes is not None:
            add(
                "Disks",
                f"{mount} inodes used",
                "disk.inodes_used_pct",
                "pct",
                above(inodes, s.hostmon_disk_warn_pct, s.hostmon_disk_crit_pct),
                labels={"mount": mount},
                limit=s.hostmon_disk_warn_pct,
            )
        full = project_full(trend.get(mount, []), totals.get(mount) or 0.0)
        days = (full - now.timestamp()) / 86400 if full is not None else None
        add(
            "Disks",
            f"{mount} full in",
            None,
            "days",
            "warn" if days is not None and days < s.hostmon_disk_full_days else "ok",
            f"projected {datetime.fromtimestamp(full, now.tzinfo):%Y-%m-%d} (linear, 7 days)"
            if full is not None and days is not None and days < 3650
            else "not filling up (7-day trend)",
            value=max(0.0, days) if days is not None and days < 3650 else None,
        )
    for lab in sorted(labelsets.get("disk.io_latency_ms", []), key=lambda x: str(x.get("device"))):
        dev = str(lab.get("device", ""))
        if v("disk.io_util_pct", device=dev) is not None:
            add("Disks", f"{dev} IO utilisation", "disk.io_util_pct", "pct", labels={"device": dev})
        add("Disks", f"{dev} IO latency", "disk.io_latency_ms", "ms", labels={"device": dev})

    # --- network
    errors: dict[tuple[str, str], float] = {
        (r["metric"], str(r["iface"])): r["v"]
        for r in conn.execute(
            """
            SELECT metric, labels ->> 'iface' AS iface, sum(value) AS v FROM ops.host_metric
            WHERE metric IN ('net.errors', 'net.drops') AND ts > now() - interval '24 hours'
            GROUP BY 1, 2
            """
        )
    }
    for lab in sorted(labelsets.get("net.rx_bps", []), key=lambda x: str(x.get("iface"))):
        nic = str(lab.get("iface", ""))
        add("Network", f"{nic} receive", "net.rx_bps", "bps", labels={"iface": nic})
        add("Network", f"{nic} transmit", "net.tx_bps", "bps", labels={"iface": nic})
        add(
            "Network",
            f"{nic} errors / drops (24 h)",
            None,
            "count",
            value=errors.get(("net.errors", nic), 0.0) + errors.get(("net.drops", nic), 0.0),
            note=f"{errors.get(('net.errors', nic), 0):.0f} errors, "
            f"{errors.get(('net.drops', nic), 0):.0f} drops",
        )

    # --- system
    add(
        "System",
        "Sampler (hostmon)",
        None,
        "s",
        "warn" if host_age is None or host_age > HOSTMON_STALE_S else "ok",
        "last sample " + (f"{host_age:.0f} s ago" if host_age is not None else "never"),
        value=host_age,
    )
    uptime = v("system.uptime_s")
    if uptime is not None:
        booted = now - timedelta(seconds=uptime)
        add(
            "System",
            "Uptime",
            "system.uptime_s",
            "s",
            "warn" if uptime < 3600 else "ok",
            f"booted {booted:%Y-%m-%d %H:%M} UTC"
            + (" (rebooted recently)" if uptime < 3600 else ""),
        )
    offset = v("clock.offset_s")
    add(
        "System",
        "Clock offset (SNTP)",
        "clock.offset_s",
        "s",
        "crit" if offset is not None and abs(offset) > s.hostmon_clock_offset_max_s else "ok",
        f"against {s.ntp_server}" if offset is not None else "no SNTP result (MDHUB_NTP_SERVER)",
        limit=s.hostmon_clock_offset_max_s,
    )
    reboot = v("system.reboot_required")
    if reboot is not None:
        add(
            "System",
            "Reboot required",
            None,
            "count",
            "warn" if reboot else "ok",
            "yes" if reboot else "no",
            value=reboot,
        )
    security = v("system.security_updates")
    if security is not None:
        add(
            "System",
            "Security updates pending",
            None,
            "count",
            "warn" if security > 0 else "ok",
            f"{v('system.updates_pending') or 0:.0f} updates pending in total",
            value=security,
        )

    # --- PostgreSQL
    add(
        "PostgreSQL",
        "Statistics (worker)",
        None,
        "s",
        "warn" if pg_age is None or pg_age > PGSTATS_STALE_S else "ok",
        "last sample " + (f"{pg_age:.0f} s ago" if pg_age is not None else "never"),
        value=pg_age,
    )
    size, size_day = (
        v("pg.db_size_bytes"),
        scalar(
            "SELECT avg AS v FROM ops.host_metric_5m WHERE metric = 'pg.db_size_bytes' "
            "AND bucket > now() - interval '24 hours' ORDER BY bucket LIMIT 1"
        ),
    )
    if size is not None:
        growth = f"{format_value(size - size_day, 'bytes')} in 24 h" if size_day is not None else ""
        add("PostgreSQL", "Database size", "pg.db_size_bytes", "bytes", note=growth)
    conns = v("pg.connections_pct")
    if conns is not None:
        add(
            "PostgreSQL",
            "Connections",
            "pg.connections_pct",
            "pct",
            above(conns, s.hostmon_pg_connections_max_pct),
            f"{v('pg.connections') or 0:.0f} of {v('pg.max_connections') or 0:.0f} max_connections",
            limit=s.hostmon_pg_connections_max_pct,
        )
    xact = v("pg.longest_xact_s")
    if xact is not None:
        add(
            "PostgreSQL",
            "Longest transaction",
            "pg.longest_xact_s",
            "s",
            above(xact, s.hostmon_pg_xact_max_minutes * 60),
            limit=s.hostmon_pg_xact_max_minutes * 60,
        )
    for label, metric, unit in (
        ("Longest query", "pg.longest_query_s", "s"),
        ("Waiting for locks", "pg.waiting_locks", "count"),
        ("Cache hit ratio", "pg.cache_hit_pct", "pct"),
        ("Last autovacuum", "pg.last_autovacuum_age_s", "s"),
        ("WAL written", "pg.wal_bps", "bps"),
    ):
        if v(metric) is not None:
            add("PostgreSQL", label, metric, unit)  # type: ignore[arg-type]
    if v("pg.checkpoints_req") is not None:
        add(
            "PostgreSQL",
            "Requested checkpoints",
            "pg.checkpoints_req",
            "count",
            note=f"{v('pg.checkpoints_timed') or 0:.0f} timed (since statistics reset)",
        )
    dead = sorted(
        (
            (str(lab.get("table")), v("pg.dead_tuples", **lab) or 0.0)
            for lab in labelsets.get("pg.dead_tuples", [])
        ),
        key=lambda x: -x[1],
    )
    for table, n in dead:
        add("PostgreSQL", f"Dead tuples {table}", None, "count", value=n)

    # --- containers (written by the updater)
    m.containers = conn.execute("SELECT * FROM ops.container_status ORDER BY name").fetchall()
    newest = max((c["updated_at"] for c in m.containers), default=None)
    m.containers_reporting = (
        newest is not None and (now - newest).total_seconds() <= CONTAINERS_STALE_S
    )
    for c in m.containers:
        restarted = (
            c["restart_count"] > 0
            and c["started_at"] is not None
            and now - c["started_at"] < timedelta(hours=1)
        )
        m.container_states[c["name"]] = (
            "crit"
            if c["state"] != "running" or c["health"] == "unhealthy"
            else "warn"
            if restarted or c["health"] == "starting"
            else "ok"
        )
    return m
