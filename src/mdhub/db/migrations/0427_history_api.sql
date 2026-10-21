-- 0427_history_api: index for the history read API (/v1/history/curves/{dataset}, DESIGN §8).
-- The end-of-day rule picks, per (snapshot_date, source), the latest snapshot of a dataset
-- (DISTINCT ON ... ORDER BY fetched_at DESC, id DESC) over a date range; the existing indexes
-- are by fetched_at only. The other history endpoints use the primary keys and the
-- (secid, trade_date) / (index_code, fixing_date, source) / (ccy, effective_date) indexes.
CREATE INDEX IF NOT EXISTS curve_snapshot_history_idx
    ON md.curve_snapshot (dataset, snapshot_date, source, fetched_at DESC, id DESC);
