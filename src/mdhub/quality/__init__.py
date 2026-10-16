"""Data quality: freshness and gap checks, backfill queue and worker (DESIGN §6).

* :mod:`mdhub.quality.checks` - dataset probing, expected dates, gap scan, range compression,
  ``ops.data_gap`` bookkeeping and ``ops.backfill_task`` queueing (driven by
  :mod:`mdhub.datasets`),
* :mod:`mdhub.quality.health` - job health vs the schedule (:data:`mdhub.schedule.RULES`),
* :mod:`mdhub.quality.report` - the structured report stored in ``ops.quality_report``,
* :mod:`mdhub.quality.job` - the ``quality`` job,
* :mod:`mdhub.quality.backfill` - the ``backfill_worker`` job.

Modules here are imported by :func:`mdhub.jobs.discover_jobs`.
"""
