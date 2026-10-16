-- 0301_legacy_sources: providers that appear only in the legacy history (mdhub legacy map-md).
-- CFETS / China Money FR007 and FDR007 IRS closing curves (one capture each, 2026-08-29).

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('cfets', 'CFETS / China Money', 'https://www.chinamoney.com.cn',
     'Legacy history only (FR007 / FDR007 IRS closing curves)')
ON CONFLICT (code) DO NOTHING;
