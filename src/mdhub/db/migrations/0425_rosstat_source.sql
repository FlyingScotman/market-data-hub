-- 0425_rosstat_source: Rosstat (Federal State Statistics Service) as a source.
-- Monthly consumer price index (CPI to the previous month, sheet "01" of ipc_mes_MM-YYYY.xlsx)
-- -> md.fixing index_code CPI_RU_MOM (mdhub.loaders.rosstat).

INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('rosstat', 'Rosstat', 'https://rosstat.gov.ru', 'Public official statistics')
ON CONFLICT (code) DO NOTHING;
