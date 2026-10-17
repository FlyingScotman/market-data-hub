"""PostgreSQL statistics for the Machine page, sampled by the worker every minute (§10).

Reads only ``pg_stat_*`` views and ordinary functions (no superuser-only or ``pg_monitor``
functions such as ``pg_ls_waldir``). Each query runs in its own savepoint: one that fails
(a column missing in another PostgreSQL version, a view the role may not read) is skipped and
logged once, the others are still written. Sessions of other roles show no times in
``pg_stat_activity`` without ``pg_read_all_stats``; they are then simply not counted in the
longest transaction / query. WAL is reported as the generation rate (``pg_stat_wal``), not
the size of ``pg_wal`` (that needs ``pg_ls_waldir``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import psycopg

from mdhub.db.pool import Database, DictConnection
from mdhub.hostmon.sampler import Sample, write_samples

__all__ = ["INTERVAL_S", "PgStatsSampler"]

log = logging.getLogger(__name__)

INTERVAL_S: Final = 60
TOP_DEAD_TABLES: Final = 5


@dataclass
class PgStatsSampler:
    """``sample(conn)`` collects; :meth:`run_once` collects and writes (the scheduler job)."""

    db: Database
    _prev: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    _failing: set[str] = field(default_factory=set)

    def run_once(self) -> None:
        try:
            with self.db.connection() as conn:
                samples = self.sample(conn)
            write_samples(self.db, samples)
        except Exception as exc:  # never break the scheduler thread
            log.warning("pg stats failed: %s", type(exc).__name__)

    def sample(self, conn: DictConnection) -> list[Sample]:
        out: list[Sample] = []
        queries: list[tuple[str, Callable[[DictConnection], list[Sample]]]] = [
            ("size", _size),
            ("connections", _connections),
            ("activity", _activity),
            ("cache", self._cache),
            ("dead_tuples", _dead_tuples),
            ("autovacuum", _autovacuum),
            ("wal", self._wal),
            ("checkpoints", _checkpoints),
        ]
        for name, query in queries:
            try:
                with conn.transaction():
                    out += query(conn)
            except psycopg.Error as exc:
                if name not in self._failing:
                    log.warning("pg stats: %s unavailable: %s", name, type(exc).__name__)
                self._failing.add(name)
            else:
                self._failing.discard(name)
        return out

    def _change(
        self, key: str, value: float, extra: float = 0.0
    ) -> tuple[float, float, float] | None:
        """Change of the counters ``value`` and ``extra`` and the seconds since the last sample."""
        now = time.monotonic()
        prev, self._prev[key] = self._prev.get(key), (now, value, extra)
        if prev is None or now <= prev[0] or value < prev[1]:  # first sample or stats reset
            return None
        return value - prev[1], extra - prev[2], now - prev[0]

    def _cache(self, conn: DictConnection) -> list[Sample]:
        row = _row(
            conn,
            "SELECT blks_hit::float8 AS hit, blks_read::float8 AS read FROM pg_stat_database "
            "WHERE datname = current_database()",
        )
        change = self._change("cache", row["hit"], row["read"])
        if change is None:
            return []
        hit, read, _ = change
        return [Sample("pg.cache_hit_pct", {}, 100.0 * hit / (hit + read) if hit + read else 100.0)]

    def _wal(self, conn: DictConnection) -> list[Sample]:
        wal = _row(conn, "SELECT wal_bytes::float8 AS v FROM pg_stat_wal")["v"]
        change = self._change("wal", wal)
        return [] if change is None else [Sample("pg.wal_bps", {}, change[0] / change[2])]


def _row(conn: DictConnection, query: str) -> Any:
    row = conn.execute(query).fetchone()
    assert row is not None
    return row


def _size(conn: DictConnection) -> list[Sample]:
    v = _row(conn, "SELECT pg_database_size(current_database())::float8 AS v")["v"]
    return [Sample("pg.db_size_bytes", {}, v)]


def _connections(conn: DictConnection) -> list[Sample]:
    row = _row(
        conn,
        """
        SELECT count(*) FILTER (WHERE backend_type = 'client backend')::float8 AS used,
               current_setting('max_connections')::float8 AS max
        FROM pg_stat_activity
        """,
    )
    return [
        Sample("pg.connections", {}, row["used"]),
        Sample("pg.max_connections", {}, row["max"]),
        Sample("pg.connections_pct", {}, 100.0 * row["used"] / row["max"]),
    ]


def _activity(conn: DictConnection) -> list[Sample]:
    row = _row(
        conn,
        """
        SELECT coalesce(max(extract(epoch FROM now() - xact_start)), 0)::float8 AS xact,
               coalesce(max(extract(epoch FROM now() - query_start))
                        FILTER (WHERE state = 'active'), 0)::float8 AS query,
               count(*) FILTER (WHERE wait_event_type = 'Lock')::float8 AS locks
        FROM pg_stat_activity
        WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
        """,
    )
    return [
        Sample("pg.longest_xact_s", {}, row["xact"]),
        Sample("pg.longest_query_s", {}, row["query"]),
        Sample("pg.waiting_locks", {}, row["locks"]),
    ]


def _dead_tuples(conn: DictConnection) -> list[Sample]:
    rows = conn.execute(
        """
        SELECT schemaname || '.' || relname AS name, n_dead_tup::float8 AS dead
        FROM pg_stat_user_tables WHERE n_dead_tup > 0
        ORDER BY n_dead_tup DESC LIMIT %s
        """,
        (TOP_DEAD_TABLES,),
    ).fetchall()
    return [Sample("pg.dead_tuples", {"table": r["name"]}, r["dead"]) for r in rows]


def _autovacuum(conn: DictConnection) -> list[Sample]:
    v = _row(
        conn,
        "SELECT extract(epoch FROM now() - max(last_autovacuum))::float8 AS v "
        "FROM pg_stat_user_tables",
    )["v"]
    return [] if v is None else [Sample("pg.last_autovacuum_age_s", {}, v)]


def _checkpoints(conn: DictConnection) -> list[Sample]:
    if conn.info.server_version >= 170000:
        query = (
            "SELECT num_timed::float8 AS timed, num_requested::float8 AS req "
            "FROM pg_stat_checkpointer"
        )
    else:
        query = (
            "SELECT checkpoints_timed::float8 AS timed, checkpoints_req::float8 AS req "
            "FROM pg_stat_bgwriter"
        )
    row = _row(conn, query)
    return [
        Sample("pg.checkpoints_timed", {}, row["timed"]),
        Sample("pg.checkpoints_req", {}, row["req"]),
    ]
