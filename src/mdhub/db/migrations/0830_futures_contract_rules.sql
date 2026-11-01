-- Verified historical contract terms from dated MOEX specification documents.
ALTER TABLE ref.futures_contract DROP CONSTRAINT futures_contract_spec_source_check;
ALTER TABLE ref.futures_contract ADD CONSTRAINT futures_contract_spec_source_check
    CHECK (spec_source IN ('rfud', 'iss_description', 'history', 'moex_rules'));
COMMENT ON COLUMN ref.futures_contract.spec_source IS
    'rfud / iss_description / history; moex_rules = retained ISS dates plus tick terms from a hash-pinned MOEX specification (capture_id points to the document)';
INSERT INTO ops.source (code, name, base_url, licence_note) VALUES
    ('moex_rules', 'MOEX published contract specifications', 'https://www.moex.com',
     'Published contract rules; no marketdata entitlement is inferred')
ON CONFLICT (code) DO NOTHING;
