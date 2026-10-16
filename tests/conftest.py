"""Shared fixtures: a throw-away PostgreSQL 16 (pgserver) and per-test databases.

* ``pg_server`` (session): PostgreSQL cluster with its data dir in a pytest tmp dir.
* ``template_db`` (session): database ``mdhub_template`` with all migrations applied.
* ``db_url``: a fresh database cloned from the template for each test (dropped afterwards).
* ``empty_db_url``: a fresh database **without** migrations (for migration tests).
* ``conn``: psycopg connection (dict rows, non-autocommit) to ``db_url``.
* ``db``: :class:`mdhub.db.Database` for ``db_url``.
* ``settings``: :class:`mdhub.config.Settings` pointing at ``db_url`` with no credentials.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import pgserver
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from mdhub.config import Settings
from mdhub.db.migrate import migrate
from mdhub.db.pool import Database

TEMPLATE_DB = "mdhub_template"

# Never let a developer's real credentials leak into tests.
for _name in (
    "MDHUB_DATABASE_URL",
    "MDHUB_HTTP_PROXY",
    "MOEX_API_KEY",
    "MOEX_PASSPORT_LOGIN",
    "MOEX_PASSPORT_PASSWORD",
    "RATESCALC_USERNAME",
    "RATESCALC_PASSWORD",
    "CBONDS_LOGIN",
    "CBONDS_PASSWORD",
    "OTC_MONITOR_TOKEN",
    "MDHUB_API_TOKENS",
):
    os.environ.pop(_name, None)


class PgCluster:
    """Helper around the pgserver instance."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.base = server.get_uri()

    def url(self, dbname: str) -> str:
        return make_conninfo(self.base, dbname=dbname)

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.url("postgres"), autocommit=True)

    def create_db(self, template: str | None = None) -> str:
        name = f"t_{uuid.uuid4().hex[:16]}"
        with self.admin() as conn:
            if template:
                conn.execute(
                    sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                        sql.Identifier(name), sql.Identifier(template)
                    )
                )
            else:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        return name

    def drop_db(self, name: str) -> None:
        with self.admin() as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgCluster]:
    datadir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(datadir, cleanup_mode="stop")
    try:
        yield PgCluster(server)
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def template_db(pg_server: PgCluster) -> str:
    pg_server.drop_db(TEMPLATE_DB)
    with pg_server.admin() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEMPLATE_DB)))
    migrate(pg_server.url(TEMPLATE_DB))
    return TEMPLATE_DB


@pytest.fixture
def db_url(pg_server: PgCluster, template_db: str) -> Iterator[str]:
    name = pg_server.create_db(template=template_db)
    try:
        yield pg_server.url(name)
    finally:
        pg_server.drop_db(name)


@pytest.fixture
def empty_db_url(pg_server: PgCluster) -> Iterator[str]:
    name = pg_server.create_db()
    try:
        yield pg_server.url(name)
    finally:
        pg_server.drop_db(name)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    connection = psycopg.connect(db_url, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def db(db_url: str) -> Iterator[Database]:
    database = Database(db_url)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(database_url=db_url, iss_rps=1000.0)


@pytest.fixture
def today_is(monkeypatch: pytest.MonkeyPatch) -> Callable[[date], None]:
    """Pin "today" (MSK) for jobs (``JobContext.today``) and gap reconciliation."""
    import mdhub.jobs
    import mdhub.quality.checks

    def set_today(day: date) -> None:
        monkeypatch.setattr(mdhub.jobs, "msk_today", lambda: day)
        monkeypatch.setattr(mdhub.quality.checks, "msk_today", lambda: day)

    return set_today
