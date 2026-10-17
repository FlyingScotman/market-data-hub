"""Process heartbeat: one ``ops.worker_heartbeat`` row per process instance (ADMIN_DESIGN §3.2).

The worker (``mdhub scheduler``) starts a :class:`Heartbeat` that upserts its row every
:data:`HEARTBEAT_INTERVAL_S` (30 s) from a daemon thread: process name, host, pid, version,
``started_at``, ``beat_at`` and the jobs running in the process (``running`` callable, e.g.
:func:`mdhub.jobs.running_jobs`). :meth:`Heartbeat.stop` sets ``stopped_at`` on a clean
shutdown, so the console can tell "stopped" (``stopped_at`` set) from "died" (stale ``beat_at``
without ``stopped_at``). A failed beat is logged (exception type only) and retried at the next
interval; it never stops the process. The updater (ADMIN_DESIGN §6) will use the same class.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any, Final

from psycopg.types.json import Jsonb

from mdhub import __version__
from mdhub.core.capture import json_default
from mdhub.db.pool import Database

__all__ = ["HEARTBEAT_INTERVAL_S", "Heartbeat"]

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S: Final = 30.0

_UPSERT: Final = """
    INSERT INTO ops.worker_heartbeat
        (instance_id, process, host, pid, version, started_at, beat_at, stopped_at, running_jobs)
    VALUES (%(id)s, %(process)s, %(host)s, %(pid)s, %(version)s, %(started_at)s, now(),
            %(stopped_at)s, %(running)s)
    ON CONFLICT (instance_id) DO UPDATE SET
        beat_at = EXCLUDED.beat_at,
        version = EXCLUDED.version,
        stopped_at = EXCLUDED.stopped_at,
        running_jobs = EXCLUDED.running_jobs
"""


def _jsonable(item: Any) -> Any:
    return asdict(item) if is_dataclass(item) and not isinstance(item, type) else item


class Heartbeat:
    """Upserts this process's ``ops.worker_heartbeat`` row every ``interval_s`` seconds.

    :param db: database (a pooled one is fine: one short transaction per beat).
    :param process: process kind (``worker``, ``updater``).
    :param running: returns the running jobs (dataclasses or dicts; stored as a JSON array).
    """

    def __init__(
        self,
        db: Database,
        process: str = "worker",
        *,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        running: Callable[[], Sequence[Any]] = list,
        version: str = __version__,
    ) -> None:
        self.db = db
        self.process = process
        self.interval_s = interval_s
        self.running = running
        self.version = version
        self.instance_id = uuid.uuid4()
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.started_at = datetime.now(UTC)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def beat(self, *, stopped: bool = False) -> None:
        """Write the row now (raises on database errors)."""
        jobs = [_jsonable(j) for j in self.running()]
        with self.db.connection() as conn:
            conn.execute(
                _UPSERT,
                {
                    "id": self.instance_id,
                    "process": self.process,
                    "host": self.host,
                    "pid": self.pid,
                    "version": self.version,
                    "started_at": self.started_at,
                    "stopped_at": datetime.now(UTC) if stopped else None,
                    "running": Jsonb(jobs, dumps=_dumps),
                },
            )

    def _safe_beat(self, *, stopped: bool = False) -> None:
        try:
            self.beat(stopped=stopped)
        except Exception as exc:
            log.warning("heartbeat failed: %s", type(exc).__name__)

    def _run(self) -> None:
        while True:
            self._safe_beat()
            if self._stop.wait(self.interval_s):
                return

    def start(self) -> None:
        """Beat now and then every ``interval_s`` in a daemon thread."""
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mdhub-heartbeat", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread and record ``stopped_at``."""
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(timeout)
        self._safe_beat(stopped=True)


def _dumps(value: Any) -> str:
    return json.dumps(value, default=json_default, ensure_ascii=False)
