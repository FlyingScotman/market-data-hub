-- 0840_dataset_history_load: state of the automatic history load of datasets that are new to the
-- hub (market-data-hub#138, owner decision of 2026-11-11): the history of a dataset whose earliest
-- stored day is later than its registered history_start is backfilled through the ordinary
-- backfill queue (ops.backfill_task, source 'history_load'), and the eod_final cuts of the last
-- 12 months are reissued ONCE afterwards so that past cuts contain the new series. One row per
-- dataset, written only by the snapshot_heal job (mdhub.quality.history_load); a dataset with a row
-- is never loaded again, so the load and the reissue happen once.

CREATE TABLE IF NOT EXISTS ops.dataset_history_load (
    dataset         text PRIMARY KEY,
    backfill_job    text NOT NULL,
    status          text NOT NULL CHECK (status IN ('loading', 'reissue', 'done')),
    history_from    date NOT NULL,
    head_to         date NOT NULL,
    first_before    date,
    first_after     date,
    load_round      integer NOT NULL DEFAULT 1 CHECK (load_round >= 1),
    pass_started_at timestamptz,
    hub_version     text NOT NULL,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    note            text
);
COMMENT ON TABLE ops.dataset_history_load IS
    'Automatic history load of datasets new to the hub (snapshot_heal): loading = backfill tasks '
    'of source history_load are open, reissue = the history is loaded and the eod_final cuts of '
    'the last 12 months are being reissued once, done = finished (never repeated)';
COMMENT ON COLUMN ops.dataset_history_load.history_from IS 'DatasetSpec.history_start when detected';
COMMENT ON COLUMN ops.dataset_history_load.head_to IS
    'Last day of the loaded range: the day before the earliest stored day (or the expected latest day of an empty table)';
COMMENT ON COLUMN ops.dataset_history_load.first_before IS 'Earliest stored day when detected (NULL: no rows)';
COMMENT ON COLUMN ops.dataset_history_load.first_after IS 'Earliest stored day when the load finished';
COMMENT ON COLUMN ops.dataset_history_load.pass_started_at IS
    'When the one-off reissue pass that covers this dataset started (gaps snapshot_cut_new_series)';
