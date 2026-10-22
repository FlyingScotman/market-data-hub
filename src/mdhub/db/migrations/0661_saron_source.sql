-- 0661_saron_source: the SNB data portal as a source (SARON, % p.a., md.fixing index_code SARON;
-- mdhub.loaders.ref_rates saron_fixing) and the job's source for ops.v_source_status.
--   snb   data.snb.ch: cube snbgwdzid (official interest rates; SARON as fixed by SIX)

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('snb', 'Swiss National Bank data portal', 'https://data.snb.ch',
     'Public statistics; SARON is administered by SIX Swiss Exchange (republished by the SNB)')
ON CONFLICT (code) DO NOTHING;

INSERT INTO ops.job_source (job, source) VALUES
    ('saron_fixing', 'snb')
ON CONFLICT DO NOTHING;
