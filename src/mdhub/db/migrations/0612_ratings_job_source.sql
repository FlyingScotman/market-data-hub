-- Job ratings_agencies (WEB-18) reads the rating agencies' public disclosure (sources: 0120).
INSERT INTO ops.job_source (job, source) VALUES
    ('ratings_agencies', 'nkr'), ('ratings_agencies', 'nra'), ('ratings_agencies', 'raexpert')
ON CONFLICT DO NOTHING;
