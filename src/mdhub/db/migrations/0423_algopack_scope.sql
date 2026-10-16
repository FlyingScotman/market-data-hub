-- 0423_algopack_scope: AlgoPack scope "option b" (owner decision 2026-10-16).
-- Order-book obstats are no longer collected: drop md.algopack_fo_obstats with everything that
-- belongs to it (indexes algopack_fo_obstats_date_idx / _capture_idx, the mdhub_row_history
-- trigger and its primary key go with the table; no view or function depends on it), its
-- ops.row_history images and its raw captures; close its bookkeeping. Tradestats and FUTOI
-- stay, as a rolling window (MDHUB_ALGOPACK_RETENTION_DAYS, default 90 calendar days) enforced
-- by `maintenance mode=retention` - no schema change needed for that: capture_id of both
-- tables is ON DELETE SET NULL (0210) and indexed (0421), and the history trigger is AFTER
-- UPDATE only, so retention DELETEs write no ops.row_history rows. History stays enabled on
-- md.algopack_fo_tradestats / md.algopack_futoi (revisions inside the window are kept;
-- retention deletes the images of rows before the window).
-- Earlier migrations stay untouched. Safe on a populated database; plain DROP (no CASCADE)
-- fails closed if something unexpected depends on the table.

-- ------------------------------------------------------------ md.algopack_fo_obstats
DROP TABLE IF EXISTS md.algopack_fo_obstats;

DELETE FROM ops.row_history WHERE table_name = 'md.algopack_fo_obstats';

-- Raw captures written only by the obstats requests of algopack_fx_futures (polls, whole-day
-- re-reads, backfill). Nothing else references them once the table is gone.
DELETE FROM ops.raw_capture
WHERE source = 'moex_apim'
  AND endpoint IN (
      'intraday:algopack_fo_obstats',
      'intraday:reread:algopack_fo_obstats',
      'algopack_fo_obstats_history'
  );

-- ------------------------------------------------------- ops bookkeeping of the dataset
-- The dataset algopack_fo_obstats is no longer registered: its open gaps are closed as
-- not_expected (kept for the record), open tasks cancelled, its coverage row removed.
UPDATE ops.data_gap
SET status = 'not_expected',
    note = 'dataset algopack_fo_obstats dropped (0423, AlgoPack scope: tradestats + FUTOI only)'
WHERE dataset = 'algopack_fo_obstats' AND status = 'open';

UPDATE ops.backfill_task
SET status = 'cancelled',
    done_at = now(),
    last_error = 'dataset algopack_fo_obstats dropped (0423)'
WHERE dataset = 'algopack_fo_obstats' AND status IN ('pending', 'running');

DELETE FROM ops.dataset_status WHERE dataset = 'algopack_fo_obstats';

-- ---------------------------------------------------------------- comments (rolling window)
COMMENT ON TABLE md.algopack_fo_tradestats IS
    'APIM AlgoPack /datashop/algopack/fo/tradestats, 5-minute bars of FX futures; one row per '
    '(secid, interval). Rolling window: trade dates of the last MDHUB_ALGOPACK_RETENTION_DAYS '
    'calendar days (default 90), older rows deleted by maintenance retention (0423)';
COMMENT ON TABLE md.algopack_futoi IS
    'APIM /analyticalproducts/futoi: open interest by client group, 5-minute, FX futures '
    'assets. Rolling window: trade dates of the last MDHUB_ALGOPACK_RETENTION_DAYS calendar '
    'days (default 90), older rows deleted by maintenance retention (0423)';
