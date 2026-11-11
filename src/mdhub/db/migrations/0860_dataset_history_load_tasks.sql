-- 0860_dataset_history_load_tasks: a history load remembers which backfill tasks are its own
-- (ops.dataset_history_load, 0840; market-data-hub#138). Tasks used to be matched to a load by
-- overlap with its head, so a new load of a dataset whose job is shared picked up the old,
-- long-finished (and possibly failed) tasks of an earlier load of the same job, re-queued their
-- ranges and could close as unfillable. Now the load keeps the ids of the tasks that cover its
-- head; later rounds follow the same (dataset, params) ranges.

ALTER TABLE ops.dataset_history_load
    ADD COLUMN IF NOT EXISTS task_ids bigint[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN ops.dataset_history_load.task_ids IS
    'ops.backfill_task ids queued (or already open and reused) for this load''s head when it was detected; progress is judged on the newest task of each of their (dataset, params) ranges';
