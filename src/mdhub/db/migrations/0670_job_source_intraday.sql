-- 0670_job_source_intraday: loader jobs of the RGBI futures monitor inputs -> their sources
-- (ops.v_source_status). All read public ISS; futures_intraday already maps to moex_apim/moex_iss;
-- bond_close_quote derives from stored rows (no source).

INSERT INTO ops.job_source (job, source) VALUES
    ('zcyc_securities',     'moex_iss'),
    ('index_intraday',      'moex_iss'),
    ('index_constituents',  'moex_iss'),
    ('repo_ccp_bond',       'moex_iss'),
    ('repo_gc_term',        'moex_iss')
ON CONFLICT DO NOTHING;
