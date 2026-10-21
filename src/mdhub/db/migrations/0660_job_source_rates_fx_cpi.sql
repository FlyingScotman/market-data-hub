-- 0660_job_source_rates_fx_cpi: loader jobs of the international reference rates, cross rates,
-- proxy curves and CBR inflation (WEB-16) -> the sources they fetch from (ops.v_source_status).
-- A job with a fallback chain reads several sources.

INSERT INTO ops.job_source (job, source) VALUES
    ('estr_fixing',   'ecb_data_portal'),
    ('estr_fixing',   'bundesbank'),
    ('estr_fixing',   'dbnomics'),
    ('estr_fixing',   'fred'),
    ('sofr_fixing',   'nyfed'),
    ('sofr_fixing',   'fred'),
    ('ecb_fx',        'ecb_data_portal'),
    ('fx_cross',      'fred'),
    ('proxy_curves',  'us_treasury'),
    ('proxy_curves',  'ecb_data_portal'),
    ('proxy_curves',  'bundesbank'),
    ('cbr_inflation', 'cbr')
ON CONFLICT DO NOTHING;
