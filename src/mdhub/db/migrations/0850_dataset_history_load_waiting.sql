-- 0850_dataset_history_load_waiting: two more facts of the automatic history load
-- (ops.dataset_history_load, 0840; market-data-hub#138): a load whose open tasks all wait for a
-- source that is not configured (credentials missing, source switched off) says so and does not
-- hold the one-off reissue pass of the other datasets back, and a load that ended with ranges it
-- could not load remembers how many (shown as an unfillable gap, not as a finished one).

ALTER TABLE ops.dataset_history_load
    ADD COLUMN IF NOT EXISTS waiting_reason text,
    ADD COLUMN IF NOT EXISTS failed_ranges integer NOT NULL DEFAULT 0 CHECK (failed_ranges >= 0);

COMMENT ON COLUMN ops.dataset_history_load.waiting_reason IS
    'Set while every open task of the load waits for a source that is not configured (credentials); such a load does not hold the reissue pass of other datasets back';
COMMENT ON COLUMN ops.dataset_history_load.failed_ranges IS
    'Ranges that could not be loaded after all retry rounds (the load ended with them missing)';
