-- The NTPro observation job reads this public source directly. Its MOEX inputs are
-- previously captured by their own jobs; do not claim this job polls those sources.
INSERT INTO ops.job_source (job, source) VALUES ('fx_input_pairs', 'ntpro_public')
ON CONFLICT DO NOTHING;
