"""The ``quality`` job (DESIGN §6: 08:30 and 20:30 on business days).

For every registered dataset (:func:`mdhub.datasets.all_datasets`): freshness check, gap scan,
``ops.data_gap`` update and backfill queueing (:mod:`mdhub.quality.checks`); then job health
(:mod:`mdhub.quality.health`) and the catch-up of scheduled fires that were skipped because
the job lock stayed busy (:func:`catch_up_missed`). The structured report is stored in
``ops.quality_report``,
``ops.dataset_status`` is refreshed (-> ``ops.v_coverage``), a summary is logged and the report
dict is returned (``JobOutcome.result``). The run itself succeeds when the checks ran - stale
data, gaps or failing jobs are reported, not raised; a dataset whose check errors (bad spec,
SQL error) makes the run ``partial``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from mdhub.datasets import DatasetSpec, all_datasets, get_dataset
from mdhub.jobs import REGISTRY, JobContext, JobParamError, job, run_job
from mdhub.quality.checks import DatasetCheck, queue_backfill, scan_dataset, sync_gaps
from mdhub.quality.health import JobHealth, catch_up_params, job_health
from mdhub.quality.report import QualityReport, store_report
from mdhub.schedule import SCHEDULED_LOCK_WAIT_S

__all__ = ["catch_up_missed", "quality", "run_quality"]


def _select(names: list[str] | None) -> list[DatasetSpec]:
    if names is None:
        return all_datasets()
    out: list[DatasetSpec] = []
    for n in names:
        try:
            out.append(get_dataset(n))
        except KeyError:
            raise JobParamError(f"unknown dataset {n!r}") from None
    return out


def _check_one(
    ctx: JobContext,
    spec: DatasetSpec,
    *,
    today: date,
    queue: bool,
    lookback_days: int | None,
) -> DatasetCheck:
    with ctx.connection() as conn:
        check = scan_dataset(
            conn, spec, today=today, calendar=ctx.calendar, lookback_days=lookback_days
        )
        synced = sync_gaps(conn, spec, check, run_id=ctx.run.id)
        check.gaps_opened, check.gaps_closed = synced.opened, synced.closed
        if queue and spec.backfill_job:
            if spec.backfill_job in REGISTRY:
                check.tasks_queued = queue_backfill(conn, spec, ctx.calendar)
            elif check.gaps:
                check.problem = f"backfill job {spec.backfill_job} is not registered"
    return check


def run_quality(
    ctx: JobContext,
    *,
    as_of: date | None = None,
    datasets: list[str] | None = None,
    queue: bool = True,
    lookback_days: int | None = None,
    check_jobs: bool = True,
    catch_up: bool = True,
) -> QualityReport:
    """Run all checks and store the report (used by the job and tests)."""
    today = as_of or ctx.today()
    report = QualityReport(as_of=today, run_id=ctx.run.id)
    for spec in _select(datasets):
        try:
            check = _check_one(ctx, spec, today=today, queue=queue, lookback_days=lookback_days)
        except Exception as exc:  # bad spec / SQL error: record it and check the rest
            ctx.logger.exception("quality check of %s failed", spec.name)
            ctx.run.mark_partial(f"{spec.name}: {type(exc).__name__}")
            check = DatasetCheck(
                dataset=spec.name,
                table=spec.table,
                frequency=spec.frequency,
                calendar=spec.calendar,
                backfill_job=spec.backfill_job,
                problem=f"check failed: {type(exc).__name__}: {exc}"[:500],
            )
        report.datasets.append(check)
        if check.is_stale:
            ctx.logger.warning(
                "dataset %s is stale: last %s, expected %s%s",
                check.dataset,
                check.last_date,
                check.expected_latest,
                f" (series {', '.join(check.stale_series)})" if check.stale_series else "",
            )
        if check.gaps:
            ctx.logger.warning(
                "dataset %s: %d gap(s), %d new, %d closed, %d task(s) queued",
                check.dataset,
                len(check.gaps),
                check.gaps_opened,
                check.gaps_closed,
                len(check.tasks_queued),
            )
    if check_jobs:
        with ctx.connection() as conn:
            report.jobs = job_health(
                conn,
                now=datetime.now(ctx.settings.zone),
                calendar=ctx.calendar,
                tz=ctx.settings.zone,
            )
        for j in report.job_problems:
            ctx.logger.warning(
                "job %s: %s (last %s at %s, %d consecutive failures)",
                j.job,
                j.status,
                j.last_status,
                j.last_started_at,
                j.consecutive_failures,
            )
        if catch_up:
            report.catch_up = catch_up_missed(ctx, report.jobs)
    with ctx.connection() as conn:
        store_report(conn, report)
    ctx.logger.info("quality: %s", report.summary_line())
    return report


def catch_up_missed(
    ctx: JobContext, jobs: list[JobHealth], *, lock_wait_s: float = SCHEDULED_LOCK_WAIT_S
) -> list[dict[str, Any]]:
    """Re-run scheduled fires that were skipped because the job lock stayed busy
    (:attr:`JobHealth.missed_by_lock`, rules with ``catch_up``). Returns one entry per job."""
    out: list[dict[str, Any]] = []
    for j in jobs:
        rule, fire = j.expected_rule, j.last_expected_at
        if not j.missed_by_lock or rule is None or fire is None or not rule.catch_up:
            continue
        if j.job == ctx.job or j.job not in REGISTRY:
            continue
        accepted, _ = REGISTRY.get(j.job).accepted_params()
        params = catch_up_params(
            rule, fire, calendar=ctx.calendar, tz=ctx.settings.zone, accepted=accepted
        )
        entry: dict[str, Any] = {"job": j.job, "missed_fire": fire.isoformat(), "params": params}
        if params is None:
            entry["status"] = "not_redone: days_back of an earlier day, job takes no dates"
            out.append(entry)
            continue
        ctx.logger.warning(
            "re-running %s (fire %s skipped: lock busy) with %s", j.job, fire, params
        )
        try:
            outcome = run_job(
                j.job,
                params,
                settings=ctx.settings,
                db=ctx.db,
                calendar=ctx.calendar,
                http_factory=ctx.http_factory,
                lock_wait_s=lock_wait_s,
            )
        except Exception as exc:
            ctx.logger.exception("catch-up run of %s failed", j.job)
            ctx.run.mark_partial(f"catch-up {j.job}: {type(exc).__name__}")
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        else:
            entry.update(status=outcome.status, run_id=outcome.run_id)
        out.append(entry)
    return out


@job("quality", "Freshness, gap scan, backfill queueing and job health for all datasets")
def quality(
    ctx: JobContext,
    *,
    as_of: date | None = None,
    datasets: list[str] | None = None,
    queue: bool = True,
    lookback_days: int | None = None,
    check_jobs: bool = True,
    catch_up: bool = True,
) -> dict[str, Any]:
    """Run the quality checks; returns the report dict (also in ``ops.quality_report``).

    :param as_of: check as of this date instead of today (MSK).
    :param datasets: restrict to these dataset names (default: all registered).
    :param queue: queue ``ops.backfill_task`` rows for open gaps.
    :param lookback_days: limit the gap scan to the last N days (all datasets).
    :param check_jobs: include job health.
    :param catch_up: re-run scheduled fires that were skipped with ``lock_busy``.
    """
    report = run_quality(
        ctx,
        as_of=as_of,
        datasets=datasets,
        queue=queue,
        lookback_days=lookback_days,
        check_jobs=check_jobs,
        catch_up=catch_up,
    )
    return report.to_dict()
