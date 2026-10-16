"""The structured quality report: built by the ``quality`` job, stored in
``ops.quality_report`` (one row per run) and ``ops.dataset_status`` (one row per dataset)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mdhub.core.capture import json_default
from mdhub.quality.checks import DatasetCheck
from mdhub.quality.health import JobHealth

__all__ = ["QualityReport", "store_report"]


@dataclass(slots=True)
class QualityReport:
    as_of: date
    run_id: int | None
    datasets: list[DatasetCheck] = field(default_factory=list)
    jobs: list[JobHealth] = field(default_factory=list)

    @property
    def stale(self) -> list[DatasetCheck]:
        return [d for d in self.datasets if d.is_stale]

    @property
    def gap_count(self) -> int:
        return sum(len(d.gaps) for d in self.datasets)

    @property
    def tasks_queued(self) -> int:
        return sum(len(d.tasks_queued) for d in self.datasets)

    @property
    def job_problems(self) -> list[JobHealth]:
        return [j for j in self.jobs if j.problem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "run_id": self.run_id,
            "summary": {
                "datasets": len(self.datasets),
                "stale": [d.dataset for d in self.stale],
                "gaps": self.gap_count,
                "tasks_queued": self.tasks_queued,
                "job_problems": [f"{j.job}:{j.status}" for j in self.job_problems],
                "dataset_problems": {
                    d.dataset: d.problem for d in self.datasets if d.problem is not None
                },
            },
            "datasets": [d.to_dict() for d in self.datasets],
            "jobs": [j.to_dict() for j in self.jobs],
        }

    def summary_line(self) -> str:
        parts = [
            f"{len(self.datasets)} datasets",
            f"{len(self.stale)} stale",
            f"{self.gap_count} gaps",
            f"{self.tasks_queued} tasks queued",
            f"{len(self.job_problems)} job problems",
        ]
        details = [d.dataset for d in self.stale] + [
            f"{j.job}:{j.status}" for j in self.job_problems
        ]
        return ", ".join(parts) + (f" ({', '.join(details)})" if details else "")


def store_report(conn: psycopg.Connection[Any], report: QualityReport) -> None:
    """Upsert ``ops.dataset_status`` rows and insert the ``ops.quality_report`` row."""
    for d in report.datasets:
        conn.execute(
            """
            INSERT INTO ops.dataset_status (dataset, table_name, frequency, calendar,
                backfill_job, first_date, last_date, row_count, expected_latest, is_stale,
                gap_days, problem, checked_at, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (dataset) DO UPDATE SET
                table_name = EXCLUDED.table_name, frequency = EXCLUDED.frequency,
                calendar = EXCLUDED.calendar, backfill_job = EXCLUDED.backfill_job,
                first_date = EXCLUDED.first_date, last_date = EXCLUDED.last_date,
                row_count = EXCLUDED.row_count, expected_latest = EXCLUDED.expected_latest,
                is_stale = EXCLUDED.is_stale, gap_days = EXCLUDED.gap_days,
                problem = EXCLUDED.problem, checked_at = EXCLUDED.checked_at,
                run_id = EXCLUDED.run_id
            """,
            (
                d.dataset,
                d.table,
                d.frequency,
                d.calendar,
                d.backfill_job,
                d.first_date,
                d.last_date,
                d.row_count,
                d.expected_latest,
                d.is_stale,
                len(d.gaps),
                d.problem,
                report.run_id,
            ),
        )
    if report.run_id is None:
        return
    payload = report.to_dict()
    conn.execute(
        """
        INSERT INTO ops.quality_report (run_id, as_of, stale_count, gap_count, tasks_queued,
            job_problems, report)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id) DO UPDATE SET report = EXCLUDED.report
        """,
        (
            report.run_id,
            report.as_of,
            len(report.stale),
            report.gap_count,
            report.tasks_queued,
            len(report.job_problems),
            Jsonb(payload, dumps=lambda v: json.dumps(v, default=json_default)),
        ),
    )
