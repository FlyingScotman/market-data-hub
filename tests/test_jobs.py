from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow

from mdhub.config import Settings
from mdhub.core.errors import JobSkipped
from mdhub.core.upsert import UpsertResult
from mdhub.db.pool import Database
from mdhub.jobs import (
    REGISTRY,
    JobContext,
    JobRegistry,
    UnknownJobError,
    coerce_params,
    discover_jobs,
    job,
    parse_param_pairs,
    run_job,
)

Conn = psycopg.Connection[DictRow]


def sample(
    ctx: JobContext,
    *,
    day: date | None = None,
    days_back: list[int] | None = None,
    limit: int = 10,
    ratio: Decimal = Decimal(1),
    dry: bool = False,
    mode: str = "final",
) -> dict[str, Any]:
    return {"day": day, "days_back": days_back, "limit": limit, "ratio": ratio, "dry": dry}


def test_parse_and_coerce_params() -> None:
    raw = parse_param_pairs(["day=2026-10-15", "days_back=1,2", "limit=5", "dry=yes", "ratio=1.5"])
    params = coerce_params(sample, raw)
    assert params == {
        "day": date(2026, 10, 15),
        "days_back": [1, 2],
        "limit": 5,
        "dry": True,
        "ratio": Decimal("1.5"),
    }
    assert coerce_params(sample, {"days_back": "[0]", "day": "none"}) == {
        "days_back": [0],
        "day": None,
    }
    assert coerce_params(sample, {"days_back": [1, 2]}) == {"days_back": [1, 2]}
    with pytest.raises(ValueError, match="unknown parameter"):
        coerce_params(sample, {"nope": "1"})
    with pytest.raises(ValueError, match="cannot convert"):
        coerce_params(sample, {"limit": "x"})
    with pytest.raises(ValueError, match="key=value"):
        parse_param_pairs(["novalue"])

    def needs(ctx: JobContext, *, required: int) -> None: ...

    with pytest.raises(ValueError, match="missing required"):
        coerce_params(needs, {})


def test_registry_rules() -> None:
    reg = JobRegistry()
    reg.register("sample", "d", sample)
    reg.register("sample", "d2", sample)  # same function: allowed (reload)
    assert reg.get("sample").description == "d2"
    with pytest.raises(ValueError, match="already registered"):
        reg.register("sample", "d", parse_param_pairs)
    with pytest.raises(ValueError, match="invalid job name"):
        reg.register("Bad-Name", "d", sample)
    with pytest.raises(UnknownJobError):
        reg.get("missing")
    names, var_kw = reg.get("sample").accepted_params()
    assert "days_back" in names and "ctx" not in names and not var_kw


def test_run_job_success_records_run(db: Database, conn: Conn, settings: Settings) -> None:
    reg = JobRegistry()

    @job_in(reg, "unit_ok")
    def unit_ok(ctx: JobContext, *, limit: int = 1) -> int:
        assert ctx.job == "unit_ok" and ctx.params == {"limit": 3}
        assert ctx.settings is settings
        with ctx.connection() as c:
            c.execute("CREATE TABLE md.jobtest (k int PRIMARY KEY)")
        ctx.run.add_counts(UpsertResult(inserted=limit))
        assert ctx.business_dates([0, 1], today=date(2026, 10, 19)) == [
            date(2026, 10, 19),
            date(2026, 10, 16),
        ]
        with ctx.http() as http:
            assert http.settings is settings
        return limit * 2

    outcome = run_job("unit_ok", {"limit": "3"}, settings=settings, db=db, registry=reg)
    assert (outcome.status, outcome.result) == ("succeeded", 6)
    row = conn.execute("SELECT * FROM ops.job_run WHERE id = %s", (outcome.run_id,)).fetchone()
    assert row is not None and row["rows_inserted"] == 3 and row["params"] == {"limit": 3}


def test_run_job_failure_and_skip(db: Database, conn: Conn, settings: Settings) -> None:
    reg = JobRegistry()

    @job_in(reg, "unit_fail")
    def unit_fail(ctx: JobContext) -> None:
        raise RuntimeError("kaput")

    @job_in(reg, "unit_skip")
    def unit_skip(ctx: JobContext) -> None:
        raise JobSkipped("disabled")

    with pytest.raises(RuntimeError, match="kaput"):
        run_job("unit_fail", settings=settings, db=db, registry=reg)
    assert run_job("unit_skip", settings=settings, db=db, registry=reg).status == "skipped"
    statuses = conn.execute("SELECT job, status FROM ops.job_run ORDER BY id").fetchall()
    assert statuses == [
        {"job": "unit_fail", "status": "failed"},
        {"job": "unit_skip", "status": "skipped"},
    ]
    with pytest.raises(ValueError, match="unknown parameter"):
        run_job("unit_skip", {"x": "1"}, settings=settings, db=db, registry=reg)


def test_run_job_is_exclusive(db_url: str, db: Database, settings: Settings) -> None:
    reg = JobRegistry()
    calls: list[int] = []

    @job_in(reg, "unit_lock")
    def unit_lock(ctx: JobContext) -> None:
        calls.append(1)

    with psycopg.connect(db_url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended('mdhub.job:unit_lock', 0))")
        outcome = run_job("unit_lock", settings=settings, db=db, registry=reg)
        assert outcome.status == "skipped" and calls == []
        assert (
            run_job("unit_lock", settings=settings, db=db, registry=reg, exclusive=False).status
            == "succeeded"
        )
    assert run_job("unit_lock", settings=settings, db=db, registry=reg).status == "succeeded"
    assert calls == [1, 1]


@pytest.fixture
def fake_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    pkg = tmp_path / "fakeloaders_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(
        textwrap.dedent(
            """
            from mdhub.jobs import job

            @job("fake_discovered_job", "registered via discovery")
            def run(ctx):
                return None
            """
        )
    )
    (pkg / "broken.py").write_text("raise ImportError('boom')\n")
    (pkg / "_private.py").write_text("raise RuntimeError('must not be imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "fakeloaders_pkg"
    REGISTRY.unregister("fake_discovered_job")
    for name in [m for m in sys.modules if m.startswith("fakeloaders_pkg")]:
        del sys.modules[name]


def test_discover_jobs(fake_pkg: str) -> None:
    report = discover_jobs((fake_pkg, "no_such_pkg_xyz"))
    assert report.imported == [f"{fake_pkg}.good"]
    assert list(report.failed) == [f"{fake_pkg}.broken"]
    assert "fake_discovered_job" in REGISTRY
    with pytest.raises(ImportError):
        discover_jobs((fake_pkg,), strict=True)


def test_discover_default_packages_imports_cleanly() -> None:
    report = discover_jobs()
    assert report.failed == {}


def job_in(reg: JobRegistry, name: str) -> Any:
    def deco(fn: Any) -> Any:
        reg.register(name, f"test job {name}", fn)
        return fn

    return deco


def test_job_decorator_registers_globally() -> None:
    @job("unit_global_job", "desc")
    def unit_global_job(ctx: JobContext) -> None: ...

    try:
        assert REGISTRY.get("unit_global_job").fn is unit_global_job
        assert REGISTRY.get("unit_global_job").module == __name__
    finally:
        REGISTRY.unregister("unit_global_job")
