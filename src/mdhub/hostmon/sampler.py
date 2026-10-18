"""``mdhub hostmon``: samples the host every 30 s into ``ops.host_metric`` (ADMIN_DESIGN §10).

In the compose service the host is visible through read-only mounts, like node_exporter:
``/proc`` at ``/host/proc``, ``/sys`` at ``/host/sys`` and ``/`` at ``/rootfs``. Without them
(a developer machine) the local ``/proc`` and ``/`` are read, so the sampler also runs on macOS
and Windows (psutil), with the Linux-only metrics missing.

Every collector is independent: one that fails is skipped and logged (a warning when it starts
failing, then quiet until it recovers), never the whole sample. Rates (CPU, disk IO, network)
are differences against the previous sample, so the first sample has none. All metrics of a
sample are written in one transaction; the SNTP clock check and the 5-minute rollup run every
:data:`SLOW_EVERY_S`.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NamedTuple

import psutil
from psycopg.types.json import Jsonb

from mdhub.db.pool import Database

__all__ = [
    "INTERVAL_S",
    "HostPaths",
    "Sample",
    "Sampler",
    "parse_sntp",
    "parse_updates",
    "run_forever",
    "sntp_offset",
    "write_samples",
]

log = logging.getLogger(__name__)

INTERVAL_S: Final = 30.0
SLOW_EVERY_S: Final = 300.0
"""Interval of the SNTP query (pool.ntp.org asks for no more than one per minute) and rollup."""
SKIP_NICS: Final = re.compile(r"^(lo\d*|veth.*|docker\d*|br-.*|virbr.*|gif\d*|stf\d*|utun\d*)$")
SKIP_DISKS: Final = re.compile(r"^(loop|ram|zram|sr|fd)\d*")
NTP_EPOCH_OFFSET: Final = 2_208_988_800
"""Seconds from 1900-01-01 (NTP era 0) to 1970-01-01."""


class Sample(NamedTuple):
    metric: str
    labels: dict[str, str]
    value: float


@dataclass(frozen=True, slots=True)
class HostPaths:
    """Where the host's ``/proc``, ``/sys`` and ``/`` are visible to this process."""

    proc: str = "/proc"
    sys: str = "/sys"
    root: str = ""
    """Prefix of host paths: ``/rootfs`` in the container, empty on the machine itself."""

    @classmethod
    def detect(cls) -> HostPaths:
        def pick(mounted: str, local: str) -> str:
            return mounted if os.path.isdir(mounted) else local

        return cls(
            proc=pick("/host/proc", "/proc"),
            sys=pick("/host/sys", "/sys"),
            root="/rootfs" if os.path.isdir("/rootfs") else "",
        )

    def host(self, path: str) -> str:
        return self.root + path if self.root else path


def _delta(now: Any, prev: Any) -> Callable[[str], float]:
    """Difference of one counter between two psutil named tuples (0 for a missing field)."""
    return lambda attr: float(getattr(now, attr, 0)) - float(getattr(prev, attr, 0))


@dataclass
class Sampler:
    """Collects one sample of every metric per :meth:`sample` call.

    :param mounts: host paths of the file systems to report (``/``, the docker volumes, the
        backup directory).
    :param ntp_server: SNTP server for ``clock.offset_s``; ``None`` = no clock check.
    :param ps: the psutil module (a fake in tests).
    """

    paths: HostPaths = field(default_factory=HostPaths)
    mounts: tuple[str, ...] = ("/",)
    ntp_server: str | None = None
    ps: Any = psutil
    clock: Callable[[], float] = time.monotonic
    _prev: dict[str, Any] = field(default_factory=dict)
    _prev_t: float | None = None
    _failing: set[str] = field(default_factory=set)
    _next_slow: float = 0.0

    def __post_init__(self) -> None:
        if hasattr(self.ps, "PROCFS_PATH"):  # Linux psutil reads /proc through this path
            self.ps.PROCFS_PATH = self.paths.proc

    def sample(self) -> list[Sample]:
        """Run every collector; a failing one is logged and contributes nothing."""
        now = self.clock()
        dt = now - self._prev_t if self._prev_t is not None else None
        self._prev_t = now
        collectors: list[tuple[str, Callable[[float | None], Iterable[Sample]]]] = [
            ("cpu", self._cpu),
            ("load", self._load),
            ("memory", self._memory),
            ("oom", self._oom),
            ("disk_space", self._disk_space),
            ("disk_io", self._disk_io),
            ("network", self._network),
            ("uptime", self._uptime),
            ("updates", self._updates),
        ]
        if self.ntp_server and now >= self._next_slow:
            collectors.append(("clock", self._clock))
        out: list[Sample] = []
        for name, collect in collectors:
            try:
                out.extend(collect(dt))
            except Exception as exc:
                if name not in self._failing:
                    log.warning("hostmon: %s failed: %s: %s", name, type(exc).__name__, exc)
                self._failing.add(name)
            else:
                if name in self._failing:
                    log.info("hostmon: %s recovered", name)
                self._failing.discard(name)
        return out

    def slow_due(self) -> bool:
        """True once per :data:`SLOW_EVERY_S` (the caller then runs the rollup)."""
        now = self.clock()
        if now < self._next_slow:
            return False
        self._next_slow = now + SLOW_EVERY_S
        return True

    # ------------------------------------------------------------------------- collectors

    def _cpu(self, dt: float | None) -> list[Sample]:
        times = self.ps.cpu_times()
        prev, self._prev["cpu"] = self._prev.get("cpu"), times
        if prev is None:
            return []
        delta = {f: float(getattr(times, f) - getattr(prev, f)) for f in times._fields}
        total = sum(delta.values())
        if total <= 0:
            return []

        def pct(*names: str) -> float:
            return 100.0 * sum(delta.get(n, 0.0) for n in names) / total

        idle = pct("idle", "iowait", "steal")
        out = [Sample("cpu.util_pct", {}, 100.0 - idle)]
        out += [Sample(f"cpu.{f}_pct", {}, pct(f)) for f in ("steal", "iowait") if f in delta]
        return out

    def _load(self, _dt: float | None) -> list[Sample]:
        load1, load5, load15 = self.ps.getloadavg()
        return [
            Sample("cpu.load1", {}, load1),
            Sample("cpu.load5", {}, load5),
            Sample("cpu.load15", {}, load15),
            Sample("cpu.cores", {}, float(self.ps.cpu_count() or 1)),
        ]

    def _memory(self, _dt: float | None) -> list[Sample]:
        vm = self.ps.virtual_memory()
        swap = self.ps.swap_memory()
        return [
            Sample("mem.total_bytes", {}, vm.total),
            Sample("mem.available_bytes", {}, vm.available),
            Sample("mem.available_pct", {}, 100.0 * vm.available / vm.total),
            Sample("mem.used_pct", {}, 100.0 - 100.0 * vm.available / vm.total),
            Sample("swap.total_bytes", {}, swap.total),
            Sample("swap.used_bytes", {}, swap.used),
            Sample("swap.used_pct", {}, swap.percent),
        ]

    def _oom(self, _dt: float | None) -> list[Sample]:
        text = Path(self.paths.proc, "vmstat").read_text()
        m = re.search(r"^oom_kill (\d+)$", text, re.M)
        return [Sample("mem.oom_kills", {}, float(m.group(1)))] if m else []

    def _disk_space(self, _dt: float | None) -> list[Sample]:
        out: list[Sample] = []
        for mount in self.mounts:
            path = self.paths.host(mount)
            labels = {"mount": mount}
            usage = self.ps.disk_usage(path)
            out += [
                Sample("disk.total_bytes", labels, usage.total),
                Sample("disk.used_bytes", labels, usage.used),
                Sample("disk.used_pct", labels, usage.percent),
            ]
            if hasattr(os, "statvfs"):
                st = os.statvfs(path)
                if st.f_files > 0:  # btrfs and some others report no inode limit
                    used = st.f_files - st.f_ffree
                    out.append(Sample("disk.inodes_used_pct", labels, 100.0 * used / st.f_files))
        return out

    def _disk_io(self, dt: float | None) -> list[Sample]:
        counters: dict[str, Any] = self.ps.disk_io_counters(perdisk=True) or {}
        block = Path(self.paths.sys, "block")
        if block.is_dir():  # whole disks only: partitions are not in /sys/block
            names = set(os.listdir(block))
            counters = {k: v for k, v in counters.items() if k in names}
        counters = {k: v for k, v in counters.items() if not SKIP_DISKS.match(k)}
        prev, self._prev["disk"] = self._prev.get("disk"), counters
        if prev is None or not dt:
            return []
        out: list[Sample] = []
        for dev, cur in counters.items():
            if dev not in prev:
                continue
            d = _delta(cur, prev[dev])
            labels = {"device": dev}
            if hasattr(cur, "busy_time"):  # Linux: ms the device was busy
                out.append(Sample("disk.io_util_pct", labels, min(100.0, d("busy_time") / dt / 10)))
            ops = d("read_count") + d("write_count")
            latency = (d("read_time") + d("write_time")) / ops if ops > 0 else 0.0
            out.append(Sample("disk.io_latency_ms", labels, latency))
        return out

    def _network(self, dt: float | None) -> list[Sample]:
        nics = {
            k: v
            for k, v in (self.ps.net_io_counters(pernic=True) or {}).items()
            if not SKIP_NICS.match(k)
        }
        prev, self._prev["net"] = self._prev.get("net"), nics
        if prev is None or not dt:
            return []
        out: list[Sample] = []
        for nic, cur in nics.items():
            if nic not in prev:
                continue
            raw = _delta(cur, prev[nic])

            def d(*attrs: str, raw: Callable[[str], float] = raw) -> float:
                return sum(max(0.0, raw(a)) for a in attrs)  # counters reset on reboot

            labels = {"iface": nic}
            out += [
                Sample("net.rx_bps", labels, d("bytes_recv") / dt),
                Sample("net.tx_bps", labels, d("bytes_sent") / dt),
                Sample("net.errors", labels, d("errin", "errout")),
                Sample("net.drops", labels, d("dropin", "dropout")),
            ]
        return out

    def _uptime(self, _dt: float | None) -> list[Sample]:
        boot = float(self.ps.boot_time())
        return [Sample("system.uptime_s", {}, time.time() - boot)]

    def _updates(self, _dt: float | None) -> list[Sample]:
        # Only where the host is visible and Debian-like; elsewhere the metric is absent.
        if not (self.paths.root and Path(self.paths.host("/var/lib/dpkg")).is_dir()):
            return []
        required = Path(self.paths.host("/run/reboot-required")).exists()
        out = [Sample("system.reboot_required", {}, float(required))]
        notifier = Path(self.paths.host("/var/lib/update-notifier/updates-available"))
        if notifier.is_file():
            pending, security = parse_updates(notifier.read_text(errors="replace"))
            if pending is not None:
                out.append(Sample("system.updates_pending", {}, float(pending)))
                out.append(Sample("system.security_updates", {}, float(security or 0)))
        return out

    def _clock(self, _dt: float | None) -> list[Sample]:
        assert self.ntp_server
        offset, delay = sntp_offset(self.ntp_server)
        return [Sample("clock.offset_s", {}, offset), Sample("clock.ntp_delay_s", {}, delay)]


# --------------------------------------------------------------------------------- parsers


def parse_updates(text: str) -> tuple[int | None, int | None]:
    """``(pending, security)`` from Ubuntu's ``update-notifier/updates-available`` text."""
    pending = re.search(r"(\d+) updates? can be (?:applied|installed)", text)
    security = re.search(
        r"(\d+) (?:of these )?updates? (?:is a|are|is) (?:standard )?security", text
    )
    if pending is None:
        return (None, None) if "can be applied" not in text else (0, 0)
    return int(pending.group(1)), int(security.group(1)) if security else 0


def _ntp_time(data: bytes, offset: int) -> float:
    secs, frac = struct.unpack("!II", data[offset : offset + 8])
    return float(secs - NTP_EPOCH_OFFSET + frac / 2**32)


def _ntp_stamp(t: float) -> bytes:
    secs = int(t) + NTP_EPOCH_OFFSET
    return struct.pack("!II", secs, int((t % 1) * 2**32))


def parse_sntp(data: bytes, sent: bytes, t1: float, t4: float) -> tuple[float, float]:
    """Clock offset and round-trip delay (seconds) from an SNTP server reply (RFC 4330).

    :param sent: the transmit timestamp of our request, which the server echoes as originate
        timestamp (a reply that does not echo it is not ours).
    :param t1: our send time; :param t4: our receive time (both ``time.time()``).
    Raises :class:`ValueError` for a malformed, spoofed or unsynchronised reply.
    """
    if len(data) < 48:
        raise ValueError("short SNTP reply")
    leap, mode, stratum = data[0] >> 6, data[0] & 7, data[1]
    if mode not in (4, 5) or leap == 3 or not 1 <= stratum <= 15:
        raise ValueError(f"unusable SNTP reply (mode {mode}, leap {leap}, stratum {stratum})")
    if data[24:32] != sent:
        raise ValueError("SNTP reply does not answer our request")
    t2, t3 = _ntp_time(data, 32), _ntp_time(data, 40)
    return ((t2 - t1) + (t3 - t4)) / 2, (t4 - t1) - (t3 - t2)


def sntp_offset(server: str, *, timeout: float = 3.0) -> tuple[float, float]:
    """Query ``server`` (UDP 123); returns ``(offset_s, delay_s)``, positive = our clock behind."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 123))
        t1 = time.time()
        sent = _ntp_stamp(t1)
        sock.send(b"\x23" + bytes(39) + sent)  # LI 0, version 4, mode 3 (client)
        data = sock.recv(512)
        t4 = time.time()
    return parse_sntp(data, sent, t1, t4)


# ---------------------------------------------------------------------------------- storage

_INSERT: Final = "INSERT INTO ops.host_metric (ts, metric, labels, value) VALUES (%s, %s, %s, %s)"


def write_samples(db: Database, samples: list[Sample], ts: datetime | None = None) -> None:
    """Insert ``samples`` with one timestamp in one transaction."""
    if not samples:
        return
    at = ts or datetime.now(UTC)
    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany(_INSERT, [(at, s.metric, Jsonb(s.labels), s.value) for s in samples])


def rollup(db: Database) -> None:
    with db.connection() as conn:
        conn.execute("SELECT ops.host_metric_rollup()")


def run_forever(
    db: Database, sampler: Sampler, stop: threading.Event, *, interval_s: float = INTERVAL_S
) -> None:
    """Sample, write and (every 5 minutes) roll up until ``stop`` is set."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            write_samples(db, sampler.sample())
            if sampler.slow_due():
                rollup(db)
        except Exception as exc:  # database down: keep sampling, the next write may work
            log.warning("hostmon: write failed: %s", type(exc).__name__)
        stop.wait(max(1.0, interval_s - (time.monotonic() - started)))
