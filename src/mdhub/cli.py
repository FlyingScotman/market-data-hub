"""``mdhub`` command-line interface.

Commands: ``migrate``, ``jobs list``, ``run <job> [--param k=v ...]``, ``scheduler``,
``updater``, ``db-info``. Modules in :mod:`mdhub.cli_ext` exposing ``app: typer.Typer`` are added as
sub-command groups automatically (see ``docs/DEVELOPING.md``).
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer
from psycopg.rows import tuple_row

from mdhub import __version__, credentials, job_requests
from mdhub.config import ConfigError, Settings
from mdhub.core.bizday import DbCalendar
from mdhub.core.capture import reap_orphaned_runs
from mdhub.core.heartbeat import Heartbeat
from mdhub.core.redact import SecretRedactingFilter
from mdhub.db.migrate import MigrationError, applied_migrations, discover_migrations, migrate
from mdhub.db.pool import Database
from mdhub.hostmon import pgstats
from mdhub.hostmon.pgstats import PgStatsSampler
from mdhub.jobs import (
    REGISTRY,
    JobParamError,
    UnknownJobError,
    discover_jobs,
    parse_param_pairs,
    run_job,
    running_jobs,
)
from mdhub.schedule import build_scheduler, check_rules, effective_rules

log = logging.getLogger("mdhub.cli")

app = typer.Typer(
    name="mdhub",
    help="market-data-hub: loaders, schedule and PostgreSQL store.",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Inspect registered jobs.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")


def configure_logging(settings: Settings) -> None:
    """Root logging to stderr at ``MDHUB_LOG_LEVEL`` with secret redaction."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    handler.addFilter(SecretRedactingFilter(settings.secret_values()))
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_mdhub", False):
            root.removeHandler(h)
    handler._mdhub = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def _settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # pydantic ValidationError: never echo values
        typer.echo(f"invalid configuration: {exc.__class__.__name__}", err=True)
        raise typer.Exit(2) from None


def _database(settings: Settings, *, pool_size: int | None = None) -> Database:
    try:
        return Database.from_settings(settings, pool_size=pool_size)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


def _credentials(
    settings: Settings, db: Database, *, worker: bool
) -> credentials.CredentialStore | None:
    try:
        return credentials.activate(settings, db, worker=worker)
    except ConfigError as exc:
        db.close()
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version", help="Print version and exit.", is_eager=True, callback=_version_callback
        ),
    ] = False,
) -> None:
    configure_logging(_settings())


@app.command("migrate")
def migrate_cmd(
    directory: Annotated[
        Path | None, typer.Option("--dir", help="Migration directory (default: packaged).")
    ] = None,
    lock_timeout: Annotated[float, typer.Option(help="Seconds to wait for the lock.")] = 60.0,
) -> None:
    """Apply pending database migrations."""
    settings = _settings()
    db = _database(settings)
    try:
        report = migrate(db, directory=directory, lock_timeout=lock_timeout)
    except MigrationError as exc:
        typer.echo(f"migration failed: {exc}", err=True)
        raise typer.Exit(1) from None
    for v in report.applied:
        typer.echo(f"applied {v}")
    typer.echo(f"{len(report.applied)} applied, {len(report.already_applied)} already applied")
    for v in report.unknown_applied:
        typer.echo(f"warning: {v} applied in database but missing from code", err=True)


@jobs_app.command("list")
def jobs_list() -> None:
    """List scheduled and registered jobs."""
    report = discover_jobs()
    rules = effective_rules(_settings())
    names = sorted(set(rules) | set(REGISTRY.names()))
    width = max((len(n) for n in names), default=10)
    for name in names:
        registered = name in REGISTRY
        desc = REGISTRY.get(name).description if registered else "(not registered)"
        crons = ", ".join(r.cron for r in rules.get(name, ())) or "manual"
        typer.echo(f"{name:<{width}}  {'yes' if registered else 'no ':<3}  {crons}  - {desc}")
    for module, err in report.failed.items():
        typer.echo(f"import failed: {module}: {err}", err=True)
    for problem in check_rules(rules, REGISTRY):
        typer.echo(f"schedule problem: {problem}", err=True)


@app.command("run")
def run_cmd(
    job_name: Annotated[str, typer.Argument(metavar="JOB", help="Job name.")],
    param: Annotated[
        list[str] | None, typer.Option("--param", "-p", help="Job parameter key=value.")
    ] = None,
    no_lock: Annotated[
        bool, typer.Option("--no-lock", help="Do not take the per-job advisory lock.")
    ] = False,
) -> None:
    """Run one job now (recorded in ops.job_run)."""
    settings = _settings()
    report = discover_jobs()
    try:
        params = parse_param_pairs(param or [])
    except JobParamError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    if job_name not in REGISTRY:
        typer.echo(f"unknown job {job_name!r}", err=True)
        for module, err in report.failed.items():
            typer.echo(f"import failed: {module}: {err}", err=True)
        raise typer.Exit(2)
    db = _database(settings)
    _credentials(settings, db, worker=False)
    try:
        outcome = run_job(
            job_name,
            params,
            settings=settings,
            db=db,
            calendar=DbCalendar(db),
            exclusive=not no_lock,
            trigger="cli",
        )
    except (UnknownJobError, JobParamError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    except Exception as exc:
        typer.echo(f"job {job_name} failed: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    finally:
        db.close()
    typer.echo(f"{job_name}: {outcome.status} (run {outcome.run_id})")
    if outcome.status == "failed":
        raise typer.Exit(1)


@app.command("scheduler")
def scheduler_cmd(
    max_workers: Annotated[int, typer.Option(help="Concurrent job threads.")] = 8,
) -> None:
    """Run the blocking scheduler (DESIGN §6) until SIGTERM/SIGINT.

    Also writes the worker heartbeat (``ops.worker_heartbeat``, every 30 s) with the jobs
    running in this process, and runs the job requests of the admin console
    (``ops.job_request``, :mod:`mdhub.job_requests`). With ``MDHUB_SECRETS_PRIVATE_KEY`` it
    publishes its public key and uses the source credentials set in the console
    (:mod:`mdhub.credentials`). It also samples the PostgreSQL statistics for the Machine page
    every minute (:mod:`mdhub.hostmon.pgstats`)."""
    settings = _settings()
    report = discover_jobs()
    for module, err in report.failed.items():
        log.error("job module %s failed to import: %s", module, err)
    rules = effective_rules(settings)
    for problem in check_rules(rules, REGISTRY):
        log.warning("schedule problem: %s", problem)
    # Jobs open nested pooled connections (e.g. DbCalendar lookups inside a data transaction):
    # two per worker plus headroom avoids PoolTimeout with all workers busy.
    db = _database(settings, pool_size=2 * (max_workers + job_requests.MAX_PARALLEL) + 3)
    try:
        reap_orphaned_runs(db)
    except Exception:  # never block the scheduler start on housekeeping
        log.exception("could not reap orphaned job runs")
    store = _credentials(settings, db, worker=True)
    scheduler = build_scheduler(
        settings, db, calendar=DbCalendar(db), rules=rules, max_workers=max_workers
    )
    scheduler.add_job(
        PgStatsSampler(db).run_once, "interval", seconds=pgstats.INTERVAL_S, id="pgstats"
    )

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %d: shutting down scheduler", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    heartbeat = Heartbeat(db, "worker", running=running_jobs)
    heartbeat.start()
    requests = job_requests.JobRequestPoller(
        settings, db, calendar=DbCalendar(db), worker_instance=heartbeat.instance_id
    )
    requests.start()
    log.info("scheduler %s starting with %d trigger(s)", __version__, len(scheduler.get_jobs()))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        requests.stop()
        heartbeat.stop()
        if store is not None:
            store.stop()
        db.close()


@app.command("updater")
def updater_cmd() -> None:
    """Run the self-update agent (ADMIN_DESIGN §6) until SIGTERM/SIGINT.

    Executes the install and rollback requests of the admin console (``ops.update_request``)
    with docker compose; see :mod:`mdhub.updater`. No listening port."""
    import threading

    from mdhub import updater

    settings = _settings()
    if settings.database_url is None:
        typer.echo("MDHUB_DATABASE_URL is not set", err=True)
        raise typer.Exit(2)
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        # A step in progress is aborted; the next start resumes it from the recorded state.
        log.info("signal %d: stopping the updater", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        updater.run(settings, stop)
    except (KeyboardInterrupt, SystemExit):
        stop.set()


@app.command("db-info")
def db_info() -> None:
    """Show database connection, version and migration status (no secrets)."""
    settings = _settings()
    db = _database(settings)
    typer.echo(f"database: {db.redacted}")
    try:
        with db.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
            row = cur.execute("SELECT version(), current_database(), now()").fetchone()
            assert row is not None
            typer.echo(f"server:   {row[0]}")
            typer.echo(f"name:     {row[1]}")
            applied = applied_migrations(conn)
            known = [m.version for m in discover_migrations()]
            pending = [v for v in known if v not in applied]
            typer.echo(f"migrations: {len(applied)} applied, {len(pending)} pending")
            for v in pending:
                typer.echo(f"  pending {v}")
            if applied:
                runs: list[Any] = cur.execute(
                    """
                    SELECT status, count(*) FROM ops.job_run
                    WHERE started_at > now() - interval '24 hours'
                    GROUP BY status ORDER BY status
                    """
                ).fetchall()
                summary = ", ".join(f"{s}={n}" for s, n in runs) or "none"
                typer.echo(f"job runs (24h): {summary}")
    except Exception as exc:
        typer.echo(f"database error: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    finally:
        db.close()


# ----------------------------------------------------------------------- extensions


def register_extensions(target: typer.Typer, package: str = "mdhub.cli_ext") -> list[str]:
    """Add every ``app: typer.Typer`` found in modules of ``package`` as a sub-command group.

    The group name is the module's ``name`` attribute (``str``) or the module name with
    ``_`` -> ``-``. Modules starting with ``_`` are ignored. Import errors are reported on
    stderr and do not break the CLI. Returns the registered group names.
    """
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        return []
    added: list[str] = []
    for info in pkgutil.iter_modules(pkg.__path__, prefix=f"{package}."):
        short = info.name.rsplit(".", 1)[-1]
        if short.startswith("_"):
            continue
        try:
            module = importlib.import_module(info.name)
        except Exception as exc:
            print(f"mdhub: CLI extension {info.name} failed to import: {exc}", file=sys.stderr)
            continue
        sub = getattr(module, "app", None)
        if not isinstance(sub, typer.Typer):
            continue
        name = getattr(module, "name", None)
        group = name if isinstance(name, str) and name else short.replace("_", "-")
        target.add_typer(sub, name=group)
        added.append(group)
    return added


register_extensions(app)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
