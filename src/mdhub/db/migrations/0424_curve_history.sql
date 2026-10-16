-- Row history for curve snapshots and quotes.
--
-- Until now md.curve_snapshot / md.curve_quote were append-only (live loaders insert a new
-- snapshot when content changes and never update one). The andromeda CNY history importer
-- (mdhub.legacy.andromeda_cny) upserts one snapshot per (dataset, date) and must revise a day
-- in place when a later export changes it; DESIGN §2 requires the previous image in
-- ops.row_history. Quote rows a revision removes are written to ops.row_history by the
-- importer itself (the trigger covers UPDATE only).
CALL ops.enable_history('md.curve_snapshot', ARRAY['source', 'dataset', 'fetched_at']);
CALL ops.enable_history('md.curve_quote', ARRAY['snapshot_id', 'tenor']);
