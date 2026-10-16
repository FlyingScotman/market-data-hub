"""Connection factory / pool for ``MDHUB_DATABASE_URL``.

Every connection created here:

* uses :func:`psycopg.rows.dict_row` (rows are ``dict[str, Any]``),
* runs with session ``TimeZone = 'UTC'`` (convert business dates explicitly with
  ``AT TIME ZONE 'Europe/Moscow'`` in SQL, or in Python via :mod:`mdhub.core.timeutil`),
* has ``application_name = 'mdhub'`` unless the conninfo sets one.

Usage::

    db = Database.from_settings(settings)          # or Database(conninfo, pool_size=4)
    with db.connection() as conn:                  # one transaction: commit / rollback
        conn.execute("select 1")
    db.close()
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from mdhub.config import Settings

__all__ = ["Database", "DictConnection", "redact_conninfo"]

type DictConnection = psycopg.Connection[DictRow]
"""A psycopg connection whose rows are ``dict[str, Any]``."""


def redact_conninfo(conninfo: str) -> str:
    """Return ``conninfo`` without the password (safe to log / print)."""
    try:
        params = conninfo_to_dict(conninfo)
    except psycopg.ProgrammingError:
        return "<invalid conninfo>"
    params.pop("password", None)
    return make_conninfo(**{k: str(v) for k, v in params.items() if v is not None})


def _configure(conn: psycopg.Connection[Any]) -> None:
    autocommit = conn.autocommit
    conn.autocommit = True
    conn.execute("SET TIME ZONE 'UTC'")
    conn.autocommit = autocommit


class Database:
    """Connection factory, optionally backed by a :class:`psycopg_pool.ConnectionPool`.

    :param conninfo: libpq URL or ``key=value`` conninfo string.
    :param pool_size: ``None`` (default) opens a fresh connection per :meth:`connection`
        call - right for CLI one-shots; an ``int`` keeps a pool of up to that many
        connections - right for the long-running scheduler and the API.

    Thread-safe: each :meth:`connection` call hands out its own connection.
    """

    def __init__(self, conninfo: str, *, pool_size: int | None = None) -> None:
        params = conninfo_to_dict(conninfo)
        params.setdefault("application_name", "mdhub")
        self._conninfo = make_conninfo(**{k: str(v) for k, v in params.items() if v is not None})
        self._pool: ConnectionPool[DictConnection] | None = None
        if pool_size is not None:
            if pool_size < 1:
                raise ValueError("pool_size must be >= 1")
            self._pool = ConnectionPool(
                self._conninfo,
                connection_class=psycopg.Connection[DictRow],
                kwargs={"row_factory": dict_row, "autocommit": False},
                min_size=1,
                max_size=pool_size,
                configure=_configure,
                open=True,
                name="mdhub",
            )

    @classmethod
    def from_settings(cls, settings: Settings, *, pool_size: int | None = None) -> Self:
        """Build from ``settings.database_url`` (raises ``ConfigError`` when unset)."""
        return cls(settings.require_database_url(), pool_size=pool_size)

    @property
    def conninfo(self) -> str:
        """Full conninfo (contains the password - do not log; use :attr:`redacted`)."""
        return self._conninfo

    @property
    def redacted(self) -> str:
        """Conninfo without the password, safe to log."""
        return redact_conninfo(self._conninfo)

    def connect(self, *, autocommit: bool = False) -> DictConnection:
        """Open a new dedicated (non-pooled) connection; the caller must close it.

        Use for session-scoped state such as advisory locks.
        """
        conn = psycopg.connect(self._conninfo, row_factory=dict_row, autocommit=autocommit)
        _configure(conn)
        return conn

    @contextmanager
    def connection(self) -> Iterator[DictConnection]:
        """Yield a connection inside a transaction.

        The transaction is committed when the block exits normally and rolled back when it
        raises. The connection is returned to the pool (or closed) afterwards.
        """
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
            return
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except BaseException:
            with suppress(psycopg.Error):
                conn.rollback()
            raise
        finally:
            conn.close()

    def close(self) -> None:
        """Close the pool (no-op without a pool)."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
