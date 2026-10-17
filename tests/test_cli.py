from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from psycopg.conninfo import conninfo_to_dict
from typer.testing import CliRunner

from mdhub import __version__, cli
from mdhub.db.migrate import discover_migrations
from mdhub.jobs import REGISTRY, JobContext

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_log_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI installs a stderr handler; keep test logging untouched.
    monkeypatch.setattr(cli, "configure_logging", lambda _s: None)


def test_version() -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0 and result.output.strip() == __version__
    assert __version__ != "0.1.0"  # from git tags (hatch-vcs), not hard-coded


def test_migrate_twice(empty_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDHUB_DATABASE_URL", empty_db_url)
    first = runner.invoke(cli.app, ["migrate"])
    assert first.exit_code == 0, first.output
    assert "applied 0001_core" in first.output
    second = runner.invoke(cli.app, ["migrate"])
    assert second.exit_code == 0
    assert f"0 applied, {len(discover_migrations())} already applied" in second.output


def test_migrate_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDHUB_DATABASE_URL", raising=False)
    result = runner.invoke(cli.app, ["migrate"])
    assert result.exit_code == 2
    assert "MDHUB_DATABASE_URL is not set" in result.output


def test_jobs_list_without_database() -> None:
    result = runner.invoke(cli.app, ["jobs", "list"])
    assert result.exit_code == 0, result.output
    assert "bonds_eod" in result.output and "maintenance" in result.output
    assert "15 7 * * *" in result.output


def test_db_info_hides_password(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    url = db_url + " password=topsecretpw"
    monkeypatch.setenv("MDHUB_DATABASE_URL", url)
    result = runner.invoke(cli.app, ["db-info"])
    assert "topsecretpw" not in result.output
    assert conninfo_to_dict(url)["password"] == "topsecretpw"
    # pgserver trust auth ignores the password, so the command succeeds.
    assert result.exit_code == 0, result.output
    assert f"{len(discover_migrations())} applied, 0 pending" in result.output


@pytest.fixture
def cli_job() -> Iterator[str]:
    def cli_unit_job(ctx: JobContext, *, n: int = 1) -> None:
        if n < 0:
            raise ValueError("negative")

    REGISTRY.register("cli_unit_job", "test", cli_unit_job)
    yield "cli_unit_job"
    REGISTRY.unregister("cli_unit_job")


def test_run_job(db_url: str, monkeypatch: pytest.MonkeyPatch, cli_job: str) -> None:
    monkeypatch.setenv("MDHUB_DATABASE_URL", db_url)
    ok = runner.invoke(cli.app, ["run", cli_job, "--param", "n=2"])
    assert ok.exit_code == 0, ok.output
    assert "cli_unit_job: succeeded" in ok.output
    failed = runner.invoke(cli.app, ["run", cli_job, "-p", "n=-1"])
    assert failed.exit_code == 1
    bad = runner.invoke(cli.app, ["run", cli_job, "-p", "zzz=1"])
    assert bad.exit_code == 2 and "unknown parameter" in bad.output
    unknown = runner.invoke(cli.app, ["run", "no_such_job"])
    assert unknown.exit_code == 2 and "unknown job" in unknown.output


def test_cli_extensions_are_registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "fake_cli_ext"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "legacy_tools.py").write_text(
        textwrap.dedent(
            """
            import typer
            app = typer.Typer()

            @app.command()
            def hello(who: str = "world") -> None:
                typer.echo(f"hello {who}")

            @app.command()
            def bye() -> None:
                typer.echo("bye")
            """
        )
    )
    (pkg / "named.py").write_text(
        "import typer\nname = 'custom'\napp = typer.Typer()\n"
        "@app.command()\ndef ping() -> None:\n    typer.echo('pong')\n"
    )
    (pkg / "not_cli.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        root = typer.Typer()

        @root.callback()
        def _cb() -> None: ...

        added = cli.register_extensions(root, "fake_cli_ext")
        assert sorted(added) == ["custom", "legacy-tools"]
        out = runner.invoke(root, ["legacy-tools", "hello", "--who", "mdhub"])
        assert out.exit_code == 0 and "hello mdhub" in out.output
        assert runner.invoke(root, ["custom", "ping"]).output.strip() == "pong"
    finally:
        for name in [m for m in sys.modules if m.startswith("fake_cli_ext")]:
            del sys.modules[name]
